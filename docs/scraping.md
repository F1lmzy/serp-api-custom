# Capture pipeline and the shared JSON format

This page documents how the runtime fits together: which module owns which
responsibility, what each format contains, and where the hard boundaries
(never retried, never guessed, never uploaded twice) sit. CLI quick-start
examples live in the [README](../README.md); the database contract lives in
[SQL storage](sql-storage.md).

## Pipeline

```
capture (browser / SerpAPI)
   → native JSON          aio_scraper.py / serpapi_client.py
   → shared records       search_export.py normalize_response
   → validation + SQL     supabase_results.py / response_sql.py
   → optional upload      one atomic statement, no retry
   → browsing             aio_viewer/ (local) or worker/ (Cloudflare)
```

Two entry formats exist, and both converge on the same stored rows:

- **Native capture**: a provider response in its original structure with
  capture context and credential redaction. A custom batch capture is one
  JSON envelope with a run UUID, planned query count and ordered results.
- **Shared record**: the same response expressed in the common AIO and
  organic vocabulary with provenance, statuses and warnings. It retains
  provider and query-occurrence identity rather than merging observations.

`search_export.py` converts native → shared. `supabase_results.py` converts
either → validated two-table rows. Nothing in the pipeline downloads linked
pages or invents citation mappings. The only redirect resolution is bounded
and live-only: a custom capture observes Google's own redirect response for
each opaque `/url`/`/goto` link once, in the same browser session, and never
follows it to the destination page. Saved input is never resolved after the
fact.

## Module responsibilities

| Module | Owns | Notable boundaries |
| --- | --- | --- |
| `aio_scraper.py` | Playwright parser/session, proxy selection, challenge detection and 2Captcha solving | One browser session per batch; at most two token/form attempts, each potentially charged; AIO observation state is attempt-local |
| `redirect_resolver.py` | Live opaque-redirect destination observation: one response, a saved capture, or ad-hoc links (`google-aio-resolve`) | At most two observations per unique link, never followed to destination content; checkpointed link→destination caches so interrupted runs resume; saved input is resolved only against a live session, never guessed offline |
| `serpapi_client.py` | Bounded SerpAPI search/lazy-AIO capture and credential redaction | One search request plus at most one lazy follow-up; no pagination, no retries |
| `compare_serpapi.py` | Paired capture/repeats, per-source timing, checkpoints, AIO/organic structural comparison | Saves files only; never uploads; comparison is structural, not scalar wording |
| `run_supabase_batch.py` | Custom single-query/batch JSON controller and optional native-snapshot upload | Checkpoints after every query; `--upload-file` never rescrapes; incomplete snapshots fail validation |
| `search_export.py` | Scoped normalization and atomic JSON-only replay/export | No network; rejects duplicate provider/snapshot identities and existing output paths |
| `supabase_results.py` | Validation, response-row projection, SQL export and upload policy for both input paths | One fixed-host, redirect-refusing HTTP implementation; no uncertain-write retry; native vs comparison exception/receipt contracts stay distinct |
| `comparison_storage.py` | The `google-aio-sql` command and compatible Python import paths | No storage implementation of its own; delegates to `supabase_results.py` |
| `response_sql.py` | The shared two-table PostgreSQL write contract | The column list in `TABLE_COLUMNS` is the upload projection, not the entire database schema |
| `aio_viewer/` | Local loopback-only read-only viewer | Binds only to `127.0.0.1`; no public-host option or login |
| `worker/` | Cloudflare Worker hosting the same frontend and a read-only API | Deliberately public, no login; only fixed-table/column GET queries; secret stays server-side |

See [domain terms](../GLOSSARY.md) for provider response, query occurrence,
native capture and shared record. The [state-flow map](state-flow.md) ties
guards, budgets, terminal outcomes and side effects to the actual functions.

## Identities, ordering and statuses

- **Run identity.** A custom batch capture generates a fresh run UUID and
  keeps it for its lifetime; uploads preserve it. Comparison datasets get a
  comparison UUID you generate once, and each provider/repeat round derives a
  deterministic run UUID from it, so a comparison never overwrites its own
  earlier round.
- **Query identity.** Query IDs are positional (`P01`, `P02`, …) within a
  round; repeat rounds reuse the IDs but not the snapshot IDs. A stored row
  is unique per `(run_id, query_id)`, ordered by `ordinal`.
