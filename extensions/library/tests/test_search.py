"""Synthetic query revision 2 regressions; no private corpus material."""
from __future__ import annotations

import hashlib
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from extensions.library.model import LIB_FIELDS, LibraryError
from extensions.library.native import NativeClient
from extensions.library.search import LibrarySearch, evidence_for, variants


IDS = {name: number for number, name in enumerate(LIB_FIELDS, 1)}
LEGACY = {"wx_source_url": 90, "wx_publish_date": 91, "wx_enrichment": 92}


def document(ident, title, content, fields=None):
    return {"id": ident, "title": title, "content": content,
            "custom_fields": [{"field": field, "value": value} for field, value in (fields or {}).items()]}


class Reader:
    def __init__(self, documents):
        self.docs = {row["id"]: row for row in documents}
        self.terms, self.reads = [], []

    def search(self, term):
        self.terms.append(term)
        return [{"id": row["id"], "title": row["title"]} for row in self.docs.values()
                if term.lower() in (row["title"] + " " + row["content"]).lower()]

    def document(self, ident):
        self.reads.append(ident)
        return self.docs[ident]


class BoundedNative(NativeClient):
    def __init__(self, documents, denied=False):
        super().__init__("http://127.0.0.1:4386", "synthetic")
        self.docs = {row["id"]: row for row in documents}
        self.calls, self.denied = [], denied

    def pages(self, path):
        raise AssertionError("unbounded pagination was called")

    def request(self, method, path, payload=None, content_type="application/json"):
        self.calls.append((method, path))
        if self.denied:
            raise LibraryError("native permission denied", "native_permission", 403)
        if path.startswith("/api/documents/?"):
            params = parse_qs(urlsplit(path).query)
            term = params["search"][0].lower()
            rows = [{"id": row["id"], "title": row["title"]} for row in self.docs.values()
                    if term in (row["title"] + " " + row["content"]).lower()]
            return {"results": rows[:int(params["page_size"][0])], "next": "/api/documents/?page=999"}
        return self.docs[int(path.rstrip("/").split("/")[-1])]


