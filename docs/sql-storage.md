# SQL storage and Supabase review

## Deployment status

The **one-row response implementation is deployed and verified**, as of 2026-10-28. Migration `20261028032336` is recorded in Supabase history. The project is healthy; Management API access and secret-key Data API reads work. A fresh backup and PostgreSQL forward/write/rollback execution tests preceded deployment.

Only `scrape_runs` and `queries` remain as stored public tables. `ai_overviews`, `ai_references` and `organic_results` were dropped after their complete content was consolidated and verified. Both private review views retain their previous output.

## Structure: one response per row

Only two tables store data:

| Table | Purpose |
| --- | --- |
| `scrape_runs` | Shared provider, comparison UUID, repeat index and run metadata |
| `queries` | One complete response per provider/query/run, including metadata and both JSONB payloads |

`queries` retains its `(run_id, query_id)` primary key, original query/order, source snapshot, capture time, locale/device/domain, statuses/errors and warnings. New content columns are:

- `response_schema_version`: JSON envelope version 1.
- `aio_response`: JSONB object with `text`, `paragraph`, ordered `blocks` and `references`, or SQL NULL for an unobserved overview. The paragraph representation is retained for migration fidelity.
- `organic_results`: ordered JSONB array, default `[]`.

Blocks contain full paragraphs/headings, recursive lists/tables, inline links and actual citation indexes. Reference objects preserve ordinal, observed `index`, URL/title/source/snippet when present; missing indexes stay missing. Organic objects preserve ordinal separately from returned position, full title/snippet, URL/displayed URL, source/date/author, sitelinks and textual rich snippets. Optional NULL child fields are omitted. New blocks/results are array elements, not additional SQL rows or columns.

AIO status distinguishes valid absence/unavailability from empty/unresolved/invalid/error extraction. Native snapshots now give extraction failures `capture_status='unresolved'` or `'error'`, `status='error'` and a non-zero collector exit code. Valid absent/unavailable answers remain successful. Unknown locale remains NULL rather than defaulting trimmed native snapshots to en/us; normal live captures already supply observed settings. These are deliberate changes from the legacy formatter.

The uploader preserves new substantive fields inside supported AIO blocks with warnings instead of losing them or requiring new columns. Unknown block types still require explicit parser/normalizer support. Media, ads, related searches/questions, separate video/forum modules, service helpers, credentials/cookies/CAPTCHA data, redundant formatted-table representations and linked-page article extracts remain excluded. Invalid table-span placeholders are omitted with warnings. Original opaque Google redirects are not resolved or replaced.

## Verified dataset

Comparison UUID: `f7384551-5bdc-45f3-8a9a-c1d49cdb58b6`.

| Provider | Run UUID | Responses | AIOs | References | Organic results |
| --- | --- | ---: | ---: | ---: | ---: |
| custom | `1fa87bf9-4798-57c3-91da-490666f655a3` | 28 | 25 | 170 | 249 |
| serpapi | `6422fbde-a5ae-5d8b-b1c7-bfcb8fb3f026` | 28 | 23 | 206 | 246 |

These are **56 complete response rows**, not 28 merged provider responses. The previous historical run `92ae43d8-e80e-4254-ba82-fd4c39356f3f` was separately removed at the user's request. Its backup/receipt is under `query_test/historical_run_removal_20261019T155111Z/`; consolidation must not restore it.

## Read responses after consolidation

```sql
SELECT q.query_id, q.query, r.provider, q.captured_at,
       q.aio_status, q.aio_response, q.organic_results
FROM scrape_runs r
JOIN queries q ON q.run_id = r.run_id
WHERE r.comparison_id = 'f7384551-5bdc-45f3-8a9a-c1d49cdb58b6'::uuid
ORDER BY r.run_index, q.query_id, r.provider;
```

