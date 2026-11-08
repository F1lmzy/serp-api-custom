# Google AIO Scraper

Google AIO Scraper is a Python command-line tool for researchers and developers who want to collect and compare Google's AI Overview answers and organic search results.

- Collect results with a custom Playwright browser scraper.
- Fetch the same queries through SerpAPI and compare the responses.
- Run query batches and repeat rounds while tracking each query occurrence.
- Save answer text, lists, tables, citations and ordered search results as JSON.
- Replay and normalize saved captures without making new paid requests.
- Export PostgreSQL SQL or upload complete responses atomically to Supabase.
- Use optional DataImpulse proxies, 2Captcha solving and persistent browser sessions.

## Requirements

Only configure the services you plan to use. Local JSON processing does not need service credentials or a browser installation.

| Requirement | When needed | Version or configuration |
| --- | --- | --- |
| Git | Clone the repository | A local Git installation |
| Python | All commands | 3.11 or newer |
| Python packages | All commands | Beautiful Soup `>=4.12,<5` and Playwright `>=1.45,<2`, installed by pip |
| Playwright browser | Live custom capture, including paired comparisons | Chromium for direct access; Firefox for the recommended DataImpulse configuration |
| SerpAPI account with credits | Live paired comparisons | `SERPAPI_API_KEY` |
| DataImpulse account | Optional proxy access | `DATAIMPULSE_PROXY_URL`, or `DATAIMPULSE_PROXY_USER` and `DATAIMPULSE_PROXY_PASSWORD` |
| 2Captcha account with credits | Optional automatic CAPTCHA solving | `TWOCAPTCHA_API_KEY` |
| Supabase project with the database schema applied | Uploads | `SUPABASE_PROJECT_ID` and `SUPABASE_ACCESS_TOKEN`, a personal Management API token |
| Supabase project secret key | Optional private Data API reads | `SUPABASE_SECRET_KEY`; this does not replace the upload token |

Live capture can spend SerpAPI, proxy and CAPTCHA credits. Each paired query uses one SerpAPI search request and at most one AI Overview follow-up. SerpAPI requests and database writes never retry automatically.

## Capture results

Complete the [installation steps](#installation) before running these commands. Run them from the repository directory with your virtual environment active.

### Compare both providers

Create a UTF-8 file named `queries.txt` with one query per line. Blank lines and lines starting with `#` are ignored. Repeated query text remains a separate occurrence.

```sh
google-aio-compare \
  --queries queries.txt \
  --out-prefix query_test/my-comparison \
  --browser chromium --proxy direct
```

This runs both providers and saves:

| File | Contents |
| --- | --- |
| `query_test/my-comparison.scraper.json` | Native custom captures |
| `query_test/my-comparison.serpapi.json` | Native SerpAPI captures |
| `query_test/my-comparison.comparison.json` | Timings, statuses and structural differences |
| `query_test/my-comparison.main-body/records.json` | Both providers' responses in the shared JSON format |

Choose a new output prefix for every capture. Native files are checkpointed after each query pair. Credentials and short-lived tokens are redacted. Add `--runs 2` to repeat the query list twice, or replace `--queries queries.txt` with a quoted query for a single pair.

This command saves files only. It does not upload to Supabase.

### Use the custom scraper alone

```sh
google-aio "what causes a solar eclipse" \
  --out-prefix query_test/my-custom \
  --browser chromium --proxy direct
```

This saves `query_test/my-custom.json`, including its run UUID and ordered responses. Use `--queries queries.txt` instead of the quoted query for a batch. Add `--upload` to upload after capture.

For DataImpulse, use `--browser firefox --proxy dataimpulse` and set `GOOGLE_AIO_PROFILE=.aio-profile-firefox`. Keep separate profiles for each browser. Proxy credentials alone do not enable a proxy.

## Work with saved results

### Upload to Supabase

For a paired capture, generate a comparison UUID once and retain it with the dataset:

```sh
COMPARISON_ID="$(python -c 'import uuid; print(uuid.uuid4())')"
google-aio-sql \
  --input query_test/my-comparison.main-body/records.json \
  --comparison-id "$COMPARISON_ID" \
  --output-dir query_test/my-sql-export \
  --upload
```

The command writes `data.sql`, `tables.json` and `manifest.json` before uploading, then writes `upload.json` on success. Omit `--upload` to export locally only. The output directory must be new.

Both providers insert in one atomic database operation. Duplicate identities fail rather than overwrite. If a request fails after it may have reached the server, inspect the retained UUID before retrying. Do not generate a new UUID to bypass an uncertain write.

For a complete custom-only snapshot, upload directly without scraping again:

```sh
google-aio --upload-file query_test/my-custom.json
```

### Normalize or compare offline

Convert a saved native custom capture into shared `records.json`:

```sh
google-aio-export --provider custom \
  --input query_test/my-custom.json \
  --output-dir query_test/my-normalized
```

Use `--provider serpapi` for saved SerpAPI captures. Shared `records.json` files go directly to `google-aio-sql`; do not normalize them again. [Saved-input replay](docs/scraping.md#saved-input-replay) also covers offline comparison of a native single-query pair.

## Understand the data and limits

Supabase stores data in two tables. `scrape_runs` holds provider and run metadata. Each `queries` row holds one provider's complete scoped response for one query occurrence, with AI Overview content in `aio_response` and ordered search results in `organic_results`. Both are JSONB values, so lists, tables and citations do not need separate database rows.

The project preserves full scoped text and array order. It excludes ads, related questions/searches, media assets and linked-page article text. Failed captures and unfinished AI Overviews remain explicit, rather than appearing as successful searches with no answer.

Google can change its markup or block a session. A solver token does not guarantee access. Differences between providers are observations, not proof of a scraper defect: their IPs, sessions and capture times can differ.

Keep credentials and `.aio-profile*` directories private. Browser profiles contain session state. Do not use an authenticated personal profile for CAPTCHA-solving workflows.

## Run checks

```sh
python -m unittest discover -s tests -v
```

The default suite uses synthetic HTML and mocked browser/HTTP sessions, not paid services. Real Firefox checks are opt-in; see [verification](docs/scraping.md#verification).

## Further reading

- [Scraping, proxy/CAPTCHA settings and JSON format](docs/scraping.md)
- [Supabase schema setup, private access and recovery](docs/sql-storage.md)
- [Project terminology](GLOSSARY.md)
- [Changelog](CHANGELOG.md)

## Installation

The commands below use a macOS/Linux shell. On Windows, activate the virtual environment with `.venv\Scripts\Activate.ps1` in PowerShell instead of `source`.

1. Clone the repository and enter it:

   ```sh
   git clone https://github.com/F1lmzy/serp-api-custom.git
   cd serp-api-custom
   ```

2. Create a virtual environment with Python 3.11 or newer, activate it and install the package:

   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -e .
   ```

3. Install the browsers for live capture. Skip this step for offline JSON processing or uploads only:

   ```sh
   python -m playwright install chromium firefox
   ```

4. If you do not already have a `.env` file, copy the template:

   ```sh
   cp .env.example .env
   ```

   Edit `.env` and fill in only the credentials listed in the requirements table for your workflow. Exported environment variables override `.env` values. If a setting appears more than once in the file, the last value wins. Never commit credentials or put them directly in commands.

5. If you plan to upload, follow the [database setup instructions](docs/sql-storage.md#migrations-and-rollback). For an existing project, inspect its migration history and take a backup before applying anything.

6. Confirm that the installed commands load without making capture or upload requests:

   ```sh
   google-aio --help
   google-aio-compare --help
   google-aio-export --help
   google-aio-sql --help
   ```
