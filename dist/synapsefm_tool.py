"""
title: Synapse-FM
description: Stream live AI-generated radio from Synapse-FM directly in your Open WebUI chat interface.
author: ther3zz
author_url: https://github.com/ther3zz
git_url: https://github.com/ther3zz/synapse-fm_open-webui_tool.git
version: 1.0.0
license: MIT
"""

import json
from pydantic import BaseModel, Field
from typing import Optional

# ── Inline Module Imports ──────────────────────────────────────────────────────
# In the bundled version, the module code is inlined above this class.
# In the source version, they're imported from the modules/ directory.
# For Open WebUI deployment, everything is in one file.

# --- BEGIN MODULE: sanitizer ---
import html
import re

KEY_PATTERN = re.compile(r"sfm_[A-Za-z0-9_\-]{8,}")
MAX_STATION_NAME = 100
MAX_STATION_DESCRIPTION = 200
MAX_TRACK_TITLE = 200
MAX_GENRE = 50


def sanitize_for_llm(text, max_length=200):
    if not isinstance(text, str):
        return ""
    sanitized = KEY_PATTERN.sub("[REDACTED]", text)
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", sanitized)
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "…"
    return sanitized


def sanitize_for_html(text):
    if not isinstance(text, str):
        return ""
    return html.escape(text, quote=True)


STATION_ALLOWED_FIELDS = {"id", "name", "genre", "description", "isPrivate", "image"}


def sanitize_station(raw):
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key in STATION_ALLOWED_FIELDS:
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            max_len = {"name": MAX_STATION_NAME, "description": MAX_STATION_DESCRIPTION, "genre": MAX_GENRE}.get(key, 200)
            result[key] = sanitize_for_llm(value, max_len)
        elif isinstance(value, bool):
            result[key] = value
    return result


def sanitize_station_for_html(raw):
    if not isinstance(raw, dict):
        return {}
    return {
        "id": sanitize_for_html(str(raw.get("id", ""))),
        "name": sanitize_for_html(str(raw.get("name", "Unknown Station"))[:MAX_STATION_NAME]),
        "genre": sanitize_for_html(str(raw.get("genre", ""))[:MAX_GENRE]),
        "image": sanitize_for_html(str(raw.get("image", ""))),
    }


NOW_PLAYING_ALLOWED_FIELDS = {"title", "style", "bpm", "duration"}


def sanitize_now_playing(raw):
    if not isinstance(raw, dict):
        return {}
    np = raw.get("nowPlaying", raw)
    if not isinstance(np, dict):
        return {"status": "Nothing playing"}
    result = {
        "stationName": sanitize_for_llm(str(raw.get("stationName", "")), MAX_STATION_NAME),
        "genre": sanitize_for_llm(str(raw.get("genre", "")), MAX_GENRE),
    }
    for key in NOW_PLAYING_ALLOWED_FIELDS:
        value = np.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            max_len = MAX_TRACK_TITLE if key == "title" else 100
            result[key] = sanitize_for_llm(value, max_len)
        elif isinstance(value, (int, float)):
            result[key] = value
    return result


# --- END MODULE: sanitizer ---

# --- BEGIN MODULE: http_client ---
import asyncio
import ssl
from urllib.parse import urlparse, urljoin

MAX_RESPONSE_BYTES = 512 * 1024
ALLOWED_PATH_PREFIXES = ("/api/live/", "/api/public/")

_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_valid_uuid(value):
    return bool(value and isinstance(value, str) and _UUID_PATTERN.match(value))


class SynapseFMClientError(Exception):
    def __init__(self, user_message):
        super().__init__(user_message)
        self.user_message = user_message


class SynapseFMConfigError(SynapseFMClientError):
    pass


class SynapseFMAPIError(SynapseFMClientError):
    pass


def validate_base_url(url):
    if not url or not isinstance(url, str):
        raise SynapseFMConfigError("SynapseFM URL is not configured. Set it in the plugin Valves.")
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise SynapseFMConfigError("SynapseFM URL must use https scheme.")
    if not parsed.hostname:
        raise SynapseFMConfigError("SynapseFM URL must have a valid hostname.")
    if parsed.path and parsed.path not in ("", "/"):
        raise SynapseFMConfigError("SynapseFM URL should be the base URL only (e.g., https://synapse-fm.ai).")
    return f"{parsed.scheme}://{parsed.netloc}"


def _build_url(base_url, path):
    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    if ".." in path:
        raise SynapseFMClientError("Invalid API path.")
    if not any(path.startswith(prefix) for prefix in ALLOWED_PATH_PREFIXES):
        raise SynapseFMClientError("Invalid API path.")
    return urljoin(base_url + "/", path.lstrip("/"))


class SynapseFMClient:
    def __init__(self, base_url, stream_key="", timeout=10):
        self._base_url = validate_base_url(base_url)
        self._stream_key = stream_key
        self._timeout = max(1, min(timeout, 30))
        self._key_validated = False

    async def _fetch(self, path, authenticated=False):
        try:
            import aiohttp
        except ImportError:
            raise SynapseFMAPIError("HTTP client library not available. Contact your admin.")

        url = _build_url(self._base_url, path)
        headers = {"Accept": "application/json", "User-Agent": "SynapseFM-OpenWebUI-Plugin/1.0"}
        if authenticated:
            if not self._stream_key:
                raise SynapseFMConfigError("Stream key is not configured. Set it in the plugin Valves.")
            headers["Authorization"] = f"Bearer {self._stream_key}"

        try:
            async with asyncio.timeout(self._timeout):
                ssl_ctx = ssl.create_default_context()
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers, allow_redirects=False, ssl=ssl_ctx) as resp:
                        if resp.status == 401:
                            self._key_validated = False
                            raise SynapseFMConfigError("Stream key is invalid or expired. Update it in the plugin Valves.")
                        if resp.status == 503:
                            raise SynapseFMAPIError("SynapseFM external streaming is currently disabled.")
                        if resp.status != 200:
                            raise SynapseFMAPIError("SynapseFM is temporarily unavailable. Please try again later.")
                        body = await resp.content.read(MAX_RESPONSE_BYTES + 1)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise SynapseFMAPIError("Response from SynapseFM was too large.")
                        try:
                            return json.loads(body)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            raise SynapseFMAPIError("Received invalid response from SynapseFM.")
        except SynapseFMClientError:
            raise
        except asyncio.TimeoutError:
            raise SynapseFMAPIError("SynapseFM did not respond in time. Please try again later.")
        except Exception:
            raise SynapseFMAPIError("Could not connect to SynapseFM. Please try again later.")

    async def validate_key(self):
        if self._key_validated:
            return True
        await self._fetch("/api/live/stations", authenticated=True)
        self._key_validated = True
        return True

    async def fetch_stations(self):
        data = await self._fetch("/api/live/stations", authenticated=True)
        stations = data.get("stations", [])
        return stations if isinstance(stations, list) else []

    async def fetch_now_playing(self, station_id):
        if not _is_valid_uuid(station_id):
            raise SynapseFMClientError("Invalid station identifier.")
        return await self._fetch(f"/api/live/now-playing/{station_id}", authenticated=True)

    def get_stream_url(self, station_id):
        if not _is_valid_uuid(station_id):
            raise SynapseFMClientError("Invalid station identifier.")
        return _build_url(self._base_url, f"/api/live/stream/{station_id}")

    def get_stream_key(self):
        return self._stream_key


# --- END MODULE: http_client ---

# --- BEGIN MODULE: player_builder ---
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
# --- END MODULE: player_builder ---

# --- BEGIN MODULE: bootloader ---
# Marker comments used to identify the bootloader in index.html
# Must match BOTH old (single-script) and new (vendor + bootloader) layouts
BOOTLOADER_START = "<!-- SynapseFM Player Bootloader"
BOOTLOADER_END_SENTINEL = "<!-- /SynapseFM -->"
BOOTLOADER_ID = "synapsefm-player-bootloader"