No content-child joins are required. Provider identity comes from `scrape_runs.provider`. The query returns one complete row per response. For P01/P02 side by side, use `supabase/queries/query_responses_jsonb.sql` after migration.

The existing private `search_query_review` and `search_provider_comparison` views keep their column contracts. The migration changes the first view to read JSONB instead of child tables; the paired view still returns one row per query with actual content from both providers. `supabase/queries/review_comparison.sql` remains compatible with both layouts.

## Response storage module

`supabase_results.py` owns validation, response-row projection, SQL export and
upload policy for both input paths. `normalize_response_snapshot` accepts a
complete native capture and preserves its run UUID and query order.
`normalize_records` accepts shared records and groups them into stable
provider/repeat runs. Both use the same overview, organic-result and response-row
implementation.

`comparison_storage.py` retains the comparison command and existing Python
imports, including `normalize_native_snapshot`, `normalize_records`,
`upload_records`, `execute_sql` and `sql_export`. It has no storage implementation
of its own. Its upload adapter retains the replaceable SQL executor used by
existing callers and tests.

Both upload paths use one fixed-host, redirect-refusing HTTP implementation.
Native uploads still raise `ConfigurationError`, `SnapshotError` or
`UploadError`; comparison requests retain their existing `ValueError` contract.
Native receipts contain a run UUID and logical content counts; comparison
receipts contain a comparison UUID and table-row counts. These are deliberately
distinct. Neither path retries an uncertain write.

This refactor does not change the two-table schema, saved JSON/SQL formats,
identity rules or command options. See [domain terms](../GLOSSARY.md) for native
captures, shared records and response storage.

## Capture/import

After migration, both paths write only run/query rows:

```sh
# Normalized custom/SerpAPI records. Generate the UUID ONCE per new dataset.
BATCH_ID="$(.venv/bin/python -c 'import uuid; print(uuid.uuid4())')"
.venv/bin/python -m comparison_storage \
  --input path/to/NEW_CAPTURE.main-body/records.json \
  --comparison-id "$BATCH_ID" --label 'AIO/organic comparison' \
  --output-dir query_test/NEW_SQL_EXPORT --upload
```

Without `--upload`, this only writes `data.sql`, `tables.json` and a manifest. The manifest identifies storage schema 3 and response envelope version 1; table counts are just runs and query rows. `data.sql` contains hex-encoded JSON values inside a transaction, never executable user text. Old five-table SQL seed artifacts must not be replayed against the consolidated schema.

The custom native-snapshot collector's `--upload`/`--upload-file` also uses this contract and preserves the capture's run UUID and original order. Native writes use `normalize_response_snapshot`, the same one-row contract as normalized comparison uploads. Summary keys such as `ai_references` count JSON objects, not SQL rows. New imports add metadata `schema_version=3`; migrated runs retain their original metadata version/fingerprint as source provenance. Each consolidated response has `response_schema_version=1`. Fingerprints identify response content plus provenance within a run; they are not cross-provider quality/completeness scores.

Each upload is one parameterized, atomic statement. Duplicate UUIDs/query identities fail, never overwrite. Missing parent runs cause a NOT NULL failure, not silently skipped responses. Complete snapshots are required. Errors are sanitized. Writes never retry automatically. A failed response can still mean the server committed: inspect the retained UUID remotely before retrying. Do not invent another UUID to bypass an uncertain write. Output directories must be new; intent/SQL/manifest are written before upload and `upload.json` records a successful receipt.

## Private access

- `SUPABASE_PROJECT_ID`: intended active project reference.
- `SUPABASE_ACCESS_TOKEN`: server-only personal access token for Management API setup/atomic upload.
- `SUPABASE_SECRET_KEY`: server-only Data API reads via the `apikey` header.

Never send these keys to a browser or paste them in chat. Exported environment values win; duplicate `.env` entries use the last value. Credentials were not edited.

