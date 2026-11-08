# Scraping and the shared JSON format

## Retained components

- `aio_scraper.py`: custom Playwright parser/session, DataImpulse selection, challenge detection and 2Captcha solving.
- `serpapi_client.py`: bounded SerpAPI search/lazy-AIO capture and credential redaction.
- `compare_serpapi.py`: paired capture/repeats, per-source timing, checkpoints and AIO/organic structural comparison.
- `run_supabase_batch.py`: the shared single-query/custom-batch JSON controller and optional native-snapshot upload.
- `search_export.py`: scoped normalization and atomic JSON-only replay/export.
- `supabase_results.py`: response storage for both native captures and shared records, including validation, complete response-row projection, SQL export and atomic uploads through one fixed-host HTTP implementation.
- `comparison_storage.py`: the comparison storage command and compatible Python imports. It delegates storage policy to `supabase_results.py`.
- `response_sql.py`: the shared two-table PostgreSQL write contract.

The only storage format is the [two-table response contract](sql-storage.md). There is no CSV writer, alternative five-table formatter or whole-response comparison mode.

## Proxy and CAPTCHA configuration

The CLI `--browser` and `--proxy` options override `GOOGLE_AIO_BROWSER` and `GOOGLE_AIO_PROXY`. Without an explicit selection, the library defaults to Chromium/direct. Stored DataImpulse credentials alone do not enable a proxy.

Use `--browser firefox --proxy dataimpulse` for this project's working proxy configuration. Chromium through DataImpulse repeatedly timed out in the recorded environment; no silent browser/proxy fallback occurs. Direct Chromium remains available with `--browser chromium --proxy direct`.

Proxy credentials can be supplied as `DATAIMPULSE_PROXY_URL` or the pair `DATAIMPULSE_PROXY_USER`/`DATAIMPULSE_PROXY_PASSWORD`. The pair defaults to host `gw.dataimpulse.com`, port `823`, overridable with `DATAIMPULSE_PROXY_HOST`/`DATAIMPULSE_PROXY_PORT`. URL credentials are percent-decoded for Playwright. Explicit direct mode ignores stored proxy credentials.

When Google presents `/sorry` or a reCAPTCHA challenge, the custom scraper uses `TWOCAPTCHA_API_KEY` if configured. It extracts the observed sitekey, Enterprise flag and available challenge fields, submits the task to 2Captcha, polls for a token, applies returned worker cookies and submits the challenge form. Browser cookies/User-Agent are sent to the solver for session-context alignment; do not use an authenticated personal browser profile for this workflow.

Every solve attempt can cost credits even if Google rejects the token. Missing solver configuration or a rejected challenge is an explicit capture failure, not an empty successful SERP. No solver token, challenge URL parameters or cookies should appear in output. Native JSON checkpoints and error messages are sanitized.

`GOOGLE_AIO_PROFILE` controls the persistent profile, default `.aio-profile` beside the module. Use a separate profile for each browser/transport, for example `.aio-profile-firefox`. Set it to `off` for a fresh session. Keep profiles private and out of git. `GOOGLE_AIO_HEADLESS=0` opens a visible browser; otherwise it is headless.

One browser session serves the full custom batch. A solved challenge can cover later requests in that session, but this is not guaranteed.

AIO polling and HTML parsing share the same completion classifier. The collector observes delayed AIO containers for up to eight seconds. Once a container appears, it waits up to 20 seconds for a real answer, not the `Thinking`/`Searching` status wrapper, and requires two seconds of unchanged answer structure and citations with no active generation indicator. An unfinished shell or transient generation error permits one fresh navigation with a 30-second rendering budget. These are rendering budgets, separate from the navigation timeout and CAPTCHA handling.

Only a displayed `An AI Overview is not available for this search` notice is classified `unavailable`. Hidden notice templates never establish unavailability. `Can't generate an AI overview right now. Try again later.` is `unresolved`, with `extraction_metadata.aio_reason="generation_failed"`. Loading/unfinished states are also `unresolved`, with reason `generation_in_progress` or `render_incomplete`; they must not be counted as confirmed no-AIO observations. Continuously changing answers that do not settle fail explicitly.

Browser snapshots serialize a clone of the page and annotate computed-hidden AIO elements with `hidden`, without modifying the live page. Retained DOM evidence therefore preserves stylesheet-based visibility after style/script removal. Parsing excludes hidden subtrees, UI-only copy and source cards from answer text.

## Fresh paired capture

```sh
GOOGLE_AIO_PROFILE=.aio-profile-firefox \
.venv/bin/python -m compare_serpapi \
  --queries query_test/queries.txt --out-prefix query_test/NEW_COMPARISON \
  --browser firefox --proxy dataimpulse --api-timeout 90 --timeout 90000 --delay 3
```

Use a positional query instead of `--queries` for one pair. `--runs N` repeats the input round, preserving separate query/run/snapshot identities. Input query files have one query per line; repeated text is permitted and gets a distinct query ID by position. IDs remain stable between repeat rounds.

