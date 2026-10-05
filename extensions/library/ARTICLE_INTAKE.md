# Article intake — owner acceptance revision 1

Date: 2026-10-06 Asia/Taipei. Owner card: `paperless-radar/t_d7c39023`.

This delivery follows the latest owner-card scope correction: connect the existing readable article corpus to the existing Paperless service and verify native readback. It does not release the pending UI, XHS, maintenance or general stage-4 implementation.

## Actual result

- Legacy registry: 404 records, unchanged snapshot throughout intake.
- Readable source entries: 279 unique articles. All 279 passed restricted-native-reader exact source text/SHA-256, canonical identity, author/date, preserved analysis and extraction-status comparison.
- Newly stored native articles: 259; existing articles deduplicated: 20. Final unresolved import failures: 0.
- Native document count: 25 before, 284 after. All 25 pre-existing documents retained their content, titles, tags, permissions and existing custom-field values.
- The remaining 125 legacy records have no readable source body; no article body or summary was invented for them.
- Final batch: 279 items, 20 duplicate and 259 partial, with no queued/importing/failed/blocked items. `partial` retains the legacy unknown/partial extraction-completeness status; it is not an upload failure or a claim of complete source extraction.

## Necessary native format recovery

Native libmagic rejected 14 UTF-8 article text files containing Python/Java/C/JavaScript/HTML as unsupported code/HTML formats. Those 14 were stored as local PDF transport documents with the exact UTF-8 source archive embedded. Downloaded original PDF hashes and embedded source bytes were checked; indexed native content was set from that exact source, then the existing migration verifier checked native writer/reader identity, full metadata and source fidelity. Known successful native task IDs were reconciled into the original queue, and the live worker completed its ordinary verification. Original rejection receipts remain private.

A real registered MCP `read_evidence` call on one recovered article returned source excerpts with author, date and exact excerpt hashes. No remote OCR, paid inference or additional external service was introduced.

## Evidence and boundaries

Private runtime receipts: `intake-native-before.json`, `intake-enqueue.json`, `intake-status.json`, `intake-readback.json`, `intake-rejections.json`, `intake-pdf-recovery.json`, and the pre-intake queue snapshot, under the existing owner-only acceptance directory. No private article body, URL, credentials, screenshots or runtime database is committed here.

This is an operational acceptance record, not a production source release. Existing independently reviewed migration source was exercised; this delivery changed no tracked implementation code. The old corpus/service/maintenance remain intact. Historical UI/XHS/offsite/remote-device/restore criteria are outside the latest narrowed request and are not claimed as accepted.
