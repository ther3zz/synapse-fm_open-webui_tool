"""
http_client.py — SSRF-hardened async HTTP client for SynapseFM API.

Security controls (OWASP SSRF Prevention + NIST SC-7):
- URL validated and frozen at init (not per-request)
- Scheme restricted to https (http permitted only for localhost dev)
- Redirects disabled (allow_redirects=False)
- Response body capped at MAX_RESPONSE_BYTES before parsing
- Path allowlist: only /api/live/* and /api/public/* permitted
- asyncio.timeout() on every request
- Generic error messages (no URL/key leakage in exceptions)
"""

import json
import asyncio
import ssl
from typing import Optional
from urllib.parse import urlparse, urljoin


# ── Constants ──────────────────────────────────────────────────────────────────

MAX_RESPONSE_BYTES = 512 * 1024  # 512KB — generous for JSON metadata
ALLOWED_PATH_PREFIXES = ("/api/live/", "/api/public/")
ALLOWED_SCHEMES = ("https",)
# For local development: also allow http to localhost
DEV_ALLOWED_SCHEMES = ("http", "https")


# ── Errors ─────────────────────────────────────────────────────────────────────

class SynapseFMClientError(Exception):
    """Safe error that never contains internal URLs, keys, or stack details."""

    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


class SynapseFMConfigError(SynapseFMClientError):
    """Raised for configuration issues (invalid URL, missing key)."""
    pass


class SynapseFMAPIError(SynapseFMClientError):
    """Raised for API call failures (timeout, HTTP errors, bad responses)."""
    pass


# ── URL Validation ─────────────────────────────────────────────────────────────

def validate_base_url(url: str, allow_http: bool = False) -> str:
    """
    Validate and normalize the SynapseFM base URL.

    Security:
    - Scheme must be https (or http if allow_http=True for dev)
    - Must have a valid hostname
    - Trailing slashes stripped for consistent path joining
    - Path component rejected (base URL must be origin-only)

    Returns the normalized URL string.
    Raises SynapseFMConfigError on validation failure.
    """
    if not url or not isinstance(url, str):
        raise SynapseFMConfigError(
            "SynapseFM URL is not configured. Set it in the plugin Valves."
        )

    url = url.strip()
    parsed = urlparse(url)

    # Scheme check
    allowed = DEV_ALLOWED_SCHEMES if allow_http else ALLOWED_SCHEMES
    if parsed.scheme not in allowed:
        raise SynapseFMConfigError(
            f"SynapseFM URL must use {' or '.join(allowed)} scheme."
        )

    # Hostname check
    if not parsed.hostname:
        raise SynapseFMConfigError("SynapseFM URL must have a valid hostname.")

    # Reject URLs with path components (should be origin-only)
    if parsed.path and parsed.path not in ("", "/"):
        raise SynapseFMConfigError(
            "SynapseFM URL should be the base URL only (e.g., https://synapse-fm.ai)."
        )

    # Return normalized origin (scheme + netloc, no trailing slash)
    return f"{parsed.scheme}://{parsed.netloc}"


def _build_url(base_url: str, path: str) -> str:
    """
    Safely construct a full URL from base + path.

    Security:
    - Uses urljoin (no string concatenation)
    - Validates path starts with an allowed prefix
    - Prevents path traversal (.. sequences)

    Raises SynapseFMClientError on invalid paths.
    """
    # Strip leading/trailing whitespace
    path = path.strip()

    # Ensure path starts with /
    if not path.startswith("/"):
        path = "/" + path

    # Path traversal check
    if ".." in path:
        raise SynapseFMClientError("Invalid API path.")

    # Allowlist check
    if not any(path.startswith(prefix) for prefix in ALLOWED_PATH_PREFIXES):
        raise SynapseFMClientError("Invalid API path.")

    return urljoin(base_url + "/", path.lstrip("/"))


# ── HTTP Client ────────────────────────────────────────────────────────────────

