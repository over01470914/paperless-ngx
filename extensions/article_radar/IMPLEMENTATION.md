# Article Radar v0.1.0 implementation evidence

Contract: `extensions/ARTICLE_RADAR_SPEC.md` revision 2. Product code and assets are confined to `extensions/article_radar/`; the changelog and catalog are the only other changed paths. Existing operator files in this directory were not edited.

Operator correction: the parent reports that the first live import stopped after its first task failed and zero documents were confirmed. The reported root cause is native `CustomFieldInstance.value_url`'s 200-character limit. The parent reports it changed the empty native field to `longtext` and verified the GET readback, and retains ownership of failed receipt reconciliation. This writer did not access the live API or receipt and cannot claim a successful live import. The product field contract and synthetic regression now use full-length `longtext` for `wx_source_url`.

Source inspection was read-only. The native `PostDocumentSerializer` accepts multipart `document` and `custom_fields` as an id-to-value JSON mapping; `PostDocumentView` returns a task UUID. The v10 task serializer exposes lower-case status and `result_data`; `DocumentSerializer` exposes `content` and `{field,value}` custom fields. The source library inspection printed only structure and aggregate value counts, never private article text, title or URL.

## Checks

- `python3 -m unittest discover -s extensions/article_radar/tests -v` — 16 synthetic tests passed (exit 0), including a full URL longer than 200 characters through mapping, multipart, dedupe and readback.
- `python3 -m compileall -q extensions/article_radar` — initially failed because this macOS Python attempted to write bytecode in `~/Library/Caches/com.apple.python`, outside the workspace sandbox. No source compilation error was reported.
- `PYTHONPYCACHEPREFIX=extensions/article_radar/.pycache python3 -m compileall -q extensions/article_radar` — passed (exit 0) with bytecode confined to this allowed, ignored extension directory.
- `node --check extensions/article_radar/static/app.js` — passed (exit 0).
- `python3 -m json.tool extensions/change-catalog.json` — passed (exit 0).

This writer performed no import, proxy startup, network request, live migration, deployment, credentials inspection, service restart, commit or push. The tests use synthetic article values and mocked API responses only. The parent-reported failed live task is recorded above; successful native import and browser UI remain unverified by this writer.

## Default view regression correction

Operator browser acceptance found that the default map showed zero articles: `load()` replaced the category selector's explicit `value=""` option with an option whose value fell back to its label `所有類別`. The only UI logic change sets that replacement option's value to `""`. The synthetic DOM regression is `extensions/article_radar/tests/app_default_view.test.mjs`; it models native option value fallback, paginates native v10-shaped responses, and checks that all three synthetic articles render.

- `RADAR_APP_JS=/Users/garbagod/.hermes/profiles/developer/cache/scratch/radar-review/app.js.prefix-snapshot node extensions/article_radar/tests/app_default_view.test.mjs` — exit 1: `FAIL app_default_view: default category option.value = "所有類別" (expected "")`; Node also printed `'所有類別' !== ''`.
- `node --check extensions/article_radar/static/app.js` — exit 0, no output.
- `node extensions/article_radar/tests/app_default_view.test.mjs` — exit 0: `PASS app_default_view: option.value=""; 3 / 3 篇; 3 article nodes; 2 document pages`.
- `python3 -m unittest discover -s extensions/article_radar/tests -v` — exit 0: `Ran 16 tests in 0.004s`, `OK`.

The operator owns live browser recheck and release acceptance; this local synthetic regression does not establish live UI acceptance.
