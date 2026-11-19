# Google AIO Scraper

Google AIO Scraper is a Python command-line tool for collecting and comparing Google's AI Overview answers and organic search results.

- Collect results with a custom Playwright browser scraper.
- Fetch the same queries through SerpAPI and compare the responses.
- Run query batches and repeat rounds while tracking each query occurrence.
- Save answer text, lists, tables, citations and ordered search results as JSON.
- Replay and normalize saved captures without making new paid requests.
- Export PostgreSQL SQL or upload complete responses atomically to Supabase.
- Browse saved responses in a read-only local viewer, or host them as a deliberately public archive on Cloudflare Workers.
- Use optional DataImpulse proxies, 2Captcha solving and persistent browser sessions.

## Six commands

`pyproject.toml` installs six console commands. The default environment for all of them is `uv run` from the repository directory.

| Command | What it does | Network use |
| --- | --- | --- |
| `google-aio` | Custom browser capture of one query or a batch, optional upload | Google via browser; optional Supabase write |
| `google-aio-compare` | Paired custom + SerpAPI capture with structural comparison, or offline comparison of a saved pair | Google via browser **and paid SerpAPI**, or none in offline mode |
| `google-aio-export` | Normalize saved native captures into the shared JSON format | None |
| `google-aio-resolve` | Resolve opaque Google redirect links (ad-hoc or across a saved capture) by observing Google's redirect responses | Google via browser; never downloads destination pages |
| `google-aio-sql` | Validate shared records, export SQL, optionally upload | Optional Supabase write |
| `google-aio-web` | Local read-only viewer for stored responses | Supabase reads only |

Safe offline commands (`google-aio-export`, `google-aio-compare --scraper-json/--serpapi-json`, `google-aio-sql` without `--upload`) make no Google, SerpAPI or database requests. Live capture can spend SerpAPI, proxy and CAPTCHA credits. Database writes are never retried automatically.

## Installation

The commands below use a macOS/Linux shell. On Windows, use `uv run` the same way; uv creates a platform-specific `.venv`, so no shell activation is needed.

1. Clone the repository and enter it:

   ```sh
   git clone https://github.com/F1lmzy/serp-api-custom.git
   cd serp-api-custom
   ```

2. Create the environment and install dependencies:

   ```sh
   uv sync
   ```

   This creates `.venv` from `uv.lock` and uses the Python version pinned in `.python-version` (3.12), downloading it if needed. Run `uv sync` again after pulling dependency changes; use `uv lock` or `uv add` when you change dependencies.

3. Install the browsers for live capture. Skip this step for offline JSON processing, uploads or browsing only:

   ```sh
   uv run playwright install chromium firefox
   ```

4. If you do not already have a `.env` file, copy the template:

   ```sh
   cp .env.example .env
   ```

   Edit `.env` and fill in only the credentials from the table below that your workflow needs. Exported environment variables override `.env` values. If a setting appears more than once in the file, the last value wins. Never commit credentials or put them directly in commands.

