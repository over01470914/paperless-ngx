"""Import worker with explicit uncertainty reconciliation."""
from __future__ import annotations

import json
import time
import uuid
import mimetypes
from pathlib import Path
from typing import Any

from .ledger import Ledger
from .model import LibraryError, now, normalized_text, sha256_bytes
from .native import archive_text
from .sources import (SafeFetcher, canonical_url, checked_file, extract_file, parse_wechat_html, platform_for,
                      stable_id)


class LibraryWorker:
    def __init__(self, ledger: Ledger, native: Any, *, fetcher: SafeFetcher | None = None,
                 accepted_roots: list[str] | None = None, rate_limit_seconds: float = 1.5, sleep=time.sleep):
        self.ledger, self.native, self.fetcher = ledger, native, fetcher
        self.accepted_roots, self.sleep, self.owner = accepted_roots or [], sleep, uuid.uuid4().hex
        self.rate_limit_seconds, self._last_fetch = max(1.5, float(rate_limit_seconds)), 0.0

    def run_once(self) -> bool:
        if not self.ledger.acquire_worker(self.owner): return False
        item = self.ledger.claim(self.owner)
        if not item: return False
        try: self.process(item)
        except LibraryError as exc:
            self.ledger.transition(item["id"], "failed" if exc.code not in {"identity_pending", "spool_missing"} else "blocked",
                                   "failed", reason=exc)
        except Exception as exc:
            self.ledger.transition(item["id"], "failed", "failed", reason=exc)
        return True

    def idle_loop(self, stop) -> None:
        while not stop.is_set():
            try:
                if not self.run_once(): stop.wait(0.5)
            except Exception:
                # A broken connection is surfaced by the next API operation; do
                # not let one unexpected worker exception silently kill the loop.
                stop.wait(1.0)

    def process(self, item: dict) -> None:
        if item.get("lease_owner") and not self.ledger.renew(self.owner, item["id"]):
            raise LibraryError("worker lease was lost", "lease_lost", 409)
        if item.get("metadata", {}).get("reanalyze"):
            result = self.native.reanalyze(item["document_id"], item["metadata"]["reanalyze_version"])
            self.ledger.transition(item["id"], "confirmed", "reanalyzed" if result == "updated" else "analysis_unchanged",
                                   metadata={**item["metadata"], "reanalyze": False, "analysis_version": item["metadata"]["reanalyze_version"]})
            return
        if item["task_uuid"]:
            self._reconcile_task(item); return
        if item["attempts"] > 3:
            self.ledger.transition(item["id"], "failed", "retry", reason="retry limit reached")
            return
        if item["status"] == "importing" or item.get("metadata", {}).get("upload_intent"):
            self._reconcile_without_task(item); return
        data = self._extract(item)
        if data.get("terminal"):
            self.ledger.transition(item["id"], data["terminal"], "extract", reason=data.get("reason")); return
        source_id, body = data["source_id"], data["body"]
        canonical, content_hash = data.get("canonical_url"), sha256_bytes(data["file_bytes"] if data.get("file_bytes") is not None else body.encode())
        self.ledger.transition(item["id"], "importing", "upload", source_id=source_id,
                               canonical_url=canonical, content_hash=content_hash,
                               metadata={**item["metadata"], "upload_intent": True})
        current = self.ledger.get_item(item["id"])
        existing = self.native.find_existing(source_id, canonical, content_hash, data.get("original_url"))
        if existing:
            self.ledger.transition(item["id"], "duplicate", "reconciled", document_id=existing)
            self.ledger.clean_spool(self.ledger.get_item(item["id"])); return
        task = self.native.upload(data, archive_text(body), source_id, canonical, content_hash)
        if not isinstance(task, str) or not task:
            # Intent was persisted; retry enters reconciliation, never second upload.
            raise LibraryError("upload outcome is uncertain; manual reconciliation required", "upload_uncertain", 409)
        self.ledger.transition(item["id"], "importing", "task_pending", task_uuid=task)
        self._reconcile_task(self.ledger.get_item(item["id"]))

    def _reconcile_task(self, item: dict) -> None:
        if item.get("lease_owner"): self.ledger.renew(self.owner, item["id"])
        result = self.native.task_result(item["task_uuid"])
        if result is None:
            self.ledger.transition(item["id"], "importing", "task_pending", task_uuid=item["task_uuid"], next_attempt_at=time.time() + 1.0); return
        if result.get("duplicate"):
            document_id = result.get("document_id") or self.native.find_existing(item["source_id"], item["canonical_url"], item["content_hash"])
            self.ledger.transition(item["id"], "duplicate", "native_duplicate", document_id=document_id); return
        document_id = result.get("document_id")
        if not isinstance(document_id, int):
            raise LibraryError("native task lacks document ID", "native_failed", 502)
        ocr_text = self._ocr_text(item)
        if ocr_text and hasattr(self.native, "attach_ocr"):
            self.native.attach_ocr(document_id, "", ocr_text, item.get("metadata", {}).get("ocr", []))
        if not self.native.verify(document_id, item):
            raise LibraryError("native upload readback could not be verified", "native_failed", 502)
        self.ledger.transition(item["id"], "confirmed", "confirmed", document_id=document_id)
        self.ledger.clean_spool(self.ledger.get_item(item["id"]))
        path = item.get("metadata", {}).get("ocr_spool_path")
        if path:
            candidate = Path(path)
            if candidate.parent == self.ledger.spool_dir and candidate.is_file() and not candidate.is_symlink(): candidate.unlink()

    def _ocr_text(self, item: dict) -> str:
        path = item.get("metadata", {}).get("ocr_spool_path")
        if not path: return ""
        candidate = Path(path)
        if candidate.parent != self.ledger.spool_dir or not candidate.is_file() or candidate.is_symlink(): return ""
        try:
            rows = json.loads(candidate.read_text(encoding="utf-8"))
            return "\n".join(row["text"].strip() for row in rows if isinstance(row, dict) and isinstance(row.get("text"), str) and row["text"].strip())
        except (OSError, ValueError): return ""

    def _reconcile_without_task(self, item: dict) -> None:
        existing = self.native.find_existing(item.get("source_id"), item.get("canonical_url"), item.get("content_hash"), item.get("private_locator"))
        if existing:
            self.ledger.transition(item["id"], "duplicate", "uncertain_reconciled", document_id=existing); return
        self.ledger.transition(item["id"], "blocked", "reconcile", reason="upload intent lacks task UUID; manual resolution required")

    def _extract(self, item: dict) -> dict:
        if item["kind"] == "text":
            body = self.ledger.read_spool(item).decode("utf-8", "replace")
            if not normalized_text(body): raise LibraryError("submitted text is empty")
            original = item["metadata"].get("source_url")
            platform, source_id, citation = "text", stable_id("text", "", text=body), None
            if isinstance(original, str):
                try:
                    platform, citation = platform_for(original), canonical_url(original)
                    source_id = stable_id(platform, citation)
                except LibraryError:
                    # An old unreadable URL never becomes an ordinal identity.
                    platform, citation = "text", None
            return {"source_id": source_id, "body": body, "canonical_url": citation, "original_url": original,
                    "legacy_analysis": item["metadata"].get("legacy_analysis"),
                    "title": item["metadata"].get("title") or "Submitted text", "platform": platform}
        if item["kind"] == "file":
            path = checked_file(item["private_locator"], self.accepted_roots); raw, text, status = extract_file(path)
            ocr_text = self._ocr_text(item)
            if status == "needs_ocr" and not ocr_text: return {"terminal": "needs_ocr", "reason": "file needs approved OCR"}
            return {"source_id": stable_id("file", "", file_bytes=raw), "body": text or "", "canonical_url": None,
                    "title": path.name, "platform": "file", "file_bytes": raw, "filename": path.name,
                    "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream", "ocr_text": ocr_text,
                    "ocr_provenance": item["metadata"].get("ocr", [])}
        url = item["private_locator"]
        if item.get("spool_path"):
            body = self.ledger.read_spool(item).decode("utf-8", "replace")
            platform = platform_for(url)
            if platform == "xhs": return {"terminal": "needs_login", "reason": "authorized visible Edge adapter is unavailable"}
            return {"source_id": stable_id(platform, url), "body": body, "canonical_url": canonical_url(url), "original_url": url,
                    "title": item["metadata"].get("title") or "Submitted supplement", "platform": platform}
        if item["metadata"].get("supplement_file_path"):
            if item.get("metadata", {}).get("ocr"): raise LibraryError("OCR with URL file supplement is not supported", "ocr_unsupported", 409)
            path = checked_file(item["metadata"]["supplement_file_path"], self.accepted_roots); raw, text, status = extract_file(path)
            if status == "needs_ocr": return {"terminal": "needs_ocr", "reason": "file needs approved OCR"}
            platform = platform_for(url)
            return {"source_id": stable_id(platform, url), "body": text or "", "canonical_url": canonical_url(url), "original_url": url,
                    "title": path.name, "platform": platform, "file_bytes": raw, "filename": path.name,
                    "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream"}
        platform = platform_for(url)
        if platform == "xhs": return {"terminal": "needs_login", "reason": "authorized visible Edge adapter is unavailable"}
        if not self.fetcher: raise LibraryError("source fetcher is not configured", "source_failed", 503)
        wait = self.rate_limit_seconds - (time.monotonic() - self._last_fetch)
        if wait > 0: self.sleep(wait)
        final, _, raw = self.fetcher.get(url); self._last_fetch = time.monotonic()
        parsed = parse_wechat_html(raw.decode("utf-8", "replace"))
        if parsed["status"] != "complete": return {"terminal": parsed["status"], "reason": parsed["reason"]}
        return {"source_id": stable_id("wechat", final), "body": parsed["body"], "canonical_url": canonical_url(final), "original_url": url,
                "title": parsed["title"], "author": parsed["author"], "published_at": parsed["published_at"], "platform": "wechat"}
