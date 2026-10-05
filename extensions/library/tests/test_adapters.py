"""Offline source adapter and API contract checks; no browser or network calls."""
import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path

from extensions.library import VERSION
from extensions.library.ledger import Ledger
from extensions.library.local_ocr import LocalOCR, local_ocr_runner
from extensions.library.mcp_server import create_server
from extensions.library.model import LIB_FIELDS, LibraryError, canonical_url
from extensions.library.model import sha256_bytes
from extensions.library.native import NativeIngestor, custom_fields
from extensions.library.service import LibraryService, handler_factory
from extensions.library.sources import IMAGE_CDN, SafeFetcher, URLGuard, parse_wechat_html, stable_id
from extensions.library.worker import LibraryWorker
from extensions.library.xhs_edge import EdgeNoteAdapter, VISIBLE_NOTE_JS


class NoNative:
    def __init__(self): self.uploads = []; self.documents = {}
    def find_existing(self, *args): return None
    def upload(self, data, body, source_id, canonical, digest):
        self.uploads.append((data, body, source_id, canonical, digest)); return "task-1"
    def task_result(self, task): return {"document_id": 4}
    def verify(self, document_id, item): return True
    def attach_ocr(self, *args): self.ocr = args


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ledger = Ledger(root / "jobs.sqlite", root / "stage")

    def test_xhs_visible_note_only_and_stable_variants(self):
        seen = []
        def runner(url, script):
            seen.append((url, script))
            return {"status": "ok", "location": "https://www.xiaohongshu.com/discovery/item/N123?xsec_token=private",
                    "title": "Note", "text": "Visible body", "images": []}
        adapter = EdgeNoteAdapter(runner)
        data = adapter.capture("https://www.xiaohongshu.com/explore/N123?xsec_token=private")
        self.assertEqual(data["source_id"], "xhs:N123")
        self.assertNotIn("xsec", data["canonical_url"])
        self.assertNotIn("sidebar", VISIBLE_NOTE_JS.lower())
        self.assertNotIn("document.body", VISIBLE_NOTE_JS)
        self.assertLess(VISIBLE_NOTE_JS.index("if (!root)"), VISIBLE_NOTE_JS.index("const gates="))
        self.assertIn("getBoundingClientRect", VISIBLE_NOTE_JS)
        self.assertIn("unclassified_images", VISIBLE_NOTE_JS)
        self.assertNotIn("private", seen[0][0])
        with self.assertRaises(LibraryError): stable_id("xhs", "https://xhslink.com/short")
        with self.assertRaises(LibraryError): EdgeNoteAdapter(lambda *_: {**runner("", ""), "location": "https://www.xiaohongshu.com/explore/OTHER"}).capture("https://www.xiaohongshu.com/explore/N123")
        for gate, expected in (("login","needs_login"),("risk","blocked"),("deleted","failed")):
            self.assertEqual(EdgeNoteAdapter(lambda *_: {"status":gate}).capture("https://www.xiaohongshu.com/explore/N123")["terminal"], expected)
        note = EdgeNoteAdapter(lambda *_: {"status":"ok","location":"https://www.xiaohongshu.com/explore/N123",
            "title":"Visible","text":"Visible note","images":[],"unclassified_images":1}).capture("https://www.xiaohongshu.com/explore/N123")
        self.assertEqual(note["unclassified_images"],1)

    def test_xhs_partial_and_image_only_no_engine(self):
        url = "https://www.xiaohongshu.com/explore/N123"
        image = "https://sns-img-qc.xhscdn.com/image.png"
        def adapter(body): return EdgeNoteAdapter(lambda *_: {"status":"ok","location":url,"title":"Visible","text":body,"images":[image]})
        native = NoNative()
        item = self.ledger.create_batch([{"kind":"url","locator":url}])["items"][0]
        worker = LibraryWorker(self.ledger, native, edge=adapter("Visible text"), sleep=lambda _: None)
        worker.run_once()
        self.assertEqual(self.ledger.get_item(item["id"])["status"], "partial")
        self.assertEqual(native.uploads[0][0]["completeness"], "partial")
        item2 = self.ledger.create_batch([{"kind":"url","locator":url}])["items"][0]
        worker.edge = adapter("")
        worker.run_once()
        self.assertEqual(self.ledger.get_item(item2["id"])["status"], "needs_ocr")
        self.assertEqual(len(native.uploads), 1)

    def test_xhs_shortlink_resolves_before_edge_and_never_uses_slug(self):
        short = "https://xhslink.com/synthetic-short"
        final = "https://www.xiaohongshu.com/explore/N123?xsec_token=private"
        class Fetcher:
            def get(self, url):
                self.requested = url
                return final, {"Content-Type":"text/html"}, b""
        fetcher = Fetcher(); seen = []
        edge = EdgeNoteAdapter(lambda url, _: seen.append(url) or {"status":"ok","location":final,"title":"Note","text":"Visible","images":[]})
        item = self.ledger.create_batch([{"kind":"url","locator":short}])["items"][0]
        worker = LibraryWorker(self.ledger, NoNative(), fetcher=fetcher, edge=edge, sleep=lambda _: None)
        worker.run_once()
        self.assertEqual(fetcher.requested, short)
        self.assertEqual(seen, ["https://www.xiaohongshu.com/explore/N123"])
        self.assertEqual(self.ledger.get_item(item["id"])["source_id"], "xhs:N123")

    def test_local_ocr_exact_host_mime_and_no_engine(self):
        image = "https://sns-img-qc.xhscdn.com/image.png"
        guard = URLGuard(lambda *_: ["8.8.8.8"], allowed_hosts=IMAGE_CDN, secure_only=True)
        fetcher = SafeFetcher(guard, lambda *_: (200, {"Content-Type":"image/png"}, b"\x89PNG\r\n\x1a\nfixture"))
        self.assertEqual(LocalOCR(fetcher, lambda *_: None).image(image)["status"], "needs_ocr")
        self.assertEqual(LocalOCR(fetcher, lambda *_: "readable").image(image)["text"], "readable")
        self.assertEqual(LocalOCR(fetcher, lambda *_: ("readable", "vision")).image(image)["method"], "vision")
        with self.assertRaises(LibraryError): LocalOCR(fetcher).image("https://example.invalid/image.png")
        bad = SafeFetcher(guard, lambda *_: (200, {"Content-Type":"image/jpeg"}, b"not jpeg"))
        with self.assertRaises(LibraryError): LocalOCR(bad, lambda *_: "invented").image(image)
        redirect = SafeFetcher(guard, lambda *_: (302, {"Location":"https://example.invalid/out"}, b""))
        with self.assertRaises(LibraryError): LocalOCR(redirect, lambda *_: "invented").image(image)

    def test_swift_vision_fallback_uses_private_file_and_cleans_it(self):
        fake_swift = Path(self.tmp.name) / "swift"; fake_swift.write_text("synthetic executable marker")
        seen = {}
        def run(argv, folder, timeout, env=None):
            seen.update({"argv":argv,"folder":folder,"timeout":timeout,"env":env,
                         "mode":(folder / "image.png").stat().st_mode & 0o777,
                         "bytes":(folder / "image.png").read_bytes()})
            return "synthetic OCR"
        result = local_ocr_runner(b"\x89PNG\r\n\x1a\nfixture", ".png", swift_path=fake_swift,
                                  which=lambda _:None, run=run)
        self.assertEqual(result,("synthetic OCR","vision"))
        self.assertEqual(seen["argv"][0],str(fake_swift))
        self.assertEqual(Path(seen["argv"][1]).name,"vision_ocr.swift")
        self.assertEqual(seen["mode"],0o600)
        self.assertEqual(seen["bytes"],b"\x89PNG\r\n\x1a\nfixture")
        self.assertLessEqual(seen["timeout"],60)
        self.assertFalse(seen["folder"].exists())

    def test_wechat_public_identity_and_canonical_allowlist(self):
        canonical = "https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1&sn=xyz"
        html = '<meta property="og:title" content="Article"><div id="js_content">Actual body</div><script>var msg_link="' + canonical.replace("/", "\\/") + '&token=private";</script>'
        parsed = parse_wechat_html(html)
        self.assertEqual(parsed["canonical_url"], canonical)
        self.assertEqual(stable_id("wechat", canonical), stable_id("wechat", canonical + "&utm_source=x"))
        self.assertEqual(canonical_url(canonical + "&extra=private"), canonical)
        with self.assertRaises(LibraryError): stable_id("wechat", "https://mp.weixin.qq.com/s/ordinal-4")
        with self.assertRaises(LibraryError): stable_id("wechat", "https://other.invalid/s?__biz=a&mid=1&idx=1")
        with self.assertRaises(LibraryError): stable_id("wechat", canonical + "&mid=999")
        with self.assertRaises(LibraryError): canonical_url(canonical + "&mid=999")
        with self.assertRaises(LibraryError): stable_id("xhs", "https://www.xiaohongshu.com/explore/N123/other")
        self.assertIsNone(parse_wechat_html('<div id="js_content">Body</div><a href="' + canonical + '">Related</a>')["canonical_url"])
        self.assertIsNone(parse_wechat_html('<div id="js_content">Body</div><span id="publish_time">unknown</span>')["published_at"])

    def test_shortlink_resume_metadata_and_binary_bytes(self):
        root = Path(self.tmp.name) / "approved"; root.mkdir()
        source = root / "scan.pdf"; raw = b"%PDF-1.4\nsynthetic\x00\xff"; source.write_bytes(raw)
        short = "https://mp.weixin.qq.com/s/short-slug?token=private"
        canonical = "https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1&sn=xyz"
        item = self.ledger.create_batch([{"kind":"url","locator":short}])["items"][0]
        worker = LibraryWorker(self.ledger, NoNative(), accepted_roots=[root], sleep=lambda _: None)
        service = LibraryService(self.ledger, worker, None, None, {}, [root])
        with self.assertRaises(LibraryError): service.resume(item["id"], {"text":"Body", "canonical_url":canonical, "publish_date":"tomorrow", "provenance":"operator-public"})
        with self.assertRaises(LibraryError): service.resume(item["id"], {"text":"Body", "canonical_url":"https://example.invalid/a", "provenance":"operator-public"})
        service.resume(item["id"], {"file_path":str(source), "title":"Retained title", "ocr":[{"method":"manual","text":"Scanned words"}],
                                    "canonical_url":canonical, "author":"Known", "publish_date":"2026-01-02", "fetched_at":"2026-01-03T00:00:00Z", "provenance":"operator-public"})
        data = worker._extract(self.ledger.get_item(item["id"]))
        self.assertEqual(data["file_bytes"], raw)
        self.assertEqual(data["title"], "Retained title")
        self.assertEqual(data["source_id"], stable_id("wechat", canonical))
        self.assertEqual((data["author"], data["published_at"]), ("Known", "2026-01-02"))
        self.assertEqual(self.ledger.get_item(item["id"])["private_locator"], short)
        self.assertNotIn("private", json.dumps(service.resume(item["id"], {"title":"Retained title"})))

    def test_changed_source_creates_separate_revision_with_provenance(self):
        class RevisionNative(NoNative):
            def __init__(self):
                super().__init__()
                self.original = {"id":8, "content":"Original source text"}
                self.patches = []
            def find_existing(self, *args): return 8
            def source_changed(self, doc, digest): return True
            def find_version(self, version_id, digest): return None
        native = RevisionNative()
        item = self.ledger.create_batch([{"kind":"text","spool_path":self.ledger.spool("New source text"),
                                          "metadata":{"source_label":"Synthetic label"}}])["items"][0]
        worker = LibraryWorker(self.ledger, native, sleep=lambda _: None)
        worker.run_once()
        self.assertEqual(self.ledger.get_item(item["id"])["status"], "confirmed")
        data, _, version_id, _, digest = native.uploads[0]
        self.assertTrue(version_id.endswith(":revision:" + digest[:16]))
        self.assertEqual(data["provenance"]["revision_of"], 8)
        self.assertEqual(data["source_label"], "Synthetic label")
        self.assertEqual(native.original["content"], "Original source text")
        self.assertEqual(native.patches, [])
        self.assertIn("New source text", native.uploads[0][1])
        uncertain = self.ledger.create_batch([{"kind":"text","spool_path":self.ledger.spool("New source text")}])["items"][0]
        self.ledger.transition(uncertain["id"], "importing", "version_upload", source_id=version_id,
                               content_hash=digest, metadata={"upload_intent":True,"revision_of":8})
        worker._reconcile_without_task(self.ledger.get_item(uncertain["id"]))
        self.assertEqual(self.ledger.get_item(uncertain["id"])["status"], "blocked")
        self.assertEqual(len(native.uploads), 1)

    def test_identity_aware_reconciliation_does_not_merge_equal_bodies(self):
        ids = {name:index + 1 for index,name in enumerate(LIB_FIELDS)}
        legacy_id, legacy_url_id = 900, 901
        canonical_a = "https://mp.weixin.qq.com/s?__biz=abc&mid=111&idx=1&sn=aaa"
        canonical_b = "https://mp.weixin.qq.com/s?__biz=abc&mid=222&idx=1&sn=bbb"
        same_hash = "shared-body-hash"
        def doc(doc_id, source_id=None, canonical=None, legacy_url=None):
            values = {ids["lib_content_hash"]:same_hash}
            if source_id: values[ids["lib_source_id"]] = source_id
            if canonical: values[ids["lib_canonical_url"]] = canonical
            if legacy_url: values[legacy_url_id] = legacy_url
            return {"id":doc_id,"custom_fields":[{"field":key,"value":value} for key,value in values.items()]}
        class Reader:
            def __init__(self,rows): self.rows=rows
            def pages(self,path): return self.rows
        reader = Reader([doc(1,stable_id("wechat",canonical_a),canonical_a)])
        native = NativeIngestor(reader,reader,ids,legacy_field_ids={"wx_source_id":legacy_id,"wx_source_url":legacy_url_id})
        self.assertIsNone(native.find_existing(stable_id("wechat",canonical_b),canonical_b,same_hash,canonical_b))
        self.assertEqual(native.find_existing(stable_id("wechat",canonical_a),canonical_a,"new-hash",canonical_a),1)
        short = "https://mp.weixin.qq.com/s/synthetic-short"
        self.assertEqual(native.find_existing(stable_id("wechat",canonical_a),canonical_a,same_hash,short),1)
        reader.rows = [doc(2,None,None,short)]
        self.assertEqual(native.find_existing(stable_id("wechat",canonical_a),canonical_a,same_hash,short),2)
        self.assertIsNone(native.find_existing(stable_id("wechat",canonical_b),canonical_b,same_hash,"https://mp.weixin.qq.com/s/other"))
        reader.rows = [doc(3)]
        self.assertEqual(native.find_existing(None,None,same_hash,None),3)
        self.assertIsNone(native.find_existing(stable_id("wechat",canonical_b),canonical_b,same_hash,None))
        tracked = canonical_a + "&utm_source=synthetic&scene=2"
        reader.rows = [doc(4,None,None,tracked)]
        self.assertEqual(native.find_existing(stable_id("wechat",canonical_a),canonical_a,same_hash,short),4)
        self.assertEqual(native.find_existing(stable_id("wechat",canonical_a),tracked,same_hash,None),4)
        reader.rows = [doc(5,stable_id("wechat",canonical_b),canonical_a,tracked)]
        self.assertIsNone(native.find_existing(stable_id("wechat",canonical_a),canonical_a,same_hash,short))
        reader.rows = [doc(6,None,canonical_b,short)]
        self.assertIsNone(native.find_existing(stable_id("wechat",canonical_a),canonical_a,same_hash,short))
        xhs = "https://www.xiaohongshu.com/explore/Synthetic123"
        reader.rows = [doc(7,stable_id("xhs",xhs),xhs)]
        self.assertIsNone(native.find_existing(stable_id("wechat",canonical_a),canonical_a,same_hash,None))
        reader.rows = [doc(8,None,xhs)]
        self.assertIsNone(native.find_existing(stable_id("wechat",canonical_a),canonical_a,same_hash,None))

    def test_confirmed_metadata_backfill_is_exact_idempotent_and_body_free(self):
        ids = {name:index + 1 for index,name in enumerate(LIB_FIELDS)}
        canonical = "https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1&sn=xyz"
        original = canonical + "&token=private"
        source_id = stable_id("wechat", canonical)
        document = {"id":7,"title":"Original title","content":"Original indexed source",
                    "custom_fields":[{"field":ids[name],"value":value} for name,value in {
                        "lib_platform":"wechat","lib_source_id":source_id,"lib_content_hash":"source-hash",
                        "lib_canonical_url":canonical,"lib_provenance":json.dumps({"source":{"adapter":"wechat"}}),
                        "lib_reading_state":"read","lib_starred":True,"lib_pending":False}.items()]}
        class Client:
            def __init__(self): self.patches = []
            def document(self, doc_id): return document
            def request(self, method, path, payload):
                self.patches.append((method,path,payload))
                self.assert_patch(payload)
                document["custom_fields"] = payload["custom_fields"]
            def assert_patch(self,payload):
                if set(payload) != {"custom_fields"}: raise AssertionError("body field was patched")
        client = Client(); native = NativeIngestor(client,client,ids)
        item = self.ledger.create_batch([{"kind":"url","locator":original}])["items"][0]
        self.ledger.transition(item["id"],"confirmed","confirmed",document_id=7,source_id=source_id,
                               canonical_url=canonical,content_hash="source-hash")
        worker = LibraryWorker(self.ledger,native)
        service = LibraryService(self.ledger,worker,None,None,{})
        payload = {"canonical_url":canonical,"author":"Known author","publish_date":"2026-01-02",
                   "fetched_at":"2026-01-03T04:05:06Z","provenance":"operator-public"}
        public = service.resume(item["id"],payload)
        self.assertEqual(public["status"],"confirmed")
        self.assertNotIn("private",json.dumps(public))
        self.assertEqual(len(client.patches),1)
        values = custom_fields(document)
        self.assertEqual(values[ids["lib_source_id"]],source_id)
        self.assertEqual(values[ids["lib_content_hash"]],"source-hash")
        self.assertEqual(values[ids["lib_reading_state"]],"read")
        self.assertTrue(values[ids["lib_starred"]])
        self.assertEqual(document["content"],"Original indexed source")
        revisions = json.loads(values[ids["lib_provenance"]])["metadata_revisions"]
        self.assertEqual(len(revisions),1)
        self.assertEqual(revisions[0]["provenance"],"operator-public")
        service.resume(item["id"],payload)
        self.assertEqual(len(client.patches),1)
        self.assertEqual(self.ledger.get_item(item["id"])["status"],"confirmed")
        for invalid in ({"text":"replacement","provenance":"operator-public"},
                        {"ocr":[{"text":"invented"}],"provenance":"operator-public"},
                        {"file_path":"/not-read","provenance":"operator-public"},
                        {"title":"New title","provenance":"operator-public"},
                        {"author":"Bad"},
                        {"publish_date":"tomorrow","provenance":"operator-public"},
                        {"canonical_url":"https://www.xiaohongshu.com/explore/N123","provenance":"operator-public"},
                        {"canonical_url":canonical.replace("https://","https://user:pass@"),"provenance":"operator-public"},
                        {"canonical_url":canonical.replace("mid=123","mid=999"),"provenance":"operator-public"}):
            with self.assertRaises(LibraryError): service.resume(item["id"],invalid)
        self.assertEqual(len(client.patches),1)

    def test_text_ocr_is_in_upload_and_binary_ocr_attaches_after_original(self):
        class IndexedNative(NoNative):
            def attach_ocr(self,*args): self.attached = args
            def verify_indexed_ocr(self,doc,ocr): return ocr == "Recognized words"
            def mark_ocr_indexed(self,doc,completeness): self.marked = (doc,completeness)
        native = IndexedNative()
        ocr_path = self.ledger.spool(json.dumps([{"text":"Recognized words","method":"manual"}]))
        text_item = self.ledger.create_batch([{"kind":"text","spool_path":self.ledger.spool("Visible text"),
            "metadata":{"ocr_spool_path":ocr_path,"ocr":[{"method":"manual"}]}}])["items"][0]
        worker = LibraryWorker(self.ledger,native,sleep=lambda _:None)
        worker.run_once()
        self.assertEqual(self.ledger.get_item(text_item["id"])["status"],"confirmed")
        self.assertIn("── SOURCE ──\nVisible text",native.uploads[0][1])
        self.assertIn("── OCR ──\nRecognized words\n── END OCR ──",native.uploads[0][1])
        self.assertEqual(native.marked,(4,"complete"))
        root = Path(self.tmp.name) / "approved"; root.mkdir()
        pdf = root / "scan.pdf"; original = b"%PDF-1.4\nsynthetic scan\x00\xff"; pdf.write_bytes(original)
        binary_ocr = self.ledger.spool(json.dumps([{"text":"Recognized words","method":"manual"}]))
        pdf_item = self.ledger.create_batch([{"kind":"file","locator":str(pdf),
            "metadata":{"ocr_spool_path":binary_ocr,"ocr":[{"method":"manual"}]}}])["items"][0]
        worker.accepted_roots = [root]
        worker.run_once()
        self.assertEqual(self.ledger.get_item(pdf_item["id"])["status"],"confirmed")
        self.assertEqual(native.uploads[1][0]["file_bytes"],original)
        self.assertNotIn("Recognized words",native.uploads[1][1])
        self.assertFalse(native.uploads[1][0]["ocr_indexed"])
        self.assertEqual(native.attached[2],"Recognized words")
        self.assertEqual(native.marked,(4,"complete"))

    def test_native_attach_ocr_preserves_extracted_content_and_is_idempotent(self):
        ids = {name:index + 1 for index,name in enumerate(LIB_FIELDS)}
        original = b"%PDF-1.4\nsynthetic\x00\xff"
        document = {"id":9,"content":"Paperless extracted source\n", "original":original,
                    "custom_fields":[{"field":ids["lib_provenance"],"value":json.dumps({"source":"file","ai":{"version":"v1"}})},
                                     {"field":ids["lib_starred"],"value":True}]}
        class Reader:
            def document(self,doc_id): return document.copy()
        class Writer:
            def __init__(self): self.patches=[]
            def request(self,method,path,payload):
                self.patches.append(payload)
                document["content"]=payload["content"]
                document["custom_fields"]=payload["custom_fields"]
        writer=Writer(); native=NativeIngestor(Reader(),writer,ids)
        provenance=[{"image_url":"synthetic-image","method":"manual"}]
        native.attach_ocr(9,"caller source must not replace native source","OCR words",provenance)
        native.attach_ocr(9,"","OCR words",provenance)
        self.assertEqual(len(writer.patches),1)
        self.assertEqual(document["original"],original)
        self.assertEqual(document["content"],"Paperless extracted source\n── OCR ──\nOCR words\n── END OCR ──")
        self.assertEqual(custom_fields(document)[ids["lib_starred"]],True)
        record=json.loads(custom_fields(document)[ids["lib_provenance"]])
        self.assertEqual(record["source"],"file")
        self.assertEqual(record["ai"],{"version":"v1"})
        self.assertEqual(record["ocr"],provenance)
        with self.assertRaises(LibraryError): native.attach_ocr(9,"","different OCR",provenance)
        self.assertEqual(len(writer.patches),1)

    def test_native_attach_ocr_requires_exact_reader_content_and_fields(self):
        ids = {name:index + 1 for index,name in enumerate(LIB_FIELDS)}
        document={"id":9,"content":"","custom_fields":[{"field":ids["lib_provenance"],"value":"{}"}]}
        class Reader:
            def document(self,doc_id): return document.copy()
        class Writer:
            def request(self,method,path,payload):
                document["custom_fields"]=payload["custom_fields"]
                # Simulate accepted PATCH whose content is absent from independent readback.
        native=NativeIngestor(Reader(),Writer(),ids)
        with self.assertRaises(LibraryError): native.attach_ocr(9,"","OCR only",[])
        self.assertEqual(document["content"],"")
        self.assertFalse(native.verify_indexed_ocr(9,"OCR only"))

    def test_binary_ocr_readback_failure_keeps_pending_without_second_upload(self):
        class FailedReadback(NoNative):
            def attach_ocr(self,*args): raise LibraryError("reader content mismatch","ocr_unindexed",502)
            def verify_indexed_ocr(self,*args): return False
        root=Path(self.tmp.name)/"approved"; root.mkdir()
        path=root/"scan.pdf"; original=b"%PDF-1.4\nsynthetic\x00\xff"; path.write_bytes(original)
        ocr_path=self.ledger.spool(json.dumps([{"text":"OCR only","method":"manual"}]))
        item=self.ledger.create_batch([{"kind":"file","locator":str(path),"metadata":{"ocr_spool_path":ocr_path}}])["items"][0]
        native=FailedReadback(); worker=LibraryWorker(self.ledger,native,accepted_roots=[root],sleep=lambda _:None)
        worker.run_once()
        saved=self.ledger.get_item(item["id"])
        self.assertEqual(saved["status"],"needs_ocr")
        self.assertEqual(saved["document_id"],4)
        self.assertTrue(Path(ocr_path).is_file())
        self.assertEqual(native.uploads[0][0]["file_bytes"],original)
        worker.run_once()
        self.assertEqual(len(native.uploads),1)

    def test_native_ocr_flag_requires_exact_reader_content_and_never_patches_body(self):
        ids = {name:index + 1 for index,name in enumerate(LIB_FIELDS)}
        document = {"id":9,"title":"Synthetic","content":"── SOURCE ──\nVisible\n── END SOURCE ──\n── OCR ──\nOCR words\n── END OCR ──",
                    "custom_fields":[{"field":ids["lib_source_id"],"value":"text:synthetic"},
                                     {"field":ids["lib_content_hash"],"value":"hash"},
                                     {"field":ids["lib_provenance"],"value":json.dumps({"source":"synthetic","ocr_indexed":False})},
                                     {"field":ids["lib_completeness"],"value":"partial"},
                                     {"field":ids["lib_extraction_status"],"value":"ocr_pending_readback"}]}
        class Client:
            def __init__(self): self.patches=[]
            def document(self,doc_id): return document
            def request(self,method,path,payload):
                self.patches.append(payload)
                if set(payload)!={"custom_fields"}: raise AssertionError("content PATCH is forbidden")
                document["custom_fields"]=payload["custom_fields"]
        client=Client(); native=NativeIngestor(client,client,ids)
        self.assertFalse(native.verify_indexed_ocr(9,"different OCR"))
        self.assertTrue(native.verify_indexed_ocr(9,"OCR words"))
        native.mark_ocr_indexed(9,"complete")
        self.assertEqual(len(client.patches),1)
        values=custom_fields(document)
        self.assertTrue(json.loads(values[ids["lib_provenance"]])["ocr_indexed"])
        self.assertEqual(values[ids["lib_completeness"]],"complete")
        self.assertEqual(document["content"].count("── OCR ──"),1)
        with self.assertRaises(LibraryError): native.attach_ocr(9,"","new OCR",[])
        self.assertEqual(len(client.patches),1)

    def test_route_validation_and_openapi_semantics(self):
        class Search:
            def search(self, query, limit): return {"limit":limit}
            def evidence(self, doc, query): return {"id":doc}
        service = LibraryService(self.ledger, None, Search(), None, {})
        handler = handler_factory(service, "synthetic-token")
        def dispatch(method, path, body=b"{}"):
            obj = object.__new__(handler); obj.command, obj.path, obj.rfile = method, path, io.BytesIO(body)
            obj.headers = {"Authorization":"Bearer synthetic-token", "Content-Length":str(len(body))}
            return obj._dispatch()
        for path in ("/v1/batches/a/extra", "/v1/items/a/resume/extra", "/v1/documents/1/evidence/extra"):
            with self.assertRaises(LibraryError) as caught: dispatch("GET", path)
            self.assertEqual(caught.exception.status, 404)
        for path in ("/v1/documents/nope/evidence", "/v1/documents/0/evidence"):
            with self.assertRaises(LibraryError) as caught: dispatch("GET", path)
            self.assertEqual(caught.exception.status, 400)
        for limit in ("bad", True, 0, 9):
            with self.assertRaises(LibraryError) as caught: dispatch("POST", "/v1/search", json.dumps({"query":"x","limit":limit}).encode())
            self.assertEqual(caught.exception.status, 400)
        api = json.loads((Path(__file__).parents[1] / "contracts/openapi.json").read_text())
        self.assertEqual(api["info"]["version"], VERSION)
        self.assertEqual(api["security"], [{"bearerAuth":[]}])
        self.assertEqual(api["components"]["securitySchemes"]["bearerAuth"]["scheme"], "bearer")
        self.assertEqual(api["components"]["schemas"]["Counts"]["properties"]["blocked"]["type"], "integer")
        self.assertEqual(api["components"]["parameters"]["DocumentId"]["schema"]["type"], "integer")
        self.assertEqual(api["components"]["schemas"]["SearchRequest"]["properties"]["limit"]["maximum"], 8)
        self.assertIn("401", api["paths"]["/v1/search"]["post"]["responses"])
        self.assertEqual(api["paths"]["/health"]["get"]["security"], [])
        self.assertIn("Confirmed URL items",api["components"]["schemas"]["ResumeItem"]["description"])
        self.assertIn("metadata only",api["paths"]["/v1/items/{id}/resume"]["post"]["description"])
        for path, operations in api["paths"].items():
            for operation in operations.values():
                self.assertIn("200", operation["responses"])
                self.assertEqual(operation["responses"]["200"]["$ref"].split("/")[-1] in api["components"]["responses"], True)
                if path != "/health":
                    self.assertIn("401", operation["responses"])
                if "{id}" in path:
                    parameter = operation["parameters"][0]["$ref"].split("/")[-1]
                    self.assertEqual(api["components"]["parameters"][parameter]["required"], True)
            if "{id}" in path:
                self.assertTrue(all("parameters" in operation for operation in operations.values()))
        for name in ("SubmitBatch", "ResumeItem", "SearchRequest", "StateRequest"):
            self.assertEqual(api["components"]["schemas"][name]["additionalProperties"], False)
        names = {tool.name:tool.inputSchema for tool in asyncio.run(create_server(type('API',(),{'call':lambda *a: {}})()).list_tools())}
        self.assertTrue({"canonical_url","publish_date","author","fetched_at","provenance","ocr","file_path"}.issubset(names["resume_item"]["properties"]))
        self.assertIn("source_label", names["submit_batch"]["properties"])
        self.assertEqual(set(api["components"]["schemas"]["ResumeItem"]["properties"]) - {"text","title","file_path","ocr"},
                         {"canonical_url","publish_date","author","fetched_at","provenance"})


if __name__ == "__main__": unittest.main()
