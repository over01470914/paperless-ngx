"""Synthetic-only contract tests. No source-library contents or live API calls."""

import email.message
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migrate
import radar
import server

URL1 = "https://example.invalid/synthetic-1"
URL2 = "https://example.invalid/synthetic-2"
LONG_URL = "https://example.invalid/article?synthetic=" + "x" * 240


def item(url=URL1, **overrides):
    value = {"id": "synthetic-id", "url": url, "title": "Synthetic title",
             "summary": "Synthetic summary", "category": "synthetic-topic",
             "account": "Synthetic account", "tags": ["synthetic-tag"],
             "actionable": "high", "shelf_life": "fast", "publish_date": "2026-01-02",
             "key_points": ["Synthetic point"], "user_state": "unread"}
    value.update(overrides)
    return value


class MappingSelectionTests(unittest.TestCase):
    def test_raw_url_mapping_keeps_best_nonempty_and_final_url(self):
        rows = [{"url": URL1, "text": ""}, {"url": URL1, "text": "short"},
                {"url": URL1, "final_url": URL2, "text": "longer synthetic body"}]
        raw = radar.raw_by_url(json.dumps(row) for row in rows)
        self.assertEqual(raw[URL1], "longer synthetic body")
        self.assertEqual(raw[URL2], "longer synthetic body")

    def test_selection_skips_nonarticles_and_spreads_categories(self):
        rows = [item(id="a", category="A"), item(URL2, id="b", category="B"),
                item("https://example.invalid/3", id="c", category="A"),
                item("https://example.invalid/4", id="d", nonarticle_kind="video")]
        raw = {row["url"]: "synthetic raw body" for row in rows}
        selected, skipped = radar.select_articles(rows, raw, 2)
        self.assertEqual(skipped, 1)
        self.assertEqual({x["category"] for x in selected}, {"A", "B"})

    def test_selection_covers_shelf_lives_and_action_ratings(self):
        rows = [item(f"https://example.invalid/{n}", id=str(n), category="A",
                     shelf_life=shelf, actionable=action)
                for n, (shelf, action) in enumerate((("fast", "high"), ("medium", "medium"),
                                                      ("evergreen", "low")))]
        selected, _ = radar.select_articles(rows, {row["url"]: "synthetic body" for row in rows}, 3)
        self.assertEqual({i["shelf_life"] for i in selected}, {"fast", "medium", "evergreen"})
        self.assertEqual({i["actionable"] for i in selected}, {"high", "medium", "low"})

    def test_provenance_excludes_user_state_and_archive_separates_analysis(self):
        source = item()
        fields = radar.field_values(source)
        self.assertEqual(fields["wx_reading_state"], "unread")
        self.assertNotIn("user_state", json.loads(fields["wx_enrichment"]))
        body = radar.archive_text(source, "Synthetic raw body")
        self.assertIn("Synthetic raw body", body)
        self.assertIn("分析資料（非原文", body)


