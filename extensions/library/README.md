# Paperless Library 0.2.0 stage 1

This package provides durable ingress, a private SQLite job ledger, WeChat/text/file import adapters, bounded native-reader search/evidence, a local authenticated API, and a FastMCP stdio facade. It does not enable an XHS browser adapter, OCR, UI, deployment, or a live migration.

## Dependencies

Use the parent-provisioned Python 3.11 environment. Runtime pin: `mcp>=1.30,<2`; `beautifulsoup4`, `opencc-python-reimplemented`, and `PyMuPDF` are optional extraction/search enhancements. The package itself uses the standard library for its HTTP API and ledger.

## Parent bootstrap

Parent creates native `library-reader` and `library-writer` restricted identities, configures native visibility, and creates an external mode-0600 JSON file (normally `~/.config/paperless-ngx/library.json`):

```json
{"native_url":"http://127.0.0.1:4386","reader_token":"[external]","writer_token":"[external]","service_token":"[external]","runtime_dir":"~/Library/Application Support/paperless-radar/library","allowed_file_roots":["/approved/attachments"],"legacy_library":"/parent-owned/old-library","service_url":"http://127.0.0.1:4388","rate_limit_seconds":1.5,"reader_user_id":101,"writer_user_id":102,"boss_user_id":103}
```

`runtime_dir` is outside Git; it contains a mode-0600 ledger and mode-0700 staging directory. `allowed_file_roots` must be explicit directories; only regular TXT, MD, PDF, and DOCX files are accepted. The three native user IDs are mandatory: successful imports grant reader/boss view and writer/boss change permissions after task completion, then require reader readback. Start with `python -m extensions.library.app` and register `python -m extensions.library.mcp_server` with the parent MCP profile. Registration, credentials, roles, live service startup, and real source ingestion remain parent-owned.

The service starts its owned background worker thread before accepting requests. Ledger access is serialized with a reentrant lock; leases use persisted epoch time so expired fetching/importing claims recover after restart. Native-task polling has its own delayed schedule and does not consume source retry attempts. An uncertain intent without a task reconciles readable native records before becoming blocked; it is never blindly uploaded again. The fixed `legacy_library` is read only by `/v1/migrate`; it preserves usable legacy analysis metadata and records skipped unreadable rows. Existing readable Paperless documents are reconciled by platform source ID, full private URL, canonical URL, then content hash before any upload.

Resume responses expose only sanitized item fields. URL supplements are staged as text or an accepted-root file and preserve the existing input identity. File imports upload the original submitted bytes with their filename/MIME and calculate identity/hash from those bytes. Stage 1 does not yet download a native original to independently hash-verify retention after import; parent real-native QA must verify that server-side behavior before claiming original-file readback.

Optional approved enrichment is disabled unless `enrichment_enabled` is true. When enabled, configuration must set `enrichment_endpoint` to the parent-approved compatible endpoint, `enrichment_model` to `deepseek-v4.1-flash`, `enrichment_key_file` to an external mode-0600 reference containing the existing `DASHSCOPE_API_KEY`, and `enrichment_version`. The key is read only at an enabled request; it is never logged or stored. Calls are bounded to two attempts and require a non-truncated strict JSON response. Default analysis remains labelled `method=extractive`.

Search uses the native reader's permission-aware DRF `search` parameter, not `query`. It issues at most eight balanced exact-term variants, preserving English tokens and expanding fixed aliases and Chinese terms in both OpenCC directions. Evidence is exact source text only. Legacy citations resolve numeric `wx_source_url`, `wx_publish_date`, and `wx_enrichment` fields through the native custom-field schema; public citations canonicalize the URL.

WeChat parsing tracks the actual `#js_content` root boundary, ignores void/self-closing elements and hidden/script/style descendants, and gives a nonempty article body precedence over page chrome. Article prose mentioning verification or login is not treated as a gate. Credentialed input URLs are rejected before batch creation, and public locators never include userinfo.

For a scanned PDF/DOCX, a bounded manual OCR supplement is stored only in temporary staging, with public provenance excluding OCR text. The original file bytes remain the upload payload; after native task completion the service preserves existing extracted source content, appends one marked OCR section, patches sanitized provenance, and verifies both through the restricted reader. OCR source/content patch semantics and original-binary retention require parent real-native QA. Reanalysis queues only an existing confirmed document, never reuploads; it compares a normalized archived-source hash held in analysis metadata before approving the configured analysis version, then patches and reads back analysis fields.