class SearchRevisionTests(unittest.TestCase):
    def test_precise_phrase_alias_and_cross_script_budget(self):
        used = variants("context compression memory MCP 記憶問題")
        self.assertLessEqual(len(used), 8)
        self.assertEqual(used[0], "context compression")
        self.assertIn("Model Context Protocol", used)
        self.assertIn("記憶", used)
        self.assertIn("记忆", used)
        used = variants("alpha beta gamma delta epsilon zeta 記憶問題")
        self.assertIn("記憶", used)
        self.assertIn("记忆", used)
        self.assertLessEqual(len(used), 8)
        self.assertIn("context compression", variants("上下文壓縮"))
        self.assertIn("MCP", variants("Model Context Protocol"))

    def test_precise_raw_fullphrase_and_title_beat_generic_low_id(self):
        rows = [
            document(1, "Synthetic general question", "layout navigation notes"),
            document(2, "OPC layout guide", "The OPC layout uses a precise OPC layout grid."),
            document(3, "OPC overview", "OPC appears once."),
        ]
        result = LibrarySearch(Reader(rows), IDS).search("OPC layout 的做法")
        self.assertEqual(result["matches"][0]["id"], 2)
        self.assertLessEqual(len(result["matches"]), 8)
        self.assertGreater(result["matches"][0]["score"], result["matches"][-1]["score"])

    def test_chinese_precise_term_survives_generic_low_id_candidates(self):
        generic = [document(number, "一般背景", "一般背景整理操作說明。") for number in range(1, 51)]
        precise = document(999, "稀有佈局協定", "稀有佈局協定規定特殊排列方式。")
        result = LibrarySearch(Reader(generic + [precise]), IDS).search("一般背景整理操作說明 稀有佈局協定")
        self.assertFalse(result["no_match"])
        self.assertEqual(result["matches"][0]["id"], 999)

    def test_alias_evidence_requires_raw_source(self):
        rows = [document(1, "Context compression", "unrelated prose"),
                document(2, "Context compression", "The context compression preserves a memory summary."),
                document(3, "Unrelated", "上下文压缩保留关键摘要。")]
        result = LibrarySearch(Reader(rows), IDS).search("context compression")
        self.assertEqual(result["matches"][0]["id"], 2)
        self.assertNotIn(1, [row["id"] for row in result["matches"]])
        self.assertIn(3, [row["id"] for row in result["matches"]])
        reverse = LibrarySearch(Reader([rows[1]]), IDS).search("上下文壓縮")
        self.assertEqual(reverse["matches"][0]["id"], 2)

    def test_cross_article_preserves_each_raw_anchor(self):
        rows = [document(3, "OPC", "OPC handles a graph."),
                document(8, "Layout", "Layout arranges the graph.")]
        result = LibrarySearch(Reader(rows), IDS).search("OPC layout")
        self.assertEqual({row["id"] for row in result["matches"]}, {3, 8})
        crowded = [document(number, "Layout", "Layout layout layout.") for number in range(30, 50)]
        result = LibrarySearch(Reader(crowded + [rows[0]]), IDS).search("OPC layout")
        self.assertIn(3, [row["id"] for row in result["matches"]])
        self.assertTrue(any(row["id"] != 3 for row in result["matches"]))

    def test_distinct_english_concepts_cannot_be_exempted_by_alias_table(self):
        layout = document(21, "Layout notes", "A layout describes document organization.")
        search = LibrarySearch(Reader([layout]), IDS)
        self.assertTrue(search.search("codex layout")["no_match"])
        partial = search.evidence(21, "codex layout")
        self.assertFalse(partial["no_match"])
        self.assertIn("layout", partial["hits"][0]["excerpt"])
        compression = document(22, "Compression notes", "Context compression shortens a prompt.")
        self.assertTrue(LibrarySearch(Reader([compression]), IDS).search("MCP context compression")["no_match"])
        analysis_only = document(23, "Layout notes", "layout source\n── 分析資料（非原文；未經事實查核）──\ncodex")
        self.assertTrue(LibrarySearch(Reader([analysis_only]), IDS).search("codex layout")["no_match"])
        self.assertTrue(LibrarySearch(Reader([compression]), IDS).evidence(22, "unknownwidget 記憶問題")["no_match"])

    def test_historical_title_concepts_need_raw_chinese_evidence(self):
        raw = ("── SOURCE ──\n"
               "該方案曾提供一年免費試用，報名在當季截止。\n"
               "── END SOURCE ──\n"
               "── 分析資料（非原文；未經事實查核）──\n"
               "Synthetic analysis says the offer is still active.")
        fields = {IDS["lib_publish_date"]: "2025-04-03"}
        row = document(37, "Aurora Z7 Plus PixelForge archive", raw, fields)
        query = "Aurora Z7 Plus PixelForge 一年免費方案在今天仍然有效嗎"
        search = LibrarySearch(Reader([row]), IDS)
        result = search.search(query)
        self.assertFalse(result["no_match"])
        self.assertEqual(result["matches"][0]["published_at"], "2025-04-03")
        response = search.evidence(37, query)
        self.assertFalse(response["no_match"])
        self.assertEqual(response["hits"][0]["section"], "source")
        self.assertEqual(response["hits"][0]["published_at"], "2025-04-03")
        for hit in response["hits"]:
            self.assertEqual(raw[hit["start"]:hit["end"]], hit["excerpt"])
            self.assertEqual(hit["excerpt_hash"], hashlib.sha256(hit["excerpt"].encode()).hexdigest())
            self.assertNotIn("still active", hit["excerpt"])
        title_only = document(38, row["title"], "Unrelated archived paragraph.", fields)
        self.assertTrue(LibrarySearch(Reader([title_only]), IDS).search(query)["no_match"])
        self.assertTrue(LibrarySearch(Reader([title_only]), IDS).evidence(38, query)["no_match"])
        generic_time = document(39, row["title"], "今天只是一般時間說明。", fields)
        self.assertTrue(LibrarySearch(Reader([generic_time]), IDS).search(query)["no_match"])
        self.assertTrue(LibrarySearch(Reader([generic_time]), IDS).evidence(39, query)["no_match"])

    def test_two_noanswer_classes(self):
        reader = Reader([document(1, "Generic", "Notes about document layout and 記憶.")])
        search = LibrarySearch(reader, IDS)
        self.assertTrue(search.search("unfindablepropername")["no_match"])
        self.assertTrue(search.search("unfindablepropername 記憶問題")["no_match"])
        self.assertTrue(search.search("unfindablepropername memory 記憶問題")["no_match"])
        self.assertTrue(search.search("What is my API token?")["no_match"])
        self.assertEqual(reader.reads, [1, 1])
        self.assertTrue(search.evidence(1, "我的密碼是什麼")["no_match"])
        self.assertTrue(search.evidence(1, "unfindablepropername 記憶問題")["no_match"])

    def test_native_single_page_permission_and_bounded_reads(self):
        rows = [document(number, "Memory guide", "memory source") for number in range(1, 201)]
        reader = BoundedNative(rows)
        result = LibrarySearch(reader, IDS).search("memory")
        list_calls = [path for _, path in reader.calls if path.startswith("/api/documents/?")]
        doc_calls = [path for _, path in reader.calls if path.startswith("/api/documents/") and "?" not in path]
        self.assertLessEqual(len(list_calls), 8)
        self.assertTrue(all(parse_qs(urlsplit(path).query).get("page_size") == ["24"] for path in list_calls))
        self.assertLessEqual(len(doc_calls), 32)
        self.assertLessEqual(len(result["matches"]), 8)
        with self.assertRaises(LibraryError) as denied:
            LibrarySearch(BoundedNative(rows, denied=True), IDS).search("memory")
        self.assertEqual(denied.exception.code, "native_permission")

    def test_per_variant_candidates_and_total_detail_reads(self):
        rows = [document(group * 100 + number, f"Synthetic {term} {number}", f"{term} source")
                for group, term in enumerate(("alpha", "beta", "gamma", "delta"), 1)
                for number in range(1, 31)]
        reader = BoundedNative(rows)
        LibrarySearch(reader, IDS).search("alpha beta gamma delta")
        doc_calls = [path for _, path in reader.calls if path.startswith("/api/documents/") and "?" not in path]
        self.assertEqual(len(doc_calls), 32)
        self.assertLessEqual(len(reader.calls) - len(doc_calls), 8)

    def test_raw_sections_offsets_hash_and_analysis_exclusion(self):
        raw = ("── SOURCE ──\nEarly memory.\n── END SOURCE ──\n"
               "── OCR ──\nLate memory from image.\n── END OCR ──\n"
               "── 分析資料（非原文；未經事實查核）──\nInvented memory")
        row = document(7, "Synthetic", raw)
        hits = LibrarySearch(Reader([row]), IDS).evidence(7, "memory")["hits"]
        self.assertEqual({hit["section"] for hit in hits}, {"source", "ocr"})
        for hit in hits:
            self.assertEqual(raw[hit["start"]:hit["end"]], hit["excerpt"])
            self.assertEqual(hit["excerpt_hash"], hashlib.sha256(hit["excerpt"].encode()).hexdigest())
            self.assertNotIn("Invented", hit["excerpt"])
            self.assertNotIn("── SOURCE ──", hit["excerpt"])
            self.assertNotIn("── OCR ──", hit["excerpt"])
            self.assertLessEqual(len(hit["excerpt"]), 1200)
        legacy = document(9, "Legacy", "prefix memory suffix\n── 分析資料（非原文；未經事實查核）──\nignored")
        old = evidence_for(legacy, IDS, "memory")[0]
        self.assertEqual(old["section"], "source")
        self.assertEqual(legacy["content"][old["start"]:old["end"]], old["excerpt"])

    def test_near_end_and_per_document_caps(self):
        body = "filler " * 2400 + "nearendmarker " + (" memory " + "filler " * 170) * 20
        raw = "── SOURCE ──\n" + body + "\n── END SOURCE ──"
        hits = LibrarySearch(Reader([document(4, "Synthetic", raw)]), IDS).evidence(4, "nearendmarker memory")["hits"]
        self.assertTrue(hits)
        self.assertGreater(hits[0]["start"], 12000)
        self.assertLessEqual(len(hits), 8)
        self.assertLessEqual(sum(len(hit["excerpt"]) for hit in hits), 12000)
        self.assertTrue(all(len(hit["excerpt"]) <= 1200 for hit in hits))

    def test_entire_evidence_json_budget_including_repeated_metadata(self):
        body = ("memory " + "filler " * 170) * 8
        long_title = "Synthetic technical notes " * 15
        long_url = "https://example.invalid/" + "reference-" * 75
        row = document(44, long_title, body, {IDS["lib_canonical_url"]: long_url})
        query = "memory " + "請說明" * 20
        response = LibrarySearch(Reader([row]), IDS).evidence(44, query)
        self.assertFalse(response["no_match"])
        self.assertGreaterEqual(len(response["hits"]), 1)
        self.assertLess(len(response["hits"]), 8)
        self.assertLessEqual(len(json.dumps(response, ensure_ascii=False)), 12000)
        self.assertLessEqual(len(json.dumps(response, ensure_ascii=True)), 12000)
        for hit in response["hits"]:
            self.assertEqual(row["content"][hit["start"]:hit["end"]], hit["excerpt"])
            self.assertEqual(hit["excerpt_hash"], hashlib.sha256(hit["excerpt"].encode()).hexdigest())
        compact = document(45, "Short", body)
        self.assertEqual(len(LibrarySearch(Reader([compact]), IDS).evidence(45, "memory")["hits"]), 8)

    def test_oversized_query_and_single_hit_metadata_fail_closed(self):
        reader = Reader([document(46, "Short", "memory source")])
        search = LibrarySearch(reader, IDS)
        with self.assertRaises(LibraryError):
            search.search("m" * 513)
        with self.assertRaises(LibraryError):
            search.evidence(46, "m" * 513)
        self.assertEqual(reader.terms, [])
        self.assertEqual(reader.reads, [])
        oversized = document(47, "Synthetic " * 2000, "memory source")
        search = LibrarySearch(Reader([oversized]), IDS)
        response = search.evidence(47, "memory")
        self.assertTrue(response["no_match"])
        self.assertEqual(response["hits"], [])
        self.assertLessEqual(len(json.dumps(response, ensure_ascii=True)), 12000)
        self.assertTrue(search.search("memory")["no_match"])

    def test_citation_stripping_new_and_legacy_and_unsafe_fail_closed(self):
        tracked = ("https://example.invalid/article?__biz=synthetic&mid=42&idx=1&sn=stable"
                   "&chksm=tracking&mpshare=tracking&srcid=tracking&sharer_shareinfo=tracking"
                   "&utm_source=tracking&token=private#fragment")
        expected = "https://example.invalid/article?__biz=synthetic&mid=42&idx=1&sn=stable"
        for field in (IDS["lib_canonical_url"], LEGACY["wx_source_url"]):
            row = document(1, "Synthetic", "memory source", {field: tracked})
            search = LibrarySearch(Reader([row]), IDS, LEGACY)
            self.assertEqual(search.search("memory")["matches"][0]["canonical_url"], expected)
            self.assertEqual(search.evidence(1, "memory")["hits"][0]["canonical_url"], expected)
        legacy = document(5, "Legacy", "memory source",
                          {LEGACY["wx_source_url"]: tracked, LEGACY["wx_publish_date"]: "2026-01-02",
                           LEGACY["wx_enrichment"]: '{"author":"Synthetic author"}'})
        hit = LibrarySearch(Reader([legacy]), IDS, LEGACY).evidence(5, "memory")["hits"][0]
        self.assertEqual(hit["author"], "Synthetic author")
        self.assertEqual(hit["published_at"], "2026-01-02")
        self.assertEqual(hit["completeness"], "unknown")
        unsafe = document(2, "Synthetic", "memory source",
                          {LEGACY["wx_source_url"]: "https://user:private@example.invalid/article"})
        self.assertIsNone(LibrarySearch(Reader([unsafe]), IDS, LEGACY).evidence(2, "memory")["hits"][0]["canonical_url"])
        malformed = document(3, "Synthetic", "memory source", {LEGACY["wx_source_url"]: "http://[invalid"})
        self.assertIsNone(LibrarySearch(Reader([malformed]), IDS, LEGACY).evidence(3, "memory")["hits"][0]["canonical_url"])


if __name__ == "__main__":
    unittest.main()
