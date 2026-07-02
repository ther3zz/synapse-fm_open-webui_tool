"""
player_builder.py -- HTML5 audio player builder for Open WebUI iframe.

Generates a compact embedded radio player as an HTMLResponse.
The player uses fetch() with Authorization headers to stream audio from
SynapseFM's External Streaming API, bypassing <audio> element header limits.

Security controls (OWASP XSS Prevention + HTML5 Security):
- CSP meta tag restricts script/media/connect/img sources
- All dynamic content inserted via textContent (JS), never innerHTML
- Station data pre-sanitized via sanitizer.sanitize_for_html()
- No inline event handlers (addEventListener only)
- Stream key is embedded ONLY in the iframe HTML (never in LLM context)
"""

from typing import Optional
import json
import uuid


def build_player_html(
    stream_url: str,
    stream_key: str,
    station_id: str,
    station_name: str,
    station_genre: str,
    now_playing_url: str,
    synapsefm_url: str,
    stations_json: str = "[]",
    station_image: str = "",
) -> str:
    """
    Build the complete HTML for the embedded audio player.

    All parameters MUST be pre-sanitized via sanitizer.sanitize_for_html()
    before being passed here. The only exception is stream_key, which is
    inserted into the JS fetch header and never rendered as visible text.

    Args:
        stream_url: Full URL to the stream endpoint
        stream_key: Bearer token for Authorization header (iframe-only)
        station_id: Current station UUID
        station_name: HTML-escaped station name
        station_genre: HTML-escaped station genre
        now_playing_url: Base URL for now-playing polling
        synapsefm_url: Base URL for CSP directives
        stations_json: JSON-encoded array of {{id, name, genre}} for switcher

    Returns:
        Complete HTML document string for Open WebUI's HTMLResponse.
    """
    # Config for postMessage to the parent-page bootloader
    config_json = json.dumps({
        "streamUrl": stream_url,
        "streamKey": stream_key,
        "stationId": station_id,
        "stationName": station_name,
        "stationGenre": station_genre,
        "nowPlayingUrl": now_playing_url,
        "stationImage": station_image,
        "nonce": uuid.uuid4().hex[:16],
    }).replace('</','<\\/')

    # The fallback player HTML (full iframe player for read-only deployments)
    fallback_html = _build_inner_player_html(
        stream_url=stream_url,
        stream_key=stream_key,
        station_id=station_id,
        station_name=station_name,
        station_genre=station_genre,
        now_playing_url=now_playing_url,
        synapsefm_url=synapsefm_url,
        stations_json=stations_json,
    )
    fallback_escaped = json.dumps(fallback_html).replace('</','<\\/')

    # Bootstrap: tries postMessage first, falls back to blob URL iframe
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;overflow:hidden;background:transparent">
<div id="sfm-msg" style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;
  font-size:13px;color:#a1a1aa;padding:12px;text-align:center">
  Starting player...
