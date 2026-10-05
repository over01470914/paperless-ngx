# Article Radar v0.1.0

Article Radar is a removable companion to Paperless-ngx. The browser at `/radar/` reads the native authenticated API; the importer archives real source text as a native document and stores separate, labelled analysis in native custom fields. The source library is read only.

Specification revision 2 requires `wx_source_url` to be native `longtext` so the complete original URL survives. Native `url` fields are limited to 200 characters; the importer rejects an existing `wx_source_url` with that old type instead of truncating or overwriting it. The browser still permits only safe HTTP(S) source links. An operator reported that the first live import attempt stopped at a failed task with zero documents confirmed under the old type; receipt reconciliation and the already verified native field correction are operator-owned. This code update does not establish a successful live import.

## Before use

1. Back up the Paperless database and document media using the deployment's normal backup procedure, and retain a read-only copy or snapshot of the external source library. This repository does not perform backups.
2. Use a Paperless user API token with permissions to create/view documents and create/view tags, correspondents, document types and custom fields. Put exactly one line, `PAPERLESS_API_TOKEN=<token>`, in a file outside this repository and the source library, for example `~/.config/paperless-ngx/radar.env`. Set its mode to `0600`. Do not put credentials in command arguments, logs or Git.
3. Keep the receipt outside Git. The default is `~/Library/Application Support/paperless-radar/migration-receipt.json`. The importer refuses receipts inside this repository or the source library. Back it up with the Paperless data before any retry.

## Safe import

First inspect bounded counts without an API call or receipt write:

```sh
python3 extensions/article_radar/migrate.py --library /path/to/wx-article-library --limit 20 --base-url http://127.0.0.1:4386 --dry-run
```

After backing up, run the same selection against Paperless with an external token file and receipt:

```sh
python3 extensions/article_radar/migrate.py --library /path/to/wx-article-library --limit 20 --base-url http://127.0.0.1:4386 --secret-file ~/.config/paperless-ngx/radar.env --receipt "$HOME/Library/Application Support/paperless-radar/migration-receipt.json"
```

The importer only accepts a fixed loopback HTTP upstream, pages all native resources/documents, creates exact custom-field names and types (`wx_source_url: longtext`), and fails on a mismatched field type. It chooses at most 20 articles by default, covering categories, shelf lives and action ratings. It skips nonarticle entries and entries without both a real raw body and enriched summary. It stores a receipt **before** each upload, then records the returned task UUID. A rerun resolves a pending task before any further upload; confirmed source URL or source ID is deduplicated across all visible native document pages. Failed and revoked tasks exit nonzero and keep the receipt. Successful tasks must return `result_data.document_id`; the importer fetches that exact document and checks native metadata, custom fields and raw body before confirmation.

If the receipt contains an `intent` without task UUID, the upload outcome is uncertain. The importer stops. Inspect the native task list and documents with the exact source ID/URL, then reconcile the external receipt manually from authoritative evidence; do not clear the entry or rerun an upload on a guess. If a task failed because Paperless detected duplicate binary content, investigate the existing document before any retry. The tool never deletes native documents.

## Companion server

```sh
python3 extensions/article_radar/server.py --host 0.0.0.0 --port 4387 --upstream http://127.0.0.1:4386
```

Open `http://<host>:4387/radar/`. The reverse proxy forwards every other path to the fixed local Paperless upstream. Its four static radar paths contain no token or private article text. Browser API calls use the user's native Paperless session; native permissions and CSRF remain in effect. Use a trusted access boundary and TLS termination appropriate to the deployment before exposing port 4387 beyond the host. The stdlib server itself serves HTTP only.

The UI loads all native API pages, offers topic map/list, search, category, freshness and high-action filters, and opens a detail view with native document and source links. Freshness is a heuristic: `fast` prompts review at 3 months, `medium` at 9 months, and `evergreen` is never labelled stale. It is **not fact-checking**. Unknown dates or shelf lives stay explicit.

## Rollback and limits

Stop the companion process and remove its external startup configuration to remove the UI/proxy. Do not delete the receipt until its tasks are reconciled and backups are retained. Imported documents and custom fields remain in native Paperless; any document cleanup is a separate, reviewed native operation after backup. The importer does not alter the old source service or its cron jobs.

The proxy limits request bodies to 16 MiB and responses to 32 MiB, uses a 20-second upstream timeout, rejects traversal and never forwards to a client-chosen host. Native downloads larger than 32 MiB will fail through this companion port. The importer cannot deduplicate documents hidden from its API user by native permissions; use an account with visibility over the relevant archive. It does not validate article claims or repair old source data. This version has only local synthetic tests; live migration, browser acceptance and deployment remain operator-owned.
