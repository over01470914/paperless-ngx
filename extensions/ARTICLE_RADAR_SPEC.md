# Article Radar v0.1.0 — specification revision 2

Baseline: Paperless-ngx v3.2.1, commit 7575d6078227ebdb4cf443f263d53ebc7575aa37.
Fork: over01470914/paperless-ngx; upstream branches main/dev remain unmodified. Extensions live only on naya/article-intelligence.

## Goal
Keep native Paperless intact. Add an independently removable Article Radar companion powered by Paperless's existing authenticated API: topic map, reading queue, summaries, actionable value, marketing/hype assessment, freshness flags and source links. No duplicate article database or fabricated article content.

## Architecture
- Native Paperless at port 4386, PostgreSQL/Valkey internal-only, official pinned 3.2.1 image.
- Companion stdlib Python reverse proxy at port 4387. All non-/radar paths proxy to the fixed upstream http://127.0.0.1:4386 (configured fixed upstream). The native application remains accessible. /radar/ static application fetches native /api/ endpoints on the same origin; native Paperless's user session/token and permission checks remain authoritative. Never use a shared privileged admin token for browser requests; do not weaken CSRF or auth.
- Static assets carry no secrets. No new public unauthenticated article endpoints. Authenticated native API pagination must be followed; don't assume one page contains everything. Use bounded fixed-upstream forwarding, request size limits, no open proxy or path traversal. Default browser read-only radar; native document link handles edits.
- Runtime secrets and migration receipts live OUTSIDE Git under ~/.config/paperless-ngx and ~/Library/Application Support/paperless-radar. No article titles/bodies/URLs, runtime credentials or screenshots may be committed to the public fork.

## Data contract
Each imported document is a UTF-8 text article archive retaining real extracted article body plus separate clearly labelled analysis. Native title, created=publish_date (omit if unknown), correspondent=account, document_type='WeChat article', tags include 'wechat' and original category and tags.
Custom fields (exact names):
wx_source_url: longtext (full original URL; native url field is limited to 200 characters and cannot hold typical WeChat share URLs)
wx_source_id: string (stable source id / URL hash)
wx_category: string
wx_summary: longtext
wx_key_points: longtext (JSON string)
wx_actionable: string
wx_actionable_note: longtext
wx_hype: string
wx_hype_note: longtext
wx_shelf_life: string
wx_publish_date: date (omit unknown)
wx_share_date: date (omit unknown)
wx_content_state: string
wx_reading_state: string
wx_enrichment: longtext (JSON retaining original item fields except user_state; preserves provenance)
Treat native created metadata as authoritative only when wx_publish_date exists. Radar freshness recomputed client-side as a heuristic: fast >=3 months, medium >=9 months; evergreen not stale. Explain that this is NOT fact-checking.

Import only entries with nonempty real raw text and enriched summary; default 20 maximum, representative categories and quality/freshness values. Nonarticle pushes are reported as skipped, never invented. Existing library is strictly read-only; use data/articles_raw.jsonl and app/library.json. Map raw URL->best nonempty text. API get/list custom_fields, correspondents, tags, document_types; create missing exact names. Upload via POST /api/documents/post_document/ multipart, custom_fields as id->value JSON mapping. Poll GET /api/tasks/?task_id=uuid to terminal SUCCESS/FAILURE; fetch exact created document and verify source_id, URL, content and metadata. Page existing documents before deciding dedupe using source_id/source_url. Re-run must upload zero duplicates; pending upload receipt checked BEFORE another upload. Failed imports exit nonzero and retain receipt. No deletion of existing documents.

## UX
Traditional Chinese labels. Keep native Paperless visual language (white/green, understated dense cards; no generic flashy redesign). Visible nav 'Paperless 文件庫' and '文章地圖'. Clear login-required empty state vs real empty data vs API failure. Topic map/list toggle, search, category, freshness, actionable/high-value filter. Click an article opens summary/details with real source and native document links. All HTML rendering safely escaped / textContent. Mobile responsive. No fake demo records.

## Acceptance
1. Focused unittest suite for migration URL mapping, selection, pending/duplicate dedupe, metadata, failure handling and proxy restrictions/pagination transform.
2. Syntax checks (Python and JavaScript), tests with deterministic synthetic fixtures explicitly restricted to tests.
3. CLI `python3 extensions/article_radar/migrate.py --library <old root> --limit 20 --base-url http://127.0.0.1:4386 --secret-file <external env> --receipt <external path>` and `python3 extensions/article_radar/server.py --host 0.0.0.0 --port 4387 --upstream http://127.0.0.1:4386`.
4. No upstream src/ or src-ui/ changes. No modification/stop of old service or cron. No git push from worker. Native Paperless contract unchanged; extension protocol/schema v0.1.0.
5. Module README with startup, rollback, external secrets, security limits, source data and backup procedure. Git-tracked CHANGELOG.md and change catalog list exact changed paths, version, canonical contract, tests and limitations (no runtime claims before parent validation).

Owner: Naya accepts and deploys. Worker implements only extensions/article_radar/** plus extensions/CHANGELOG.md and extensions/change-catalog.json. Must escalate architecture or auth choices, never bypass gates. Parent owns extensions/deploy/** and this spec; single worker writer elsewhere.