def _compute_bootloader_version():
    """Compute a short hash of the bootloader content for version tracking."""
    import hashlib
    combined = _MSE_WRAPPER_JS + BOOTLOADER_SCRIPT
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:12]

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
# mse-audio-wrapper v1.4.15 (LGPL-3.0-or-later) by Ethan Halsall
# https://github.com/eshaz/mse-audio-wrapper
# Wraps raw MP3 into fMP4 (ISO BMFF) for gapless MSE playback.
# Bundled as IIFE; exposes MSEAudioWrapperModule.MSEAudioWrapper global.
_MSE_WRAPPER_JS = """var MSEAudioWrapperModule=(()=>{var je=Object.defineProperty;var $n=Object.getOwnPropertyDescriptor;var Gn=Object.getOwnPropertyNames;var Wn=Object.prototype.hasOwnProperty;var qn=(s,t)=>{for(var e in t)je(s,e,{get:t[e],enumerable:!0})},Xn=(s,t,e,r)=>{if(t&&typeof t=="object"||typeof t=="function")for(let n of Gn(t))!Wn.call(s,n)&&n!==e&&je(s,n,{get:()=>t[n],enumerable:!(r=$n(t,n))||r.enumerable});return s};var jn=s=>Xn(je({},"__esModule",{value:!0}),s);var gr={};qn(gr,{MSEAudioWrapper:()=>Jt});var P=Symbol,un=", ",h=(()=>{let s="front",t="side",e="rear",r="left",n="center",o="right";return["",s+" ",t+" ",e+" "].map(a=>[[r,o],[r,o,n],[r,n,o],[n,r,o],[n]].flatMap(x=>x.map(y=>a+y).join(un)))})(),c0="LFE",b0="monophonic (mono)",g0="stereo",St="surround",D=(s,...t)=>`${[b0,g0,`linear ${St}`,"quadraphonic",`5.0 ${St}`,`5.1 ${St}`,`6.1 ${St}`,`7.1 ${St}`][s-1]} (${t.join(un)})`,wt=[b0,D(2,h[0][0]),D(3,h[0][2]),D(4,h[1][0],h[3][0]),D(5,h[1][2],h[3][0]),D(6,h[1][2],h[3][0],c0),D(7,h[1][2],h[2][0],h[3][4],c0),D(8,h[1][2],h[2][0],h[3][0],c0)],dn=192e3,fn=176400,Kt=96e3,Zt=88200,bn=64e3,_0=48e3,rt=44100,st=32e3,ot=24e3,it=22050,at=16e3,Qt=12e3,te=11025,ct=8e3,gn=7350,x0="absoluteGranulePosition",f="bandwidth",V="bitDepth",z="bitrate",ee=z+"Maximum",ne=z+"Minimum",re=z+"Nominal",h0="buffer",se=h0+"Fullness",k="codec",W=k+"Frames",oe="coupledStreamCount",Ft="crc",ie=Ft+"16",ae=Ft+"32",T="data",m="description",m0="duration",Mt="emphasis",ce="hasOpusPadding",n0="header",V0="isContinuedPacket",xe="isCopyrighted",z0="isFirstPage",le="isHome",r0="isLastPage",C0="isOriginal",y0="isPrivate",pe="isVbr",q="layer",i="length",d="mode",S0=d+"Extension",Je="mpeg",w0=Je+"Version",he="numberAACFrames",me="outputGain",xt="preSkip",ue="profile",de=P(),F0="protection",_n="rawData",s0="segments",b="subarray",$0="version",lt="vorbis",fe=lt+"Comments",be=lt+"Setup",ge="block",_e=ge+"ingStrategy",Ce=P(),M0=ge+"Size",G0=ge+"size0",W0=ge+"size1",Pt=P(),ye="channel",P0=ye+"MappingFamily",Se=ye+"MappingTable",$=ye+"Mode",kt=P(),p=ye+"s",Cn="copyright",we=Cn+"Id",Fe=Cn+"IdStart",k0="frame",T0=k0+"Count",X=k0+"Length",Me="Number",A0=k0+Me,u0=k0+"Padding",u=k0+"Size",yn="Rate",Pe="inputSample"+yn,Ye="page",pt=Ye+"Checksum",ht=P(),q0=Ye+"SegmentTable",R=Ye+"Sequence"+Me,Ke="sample",ke=Ke+Me,_=Ke+yn,l0=P(),C=Ke+"s",Te="stream",Ae=Te+"Count",De=Te+"Info",d0=Te+"Serial"+Me,Sn=Te+"StructureVersion",Ze="total",mt=Ze+"BytesOut",ut=Ze+"Duration",Tt=Ze+"Samples",S=P(),K=P(),At=P(),D0=P(),o0=P(),Ue=P(),Qe=P(),U0=P(),w=P(),Z=P(),Q=P(),p0=P(),B0=P(),Be=P(),i0=P(),a0=P(),t0=P(),Re=P(),H=Uint8Array,R0=DataView,F="reserved",N="bad",dt="free",Dt="none",Ee="16bit CRC";var tn=(s,t,e)=>{for(let r=0;r<s[i];r++){let n=t(r);for(let o=8;o>0;o--)n=e(n);s[r]=n}return s},Jn=tn(new H(256),s=>s,s=>s&128?7^s<<1:s<<1),U=[tn(new Uint16Array(256),s=>s<<8,s=>s<<1^(s&32768?32773:0))],B=[tn(new Uint32Array(256),s=>s,s=>s>>>1^(s&1)*3988292384)];for(let s=0;s<15;s++){U.push(new Uint16Array(256)),B.push(new Uint32Array(256));for(let t=0;t<=255;t++)U[s+1][t]=U[0][U[s][t]>>>8]^U[s][t]<<8,B[s+1][t]=B[s][t]>>>8^B[0][B[s][t]&255]}var Fn=s=>{let t=0,e=s[i];for(let r=0;r!==e;r++)t=Jn[t^s[r]];return t},Mn=s=>{let t=s[i],e=t-16,r=0,n=0;for(;n<=e;)r^=s[n++]<<8|s[n++],r=U[15][r>>8]^U[14][r&255]^U[13][s[n++]]^U[12][s[n++]]^U[11][s[n++]]^U[10][s[n++]]^U[9][s[n++]]^U[8][s[n++]]^U[7][s[n++]]^U[6][s[n++]]^U[5][s[n++]]^U[4][s[n++]]^U[3][s[n++]]^U[2][s[n++]]^U[1][s[n++]]^U[0][s[n++]];for(;n!==t;)r=(r&255)<<8^U[0][r>>8^s[n++]];return r},Pn=s=>{let t=s[i],e=t-16,r=0,n=0;for(;n<=e;)r=B[15][(s[n++]^r)&255]^B[14][(s[n++]^r>>>8)&255]^B[13][(s[n++]^r>>>16)&255]^B[12][s[n++]^r>>>24]^B[11][s[n++]]^B[10][s[n++]]^B[9][s[n++]]^B[8][s[n++]]^B[7][s[n++]]^B[6][s[n++]]^B[5][s[n++]]^B[4][s[n++]]^B[3][s[n++]]^B[2][s[n++]]^B[1][s[n++]]^B[0][s[n++]];for(;n!==t;)r=B[0][(r^s[n++])&255]^r>>>8;return r^-1},Bt=(...s)=>{let t=new H(s.reduce((e,r)=>e+r[i],0));return s.reduce((e,r)=>(t.set(r,e),e+r[i]),0),t},j=s=>String.fromCharCode(...s),wn=[0,8,4,12,2,10,6,14,1,9,5,13,3,11,7,15],Ut=s=>wn[s&15]<<4|wn[s>>4],Ie=class{constructor(t){this._data=t,this._pos=t[i]*8}set position(t){this._pos=t}get position(){return this._pos}read(t){let e=Math.floor(this._pos/8),r=this._pos%8;return this._pos-=t,(Ut(this._data[e-1])<<8)+Ut(this._data[e])>>7-r&255}},kn=(s,t)=>{try{return s.getBigInt64(t,!0)}catch{let e=s.getUint8(t+7)&128?-1:1,r=s.getUint32(t,!0),n=s.getUint32(t+4,!0);return e===-1&&(r=~r+1,n=~n+1),n>1048575&&console.warn("This platform does not support BigInt"),e*(r+n*2**32)}};var Rt=class{constructor(t,e){this._onCodecHeader=t,this._onCodecUpdate=e,this[i0]()}[a0](){this._isEnabled=!0}[i0](){this._headerCache=new Map,this._codecUpdateData=new WeakMap,this._codecHeaderSent=!1,this._codecShouldUpdate=!1,this._bitrate=null,this._isEnabled=!1}[Be](t,e){if(this._onCodecUpdate){this._bitrate!==t&&(this._bitrate=t,this._codecShouldUpdate=!0);let r=this._codecUpdateData.get(this._headerCache.get(this._currentHeader));this._codecShouldUpdate&&r&&this._onCodecUpdate({bitrate:t,...r},e),this._codecShouldUpdate=!1}}[w](t){let e=this._headerCache.get(t);return e&&this._updateCurrentHeader(t),e}[Z](t,e,r){this._isEnabled&&(this._codecHeaderSent||(this._onCodecHeader({...e}),this._codecHeaderSent=!0),this._updateCurrentHeader(t),this._headerCache.set(t,e),this._codecUpdateData.set(e,r))}_updateCurrentHeader(t){this._onCodecUpdate&&t!==this._currentHeader&&(this._codecShouldUpdate=!0,this._currentHeader=t)}};var E=new WeakMap,I=new WeakMap;var O=class{constructor(t,e){this._codecParser=t,this._headerCache=e}*[Qe](){let t;do{if(t=yield*this.Frame[Q](this._codecParser,this._headerCache,0),t)return t;this._codecParser[K](1)}while(!0)}*[U0](t){let e=yield*this[Qe](),r=I.get(e)[i];if(t||this._codecParser._flushing||(yield*this.Header[w](this._codecParser,this._headerCache,r)))return this._headerCache[a0](),this._codecParser[K](r),this._codecParser[D0](e),e;this._codecParser[o0](`Missing ${k0} at ${r} bytes from current position.`,`Dropping current ${k0} and trying again.`),this._headerCache[i0](),this._codecParser[K](1)}};var X0=class{constructor(t,e){I.set(this,{[n0]:t}),this[T]=e}};var J=class extends X0{static*[Q](t,e,r,n,o){let a=yield*t[w](r,n,o);if(a){let x=E.get(a)[X],y=E.get(a)[C],M=(yield*r[S](x,o))[b](0,x);return new e(a,M,y)}else return null}constructor(t,e,r){super(t,e),this[n0]=t,this[C]=r,this[m0]=r/t[_]*1e3,this[A0]=null,this[mt]=null,this[Tt]=null,this[ut]=null,I.get(this)[i]=e[i]}};var en="unsynchronizationFlag",nn="extendedHeaderFlag",rn="experimentalFlag",sn="footerPresent",Et=class s{static*getID3v2Header(t,e,r){let o={},a=yield*t[S](3,r);if(a[0]!==73||a[1]!==68||a[2]!==51||(a=yield*t[S](10,r),o[$0]=`id3v2.${a[3]}.${a[4]}`,a[5]&15)||(o[en]=!!(a[5]&128),o[nn]=!!(a[5]&64),o[rn]=!!(a[5]&32),o[sn]=!!(a[5]&16),a[6]&128||a[7]&128||a[8]&128||a[9]&128))return null;let x=a[6]<<21|a[7]<<14|a[8]<<7|a[9];return o[i]=10+x,new s(o)}constructor(t){this[$0]=t[$0],this[en]=t[en],this[nn]=t[nn],this[rn]=t[rn],this[sn]=t[sn],this[i]=t[i]}};var Y=class{constructor(t){E.set(this,t),this[V]=t[V],this[z]=null,this[p]=t[p],this[$]=t[$],this[_]=t[_]}};var Bn={0:[dt,dt,dt,dt,dt],16:[32,32,32,32,8],240:[N,N,N,N,N]},Le=(s,t,e)=>8*((s+e)%t+t)*(1<<(s+e)/t)-8*t*(t/8|0);for(let s=2;s<15;s++)Bn[s<<4]=[s*32,Le(s,4,0),Le(s,4,-1),Le(s,8,4),Le(s,8,0)];var Yn=0,Kn=1,Zn=2,Qn=3,Tn=4,ve="bands ",He=" to 31",An={0:ve+4+He,16:ve+8+He,32:ve+12+He,48:ve+16+He},j0="bitrateIndex",It="v2",$e="v1",Ne="Intensity stereo ",Oe=", MS stereo ",Ve="on",ze="off",tr={0:Ne+ze+Oe+ze,16:Ne+Ve+Oe+ze,32:Ne+ze+Oe+Ve,48:Ne+Ve+Oe+Ve},on={0:{[m]:F},2:{[m]:"Layer III",[u0]:1,[S0]:tr,[$e]:{[j0]:Zn,[C]:1152},[It]:{[j0]:Tn,[C]:576}},4:{[m]:"Layer II",[u0]:1,[S0]:An,[C]:1152,[$e]:{[j0]:Kn},[It]:{[j0]:Tn}},6:{[m]:"Layer I",[u0]:4,[S0]:An,[C]:384,[$e]:{[j0]:Yn},[It]:{[j0]:Qn}}},an="MPEG Version ",Dn="ISO/IEC ",er={0:{[m]:`${an}2.5 (later extension of MPEG 2)`,[q]:It,[_]:{0:te,4:Qt,8:ct,12:F}},8:{[m]:F},16:{[m]:`${an}2 (${Dn}13818-3)`,[q]:It,[_]:{0:it,4:ot,8:at,12:F}},24:{[m]:`${an}1 (${Dn}11172-3)`,[q]:$e,[_]:{0:rt,4:_0,8:st,12:F}},length:i},nr={0:Ee,1:Dt},rr={0:Dt,1:"50/15 ms",2:F,3:"CCIT J.17"},Un={0:{[p]:2,[m]:g0},64:{[p]:2,[m]:"joint "+g0},128:{[p]:2,[m]:"dual channel"},192:{[p]:1,[m]:b0}},J0=class s extends Y{static*[w](t,e,r){let n={},o=yield*Et.getID3v2Header(t,e,r);o&&(yield*t[S](o[i],r),t[K](o[i]));let a=yield*t[S](4,r),x=j(a[b](0,4)),y=e[w](x);if(y)return new s(y);if(a[0]!==255||a[1]<224)return null;let M=er[a[1]&24];if(M[m]===F)return null;let v=a[1]&6;if(on[v][m]===F)return null;let G={...on[v],...on[v][M[q]]};if(n[w0]=M[m],n[q]=G[m],n[C]=G[C],n[F0]=nr[a[1]&1],n[i]=4,n[z]=Bn[a[2]&240][G[j0]],n[z]===N||(n[_]=M[_][a[2]&12],n[_]===F)||(n[u0]=a[2]&2&&G[u0],n[y0]=!!(a[2]&1),n[X]=Math.floor(125*n[z]*n[C]/n[_]+n[u0]),!n[X]))return null;let et=a[3]&192;if(n[$]=Un[et][m],n[p]=Un[et][p],n[S0]=G[S0][a[3]&48],n[xe]=!!(a[3]&8),n[C0]=!!(a[3]&4),n[Mt]=rr[a[3]&3],n[Mt]===F)return null;n[V]=16;{let{length:Yt,frameLength:nt,samples:hn,...Xe}=n;e[Z](x,n,Xe)}return new s(n)}constructor(t){super(t),this[z]=t[z],this[Mt]=t[Mt],this[u0]=t[u0],this[xe]=t[xe],this[C0]=t[C0],this[y0]=t[y0],this[q]=t[q],this[S0]=t[S0],this[w0]=t[w0],this[F0]=t[F0]}};var Lt=class s extends J{static*[Q](t,e,r){return yield*super[Q](J0,s,t,e,r)}constructor(t,e,r){super(t,e,r)}};var vt=class extends O{constructor(t,e,r){super(t,e),this.Frame=Lt,this.Header=J0,r(this[k])}get[k](){return Je}*[p0](){return yield*this[U0]()}};var sr={0:"MPEG-4",8:"MPEG-2"},or={0:"valid",2:N,4:N,6:N},ir={0:Ee,1:Dt},ar={0:"AAC Main",64:"AAC LC (Low Complexity)",128:"AAC SSR (Scalable Sample Rate)",192:"AAC LTP (Long Term Prediction)"},cr={0:Kt,4:Zt,8:bn,12:_0,16:rt,20:st,24:ot,28:it,32:at,36:Qt,40:te,44:ct,48:gn,52:F,56:F,60:"frequency is written explicitly"},Rn={0:{[p]:0,[m]:"Defined in AOT Specific Config"},64:{[p]:1,[m]:b0},128:{[p]:2,[m]:D(2,h[0][0])},192:{[p]:3,[m]:D(3,h[1][3])},256:{[p]:4,[m]:D(4,h[1][3],h[3][4])},320:{[p]:5,[m]:D(5,h[1][3],h[3][0])},384:{[p]:6,[m]:D(6,h[1][3],h[3][0],c0)},448:{[p]:8,[m]:D(8,h[1][3],h[2][0],h[3][0],c0)}},Y0=class s extends Y{static*[w](t,e,r){let n={},o=yield*t[S](7,r),a=j([o[0],o[1],o[2],o[3]&252|o[6]&3]),x=e[w](a);if(x)Object.assign(n,x);else{if(o[0]!==255||o[1]<240||(n[w0]=sr[o[1]&8],n[q]=or[o[1]&6],n[q]===N))return null;let M=o[1]&1;n[F0]=ir[M],n[i]=M?7:9,n[de]=o[2]&192,n[l0]=o[2]&60;let v=o[2]&2;if(n[ue]=ar[n[de]],n[_]=cr[n[l0]],n[_]===F)return null;n[y0]=!!v,n[kt]=(o[2]<<8|o[3])&448,n[$]=Rn[n[kt]][m],n[p]=Rn[n[kt]][p],n[C0]=!!(o[3]&32),n[le]=!!(o[3]&8),n[we]=!!(o[3]&8),n[Fe]=!!(o[3]&4),n[V]=16,n[C]=1024,n[he]=o[6]&3;{let{length:G,channelModeBits:et,profileBits:Yt,sampleRateBits:nt,frameLength:hn,samples:Xe,numberAACFrames:mn,...zn}=n;e[Z](a,n,zn)}}if(n[X]=(o[3]<<11|o[4]<<3|o[5]>>5)&8191,!n[X])return null;let y=(o[5]<<6|o[6]>>2)&2047;return n[se]=y===2047?"VBR":y,new s(n)}constructor(t){super(t),this[we]=t[we],this[Fe]=t[Fe],this[se]=t[se],this[le]=t[le],this[C0]=t[C0],this[y0]=t[y0],this[q]=t[q],this[i]=t[i],this[w0]=t[w0],this[he]=t[he],this[ue]=t[ue],this[F0]=t[F0]}get audioSpecificConfig(){let t=E.get(this),e=t[de]+64<<5|t[l0]<<5|t[kt]>>3,r=new H(2);return new R0(r[h0]).setUint16(0,e,!1),r}};var Ht=class s extends J{static*[Q](t,e,r){return yield*super[Q](Y0,s,t,e,r)}constructor(t,e,r){super(t,e,r)}};var Nt=class extends O{constructor(t,e,r){super(t,e),this.Frame=Ht,this.Header=Y0,r(this[k])}get[k](){return"aac"}*[p0](){return yield*this[U0]()}};var E0=class s extends J{static _getFrameFooterCrc16(t){return(t[t[i]-2]<<8)+t[t[i]-1]}static[Re](t){let e=s._getFrameFooterCrc16(t),r=Mn(t[b](0,-2));return e===r}constructor(t,e,r){e[De]=r,e[ie]=s._getFrameFooterCrc16(t),super(e,t,E.get(e)[C])}};var En="get from STREAMINFO metadata block",xr={0:"Fixed",1:"Variable"},In={0:F,16:192};for(let s=2;s<16;s++)In[s<<4]=s<6?576*2**(s-2):2**s;var lr={0:En,1:Zt,2:fn,3:dn,4:ct,5:at,6:it,7:ot,8:st,9:rt,10:_0,11:Kt,15:N},pr={0:{[p]:1,[m]:b0},16:{[p]:2,[m]:D(2,h[0][0])},32:{[p]:3,[m]:D(3,h[0][1])},48:{[p]:4,[m]:D(4,h[1][0],h[3][0])},64:{[p]:5,[m]:D(5,h[1][1],h[3][0])},80:{[p]:6,[m]:D(6,h[1][1],c0,h[3][0])},96:{[p]:7,[m]:D(7,h[1][1],c0,h[3][4],h[2][0])},112:{[p]:8,[m]:D(8,h[1][1],c0,h[3][0],h[2][0])},128:{[p]:2,[m]:`${g0} (left, diff)`},144:{[p]:2,[m]:`${g0} (diff, right)`},160:{[p]:2,[m]:`${g0} (avg, diff)`},176:F,192:F,208:F,224:F,240:F},hr={0:En,2:8,4:12,6:F,8:16,10:20,12:24,14:F},I0=class s extends Y{static _decodeUTF8Int(t){if(t[0]>254)return null;if(t[0]<128)return{value:t[0],length:1};let e=1;for(let a=64;a&t[0];a>>=1)e++;let r=e-1,n=0,o=0;for(;r>0;o+=6,r--){if((t[r]&192)!==128)return null;n|=(t[r]&63)<<o}return n|=(t[r]&127>>e)<<o,{value:n,length:e}}static[t0](t,e){let r={[S]:function*(){return t}};return s[w](r,e,0).next().value}static*[w](t,e,r){let n=yield*t[S](6,r);if(n[0]!==255||!(n[1]===248||n[1]===249))return null;let o={},a=j(n[b](0,4)),x=e[w](a);if(x)Object.assign(o,x);else{if(o[Ce]=n[1]&1,o[_e]=xr[o[Ce]],o[Pt]=n[2]&240,o[l0]=n[2]&15,o[M0]=In[o[Pt]],o[M0]===F||(o[_]=lr[o[l0]],o[_]===N)||n[3]&1)return null;let M=pr[n[3]&240];if(M===F||(o[p]=M[p],o[$]=M[m],o[V]=hr[n[3]&14],o[V]===F))return null}o[i]=5,n=yield*t[S](o[i]+8,r);let y=s._decodeUTF8Int(n[b](4));if(!y||(o[Ce]?o[ke]=y.value:o[A0]=y.value,o[i]+=y[i],o[Pt]===96?(n[i]<o[i]&&(n=yield*t[S](o[i],r)),o[M0]=n[o[i]-1]+1,o[i]+=1):o[Pt]===112&&(n[i]<o[i]&&(n=yield*t[S](o[i],r)),o[M0]=(n[o[i]-1]<<8)+n[o[i]]+1,o[i]+=2),o[C]=o[M0],o[l0]===12?(n[i]<o[i]&&(n=yield*t[S](o[i],r)),o[_]=n[o[i]-1]*1e3,o[i]+=1):o[l0]===13?(n[i]<o[i]&&(n=yield*t[S](o[i],r)),o[_]=(n[o[i]-1]<<8)+n[o[i]],o[i]+=2):o[l0]===14&&(n[i]<o[i]&&(n=yield*t[S](o[i],r)),o[_]=((n[o[i]-1]<<8)+n[o[i]])*10,o[i]+=2),n[i]<o[i]&&(n=yield*t[S](o[i],r)),o[Ft]=n[o[i]-1],o[Ft]!==Fn(n[b](0,o[i]-1))))return null;if(!x){let{blockingStrategyBits:M,frameNumber:v,sampleNumber:G,samples:et,sampleRateBits:Yt,blockSizeBits:nt,crc:hn,length:Xe,...mn}=o;e[Z](a,o,mn)}return new s(o)}constructor(t){super(t),this[ie]=null,this[_e]=t[_e],this[M0]=t[M0],this[A0]=t[A0],this[ke]=t[ke],this[De]=null}};var mr=2,ur=512*1024,K0=class extends O{constructor(t,e,r){super(t,e),this.Frame=E0,this.Header=I0,r(this[k])}get[k](){return"flac"}*_getNextFrameSyncOffset(t){let e=yield*this._codecParser[S](2,0),r=e[i]-2;for(;t<r;){if(e[t]===255){let o=e[t+1];if(o===248||o===249)break;o!==255&&t++}t++}return t}*[p0](){do{let t=yield*I0[w](this._codecParser,this._headerCache,0);if(t){let e=E.get(t)[i]+mr;for(;e<=ur;){if(this._codecParser._flushing||(yield*I0[w](this._codecParser,this._headerCache,e))){let r=yield*this._codecParser[S](e);if(this._codecParser._flushing||(r=r[b](0,e)),E0[Re](r)){let n=new E0(r,t);return this._headerCache[a0](),this._codecParser[K](e),this._codecParser[D0](n),n}}e=yield*this._getNextFrameSyncOffset(e+1)}this._codecParser[o0](`Unable to sync FLAC frame after searching ${e} bytes.`),this._codecParser[K](e)}else this._codecParser[K](yield*this._getNextFrameSyncOffset(1))}while(!0)}[B0](t){return t[R]===0?(this._headerCache[a0](),this._streamInfo=t[T][b](13)):t[R]===1||(t[W]=I.get(t)[s0].map(e=>{let r=I0[t0](e,this._headerCache);if(r)return new E0(e,r,this._streamInfo);this._codecParser[o0]("Failed to parse Ogg FLAC frame","Skipping invalid FLAC frame")}).filter(e=>!!e)),t}};var Z0=class s{static*[w](t,e,r){let n={},o=yield*t[S](28,r);if(o[0]!==79||o[1]!==103||o[2]!==103||o[3]!==83||(n[Sn]=o[4],o[5]&248))return null;n[r0]=!!(o[5]&4),n[z0]=!!(o[5]&2),n[V0]=!!(o[5]&1);let x=new R0(H.from(o[b](0,28))[h0]);n[x0]=kn(x,6),n[d0]=x.getInt32(14,!0),n[R]=x.getInt32(18,!0),n[pt]=x.getInt32(22,!0);let y=o[26];n[i]=y+27,o=yield*t[S](n[i],r),n[X]=0,n[q0]=[],n[ht]=H.from(o[b](27,n[i]));for(let M=0,v=0;M<y;M++){let G=n[ht][M];n[X]+=G,v+=G,(G!==255||M===y-1)&&(n[q0].push(v),v=0)}return new s(n)}constructor(t){E.set(this,t),this[x0]=t[x0],this[V0]=t[V0],this[z0]=t[z0],this[r0]=t[r0],this[q0]=t[q0],this[R]=t[R],this[pt]=t[pt],this[d0]=t[d0]}};var Ot=class s extends X0{static*[Q](t,e,r){let n=yield*Z0[w](t,e,r);if(n){let o=E.get(n)[X],a=E.get(n)[i],x=a+o,y=(yield*t[S](x,0))[b](0,x),M=y[b](a,x);return new s(n,M,y)}else return null}constructor(t,e,r){super(t,e),I.get(this)[i]=r[i],this[W]=[],this[_n]=r,this[x0]=t[x0],this[ae]=t[pt],this[m0]=0,this[V0]=t[V0],this[z0]=t[z0],this[r0]=t[r0],this[R]=t[R],this[C]=0,this[d0]=t[d0]}};var ft=class extends J{constructor(t,e,r){super(e,t,r)}};var Ln={0:wt.slice(0,2),1:wt},e0="SILK-only",L="CELT-only",Ge="Hybrid",L0="narrowband",We="medium-band",v0="wideband",bt="super-wideband",gt="fullband",dr={0:{[d]:e0,[f]:L0,[u]:10},8:{[d]:e0,[f]:L0,[u]:20},16:{[d]:e0,[f]:L0,[u]:40},24:{[d]:e0,[f]:L0,[u]:60},32:{[d]:e0,[f]:We,[u]:10},40:{[d]:e0,[f]:We,[u]:20},48:{[d]:e0,[f]:We,[u]:40},56:{[d]:e0,[f]:We,[u]:60},64:{[d]:e0,[f]:v0,[u]:10},72:{[d]:e0,[f]:v0,[u]:20},80:{[d]:e0,[f]:v0,[u]:40},88:{[d]:e0,[f]:v0,[u]:60},96:{[d]:Ge,[f]:bt,[u]:10},104:{[d]:Ge,[f]:bt,[u]:20},112:{[d]:Ge,[f]:gt,[u]:10},120:{[d]:Ge,[f]:gt,[u]:20},128:{[d]:L,[f]:L0,[u]:2.5},136:{[d]:L,[f]:L0,[u]:5},144:{[d]:L,[f]:L0,[u]:10},152:{[d]:L,[f]:L0,[u]:20},160:{[d]:L,[f]:v0,[u]:2.5},168:{[d]:L,[f]:v0,[u]:5},176:{[d]:L,[f]:v0,[u]:10},184:{[d]:L,[f]:v0,[u]:20},192:{[d]:L,[f]:bt,[u]:2.5},200:{[d]:L,[f]:bt,[u]:5},208:{[d]:L,[f]:bt,[u]:10},216:{[d]:L,[f]:bt,[u]:20},224:{[d]:L,[f]:gt,[u]:2.5},232:{[d]:L,[f]:gt,[u]:5},240:{[d]:L,[f]:gt,[u]:10},248:{[d]:L,[f]:gt,[u]:20}},_t=class s extends Y{static[t0](t,e,r){let n={};if(n[p]=t[9],n[P0]=t[18],n[i]=n[P0]!==0?21+n[p]:19,t[i]<n[i])throw new Error("Out of data while inside an Ogg Page");let o=e[0]&3,a=o===3?2:1,x=j(t[b](0,n[i]))+j(e[b](0,a)),y=r[w](x);if(y)return new s(y);if(x.substr(0,8)!=="OpusHead"||t[8]!==1)return null;n[T]=H.from(t[b](0,n[i]));let M=new R0(n[T][h0]);if(n[V]=16,n[xt]=M.getUint16(10,!0),n[Pe]=M.getUint32(12,!0),n[_]=_0,n[me]=M.getInt16(16,!0),n[P0]in Ln&&(n[$]=Ln[n[P0]][n[p]-1],!n[$]))return null;n[P0]!==0&&(n[Ae]=t[19],n[oe]=t[20],n[Se]=[...t[b](21,n[p]+21)]);let v=dr[248&e[0]];switch(n[d]=v[d],n[f]=v[f],n[u]=v[u],o){case 0:n[T0]=1;break;case 1:case 2:n[T0]=2;break;case 3:n[pe]=!!(128&e[1]),n[ce]=!!(64&e[1]),n[T0]=63&e[1];break;default:return null}{let{length:G,data:et,channelMappingFamily:Yt,...nt}=n;r[Z](x,n,nt)}return new s(n)}constructor(t){super(t),this[T]=t[T],this[f]=t[f],this[P0]=t[P0],this[Se]=t[Se],this[oe]=t[oe],this[T0]=t[T0],this[u]=t[u],this[ce]=t[ce],this[Pe]=t[Pe],this[pe]=t[pe],this[d]=t[d],this[me]=t[me],this[xt]=t[xt],this[Ae]=t[Ae]}};var Vt=class extends O{constructor(t,e,r){super(t,e),this.Frame=ft,this.Header=_t,r(this[k]),this._identificationHeader=null,this._preSkipRemaining=null}get[k](){return"opus"}[B0](t){return t[R]===0?(this._headerCache[a0](),this._identificationHeader=t[T]):t[R]===1||(t[W]=I.get(t)[s0].map(e=>{let r=_t[t0](this._identificationHeader,e,this._headerCache);if(r){this._preSkipRemaining===null&&(this._preSkipRemaining=r[xt]);let n=r[u]*r[T0]/1e3*r[_];return this._preSkipRemaining>0&&(this._preSkipRemaining-=n,n=this._preSkipRemaining<0?-this._preSkipRemaining:0),new ft(e,r,n)}this._codecParser[Ue]("Failed to parse Ogg Opus Header","Not a valid Ogg Opus file")})),t}};var Ct=class extends J{constructor(t,e,r){super(e,t,r)}};var cn={};for(let s=0;s<8;s++)cn[s+6]=2**(6+s);var zt=class s extends Y{static[t0](t,e,r,n){if(t[i]<30)throw new Error("Out of data while inside an Ogg Page");let o=j(t[b](0,30)),a=e[w](o);if(a)return new s(a);let x={[i]:30};if(o.substr(0,7)!=="vorbis")return null;x[T]=H.from(t[b](0,30));let y=new R0(x[T][h0]);if(x[$0]=y.getUint32(7,!0),x[$0]!==0||(x[p]=t[11],x[$]=wt[x[p]-1]||"application defined",x[_]=y.getUint32(12,!0),x[ee]=y.getInt32(16,!0),x[re]=y.getInt32(20,!0),x[ne]=y.getInt32(24,!0),x[W0]=cn[(t[28]&240)>>4],x[G0]=cn[t[28]&15],x[G0]>x[W0])||t[29]!==1)return null;x[V]=32,x[be]=n,x[fe]=r;{let{length:M,data:v,version:G,vorbisSetup:et,vorbisComments:Yt,...nt}=x;e[Z](o,x,nt)}return new s(x)}constructor(t){super(t),this[ee]=t[ee],this[ne]=t[ne],this[re]=t[re],this[G0]=t[G0],this[W0]=t[W0],this[T]=t[T],this[fe]=t[fe],this[be]=t[be]}};var $t=class extends O{constructor(t,e,r){super(t,e),this.Frame=Ct,r(this[k]),this._identificationHeader=null,this._setupComplete=!1,this._prevBlockSize=null}get[k](){return lt}[B0](t){t[W]=[];for(let e of I.get(t)[s0])if(e[0]===1)this._headerCache[a0](),this._identificationHeader=t[T],this._setupComplete=!1;else if(e[0]===3)this._vorbisComments=e;else if(e[0]===5)this._vorbisSetup=e,this._mode=this._parseSetupHeader(e),this._setupComplete=!0;else if(this._setupComplete){let r=zt[t0](this._identificationHeader,this._headerCache,this._vorbisComments,this._vorbisSetup);r?t[W].push(new Ct(e,r,this._getSamples(e,r))):this._codecParser[logError]("Failed to parse Ogg Vorbis Header","Not a valid Ogg Vorbis file")}return t}_getSamples(t,e){let n=this._mode.blockFlags[t[0]>>1&this._mode.mask]?e[W0]:e[G0],o=this._prevBlockSize===null?0:(this._prevBlockSize+n)/4;return this._prevBlockSize=n,o}_parseSetupHeader(t){let e=new Ie(t),r={count:0,blockFlags:[]};for(;(e.read(1)&1)!==1;);let n;for(;r.count<64&&e.position>0;){Ut(e.read(8));let o=0;for(;e.read(8)===0&&o++<3;);if(o===4)n=e.read(7),r.blockFlags.unshift(n&1),e.position+=6,r.count++;else{((Ut(n)&126)>>1)+1!==r.count&&this._codecParser[o0]("vorbis derived mode count did not match actual mode count");break}}return r.mask=(1<<Math.log2(r.count))-1,r}};var xn=class{constructor(t,e,r){this._codecParser=t,this._headerCache=e,this._onCodec=r,this._continuedPacket=new H,this._codec=null,this._isSupported=null,this._previousAbsoluteGranulePosition=null}get[k](){return this._codec||""}_updateCodec(t,e){this._codec!==t&&(this._headerCache[i0](),this._parser=new e(this._codecParser,this._headerCache,this._onCodec),this._codec=t)}_checkCodecSupport({data:t}){let e=j(t[b](0,8));switch(e){case"fishead\0":return!1;case"OpusHead":return this._updateCodec("opus",Vt),!0;case(/^\x7fFLAC/.test(e)&&e):return this._updateCodec("flac",K0),!0;case(/^\x01vorbis/.test(e)&&e):return this._updateCodec(lt,$t),!0;default:return!1}}_checkPageSequenceNumber(t){t[R]!==this._pageSequenceNumber+1&&this._pageSequenceNumber>1&&t[R]>1&&this._codecParser[o0]("Unexpected gap in Ogg Page Sequence Number.",`Expected: ${this._pageSequenceNumber+1}, Got: ${t[R]}`),this._pageSequenceNumber=t[R]}_parsePage(t){this._isSupported===null&&(this._pageSequenceNumber=t[R],this._isSupported=this._checkCodecSupport(t)),this._checkPageSequenceNumber(t);let e=I.get(t),r=E.get(e[n0]),n=0;if(e[s0]=r[q0].map(o=>t[T][b](n,n+=o)),this._continuedPacket[i]&&(e[s0][0]=Bt(this._continuedPacket,e[s0][0]),this._continuedPacket=new H),r[ht][r[ht][i]-1]===255&&(this._continuedPacket=Bt(this._continuedPacket,e[s0].pop())),this._previousAbsoluteGranulePosition!==null&&(t[C]=Number(t[x0]-this._previousAbsoluteGranulePosition)),this._previousAbsoluteGranulePosition=t[x0],this._isSupported){let o=this._parser[B0](t);return this._codecParser[D0](o),o}else return t}},Gt=class extends O{constructor(t,e,r){super(t,e),this._onCodec=r,this.Frame=Ot,this.Header=Z0,this._streams=new Map,this._currentSerialNumber=null}get[k](){let t=this._streams.get(this._currentSerialNumber);return t?t.codec:""}*[p0](){let t=yield*this[U0](!0);this._currentSerialNumber=t[d0];let e=this._streams.get(this._currentSerialNumber);return e||(e=new xn(this._codecParser,this._headerCache,this._onCodec),this._streams.set(this._currentSerialNumber,e)),t[r0]&&this._streams.delete(this._currentSerialNumber),e._parsePage(t)}};var ln=()=>{},Wt=class{constructor(t,{onCodec:e,onCodecHeader:r,onCodecUpdate:n,enableLogging:o=!1,enableFrameCRC32:a=!0}={}){this._inputMimeType=t,this._onCodec=e||ln,this._onCodecHeader=r||ln,this._onCodecUpdate=n,this._enableLogging=o,this._crc32=a?Pn:ln,this[i0]()}get[k](){return this._parser?this._parser[k]:""}[i0](){this._headerCache=new Rt(this._onCodecHeader,this._onCodecUpdate),this._generator=this._getGenerator(),this._generator.next()}*flush(){this._flushing=!0;for(let t=this._generator.next();t.value;t=this._generator.next())yield t.value;this._flushing=!1,this[i0]()}*parseChunk(t){for(let e=this._generator.next(t);e.value;e=this._generator.next())yield e.value}parseAll(t){return[...this.parseChunk(t),...this.flush()]}*_getGenerator(){if(this._inputMimeType.match(/aac/))this._parser=new Nt(this,this._headerCache,this._onCodec);else if(this._inputMimeType.match(/mpeg/))this._parser=new vt(this,this._headerCache,this._onCodec);else if(this._inputMimeType.match(/flac/))this._parser=new K0(this,this._headerCache,this._onCodec);else if(this._inputMimeType.match(/ogg/))this._parser=new Gt(this,this._headerCache,this._onCodec);else throw new Error(`Unsupported Codec ${mimeType}`);for(this._frameNumber=0,this._currentReadPosition=0,this._totalBytesIn=0,this._totalBytesOut=0,this._totalSamples=0,this._sampleRate=void 0,this._rawData=new Uint8Array(0);;){let t=yield*this._parser[p0]();t&&(yield t)}}*[S](t=0,e=0){let r;for(;this._rawData[i]<=t+e;){if(r=yield,this._flushing)return this._rawData[b](e);r&&(this._totalBytesIn+=r[i],this._rawData=Bt(this._rawData,r))}return this._rawData[b](e)}[K](t){this._currentReadPosition+=t,this._rawData=this._rawData[b](t)}[At](t){this._sampleRate=t[n0][_],t[n0][z]=t[m0]>0?Math.round(t[T][i]/t[m0])*8:0,t[A0]=this._frameNumber++,t[mt]=this._totalBytesOut,t[Tt]=this._totalSamples,t[ut]=this._totalSamples/this._sampleRate*1e3,t[ae]=this._crc32(t[T]),this._headerCache[Be](t[n0][z],t[ut]),this._totalBytesOut+=t[T][i],this._totalSamples+=t[C]}[D0](t){if(t[W]){if(t[r0]){let e=t[C];t[W].forEach(r=>{let n=r[C];e<n&&(r[C]=e>0?e:0,r[m0]=r[C]/r[n0][_]*1e3),e-=n,this[At](r)})}else t[C]=0,t[W].forEach(e=>{t[C]+=e[C],this[At](e)});t[m0]=t[C]/this._sampleRate*1e3||0,t[Tt]=this._totalSamples,t[ut]=this._totalSamples/this._sampleRate*1e3||0,t[mt]=this._totalBytesOut}else this[At](t)}_log(t,e){if(this._enableLogging){let r=[`${k}:         ${this[k]}`,`inputMimeType: ${this._inputMimeType}`,`readPosition:  ${this._currentReadPosition}`,`totalBytesIn:  ${this._totalBytesIn}`,`${mt}: ${this._totalBytesOut}`],n=Math.max(...r.map(o=>o[i]));e.push(`--stats--${"-".repeat(n-9)}`,...r,"-".repeat(n)),t("codec-parser",e.reduce((o,a)=>o+`
  `+a,""))}}[o0](...t){this._log(console.warn,t)}[Ue](...t){this._log(console.error,t)}};var vn=Wt;var H0="webm";var Q0="mp4a.40.2",qt="flac",Xt="vorbis",f0="opus",Hn="audio/",Nn=";codecs=",jt=Hn+"mp4"+Nn,pn=Hn+H0+Nn,tt="mse-audio-wrapper";var A=class s{constructor({name:t,contents:e=[],children:r=[]}){this._name=t,this._contents=e,this._children=r}static stringToByteArray(t){return[...t].map(e=>e.charCodeAt(0))}static getFloat64(t){let e=new Uint8Array(8);return new DataView(e.buffer).setFloat64(0,t),e}static getUint64(t){let e=new Uint8Array(8);return new DataView(e.buffer).setBigUint64(0,BigInt(t)),e}static getUint32(t){let e=new Uint8Array(4);return new DataView(e.buffer).setUint32(0,t),e}static getUint16(t){let e=new Uint8Array(2);return new DataView(e.buffer).setUint16(0,t),e}static getInt16(t){let e=new Uint8Array(2);return new DataView(e.buffer).setInt16(0,t),e}static*flatten(t){for(let e of t)Array.isArray(e)?yield*s.flatten(e):yield e}get contents(){let t=new Uint8Array(this.length),e=this._buildContents(),r=0;for(let n of s.flatten(e))typeof n!="object"?(t[r]=n,r++):(t.set(n,r),r+=n.length);return t}get length(){return this._buildLength()}_buildContents(){return[this._contents,...this._children.map(t=>t._buildContents())]}_buildLength(){let t;return Array.isArray(this._contents)?t=this._contents.reduce((e,r)=>e+(r.length===void 0?1:r.length),0):t=this._contents.length===void 0?1:this._contents.length,t+this._children.reduce((e,r)=>e+r.length,0)}addChild(t){this._children.push(t)}};var c=class extends A{constructor(t,{contents:e,children:r}={}){super({name:t,contents:e,children:r})}_buildContents(){return[...this._lengthBytes,...A.stringToByteArray(this._name),...super._buildContents()]}_buildLength(){return this._length||(this._length=4+this._name.length+super._buildLength(),this._lengthBytes=A.getUint32(this._length)),this._length}};var N0=class s extends A{constructor(t,{contents:e,tags:r}={}){super({name:t,contents:e,children:r})}static getLength(t){let e=A.getUint32(t);return e.every((r,n,o)=>r===0?(o[n]=128,!0):!1),e}_buildContents(){return[this._name,...this._lengthBytes,...super._buildContents()]}_buildLength(){if(!this._length){let t=super._buildLength();this._lengthBytes=s.getLength(t),this._length=1+t+this._lengthBytes.length}return this._length}addTag(t){this.addChild(t)}};var O0=class{constructor(t){this._codec=t}getCodecBox(t){switch(this._codec){case"mp3":return this.getMp4a(t,107);case Q0:return this.getMp4a(t,64);case f0:return this.getOpus(t);case qt:return this.getFlaC(t)}}getOpus(t){return new c("Opus",{contents:[0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,0,0,t.channels,0,t.bitDepth,0,0,0,0,c.getUint16(t.sampleRate),0,0],children:[new c("dOps",{contents:[0,t.channels,c.getUint16(t.preSkip),c.getUint32(t.inputSampleRate),c.getInt16(t.outputGain),t.channelMappingFamily,t.channelMappingFamily!==0?[t.streamCount,t.coupledStreamCount,t.channelMappingTable]:[]]})]})}getFlaC(t){return new c("fLaC",{contents:[0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,0,0,t.channels,0,t.bitDepth,0,0,0,0,c.getUint16(t.sampleRate),0,0],children:[new c("dfLa",{contents:[0,0,0,0,...t.streamInfo||[128,0,0,34,c.getUint16(t.blockSize),c.getUint16(t.blockSize),0,0,0,0,0,0,c.getUint32(t.sampleRate<<12|t.channels<<8|t.bitDepth-1<<4),0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]]})]})}getMp4a(t,e){let r=new N0(4,{contents:[e,21,0,0,0,0,0,0,0,0,0,0,0]});return e===64&&r.addTag(new N0(5,{contents:t.audioSpecificConfig})),new c("mp4a",{contents:[0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,0,0,t.channels,0,16,0,0,0,0,c.getUint16(t.sampleRate),0,0],children:[new c("esds",{contents:[0,0,0,0],children:[new N0(3,{contents:[0,1,0],tags:[r,new N0(6,{contents:2})]})]})]})}getInitializationSegment({header:t,samples:e}){return new A({children:[new c("ftyp",{contents:[c.stringToByteArray("iso5"),0,0,2,0,c.stringToByteArray("iso6mp41")]}),new c("moov",{children:[new c("mvhd",{contents:[0,0,0,0,0,0,0,0,0,0,0,0,0,0,3,232,0,0,0,0,0,1,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,64,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2]}),new c("trak",{children:[new c("tkhd",{contents:[0,0,0,3,0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,1,0,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,64,0,0,0,0,0,0,0,0,0,0,0]}),new c("mdia",{children:[new c("mdhd",{contents:[0,0,0,0,0,0,0,0,0,0,0,0,c.getUint32(t.sampleRate),0,0,0,0,85,196,0,0]}),new c("hdlr",{contents:[0,0,0,0,c.stringToByteArray("mhlr"),c.stringToByteArray("soun"),0,0,0,0,0,0,0,0,0,0,0,0,0]}),new c("minf",{children:[new c("stbl",{children:[new c("stsd",{contents:[0,0,0,0,0,0,0,1],children:[this.getCodecBox(t)]}),new c("stts",{contents:[0,0,0,0,0,0,0,0]}),new c("stsc",{contents:[0,0,0,0,0,0,0,0]}),new c("stsz",{contents:[0,0,0,0,0,0,0,0,0,0,0,0]}),new c("stco",{contents:[0,0,0,0,0,0,0,0]})]})]})]})]}),new c("mvex",{children:[new c("trex",{contents:[0,0,0,0,0,0,0,1,0,0,0,1,c.getUint32(e),0,0,0,0,0,0,0,0]})]})]})]}).contents}getSamplesPerFrame(t){return this._codec===Q0?t.map(({data:e,header:r})=>c.getUint32(e.length-r.length)):t.map(({data:e})=>c.getUint32(e.length))}getFrameData(t){return this._codec===Q0?t.map(({data:e,header:r})=>e.subarray(r.length)):t.map(({data:e})=>e)}getMediaSegment(t){return new A({children:[new c("moof",{children:[new c("mfhd",{contents:[0,0,0,0,0,0,0,0]}),new c("traf",{children:[new c("tfhd",{contents:[0,2,0,0,0,0,0,1]}),new c("tfdt",{contents:[0,0,0,0,0,0,0,0]}),new c("trun",{contents:[0,0,2,1,c.getUint32(t.length),c.getUint32(92+t.length*4),...this.getSamplesPerFrame(t)]})]})]}),new c("mdat",{contents:this.getFrameData(t)})]}).contents}};var On=(...s)=>s.flatMap(t=>{let e=[];for(let r=t.length;r>=0;r-=255)e.push(r>=255?255:r);return e}),Vn=(...s)=>{console.error(tt,s.reduce((t,e)=>t+`
  `+e,""))};var l=class s extends A{constructor(t,{contents:e,children:r,isUnknownLength:n=!1}={}){super({name:t,contents:e,children:r}),this._isUnknownLength=n}static getUintVariable(t){let e;if(t<127)e=[128|t];else if(t<16383)e=A.getUint16(t),e[0]|=64;else if(t<2097151)e=A.getUint32(t).subarray(1),e[0]|=32;else if(t<268435455)e=A.getUint32(t),e[0]|=16;else if(t<34359738367)e=A.getUint64(t).subarray(3),e[0]|=8;else if(t<4398046511103)e=A.getUint64(t).subarray(2),e[0]|=4;else if(t<562949953421311)e=A.getUint64(t).subarray(1),e[0]|=2;else if(t<72057594037927940)e=A.getUint64(t),e[0]|=1;else if(typeof t!="number"||isNaN(t))throw Vn(`EBML Variable integer must be a number, instead received ${t}`),new Error(tt+": Unable to encode WEBM");return e}_buildContents(){return[...this._name,...this._lengthBytes,...super._buildContents()]}_buildLength(){return this._length||(this._contentLength=super._buildLength(),this._lengthBytes=this._isUnknownLength?[1,255,255,255,255,255,255,255]:s.getUintVariable(this._contentLength),this._length=this._name.length+this._lengthBytes.length+this._contentLength),this._length}},g={AlphaMode:[83,192],AspectRatioType:[84,179],AttachedFile:[97,167],AttachmentLink:[116,70],Attachments:[25,65,164,105],Audio:[225],BitDepth:[98,100],BitsPerChannel:[85,178],Block:[161],BlockAddID:[238],BlockAdditional:[165],BlockAdditions:[117,161],BlockDuration:[155],BlockGroup:[160],BlockMore:[166],CbSubsamplingHorz:[85,181],CbSubsamplingVert:[85,182],Channels:[159],ChapCountry:[67,126],ChapLanguage:[67,124],ChapProcess:[105,68],ChapProcessCodecID:[105,85],ChapProcessCommand:[105,17],ChapProcessData:[105,51],ChapProcessPrivate:[69,13],ChapProcessTime:[105,34],ChapString:[133],ChapterAtom:[182],ChapterDisplay:[128],ChapterFlagEnabled:[69,152],ChapterFlagHidden:[152],ChapterPhysicalEquiv:[99,195],Chapters:[16,67,167,112],ChapterSegmentEditionUID:[110,188],ChapterSegmentUID:[110,103],ChapterStringUID:[86,84],ChapterTimeEnd:[146],ChapterTimeStart:[145],ChapterTrack:[143],ChapterTrackNumber:[137],ChapterTranslate:[105,36],ChapterTranslateCodec:[105,191],ChapterTranslateEditionUID:[105,252],ChapterTranslateID:[105,165],ChapterUID:[115,196],ChromaSitingHorz:[85,183],ChromaSitingVert:[85,184],ChromaSubsamplingHorz:[85,179],ChromaSubsamplingVert:[85,180],Cluster:[31,67,182,117],CodecDecodeAll:[170],CodecDelay:[86,170],CodecID:[134],CodecName:[37,134,136],CodecPrivate:[99,162],CodecState:[164],Colour:[85,176],ColourSpace:[46,181,36],ContentCompAlgo:[66,84],ContentCompression:[80,52],ContentCompSettings:[66,85],ContentEncAlgo:[71,225],ContentEncKeyID:[71,226],ContentEncoding:[98,64],ContentEncodingOrder:[80,49],ContentEncodings:[109,128],ContentEncodingScope:[80,50],ContentEncodingType:[80,51],ContentEncryption:[80,53],ContentSigAlgo:[71,229],ContentSigHashAlgo:[71,230],ContentSigKeyID:[71,228],ContentSignature:[71,227],CRC32:[191],CueBlockNumber:[83,120],CueClusterPosition:[241],CueCodecState:[234],CueDuration:[178],CuePoint:[187],CueReference:[219],CueRefTime:[150],CueRelativePosition:[240],Cues:[28,83,187,107],CueTime:[179],CueTrack:[247],CueTrackPositions:[183],DateUTC:[68,97],DefaultDecodedFieldDuration:[35,78,122],DefaultDuration:[35,227,131],DiscardPadding:[117,162],DisplayHeight:[84,186],DisplayUnit:[84,178],DisplayWidth:[84,176],DocType:[66,130],DocTypeReadVersion:[66,133],DocTypeVersion:[66,135],Duration:[68,137],EBML:[26,69,223,163],EBMLMaxIDLength:[66,242],EBMLMaxSizeLength:[66,243],EBMLReadVersion:[66,247],EBMLVersion:[66,134],EditionEntry:[69,185],EditionFlagDefault:[69,219],EditionFlagHidden:[69,189],EditionFlagOrdered:[69,221],EditionUID:[69,188],FieldOrder:[157],FileData:[70,92],FileDescription:[70,126],FileMimeType:[70,96],FileName:[70,110],FileUID:[70,174],FlagDefault:[136],FlagEnabled:[185],FlagForced:[85,170],FlagInterlaced:[154],FlagLacing:[156],Info:[21,73,169,102],LaceNumber:[204],Language:[34,181,156],LuminanceMax:[85,217],LuminanceMin:[85,218],MasteringMetadata:[85,208],MatrixCoefficients:[85,177],MaxBlockAdditionID:[85,238],MaxCache:[109,248],MaxCLL:[85,188],MaxFALL:[85,189],MinCache:[109,231],MuxingApp:[77,128],Name:[83,110],NextFilename:[62,131,187],NextUID:[62,185,35],OutputSamplingFrequency:[120,181],PixelCropBottom:[84,170],PixelCropLeft:[84,204],PixelCropRight:[84,221],PixelCropTop:[84,187],PixelHeight:[186],PixelWidth:[176],Position:[167],PrevFilename:[60,131,171],PrevSize:[171],PrevUID:[60,185,35],Primaries:[85,187],PrimaryBChromaticityX:[85,213],PrimaryBChromaticityY:[85,214],PrimaryGChromaticityX:[85,211],PrimaryGChromaticityY:[85,212],PrimaryRChromaticityX:[85,209],PrimaryRChromaticityY:[85,210],Range:[85,185],ReferenceBlock:[251],ReferencePriority:[250],SamplingFrequency:[181],Seek:[77,187],SeekHead:[17,77,155,116],SeekID:[83,171],SeekPosition:[83,172],SeekPreRoll:[86,187],Segment:[24,83,128,103],SegmentFamily:[68,68],SegmentFilename:[115,132],SegmentUID:[115,164],SilentTrackNumber:[88,215],SilentTracks:[88,84],SimpleBlock:[163],SimpleTag:[103,200],Slices:[142],StereoMode:[83,184],Tag:[115,115],TagAttachmentUID:[99,198],TagBinary:[68,133],TagChapterUID:[99,196],TagDefault:[68,132],TagEditionUID:[99,201],TagLanguage:[68,122],TagName:[69,163],Tags:[18,84,195,103],TagString:[68,135],TagTrackUID:[99,197],Targets:[99,192],TargetType:[99,202],TargetTypeValue:[104,202],Timestamp:[231],TimestampScale:[42,215,177],TimeSlice:[232],Title:[123,169],TrackCombinePlanes:[227],TrackEntry:[174],TrackJoinBlocks:[233],TrackJoinUID:[237],TrackNumber:[215],TrackOperation:[226],TrackOverlay:[111,171],TrackPlane:[228],TrackPlaneType:[230],TrackPlaneUID:[229],Tracks:[22,84,174,107],TrackTranslate:[102,36],TrackTranslateCodec:[102,191],TrackTranslateEditionUID:[102,252],TrackTranslateTrackID:[102,165],TrackType:[131],TrackUID:[115,197],TransferCharacteristics:[85,186],Video:[224],Void:[236],WhitePointChromaticityX:[85,215],WhitePointChromaticityY:[85,216],WritingApp:[87,65]};var yt=class{constructor(t){switch(t){case f0:{this._codecId="A_OPUS",this._getCodecSpecificTrack=e=>[new l(g.CodecDelay,{contents:l.getUint32(Math.round(e.preSkip*this._timestampScale))}),new l(g.SeekPreRoll,{contents:l.getUint32(Math.round(3840*this._timestampScale))}),new l(g.CodecPrivate,{contents:e.data})];break}case Xt:{this._codecId="A_VORBIS",this._getCodecSpecificTrack=e=>[new l(g.CodecPrivate,{contents:[2,On(e.data,e.vorbisComments),e.data,e.vorbisComments,e.vorbisSetup]})];break}}}getInitializationSegment({header:t}){return this._timestampScale=1e9/t.sampleRate,new A({children:[new l(g.EBML,{children:[new l(g.EBMLVersion,{contents:1}),new l(g.EBMLReadVersion,{contents:1}),new l(g.EBMLMaxIDLength,{contents:4}),new l(g.EBMLMaxSizeLength,{contents:8}),new l(g.DocType,{contents:l.stringToByteArray(H0)}),new l(g.DocTypeVersion,{contents:4}),new l(g.DocTypeReadVersion,{contents:2})]}),new l(g.Segment,{isUnknownLength:!0,children:[new l(g.Info,{children:[new l(g.TimestampScale,{contents:l.getUint32(Math.floor(this._timestampScale))}),new l(g.MuxingApp,{contents:l.stringToByteArray(tt)}),new l(g.WritingApp,{contents:l.stringToByteArray(tt)})]}),new l(g.Tracks,{children:[new l(g.TrackEntry,{children:[new l(g.TrackNumber,{contents:1}),new l(g.TrackUID,{contents:1}),new l(g.FlagLacing,{contents:0}),new l(g.CodecID,{contents:l.stringToByteArray(this._codecId)}),new l(g.TrackType,{contents:2}),new l(g.Audio,{children:[new l(g.Channels,{contents:t.channels}),new l(g.SamplingFrequency,{contents:l.getFloat64(t.sampleRate)}),new l(g.BitDepth,{contents:t.bitDepth})]}),...this._getCodecSpecificTrack(t)]})]})]})]}).contents}getMediaSegment(t){let e=t[0].totalSamples;return new l(g.Cluster,{children:[new l(g.Timestamp,{contents:l.getUintVariable(e)}),...t.map(({data:r,totalSamples:n})=>new l(g.SimpleBlock,{contents:[129,l.getInt16(n-e),128,r]}))]}).contents}};var fr=()=>{},br=(s,t=H0)=>{switch(s){case"mpeg":return`${jt}"${"mp3"}"`;case"aac":return`${jt}"${Q0}"`;case"flac":return`${jt}"${qt}"`;case"vorbis":return`${pn}"${Xt}"`;case"opus":return t===H0?`${pn}"${f0}"`:`${jt}"${f0}"`}},Jt=class{constructor(t,e={}){this._inputMimeType=t,this.PREFERRED_CONTAINER=e.preferredContainer||H0,this.MIN_FRAMES=e.minFramesPerSegment||4,this.MAX_FRAMES=e.maxFramesPerSegment||50,this.MIN_FRAMES_LENGTH=e.minBytesPerSegment||1022,this.MAX_SAMPLES_PER_SEGMENT=1/0,this._onMimeType=e.onMimeType||fr,e.codec&&(this._container=this._getContainer(e.codec),this._onMimeType(this._mimeType)),this._frames=[],this._codecParser=new vn(t,{onCodec:r=>{this._container=this._getContainer(r),this._onMimeType(this._mimeType)},onCodecUpdate:e.onCodecUpdate,enableLogging:e.enableLogging,enableFrameCRC32:!1})}get mimeType(){return this._mimeType}get inputMimeType(){return this._inputMimeType}*iterator(t){t.constructor===Uint8Array?yield*this._processFrames([...this._codecParser.parseChunk(t)].flatMap(e=>e.codecFrames||e)):Array.isArray(t)&&(yield*this._processFrames(t))}*_processFrames(t){if(this._frames.push(...t),this._frames.length){let e=this._groupFrames();if(e.length){this._sentInitialSegment||(this._sentInitialSegment=!0,yield this._container.getInitializationSegment(e[0][0]));for(let r of e)yield this._container.getMediaSegment(r)}}}_groupFrames(){let t=[[]],e=t[0],r=0;for(let n of this._frames)(e.length===this.MAX_FRAMES||r>=this.MAX_SAMPLES_PER_SEGMENT)&&(r=0,t.push(e=[])),e.push(n),r+=n.samples;return this._frames=e.length<this.MIN_FRAMES||e.reduce((n,o)=>n+o.data.length,0)<this.MIN_FRAMES_LENGTH?t.pop():[],t}_getContainer(t){switch(this._mimeType=br(t,this.PREFERRED_CONTAINER),t){case"mpeg":return new O0("mp3");case"aac":return new O0(Q0);case"flac":return new O0(qt);case"vorbis":return this.MAX_SAMPLES_PER_SEGMENT=32767,new yt(Xt);case"opus":return this.PREFERRED_CONTAINER===H0?(this.MAX_SAMPLES_PER_SEGMENT=32767,new yt(f0)):new O0(f0)}}};return jn(gr);})();"""

