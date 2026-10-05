# Paperless Library MCP skill template

Discover the parent-registered `paperless-library` MCP server through the host MCP tool list. It exposes `submit_batch`, `batch_status`, `resume_item`, `cancel_batch`, `search_library`, `read_evidence`, `set_reading_state`, and `migrate_legacy`.

The service binds only to `http://127.0.0.1:4388`. Registration is parent-owned and this repository does not claim that a profile is currently registered. If MCP discovery is unavailable, use the authenticated local API only through the parent-provisioned service token; never put that token in chat, source, or command arguments.

Search first, then obtain exact evidence before making a claim. `no_match: true`, permission errors, `needs_login`, and `needs_ocr` are valid outcomes and must be reported honestly.