class NativeContractTests(unittest.TestCase):
    def setUp(self):
        self.fields = {name: n for n, name in enumerate(radar.FIELDS, 1)}
        self.source = item()
        self.values = radar.field_values(self.source)
        self.document = {"id": 7, "title": self.source["title"],
                         "content": radar.archive_text(self.source, "Synthetic raw body"),
                         "correspondent": 3, "document_type": 4, "tags": [5, 6],
                         "created": "2026-01-02",
                         "custom_fields": [{"field": self.fields[name], "value": value}
                                           for name, value in self.values.items()]}

    def test_page_normalization_and_full_pagination(self):
        self.assertEqual(radar.normalize_page({"results": [{"id": 1}], "next": None})[0], [{"id": 1}])
        api = migrate.PaperlessAPI("http://127.0.0.1:4386", "synthetic-token")
        pages = {"/api/documents/": {"results": [{"id": 1}], "next": "http://127.0.0.1:4386/api/documents/?page=2"},
                 "/api/documents/?page=2": {"results": [{"id": 2}], "next": None}}
        api.request = lambda method, path, *args: pages[path]
        self.assertEqual([d["id"] for d in api.pages("/api/documents/")], [1, 2])
        pages["/api/documents/?page=2"]["next"] = "http://evil.invalid/api/documents/"
        with self.assertRaisesRegex(ValueError, "escaped"):
            api.pages("/api/documents/")

    def test_exact_keys_dedupe_across_pages_not_title(self):
        other = dict(self.document, title="Different synthetic title")
        ids, urls = radar.existing_keys([other], self.fields)
        self.assertIn("synthetic-id", ids)
        self.assertIn(URL1, urls)

    def test_multipart_custom_fields_and_native_metadata(self):
        payload, content_type = migrate.multipart(self.source, self.document["content"],
                                                    self.values, self.fields, 3, 4, {5, 6})
        self.assertIn(b'name="custom_fields"', payload)
        self.assertIn(json.dumps({str(self.fields["wx_source_id"]): "synthetic-id"})[1:-1].encode(), payload)
        self.assertIn(b'name="tags"', payload)
        self.assertIn(b'name="created"', payload)
        self.assertIn("boundary=", content_type)
        radar.verify_document(self.document, self.source, "Synthetic raw body", self.values,
                              self.fields, 3, 4, {5, 6})
        self.document["custom_fields"][0]["value"] = "https://example.invalid/wrong"
        with self.assertRaisesRegex(ValueError, "readback mismatch"):
            radar.verify_document(self.document, self.source, "Synthetic raw body", self.values,
                                  self.fields, 3, 4, {5, 6})

    def test_pending_intent_blocks_without_api_call(self):
        class FailAPI:
            def request(self, *args): raise AssertionError("must not call API")
        receipt = {"entries": {"synthetic-id": {"state": "intent", "url": URL1}}}
        with self.assertRaisesRegex(ValueError, "uncertain"):
            migrate.resume_pending(FailAPI(), receipt, Path("unused"), {}, {}, {}, {}, 4, {})

    def test_receipt_persists_state_atomically_and_rejects_repo_path(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "synthetic-receipt.json"
            with self.assertRaisesRegex(ValueError, "outside repository"):
                migrate.validate_receipt_path(path, Path(directory))
            data = {"version": radar.VERSION, "upstream": "http://127.0.0.1:4386",
                    "entries": {"synthetic-id": {"state": "intent", "url": URL1}}}
            migrate.save_receipt(path, data)
            self.assertEqual(migrate.load_receipt(path, data["upstream"]), data)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_success_task_needs_document_id_and_failure_is_terminal(self):
        class FakeAPI:
            def __init__(self, row): self.row = row
            def pages(self, path): return [self.row]
        task = {"task_id": "synthetic-uuid", "status": "success", "result_data": {"document_id": 7}}
        self.assertEqual(migrate.task_document_id(FakeAPI(task), "synthetic-uuid"), 7)
        task["result_data"] = {}
        with self.assertRaisesRegex(ValueError, "document_id"):
            migrate.task_document_id(FakeAPI(task), "synthetic-uuid")
        task["status"] = "failure"
        with self.assertRaisesRegex(ValueError, "failure"):
            migrate.task_document_id(FakeAPI(task), "synthetic-uuid")
        task["result_data"] = {"duplicate_of": 99}
        with self.assertRaisesRegex(ValueError, "duplicate content"):
            migrate.task_document_id(FakeAPI(task), "synthetic-uuid")
        task["status"] = "revoked"
        task["result_data"] = {}
        with self.assertRaisesRegex(ValueError, "revoked"):
            migrate.task_document_id(FakeAPI(task), "synthetic-uuid")

    def test_existing_custom_field_type_mismatch_fails(self):
        class FakeAPI:
            def pages(self, path): return [{"id": 1, "name": "wx_source_url", "data_type": "url"}]
            def request(self, *args): raise AssertionError("must not overwrite")
        with self.assertRaisesRegex(ValueError, "type mismatch"):
            migrate.get_or_create(FakeAPI(), "/api/custom_fields/", "wx_source_url", radar.FIELDS["wx_source_url"])

    def test_source_url_field_is_created_as_longtext(self):
        self.assertEqual(radar.FIELDS["wx_source_url"], "longtext")
        class FakeAPI:
            def __init__(self): self.payloads = []
            def pages(self, path): return []
            def request(self, method, path, payload):
                self.payloads.append(payload)
                return {"id": 8, **payload}
        api = FakeAPI()
        self.assertEqual(migrate.get_or_create(api, "/api/custom_fields/", "wx_source_url",
                                               radar.FIELDS["wx_source_url"]), 8)
        self.assertEqual(api.payloads, [{"name": "wx_source_url", "data_type": "longtext"}])

    def test_long_source_url_is_exact_through_mapping_upload_dedupe_and_readback(self):
        self.assertGreater(len(LONG_URL), 200)
        source = item(LONG_URL)
        raw = radar.raw_by_url([json.dumps({"url": LONG_URL, "text": "Synthetic raw body"})])
        self.assertEqual(raw[LONG_URL], "Synthetic raw body")
        values = radar.field_values(source)
        self.assertEqual(values["wx_source_url"], LONG_URL)
        payload, _ = migrate.multipart(source, radar.archive_text(source, raw[LONG_URL]), values,
                                       self.fields, 3, 4, {5, 6})
        self.assertIn(LONG_URL.encode("utf-8"), payload)
        document = dict(self.document)
        document["custom_fields"] = [{"field": self.fields[name], "value": value}
                                     for name, value in values.items()]
        document["content"] = radar.archive_text(source, raw[LONG_URL])
        ids, urls = radar.existing_keys([document], self.fields)
        self.assertIn(LONG_URL, urls)
        self.assertIn("synthetic-id", ids)
        radar.verify_document(document, source, raw[LONG_URL], values, self.fields, 3, 4, {5, 6})
        document["custom_fields"][0]["value"] = LONG_URL[:200]
        with self.assertRaisesRegex(ValueError, "readback mismatch"):
            radar.verify_document(document, source, raw[LONG_URL], values, self.fields, 3, 4, {5, 6})


class ProxyTests(unittest.TestCase):
    def test_fixed_upstream_and_traversal(self):
        self.assertEqual(radar.validate_upstream("http://127.0.0.1:4386"), "http://127.0.0.1:4386")
        for value in ("http://example.invalid:4386", "http://127.0.0.1:4386/path", "https://127.0.0.1:4386"):
            with self.assertRaises(ValueError): radar.validate_upstream(value)
        for path in ("/radar/%2e%2e/api/", "/radar/%252e%252e/api/", "http://evil.invalid/api/", "//evil.invalid/api/"):
            with self.assertRaises(ValueError): server.safe_target(path)

    def test_hop_headers_and_fixed_static_allowlist(self):
        headers = email.message.Message()
        for key, value in [("Connection", "X-Private"), ("X-Private", "no"),
                           ("X-Forwarded-Host", "evil.invalid"), ("Cookie", "synthetic=session")]:
            headers.add_header(key, value)
        self.assertEqual(server.filtered_headers(headers, request=True), [("Cookie", "synthetic=session")])
        self.assertNotIn("/radar/../secret", server.FILES)
        self.assertEqual(set(server.FILES), {"/radar/", "/radar/index.html", "/radar/app.js", "/radar/style.css"})

    def test_body_limit_and_origin_rewrite(self):
        headers = email.message.Message()
        headers.add_header("Content-Length", str(server.MAX_REQUEST + 1))
        with self.assertRaises(OverflowError): server.request_length(headers, "POST")
        headers.replace_header("Content-Length", "1")
        self.assertEqual(server.request_length(headers, "POST"), 1)
        self.assertEqual(server.rewrite_origin("http://radar.invalid:4387/radar/", "radar.invalid:4387",
                                               "http://127.0.0.1:4386"), "http://127.0.0.1:4386/radar/")
        self.assertEqual(server.rewrite_origin("http://evil.invalid/", "radar.invalid:4387",
                                               "http://127.0.0.1:4386"), "http://evil.invalid/")


if __name__ == "__main__":
    unittest.main()
