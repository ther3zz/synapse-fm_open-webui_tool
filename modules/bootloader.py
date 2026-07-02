"""
bootloader.py - Persistent player bootloader for Open WebUI index.html.

Generates a <script> block that gets injected into Open WebUI's index.html
during tool initialization. The bootloader creates a floating audio player
bar in the parent page DOM and listens for postMessage commands from tool
iframe responses.

This approach avoids all srcdoc iframe streaming limitations (Firefox
Fission timeouts, MediaSource decode failures, blob segmentation gaps)
by running audio playback directly in the main page context.

Security:
- Stream keys are received via postMessage (never in localStorage/DOM)
- All text rendered via textContent (no innerHTML)
- Auth headers are closure-scoped (not accessible externally)
- URL validation enforces https: protocol
- Unique message channel prevents spoofing from unrelated scripts
"""

# Marker comments used to identify the bootloader in index.html
BOOTLOADER_START = "<!-- SynapseFM Player Bootloader -->"
BOOTLOADER_END_TAG = "</script>"
BOOTLOADER_ID = "synapsefm-player-bootloader"


def _compute_bootloader_version():
    """Compute a short hash of the bootloader content for version tracking."""
    import hashlib
    return hashlib.sha256(BOOTLOADER_SCRIPT.encode("utf-8")).hexdigest()[:12]

# Paths where Open WebUI's index.html might be found
INDEX_PATHS = [
    "/app/backend/open_webui/frontend/index.html",
    "/app/backend/build/index.html",
    "/app/backend/public/index.html",
    "/app/build/index.html",
    "../build/index.html",
    "./build/index.html",
    "../backend/public/index.html",
    "./backend/public/index.html",
]

