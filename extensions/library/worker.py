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
from .xhs_edge import EdgeNoteAdapter

MAX_OCR_IMAGES = 4


class LibraryWorker:
    def __init__(self, ledger: Ledger, native: Any, *, fetcher: SafeFetcher | None = None,
                 accepted_roots: list[str] | None = None, rate_limit_seconds: float = 1.5, sleep=time.sleep,
                 edge: EdgeNoteAdapter | None = None, ocr=None):
        self.ledger, self.native, self.fetcher = ledger, native, fetcher
        self.edge, self.ocr = edge or EdgeNoteAdapter(), ocr
        self.accepted_roots, self.sleep, self.owner = accepted_roots or [], sleep, uuid.uuid4().hex
        self.rate_limit_seconds, self._last_fetch = max(1.5, float(rate_limit_seconds)), 0.0

    def run_once(self) -> bool:
        if not self.ledger.acquire_worker(self.owner): return False
        item = self.ledger.claim(self.owner)
        if not item: return False
        try: self.process(item)
        except LibraryError as exc:
            state = "needs_login" if exc.code == "needs_login" else "needs_ocr" if exc.code in {"ocr_failed", "image_blocked"} else "blocked" if exc.code in {"identity_pending", "spool_missing", "source_timeout", "upload_uncertain"} else "failed"
            self.ledger.transition(item["id"], state,
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
        metadata = {**item["metadata"], "upload_intent": True,
                    "source_provenance": data.get("provenance", {}), "pending_images": data.get("pending_images", 0),
                    "source_present": bool(normalized_text(body))}
        if data.get("ocr_rows"):
            rows = data["ocr_rows"]
            metadata["ocr_spool_path"] = self.ledger.spool(json.dumps(rows, ensure_ascii=False))
            metadata["ocr"] = [{"image_url": canonical_url(row["image_url"]), "method": row["method"]} for row in rows]
            data["ocr_provenance"] = metadata["ocr"]
        ocr_text = self._ocr_text({"metadata": metadata})
        binary = data.get("file_bytes") is not None and not data.get("mime", "").startswith("text/")
        metadata["binary_original"] = binary
        if binary:
            metadata["desired_completeness"] = data.get("completeness", "complete")
            data["completeness"] = "partial"
            data["extraction_status"] = "needs_ocr" if not normalized_text(body) else "partial"
            data["ocr_indexed"] = False
            metadata["completion_after_upload"] = "needs_ocr" if not normalized_text(body) else "partial"
            metadata["ocr_expected_indexed"] = bool(ocr_text)
        elif ocr_text:
            data["ocr_indexed"] = False  # native reader must prove indexing after upload
            data["upload_text_with_ocr"] = data.get("file_bytes") is not None
            metadata["ocr_expected_indexed"] = True
            metadata["desired_completeness"] = data.get("completeness", "complete")
            data["completeness"] = "partial"
            data["extraction_status"] = "ocr_pending_readback"
        metadata["completeness"] = data.get("completeness", "complete")
        canonical, content_hash = data.get("canonical_url"), (data.get("source_hash") or
            sha256_bytes(data["file_bytes"] if data.get("file_bytes") is not None else body.encode()))
        self.ledger.transition(item["id"], "importing", "upload", source_id=source_id,
                               canonical_url=canonical, content_hash=content_hash,
                               metadata=metadata)
        current = self.ledger.get_item(item["id"])
        existing = self.native.find_existing(source_id, canonical, content_hash, data.get("original_url"))
        if existing:
            if hasattr(self.native, "source_changed") and self.native.source_changed(existing, content_hash):
                if not hasattr(self.native, "find_version"):
                    self.ledger.transition(item["id"], "blocked", "source_changed", document_id=existing,
                                           reason="confirmed source changed; version readback unavailable")
                    return
                version_id = source_id + ":revision:" + content_hash[:16]
                prior_version = self.native.find_version(version_id, content_hash)
                if prior_version:
                    self.ledger.transition(item["id"], "duplicate", "version_reconciled", document_id=prior_version)
                    return
                data["provenance"] = {**data.get("provenance", {}), "stable_source_id": source_id, "revision_of": existing,
                                      "revision_hash": content_hash}
                source_id = version_id
                metadata["revision_of"] = existing
                metadata["source_provenance"] = data["provenance"]
                self.ledger.transition(item["id"], "importing", "version_upload", source_id=version_id, metadata=metadata)
            else:
                self.ledger.transition(item["id"], "duplicate", "reconciled", document_id=existing)
                self.ledger.clean_spool(self.ledger.get_item(item["id"])); return
        task = self.native.upload(data, archive_text(body, ocr_text if not binary else None), source_id, canonical, content_hash)
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
            document_id = result.get("document_id") or (self.native.find_version(item["source_id"], item["content_hash"])
                if item.get("metadata", {}).get("revision_of") else self.native.find_existing(item["source_id"], item["canonical_url"], item["content_hash"]))
            self.ledger.transition(item["id"], "duplicate", "native_duplicate", document_id=document_id); return
        document_id = result.get("document_id")
        if not isinstance(document_id, int):
            raise LibraryError("native task lacks document ID", "native_failed", 502)
        try:
            verified = self.native.verify(document_id, item)
        except LibraryError as exc:
            if not item.get("metadata", {}).get("ocr_expected_indexed"): raise
            pending = "partial" if item["metadata"].get("source_present") else "needs_ocr"
            self.ledger.transition(item["id"], pending, "ocr_unindexed", document_id=document_id, reason=exc)
            return
        if not verified:
            if item.get("metadata", {}).get("ocr_expected_indexed"):
                pending = "partial" if item["metadata"].get("source_present") else "needs_ocr"
                self.ledger.transition(item["id"], pending, "ocr_unindexed", document_id=document_id,
                                       reason="native upload readback could not be verified")
                return
            raise LibraryError("native upload readback could not be verified", "native_failed", 502)
        completeness = item.get("metadata", {}).get("completeness", "complete")
        status = item.get("metadata", {}).get("completion_after_upload") or ("partial" if completeness == "partial" else "confirmed")
        if item.get("metadata", {}).get("ocr_expected_indexed"):
            ocr_text = self._ocr_text(item)
            try:
                if not ocr_text: raise LibraryError("OCR staging is unavailable", "ocr_unindexed", 409)
                if item["metadata"].get("binary_original"):
                    if not hasattr(self.native, "attach_ocr"):
                        raise LibraryError("native OCR attachment is unavailable", "ocr_unindexed", 409)
                    self.native.attach_ocr(document_id, "", ocr_text, item["metadata"].get("ocr", []))
                if hasattr(self.native, "verify_indexed_ocr"):
                    if not self.native.verify_indexed_ocr(document_id, ocr_text) or not hasattr(self.native, "mark_ocr_indexed"):
                        raise LibraryError("native OCR content readback mismatch", "ocr_unindexed", 502)
                    self.native.mark_ocr_indexed(document_id, item["metadata"].get("desired_completeness", "complete"))
                    if not self.native.verify_indexed_ocr(document_id, ocr_text):
                        raise LibraryError("native OCR content changed after field patch", "ocr_unindexed", 502)
                desired = item["metadata"].get("desired_completeness", "complete")
                status = "confirmed" if desired == "complete" else "partial"
            except LibraryError as exc:
                status = "partial" if item.get("metadata", {}).get("source_present") else "needs_ocr"
                self.ledger.transition(item["id"], status, "ocr_unindexed", document_id=document_id, reason=exc)
                return
        self.ledger.transition(item["id"], status, "confirmed" if status == "confirmed" else "ocr_unindexed",
                               document_id=document_id)
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
        existing = (self.native.find_version(item["source_id"], item["content_hash"])
                    if item.get("metadata", {}).get("revision_of") and hasattr(self.native, "find_version") else
                    self.native.find_existing(item.get("source_id"), item.get("canonical_url"), item.get("content_hash"), item.get("private_locator")))
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
                    "title": item["metadata"].get("title") or "Submitted text", "platform": platform,
                    "source_label": item["metadata"].get("source_label")}
        if item["kind"] == "file":
            path = checked_file(item["private_locator"], self.accepted_roots); raw, text, status = extract_file(path)
            ocr_text = self._ocr_text(item)
            if status == "needs_ocr" and not ocr_text: return {"terminal": "needs_ocr", "reason": "file needs approved OCR"}
            return {"source_id": stable_id("file", "", file_bytes=raw), "body": text or "", "canonical_url": None,
                    "title": path.name, "platform": "file", "file_bytes": raw, "filename": path.name,
                    "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream", "ocr_text": ocr_text,
                    "ocr_provenance": item["metadata"].get("ocr", []), "source_label": item["metadata"].get("source_label")}
        url = item["private_locator"]
        platform = platform_for(url)
        verified = item["metadata"].get("verified_canonical_url")
        if verified:
            if platform != "wechat": raise LibraryError("canonical supplement platform mismatch", "identity_pending", 422)
            stable_id("wechat", verified)
        if item.get("spool_path"):
            body = self.ledger.read_spool(item).decode("utf-8", "replace")
            citation = verified or url
            return {"source_id": stable_id(platform, citation), "body": body, "canonical_url": canonical_url(citation), "original_url": url,
                    "title": item["metadata"].get("title") or "Submitted supplement", "platform": platform,
                    "author": item["metadata"].get("author"), "published_at": item["metadata"].get("published_at"),
                    "fetched_at": item["metadata"].get("fetched_at"), "source_label": item["metadata"].get("source_label"),
                    "provenance": {"source": "operator-supplement", "metadata": item["metadata"].get("metadata_provenance")}}
        if item["metadata"].get("supplement_file_path"):
            path = checked_file(item["metadata"]["supplement_file_path"], self.accepted_roots); raw, text, status = extract_file(path)
            if status == "needs_ocr" and not self._ocr_text(item): return {"terminal": "needs_ocr", "reason": "file needs approved OCR"}
            citation = verified or url
            return {"source_id": stable_id(platform, citation), "body": text or "", "canonical_url": canonical_url(citation), "original_url": url,
                    "title": item["metadata"].get("title") or path.name, "platform": platform, "file_bytes": raw, "filename": path.name,
                    "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    "author": item["metadata"].get("author"), "published_at": item["metadata"].get("published_at"),
                    "fetched_at": item["metadata"].get("fetched_at"), "source_label": item["metadata"].get("source_label"),
                    "provenance": {"source": "operator-file-supplement", "metadata": item["metadata"].get("metadata_provenance")}}
        if platform == "xhs":
            if url.startswith("https://xhslink.com/") or url.startswith("http://xhslink.com/"):
                if not self.fetcher: raise LibraryError("source fetcher is not configured", "source_failed", 503)
                final, _, _ = self.fetcher.get(url)
                stable_id("xhs", final)
            else: final = url
            data = self.edge.capture(final)
            if data.get("terminal"): return data
            data["original_url"] = url
            data["source_label"] = item["metadata"].get("source_label")
            rows, pending = [], data.pop("unclassified_images", 0)
            images = data.pop("images")
            for image_url in images[:MAX_OCR_IMAGES]:
                if self.ocr is None: pending += 1; continue
                try: result = self.ocr.image(image_url)
                except LibraryError: pending += 1; continue
                if result["status"] == "complete": rows.append(result)
                else: pending += 1
            pending += max(0, len(images) - MAX_OCR_IMAGES)
            if not normalized_text(data["body"]) and not rows:
                return {"terminal": "needs_ocr", "reason": "visible note images need local OCR"}
            data["ocr_rows"], data["pending_images"] = rows, pending
            data["completeness"] = "partial" if pending else "complete"
            data["source_hash"] = sha256_bytes(json.dumps({"text": data["body"],
                "images": [canonical_url(value) for value in images],
                "ocr": [row["text"] for row in rows]}, ensure_ascii=False, sort_keys=True).encode())
            return data
        if not self.fetcher: raise LibraryError("source fetcher is not configured", "source_failed", 503)
        wait = self.rate_limit_seconds - (time.monotonic() - self._last_fetch)
        if wait > 0: self.sleep(wait)
        final, _, raw = self.fetcher.get(url); self._last_fetch = time.monotonic()
        parsed = parse_wechat_html(raw.decode("utf-8", "replace"))
        if parsed["status"] != "complete": return {"terminal": parsed["status"], "reason": parsed["reason"]}
        citation = parsed.get("canonical_url") or final
        try: final_id = stable_id("wechat", final)
        except LibraryError: final_id = None
        if final_id and stable_id("wechat", citation) != final_id:
            raise LibraryError("public article identity conflicts with redirect", "identity_pending", 422)
        return {"source_id": stable_id("wechat", citation), "body": parsed["body"], "canonical_url": canonical_url(citation), "original_url": url,
                "title": parsed["title"], "author": parsed["author"], "published_at": parsed["published_at"], "fetched_at": parsed["fetched_at"],
                "source_label": item["metadata"].get("source_label"), "provenance": {"source": "public-html", "metadata": "public-html"}, "platform": "wechat"}
