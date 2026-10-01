# Run and upload a batch

## Usage

Capture and upload a new batch:

```bash
.venv/bin/python -m run_supabase_batch --queries query_test/queries.txt \
  --out-prefix query_test/results_supabase_$(date +%Y%m%d_%H%M%S) \
  --browser chromium --proxy direct --delay 3 --timeout 90000 --upload
```

Omit `--upload` to capture locally without writing to Supabase. The output prefix must be new. Each completed query is checkpointed to `.json` and appended to `.csv`. An interrupted snapshot cannot be uploaded if it has fewer results than planned.

Upload a complete saved native snapshot without scraping again:

```bash
.venv/bin/python -m run_supabase_batch --upload-file path/to/new_snapshot.json
```

An already uploaded UUID cannot be uploaded again. A failed network request may have committed before the response was lost, so inspect `scrape_runs` for that UUID before retrying. The uploader never retries writes or generates a replacement UUID automatically. Do not use the verification snapshot below as an upload example; it is already stored.

Both upload commands use `SUPABASE_PROJECT_ID` and `SUPABASE_ACCESS_TOKEN` from `.env`. This implementation uses the Management API, not a Data API ingestion RPC. `SUPABASE_SECRET_KEY` is sufficient for reading the populated tables but is not used for this upload path. Keep the management token available for these commands or arrange a narrower ingestion RPC separately.

Exit 0 means clean capture/upload; exit 1 means query errors were retained explicitly, even if upload succeeded; exit 2 means configuration, local capture, validation, or upload failed. Local artifacts remain available after failures. Summary JSON reports `uploaded` when the upload completes.

The optional `google-aio-supabase` console command is registered after `pip install -e .`. `python -m run_supabase_batch` works directly from this repository without reinstalling.

## Verified live run

On 2026-09-30, all 28 queries completed successfully in 277.4 seconds using direct headless Chromium and the existing browser profile. The uploaded run is `92ae43d8-e80e-4254-ba82-fd4c39356f3f`:

- 28 query rows, no errors.
- 25 AI Overview rows.
- 191 ordered reference rows.
- 244 ranked organic result rows.

Local artifacts are `query_test/results_supabase_20260930_115740.csv` and `.json`, with an `.upload.json` verification receipt. Every remote result row was compared to the native snapshot. Full text, all structured blocks, citation indexes, source order, metadata fingerprint, and capture time match. Secret-key reads work on all five tables; RLS and private role grants remain unchanged. No previous runs were replaced.

84 unit tests pass, and the uploader SQL was tested on both local PostgreSQL and the remote Supabase Management API.

## Contract

Run `query_test/queries.txt` with direct Chromium and the existing browser profile. Save CSV for spreadsheet use and a lossless native JSON snapshot containing the run UUID, UTC batch-start time, source filename, query IDs, and parser results. Full AIO blocks and lists must be preserved, not reconstructed from CSV. Query failures are stored explicitly with sanitized error messages.

Upload a new run, never replace or delete prior runs. Parameterize all values. Insert all five tables atomically with a single PostgreSQL statement through the configured project-scoped Management API endpoint. The request uses `SUPABASE_ACCESS_TOKEN`; no credentials go in data, command arguments, or logs. Do not retry writes automatically after uncertain transport failures. Retain the snapshot UUID for troubleshooting and retries. A repeated successful upload must fail on the existing run UUID rather than duplicate/overwrite results.

Validate query IDs/ordinals, reference indexes, organic positions, block types, and citation bounds before upload. Normalize only known result fields. JSON array order and all answer text remain intact. Uploaded run metadata may contain counts and a snapshot hash, but not arbitrary environment values, cookies, User-Agent context, proxy credentials, CAPTCHA tokens, or challenge URL query parameters.

## Files and checks

- `supabase_results.py`: validated normalization and atomic upload of a native snapshot.
- `run_supabase_batch.py`: batch runner that uses `GoogleAIOScraper` and writes progressive CSV/JSON output.
- Tests under `tests/`, using `unittest`, local temporary files, and mocked HTTP responses. No external writes in unit tests.
- Runtime verification compares run-scoped row counts, JSON blocks, ranks, and citation indexes with the local snapshot and confirms previous runs are untouched.

This workflow does not implement local SQLite export or automatic upload from the existing `aio_scraper.py` CLI. Those remain separate work. Supabase secrets stay in `.env` and tables remain private.
