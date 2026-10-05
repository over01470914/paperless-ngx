"""Shared schema, privacy helpers, and bounded validation."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import VERSION

MAX_BATCH_ITEMS = 500
STATUSES = ("queued", "fetching", "needs_login", "needs_ocr", "enriching", "importing",
            "confirmed", "duplicate", "failed", "cancelled", "partial", "blocked")
TERMINAL = {"confirmed", "duplicate", "failed", "cancelled", "partial", "blocked",
            "needs_login", "needs_ocr"}
LIB_FIELDS = {
    "lib_platform": "string", "lib_source_id": "string", "lib_original_url": "longtext",
    "lib_canonical_url": "longtext", "lib_author": "string", "lib_publish_date": "date",
    "lib_fetched_at": "string", "lib_content_hash": "string", "lib_completeness": "string",
    "lib_extraction_status": "string", "lib_provenance": "longtext", "lib_analysis": "longtext",
    "lib_analysis_version": "string", "lib_reading_state": "string", "lib_starred": "boolean",
    "lib_pending": "boolean",
}
SENSITIVE_QUERY = {"token", "access_token", "xsec_token", "auth", "authorization", "code",
                   "login", "session", "sig", "signature", "key", "password"}
TRACKING_QUERY = SENSITIVE_QUERY | {"from", "source", "scene", "utm_source", "utm_medium",
                                    "utm_campaign", "utm_term", "utm_content", "feature"}


class LibraryError(ValueError):
    """Error safe to return through the local API."""

    def __init__(self, message: str, code: str = "invalid_request", status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def canonical_url(value: str) -> str:
    """Return a citation URL with tracking and credential-like parameters removed."""
    parts = urlsplit(value)
    if parts.username is not None or parts.password is not None:
        raise LibraryError("credentialed URL is not accepted", "unsafe_url", 422)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_QUERY and not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(pairs), ""))


def safe_locator(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return canonical_url(value)
    except (ValueError, LibraryError):
        return "private-input"


def safe_error(exc: Exception | str) -> str:
    text = str(exc)
    text = re.sub(r"https?://[^\s'\"]+", "source URL", text)
    text = re.sub(r"(?i)(token|secret|authorization|password)\s*[=:]\s*[^\s,;]+", r"\1=[REDACTED]", text)
    return text[:240] or "operation failed"


def require_object(value: Any, *, max_bytes: int = 128 * 1024) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LibraryError("JSON object required")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > max_bytes:
        raise LibraryError("JSON request exceeds limit")
    return value


def bearer_ok(header: str | None, token: str) -> bool:
    return bool(header and header.startswith("Bearer ") and
                hmac.compare_digest(header[7:], token))


@dataclass(frozen=True)
class Evidence:
    document_id: int
    canonical_url: str | None
    title: str
    author: str | None
    published_at: str | None
    fetched_at: str | None
    completeness: str
    section: str
    start: int
    end: int
    excerpt: str

    def as_dict(self) -> dict[str, Any]:
        value = self.__dict__.copy()
        value["excerpt_hash"] = sha256_bytes(self.excerpt.encode("utf-8"))
        return value