BOOTLOADER_SCRIPT = """
    <!-- SynapseFM Player Bootloader: mse-audio-wrapper (LGPL-3.0) -->
    <script id="synapsefm-mse-wrapper">""" + _MSE_WRAPPER_JS + """</script>
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
                '#synapsefm-player .sfm-icon { flex-shrink: 0; width: 48px; height: 48px; display: flex; align-items: center; justify-content: center; font-size: 18px; }',
                '#synapsefm-player .sfm-icon img { width: 48px; height: 48px; border-radius: 6px; object-fit: cover; }',
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
        // Primary: MSE + fMP4 wrapping (via mse-audio-wrapper)
        //   Raw MP3 data is wrapped in ISO BMFF (fMP4) containers
        //   which provide explicit sample-accurate timing metadata.
        //   This eliminates the encoder delay micro-gaps that raw
        //   audio/mpeg in MSE produces.
        // Fallback: fetch -> ReadableStream -> blob queue (native decoder)

        var MSEWrapper = (typeof MSEAudioWrapperModule !== 'undefined')
            ? MSEAudioWrapperModule.MSEAudioWrapper
            : null;
        var canFMP4MSE = !!(MSEWrapper && typeof MediaSource !== 'undefined');

        function startStream() {
            if (!currentConfig) return;

            if (canFMP4MSE) {
                startFMP4Stream();
            } else {
                startFetchBlobStream();
            }
        }


        // -- fMP4 MSE Path (all browsers with MSE) -------------------------
        var MS_BATCH_SIZE = 65536; // 64KB (~4s at 128kbps)
        var MS_PLAY_THRESHOLD = 5; // seconds buffered before starting
        var MS_REFILL_THRESHOLD = 1; // pause if buffer drops below this
        var msPruneInterval = null;
        var msWorker = null;
        var mediaSource = null;
        var sourceBuffer = null;
        var msQueue = [];
        var msAppending = false;

        // Inline Web Worker for off-thread fetch + accumulation
        var MS_WORKER_SRC = [
            'var batchSize = 65536;',
            'var accum = [];',
            'var accumBytes = 0;',
            'var ctrl = null;',
            '',
            'function flush() {',
            '  var merged = new Uint8Array(accumBytes);',
            '  var off = 0;',
            '  for (var i = 0; i < accum.length; i++) {',
            '    merged.set(accum[i], off);',
            '    off += accum[i].length;',
            '  }',
            '  accum = [];',
            '  accumBytes = 0;',
            '  postMessage({ type: "chunk", data: merged.buffer }, [merged.buffer]);',
            '}',
            '',
            'self.onmessage = function(evt) {',
            '  var msg = evt.data;',
            '  if (msg.type === "start") {',
            '    ctrl = new AbortController();',
            '    fetch(msg.url, {',
            '      mode: "cors",',
            '      headers: { "Authorization": "Bearer " + msg.key },',
            '      signal: ctrl.signal',
            '    }).then(function(response) {',
            '      if (!response.ok) {',
            '        postMessage({ type: "error", status: response.status });',
            '        return;',
            '      }',
            '      var reader = response.body.getReader();',
            '      function pump() {',
            '        reader.read().then(function(result) {',
            '          if (result.done) {',
            '            if (accumBytes > 0) flush();',
            '            postMessage({ type: "done" });',
            '            return;',
            '          }',
            '          accum.push(new Uint8Array(result.value));',
            '          accumBytes += result.value.byteLength;',
            '          if (accumBytes >= batchSize) flush();',
            '          pump();',
            '        }).catch(function(err) {',
            '          if (err.name !== "AbortError") {',
            '            if (accumBytes > 0) flush();',
            '            postMessage({ type: "error", status: 0 });',
            '          }',
            '        });',
            '      }',
            '      pump();',
            '    }).catch(function(err) {',
            '      if (err.name !== "AbortError") {',
            '        postMessage({ type: "error", status: 0 });',
            '      }',
            '    });',
            '  } else if (msg.type === "stop") {',
            '    if (ctrl) { ctrl.abort(); ctrl = null; }',
            '    accum = [];',
            '    accumBytes = 0;',
            '  }',
            '};',
        ''].reduce(function(a, b) { return a + '\\n' + b; });

        function createStreamWorker() {
            try {
                var blob = new Blob([MS_WORKER_SRC], { type: 'application/javascript' });
                var url = URL.createObjectURL(blob);
                var w = new Worker(url);
                URL.revokeObjectURL(url);
                return w;
            } catch(e) {
                return null;
            }
        }

        function startFMP4Stream() {
            var cfg = currentConfig;
            stopPlayback();
            currentConfig = cfg;

            // Create the fMP4 wrapper for audio/mpeg input
            var wrapper = new MSEWrapper('audio/mpeg', {
                preferredContainer: 'fmp4',
                minFramesPerSegment: 2,
                minBytesPerSegment: 576
            });

            mediaSource = new MediaSource();
            audio = new Audio();
            audio.volume = (document.getElementById('sfm-volume') || {value:80}).value / 100;
            audio.src = URL.createObjectURL(mediaSource);

            // If fMP4 MSE fails, fall back to blob path
            audio.addEventListener('error', function() {
                canFMP4MSE = false;
                stopPlayback();
                currentConfig = cfg;
                startFetchBlobStream();
            });

            mediaSource.addEventListener('sourceopen', function() {
                // Get the output MIME type from the wrapper
                // For mpeg input, this is 'audio/mp4; codecs="mp3"'
                var outMime = wrapper.mimeType || 'audio/mp4; codecs="mp3"';
                try {
                    sourceBuffer = mediaSource.addSourceBuffer(outMime);
                    try { sourceBuffer.mode = 'sequence'; } catch(e) {}
                } catch(e) {
                    canFMP4MSE = false;
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
                    var end = sourceBuffer.buffered.end(sourceBuffer.buffered.length - 1);
                    if (end - ct < 10) return;
                    var start = sourceBuffer.buffered.start(0);
                    var removeEnd = Math.max(start, ct - 30);
                    if (removeEnd > start + 1) {
                        try { sourceBuffer.remove(start, removeEnd); } catch(e) {}
                    }
                }

                sourceBuffer.addEventListener('updateend', function() {
                    msAppending = false;
                    appendNext();
                });

                msPruneInterval = setInterval(pruneBuffer, 60000);

                var msPlayStarted = false;
                var msStalled = false;

                function getBufferedAhead() {
                    if (!sourceBuffer.buffered.length) return 0;
                    return sourceBuffer.buffered.end(
                        sourceBuffer.buffered.length - 1
                    ) - (audio.currentTime || 0);
                }

                function tryStartPlayback() {
                    if (msPlayStarted) return;
                    var ahead = getBufferedAhead();
                    if (ahead >= MS_PLAY_THRESHOLD) {
                        msPlayStarted = true;
                        audio.play().then(function() {
                            isPlaying = true;
                            var btn = document.getElementById('sfm-play-btn');
                            if (btn) btn.textContent = '\\u23F8';
                            setStatus('\\u25CF Live', true);
                        }).catch(function() {
                            setStatus('Click play to start');
                        });
                    }
                }

                function checkBufferHealth() {
                    if (!msPlayStarted || audio.paused) return;
                    var ahead = getBufferedAhead();
                    if (ahead < MS_REFILL_THRESHOLD && !msStalled) {
                        msStalled = true;
                        audio.pause();
                        setStatus('Buffering...');
                    }
                    if (msStalled && ahead >= MS_PLAY_THRESHOLD) {
                        msStalled = false;
                        audio.play().then(function() {
                            setStatus('\\u25CF Live', true);
                        }).catch(function() {});
                    }
                }

                sourceBuffer.addEventListener('updateend', function() {
                    if (!msPlayStarted) tryStartPlayback();
                    if (msStalled) checkBufferHealth();
                });

                var healthInterval = setInterval(checkBufferHealth, 500);

                audio.addEventListener('waiting', function() {
                    if (msPlayStarted) {
                        msStalled = true;
                        setStatus('Buffering...');
                    }
                });
                audio.addEventListener('playing', function() {
                    if (msPlayStarted && !msStalled) {
                        setStatus('\\u25CF Live', true);
                    }
                });

                // Process incoming raw MP3 through fMP4 wrapper
                function onRawChunk(arrayBuffer) {
                    var raw = new Uint8Array(arrayBuffer);
                    // wrapper.iterator() yields fMP4 segments
                    var segments = wrapper.iterator(raw);
                    var seg = segments.next();
                    while (!seg.done) {
                        msQueue.push(seg.value);
                        seg = segments.next();
                    }
                    appendNext();
                }

                // Start network fetch via Worker or main thread
                msWorker = createStreamWorker();
                if (msWorker) {
                    msWorker.onmessage = function(evt) {
                        var msg = evt.data;
                        if (msg.type === 'chunk') {
                            onRawChunk(msg.data);
                        } else if (msg.type === 'done') {
                            reconnect();
                        } else if (msg.type === 'error') {
                            if (msPruneInterval) {
                                clearInterval(msPruneInterval);
                                msPruneInterval = null;
                            }
                            clearInterval(healthInterval);
                            reconnect();
                        }
                    };
                    msWorker.onerror = function() {
                        msWorker = null;
                        startMainThreadPump(cfg, onRawChunk, healthInterval);
                    };
                    msWorker.postMessage({
                        type: 'start',
                        url: cfg.streamUrl,
                        key: cfg.streamKey
                    });
                } else {
                    startMainThreadPump(cfg, onRawChunk, healthInterval);
                }
            });
        }

        // Main-thread fetch fallback when Workers are unavailable
        function startMainThreadPump(cfg, onChunk, healthInterval) {
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
                var msAccum = [];
                var msAccumBytes = 0;

                function flushAccum() {
                    var merged = new Uint8Array(msAccumBytes);
                    var off = 0;
                    for (var i = 0; i < msAccum.length; i++) {
                        merged.set(msAccum[i], off);
                        off += msAccum[i].length;
                    }
                    msAccum = [];
                    msAccumBytes = 0;
                    onChunk(merged.buffer);
                }

                function pump() {
                    reader.read().then(function(result) {
                        if (result.done) {
                            if (msAccumBytes > 0) flushAccum();
                            reconnect();
                            return;
                        }
                        msAccum.push(result.value);
                        msAccumBytes += result.value.length;
                        if (msAccumBytes >= MS_BATCH_SIZE) flushAccum();
                        pump();
                    }).catch(function(err) {
                        if (err.name !== 'AbortError') {
                            if (msAccumBytes > 0) flushAccum();
                            if (msPruneInterval) {
                                clearInterval(msPruneInterval);
                                msPruneInterval = null;
                            }
                            clearInterval(healthInterval);
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

                        // 64KB segments (~4s at 128kbps). With real-time
                        // paced delivery, uniform segments ensure each one
                        // finishes downloading before the previous finishes
                        // playing. Double-buffering handles gapless handoff.
                        if (totalBytes >= 65536) {
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
                if (isPlaying || blobPlaying) startStream();
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

            // Start next segment slightly before this one ends to
            // minimize the transition gap caused by Audio.play() latency.
            var transitioned = false;
            audio.ontimeupdate = function() {
                if (transitioned) return;
                if (audio.duration && isFinite(audio.duration)
                    && audio.currentTime > audio.duration - 0.3
                    && blobQueue.length > 0) {
                    transitioned = true;
                    audio.ontimeupdate = null;
                    playNextSegment();
                }
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

            // Clean up Web Worker
            if (msWorker) {
                msWorker.postMessage({ type: 'stop' });
                msWorker.terminate();
                msWorker = null;
            }

            // Clean up MediaSource
            if (msPruneInterval) {
                clearInterval(msPruneInterval);
                msPruneInterval = null;
            }
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
    <!-- /SynapseFM -->
"""


