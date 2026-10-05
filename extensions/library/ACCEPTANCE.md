# Paperless Library 0.2.0 — owner acceptance revision 1

Date: 2026-10-05 Asia/Taipei. Owner card: paperless-radar/t_d7c39023. Source/QA card: t_9248faca. Native 3.2.1 and Article Radar 0.1.0 unchanged.

## Verified in the deployed system

- Dedicated Python 3.11 runtime; user LaunchAgent com.garbagod.paperless-library runs the actual library package. Loopback 4388 health returns version 0.2.0; unauthenticated operations return 401. RunAtLoad/KeepAlive are configured; reboot recovery has not been tested.
- CURRENT dialogue's registered MCP tools submit_batch, batch_status, search_library, read_evidence and resume_item were called, not merely listed. Real user-submitted TXT/PDF imports both reached confirmed native task results and restricted-reader document readback. A separate real direct-body submission also confirmed.
- A true browser-readable WeChat article was supplemented via the registered resume_item text tool after its canonical source identity was verified from the actual public article page. Native import confirmed. This proves authorized browser-text supplementation, not autonomous HTTP extraction or short-link resolution.
- Downloaded original TXT and PDF bytes through the restricted native reader. Both SHA-256 values exactly match the actual submitted files. Verbatim evidence offsets and hashes match exact native source content for all three confirmed records. Actual receipts remain private outside Git.
- Focused unit suite: 30 tests PASS. Code QA performed independently by the native qa profile; fake-native socket tests are NOT claimed as native acceptance.
- No new external AI provider or remote OCR is enabled. Default analysis is explicitly extractive.

## Findings — user goal remains incomplete

- The supplied WeChat short link is browser-readable, but guarded HTTP returns a nonarticle response. Supplemental text/file for that short link cannot resolve its canonical platform identity. The blocked receipt is preserved, with zero upload for that request. Public article identifiers were independently read from the actual browser page; canonical-link supplemental ingestion is separately being exercised. This is not autonomous short-link acceptance.
- Search is callable and returns verbatim evidence, but query ranking is inadequate. The prior real 21-question baseline is 18/21; new daily-use queries also expose generic-term ranking noise. No full retrieval-benchmark PASS is claimed.
- The preexisting host-wide Tailscale loopback bridge automatically exposes loopback listeners on the authenticated tailnet interface. Therefore application bind=loopback does not prove host-wide loopback isolation. No public exposure/Funnel was created. Bridge policy needs a narrowly scoped exclusion before strict loopback-only acceptance.
- XHS visible Edge adapter/local OCR, daily UI, complete legacy metadata migration, full larger restore and daily backups remain incomplete. Old source library and its maintenance job remain live and read-only to this project.
- Remote Windows peers, off-device backup and reboot acceptance remain unverified. No new backup destination is authorized.

## Private evidence

Owner runtime receipts and original/source hashes are under the external library acceptance directory. Real article titles, bodies, URLs, credentials, screenshots and backups are intentionally absent from this public document.