Both tables retain RLS with no client policies. `PUBLIC`, `anon` and `authenticated` cannot access tables/views. `service_role` has SELECT/INSERT/UPDATE, not DELETE/TRUNCATE, on tables and SELECT only on views. Views use `security_invoker=true`; no public policy or ingestion RPC is added. Secret keys bypass RLS and must stay private. The fixed-host Management transport rejects redirects and automatic retries.

## Migrations and rollback

The current schema requires **all four** migrations, in order:

1. `supabase/migrations/202609300001_results_storage.sql`
2. `supabase/migrations/202609300002_restrict_service_role.sql`
3. `supabase/migrations/20261019152000_scoped_comparison_storage.sql`
4. `supabase/migrations/20261028032336_query_response_jsonb.sql` — deployed and PostgreSQL-runtime-verified.

For an existing project, inspect migration history and take a fresh backup first. The earlier project recorded the first two under `20260930031633` and `20260930031938`, the third as `20261019152000`, and the fourth as `20261028032336`. Reconcile those historical filenames/versions before CLI migration workflows; do not blindly replay them or edit applied migrations.

The fourth migration locks/backfills/checks all rows, preserves every original run/query field, repoints the review view and only then drops `ai_references`, `organic_results` and `ai_overviews`. No CASCADE is used: unexpected dependencies abort the transaction. Array counts and exact JSON content/order must match source before commit.

Optional rollback SQL lives outside forward migrations at `supabase/rollbacks/20261028032336_query_response_jsonb.sql`. It reconstructs from **current** JSON, checks an exact round trip and refuses JSON extensions the old schema cannot represent. Stop writers and restore old application code before rollback; repair migration history separately. Both forward and rollback SQL passed PostgreSQL execution tests before deployment. Any future edits or use against another dataset require the same fresh-backup/runtime checks.

## Verification evidence

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q comparison_storage.py response_sql.py supabase_results.py run_supabase_batch.py
```

Local tests cover one row containing 200 paragraphs and 200 organic results, native tables/sparse indexes/long snippets, missing locale, capture-status distinctions, duplicate refusal, parameterized values, credential redaction, versioned offline exports and receipt validation. SQL text-contract checks do not substitute for PostgreSQL execution.

An offline conversion of the last saved 56-response snapshot matches the canonical extraction records, including ordered blocks, citation indexes/snippets, organic positions/metadata and timestamps. Logical totals remain 48 AIOs, 376 references and 495 organic results. Candidate evidence: `query_test/jsonb_consolidation_prepared_20261028T032336Z/`. This is **not** a fresh backup or deployment receipt. A fresh-context source review was performed. This initial offline evidence is superseded by the fresh deployment verification below.

Fresh deployment evidence is under `query_test/jsonb_migration_20261028T053302Z/`: full backup, expected consolidated documents, forward/write/reverse rollback test and receipt, guarded apply SQL, committed migration receipt, after snapshot and final verification. Both uploaders were exercised as `service_role`; duplicate, invalid-JSON and missing-parent writes failed atomically. Fresh reads proved the test transaction left no changes. The committed migration retained all 56 responses, their original metadata, every JSON field/array order and both review-view outputs. Secret-key Data API reads match all 56 documents. RLS/private grants and removal of all three old table names were verified. The 182 local tests passed before deployment.

Historical evidence for the previous five-table migration remains under `query_test/sql_migration_20261019T151452Z/`. No additional Google/SerpAPI calls, credential edits or historical-data restoration were made for migration or workflow cleanup. Captures/backups remain outside git. SQLite and older CSV imports remain unsupported.

## Official references

- https://supabase.com/docs/reference/api/v1-run-a-query
- https://supabase.com/docs/guides/api/api-keys
- https://supabase.com/docs/guides/database/postgres/row-level-security#views
- https://supabase.com/docs/guides/deployment/database-migrations
- https://www.postgresql.org/docs/current/datatype-json.html
- https://www.postgresql.org/docs/current/functions-json.html