- **Capture status vs AIO status.** `capture_status` (`ok` / `unresolved` /
  `error`) describes whether the capture itself succeeded; `aio_status`
  (present / absent / unavailable / empty / unresolved / invalid / error)
  describes the AI Overview observation. A valid absent/unavailable AIO is a
  **successful** observation (`status='ok'`); empty/unresolved/invalid
  extraction is not treated as clean comparison or a clean row.
- **Checkpoint limits.** Native capture is checkpointed per query, and paired
  runs checkpoint three files per pair. Each file is atomic on its own; the
  three files together are **not** a transaction, so an interrupted pair can
  leave a saved-but-inconsistent checkpoint set. An interrupted report may
  remain marked `running`. There is no automatic resume.
- **Uncertain-write recovery.** A failed request may still have committed.
  Never regenerate an ID to retry; inspect the retained run/comparison UUID
  remotely, then decide. Core uploads insert the whole payload in one atomic
  statement and never fall back to chunks; the one historical chunked
  workaround is a separate operational script (see
  [the 413 exception](state-flow.md#the-http-413-operational-exception)).

## Proxy and CAPTCHA configuration

The CLI `--browser` and `--proxy` options override `GOOGLE_AIO_BROWSER` and
`GOOGLE_AIO_PROXY`. Without an explicit selection, the library defaults to
Chromium/direct. Stored DataImpulse credentials alone do not enable a proxy.

Use `--browser firefox --proxy dataimpulse` for this project's working proxy
configuration. Chromium through DataImpulse repeatedly timed out in the
recorded environment; no silent browser/proxy fallback occurs. Direct
Chromium remains available with `--browser chromium --proxy direct`.

Proxy credentials can be supplied as `DATAIMPULSE_PROXY_URL` or the pair
`DATAIMPULSE_PROXY_USER`/`DATAIMPULSE_PROXY_PASSWORD`. The pair defaults to
host `gw.dataimpulse.com`, port `823`, overridable with
`DATAIMPULSE_PROXY_HOST`/`DATAIMPULSE_PROXY_PORT`. URL credentials are
percent-decoded for Playwright. Explicit direct mode ignores stored proxy
credentials.

When Google presents `/sorry` or a reCAPTCHA challenge, the custom scraper
uses `TWOCAPTCHA_API_KEY` if configured. It extracts the observed sitekey,
Enterprise flag and available challenge fields, submits the task to 2Captcha,
polls for a token, applies returned worker cookies and submits the challenge
form. Browser cookies/User-Agent are sent to the solver for session-context
alignment; do not use an authenticated personal browser profile for this
workflow.

Every solve attempt can cost credits even if Google rejects the token.
Missing solver configuration or a rejected challenge is an explicit capture
failure, not an empty successful SERP. No solver token, challenge URL
parameters or cookies should appear in output. Native JSON checkpoints and
error messages are sanitized.

`GOOGLE_AIO_PROFILE` controls the persistent profile, default `.aio-profile`
beside the module. Use a separate profile for each browser/transport, for
example `.aio-profile-firefox`. Set it to `off` for a fresh session. Keep
profiles private and out of git. `GOOGLE_AIO_HEADLESS=0` opens a visible
browser; otherwise it is headless.

One browser session serves the full custom batch. A solved challenge can
cover later requests in that session, but this is not guaranteed.

AIO polling and HTML parsing share the same completion classifier. The
collector observes delayed AIO containers for up to eight seconds within a
20-second total polling budget, requiring two seconds of unchanged answer
structure and citations with no active generation indicator. It never
settles on the `Thinking`/`Searching` status wrapper. A seen shell with no
`ai_overview` key permits one fresh navigation with a 30-second polling
budget. This existing guard also includes visible unavailability notices; it
does not retry exceptions or an `empty` overview object. These are rendering
budgets, separate from navigation and CAPTCHA handling. See the
[state/transition tables](state-flow.md) for exact guards, ownership and
failure boundaries.

Only a displayed `An AI Overview is not available for this search` notice is
classified `unavailable`. Hidden notice templates never establish
unavailability. `Can't generate an AI overview right now. Try again later.`
is `unresolved`, with `extraction_metadata.aio_reason="generation_failed"`.
Loading/unfinished states are also `unresolved`, with reason
`generation_in_progress` or `render_incomplete`; they must not be counted as
confirmed no-AIO observations. Continuously changing answers that do not
settle fail explicitly.

Browser snapshots serialize a clone of the page and annotate computed-hidden
AIO elements with `hidden`, without modifying the live page. Retained DOM
evidence therefore preserves stylesheet-based visibility after style/script
removal. Parsing excludes hidden subtrees, UI-only copy and source cards from
answer text.

## Opaque redirect destination observation

`redirect_resolver.py` owns every form of opaque-redirect destination
observation: the scraper calls it once per final attempt, and
`google-aio-resolve` exposes the same machinery for saved captures and
ad-hoc links. Google emits organic `/goto?url=<token>&gsessionid=…` and
AI-reference
`/url?…url=<token>…` links whose destinations are opaque server-side tokens,
not decodable URLs; the tokens carry no embedded destination, and most
AI-reference links also carry `psig`/`ust` click signatures that expire roughly
24 hours after capture. After `search` selects its final attempt (so a
discarded reload attempt never pays the cost), the session observes Google's
redirect response for each unique opaque link: one request per link through
the page's own context, `Referer` set to the results page, redirects never
followed (`max_redirects=0`), a 10s per-link timeout, at most 50 links and
a 75s total budget per query, with four concurrent observations. Fresh
in-session `/goto` links answer with an HTML interstitial whose own
meta-refresh delay runs about 24–26s, and the identical link observes a
clean 3xx once that window passes (28s in one recorded probe, with some
links still interstitial near 38s), so still-opaque links get one bounded
settle retry — a 45s pause, covering every window observed so far, followed
by a single re-observation, mirroring the pipeline's one lazy follow-up
doctrine. At most two observations are ever made per link.

A 3xx response with a non-Google HTTP(S) `Location` replaces the emitted link
verbatim (query parameters preserved, no canonicalization) and the original
redirect URL is retained as `redirect_link`. Anything else — an interstitial
page, an unsafe or Google `Location`, a timeout, budget exhaustion or a
transport error — leaves the link exactly as parsed, counted in
`extraction_metadata.redirect_resolution` (`attempted`/`resolved`), and the
shared record still carries the `opaque_google_redirect_url` warning.
Resolution never changes capture status or AIO classification; failure only
leaves links opaque. Organic order and counts, citation indexes and reference
deduplication are untouched, because the offline parser establishes them
before any destination is observed. Tokens are session- and time-bound, so
saved captures are never resolved retroactively: `search_export.py` and
`supabase_results.py` stay offline, and `redirect_link` is provenance, never a
substitute for `link`.

## Fresh paired capture

```sh
GOOGLE_AIO_PROFILE=.aio-profile-firefox \
uv run google-aio-compare \
  --queries query_test/queries.txt --out-prefix query_test/NEW_COMPARISON \
  --browser firefox --proxy dataimpulse --api-timeout 90 --timeout 90000 --delay 3
```

Use a positional query instead of `--queries` for one pair. `--runs N`
repeats the input round, preserving separate query/run/snapshot identities.
Input query files have one query per line; repeated text is permitted and
gets a distinct query ID by position. IDs remain stable between repeat
rounds.

The custom browser timeout is milliseconds; the SerpAPI timeout is seconds.
One pair permits one search and at most one lazy-AIO follow-up. No API
retry, pagination, destination download or paid call occurs during
replay/upload. Matching locale does not match Google IP/session/personalization.

Native captures and a JSON comparison report are checkpointed per pair.
Canonical JSON is published atomically under `<prefix>.main-body/records.json`.
Existing output paths are refused. Export failures retain native captures; a
local export retry does not repeat paid capture requests.

## Shared JSON contents

Each canonical array item has:

- `schema_version` and `provenance`: provider, query ID/text, snapshot ID,
  repeat index, source filename, observed capture time/settings and available
  source metadata.
- `aio_status`: present, absent, unavailable, empty, unresolved, invalid or
  error.
- `ai_overview`: ordered `blocks`/`references`, or NULL when no overview was
  observed.
- `organic_count` and ordered `organic_results`.
- `capture_status`, errors and warnings when applicable.

AIO blocks preserve full paragraphs/headings, recursive list items,
tables/cells/spans, inline links and observed citation indexes. References
preserve actual indexes and full snippets when present. Organic entries
preserve source order separately from returned rank, title, URL/displayed
URL, full snippet and observed source/date/author/sitelinks/textual rich
snippets.

No text truncation is applied. Credentials, short-lived tokens and URL
userinfo are redacted in native and common JSON; harmless URLs/query
parameters and all other content remain unchanged. New substantive AIO
fields are preserved with warnings in storage, while unsupported block types
require explicit parser support. Opaque Google redirects are resolved only
from redirect responses observed in the live capture session; resolved
records keep the original redirect URL as `redirect_link`, and unresolved
links stay opaque with their warning. Destinations and citation mappings are
never guessed, saved input is never resolved, and destination page content is
never downloaded.
Unknown locale/time stays unknown. A shell without extracted content is
unresolved, not proof Google generated no answer.

The common normalizer strips media assets, ads, related searches/questions,
separate ancillary modules, service helper fields and credentials. SQL
projection retains the scoped fields described in
[SQL storage](sql-storage.md), not entire native responses. JSON strings
keep their original leading characters; there is no spreadsheet-specific
escaping.

## Saved-input replay

Compare a saved native single-query pair without browser/network calls:

```sh
uv run google-aio-compare \
  --scraper-json saved/custom.json --serpapi-json saved/serpapi.json \
  --out-prefix query_test/NEW_REVIEW
```

Convert a complete native batch, list of snapshots or saved provider captures
to common JSON:

```sh
uv run google-aio-export --provider custom \
  --input query_test/NEW_CUSTOM.json --output-dir query_test/NEW_NORMALIZED

uv run google-aio-export --provider serpapi \
  --input saved/serpapi.json --output-dir query_test/NEW_SERPAPI_NORMALIZED
```

Export directories contain only `records.json`. Duplicate provider/snapshot
identities and existing output directories are rejected. Multiple input
paths preserve separate occurrences. Do not normalize `records.json` again:
pass it directly to `google-aio-sql`.

## Upload and failure handling

Use `google-aio-sql --input .../records.json --comparison-id UUID
--output-dir NEW --upload` for normalized pairs. Generate the UUID once per
new dataset and retain it. The SQL artifact/manifest are written before
upload; both providers insert atomically. Never regenerate an ID to retry an
uncertain write.

Custom-only capture uses `google-aio QUERY` or `--queries FILE`,
`--out-prefix NEW`, optional proxy/browser flags and optional `--upload`. It
checkpoints a native JSON envelope with run UUID, planned query count and
ordered `results` entries. `--upload-file FILE` uploads that complete native
envelope without a new scrape, preserving its run UUID. Incomplete snapshots
fail validation.

Collector/comparison exit codes are 0 for clean completion, 1 for retained
per-query failures, and 2 for configuration/validation/I/O failures or an
aborted run. A valid absent/unavailable AIO can still be a successful
observation; empty/unresolved/invalid extraction is not treated as a clean
comparison. Failed queries remain separate response records with
statuses/warnings. An interrupted capture keeps its last JSON checkpoint.
Database writes and SerpAPI requests never retry automatically. Custom
challenge handling may make bounded solver attempts, each potentially
charged.

## Verification

The [refactor verification record](refactor-verification.md) is a historical
snapshot of one offline refactor verification: before/after results, fault
injection, saved-input equivalence and remaining limitations. The default
tests use mocked clients and synthetic HTML. Proxy precedence, Chromium proxy
diagnostics, CAPTCHA context/token handling, ordered full-text extraction,
sparse/missing citations, secret redaction, JSON-only outputs, repeats,
collisions, failed export recovery, one-row SQL payloads and opaque-redirect
destination observation (resolved, rejected, capped, budget-exhausted and
failing cases, plus warning semantics for retained provenance) have
regression coverage. AIO regressions cover stable loading wrappers, delayed containers,
hidden notice templates/ancestors, active generation with partial text,
visible refusals versus transient errors, disappearing containers and
bounded reload exhaustion.

Run the default suite:

```sh
uv run python -m unittest discover -s tests -v
```

Run the opt-in real Firefox checks with
`AIO_BROWSER_TESTS=1 uv run python -m unittest discover -s tests -p test_aio_browser.py -v`.
These checks use local HTML and intercepted responses with external requests
blocked. They exercise computed CSS visibility, delayed JavaScript rendering
and actual search/reload behavior without Google requests or solver charges.
Runtime PostgreSQL migration/upload/reverse checks and fresh-data fidelity
evidence are documented in [SQL storage](sql-storage.md#verification-evidence).
