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
MAX_JSON_BYTES = 128 * 1024
MAX_INGRESS_TEXT_CHARS = 100_000
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
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or "\\" in value:
        raise LibraryError("invalid source URL", "unsafe_url", 422)
    if parts.username is not None or parts.password is not None:
        raise LibraryError("credentialed URL is not accepted", "unsafe_url", 422)
    host = (parts.hostname or "").lower().rstrip(".")
    if host in {"mp.weixin.qq.com", "weixin.qq.com"}:
        raw_pairs = parse_qsl(parts.query, keep_blank_values=True)
        if any(sum(k == key for k, _ in raw_pairs) > 1 for key in ("__biz", "mid", "idx", "sn")):
            raise LibraryError("ambiguous WeChat identity", "unsafe_url", 422)
        query = dict(raw_pairs)
        keys = ("__biz", "mid", "idx", "sn")
        if (parts.path == "/s" and all(re.fullmatch(r"[A-Za-z0-9_=-]{1,128}", query.get(k, "")) for k in keys[:3])
                and (not query.get("sn") or re.fullmatch(r"[A-Za-z0-9_=-]{1,128}", query["sn"]))):
            pairs = [(k, query[k]) for k in keys if query.get(k) and re.fullmatch(r"[A-Za-z0-9_=-]{1,128}", query[k])]
            return urlunsplit((parts.scheme.lower(), host, "/s", urlencode(pairs), ""))
        return urlunsplit((parts.scheme.lower(), host, parts.path, "", ""))
    if host in {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com"}:
        return urlunsplit((parts.scheme.lower(), host, parts.path, "", ""))
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_QUERY and not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(pairs), ""))


def safe_locator(value: str | None) -> str | None:
    if not value:
        return None
    try:
        if (urlsplit(value).hostname or "").lower() not in {"mp.weixin.qq.com", "weixin.qq.com", "xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com"}:
            return "private-input"
        return canonical_url(value)
    except (ValueError, LibraryError):
        return "private-input"


def safe_error(exc: Exception | str) -> str:
    text = str(exc)
    text = re.sub(r"https?://[^\s'\"]+", "source URL", text)
    text = re.sub(r"(?i)(token|secret|authorization|password)\s*[=:]\s*[^\s,;]+", r"\1=[REDACTED]", text)
    return text[:240] or "operation failed"


def require_object(value: Any, *, max_bytes: int = MAX_JSON_BYTES, canonical: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LibraryError("JSON object required")
    encoded = (json.dumps(value, ensure_ascii=True, separators=(",", ":")) if canonical
               else json.dumps(value, ensure_ascii=False))
    if len(encoded.encode("utf-8")) > max_bytes:
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