# Note: BOOTLOADER_SCRIPT uses a data-version attribute set dynamically
# by patch_frontend_index() / ensure_bootloader() using _compute_bootloader_version().
# This allows detection of outdated bootloaders after tool upgrades.
BOOTLOADER_SCRIPT = """
    <!-- SynapseFM Player Bootloader -->
    <script id="synapsefm-player-bootloader" data-version="{version}">
    (function() {
        'use strict';

        // -- State -------------------------------------------------------
        var playerEl = null;
        var audio = null;
        var isPlaying = false;
        var currentConfig = null;
        var pollTimer = null;
        var abortCtrl = null;
        var dismissedNonce = null; // tracks dismissed request nonce
        // -- state (Chrome -----------------------------------------------
        var mediaSource = null;
        var sourceBuffer = null;
        var msQueue = [];
        var msAppending = false;
        // Firefox detection
        var isFirefox = navigator.userAgent.indexOf('Firefox') !== -1;

        // -- Message Listener --------------------------------------------
        window.addEventListener('message', function(evt) {
            if (!evt.data || typeof evt.data !== 'object') return;

            if (evt.data.type === 'synapsefm-ping') {
                evt.source.postMessage({ type: 'synapsefm-pong' }, '*');
                return;
            }

            if (evt.data.type === 'synapsefm-play') {
                var cfg = evt.data.config;
                if (!cfg || !cfg.streamUrl || !cfg.streamKey || !cfg.stationId) return;

                // Validate URL (enforce https)
                try {
                    var u = new URL(cfg.streamUrl);
                    if (u.protocol !== 'https:') return;
                } catch(e) { return; }

                // Sanitize string fields (length limits)
                cfg.stationName = String(cfg.stationName || 'SynapseFM').slice(0, 100);
                cfg.stationGenre = String(cfg.stationGenre || '').slice(0, 50);
                cfg.nowPlayingUrl = String(cfg.nowPlayingUrl || '');

                // Skip if this exact request was dismissed (chat re-render)
                // Each play_station() call generates a unique nonce;
                // re-rendering the same chat replays the same nonce.
                var nonce = String(cfg.nonce || '');
                if (nonce && nonce === dismissedNonce) {
                    return;
                }
                dismissedNonce = null;

                // If already playing the same station, don't restart.
                // This prevents chat navigation from tearing down
                // an active stream (which causes 429s from rapid reconnects).
                if (currentConfig && currentConfig.stationId === cfg.stationId
                    && (isPlaying || blobPlaying)) {
                    // Update nonce so close tracks the latest request
                    currentConfig.nonce = nonce;
                    return;
                }

                handlePlay(cfg);
            }
        });

        // -- Play Handler ------------------------------------------------
        function handlePlay(cfg) {
            stopPlayback();
            currentConfig = cfg;
            ensurePlayerUI();
            updateStationInfo(cfg.stationName, cfg.stationGenre);
            setStatus('Buffering...');
            startStream();
            startNowPlayingPoll();
        }

        // -- Player UI --------------------------------------------------------
        function ensurePlayerUI() {
            if (playerEl) return;

            playerEl = document.createElement('div');
            playerEl.id = 'synapsefm-player';

            // Inject styles
            var style = document.createElement('style');
            style.id = 'synapsefm-player-styles';
            style.textContent = [
                '#synapsefm-wrap {',
                '  position: fixed; top: 0; left: 50%; transform: translateX(-50%);',
                '  z-index: 99999; transition: transform 0.35s cubic-bezier(0.4, 0, 0.2, 1);',
                '}',
                '#synapsefm-wrap.collapsed { transform: translateX(-50%) translateY(-100%); }',
                '#synapsefm-player {',
                '  display: flex; align-items: center; gap: 10px;',
                '  background: linear-gradient(135deg, rgba(255,255,255,0.08), rgba(255,255,255,0.02));',
                '  backdrop-filter: blur(28px) saturate(1.6);',
                '  -webkit-backdrop-filter: blur(28px) saturate(1.6);',
                '  border: 1px solid rgba(255,255,255,0.15); border-top: none;',
                '  border-radius: 0 0 16px 16px;',
                '  padding: 10px 18px; min-width: 300px; max-width: 520px;',
                '  box-shadow: 0 8px 32px rgba(0,0,0,0.3),',
                '    0 1px 0 rgba(255,255,255,0.1) inset,',
                '    0 -1px 0 rgba(255,255,255,0.04) inset;',
                '  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;',
                '  color: #e4e4e7; font-size: 13px;',
                '}',
                '#synapsefm-tab {',
                '  position: absolute; bottom: -26px; left: 50%;',
                '  transform: translateX(-50%);',
                '  background: linear-gradient(135deg, rgba(255,255,255,0.07), rgba(255,255,255,0.02));',
                '  backdrop-filter: blur(28px) saturate(1.6);',
                '  -webkit-backdrop-filter: blur(28px) saturate(1.6);',
                '  border: 1px solid rgba(255,255,255,0.12); border-top: none;',
                '  border-radius: 0 0 10px 10px; padding: 3px 14px;',
                '  cursor: pointer; color: rgba(161,161,170,0.8); font-size: 11px;',
                '  font-family: -apple-system, BlinkMacSystemFont, sans-serif;',
                '  transition: color 0.2s, background 0.2s;',
                '  user-select: none; white-space: nowrap;',
                '}',
                '#synapsefm-tab:hover { color: #e4e4e7; background: rgba(255,255,255,0.08); }',
                '#synapsefm-player .sfm-icon { flex-shrink: 0; width: 36px; height: 36px; display: flex; align-items: center; justify-content: center; font-size: 16px; }',
                '#synapsefm-player .sfm-icon img { width: 36px; height: 36px; border-radius: 6px; object-fit: cover; }',
                '#synapsefm-player .sfm-info { flex: 1; min-width: 0; overflow: hidden; }',
                '#synapsefm-player .sfm-station {',
                '  font-weight: 600; font-size: 12px; white-space: nowrap;',
                '  overflow: hidden; text-overflow: ellipsis;',
                '  color: rgba(244,244,245,0.95); letter-spacing: 0.2px;',
                '}',
                '#synapsefm-player .sfm-track {',
                '  font-size: 11px; color: rgba(161,161,170,0.85);',
                '  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px;',
                '}',
                '#synapsefm-player .sfm-status {',
                '  font-size: 10px; color: rgba(113,113,122,0.9); margin-top: 2px;',
                '}',
                '#synapsefm-player .sfm-status.live {',
                '  color: #34d399; text-shadow: 0 0 8px rgba(52,211,153,0.3);',
                '}',
                '#synapsefm-player .sfm-btn {',
                '  background: rgba(255,255,255,0.06);',
                '  border: 1px solid rgba(255,255,255,0.1);',
                '  color: #e4e4e7; cursor: pointer;',
                '  font-size: 15px; padding: 5px 6px; line-height: 1; border-radius: 8px;',
                '  transition: background 0.2s, border-color 0.2s, transform 0.1s;',
                '}',
                '#synapsefm-player .sfm-btn:hover {',
                '  background: rgba(255,255,255,0.14); border-color: rgba(255,255,255,0.22);',
                '}',
                '#synapsefm-player .sfm-btn:active { transform: scale(0.94); }',
                '#synapsefm-player .sfm-vol {',
                '  width: 54px; height: 4px; -webkit-appearance: none; appearance: none;',
                '  background: rgba(255,255,255,0.12); border-radius: 2px; outline: none;',
                '  cursor: pointer;',
                '}',
                '#synapsefm-player .sfm-vol::-webkit-slider-thumb {',
                '  -webkit-appearance: none; width: 12px; height: 12px;',
                '  border-radius: 50%; background: linear-gradient(135deg, #e4e4e7, #a1a1aa);',
                '  cursor: pointer; box-shadow: 0 1px 4px rgba(0,0,0,0.3);',
                '}',
                '#synapsefm-player .sfm-vol::-moz-range-thumb {',
                '  width: 12px; height: 12px; border-radius: 50%;',
                '  background: linear-gradient(135deg, #e4e4e7, #a1a1aa);',
                '  cursor: pointer; border: none; box-shadow: 0 1px 4px rgba(0,0,0,0.3);',
                '}',
                '@media (max-width: 768px) {',
                '  #synapsefm-wrap { left: 50%; }',
                '  #synapsefm-player { max-width: calc(100vw - 32px); min-width: 0; }',
                '}',
            ].join('\\n');

            if (!document.getElementById('synapsefm-player-styles')) {
                document.head.appendChild(style);
            }

            // Build DOM via createElement (no innerHTML)
            // Outer wrapper for collapse animation
            var wrap = document.createElement('div');
            wrap.id = 'synapsefm-wrap';

            var icon = document.createElement('span');
            icon.className = 'sfm-icon';
            icon.id = 'sfm-artwork';
            // Show station artwork if available, otherwise emoji
            if (currentConfig && currentConfig.stationImage) {
                var img = document.createElement('img');
                img.src = currentConfig.stationImage;
                img.alt = '';
                img.draggable = false;
                icon.appendChild(img);
            } else {
                icon.textContent = '\\uD83D\\uDCFB'; // 📻
            }

            var info = document.createElement('div');
            info.className = 'sfm-info';

            var stationEl = document.createElement('div');
            stationEl.className = 'sfm-station';
            stationEl.id = 'sfm-station-name';
            stationEl.textContent = 'SynapseFM';

            var trackEl = document.createElement('div');
            trackEl.className = 'sfm-track';
            trackEl.id = 'sfm-track-title';
            trackEl.textContent = 'Loading...';

            var statusEl = document.createElement('div');
            statusEl.className = 'sfm-status';
            statusEl.id = 'sfm-status';
            statusEl.textContent = '';

            info.appendChild(stationEl);
            info.appendChild(trackEl);
            info.appendChild(statusEl);

            var playBtn = document.createElement('button');
            playBtn.className = 'sfm-btn';
            playBtn.id = 'sfm-play-btn';
            playBtn.textContent = '\\u23F8'; // ⏸
            playBtn.title = 'Play/Pause';
            playBtn.addEventListener('click', togglePlayback);

            var volInput = document.createElement('input');
            volInput.type = 'range';
            volInput.className = 'sfm-vol';
            volInput.id = 'sfm-volume';
            volInput.min = '0';
            volInput.max = '100';
            volInput.value = '80';
            volInput.title = 'Volume';
            volInput.addEventListener('input', function() {
                if (audio) audio.volume = this.value / 100;
            });

            var closeBtn = document.createElement('button');
            closeBtn.className = 'sfm-btn';
            closeBtn.textContent = '\\u2715'; // ✕
            closeBtn.title = 'Close player';
            closeBtn.addEventListener('click', function() {
                // Record nonce so chat re-renders don't reopen
                if (currentConfig && currentConfig.nonce) {
                    dismissedNonce = currentConfig.nonce;
                }
                stopPlayback();
                stopNowPlayingPoll();
                destroyPlayerUI();
            });

            // Collapse/expand toggle tab
            var tab = document.createElement('div');
            tab.id = 'synapsefm-tab';
            tab.textContent = '\\u25B2 SynapseFM'; // up arrow when expanded
            tab.addEventListener('click', function() {
                var isCollapsed = wrap.classList.toggle('collapsed');
                tab.textContent = (isCollapsed ? '\\u25BC' : '\\u25B2') + ' SynapseFM';
            });

            playerEl.appendChild(icon);
            playerEl.appendChild(info);
            playerEl.appendChild(playBtn);
            playerEl.appendChild(volInput);
            playerEl.appendChild(closeBtn);

            wrap.appendChild(playerEl);
            wrap.appendChild(tab);
            document.body.appendChild(wrap);
        }

        function destroyPlayerUI() {
            var wrap = document.getElementById('synapsefm-wrap');
            if (wrap && wrap.parentNode) {
                wrap.parentNode.removeChild(wrap);
            }
            playerEl = null;
            currentConfig = null;
        }

        function updateStationInfo(name, genre) {
            var el = document.getElementById('sfm-station-name');
            if (el) el.textContent = (name || 'SynapseFM') + (genre ? ' -- ' + genre : '');
        }

        function updateArtwork(imageUrl) {
            var el = document.getElementById('sfm-artwork');
            if (!el) return;
            if (imageUrl) {
                // Replace contents with img
                while (el.firstChild) el.removeChild(el.firstChild);
                var img = document.createElement('img');
                img.src = imageUrl;
                img.alt = '';
                img.draggable = false;
                el.appendChild(img);
            }
        }

        function updateTrackTitle(title) {
            var el = document.getElementById('sfm-track-title');
            if (el) el.textContent = title || 'Loading...';
        }

        function setStatus(text, isLive) {
            var el = document.getElementById('sfm-status');
            if (!el) return;
            el.textContent = text;
            el.className = 'sfm-status' + (isLive ? ' live' : '');
        }

        // -- Playback Toggle ---------------------------------------------
        function togglePlayback() {
            if (!currentConfig) return;
            var btn = document.getElementById('sfm-play-btn');

            if (isPlaying) {
                stopPlayback();
                stopNowPlayingPoll();
                setStatus('Paused');
                if (btn) btn.textContent = '\\u25B6'; // ▶
            } else {
                setStatus('Buffering...');
                if (btn) btn.textContent = '\\u23F8'; // ⏸
                startStream();
                startNowPlayingPoll();
            }
        }

        // -- Audio Streaming ---------------------------------------------
        // Chrome: MediaSource + appendBuffer (low-latency, gapless)
        // Firefox: fetch -> ReadableStream -> blob queue (native decoder)

        var canMediaSource = (
            !isFirefox &&
            typeof MediaSource !== 'undefined' &&
            MediaSource.isTypeSupported('audio/mpeg')
        );

        function startStream() {
            if (!currentConfig) return;

            if (canMediaSource) {
                startMediaSourceStream();
            } else {
                startFetchBlobStream();
            }
        }


        // -- Chrome: MediaSource Path ------------------------------------
        var msPruneInterval = null;

        function startMediaSourceStream() {
            var cfg = currentConfig;

            // -- up any previous MediaSource state ---------------------------
            stopPlayback();
            currentConfig = cfg; // restore after stopPlayback clears it

            mediaSource = new MediaSource();
            audio = new Audio();
            audio.volume = (document.getElementById('sfm-volume') || {value:80}).value / 100;
            audio.src = URL.createObjectURL(mediaSource);

            // If MediaSource fails, fall back to blob path
            audio.addEventListener('error', function() {
                canMediaSource = false;
                stopPlayback();
                currentConfig = cfg;
                startFetchBlobStream();
            });

            mediaSource.addEventListener('sourceopen', function() {
                try {
                    sourceBuffer = mediaSource.addSourceBuffer('audio/mpeg');
                } catch(e) {
                    canMediaSource = false;
                    stopPlayback();
                    currentConfig = cfg;
                    startFetchBlobStream();
                    return;
                }

                msQueue = [];
                msAppending = false;

                function appendNext() {
                    if (msQueue.length === 0 || msAppending) return;
                    if (sourceBuffer.updating) return;
                    msAppending = true;
                    var chunk = msQueue.shift();
                    try {
                        sourceBuffer.appendBuffer(chunk);
                    } catch(e) {
                        msAppending = false;
                        msQueue.unshift(chunk);
                        pruneBuffer();
                    }
                }

                function pruneBuffer() {
                    if (sourceBuffer.updating) return;
                    if (sourceBuffer.buffered.length === 0) return;
                    var ct = audio.currentTime || 0;
                    var start = sourceBuffer.buffered.start(0);
                    var removeEnd = Math.max(start, ct - 5);
                    if (removeEnd > start) {
                        try { sourceBuffer.remove(start, removeEnd); } catch(e) {}
                    }
                }

                sourceBuffer.addEventListener('updateend', function() {
                    msAppending = false;
                    appendNext();
                });

                // Periodic buffer pruning (every 30s)
                msPruneInterval = setInterval(pruneBuffer, 30000);

                abortCtrl = new AbortController();
                fetch(cfg.streamUrl, {
                    mode: 'cors',
                    headers: { 'Authorization': 'Bearer ' + cfg.streamKey },
                    signal: abortCtrl.signal,
                }).then(function(response) {
                    if (!response.ok) {
                        setStatus('Error: ' + response.status);
                        return;
                    }
                    var reader = response.body.getReader();
                    function pump() {
                        reader.read().then(function(result) {
                            if (result.done) {
                                // Stream closed by server - reconnect
                                reconnect();
                                return;
                            }
                            msQueue.push(result.value);
                            appendNext();
                            pump();
                        }).catch(function(err) {
                            if (err.name !== 'AbortError') {
                                // Network error - clean up and reconnect
                                if (msPruneInterval) {
                                    clearInterval(msPruneInterval);
                                    msPruneInterval = null;
                                }
                                reconnect();
                            }
                        });
                    }
                    pump();

                    audio.play().then(function() {
                        isPlaying = true;
                        setStatus('\\u25CF Live', true);
                    }).catch(function() {
                        setStatus('Click play to start');
                    });
                }).catch(function(err) {
                    if (err.name !== 'AbortError') {
                        reconnect();
                    }
                });
            });
        }


        // -- Firefox: Fetch + Blob Queue Path ----------------------------
        var blobQueue = [];
        var blobPlaying = false;
        var reconnectAttempts = 0;

        function startFetchBlobStream() {
            var cfg = currentConfig;
            if (!cfg) return;

            abortCtrl = new AbortController();

            fetch(cfg.streamUrl, {
                mode: 'cors',
                headers: { 'Authorization': 'Bearer ' + cfg.streamKey },
                signal: abortCtrl.signal,
            }).then(function(response) {
                if (!response.ok) {
                    setStatus('Error: ' + response.status);
                    return;
                }
                reconnectAttempts = 0;
                var reader = response.body.getReader();
                var chunks = [];
                var totalBytes = 0;

                function pump() {
                    reader.read().then(function(result) {
                        if (result.done) {
                            if (chunks.length > 0) enqueueBlobSegment(chunks);
                            reconnect();
                            return;
                        }

                        chunks.push(result.value);
                        totalBytes += result.value.length;

                        // Create segment every ~512KB (~32s of 128kbps audio)
                        if (totalBytes >= 524288) {
                            enqueueBlobSegment(chunks);
                            chunks = [];
                            totalBytes = 0;
                        }

                        pump();
                    }).catch(function(err) {
                        if (err.name !== 'AbortError') {
                            if (chunks.length > 0) {
                                enqueueBlobSegment(chunks);
                                chunks = [];
                            }
                            reconnect();
                        }
                    });
                }
                pump();
            }).catch(function(err) {
                if (err.name !== 'AbortError') {
                    reconnect();
                }
            });
        }

        function reconnect() {
            reconnectAttempts++;
            if (reconnectAttempts > 5) {
                setStatus('Connection lost');
                stopNowPlayingPoll();
                return;
            }
            // Exponential backoff: 1s, 2s, 4s, 8s, 16s
            var delay = Math.min(1000 * Math.pow(2, reconnectAttempts - 1), 16000);
            setStatus('Reconnecting (' + reconnectAttempts + '/5)...');
            setTimeout(function() {
                if (isPlaying || blobPlaying) startFetchBlobStream();
            }, delay);
        }

        function enqueueBlobSegment(chunks) {
            // Merge and find first MP3 frame sync (0xFFE0)
            var totalLen = 0;
            for (var i = 0; i < chunks.length; i++) totalLen += chunks[i].length;
            var merged = new Uint8Array(totalLen);
            var offset = 0;
            for (var i = 0; i < chunks.length; i++) {
                merged.set(chunks[i], offset);
                offset += chunks[i].length;
            }

            var syncOffset = 0;
            for (var i = 0; i < merged.length - 1; i++) {
                if (merged[i] === 0xFF && (merged[i + 1] & 0xE0) === 0xE0) {
                    syncOffset = i;
                    break;
                }
            }

            var aligned = syncOffset > 0 ? merged.slice(syncOffset) : merged;
            var blob = new Blob([aligned], { type: 'audio/mpeg' });
            var blobUrl = URL.createObjectURL(blob);
            blobQueue.push(blobUrl);

            while (blobQueue.length > 4) {
                URL.revokeObjectURL(blobQueue.shift());
            }

            if (!blobPlaying) playNextSegment();
        }

        // Double-buffered playback: pre-load the next segment while
        // the current one plays to minimize transition gaps.
        var nextAudio = null;

        function playNextSegment() {
            if (blobQueue.length === 0) {
                blobPlaying = false;
                return;
            }

            blobPlaying = true;
            var blobUrl = blobQueue.shift();
            var vol = (document.getElementById('sfm-volume') || {value:80}).value / 100;

            // Use pre-loaded audio if available, otherwise create new
            var newAudio;
            if (nextAudio && nextAudio._sfmUrl === blobUrl) {
                newAudio = nextAudio;
                nextAudio = null;
            } else {
                newAudio = new Audio();
                newAudio.volume = vol;
                newAudio.src = blobUrl;
            }

            // Swap: stop old audio, start new
            if (audio) {
                audio.onended = null;
                audio.onerror = null;
                audio.pause();
                if (audio.src && audio.src.indexOf('blob:') === 0) {
                    URL.revokeObjectURL(audio.src);
                }
            }
            audio = newAudio;

            audio.onended = function() {
                URL.revokeObjectURL(blobUrl);
                playNextSegment();
            };
            audio.onerror = function() {
                URL.revokeObjectURL(blobUrl);
                playNextSegment();
            };

            audio.play().then(function() {
                isPlaying = true;
                var btn = document.getElementById('sfm-play-btn');
                if (btn) btn.textContent = '\\u23F8';
                setStatus('\\u25CF Live', true);
                // Pre-load next segment while this one plays
                preloadNext();
            }).catch(function() {
                URL.revokeObjectURL(blobUrl);
                setStatus('Click play to start');
                blobPlaying = false;
            });
        }

        function preloadNext() {
            if (nextAudio || blobQueue.length === 0) return;
            var url = blobQueue[0]; // peek, don't shift
            var vol = (document.getElementById('sfm-volume') || {value:80}).value / 100;
            nextAudio = new Audio();
            nextAudio.volume = vol;
            nextAudio.preload = 'auto';
            nextAudio.src = url;
            nextAudio._sfmUrl = url;
        }

        // -- Stop Playback -----------------------------------------------
        function stopPlayback() {
            if (abortCtrl) {
                abortCtrl.abort();
                abortCtrl = null;
            }

            // Drain blob queue
            while (blobQueue.length > 0) {
                URL.revokeObjectURL(blobQueue.shift());
            }
            blobPlaying = false;
            reconnectAttempts = 0;

            // Clean up MediaSource
            mediaSource = null;
            sourceBuffer = null;
            msQueue = [];
            msAppending = false;

            if (audio) {
                audio.onended = null;
                audio.onerror = null;
                audio.pause();
                if (audio.src && audio.src.indexOf('blob:') === 0) {
                    URL.revokeObjectURL(audio.src);
                }
                audio.src = '';
                audio.load();
                audio = null;
            }
            isPlaying = false;

            var btn = document.getElementById('sfm-play-btn');
            if (btn) btn.textContent = '\\u25B6';
        }

        // -- Now Playing Polling -----------------------------------------
        function startNowPlayingPoll() {
            stopNowPlayingPoll();
            pollNow();
            pollTimer = setInterval(pollNow, 10000);
        }

        function stopNowPlayingPoll() {
            if (pollTimer) {
                clearInterval(pollTimer);
                pollTimer = null;
            }
        }

        function pollNow() {
            if (!currentConfig || !currentConfig.nowPlayingUrl) return;
            // Skip polling when not actively playing
            if (!isPlaying && !blobPlaying) return;
            fetch(currentConfig.nowPlayingUrl, {
                headers: { 'Authorization': 'Bearer ' + currentConfig.streamKey },
            }).then(function(r) {
                if (r.status === 429) {
                    // Rate limited -- back off by stopping poll temporarily
                    stopNowPlayingPoll();
                    setTimeout(function() {
                        if (isPlaying || blobPlaying) startNowPlayingPoll();
                    }, 60000);
                    return null;
                }
                return r.json();
            }).then(function(data) {
                if (!data) return;
                var np = data.nowPlaying;
                if (np && np.title) {
                    var display = np.title;
                    if (np.artist) display = np.artist + ' - ' + np.title;
                    if (np.style) display += ' \\u00B7 ' + np.style;
                    updateTrackTitle(display);
                } else {
                    updateTrackTitle('Waiting for track...');
                }
            }).catch(function() {});
        }

    })();
    </script>
"""


