# SQL storage and Supabase review

The response store is two private PostgreSQL tables in a Supabase project.
This page is the schema reference, derived from the four cumulative
migrations in `supabase/migrations/`, cross-checked against the application
validation in `supabase_results.py` and the write contract in
`response_sql.py`.

**Verification status.** The consolidated schema was deployed and
PostgreSQL-runtime-verified on **2026-10-28**; the receipts and evidence from
that work are linked below under [Verification evidence](#verification-evidence).
Those are records of a past deployment, not a live health check: no PostgreSQL
runtime or remote state has been verified by this documentation. Migration
`20261028032336` is recorded in the target project's history.

## Structure: one response per row

Each `queries` row is one provider's complete scoped response for one query
occurrence in one run. `scrape_runs` identifies the provider, comparison and
repeat round.

| Table | Purpose |
| --- | --- |
| `scrape_runs` | Provider, comparison UUID, repeat index and run metadata |
| `queries` | One complete response per provider/query/run, including statuses and both JSONB payloads |

The three former child tables (`ai_overviews`, `ai_references`,
`organic_results`) were dropped after their content was consolidated into
JSONB and verified; see [Retired child tables](#retired-child-tables). They
are recreated only by the guarded rollback.

### `scrape_runs`

| Column | Type | Default | Null | Constraints |
| --- | --- | --- | --- | --- |
| `run_id` | uuid | `gen_random_uuid()` | no | primary key |
| `label` | text | `''` | no | |
| `source_file` | text | `''` | no | |
| `captured_at` | timestamptz | — | yes | |
| `created_at` | timestamptz | `now()` | no | **database-managed**; never supplied by uploads |
| `metadata` | jsonb | `'{}'::jsonb` | no | `CHECK (jsonb_typeof(metadata) = 'object')` |
| `comparison_id` | uuid | — | yes | groups a paired dataset |
| `provider` | text | `'custom'` | no | `CHECK (provider IN ('custom', 'serpapi'))` |
| `run_index` | integer | `1` | no | `CHECK (run_index > 0)` |

Unique index `scrape_runs_comparison_provider_round_uidx` on
`(comparison_id, provider, run_index)`: one run per provider per repeat round
per comparison. `scrape_runs_created_at_idx` orders runs by creation time.
`ON DELETE CASCADE` from `queries.run_id` removes a run's responses with it.

A custom native capture keeps the run UUID generated at capture time. A
comparison upload derives each run's UUID deterministically
(`uuid5(comparison_id, '<provider>:<run_index>')` in `normalize_records`), so
rounds never overwrite each other and duplicates fail instead of mutating.

### `queries`

| Column | Type | Default | Null | Constraints |
| --- | --- | --- | --- | --- |
| `run_id` | uuid | — | no | references `scrape_runs(run_id)` on delete cascade |
| `query_id` | text | — | no | `CHECK (length > 0)`; part of primary key |
| `ordinal` | integer | — | no | `CHECK (ordinal > 0)`; unique per run |
| `query` | text | — | no | `CHECK (length > 0)` |
| `hl`, `gl` | text | `'en'` / `'us'` | **yes** | `NOT NULL` dropped in migration 3: unknown observed locale stays NULL, not a invented value |
| `status` | text | `'ok'` | no | `CHECK (status IN ('ok', 'error'))`; stored projection of capture+AIO outcome |
| `error_code` | text | — | yes | |
| `error_message` | text | — | yes | sanitized at upload |
| `snapshot_id` | text | — | yes | `CHECK (NULL or length > 0)`; unique per run |
| `captured_at` | timestamptz | — | yes | |
| `capture_status` | text | — | yes | `CHECK (IN ('ok', 'error', 'unresolved'))` |
| `aio_status` | text | — | yes | `CHECK (IN ('present','absent','unavailable','empty','unresolved','invalid','error'))` |
| `device` | text | — | yes | |
| `google_domain` | text | — | yes | |
| `warnings` | jsonb | `'[]'::jsonb` | no | `CHECK (array)` |
| `response_schema_version` | integer | `1` | no | `CHECK (= 1)` |
| `aio_response` | jsonb | — | yes | shape-checked; see below |
| `organic_results` | jsonb | `'[]'::jsonb` | no | `CHECK (array)` |

Keys and indexes:

- Primary key `(run_id, query_id)` — a stored response is unique per run and
  query occurrence.
- Unique `(run_id, ordinal)` — input order is stored, not incidental.
- Unique `(run_id, snapshot_id)` — a snapshot identity can be stored once.

Content checks added by the final migration:

- `aio_response` must be an object whose `blocks` and `references` are arrays
  (or be SQL NULL).
- `aio_status = 'present'` **requires** at least one block
  (`queries_present_aio_content`).
- `aio_status` in (`'absent'`, `'unavailable'`) requires `aio_response` to be
  NULL or have zero blocks (`queries_absent_aio_content`) — absence cannot
  carry an answer, and presence cannot be empty.

Application-side validation (`supabase_results.py`) is deliberately stricter
or equal: complete snapshots only, non-empty query/snapshot identities,
lowercase UUID run IDs, `ordinal` assigned in input order, `status='ok'`
only when the capture succeeded **and** the AIO status is
present/absent/unavailable, warnings as an array of text, and absolute
http(s) links. `response_sql.py`'s `TABLE_COLUMNS` lists the columns uploads
supply (everything above except `created_at`); it is the write projection,
not the definition of the schema.

## JSONB content shapes

`aio_response` is NULL **iff** no overview was observed; `organic_results`
defaults to `[]` when nothing was retained. A failed or unresolved capture is
an explicit row with `capture_status`, `error_code`/`error_message` and its
(possibly partial) JSONB — never a silently missing row.

Representative `aio_response` (present, with a sparse citation index):

```json
{
  "text": "Paragraph text.\n\n• First point\n• Second point",
  "paragraph": "Paragraph text.",
  "blocks": [
    {"type": "paragraph", "snippet": "Paragraph text.", "reference_indexes": [0, 2]},
    {"type": "list", "items": ["First point", "Second point"], "ordered": true, "reference_indexes": [2]}
  ],
  "references": [
    {"ordinal": 1, "index": 0, "title": "Example - Wikipedia", "source": "Wikipedia",
     "link": "https://en.wikipedia.org/wiki/Example"},
    {"ordinal": 2, "index": 2, "link": "https://example.org/guide", "snippet": "A full saved snippet."}
  ]
}
```

- `text` is the fully rendered answer; `paragraph` is the concatenated
  paragraphs, retained for migration fidelity. Blocks preserve paragraphs,
  headings, recursive lists (with `ordered`/`start`), tables (with
  `table`/`detailed`, `colspan`/`rowspan`, `is_header`) and inline
  `snippet_links`.
- `references` preserve display order (`ordinal`), the **observed**
  `index` (possibly sparse or missing entirely), `link` (required,
  validated), and `title`/`source`/`snippet` when present. Null fields are
  omitted, not stored as JSON null.
- Zero or unmapped citation indexes are preserved and warned about
  (`unmapped_citation_index`, `duplicate_reference_index`), never repaired.
- New substantive fields inside supported blocks are kept with warnings;
  unsupported block types are rejected. Media, ads, related
  questions/searches, service helpers and credentials are excluded upstream.

Representative `organic_results` entry:

```json
[
  {"ordinal": 1, "position": 1, "title": "Example title",
   "link": "https://example.com/a", "displayed_link": "https://example.com › a",
   "snippet": "A full saved snippet.", "source": "Example", "date": "Nov 1, 2026"}
]
```

`ordinal` is storage order and is always present; `position` is the
provider's returned rank and may be missing. `sitelinks` and `rich_snippet`
are structured sub-objects when present. A live capture may replace opaque
Google redirect links with destinations observed from Google's own redirect
responses, retaining the original as `redirect_link`; unresolved links stay
opaque and their destinations are never guessed or fetched after the fact.

## Review views

Two private views read the tables; both keep their pre-consolidation column
contracts.

- `search_query_review`: one row per stored response. After migration 4 it
  reads JSONB directly: `aio_text`, `aio_blocks`, `aio_block_count`,
  `aio_references`, `reference_count`, `organic_results`, `organic_count`,
  plus all identity/status columns.
- `search_provider_comparison`: one row per paired query, `FULL JOIN`ing the
  custom and serpapi review rows on `(comparison_id, run_index, query_id)`.
  Both count columns are `bigint` to retain the old result types.

`security_invoker = true` on both. `supabase/queries/review_comparison.sql`
queries the paired view; `supabase/queries/query_responses_jsonb.sql` shows
complete-response and P01/P02 side-by-side reads. No content-child joins are
needed anywhere.

## Private access

- `SUPABASE_PROJECT_ID`: intended active project reference.
- `SUPABASE_ACCESS_TOKEN`: server-only personal access token for the
  Management API (uploads and SQL execution).
- `SUPABASE_SECRET_KEY`: server-only Data API key for reads via the `apikey`
  header; used by the local viewer and the hosted Worker.

Never send these keys to a browser or paste them in chat. Exported
environment values win; duplicate `.env` entries use the last value.

Both tables have RLS enabled with **no policies**, so `anon` and
`authenticated` roles see nothing. `PUBLIC`, `anon` and `authenticated` are
revoked from the tables and views. `service_role` has
SELECT/INSERT/UPDATE on the tables — `DELETE`/`TRUNCATE` are explicitly
revoked — and SELECT only on the views. The Data API secret bypasses RLS,
which is exactly why it must stay server-side. The Management API transport
refuses redirects and never retries. Each migration ends with
`NOTIFY pgrst, 'reload schema'` to refresh the Data API cache.

Private tables and server-side keys do **not** make the hosted Worker's
responses private: the Worker is deliberately public and reads through its
own server-side key. See [Cloudflare deployment](cloudflare-workers.md) and
[the local viewer](web-viewer.md).

## Migrations and rollback

The current schema requires **all four** migrations, in order:

1. `supabase/migrations/202609300001_results_storage.sql` — creates the two
   tables (plus the three child tables, since dropped), checks, indexes, RLS
   and service_role grants.
2. `supabase/migrations/202609300002_restrict_service_role.sql` — resets
   service_role to SELECT/INSERT/UPDATE without DELETE/TRUNCATE.
3. `supabase/migrations/20261019152000_scoped_comparison_storage.sql` — adds
   comparison identity (`comparison_id`, `provider`, `run_index`,
   `snapshot_id`, capture/AIO statuses, device/domain/warnings), drops the
   `hl`/`gl` NOT NULL, and creates both review views.
4. `supabase/migrations/20261028032336_query_response_jsonb.sql` — adds
   `response_schema_version`, `aio_response`, `organic_results` and their
   checks; backfills from the child tables with exact row/content
   verification in the same transaction; repoints `search_query_review`;
   drops the child tables **without CASCADE** so unexpected dependencies
   abort instead of being silently removed.

For an existing project, inspect migration history and take a fresh backup
first. The earlier project recorded the first two under `20260930031633` and
`20260930031938`; reconcile those historical filenames before CLI migration
workflows, and never edit applied migrations. The fourth migration locks the
tables, keeps a transaction-local copy of every original row, verifies counts
and exact JSON content/order before commit, and rolls back entirely on any
mismatch.

Optional rollback SQL lives outside forward migrations at
`supabase/rollbacks/20261028032336_query_response_jsonb.sql`. It reconstructs
the child tables from **current** JSONB, checks an exact round trip, and
refuses JSON extensions the old schema cannot represent. Stop writers and
restore the pre-consolidation application code before rollback; repair the
migration history entry separately. Both forward and rollback SQL passed
PostgreSQL execution tests before the 2026-10-28 deployment; any future use
against another dataset requires the same fresh-backup/runtime checks.

## Retired child tables

`ai_overviews`, `ai_references` and `organic_results` stored nested content
as SQL rows until migration 4 consolidated them into `aio_response` and
`organic_results`. Their content was verified byte-for-byte during the
backfill, then they were dropped. Do not replay old five-table SQL seed
files against the consolidated schema; the rollback is the only supported
path back to that shape.

## Capture/import

After migration, both paths write only run/query rows:

```sh
# Normalized custom/SerpAPI records. Generate the UUID ONCE per new dataset.
COMPARISON_ID="$(uv run python -c 'import uuid; print(uuid.uuid4())')"
uv run google-aio-sql \
  --input path/to/NEW_CAPTURE.main-body/records.json \
  --comparison-id "$COMPARISON_ID" --label 'AIO/organic comparison' \
  --output-dir query_test/NEW_SQL_EXPORT --upload
```

Without `--upload`, this only writes `data.sql`, `tables.json` and a
manifest. The manifest identifies storage schema 3 and response envelope
version 1; table counts are runs and query rows. `data.sql` contains
hex-encoded JSON values inside a transaction, never executable user text.
The custom collector's `--upload`/`--upload-file` uses the same contract and
preserves the capture's run UUID and original order. Summary keys such as
`ai_references` count JSON objects, not SQL rows. Fingerprints identify
response content plus provenance within a run; they are not
cross-provider quality scores.

Each core upload is one parameterized, atomic statement for the whole
payload. There is no automatic HTTP 413 chunk fallback. The separate
historical `query_test/upload_pap_chunked.py` workaround commits a run and
individual chunks separately — 30 chunks of 10 rows are recorded in
`query_test/pap-300-final.chunked-upload.log` — and earlier chunks stay
committed if a later one fails; see
[the operational-policy distinction](state-flow.md#the-http-413-operational-exception).
That log is historical evidence of a past run, not current database health.

Duplicate UUIDs/query identities fail, never overwrite. Missing parent runs
cause a NOT NULL failure, not silently skipped responses. Complete
snapshots are required. Errors are sanitized. Writes never retry
automatically. A failed response can still mean the server committed:
inspect the retained UUID remotely before retrying. Do not invent another
UUID to bypass an uncertain write. Output directories must be new;
intent/SQL/manifest are written before upload and `upload.json` records a
successful receipt.

Native uploads raise `ConfigurationError`, `SnapshotError` or `UploadError`;
comparison uploads retain their `ValueError` contract. Native receipts
contain a run UUID and logical content counts; comparison receipts contain a
comparison UUID and table-row counts. These are deliberately distinct.
`uploaded=False` in a manifest is not proof that a failed remote request
rolled back.

## Verification evidence

The default local suite and compilation checks:

```sh
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q comparison_storage.py response_sql.py supabase_results.py run_supabase_batch.py
```

Local tests cover one row containing 200 paragraphs and 200 organic results,
native tables/sparse indexes/long snippets, missing locale, capture-status
distinctions, duplicate refusal, parameterized values, credential redaction,
versioned offline exports and receipt validation. SQL text-contract checks
do not substitute for PostgreSQL execution.

Historical deployment evidence for the consolidated schema (2026-10-28) was
recorded on the deployment machine under `query_test/jsonb_migration_20261028T053302Z/`
(full backup, expected consolidated documents, forward/write/reverse rollback
test and receipt, guarded apply SQL, committed migration receipt, after
snapshot and final verification). **These evidence directories are untracked
local artifacts and are not present in this checkout**; the names are kept
for traceability, and their contents cannot be re-inspected here. At that
time both uploaders were exercised as `service_role`; duplicate,
invalid-JSON and missing-parent writes failed atomically; fresh reads proved
the test transaction left no changes. The committed migration retained all
56 responses of the verified dataset, their original metadata, every JSON
field/array order and both review-view outputs. RLS/private grants and
removal of all three old table names were verified at that time. The 182
local tests then current passed before deployment.

Verified dataset at deployment — comparison UUID
`f7384551-5bdc-45f3-8a9a-c1d49cdb58b6`, run UUIDs
`1fa87bf9-4798-57c3-91da-490666f655a3` (custom) and
`6422fbde-a5ae-5d8b-b1c7-bfcb8fb3f026` (serpapi):

| Provider | Response rows | AI Overviews | References | Organic results |
| --- | ---: | ---: | ---: | ---: |
| custom | 28 | 25 | 170 | 249 |
| serpapi | 28 | 23 | 206 | 246 |

These are 56 complete response rows, not 28 merged provider responses; the
nested-content counts describe JSONB values, not SQL rows. The previously
removed historical run `92ae43d8-...` must not be restored by consolidation;
its backup/receipt was recorded under
`query_test/historical_run_removal_20261019T155111Z/` (also not present in
this checkout).

Evidence for the five-table era (`query_test/sql_migration_20261019T151452Z/`)
and the pre-deployment offline conversion
(`query_test/jsonb_consolidation_prepared_20261028T032336Z/`) are likewise
untracked local artifacts with the same caveat.
No additional Google/SerpAPI calls, credential edits or historical-data
restoration were made for documentation work. Captures/backups remain
outside git. SQLite and older CSV imports remain unsupported.

## Official references

- https://supabase.com/docs/reference/api/v1-run-a-query
- https://supabase.com/docs/guides/api/api-keys
- https://supabase.com/docs/guides/database/postgres/row-level-security#views
- https://supabase.com/docs/guides/deployment/database-migrations
- https://www.postgresql.org/docs/current/datatype-json.html
- https://www.postgresql.org/docs/current/functions-json.html
