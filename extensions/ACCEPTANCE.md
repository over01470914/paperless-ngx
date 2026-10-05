# Article Radar 0.1.0 — acceptance revision 1

Project: over01470914/paperless-ngx. Change/card: t_cdb93f5d, board paperless-radar. Date: 2026-10-05 Asia/Taipei. Baseline: upstream v3.2.1 / 7575d6078227ebdb4cf443f263d53ebc7575aa37. Extensions: absent -> 0.1.0; specification revision 1 -> 2. Upstream application/API unchanged.

## Independently exercised by Naya

- Native Paperless, PostgreSQL and Valkey running; native webserver and database healthy. Native authenticated document API and dashboard worked.
- Companion installed as a user LaunchAgent with KeepAlive and RunAtLoad. Both service entries added to the device-local launcher and read back. Runtime config, credentials and data remain outside the public repository.
- Bounded import: selected 20, uploaded 20, duplicates 0, skipped nonarticle/missing 127. Exact document GET for each record verified source link, metadata and content; 20 confirmed task receipts. Eleven categories; 20 unique source URLs. Longest exact original URL: 347 characters.
- Repeat import: selected 20, uploaded 0, duplicates 20. No duplicate documents created.
- Real browser: native document list contains 20 records; companion default map 20/20, category filter 10/20, search 9/20, high-action filter 16/20, list view and detail dialog verified. Detail shows source links and analysis separately. Mobile viewport 390px and body scroll width 390px; no horizontal overflow.
- Desktop/detail/mobile screenshots retained outside Git. No article body, title, source URLs or screenshots are committed to the public fork.
- Authentication: unauthenticated native API through companion returns 401; native session/token returns 200. Traversal path rejected with 400. Authenticated companion API at both localhost and the Mac's Tailscale IP returns 20 documents.
- Focused checks: 16 Python unittest cases PASS; JS default-view/two-page regression PASS; node --check PASS; Python 3.11 compileall PASS. No broad upstream build/test was run because core source is unchanged.
- Native exporter snapshot completed after creating its destination. Manifest contains 20 document records; 42 backup files. Export permissions restricted to the local owner.

## Root causes corrected during acceptance

1. Native URLField has max_length=200. A first ingestion task failed with zero documents. The failure receipt was preserved; terminal failure and zero related documents were confirmed before retry. wx_source_url is longtext; full URLs are retained, not truncated. Long-URL regression covers upload/dedupe/readback.
2. Default category option lost its explicit empty value during DOM replacement, hiding every article. Explicit empty value restored; JS regression demonstrated failure before and success after.
3. OCR mode `skip` is deprecated/invalid on this release; deployment uses `auto`. Chinese language packages install during startup; health readiness was awaited before export.

## Limits / unverified

- Mac-side Tailscale-IP checks are not Windows end-to-end checks. Windows peer was offline; no Windows-device acceptance claimed.
- Export is on the same host, not an off-device/disaster recovery backup. Restore was not exercised. Reboot recovery was not tested.
- Only 20 articles migrated, not the full archive. Existing article library, maintenance schedule and data remain unchanged.
- Companion is a removable read-only browsing module; reading-state editing, a true treemap, monthly timeline and automatic daily synchronization are not implemented in this release.
- No public exposure; HTTP transport is for trusted LAN/Tailscale. Files remain plaintext on disk. External AI is not enabled. Native proxy limits uploads to 16 MiB and responses to 32 MiB.

## Operating paths

Native Paperless: port 4386. Companion: port 4387, /radar/. User: boss; credential is in the protected external runtime store, never source or chat. Set a user-known password through a secure credential flow before routine cross-device use. Module and migration usage: extensions/article_radar/README.md. Deployment/upstream/backup guidance: extensions/deploy/README.md. Canonical contract: extensions/ARTICLE_RADAR_SPEC.md revision 2.