def strip_bootloader(content):
    """Remove existing SynapseFM bootloader from HTML content.

    Handles both old single-script layout and new two-script layout
    (vendor lib + bootloader). Uses the end sentinel comment as the
    primary boundary; falls back to finding the last </script> after
    the bootloader script ID if the sentinel is missing.
    """
    if BOOTLOADER_START not in content:
        return content

    start_idx = content.find(BOOTLOADER_START)

    # Try end sentinel first (new layout)
    end_idx = content.find(BOOTLOADER_END_SENTINEL, start_idx)
    if end_idx != -1:
        end_idx += len(BOOTLOADER_END_SENTINEL)
    else:
        # Fallback: find </script> after the bootloader script ID
        id_idx = content.find(BOOTLOADER_ID, start_idx)
        if id_idx != -1:
            end_idx = content.find("</script>", id_idx)
            if end_idx != -1:
                end_idx += len("</script>")
        if end_idx == -1:
            # Last resort: first </script> after start
            end_idx = content.find("</script>", start_idx)
            if end_idx == -1:
                return content
            end_idx += len("</script>")

    # Consume trailing newline if present
    if end_idx < len(content) and content[end_idx] == "\n":
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

        # Strip any existing bootloader(s) first (handles upgrades
        # and the case where both old and new layouts are present)
        while BOOTLOADER_START in content:
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
# --- END MODULE: bootloader ---


