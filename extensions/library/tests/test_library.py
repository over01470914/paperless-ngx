from __future__ import annotations

import asyncio
import io
import json
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mcp.server.fastmcp.exceptions import ToolError

from extensions.library.ledger import Ledger
from extensions.library.mcp_server import LocalAPI, create_server
from extensions.library.model import LIB_FIELDS, LibraryError, MAX_INGRESS_TEXT_CHARS, MAX_JSON_BYTES
from extensions.library.model import normalized_text, sha256_bytes
from extensions.library.native import NativeClient, NativeIngestor
from extensions.library.search import LibrarySearch, variants
from extensions.library.enrich import enrich, validate_provider_response
from extensions.library.service import LibraryService, handler_factory
from extensions.library.sources import SafeFetcher, URLGuard, extract_urls, stable_id
from extensions.library.sources import parse_wechat_html
from extensions.library.worker import LibraryWorker


class FakeNative:
    def __init__(self): self.documents = {}
    def find_existing(self, source_id, canonical, content_hash, original_url=None): return None
    def upload(self, *args): return None
    def task_result(self, task): return None
    def verify(self, doc, item): return True

class ConfirmingNative(FakeNative):
    def upload(self, *args): return "task-scalar-uuid"
    def task_result(self, task): return {"document_id": 73}

class PendingNative(ConfirmingNative):
    def __init__(self): super().__init__(); self.uploads = 0; self.polls = 0
    def upload(self, *args): self.uploads += 1; return "task-scalar-uuid"
    def task_result(self, task):
        self.polls += 1
        return None if self.polls <= 6 else {"document_id": 73}

class OcrNative(ConfirmingNative):
    def __init__(self): super().__init__(); self.ocr = None
    def attach_ocr(self, document_id, source, ocr, provenance): self.ocr = (document_id, source, ocr, provenance)

class ReanalysisNative(FakeNative):
    def __init__(self, result="updated"): super().__init__(); self.result, self.calls = result, 0
    def reanalyze(self, document_id, version): self.calls += 1; return self.result

class FakeNativeClient:
    def __init__(self, ids, document): self.ids, self.document_value, self.calls = ids, document, []
    def pages(self, path):
        self.calls.append(("pages", path))
        if path.startswith("/api/tasks/"): return [{"task_id": "task-scalar-uuid", "status": "success", "result_data": {"document_id": 73}}]
        if path.startswith("/api/documents/"): return [self.document_value]
        return []
    def request(self, method, path, payload=None, content_type=None):
        self.calls.append((method, path, payload))
        if path == "/api/documents/post_document/": return "task-scalar-uuid"
        return {}
    def document(self, ident): self.calls.append(("document", ident)); return self.document_value

class MutableNativeClient:
    def __init__(self, document): self.document_value, self.calls = document, []
    def document(self, ident): return self.document_value
    def request(self, method, path, payload=None, content_type=None):
        self.calls.append((method, path, payload))
        if method == "PATCH":
            if "content" in payload: self.document_value["content"] = payload["content"]
            if "custom_fields" in payload: self.document_value["custom_fields"] = payload["custom_fields"]
        return {}


class FakeReader:
    def __init__(self, document): self.document_value = document
    def search(self, term): return [self.document_value]
    def document(self, ident): return self.document_value

class SearchReader:
    def __init__(self, document): self.document_value, self.terms = document, []
    def search(self, term): self.terms.append(term); return [{"id": self.document_value["id"]}] if term == "witr" else []
    def document(self, ident): return self.document_value


class FakeWriter:
    def update_state(self, document_id, ids, state): return {"id": document_id, **state}

class RecordingAPI:
    def __init__(self): self.calls = []
    def call(self, method, path, payload=None): self.calls.append((method, path, payload)); return {"ok": True}