The custom browser timeout is milliseconds; the SerpAPI timeout is seconds. One pair permits one search and at most one lazy-AIO follow-up. No API retry, pagination, destination download or paid call occurs during replay/upload. Matching locale does not match Google IP/session/personalization.

Native captures and a JSON comparison report are checkpointed per pair. Canonical JSON is published atomically under `<prefix>.main-body/records.json`. Existing output paths are refused. Export failures retain native captures; a local export retry does not repeat paid capture requests.

## JSON contents

Each canonical array item has:

- `schema_version` and `provenance`: provider, query ID/text, snapshot ID, repeat index, source filename, observed capture time/settings and available source metadata.
- `aio_status`: present, absent, unavailable, empty, unresolved, invalid or error.
- `ai_overview`: ordered `blocks`/`references`, or NULL when no overview was observed.
- `organic_count` and ordered `organic_results`.
- `capture_status`, errors and warnings when applicable.

AIO blocks preserve full paragraphs/headings, recursive list items, tables/cells/spans, inline links and observed citation indexes. References preserve actual indexes and full snippets when present. Organic entries preserve source order separately from returned rank, title, URL/displayed URL, full snippet and observed source/date/author/sitelinks/textual rich snippets.

No text truncation is applied. Credentials, short-lived tokens and URL userinfo are redacted in native and common JSON; harmless URLs/query parameters and all other content remain unchanged. New substantive AIO fields are preserved with warnings in storage, while unsupported block types require explicit parser support. Original opaque Google redirects are retained; destinations and citation mappings are not guessed or fetched. Unknown locale/time stays unknown. A shell without extracted content is unresolved, not proof Google generated no answer.

The common normalizer strips media assets, ads, related searches/questions, separate ancillary modules, service helper fields and credentials. SQL projection retains the scoped fields described in [SQL storage](sql-storage.md), not entire native responses. JSON strings keep their original leading characters; there is no spreadsheet-specific escaping.

## Saved-input replay

Compare a saved native single-query pair without browser/network calls:

```sh
.venv/bin/python -m compare_serpapi \
  --scraper-json saved/custom.json --serpapi-json saved/serpapi.json \
  --out-prefix query_test/NEW_REVIEW
```

Convert a complete native batch, list of snapshots or saved provider captures to common JSON:

```sh
.venv/bin/python -m search_export --provider custom \
  --input query_test/NEW_CUSTOM.json --output-dir query_test/NEW_NORMALIZED

.venv/bin/python -m search_export --provider serpapi \
  --input saved/serpapi.json --output-dir query_test/NEW_SERPAPI_NORMALIZED
```

Export directories contain only `records.json`. Duplicate provider/snapshot identities and existing output directories are rejected. Multiple input paths preserve separate occurrences. Do not normalize `records.json` again: pass it directly to `comparison_storage`.

## Upload and failure handling

Use `comparison_storage --input .../records.json --comparison-id UUID --output-dir NEW --upload` for normalized pairs. Generate the UUID once per new dataset and retain it. The SQL artifact/manifest are written before upload; both providers insert atomically. Never regenerate an ID to retry an uncertain write.

Custom-only capture uses `run_supabase_batch QUERY` or `--queries FILE`, `--out-prefix NEW`, optional proxy/browser flags and optional `--upload`. It checkpoints a native JSON envelope with run UUID, planned query count and ordered `results` entries. `--upload-file FILE` uploads that complete native envelope without a new scrape, preserving its run UUID. Incomplete snapshots fail validation.

Collector/comparison exit codes are 0 for clean completion, 1 for retained per-query failures, and 2 for configuration/validation/I/O failures or an aborted run. A valid absent/unavailable AIO can still be a successful observation; empty/unresolved/invalid extraction is not treated as a clean comparison. Failed queries remain separate response records with statuses/warnings. An interrupted capture keeps its last JSON checkpoint. Database writes and SerpAPI requests never retry automatically. Custom challenge handling may make bounded solver attempts, each potentially charged.

## Verification

The default tests use mocked clients and synthetic HTML. Proxy precedence, Chromium proxy diagnostics, CAPTCHA context/token handling, ordered full-text extraction, sparse/missing citations, secret redaction, JSON-only outputs, repeats, collisions, failed export recovery and one-row SQL payloads have regression coverage. AIO regressions cover stable loading wrappers, delayed containers, hidden notice templates/ancestors, active generation with partial text, visible refusals versus transient errors, disappearing containers and bounded reload exhaustion.

Run the opt-in real Firefox checks with `AIO_BROWSER_TESTS=1 .venv/bin/python -m unittest discover -s tests -p test_aio_browser.py -v`. These checks use local HTML and intercepted responses with external requests blocked. They exercise computed CSS visibility, delayed JavaScript rendering and actual search/reload behavior without Google requests or solver charges. Runtime PostgreSQL migration/upload/reverse checks and fresh-data fidelity evidence are documented in [SQL storage](sql-storage.md).
