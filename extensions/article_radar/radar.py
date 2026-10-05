"""Article Radar's source mapping and native Paperless API contract."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from urllib.parse import urlsplit

VERSION = "0.1.0"
FIELDS = {
    "wx_source_url": "longtext", "wx_source_id": "string", "wx_category": "string",
    "wx_summary": "longtext", "wx_key_points": "longtext",
    "wx_actionable": "string", "wx_actionable_note": "longtext",
    "wx_hype": "string", "wx_hype_note": "longtext",
    "wx_shelf_life": "string", "wx_publish_date": "date",
    "wx_share_date": "date", "wx_content_state": "string",
    "wx_reading_state": "string", "wx_enrichment": "longtext",
}


def validate_upstream(value: str) -> str:
    if not re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", value):
        raise ValueError("upstream must be a fixed http://127.0.0.1:<port> URL")
    if urlsplit(value).port > 65535:
        raise ValueError("upstream port is invalid")
    return value


def source_id(item: dict) -> str:
    value = item.get("id")
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value).strip()
    return "url-sha256:" + hashlib.sha256(item["url"].encode("utf-8")).hexdigest()


def raw_by_url(lines) -> dict[str, str]:
    result = {}
    for line in lines:
        row = json.loads(line)
        body = row.get("text")
        if not isinstance(body, str) or not body.strip():
            continue
        for key in ("url", "final_url"):
            url = row.get(key)
            if isinstance(url, str) and url.strip():
                url = url.strip()
                if len(body.strip()) > len(result.get(url, "").strip()):
                    result[url] = body
    return result


def _is_article(item: dict, raw: dict[str, str]) -> bool:
    url = item.get("url")
    parsed = urlsplit(url.strip()) if isinstance(url, str) else None
    return (isinstance(url, str) and bool(url.strip()) and
            parsed.scheme in {"http", "https"} and bool(parsed.netloc) and
            isinstance(item.get("summary"), str) and bool(item["summary"].strip()) and
            not item.get("nonarticle_kind") and bool(raw.get(url.strip(), "").strip()))


def select_articles(items: list[dict], raw: dict[str, str], limit: int) -> tuple[list[dict], int]:
    """Deterministically cover categories, shelf lives and action ratings."""
    if limit < 0:
        raise ValueError("limit must be nonnegative")
    eligible = [i for i in items if _is_article(i, raw)]
    skipped = len(items) - len(eligible)
    selected, seen_ids, seen_urls = [], set(), set()
    categories, shelves, actions = set(), set(), set()
    while len(selected) < limit:
        candidates = [i for i in eligible if source_id(i) not in seen_ids and i["url"].strip() not in seen_urls]
        if not candidates:
            break
        def rank(item):
            category = str(item.get("category") or "未分類")
            shelf = str(item.get("shelf_life") or "unknown")
            action = str(item.get("actionable") or "unknown")
            score = (100 * (category not in categories) + 20 * (shelf not in shelves) +
                     10 * (action not in actions) + 3 * (action == "high"))
            return (-score, category, shelf, item["url"].strip(), source_id(item))
        item = min(candidates, key=rank)
        selected.append(item)
        seen_ids.add(source_id(item))
        seen_urls.add(item["url"].strip())
        categories.add(str(item.get("category") or "未分類"))
        shelves.add(str(item.get("shelf_life") or "unknown"))
        actions.add(str(item.get("actionable") or "unknown"))
    return selected, skipped


def iso_date(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def field_values(item: dict) -> dict[str, str]:
    values = {
        "wx_source_url": item["url"].strip(),
        "wx_source_id": source_id(item),
        "wx_category": str(item.get("category") or ""),
        "wx_summary": item["summary"].strip(),
        "wx_key_points": json.dumps(item.get("key_points") or [], ensure_ascii=False),
        "wx_actionable": str(item.get("actionable") or ""),
        "wx_actionable_note": str(item.get("actionable_note") or ""),
        "wx_hype": str(item.get("hype") or ""),
        "wx_hype_note": str(item.get("hype_note") or ""),
        "wx_shelf_life": str(item.get("shelf_life") or ""),
        "wx_content_state": str(item.get("content_state") or ""),
        "wx_reading_state": (str(item.get("user_state") or "")
                             if not isinstance(item.get("user_state"), dict)
                             else str(item["user_state"].get("reading_state") or "")),
        "wx_enrichment": json.dumps({k: v for k, v in item.items() if k != "user_state"},
                                    ensure_ascii=False, sort_keys=True),
    }
    for name, key in (("wx_publish_date", "publish_date"), ("wx_share_date", "share_date")):
        if value := iso_date(item.get(key)):
            values[name] = value
    return values


def archive_text(item: dict, raw_text: str) -> str:
    if not raw_text.strip():
        raise ValueError("raw article body is empty")
    return ("原始文章內容（來源擷取文字）\n" + raw_text +
            "\n\n── 分析資料（非原文；未經事實查核）──\n" +
            "摘要：" + item["summary"].strip() + "\n" +
            "重點：" + json.dumps(item.get("key_points") or [], ensure_ascii=False) + "\n")


def normalize_page(data) -> tuple[list[dict], str | None]:
    if isinstance(data, list):
        return data, None
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("invalid paginated API response")
    return data["results"], data.get("next")


def custom_map(document: dict) -> dict[int, object]:
    result = {}
    for entry in document.get("custom_fields") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("field"), int):
            raise ValueError("invalid document custom fields")
        result[entry["field"]] = entry.get("value")
    return result


def existing_keys(documents: list[dict], field_ids: dict[str, int]) -> tuple[set[str], set[str]]:
    ids, urls = set(), set()
    for doc in documents:
        values = custom_map(doc)
        if value := values.get(field_ids["wx_source_id"]):
            ids.add(str(value))
        if value := values.get(field_ids["wx_source_url"]):
            urls.add(str(value))
    return ids, urls


def verify_document(doc: dict, item: dict, body: str, values: dict,
                    field_ids: dict[str, int], correspondent_id: int | None,
                    document_type_id: int, tag_ids: set[int]) -> None:
    expected = {field_ids[k]: v for k, v in values.items()}
    actual = custom_map(doc)
    for field_id, value in expected.items():
        if actual.get(field_id) != value:
            raise ValueError("document metadata readback mismatch")
    content = doc.get("content") or ""
    # Native TXT extraction can normalize whitespace but must retain the real body.
    if re.sub(r"\s+", " ", body.strip()) not in re.sub(r"\s+", " ", content):
        raise ValueError("document content readback mismatch")
    if (doc.get("title") != item.get("title") or
            doc.get("correspondent") != correspondent_id or
            doc.get("document_type") != document_type_id or
            not tag_ids.issubset(set(doc.get("tags") or []))):
        raise ValueError("document native metadata readback mismatch")
    if values.get("wx_publish_date") and str(doc.get("created", ""))[:10] != values["wx_publish_date"]:
        raise ValueError("document created date readback mismatch")