def strip_bootloader(content):
    """Remove existing SynapseFM bootloader from HTML content."""
    start_marker = BOOTLOADER_START
    if start_marker not in content:
        return content

    start_idx = content.find(start_marker)
    # Find the closing </script> after the start marker
    end_idx = content.find(BOOTLOADER_END_TAG, start_idx)
    if end_idx == -1:
        return content

    end_idx += len(BOOTLOADER_END_TAG)
    # Consume trailing newline if present
    if end_idx < len(content) and content[end_idx] == "\\n":
        end_idx += 1

    return content[:start_idx] + content[end_idx:]


def find_index_file():
    """Find the first existing Open WebUI index.html path.

    Priority:
    1. FRONTEND_BUILD_DIR env var (set by Open WebUI)
    2. Open WebUI config import (FRONTEND_BUILD_DIR from open_webui.config)
    3. Hardcoded search paths (covers common Docker layouts)
    """
    import os
    from pathlib import Path

    candidates = []

    # Try to import the exact path from Open WebUI's config module
    try:
        from open_webui.config import FRONTEND_BUILD_DIR as owui_build_dir
        idx = Path(owui_build_dir) / "index.html"
        candidates.append(str(idx))
    except Exception:
        pass

    # Check the environment variable
    env_dir = os.environ.get("FRONTEND_BUILD_DIR", "")
    if env_dir:
        candidates.append(os.path.join(env_dir, "index.html"))

    # Hardcoded search paths (Docker & dev layouts)
    candidates.extend(INDEX_PATHS)

    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _inject_bootloader(path):
    """Write the bootloader into the given index.html file."""
    import logging

    version = _compute_bootloader_version()
    script = BOOTLOADER_SCRIPT.replace("{version}", version)

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        # Strip any existing bootloader first (handles upgrades)
        content = strip_bootloader(content)

        if "</head>" not in content:
            logging.warning(
                f"[SynapseFM] index.html found at {path} but "
                "no </head> tag -- cannot inject bootloader"
            )
            return False

        new_content = content.replace("</head>", script + "</head>")
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        logging.info(
            f"[SynapseFM] Injected bootloader v{version} into {path}"
        )
        return True
    except PermissionError:
        logging.warning(
            f"[SynapseFM] Permission denied writing to {path} -- "
            "bootloader not injected. Player will use iframe fallback."
        )
    except Exception as e:
        logging.warning(
            f"[SynapseFM] Error injecting bootloader into {path}: {e}"
        )
    return False


