# Google search scraper

Scrapes Google AI Overviews and organic search results with Playwright. The CLI writes CSV, including when a page has no AI Overview. Google changes its HTML without notice, so selectors may need updating.

## Install

Requires Python 3.11 or newer.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
playwright install chromium
```

## Run

```sh
python -m aio_scraper "how tall is mount everest" --out results.csv
python -m aio_scraper --queries query_test/queries.txt --out query_test/results.csv --delay 2
```

`query_test/` contains local query files and results and is excluded from Git. Create your own query file at the path used in these examples, or substitute another path.

Without `--out`, CSV goes to stdout. `--queries` reads one query per line, skipping blank lines and `#` comments. The navigation timeout defaults to 30 seconds; change it with `--timeout` in milliseconds. `--hl` and `--gl` default to `en` and `us`.

## CSV layout

The first column, `Query`, identifies each query in input order as `P01`, `P02`, and so on. `Search Query` contains the query text. One query may produce multiple rows. The columns are:

`Query, Search Query, HL, GL, Type, Position, Title, Link, Snippet, Source, Reference Indexes, Error Code, Error Message`

Rows appear in groups by query. Within a group, AI Overview text blocks come first, then AI references, then organic results in their Google page order. `Type` distinguishes `aio_paragraph`, `aio_heading`, `aio_list_item`, `aio_reference`, `organic`, `no_results`, and `error`. `Position` is 1-based within each type of section; list items use `block.item` numbering such as `2.1` and `2.2`. `Reference Indexes` holds comma-separated, zero-based indexes of AI references, corresponding to the `aio_reference` rows in order. Empty fields mean Google did not provide that data. Text starting with a spreadsheet formula character is prefixed with an apostrophe for safer CSV opening in spreadsheet apps. A query with neither an overview nor organic results gets one `no_results` row. Query failures get an `error` row and a nonzero exit code.

Organic and AI reference links use destination URLs when Google provides them, including decoded `/url?q=...` or `/url?url=...` redirects. AI references may retain opaque `https://www.google.com/url?...` links, and both types may retain opaque `https://www.google.com/goto?url=...` links. The parser preserves opaque redirects without following them or inventing destination URLs. It matches inline AIO citations to source cards by destination URL or opaque token, ignoring Google's redirect tracking parameters. Repeated references to that destination share one reference index. Organic snippets are extracted only when Google exposes a recognized snippet element in the result card. No missing text is invented.

The prior `query_test/results_chromium.jsonl` file is a sample from the earlier JSONL output. The CLI now writes CSV; it does not convert old files automatically.

## SQL storage setup

The Supabase schema has been applied and populated on the configured remote project. It stores separate runs, queries, full AI Overviews, ordered references, and organic results. All five tables are private with RLS enabled. The server key has read/insert/update access, not delete/truncate access.

Capture and upload a new run without overwriting history:

```sh
.venv/bin/python -m run_supabase_batch --queries query_test/queries.txt \
  --out-prefix query_test/results_supabase_$(date +%Y%m%d_%H%M%S) \
  --browser chromium --proxy direct --delay 3 --timeout 90000 --upload
```

This saves lossless native JSON alongside CSV and uploads all rows atomically. `SUPABASE_ACCESS_TOKEN` is required for this Management API upload path; `SUPABASE_SECRET_KEY` can read results through the Data API. The existing `aio_scraper.py` CLI still writes CSV only. Local SQLite export and imports of older CSV files are not implemented yet.

See [batch upload commands and verification](docs/supabase-batch-workflow.md) and [SQL setup and read queries](docs/sql-storage.md). For a new project, apply both files in `supabase/migrations/` in order. The verified run has 28 queries, 25 AIOs, 191 references, 244 organic results, and no query errors.

## Proxy

Connections now default to **direct**, even when DataImpulse credentials are stored in `.env`. The CLI loads `.env` next to `aio_scraper.py`; exported variables take precedence. Set `GOOGLE_AIO_PROXY=dataimpulse` to opt in, or use `--proxy dataimpulse`. The `--proxy` option overrides `GOOGLE_AIO_PROXY` for single-query and batch runs. `--proxy direct` bypasses stored proxy credentials without deleting them.

```sh
# Chromium, direct connection, existing profile
GOOGLE_AIO_BROWSER=chromium python -m aio_scraper --queries query_test/queries.txt --proxy direct --out query_test/results_direct.csv

# Firefox, explicitly selected DataImpulse proxy, separate profile
GOOGLE_AIO_BROWSER=firefox GOOGLE_AIO_PROFILE=.aio-profile-firefox python -m aio_scraper --queries query_test/queries.txt --proxy dataimpulse --out query_test/results_proxy.csv
```

For DataImpulse, set `DATAIMPULSE_PROXY_URL` in `.env` or both `DATAIMPULSE_PROXY_USER` and `DATAIMPULSE_PROXY_PASSWORD`. The latter defaults to host `gw.dataimpulse.com`, port `823`; optionally set `DATAIMPULSE_PROXY_HOST` and `DATAIMPULSE_PROXY_PORT`. Selecting DataImpulse without credentials fails before launching the browser. Keep credentials out of command lines and commits.

Chromium navigation through DataImpulse timed out even with a 15-minute timeout in our tests. Use direct Chromium or proxy-backed Firefox with a separate profile. The scraper warns when Chromium is paired with a proxy; it never silently switches engines or connection modes. Firefox reaching Google does not guarantee a successful scrape: Google may return a CAPTCHA and require a working solver/session.

## Limitations and tests

Google's AI Overview is parsed from the observed `#eKIzJc` markup. Organic results are read from standard `.g` and `.MjjYud` cards in `#rso`; special result modules may not be captured. If no organic rows appear, the scraper logs the number of result headings and candidate cards found, so a changed Google layout can be distinguished from an empty results page. Only the English AI follow-up prompt is filtered. Pages without `#search` or `#rso` fail rather than being treated as empty results. CAPTCHA support depends on the configured environment and may fail when Google rejects a solver token.

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use synthetic HTML and mocked browser sessions and do not require live Google access. They cover opaque organic `/goto` links, AI reference `/url` redirects, reference deduplication and citation indexes across tracking variants, proxy opt-in, and CLI-over-environment precedence for single-query and batch runs.