def fields(): return {name: index for index, name in enumerate(LIB_FIELDS, 1)}


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        self.ledger = Ledger(root / "state" / "jobs.sqlite", root / "state" / "spool")
    def tearDown(self): self.temp.cleanup()

    def size_service(self):
        ids = fields()
        return LibraryService(self.ledger, LibraryWorker(self.ledger, FakeNative()),
                              LibrarySearch(FakeReader({"id": 1, "content": "", "custom_fields": []}), ids),
                              FakeWriter(), ids)

    def size_dispatch(self, service, path, payload=None, body=None, content_length=None):
        raw = body if body is not None else json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
        handler = object.__new__(handler_factory(service, "service-test-token"))
        handler.command, handler.path, handler.rfile = "POST", path, io.BytesIO(raw)
        handler.headers = {"Content-Length": str(len(raw) if content_length is None else content_length),
                           "Authorization": "Bearer service-test-token"}
        return handler._dispatch()

    def assert_size_rejected_without_effects(self, invoke, item_id=None, message=None):
        counts = tuple(self.ledger.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                       for table in ("batches", "items"))
        spools = {path.name: path.read_bytes() for path in self.ledger.spool_dir.iterdir()}
        item = (dict(self.ledger.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())
                if item_id else None)
        with (patch.object(self.ledger, "get_item", side_effect=AssertionError("ledger read before size validation")),
              patch.object(self.ledger, "spool", side_effect=AssertionError("spool before size validation")),
              patch.object(self.ledger, "create_batch", side_effect=AssertionError("batch before size validation")),
              patch.object(self.ledger, "resume", side_effect=AssertionError("resume before size validation")),
              patch.object(self.ledger, "transition", side_effect=AssertionError("transition before size validation"))):
            with self.assertRaises(LibraryError) as raised: invoke()
        if message is not None:
            self.assertEqual(str(raised.exception), message)
        self.assertEqual(counts, tuple(self.ledger.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                                       for table in ("batches", "items")))
        self.assertEqual(spools, {path.name: path.read_bytes() for path in self.ledger.spool_dir.iterdir()})
        if item_id:
            self.assertEqual(item, dict(self.ledger.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()))

    def test_atomic_idempotent_and_exact_10_50_counts(self):
        self.assertEqual(LIB_FIELDS["lib_starred"], "boolean"); self.assertEqual(LIB_FIELDS["lib_pending"], "boolean")
        ten = [{"kind": "text", "spool_path": self.ledger.spool(str(i))} for i in range(10)]
        first = self.ledger.create_batch(ten, "same-key")
        again = self.ledger.create_batch(ten, "same-key")
        self.assertEqual(first["batch_id"], again["batch_id"]); self.assertEqual(again["counts"]["queued"], 10)
        fifty = self.ledger.create_batch([{"kind": "text", "spool_path": self.ledger.spool(str(i))} for i in range(50)])
        self.assertEqual(fifty["total"], 50); self.assertEqual(fifty["counts"]["queued"], 50)
        self.assertEqual((Path(self.ledger.path).stat().st_mode & 0o777), 0o600)
        self.assertEqual((self.ledger.spool_dir.stat().st_mode & 0o777), 0o700)

    def test_spool_collision_keeps_existing_referenced_file(self):
        existing = self.ledger.spool(b"synthetic existing body")
        batch = self.ledger.create_batch([{"kind": "text", "spool_path": existing}])
        class CollidingUUID: hex = Path(existing).stem
        with patch("extensions.library.ledger.uuid.uuid4", return_value=CollidingUUID()):
            with self.assertRaises(FileExistsError): self.ledger.spool(b"synthetic new body")
        item = self.ledger.get_item(batch["items"][0]["id"])
        self.assertEqual(item["spool_path"], existing)
        self.assertEqual(Path(existing).read_bytes(), b"synthetic existing body")

    def test_submit_duplicate_key_keeps_only_committed_text_spool(self):
        ids = fields(); service = LibraryService(self.ledger, LibraryWorker(self.ledger, FakeNative()), LibrarySearch(FakeReader({"id": 1, "content": "", "custom_fields": []}), ids), FakeWriter(), ids)
        payload = {"text": "synthetic private body", "idempotency_key": "retry-key"}
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: service.submit(payload), range(4)))
        self.assertEqual({row["batch_id"] for row in responses}, {responses[0]["batch_id"]})
        self.assertEqual(self.ledger.batch(responses[0]["batch_id"])["total"], 1)
        item = self.ledger.get_item(responses[0]["items"][0]["id"])
        self.assertEqual(set(self.ledger.spool_dir.iterdir()), {Path(item["spool_path"])})

    def test_submit_batch_failure_discards_staged_text_spool(self):
        ids = fields(); service = LibraryService(self.ledger, LibraryWorker(self.ledger, FakeNative()), LibrarySearch(FakeReader({"id": 1, "content": "", "custom_fields": []}), ids), FakeWriter(), ids)
        original = self.ledger.create_batch
        self.ledger.create_batch = lambda *_args, **_kwargs: (_ for _ in ()).throw(LibraryError("synthetic batch failure"))
        try:
            with self.assertRaises(LibraryError): service.submit({"text": "synthetic private body"})
        finally:
            self.ledger.create_batch = original
        self.assertEqual(list(self.ledger.spool_dir.iterdir()), [])
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)

    def test_submit_validation_failure_discards_staged_text_spool(self):
        ids = fields(); service = LibraryService(self.ledger, LibraryWorker(self.ledger, FakeNative()), LibrarySearch(FakeReader({"id": 1, "content": "", "custom_fields": []}), ids), FakeWriter(), ids)
        with self.assertRaises(LibraryError):
            service.submit({"text": "synthetic private body", "urls": ["https://user:pass@mp.weixin.qq.com/s?__biz=b&mid=1&idx=2"]})
        self.assertEqual(list(self.ledger.spool_dir.iterdir()), [])
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)

    def test_concurrent_ledger_submissions_and_transitions(self):
        def submit(index):
            return self.ledger.create_batch([{"kind": "text", "spool_path": self.ledger.spool(f"synthetic {index}-{part}")} for part in range(5)])
        with ThreadPoolExecutor(max_workers=10) as pool: batches = list(pool.map(submit, range(10)))
        self.assertEqual(sum(batch["total"] for batch in batches), 50)
        native = ConfirmingNative(); worker = LibraryWorker(self.ledger, native)
        while worker.run_once(): pass
        self.assertEqual(sum(self.ledger.batch(batch["batch_id"])["counts"]["confirmed"] for batch in batches), 50)

    def test_two_urls_one_line_stable_ids_and_ssrf_redirect(self):
        urls = extract_urls("one https://mp.weixin.qq.com/s?__biz=b&mid=1&idx=2&sn=s two https://www.xiaohongshu.com/explore/note42")
        self.assertEqual(len(urls), 2); self.assertEqual(stable_id("wechat", urls[0]), "wechat:b:1:2:s")
        self.assertEqual(stable_id("xhs", urls[1]), "xhs:note42")
        calls = []
        def resolver(host, port): return ["8.8.8.8"] if host == "mp.weixin.qq.com" else ["127.0.0.1"]
        def transport(url, host, port, ips):
            calls.append((host, ips)); return 302, {"location": "https://weixin.qq.com/private"}, b""
        with self.assertRaises(LibraryError) as raised: SafeFetcher(URLGuard(resolver), transport).get(urls[0])
        self.assertEqual(raised.exception.code, "ssrf_blocked"); self.assertEqual(calls[0][1], ["8.8.8.8"])

    def test_wechat_parser_body_precedence_siblings_and_honest_gates(self):
        article = '<div id="js_content"><section><p>first verify 登錄 驗證</p></section><section><p>second sibling</p></section></div>'
        parsed = parse_wechat_html(article)
        self.assertEqual(parsed["status"], "complete"); self.assertIn("first verify", parsed["body"]); self.assertIn("second sibling", parsed["body"])
        self.assertEqual(parse_wechat_html('<div>请在微信客户端打开</div>')["status"], "needs_login")
        self.assertEqual(parse_wechat_html('<div>文章已删除</div>')["status"], "failed")
        self.assertEqual(parse_wechat_html('<div>ordinary landing page</div>')["status"], "partial")

    def test_wechat_void_hidden_and_root_boundary(self):
        html = '<div id="js_content"><p>First<br>Second<img src="x"></p><span style="display:none">hidden</span><section>Third</section><script>请先登录</script></div><p>Footer excluded</p>'
        body = parse_wechat_html(html)["body"]
        self.assertIn("First", body); self.assertIn("Second", body); self.assertIn("Third", body)
        self.assertNotIn("Footer", body); self.assertNotIn("hidden", body); self.assertNotIn("登录", body)

    def test_wechat_ignores_hidden_outside_gate_markers(self):
        self.assertEqual(parse_wechat_html('<script>请先登录</script><div>ordinary landing page</div>')["status"], "partial")
        self.assertEqual(parse_wechat_html('<div hidden>文章已删除</div><div>ordinary landing page</div>')["status"], "partial")
        self.assertEqual(parse_wechat_html('<div>请先登录</div>')["status"], "needs_login")

    def test_credentialed_url_is_rejected_and_never_public(self):
        private = "https://user:pass@mp.weixin.qq.com/s?__biz=b&mid=1&idx=2"
        ids = fields(); service = LibraryService(self.ledger, LibraryWorker(self.ledger, FakeNative()), LibrarySearch(FakeReader({"id": 1, "content": "", "custom_fields": []}), ids), FakeWriter(), ids)
        with self.assertRaises(LibraryError): service.submit({"urls": [private]})
        batch = self.ledger.create_batch([{"kind": "url", "locator": private}])
        self.assertNotIn("user:pass", json.dumps(batch)); self.assertEqual(batch["items"][0]["public_locator"], "private-input")

    def test_uncertain_upload_blocks_without_second_upload(self):
        batch = self.ledger.create_batch([{"kind": "text", "spool_path": self.ledger.spool("synthetic body")}])
        item_id = batch["items"][0]["id"]
        self.ledger.transition(item_id, "importing", "upload", source_id="text:a", content_hash="a", metadata={"upload_intent": True})
        worker = LibraryWorker(self.ledger, FakeNative())
        worker.process(self.ledger.get_item(item_id))
        self.assertEqual(self.ledger.get_item(item_id)["status"], "blocked")

    def test_worker_progress_and_restart_lease_recovery(self):
        batch = self.ledger.create_batch([{"kind": "text", "spool_path": self.ledger.spool("synthetic body")}])
        first = LibraryWorker(self.ledger, ConfirmingNative())
        self.assertTrue(first.run_once()); self.assertEqual(self.ledger.batch(batch["batch_id"])["counts"]["confirmed"], 1)
        self.ledger.db.execute("UPDATE worker_lease SET until=0 WHERE name='worker'")
        self.assertTrue(self.ledger.acquire_worker("dead-worker", seconds=100))
        second = LibraryWorker(self.ledger, ConfirmingNative())
        self.assertFalse(second.run_once())
        self.ledger.db.execute("UPDATE worker_lease SET until=0 WHERE name='worker'")
        self.assertTrue(second.ledger.acquire_worker(second.owner))

    def test_two_ledger_restart_reclaims_expired_item_without_duplicate_upload(self):
        batch = self.ledger.create_batch([{"kind": "text", "spool_path": self.ledger.spool("synthetic body")}])
        first = LibraryWorker(self.ledger, ConfirmingNative()); claimed = self.ledger.claim(first.owner)
        self.assertEqual(claimed["status"], "fetching")
        self.ledger.db.execute("UPDATE items SET lease_until=0 WHERE id=?", (claimed["id"],))
        other = Ledger(self.ledger.path, self.ledger.spool_dir)
        native, restarted = ConfirmingNative(), LibraryWorker(other, ConfirmingNative())
        restarted.native = native
        self.assertTrue(restarted.run_once()); self.assertEqual(other.batch(batch["batch_id"])["counts"]["confirmed"], 1)

    def test_pending_native_task_polls_without_source_retry_or_reupload(self):
        batch = self.ledger.create_batch([{"kind": "text", "spool_path": self.ledger.spool("synthetic body")}])
        native, worker = PendingNative(), LibraryWorker(self.ledger, PendingNative())
        worker.native = native
        for _ in range(8):
            worker.run_once()
            self.ledger.db.execute("UPDATE items SET next_attempt_at=0,lease_until=0 WHERE batch_id=?", (batch["batch_id"],))
        item = self.ledger.batch(batch["batch_id"])["items"][0]
        self.assertEqual(item["status"], "confirmed"); self.assertEqual(native.uploads, 1); self.assertGreaterEqual(native.polls, 7)

    def test_scalar_task_writer_poll_permission_grant_reader_readback_and_legacy_duplicate(self):
        ids = fields(); source_id, digest = "wechat:b:1:2:s", "hash"
        private_url = "https://example.invalid/original?x" + "sec_token=private"
        document = {"id": 73, "content": "── SOURCE ──\nsynthetic\n── END SOURCE ──", "custom_fields": [
            {"field": ids["lib_source_id"], "value": source_id}, {"field": ids["lib_content_hash"], "value": digest},
            {"field": ids["lib_canonical_url"], "value": "https://example.invalid/article"}, {"field": 90, "value": private_url}]}
        reader, writer = FakeNativeClient(ids, document), FakeNativeClient(ids, document)
        ingestor = NativeIngestor(reader, writer, ids, permission_ids={"reader_user_id": 1, "writer_user_id": 2, "boss_user_id": 3}, legacy_field_ids={"wx_source_url": 90})
        task = ingestor.upload({"platform": "wechat", "title": "Synthetic", "body": "synthetic", "original_url": private_url}, "── SOURCE ──\nsynthetic\n── END SOURCE ──", source_id, "https://example.invalid/article", digest)
        self.assertEqual(task, "task-scalar-uuid"); self.assertEqual(ingestor.task_result(task)["document_id"], 73)
        self.assertTrue(ingestor.verify(73, {"source_id": source_id, "content_hash": digest, "canonical_url": "https://example.invalid/article"}))
        patch = [call for call in writer.calls if call[0] == "PATCH"][0][2]
        self.assertEqual(patch["set_permissions"]["view"]["users"], [1, 3]); self.assertTrue(any(call[0] == "document" for call in reader.calls))
        self.assertEqual(ingestor.find_existing(None, None, None, private_url), 73)

    def test_file_multipart_preserves_original_bytes(self):
        ids = fields(); raw = b"%PDF-synthetic\x00\xff"
        document = {"id": 73, "content": "", "custom_fields": []}
        reader, writer = FakeNativeClient(ids, document), FakeNativeClient(ids, document)
        ingestor = NativeIngestor(reader, writer, ids, permission_ids={"reader_user_id": 1, "writer_user_id": 2, "boss_user_id": 3})
        ingestor.upload({"platform": "file", "title": "fixture.pdf", "body": "", "file_bytes": raw, "filename": "fixture.pdf", "mime": "application/pdf"}, "ignored wrapper", "file:hash", None, "hash")
        body = [call for call in writer.calls if call[1] == "/api/documents/post_document/"][0][2]
        self.assertIn(raw, body); self.assertNotIn(b"ignored wrapper", body)

    def test_file_ocr_supplement_confirms_without_ledger_body(self):
        root = Path(self.temp.name) / "approved"; root.mkdir(); path = root / "scan.pdf"; path.write_bytes(b"%PDF synthetic scan")
        ids = fields(); native = OcrNative(); service = LibraryService(self.ledger, LibraryWorker(self.ledger, native, accepted_roots=[root]), LibrarySearch(FakeReader({"id": 1, "content": "", "custom_fields": []}), ids), FakeWriter(), ids, [root])
        batch = self.ledger.create_batch([{"kind": "file", "locator": str(path)}]); item_id = batch["items"][0]["id"]
        public = service.resume(item_id, {"ocr": [{"image_url": "synthetic-image", "method": "manual", "text": "synthetic OCR"}]})
        self.assertNotIn("synthetic OCR", json.dumps(public)); self.assertTrue(service.worker.run_once())
        self.assertEqual(self.ledger.get_item(item_id)["status"], "confirmed"); self.assertEqual(native.ocr[2], "synthetic OCR"); self.assertEqual(native.ocr[3], [{"image_url": "synthetic-image", "method": "manual"}])
        absent = self.ledger.create_batch([{"kind": "file", "locator": str(path)}]); self.assertTrue(service.worker.run_once()); self.assertEqual(self.ledger.batch(absent["batch_id"])["counts"]["needs_ocr"], 1)

    def test_attach_ocr_preserves_source_and_provenance_without_duplicate_section(self):
        ids = fields(); document = {"id": 73, "content": "existing extracted source", "custom_fields": [{"field": ids["lib_provenance"], "value": '{"adapter":"file"}'}]}
        reader = MutableNativeClient(document); writer = MutableNativeClient(document)
        ingestor = NativeIngestor(reader, writer, ids)
        provenance = [{"image_url": "synthetic-image", "method": "manual"}]
        ingestor.attach_ocr(73, "", "synthetic OCR", provenance); ingestor.attach_ocr(73, "", "synthetic OCR", provenance)
        self.assertIn("existing extracted source", document["content"]); self.assertEqual(document["content"].count("── OCR ──"), 1)
        value = [row["value"] for row in document["custom_fields"] if row["field"] == ids["lib_provenance"]][0]
        self.assertEqual(json.loads(value)["ocr"], provenance)

    def test_resume_rejection_does_not_create_ocr_spool(self):
        ids = fields(); service = LibraryService(self.ledger, LibraryWorker(self.ledger, FakeNative()), LibrarySearch(FakeReader({"id": 1, "content": "", "custom_fields": []}), ids), FakeWriter(), ids)
        batch = self.ledger.create_batch([{"kind": "text", "spool_path": self.ledger.spool("synthetic")}]); item_id = batch["items"][0]["id"]
        self.ledger.transition(item_id, "confirmed", "confirmed", document_id=1, metadata={})
        before = set(self.ledger.spool_dir.iterdir())
        with self.assertRaises(LibraryError): service.resume(item_id, {"ocr": [{"method": "manual", "text": "synthetic OCR"}]})
        self.assertEqual(before, set(self.ledger.spool_dir.iterdir()))

    def test_resume_rejected_file_ocr_never_creates_spool(self):
        root = Path(self.temp.name) / "approved"; root.mkdir(); approved = root / "fixture.txt"; approved.write_text("synthetic")
        outside = Path(self.temp.name) / "outside.txt"; outside.write_text("synthetic")
        ids = fields(); service = LibraryService(self.ledger, LibraryWorker(self.ledger, FakeNative()), LibrarySearch(FakeReader({"id": 1, "content": "", "custom_fields": []}), ids), FakeWriter(), ids, [root])
        item_id = self.ledger.create_batch([{"kind": "url", "locator": "https://mp.weixin.qq.com/s?__biz=b&mid=1&idx=2"}])["items"][0]["id"]
        before = set(self.ledger.spool_dir.iterdir()); ocr = [{"method": "manual", "text": "synthetic OCR"}]
        with self.assertRaises(LibraryError): service.resume(item_id, {"file_path": str(approved), "ocr": ocr})
        self.assertEqual(before, set(self.ledger.spool_dir.iterdir())); self.assertNotIn("synthetic OCR", json.dumps(self.ledger.get_item(item_id)))
        with self.assertRaises(LibraryError): service.resume(item_id, {"file_path": str(outside), "ocr": ocr})
        self.assertEqual(before, set(self.ledger.spool_dir.iterdir())); self.assertNotIn("synthetic OCR", json.dumps(self.ledger.get_item(item_id)))

    def test_reanalysis_enqueues_without_upload_and_skips_unchanged(self):
        batch = self.ledger.create_batch([{"kind": "text", "spool_path": self.ledger.spool("synthetic")}]); item_id = batch["items"][0]["id"]
        self.ledger.transition(item_id, "confirmed", "confirmed", document_id=73, task_uuid="completed-task", metadata={})
        ids = fields(); native = ReanalysisNative(); service = LibraryService(self.ledger, LibraryWorker(self.ledger, native), LibrarySearch(FakeReader({"id": 1, "content": "", "custom_fields": []}), ids), FakeWriter(), ids)
        queued = service.reanalyze(item_id, {"analysis_version": "extractive-1"}); self.assertEqual(queued["status"], "queued")
        self.assertTrue(service.worker.run_once()); self.assertEqual(native.calls, 1); self.assertEqual(self.ledger.get_item(item_id)["status"], "confirmed")
        self.ledger.transition(item_id, "confirmed", "confirmed", metadata={})
        native.result = "unchanged"; service.reanalyze(item_id, {"analysis_version": "extractive-1"}); service.worker.run_once()
        self.assertEqual(self.ledger.get_item(item_id)["stage"], "analysis_unchanged")

    def test_native_reanalysis_repeat_uses_source_hash_and_no_second_analysis(self):
        ids = fields(); source = "existing source"; source_hash = sha256_bytes(normalized_text(source).encode())
        class Analyzer:
            version = "approved-1"
            def __init__(self): self.calls = 0
            def __call__(self, value): self.calls += 1; return {"version": self.version, "content_hash": sha256_bytes(normalized_text(value).encode()), "summary": "synthetic", "key_points": []}
        analyzer = Analyzer(); document = {"id": 9, "content": "── SOURCE ──\n" + source + "\n── END SOURCE ──", "custom_fields": [
            {"field": ids["lib_content_hash"], "value": "original-binary-hash"}, {"field": ids["lib_analysis_version"], "value": "old"}, {"field": ids["lib_analysis"], "value": "{}"}]}
        reader = MutableNativeClient(document); writer = MutableNativeClient(document); ingestor = NativeIngestor(reader, writer, ids, analyzer=analyzer)
        self.assertEqual(ingestor.reanalyze(9, "approved-1"), "updated"); self.assertEqual(analyzer.calls, 1)
        self.assertEqual(ingestor.reanalyze(9, "approved-1"), "unchanged"); self.assertEqual(analyzer.calls, 1)

    def test_native_permission_and_evidence_suffix_no_answer(self):
        client = NativeClient("http://127.0.0.1:4386", "not-a-secret")
        class Opener:
            def open(self, *args, **kwargs): raise HTTPError("http://127.0.0.1:4386/api/documents/", 403, "no", {}, None)
        client.opener = Opener()
        with self.assertRaises(LibraryError) as denied: client.search("term")
        self.assertEqual(denied.exception.code, "native_permission")
        with self.assertRaises(LibraryError) as writer_denied: client.request("PATCH", "/api/documents/7/", {})
        self.assertEqual(writer_denied.exception.code, "native_permission")
        ids = fields(); document = {"id": 7, "title": "Synthetic", "content": "real source memory evidence\n── 分析資料（非原文；未經事實查核）──\ngenerated memory", "custom_fields": [
            {"field": ids["lib_canonical_url"], "value": "https://example.invalid/a"}, {"field": ids["lib_completeness"], "value": "complete"}]}
        search = LibrarySearch(FakeReader(document), ids)
        hit = search.evidence(7, "memory")
        self.assertTrue(hit["hits"]); self.assertNotIn("generated", hit["hits"][0]["excerpt"])
        self.assertTrue(search.search("missing")["no_match"])

    def test_native_search_parameter_and_legacy_numeric_evidence_offsets(self):
        captured = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit): return b'{"results":[],"next":null}'
        class Opener:
            def open(self, request, timeout): captured.append(request.full_url); return Response()
        client = NativeClient("http://127.0.0.1:4386", "synthetic"); client.opener = Opener(); client.search("witr")
        self.assertIn("search=witr", captured[0]); self.assertNotIn("query=", captured[0])
        ids = fields(); raw = "prefix\nwitr exact legacy evidence\n── 分析資料（非原文；未經事實查核）──\nignored"
        document = {"id": 12, "title": "Legacy", "content": raw, "custom_fields": [
            {"field": 90, "value": "https://example.invalid/a?utm_source=synthetic"}, {"field": 91, "value": "2026-01-02"},
            {"field": 92, "value": '{"author":"Known author"}'}]}
        reader = SearchReader(document); search = LibrarySearch(reader, ids, {"wx_source_url": 90, "wx_publish_date": 91, "wx_enrichment": 92})
        result = search.search("端口被占用，witr 能幫我查到什麼？")
        self.assertEqual(reader.terms[0], "witr"); self.assertFalse(result["no_match"]); self.assertEqual(result["matches"][0]["canonical_url"], "https://example.invalid/a")
        hit = search.evidence(12, "legacy evidence")["hits"][0]
        self.assertEqual(raw.split("── 分析資料", 1)[0][hit["start"]:hit["end"]], hit["excerpt"]); self.assertEqual(hit["author"], "Known author"); self.assertEqual(hit["published_at"], "2026-01-02")

    def test_search_cross_script_past_12000_suffix_and_provider_validation(self):
        ids = fields(); source = "前綴" * 7000 + " simplified 记忆5 evidence" + "\n── 分析資料（非原文；未經事實查核）──\n分析記憶0"
        document = {"id": 8, "title": "Synthetic", "content": source, "custom_fields": []}
        search = LibrarySearch(FakeReader(document), ids)
        self.assertIn("记忆", " ".join(variants("這份記憶問題")))
        hit = search.evidence(8, "這份記憶問題")
        self.assertTrue(hit["hits"]); self.assertGreater(hit["hits"][0]["start"], 12000); self.assertNotIn("分析", hit["hits"][0]["excerpt"])
        self.assertTrue(search.search("absent-term")["no_match"])
        result = enrich("One source sentence. Another source sentence.", "extractive-1")
        self.assertEqual(result["method"], "extractive")
        approved = enrich("synthetic", "approved-1", lambda request: '{"summary":"verified","key_points":["point"]}')
        self.assertEqual(approved["method"], "approved_provider")
        with self.assertRaises(LibraryError): validate_provider_response("[]", "h", "v")

    def test_http_auth_routes_contract_and_fastmcp_registration(self):
        ids = fields(); document = {"id": 1, "title": "Synthetic", "content": "Hermes local evidence", "custom_fields": []}
        search = LibrarySearch(FakeReader(document), ids)
        service = LibraryService(self.ledger, LibraryWorker(self.ledger, FakeNative()), search, FakeWriter(), ids)
        handler_type = handler_factory(service, "service-test-token")
        def dispatch(method, path, token=None, body=b"{}"):
            handler = object.__new__(handler_type); handler.command, handler.path, handler.rfile = method, path, io.BytesIO(body)
            handler.headers = {"Content-Length": str(len(body)), **({"Authorization": "Bearer " + token} if token else {})}
            return handler._dispatch()
        with self.assertRaises(LibraryError) as denied: dispatch("POST", "/v1/batches")
        self.assertEqual(denied.exception.code, "unauthorized")
        response = dispatch("POST", "/v1/batches", "service-test-token", json.dumps({"text": "plain synthetic text"}).encode())
        self.assertEqual(response["total"], 1)
        batch_id, item_id = response["batch_id"], response["items"][0]["id"]
        self.assertEqual(dispatch("GET", f"/v1/batches/{batch_id}", "service-test-token")["batch_id"], batch_id)
        self.assertEqual(dispatch("POST", f"/v1/items/{item_id}/resume", "service-test-token")["status"], "queued")
        self.assertIn("matches", dispatch("POST", "/v1/search", "service-test-token", json.dumps({"query": "Hermes"}).encode()))
        self.assertIn("hits", dispatch("GET", "/v1/documents/1/evidence?query=Hermes", "service-test-token"))
        self.assertEqual(dispatch("PATCH", "/v1/documents/1/state", "service-test-token", json.dumps({"read": True}).encode())["read"], True)
        self.assertEqual(dispatch("POST", f"/v1/batches/{batch_id}/cancel", "service-test-token")["status"], "cancelled")
        self.assertEqual(dispatch("GET", "/health")["version"], "0.3.0")
        openapi = json.loads((Path(__file__).parents[1] / "contracts/openapi.json").read_text())
        self.assertTrue({"/v1/batches", "/v1/search", "/v1/documents/{id}/state", "/v1/migrate"}.issubset(openapi["paths"]))
        api = LocalAPI("service-test-token")
        # Registration is actual SDK registration; the client is not invoked here.
        names = {tool.name for tool in asyncio.run(create_server(api).list_tools())}
        self.assertEqual(names, {"submit_batch", "batch_status", "resume_item", "cancel_batch", "search_library", "read_evidence", "set_reading_state", "migrate_legacy"})

    def test_submit_and_resume_text_character_limits_direct_and_http(self):
        service = self.size_service()
        item_id = self.ledger.create_batch([{"kind": "text", "spool_path": self.ledger.spool("original")}])["items"][0]["id"]
        for endpoint in ("submit", "resume"):
            path = "/v1/batches" if endpoint == "submit" else f"/v1/items/{item_id}/resume"
            for transport in ("direct", "http"):
                for length in (99_999, 100_000):
                    payload = {"text": "a" * length}
                    result = (getattr(service, endpoint)(payload) if endpoint == "submit" else service.resume(item_id, payload)) if transport == "direct" else self.size_dispatch(service, path, payload)
                    self.assertEqual(result["total"] if endpoint == "submit" else result["status"], 1 if endpoint == "submit" else "queued")
                payload = {"text": "a" * 100_001}
                invoke = (lambda: getattr(service, endpoint)(payload) if endpoint == "submit" else service.resume(item_id, payload)) if transport == "direct" else (lambda: self.size_dispatch(service, path, payload))
                self.assert_size_rejected_without_effects(invoke, item_id,
                    "text must be at most 100000 characters" if endpoint == "submit" else "invalid resume text")

    def test_submit_and_resume_canonical_envelope_limits_direct_and_http(self):
        service = self.size_service()
        item_id = self.ledger.create_batch([{"kind": "text", "spool_path": self.ledger.spool("original")}])["items"][0]["id"]
        size = lambda value: len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        submit_base = {"text": "a" * 100_000, "files": [{"path": "/synthetic/"}]}
        submit_equal = {"text": submit_base["text"], "files": [{"path": "/synthetic/" + "k" * (131_072 - size(submit_base))}]}
        resume_base = {"text": "a" * 100_000, "ocr": [{"text": "x" * 20_000}, {"text": ""}]}
        resume_equal = {"text": resume_base["text"], "ocr": [resume_base["ocr"][0], {"text": "x" * (131_072 - size(resume_base))}]}
        self.assertEqual(size(submit_equal), 131_072)
        self.assertEqual(size(resume_equal), 131_072)
        self.assertLessEqual(len(resume_equal["ocr"][1]["text"]), 20_000)
        for endpoint, path, equal in (("submit", "/v1/batches", submit_equal),
                                      ("resume", f"/v1/items/{item_id}/resume", resume_equal)):
            inside = {**equal}
            over = {**equal}
            if endpoint == "submit":
                inside["files"] = [{"path": equal["files"][0]["path"][:-1]}]
                over["files"] = [{"path": equal["files"][0]["path"] + "k"}]
            else:
                inside["ocr"] = [equal["ocr"][0], {"text": equal["ocr"][1]["text"][:-1]}]
                over["ocr"] = [equal["ocr"][0], {"text": equal["ocr"][1]["text"] + "x"}]
            self.assertEqual((size(inside), size(equal), size(over)), (131_071, 131_072, 131_073))
            for transport in ("direct", "http"):
                for payload in (inside, equal):
                    result = (service.submit(payload) if endpoint == "submit" else service.resume(item_id, payload)) if transport == "direct" else self.size_dispatch(service, path, payload)
                    self.assertTrue(result)
                invoke = (lambda: service.submit(over) if endpoint == "submit" else service.resume(item_id, over)) if transport == "direct" else (lambda: self.size_dispatch(service, path, over))
                self.assert_size_rejected_without_effects(invoke, item_id, "JSON request exceeds limit")

        extra = {"text": "a", "unused": "x" * 131_072}
        self.assert_size_rejected_without_effects(lambda: service.submit(extra), item_id, "JSON request exceeds limit")
        self.assert_size_rejected_without_effects(lambda: service.resume(item_id, extra), item_id, "JSON request exceeds limit")

    def test_escaped_and_chinese_text_count_toward_canonical_bytes(self):
        service = self.size_service()
        item_id = self.ledger.create_batch([{"kind": "text", "spool_path": self.ledger.spool("original")}])["items"][0]["id"]
        size = lambda value: len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        for endpoint, path in (("submit", "/v1/batches"), ("resume", f"/v1/items/{item_id}/resume")):
            for text in ('"' * 65_536, "繁體中文" * 5_500):
                payload = {"text": text}
                self.assertLess(len(text), 100_000)
                self.assertGreater(size(payload), 131_072)
                raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if text.startswith("繁"):
                    self.assertLess(len(raw), 131_072)
                for transport in ("direct", "http"):
                    invoke = (lambda: service.submit(payload) if endpoint == "submit" else service.resume(item_id, payload)) if transport == "direct" else (lambda: self.size_dispatch(service, path, body=raw))
                    self.assert_size_rejected_without_effects(invoke, item_id, "JSON request exceeds limit")
            accepted = {"text": "繁體中文" * 4_000 + '"' * 1_000}
            self.assertLess(size(accepted), 131_072)
            self.assertTrue(service.submit(accepted) if endpoint == "submit" else service.resume(item_id, accepted))
            self.assertTrue(self.size_dispatch(service, path, body=json.dumps(accepted, ensure_ascii=False).encode("utf-8")))
            escaped = json.dumps({"text": "繁體中文" * 4_000}, ensure_ascii=True).encode("utf-8")
            self.assertTrue(self.size_dispatch(service, path, body=escaped))

    def test_raw_http_cap_and_canonical_http_cap_are_independent(self):
        service = self.size_service()
        item_id = self.ledger.create_batch([{"kind": "text", "spool_path": self.ledger.spool("original")}])["items"][0]["id"]
        for path in ("/v1/batches", f"/v1/items/{item_id}/resume"):
            compact = b'{"text":"a"}'
            raw_over = compact + b" " * (131_073 - len(compact))
            self.assert_size_rejected_without_effects(lambda: self.size_dispatch(service, path, body=raw_over), item_id, "JSON request exceeds limit")
            self.assert_size_rejected_without_effects(lambda: self.size_dispatch(service, path, body=compact, content_length=131_073), item_id, "JSON request exceeds limit")

    def test_real_loopback_submit_resume_size_status_and_no_effects(self):
        service = self.size_service()
        item_id = self.ledger.create_batch([{"kind": "text", "spool_path": self.ledger.spool("original")}])["items"][0]["id"]
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(service, "service-test-token"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def post(path, raw):
                request = Request(f"http://127.0.0.1:{server.server_port}{path}", data=raw,
                                  headers={"Authorization": "Bearer service-test-token", "Content-Type": "application/json"})
                try:
                    with urlopen(request, timeout=5) as response:
                        return response.status, json.loads(response.read())
                except HTTPError as error:
                    return error.code, json.loads(error.read())

            for path in ("/v1/batches", f"/v1/items/{item_id}/resume"):
                status, result = post(path, json.dumps({"text": "a" * 100_000}).encode())
                self.assertEqual(status, 200)
                self.assertIn("status" if path.endswith("resume") else "batch_id", result)
                counts = tuple(self.ledger.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                               for table in ("batches", "items"))
                spools = {p.name: p.read_bytes() for p in self.ledger.spool_dir.iterdir()}
                item = dict(self.ledger.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())
                for raw in (json.dumps({"text": "a" * 100_001}).encode(),
                            json.dumps({"text": "a" * 150_000}).encode(),
                            json.dumps({"text": "繁體中文" * 5_500}, ensure_ascii=False).encode()):
                    status, result = post(path, raw)
                    self.assertEqual((status, result["error"]), (400, "invalid_request"))
                    self.assertEqual(counts, tuple(self.ledger.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                                                   for table in ("batches", "items")))
                    self.assertEqual(spools, {p.name: p.read_bytes() for p in self.ledger.spool_dir.iterdir()})
                    self.assertEqual(item, dict(self.ledger.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_size_schema_and_mcp_http_wire_boundary(self):
        openapi = json.loads((Path(__file__).parents[1] / "contracts/openapi.json").read_text())
        for name in ("SubmitBatch", "ResumeItem"):
            schema = openapi["components"]["schemas"][name]
            self.assertEqual(schema["properties"]["text"]["maxLength"], 100_000)
            self.assertEqual(schema["x-max-json-utf8-bytes"], 131_072)
            self.assertIn("raw HTTP", schema["description"])
        service = self.size_service()
        handler_type = handler_factory(service, "service-test-token")
        wires = []
        class Response:
            def __init__(self, result): self.result = result
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, *_args): return json.dumps(self.result).encode()
        class Opener:
            def open(self, request, timeout):
                wires.append(request.data)
                handler = object.__new__(handler_type)
                handler.command, handler.path, handler.rfile = request.get_method(), request.full_url.removeprefix("http://127.0.0.1:4388"), io.BytesIO(request.data)
                handler.headers = {"Content-Length": str(len(request.data)), "Authorization": request.get_header("Authorization")}
                return Response(handler._dispatch())
        client = LocalAPI("service-test-token")
        server = create_server(client)
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        self.assertTrue({"submit_batch", "resume_item"}.issubset(names))
        base = {"text": "", "urls": [], "files": [], "title": None, "idempotency_key": None}
        count = (131_072 - len(json.dumps(base).encode())) // 2
        text = '"' * count
        with patch("extensions.library.mcp_server.build_opener", return_value=Opener()):
            result = asyncio.run(server.call_tool("submit_batch", {"text": text}))
            self.assertTrue(result)
            item_id = self.ledger.db.execute("SELECT id FROM items ORDER BY created_at DESC LIMIT 1").fetchone()[0]
            resumed = asyncio.run(server.call_tool("resume_item", {"item_id": item_id, "text": "繁體中文"}))
            self.assertTrue(resumed)
        self.assertLessEqual(len(wires[0]), 131_072)
        self.assertGreaterEqual(len(wires[0]), 131_071)
        self.assertGreater(len(wires[0]), len(json.dumps(json.loads(wires[0]), ensure_ascii=True, separators=(",", ":")).encode()))
        self.assertEqual(json.loads(wires[0])["title"], None)
        self.assertEqual(json.loads(wires[1]), {"text": "繁體中文"})

    def test_mcp_submit_files_schema_and_payload(self):
        api = RecordingAPI(); server = create_server(api)
        tool = [value for value in asyncio.run(server.list_tools()) if value.name == "submit_batch"][0]
        self.assertIn("files", tool.inputSchema["properties"])
        asyncio.run(server.call_tool("submit_batch", {"files": [{"path": "/approved/synthetic.txt"}]}))
        self.assertEqual(api.calls, [("POST", "/v1/batches", {"text": None, "urls": [], "files": [{"path": "/approved/synthetic.txt"}], "title": None, "idempotency_key": None})])

    def test_mcp_resume_schema_and_payload(self):
        api = RecordingAPI(); server = create_server(api)
        tool = [value for value in asyncio.run(server.list_tools()) if value.name == "resume_item"][0]
        self.assertTrue({"title", "file_path", "ocr"}.issubset(tool.inputSchema["properties"]))
        asyncio.run(server.call_tool("resume_item", {"item_id": "i1", "title": "Synthetic", "ocr": [{"method": "manual", "text": "synthetic"}]}))
        self.assertEqual(api.calls, [("POST", "/v1/items/i1/resume", {"title": "Synthetic", "ocr": [{"method": "manual", "text": "synthetic"}]})])

    def test_mcp_text_schema_limit_and_pre_http_validation(self):
        self.assertEqual((MAX_INGRESS_TEXT_CHARS, MAX_JSON_BYTES), (100_000, 131_072))
        api = RecordingAPI(); server = create_server(api)
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        for name, arguments, path in (("submit_batch", {}, "/v1/batches"),
                                      ("resume_item", {"item_id": "synthetic-item"}, "/v1/items/synthetic-item/resume")):
            schema = tools[name].inputSchema["properties"]["text"]
            string_branch, null_branch = schema["anyOf"]
            self.assertEqual((string_branch["type"], string_branch["maxLength"]), ("string", MAX_INGRESS_TEXT_CHARS))
            self.assertEqual(null_branch["type"], "null")
            self.assertIsNone(schema["default"])
            for phrase in ("Unicode code points", "json.dumps(value, ensure_ascii=True", "131072 UTF-8 bytes",
                           "raw HTTP body", "spaces", "optional null fields"):
                self.assertIn(phrase, string_branch["description"])

            self.assertTrue(asyncio.run(server.call_tool(name, {**arguments, "text": None})))
            self.assertEqual(api.calls[-1][:2], ("POST", path))
            self.assertTrue(asyncio.run(server.call_tool(name, {**arguments, "text": "繁" * MAX_INGRESS_TEXT_CHARS})))
            self.assertEqual(api.calls[-1][:2], ("POST", path))
            self.assertEqual(len(api.calls[-1][2]["text"]), MAX_INGRESS_TEXT_CHARS)
            self.assertGreater(len(json.dumps(api.calls[-1][2], ensure_ascii=True, separators=(",", ":")).encode()), MAX_JSON_BYTES)
            before = len(api.calls)
            with self.assertRaises(ToolError) as raised:
                asyncio.run(server.call_tool(name, {**arguments, "text": "a" * (MAX_INGRESS_TEXT_CHARS + 1)}))
            self.assertIn("at most 100000 characters", str(raised.exception))
            self.assertEqual(len(api.calls), before)

    def test_resume_sanitizes_response_and_url_supplement_is_processed(self):
        ids = fields(); service = LibraryService(self.ledger, LibraryWorker(self.ledger, ConfirmingNative()), LibrarySearch(FakeReader({"id": 1, "content": "", "custom_fields": []}), ids), FakeWriter(), ids)
        private_url = "https://mp.weixin.qq.com/s?__biz=b&mid=1&idx=2&sn=s&x" + "sec_token=private"
        batch = self.ledger.create_batch([{"kind": "url", "locator": private_url}]); item_id = batch["items"][0]["id"]
        response = service.resume(item_id, {"text": "approved supplement", "title": "Supplement", "ocr": [{"method": "manual", "text": "synthetic"}]})
        self.assertNotIn("private_locator", response); self.assertNotIn("spool_path", response); self.assertNotIn("xsec", json.dumps(response))
        self.assertNotIn("synthetic", json.dumps(response)); self.assertEqual(self.ledger.get_item(item_id)["metadata"]["title"], "Supplement")
        self.assertTrue(service.worker.run_once()); self.assertEqual(self.ledger.get_item(item_id)["status"], "confirmed")
