"""
sanitizer.py — Output sanitization for two contexts.

1. LLM context: text returned as tool output (prompt injection prevention)
2. HTML context: content embedded in the player iframe (XSS prevention)

Security controls (OWASP LLM01, LLM02, XSS Prevention):
- Key pattern redaction (sfm_* regex)
- HTML entity escaping for all dynamic content
- Length truncation (prevents context window flooding)
- Field allowlisting (only known-safe fields extracted from API responses)
"""

import html
import re
from typing import Any

# ── Constants ──────────────────────────────────────────────────────────────────

# Matches SynapseFM stream key format: sfm_ followed by 8+ alphanumeric/dash/underscore
KEY_PATTERN = re.compile(r"sfm_[A-Za-z0-9_\-]{8,}")

# Maximum field lengths for LLM context (prevents context window flooding)
MAX_STATION_NAME = 100
MAX_STATION_DESCRIPTION = 200
MAX_TRACK_TITLE = 200
MAX_GENRE = 50


# ── LLM Context Sanitization ──────────────────────────────────────────────────

def sanitize_for_llm(text: str, max_length: int = 200) -> str:
    """
    Sanitize a string for inclusion in LLM tool output.

    - Strips potential stream key patterns
    - Truncates to max_length
    - Strips control characters

    Returns a safe string suitable for LLM context.
    """
    if not isinstance(text, str):
        return ""
    # Remove any stream key patterns
    sanitized = KEY_PATTERN.sub("[REDACTED]", text)
    # Strip control characters (except newlines/spaces)
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", sanitized)
    # Truncate
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "…"
    return sanitized


# ── HTML Context Sanitization ─────────────────────────────────────────────────

def sanitize_for_html(text: str) -> str:
    """
    HTML-escape a string for safe embedding in the player iframe.

    Uses Python's html.escape which converts:
    - & → &amp;
    - < → &lt;
    - > → &gt;
    - " → &quot;
    - ' → &#x27;
    """
    if not isinstance(text, str):
        return ""
    return html.escape(text, quote=True)


# ── Station Data Sanitization ─────────────────────────────────────────────────

# Allowlisted fields for station data (defense against future API additions)
STATION_ALLOWED_FIELDS = {"id", "name", "genre", "description", "isPrivate", "image"}


def sanitize_station(raw: dict) -> dict:
    """
    Extract and sanitize station data for LLM context.

    Only allowlisted fields are returned. All string values are
    sanitized for LLM consumption (key redaction + truncation).
    """
    if not isinstance(raw, dict):
        return {}

    result = {}
    for key in STATION_ALLOWED_FIELDS:
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            max_len = {
                "name": MAX_STATION_NAME,
                "description": MAX_STATION_DESCRIPTION,
                "genre": MAX_GENRE,
            }.get(key, 200)
            result[key] = sanitize_for_llm(value, max_len)
        elif isinstance(value, bool):
            result[key] = value
        # Skip non-string/non-bool fields (e.g., nested objects)
    return result


def sanitize_station_for_html(raw: dict) -> dict:
    """
    Extract and sanitize station data for HTML embedding.

    Returns HTML-escaped values safe for direct insertion into
    the player template via textContent or escaped attribute values.
    """
    if not isinstance(raw, dict):
        return {}

    return {
        "id": sanitize_for_html(str(raw.get("id", ""))),
        "name": sanitize_for_html(
            str(raw.get("name", "Unknown Station"))[:MAX_STATION_NAME]
        ),
        "genre": sanitize_for_html(str(raw.get("genre", ""))[:MAX_GENRE]),
        "image": sanitize_for_html(str(raw.get("image", ""))),
    }


# ── Now-Playing Data Sanitization ─────────────────────────────────────────────

# Allowlisted fields for now-playing data
NOW_PLAYING_ALLOWED_FIELDS = {"title", "style", "bpm", "duration"}


def sanitize_now_playing(raw: dict) -> dict:
    """
    Extract and sanitize now-playing data for LLM context.

    Only allowlisted metadata fields are returned. All other fields
    from the API response are stripped.
    """
    if not isinstance(raw, dict):
        return {}

    # Extract the nowPlaying sub-object if wrapped
    np = raw.get("nowPlaying", raw)
    if not isinstance(np, dict):
        return {"status": "Nothing playing"}

    result = {
        "stationName": sanitize_for_llm(
            str(raw.get("stationName", "")), MAX_STATION_NAME
        ),
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


def sanitize_now_playing_for_html(raw: dict) -> dict:
    """
    Extract and sanitize now-playing data for HTML player display.
    """
    if not isinstance(raw, dict):
        return {}

    np = raw.get("nowPlaying", raw)
    if not isinstance(np, dict):
        return {}

    return {
        "title": sanitize_for_html(
            str(np.get("title", "Loading..."))[:MAX_TRACK_TITLE]
        ),
        "style": sanitize_for_html(str(np.get("style", ""))[:100]),
        "bpm": np.get("bpm") if isinstance(np.get("bpm"), int) else None,
        "duration": (
            np.get("duration") if isinstance(np.get("duration"), int) else None
        ),
    }
