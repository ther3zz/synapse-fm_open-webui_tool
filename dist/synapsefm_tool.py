"""
SynapseFM Integration Tool for Open WebUI

Provides LLM-accessible tools to browse stations, see what's playing,
and stream live AI-generated radio from SynapseFM — all within the
Open WebUI chat interface.

Security: OWASP API Security Top 10, OWASP LLM Top 10 (2025), NIST SP 800-53
- Zero PII exposure (aggregate metadata only)
- Stream key encrypted in Valves (never sent to LLM)
- SSRF-hardened HTTP client (path allowlist, redirect blocking, size caps)
- Output sanitization (key pattern redaction, HTML escaping)
- Dual return path (HTMLResponse for player, plain string for LLM context)

Installation: Paste this file into Open WebUI → Workspace → Tools
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
        // Primary: MediaSource + appendBuffer (low-latency, gapless)
        // Fallback: fetch -> ReadableStream -> blob queue (native decoder)
        // MediaSource.isTypeSupported() handles browser capability detection.

        var canMediaSource = (
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
                    // Sequence mode: ignore internal MP3 timestamps and
                    // play chunks in append order. Prevents timestamp
                    // discontinuities from causing micro-gaps in live streams.
                    try { sourceBuffer.mode = 'sequence'; } catch(e) {}
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
                    var end = sourceBuffer.buffered.end(sourceBuffer.buffered.length - 1);
                    // Skip pruning if buffer ahead of playback is thin.
                    // remove() blocks appendBuffer() while active, so
                    // pruning a thin buffer causes underruns.
                    if (end - ct < 10) return;
                    var start = sourceBuffer.buffered.start(0);
                    // Keep 30s behind currentTime (generous to avoid
                    // pruning during brief playback stalls)
                    var removeEnd = Math.max(start, ct - 30);
                    if (removeEnd > start + 1) {
                        try { sourceBuffer.remove(start, removeEnd); } catch(e) {}
                    }
                }

                sourceBuffer.addEventListener('updateend', function() {
                    msAppending = false;
                    appendNext();
                });

                // Periodic buffer pruning (every 60s)
                msPruneInterval = setInterval(pruneBuffer, 60000);

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

                    // Defer play until first data arrives in the buffer
                    var playOnce = function() {
                        sourceBuffer.removeEventListener('updateend', playOnce);
                        audio.play().then(function() {
                            isPlaying = true;
                            setStatus('\\u25CF Live', true);
                        }).catch(function() {
                            setStatus('Click play to start');
                        });
                    };
                    sourceBuffer.addEventListener('updateend', playOnce);
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