5. If you plan to upload, follow the [database setup instructions](docs/sql-storage.md#migrations-and-rollback). For an existing project, inspect its migration history and take a backup before applying anything.

6. Confirm that the installed commands load without making capture or upload requests:

   ```sh
   uv run google-aio --help
   uv run google-aio-compare --help
   uv run google-aio-export --help
   uv run google-aio-resolve --help
   uv run google-aio-sql --help
   uv run google-aio-web --help
   ```

## Requirements

Only configure the services you plan to use. Local JSON processing does not need service credentials or a browser installation.

| Requirement | When needed | Version or configuration |
| --- | --- | --- |
| Git | Clone the repository | A local Git installation |
| uv | All commands | A current [uv](https://docs.astral.sh/uv/getting-started/installation/) release; manages the environment and locked dependencies |
| Python | All commands | 3.11 or newer; `.python-version` pins 3.12, which uv installs automatically if needed |
| Python packages | All commands | Beautiful Soup `>=4.12,<5` and Playwright `>=1.45,<2`, installed from `uv.lock` |
| Playwright browser | Live custom capture, including paired comparisons | Chromium for direct access; Firefox for the recommended DataImpulse configuration |
| SerpAPI account with credits | Live paired comparisons | `SERPAPI_API_KEY` |
| DataImpulse account | Optional proxy access | `DATAIMPULSE_PROXY_URL`, or `DATAIMPULSE_PROXY_USER` and `DATAIMPULSE_PROXY_PASSWORD` |
| 2Captcha account with credits | Optional automatic CAPTCHA solving | `TWOCAPTCHA_API_KEY` |
| Supabase project with the database schema applied | Uploads | `SUPABASE_PROJECT_ID` and `SUPABASE_ACCESS_TOKEN`, a personal Management API token |
| Supabase project secret key | Private Data API reads used by the local viewer and the hosted Worker | `SUPABASE_SECRET_KEY`; this does not replace the upload token |

Two different Supabase credentials are involved. `SUPABASE_ACCESS_TOKEN` is a personal Management API token used for uploads and SQL execution. `SUPABASE_SECRET_KEY` is the project's Data API service key used for reads. They are not interchangeable; see [private access](docs/sql-storage.md#private-access).

## Workflows

### Custom capture

Create a UTF-8 file named `queries.txt` with one query per line. Blank lines and lines starting with `#` are ignored. Repeated query text remains a separate occurrence with its own positional query ID.

```sh
uv run google-aio "what causes a solar eclipse" \
  --out-prefix query_test/my-custom \
  --browser chromium --proxy direct
```

Use `--queries queries.txt` for a batch. This saves `query_test/my-custom.json`, a complete native snapshot with its run UUID and ordered responses; it is checkpointed after each query. Add `--upload` to upload after capture, or `--upload-file query_test/my-custom.json` to upload a saved snapshot later without scraping again.

For DataImpulse, use `--browser firefox --proxy dataimpulse` and set `GOOGLE_AIO_PROFILE=.aio-profile-firefox`. Keep separate profiles for each browser. Stored proxy credentials alone do not enable a proxy. [Proxy and CAPTCHA configuration](docs/scraping.md#proxy-and-captcha-configuration) has the full details.

### Paired custom/SerpAPI comparison

```sh
uv run google-aio-compare \
  --queries queries.txt \
  --out-prefix query_test/my-comparison \
  --browser chromium --proxy direct
```

This runs both providers for each query and saves:

| File | Contents |
| --- | --- |
| `query_test/my-comparison.scraper.json` | Native custom captures |
| `query_test/my-comparison.serpapi.json` | Native SerpAPI captures |
| `query_test/my-comparison.comparison.json` | Timings, statuses and structural differences |
| `query_test/my-comparison.main-body/records.json` | Both providers' responses in the shared JSON format |

Each paired query uses one SerpAPI search request and at most one AI Overview follow-up. Choose a new output prefix for every capture; existing paths are refused. Add `--runs 2` to repeat the query list, or use a quoted positional query for a single pair. This command saves files only; it does not upload.

To compare two saved native single-query responses without a browser, network or credits, use the [offline mode](docs/scraping.md#saved-input-replay).

### Normalize saved inputs

Convert saved native captures (custom or SerpAPI) into the shared `records.json` format. This is offline and credential-free:

```sh
uv run google-aio-export --provider custom \
  --input query_test/my-custom.json \
  --output-dir query_test/my-normalized
```

Use `--provider serpapi` for saved SerpAPI captures. The output directory contains only `records.json`. Already-shared `records.json` files go directly to `google-aio-sql`; do not normalize them again.

### SQL export and optional upload

For a paired capture, generate a comparison UUID once and retain it with the dataset:

```sh
COMPARISON_ID="$(uv run python -c 'import uuid; print(uuid.uuid4())')"
uv run google-aio-sql \
  --input query_test/my-comparison.main-body/records.json \
  --comparison-id "$COMPARISON_ID" \
  --output-dir query_test/my-sql-export \
  --upload
```

The command writes `data.sql`, `tables.json` and `manifest.json` before uploading, then writes `upload.json` on success. Omit `--upload` to export locally only. The output directory must be new.

Both providers insert in one atomic database operation. Duplicate identities fail rather than overwrite. If a request fails after it may have reached the server, inspect the retained UUID remotely before retrying. Do not generate a new UUID to bypass an uncertain write. [SQL storage](docs/sql-storage.md) documents the schema, private access and recovery.

### Browse saved responses

Two viewers read the same two-table schema; both are read-only and never spend capture credits.

**Local viewer (private).** Start it with:

```sh
uv run google-aio-web
```

Open **http://127.0.0.1:8000**. Select a capture run, search its query list, and click a response to see a Google-style reconstruction of its AI Overview, citations, source cards and organic results. The **Raw JSON** tab shows the complete database row or either JSONB entry. Set `SUPABASE_PROJECT_ID` and `SUPABASE_SECRET_KEY` in `.env` first; the key stays on the Python server and is never sent to the browser. The server binds only to `127.0.0.1` and has no public-host option or login. Use `--port 8080` to choose another port. See [local viewer guide](docs/web-viewer.md) for filters, cross-run comparison and security boundaries.

**Hosted archive (deliberately public).** The repository also deploys a read-only Cloudflare Worker that serves the same frontend and API. **It has no login: anyone can browse every stored run and inspect full raw responses.** Private tables and server-side credentials do not make the hosted responses private. Do not deploy it against data that needs to stay secret. See [Cloudflare deployment](docs/cloudflare-workers.md) for Git-based setup, runtime secrets and the custom domain.

## Understand the data and limits

Supabase stores data in two tables. `scrape_runs` holds provider and run metadata. Each `queries` row holds one provider's complete scoped response for one query occurrence, with AI Overview content in `aio_response` and ordered search results in `organic_results`. Both are JSONB values, so lists, tables and citations do not need separate database rows. [SQL storage](docs/sql-storage.md) documents every column, key, check, view and grant, and distinguishes the current schema from the three retired child tables.

The project preserves full scoped text and array order. It excludes ads, related questions/searches, media assets and linked-page article text. Failed captures and unfinished AI Overviews remain explicit rather than appearing as successful searches with no answer.

Google can change its markup or block a session. A solver token does not guarantee access. Differences between providers are observations, not proof of a scraper defect: their IPs, sessions and capture times can differ.

Keep credentials and `.aio-profile*` directories private. Browser profiles contain session state. Do not use an authenticated personal profile for CAPTCHA-solving workflows.

## Run checks

```sh
uv run python -m unittest discover -s tests -v
```

The default suite uses synthetic HTML and mocked browser/HTTP sessions, not paid services. Real Firefox checks are opt-in; see [verification](docs/scraping.md#verification).

For the Worker, `npm ci && npm run check && npm test && npm run build` runs syntax checks, unit tests and a deploy dry run without publishing anything.

## Further reading

- [Scraping, the capture pipeline and the shared JSON format](docs/scraping.md)
- [Query state transitions, ownership and walkthroughs](docs/state-flow.md)
- [Supabase schema, private access, migrations and recovery](docs/sql-storage.md)
- [Local viewer guide](docs/web-viewer.md) · [Cloudflare Worker deployment](docs/cloudflare-workers.md) · [Viewer visual fidelity and its limits](docs/visual-fidelity.md)
- [Offline refactor verification (historical record)](docs/refactor-verification.md)
- [Project terminology](GLOSSARY.md) · [Changelog](CHANGELOG.md)