</div>
<script>
(function() {{
    var config = {config_json};
    var fallbackHtml = {fallback_escaped};
    var responded = false;

    // Listen for pong from bootloader
    function onPong(evt) {{
        if (evt.data && evt.data.type === 'synapsefm-pong') {{
            responded = true;
            window.removeEventListener('message', onPong);
            // Bootloader is present -- send config via postMessage
            window.parent.postMessage({{
                type: 'synapsefm-play',
                config: config
            }}, '*');
            // Show minimal confirmation in the iframe
            var msg = document.getElementById('sfm-msg');
            if (msg) msg.textContent = '\\uD83C\\uDFB5 Player started -- look for the floating player bar above.';
        }}
    }}
    window.addEventListener('message', onPong);

    // Ping the bootloader
    window.parent.postMessage({{ type: 'synapsefm-ping' }}, '*');

    // If no pong within 300ms, fall back to full iframe player
    setTimeout(function() {{
        if (!responded) {{
            window.removeEventListener('message', onPong);
            // No bootloader -- load the full iframe player
            var blob = new Blob([fallbackHtml], {{ type: 'text/html' }});
            var url = URL.createObjectURL(blob);
            var frame = document.createElement('iframe');
            frame.src = url;
            frame.allow = 'autoplay';
            frame.style.cssText = 'width:100%;height:100%;border:none;display:block;';
            document.body.style.height = '100%';
            document.documentElement.style.height = '100%';
            var msg = document.getElementById('sfm-msg');
            if (msg) msg.style.display = 'none';
            document.body.appendChild(frame);
        }}
    }}, 300);
}})();
</script>
</body>
</html>"""


def _build_inner_player_html(
    stream_url: str,
    stream_key: str,
    station_id: str,
    station_name: str,
    station_genre: str,
    now_playing_url: str,
    synapsefm_url: str,
    stations_json: str = "[]",
) -> str:
    """Build the inner player HTML document (hosted inside blob URL iframe)."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Security-Policy"
          content="default-src 'none';
                   script-src 'unsafe-inline';
                   style-src 'unsafe-inline';
                   media-src blob: {synapsefm_url};
                   connect-src {synapsefm_url};
                   img-src {synapsefm_url} data:;">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: transparent;
            color: #e2e8f0;
            padding: 4px;
        }}

        .player {{
            width: 100%;
            max-width: 500px;
            background: rgba(15, 23, 42, 0.9);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(148, 163, 184, 0.12);
            border-radius: 10px;
            padding: 10px 16px 10px 14px;
        }}

        /* -- Row 1: Brand + Station + Status -------------------------- */
        .row-top {{
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 8px;
        }}

        .brand {{
            font-size: 9px;
            font-weight: 700;
            color: #6366f1;
            letter-spacing: 0.8px;
            text-transform: uppercase;
            flex-shrink: 0;
        }}

        .station-info {{
            flex: 1;
            min-width: 0;
        }}

        .station-name {{
            font-size: 13px;
            font-weight: 600;
            color: #f1f5f9;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            line-height: 1.2;
        }}

        .station-genre {{
            font-size: 10px;
            color: #64748b;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            line-height: 1.2;
        }}

        #status {{
            font-size: 9px;
            color: #64748b;
            flex-shrink: 0;
            white-space: nowrap;
        }}

        #status.error {{ color: #f87171; }}
        #status.connected {{ color: #34d399; }}

        .live-dot {{
            display: inline-block;
            width: 5px;
            height: 5px;
            border-radius: 50%;
            background: #34d399;
            margin-right: 3px;
            animation: pulse 2s ease-in-out infinite;
        }}

        @keyframes pulse {{
            0%, 100% {{ opacity: 1; }}
            50% {{ opacity: 0.4; }}
        }}

        /* -- Row 2: Play + Now Playing + Volume ----------------------- */
        .row-controls {{
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 6px;
            overflow: hidden;
        }}

        .play-btn {{
            width: 30px;
            height: 30px;
            border-radius: 50%;
            border: none;
            background: linear-gradient(135deg, #6366f1, #8b5cf6);
            color: white;
            font-size: 12px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: transform 0.15s, box-shadow 0.15s;
            flex-shrink: 0;
        }}

        .play-btn:hover {{
            transform: scale(1.08);
            box-shadow: 0 0 12px rgba(99, 102, 241, 0.4);
        }}

        .play-btn:active {{
            transform: scale(0.95);
        }}

        .np-info {{
            flex: 1;
            min-width: 0;
        }}

        .np-title {{
            font-size: 12px;
            font-weight: 600;
            color: #e2e8f0;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            line-height: 1.3;
        }}

        .np-meta {{
            font-size: 10px;
            color: #64748b;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            line-height: 1.3;
        }}

        .volume-container {{
            display: flex;
            align-items: center;
            gap: 4px;
            flex: 0 0 auto;
        }}

        .volume-icon {{
            font-size: 10px;
            color: #94a3b8;
        }}

        .volume-slider {{
            width: 50px;
            -webkit-appearance: none;
            appearance: none;
            height: 3px;
            border-radius: 2px;
            background: rgba(148, 163, 184, 0.2);
            outline: none;
        }}

        .volume-slider::-webkit-slider-thumb {{
            -webkit-appearance: none;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #8b5cf6;
            cursor: pointer;
            border: 1.5px solid rgba(15, 23, 42, 0.8);
        }}

        .volume-slider::-moz-range-thumb {{
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #8b5cf6;
            cursor: pointer;
            border: 1.5px solid rgba(15, 23, 42, 0.8);
        }}

        /* -- Row 3: Station Switcher ---------------------------------- */
        .station-select {{
            width: 100%;
            padding: 4px 8px;
            background: rgba(30, 41, 59, 0.7);
            border: 1px solid rgba(148, 163, 184, 0.1);
            border-radius: 5px;
            color: #94a3b8;
            font-size: 10px;
            outline: none;
            cursor: pointer;
        }}

        .station-select:focus {{
            border-color: rgba(99, 102, 241, 0.4);
        }}
    </style>
</head>
<body>
    <div class="player">
        <div class="row-top">
            <span class="brand">SynapseFM</span>
            <div class="station-info">
                <div id="stationName" class="station-name"></div>
                <div id="stationGenre" class="station-genre"></div>
            </div>
            <span id="status">Ready</span>
        </div>

        <div class="row-controls">
            <button id="playBtn" class="play-btn" aria-label="Play/Pause">&#9654;</button>
            <div class="np-info">
                <div id="npTitle" class="np-title">Loading...</div>
                <div id="npMeta" class="np-meta"></div>
            </div>
            <div class="volume-container">
                <span class="volume-icon">&#128266;</span>
                <input id="volumeSlider" type="range" class="volume-slider"
                       min="0" max="100" value="75" aria-label="Volume">
            </div>
        </div>

        <select id="stationSelect" class="station-select" aria-label="Select Station">
            <option value="">Switch station...</option>
        </select>
    </div>

    <script>
    (function() {{
        'use strict';

        // -- Configuration (injected server-side) --------------------------
        const CONFIG = {{
            streamUrl: '{stream_url}',
            streamKey: '{stream_key}',
            stationId: '{station_id}',
            nowPlayingBase: '{now_playing_url}',
            stations: {stations_json},
            pollIntervalMs: 10000,
        }};

        // -- DOM References ------------------------------------------------
        const playBtn = document.getElementById('playBtn');
        const volumeSlider = document.getElementById('volumeSlider');
        const stationNameEl = document.getElementById('stationName');
        const stationGenreEl = document.getElementById('stationGenre');
        const npTitleEl = document.getElementById('npTitle');
        const npMetaEl = document.getElementById('npMeta');
        const stationSelect = document.getElementById('stationSelect');
        const statusEl = document.getElementById('status');

        // -- State ---------------------------------------------------------
        let audio = null;
        let isPlaying = false;
        let pollTimer = null;
        let currentStationId = CONFIG.stationId;

        // -- Init ----------------------------------------------------------

        // Set initial station info (textContent -- XSS safe)
        stationNameEl.textContent = '{station_name}';
        stationGenreEl.textContent = '{station_genre}';

        // Populate station selector (textContent -- XSS safe)
        CONFIG.stations.forEach(function(s) {{
            const opt = document.createElement('option');
            opt.value = s.id;
            opt.textContent = s.name + (s.genre ? ' -- ' + s.genre : '');
            if (s.id === currentStationId) opt.selected = true;
            stationSelect.appendChild(opt);
        }});

        // -- Audio Streaming -----------------------------------------------

        // Detect MediaSource MP3 support.
        // Firefox is explicitly excluded: it falsely reports
        // isTypeSupported('audio/mpeg') = true in newer versions
        // but then fails at runtime with "No decoders" errors.
        const isFirefox = navigator.userAgent.indexOf('Firefox') !== -1;
        const canMediaSource = (
            !isFirefox &&
            typeof MediaSource !== 'undefined' &&
            MediaSource.isTypeSupported('audio/mpeg')
        );

        function startStream() {{
            stopStream();

            const baseUrl = CONFIG.streamUrl.replace(
                CONFIG.stationId, currentStationId
            );

            if (canMediaSource && !mediaSourceFailed) {{
                startMediaSourceStream(baseUrl);
            }} else {{
                startFetchBlobStream(baseUrl);
            }}
        }}

        // -- Path 1: MediaSource (Chromium) -------------------------------
        // Continuous byte-level streaming. Track transitions in the Icecast
        // relay are seamless because the byte stream never breaks.

        let mediaSourceFailed = false;

        function startMediaSourceStream(url) {{
            const mediaSource = new MediaSource();
            audio = new Audio();
            audio.volume = volumeSlider.value / 100;
            audio.src = URL.createObjectURL(mediaSource);

            // Runtime decoder failure detection: Firefox may claim
            // isTypeSupported('audio/mpeg') = true but then fail to decode.
            // If the audio element errors, fall back to the blob path.
            audio.addEventListener('error', function() {{
                if (!mediaSourceFailed) {{
                    mediaSourceFailed = true;
                    stopStream();
                    startFetchBlobStream(url);
                }}
            }});

            mediaSource.addEventListener('sourceopen', function() {{
                let sourceBuffer;
                try {{
                    sourceBuffer = mediaSource.addSourceBuffer('audio/mpeg');
                }} catch(e) {{
                    startFetchBlobStream(url);
                    return;
                }}

                let queue = [];
                let appending = false;

                function appendNext() {{
                    if (queue.length === 0 || appending) return;
                    if (sourceBuffer.updating) return;
                    appending = true;
                    const chunk = queue.shift();
                    try {{
                        sourceBuffer.appendBuffer(chunk);
                    }} catch(e) {{
                        appending = false;
                        // QuotaExceededError -- re-queue chunk, prune, retry
                        queue.unshift(chunk);
                        pruneBuffer();
                    }}
                }}

                // Prune old buffered data to prevent memory growth.
                // After pruning completes, updateend fires -> appendNext retries.
                function pruneBuffer() {{
                    if (sourceBuffer.updating) return;
                    if (sourceBuffer.buffered.length === 0) return;
                    const currentTime = audio.currentTime || 0;
                    const start = sourceBuffer.buffered.start(0);
                    // Remove everything before currentTime minus 5s safety margin
                    const removeEnd = Math.max(start, currentTime - 5);
                    if (removeEnd > start) {{
                        try {{
                            sourceBuffer.remove(start, removeEnd);
                        }} catch(e) {{}}
                    }}
                }}

                sourceBuffer.addEventListener('updateend', function() {{
                    appending = false;
                    appendNext();
                }});

                // Periodically prune the buffer (every 30 seconds)
                const pruneTimer = setInterval(pruneBuffer, 30000);

                fetch(url, {{
                    mode: 'cors',
                    headers: {{
                        'Authorization': 'Bearer ' + CONFIG.streamKey,
                    }},
                }}).then(function(response) {{
                    if (!response.ok) {{
                        clearInterval(pruneTimer);
                        throw new Error('Stream error: ' + response.status);
                    }}
                    const reader = response.body.getReader();
                    setStatus('connected', ' Live', true);
                    isPlaying = true;
                    playBtn.textContent = '\u23F8';
                    audio.play().catch(function() {{}});

                    function pump() {{
                        reader.read().then(function(result) {{
                            if (result.done) {{
                                clearInterval(pruneTimer);
                                if (mediaSource.readyState === 'open') {{
                                    try {{ mediaSource.endOfStream(); }} catch(e) {{}}
                                }}
                                setStatus('', 'Ended', false);
                                return;
                            }}
                            queue.push(result.value);
                            appendNext();
                            pump();
                        }}).catch(function() {{
                            clearInterval(pruneTimer);
                            setStatus('error', 'Lost', false);
                        }});
                    }}
                    pump();
                }}).catch(function() {{
                    clearInterval(pruneTimer);
                    setStatus('error', 'Error', false);
                }});
            }});
        }}

        // -- Path 2: fetch + ReadableStream -> Blob queue (Firefox) ------
        // Firefox can decode audio/mpeg via native <audio> element, just
        // not through MediaSource. We read the stream incrementally via
        // getReader(), accumulate chunks into ~128KB blobs, and play them
        // sequentially. Each blob is a valid MP3 fragment that the native
        // decoder handles.
        //
        // IMPORTANT: Cannot use response.blob() -- it waits for the entire
        // response, which never completes for a live stream.

        let abortController = null;
        let blobQueue = [];
        let blobPlaying = false;
        let streamUrl = '';
        let reconnectAttempts = 0;
        const MAX_RECONNECTS = 5;

        function startFetchBlobStream(url) {{
            streamUrl = url;
            abortController = new AbortController();

            // Only clear queue and show buffering on first connect
            if (reconnectAttempts === 0) {{
                blobQueue = [];
                blobPlaying = false;
                setStatus('', 'Buffering...', false);
            }}

            fetch(url, {{
                mode: 'cors',
                headers: {{
                    'Authorization': 'Bearer ' + CONFIG.streamKey,
                }},
                signal: abortController.signal,
            }}).then(function(response) {{
                if (!response.ok) {{
                    throw new Error('Stream error: ' + response.status);
                }}
                // Connection succeeded -- reset retry counter
                reconnectAttempts = 0;

                const reader = response.body.getReader();
                let chunks = [];
                let totalBytes = 0;

                function pump() {{
                    reader.read().then(function(result) {{
                        if (result.done) {{
                            if (chunks.length > 0) {{
                                enqueueBlobSegment(chunks);
                            }}
                            // Stream ended gracefully -- try to reconnect
                            reconnect();
                            return;
                        }}

                        chunks.push(result.value);
                        totalBytes += result.value.length;

                        // Create a segment every ~256KB (~16s of 128kbps MP3).
                        if (totalBytes >= 262144) {{
                            enqueueBlobSegment(chunks);
                            chunks = [];
                            totalBytes = 0;
                        }}

                        pump();
                    }}).catch(function(err) {{
                        if (err.name !== 'AbortError') {{
                            // Flush any accumulated data before reconnecting
                            if (chunks.length > 0) {{
                                enqueueBlobSegment(chunks);
                                chunks = [];
                            }}
                            reconnect();
                        }}
                    }});
                }}
                pump();
            }}).catch(function(err) {{
                if (err.name !== 'AbortError') {{
                    reconnect();
                }}
            }});
        }}

        function reconnect() {{
            reconnectAttempts++;
            if (reconnectAttempts > MAX_RECONNECTS) {{
                setStatus('error', 'Lost', false);
                return;
            }}
            // Brief delay before reconnecting (1 second)
            setStatus('', 'Reconnecting...', false);
            setTimeout(function() {{
                if (isPlaying || blobPlaying) {{
                    startFetchBlobStream(streamUrl);
                }}
            }}, 1000);
        }}

        function enqueueBlobSegment(chunks) {{
            // Merge chunks into a single Uint8Array so we can find
            // the first valid MP3 frame sync (11 set bits = 0xFFE0).
            // This ensures each segment starts on a frame boundary,
            // preventing "No decoders" errors from partial frames.
            let totalLen = 0;
            for (let i = 0; i < chunks.length; i++) totalLen += chunks[i].length;
            const merged = new Uint8Array(totalLen);
            let offset = 0;
            for (let i = 0; i < chunks.length; i++) {{
                merged.set(chunks[i], offset);
                offset += chunks[i].length;
            }}

            // Find first MP3 frame sync
            let syncOffset = 0;
            for (let i = 0; i < merged.length - 1; i++) {{
                if (merged[i] === 0xFF && (merged[i + 1] & 0xE0) === 0xE0) {{
                    syncOffset = i;
                    break;
                }}
            }}

            const aligned = syncOffset > 0 ? merged.slice(syncOffset) : merged;
            const blob = new Blob([aligned], {{ type: 'audio/mpeg' }});
            const blobUrl = URL.createObjectURL(blob);
            blobQueue.push(blobUrl);

            // Limit queue to prevent memory buildup
            while (blobQueue.length > 4) {{
                URL.revokeObjectURL(blobQueue.shift());
            }}

            if (!blobPlaying) {{
                playNextSegment();
            }}
        }}

        function playNextSegment() {{
            if (blobQueue.length === 0) {{
                blobPlaying = false;
                return;
            }}

            blobPlaying = true;
            const blobUrl = blobQueue.shift();

            // Clean up previous audio element
            if (audio) {{
                audio.onended = null;
                audio.onerror = null;
                audio.pause();
                if (audio.src && audio.src.startsWith('blob:')) {{
                    URL.revokeObjectURL(audio.src);
                }}
            }}

            audio = new Audio();
            audio.volume = volumeSlider.value / 100;
            audio.src = blobUrl;

            // When segment ends, play the next queued one
            audio.onended = function() {{
                URL.revokeObjectURL(blobUrl);
                playNextSegment();
            }};

            // If decode fails, skip to next segment
            audio.onerror = function() {{
                URL.revokeObjectURL(blobUrl);
                playNextSegment();
            }};

            audio.play().then(function() {{
                isPlaying = true;
                playBtn.textContent = '\u23F8';
                setStatus('connected', ' Live', true);
            }}).catch(function() {{
                URL.revokeObjectURL(blobUrl);
                setStatus('error', 'Blocked', false);
                blobPlaying = false;
            }});
        }}

        function stopStream() {{
            // Reset reconnect state
            reconnectAttempts = MAX_RECONNECTS + 1; // Prevent reconnect() from firing
            streamUrl = '';

            // Abort active fetch
            if (abortController) {{
                abortController.abort();
                abortController = null;
            }}

            // Drain blob queue
            while (blobQueue.length > 0) {{
                URL.revokeObjectURL(blobQueue.shift());
            }}
            blobPlaying = false;
            reconnectAttempts = 0; // Reset for next session

            // Stop audio
            if (audio) {{
                audio.onended = null;
                audio.onerror = null;
                audio.pause();
                if (audio.src && audio.src.startsWith('blob:')) {{
                    URL.revokeObjectURL(audio.src);
                }}
                audio.src = '';
                audio.load();
                audio = null;
            }}
            isPlaying = false;
            playBtn.textContent = '\u25B6';
        }}

        // -- Now Playing Polling -------------------------------------------

        function pollNowPlaying() {{
            const url = CONFIG.nowPlayingBase.replace(
                CONFIG.stationId, currentStationId
            );

            fetch(url, {{
                mode: 'cors',
                headers: {{
                    'Authorization': 'Bearer ' + CONFIG.streamKey,
                    'Accept': 'application/json',
                }},
            }}).then(function(r) {{ return r.json(); }})
              .then(function(data) {{
                if (data && data.nowPlaying) {{
                    npTitleEl.textContent = data.nowPlaying.title || 'Unknown Track';
                    const parts = [];
                    if (data.nowPlaying.style) parts.push(data.nowPlaying.style);
                    if (data.nowPlaying.bpm) parts.push(data.nowPlaying.bpm + ' BPM');
                    if (data.nowPlaying.duration) {{
                        const m = Math.floor(data.nowPlaying.duration / 60);
                        const s = data.nowPlaying.duration % 60;
                        parts.push(m + ':' + String(s).padStart(2, '0'));
                    }}
                    npMetaEl.textContent = parts.join(' \u2022 ');
                }} else {{
                    npTitleEl.textContent = 'Waiting for track...';
                    npMetaEl.textContent = '';
                }}
            }}).catch(function() {{
                // Silent fail -- don't disrupt playback
            }});
        }}

        function startPolling() {{
            stopPolling();
            pollNowPlaying();
            pollTimer = setInterval(pollNowPlaying, CONFIG.pollIntervalMs);
        }}

        function stopPolling() {{
            if (pollTimer) {{
                clearInterval(pollTimer);
                pollTimer = null;
            }}
        }}

        // -- UI Helpers ----------------------------------------------------

        function setStatus(cls, text, showLiveDot) {{
            statusEl.className = cls || '';
            statusEl.textContent = '';
            if (showLiveDot) {{
                const dot = document.createElement('span');
                dot.className = 'live-dot';
                statusEl.appendChild(dot);
            }}
            statusEl.appendChild(document.createTextNode(text));
        }}

        // -- Event Listeners -----------------------------------------------

        playBtn.addEventListener('click', function() {{
            if (isPlaying) {{
                stopStream();
                stopPolling();
                setStatus('', 'Paused', false);
            }} else {{
                startStream();
                startPolling();
            }}
        }});

        volumeSlider.addEventListener('input', function() {{
            if (audio) {{
                audio.volume = this.value / 100;
            }}
        }});

        stationSelect.addEventListener('change', function() {{
            if (!this.value) return;
            const selected = CONFIG.stations.find(function(s) {{
                return s.id === stationSelect.value;
            }});
            if (selected) {{
                currentStationId = selected.id;
                stationNameEl.textContent = selected.name;
                stationGenreEl.textContent = selected.genre || '';
                npTitleEl.textContent = 'Loading...';
                npMetaEl.textContent = '';

                if (isPlaying) {{
                    startStream();
                    startPolling();
                }} else {{
                    pollNowPlaying();
                }}
            }}
        }});

        // Start polling immediately for now-playing info
        startPolling();
    }})();
    </script>
</body>
</html>"""