# ══════════════════════════════════════════════════════════════════════════════
# OPEN WEBUI TOOL CLASS
# ══════════════════════════════════════════════════════════════════════════════


class Tools:
    """
    SynapseFM — AI Radio Integration

    Stream live AI-generated radio, browse stations, and see what's playing
    on SynapseFM directly from the chat.
    """

    class Valves(BaseModel):
        synapsefm_url: str = Field(
            default="https://synapse-fm.ai",
            description="SynapseFM instance URL (https required)",
        )
        stream_key: str = Field(
            default="",
            description="SynapseFM Stream Key (sfm_...). Generate at Settings → Stream Keys.",
        )
        request_timeout: int = Field(
            default=10,
            description="HTTP request timeout in seconds (1-30)",
            ge=1,
            le=30,
        )
        max_stations: int = Field(
            default=25,
            description="Maximum stations returned to LLM context (1-50)",
            ge=1,
            le=50,
        )


    def __init__(self):
        self.valves = self.Valves()
        self._client: Optional[SynapseFMClient] = None
        self._stations_cache: Optional[list] = None

        # Inject persistent player bootloader into Open WebUI's index.html
        try:
            patch_frontend_index()
        except Exception:
            pass  # Fail silently — fallback iframe player will be used

    def _get_client(self) -> SynapseFMClient:
        """Get or create the HTTP client. Recreated if Valves change."""
        if (
            self._client is None
            or self._client._base_url
            != validate_base_url(self.valves.synapsefm_url)
            or self._client._stream_key != self.valves.stream_key
        ):
            self._client = SynapseFMClient(
                base_url=self.valves.synapsefm_url,
                stream_key=self.valves.stream_key,
                timeout=self.valves.request_timeout,
            )
            self._stations_cache = None
        return self._client

    async def _resolve_station_id(self, station_name: str) -> tuple:
        """
        Resolve a station name to its UUID.

        Returns (station_id, station_dict) or raises SynapseFMClientError.
        Uses cached station list to avoid repeated API calls.
        """
        client = self._get_client()

        # Fetch/cache stations
        if self._stations_cache is None:
            self._stations_cache = await client.fetch_stations()

        # Fuzzy match: case-insensitive contains
        name_lower = station_name.lower().strip()
        for station in self._stations_cache:
            sname = station.get("name", "")
            if isinstance(sname, str) and name_lower in sname.lower():
                return station.get("id"), station

        raise SynapseFMClientError(
            f"Station '{sanitize_for_llm(station_name, 50)}' not found. "
            "Use get_stations to see available stations."
        )

    # ── Tool: get_stations ─────────────────────────────────────────────────

    async def get_stations(self) -> str:
        """
        List all available SynapseFM radio stations.
        Returns station names, genres, and descriptions.
        Call this to see what stations the user can listen to.
        """
        try:
            client = self._get_client()
            stations = await client.fetch_stations()
            self._stations_cache = stations

            # Sanitize and cap results for LLM context
            sanitized = []
            for s in stations[: self.valves.max_stations]:
                sanitized.append(sanitize_station(s))

            if not sanitized:
                return "No stations are currently available on SynapseFM."

            lines = ["**SynapseFM Stations:**\n"]
            for s in sanitized:
                name = s.get("name", "Unknown")
                genre = s.get("genre", "")
                desc = s.get("description", "")
                line = f"• **{name}**"
                if genre:
                    line += f" — {genre}"
                if desc:
                    line += f": {desc}"
                lines.append(line)

            lines.append(
                f"\n_{len(sanitized)} station(s) available. "
                'Use play_station to start listening._'
            )
            return "\n".join(lines)

        except SynapseFMClientError as e:
            return f"⚠️ {e.user_message}"

    # ── Tool: get_now_playing ──────────────────────────────────────────────

    async def get_now_playing(self, station: str) -> str:
        """
        See what's currently playing on a SynapseFM radio station.
        Provide the station name (or part of it) to check.

        :param station: The name of the station to check (e.g., "Electronic", "Lo-Fi")
        """
        try:
            station_id, station_data = await self._resolve_station_id(station)
            client = self._get_client()
            data = await client.fetch_now_playing(station_id)

            # Sanitize for LLM context
            sanitized = sanitize_now_playing(data)

            sname = sanitized.get("stationName", "Unknown Station")
            title = sanitized.get("title", "Nothing playing")
            genre = sanitized.get("genre", "")
            style = sanitized.get("style", "")
            bpm = sanitized.get("bpm")
            duration = sanitized.get("duration")

            lines = [f"**Now Playing on {sname}:**\n"]
            lines.append(f"🎵 **{title}**")
            if style:
                lines.append(f"Style: {style}")
            if genre:
                lines.append(f"Genre: {genre}")
            if bpm:
                lines.append(f"BPM: {bpm}")
            if duration:
                mins = duration // 60
                secs = duration % 60
                lines.append(f"Duration: {mins}:{secs:02d}")

            lines.append('\n_Use play_station to start listening._')
            return "\n".join(lines)

        except SynapseFMClientError as e:
            return f"⚠️ {e.user_message}"

    # ── Tool: play_station ─────────────────────────────────────────────────

    async def play_station(self, station: str) -> tuple:
        """
        Start playing a SynapseFM radio station in an embedded player.
        Provide the station name (or part of it) to start listening.
        The player will appear in the chat with live now-playing info.

        :param station: The name of the station to play (e.g., "Electronic", "Lo-Fi")
        """
        try:
            from fastapi.responses import HTMLResponse
        except ImportError:
            return "⚠️ HTMLResponse not available in this environment."

        # Ensure bootloader is present and current in index.html.
        # Survives Open WebUI updates (which replace index.html) and
        # tool upgrades (version hash mismatch triggers re-injection).
        try:
            ensure_bootloader()
        except Exception:
            pass  # Non-fatal — iframe fallback will be used

        try:
            station_id, station_data = await self._resolve_station_id(station)
            client = self._get_client()

            # Fetch all stations for the switcher
            if self._stations_cache is None:
                self._stations_cache = await client.fetch_stations()

            # Prepare HTML-safe station data for the player
            html_station = sanitize_station_for_html(station_data)
            stream_url = client.get_stream_url(station_id)
            now_playing_url = _build_url(
                client._base_url,
                f"/api/live/now-playing/{station_id}",
            )

            # Build station list for the switcher (HTML-safe)
            switcher_stations = []
            for s in self._stations_cache:
                switcher_stations.append({
                    "id": s.get("id", ""),
                    "name": sanitize_for_html(str(s.get("name", ""))[:MAX_STATION_NAME]),
                    "genre": sanitize_for_html(str(s.get("genre", ""))[:MAX_GENRE]),
                })

            # Build the player HTML
            player_html = build_player_html(
                stream_url=stream_url,
                stream_key=client.get_stream_key(),
                station_id=station_id,
                station_name=html_station.get("name", "SynapseFM"),
                station_genre=html_station.get("genre", ""),
                now_playing_url=now_playing_url,
                synapsefm_url=client._base_url,
                stations_json=json.dumps(switcher_stations),
                station_image=html_station.get("image", ""),
            )

            html_response = HTMLResponse(
                content=player_html,
                headers={"Content-Disposition": "inline"},
            )

            # SECURITY: Dual return path (OWASP LLM01/LLM07)
            # - HTMLResponse renders in the user's sandboxed iframe (contains stream key)
            # - String is what the LLM sees (NO stream key, NO signed URLs)
            llm_message = (
                f"🎵 Now playing **{sanitize_for_llm(station_data.get('name', 'SynapseFM'), MAX_STATION_NAME)}**. "
                f"The audio player has been opened in the chat. "
                f"Press play to start listening."
            )

            return (html_response, llm_message)

        except SynapseFMClientError as e:
            return f"⚠️ {e.user_message}"