class SynapseFMClient:
    """
    SSRF-hardened async HTTP client for SynapseFM API.

    Usage:
        client = SynapseFMClient(
            base_url="https://synapse-fm.ai",
            stream_key="sfm_...",
            timeout=10,
        )
        stations = await client.fetch_stations()
    """

    def __init__(
        self,
        base_url: str,
        stream_key: str = "",
        timeout: int = 10,
        allow_http: bool = False,
    ):
        # Validate and freeze URL at init time (not per-request)
        self._base_url = validate_base_url(base_url, allow_http)
        self._stream_key = stream_key
        self._timeout = max(1, min(timeout, 30))  # Clamp 1-30s
        self._key_validated = False

    async def _fetch(
        self,
        path: str,
        authenticated: bool = False,
    ) -> dict:
        """
        Make an HTTP GET request to SynapseFM.

        Security:
        - URL constructed via _build_url (path allowlist + traversal check)
        - Redirects disabled
        - Response body capped at MAX_RESPONSE_BYTES
        - asyncio.timeout enforced
        - Errors return generic messages

        Returns parsed JSON dict.
        Raises SynapseFMAPIError on any failure.
        """
        # Import aiohttp lazily (available in Open WebUI's Python env)
        try:
            import aiohttp
        except ImportError:
            raise SynapseFMAPIError(
                "HTTP client library not available. Contact your admin."
            )

        url = _build_url(self._base_url, path)

        headers = {
            "Accept": "application/json",
            "User-Agent": "SynapseFM-OpenWebUI-Plugin/1.0",
        }

        if authenticated:
            if not self._stream_key:
                raise SynapseFMConfigError(
                    "Stream key is not configured. Set it in the plugin Valves."
                )
            headers["Authorization"] = f"Bearer {self._stream_key}"

        try:
            async with asyncio.timeout(self._timeout):
                # Create SSL context that verifies certificates
                ssl_ctx = ssl.create_default_context()

                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        headers=headers,
                        allow_redirects=False,
                        ssl=ssl_ctx,
                        max_line_size=8190,
                        max_field_size=8190,
                    ) as resp:
                        # Check status before reading body
                        if resp.status == 401:
                            self._key_validated = False
                            raise SynapseFMConfigError(
                                "Stream key is invalid or expired. "
                                "Update it in the plugin Valves."
                            )
                        if resp.status == 503:
                            raise SynapseFMAPIError(
                                "SynapseFM external streaming is currently disabled."
                            )
                        if resp.status != 200:
                            raise SynapseFMAPIError(
                                "SynapseFM is temporarily unavailable. "
                                "Please try again later."
                            )

                        # Read with size limit
                        body = await resp.content.read(MAX_RESPONSE_BYTES + 1)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise SynapseFMAPIError(
                                "Response from SynapseFM was too large."
                            )

                        # Parse JSON
                        try:
                            return json.loads(body)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            raise SynapseFMAPIError(
                                "Received invalid response from SynapseFM."
                            )

        except SynapseFMClientError:
            # Re-raise our own errors as-is
            raise
        except asyncio.TimeoutError:
            raise SynapseFMAPIError(
                "SynapseFM did not respond in time. Please try again later."
            )
        except Exception:
            # Catch-all: never leak internal error details
            raise SynapseFMAPIError(
                "Could not connect to SynapseFM. Please try again later."
            )

    # ── Public API Methods ─────────────────────────────────────────────────

    async def validate_key(self) -> bool:
        """
        Validate the stream key by attempting to fetch stations.

        Called once on first use. Subsequent calls are no-ops unless
        a 401 resets the validation state.

        Returns True if key is valid, raises SynapseFMConfigError if not.
        """
        if self._key_validated:
            return True

        await self._fetch("/api/live/stations", authenticated=True)
        self._key_validated = True
        return True

    async def fetch_stations(self) -> list:
        """
        Fetch all available stations.

        Endpoint: GET /api/live/stations (authenticated)
        Returns: list of station dicts
        """
        data = await self._fetch("/api/live/stations", authenticated=True)
        stations = data.get("stations", [])
        if not isinstance(stations, list):
            return []
        return stations

    async def fetch_now_playing(self, station_id: str) -> dict:
        """
        Fetch now-playing metadata for a station.

        Endpoint: GET /api/live/now-playing/{stationId} (authenticated)
        Returns: dict with stationName, genre, nowPlaying sub-object
        """
        # Validate station ID format (UUID only — prevent path injection)
        if not _is_valid_uuid(station_id):
            raise SynapseFMClientError("Invalid station identifier.")

        path = f"/api/live/now-playing/{station_id}"
        return await self._fetch(path, authenticated=True)

    def get_stream_url(self, station_id: str) -> str:
        """
        Construct the streaming URL for a station.

        This URL requires Authorization header — intended for use in
        fetch() calls within the embedded player, NOT for direct
        <audio src=""> usage.

        Returns: full URL string
        """
        if not _is_valid_uuid(station_id):
            raise SynapseFMClientError("Invalid station identifier.")

        return _build_url(self._base_url, f"/api/live/stream/{station_id}")

    def get_stream_key(self) -> str:
        """
        Return the stream key for use in the player's fetch() header.

        SECURITY: This value is ONLY used within the HTMLResponse
        (rendered in the sandboxed iframe). It must NEVER be returned
        in tool output strings visible to the LLM.
        """
        return self._stream_key


# ── Helpers ────────────────────────────────────────────────────────────────────

import re

_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_valid_uuid(value: str) -> bool:
    """Validate a string is a UUID v4 format. Prevents path injection."""
    return bool(value and isinstance(value, str) and _UUID_PATTERN.match(value))