def _is_bootloader_current(path):
    """Check if the bootloader in index.html matches the current version."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if BOOTLOADER_ID not in content:
            return False
        version = _compute_bootloader_version()
        return f'data-version="{version}"' in content
    except Exception:
        return False


def patch_frontend_index():
    """Inject the SynapseFM bootloader into Open WebUI's index.html."""
    import logging

    path = find_index_file()
    if not path:
        logging.warning(
            "[SynapseFM] Could not find index.html -- bootloader not injected. "
            "Player will use iframe fallback. "
            f"Searched: FRONTEND_BUILD_DIR env, open_webui.config, {INDEX_PATHS}"
        )
        return

    if _is_bootloader_current(path):
        import logging
        logging.info(f"[SynapseFM] Bootloader already current in {path}")
        return

    _inject_bootloader(path)


def ensure_bootloader():
    """
    Verify the bootloader is present and current; re-inject if needed.

    Call this before any operation that depends on the bootloader (e.g.
    play_station). This ensures the bootloader survives Open WebUI
    container updates that replace index.html, and also handles tool
    upgrades where the bootloader code itself has changed.

    This is lightweight -- it only reads the file to check a version
    hash. The file is only written when injection is actually needed.
    """
    path = find_index_file()
    if not path:
        return

    if not _is_bootloader_current(path):
        _inject_bootloader(path)

