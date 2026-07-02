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
# The full player_builder module is in modules/player_builder.py
# For bundled deployment, it must be inlined. For development, import it:
try:
    from modules.player_builder import build_player_html
except ImportError:
    # If running as a single pasted file, the function won't be found.
    # The bundled version (README instructions) will have it inlined.
    raise ImportError(
        "player_builder module not found. "
        "If deploying to Open WebUI, use the bundled version from README.md."
    )
# --- END MODULE: player_builder ---

# --- BEGIN MODULE: bootloader ---
try:
    from modules.bootloader import patch_frontend_index, ensure_bootloader
except ImportError:
    raise ImportError(
        "bootloader module not found. "
        "If deploying to Open WebUI, use the bundled version from README.md."
    )
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
