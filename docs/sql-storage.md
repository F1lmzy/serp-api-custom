# SQL results storage

## Scope and current status

Use the normalized tables from `SERP-API`, with a parent run record so query IDs such as `P01` can repeat across runs without overwriting history. Preserve complete AIO text, ordered blocks, reference indexes, and organic result order. Tables are private, not publicly readable.

The schema was applied to the configured project through the Supabase Management API on 2026-09-30. All five tables are available through the secret-key Data API. Both schema migrations are recorded in Supabase migration history. Runtime verification passed for RLS, role grants, foreign keys, generated reference indexes, full text, and block order. Test inserts ran as `service_role` and were rolled back; no test data remains.

The fresh `query_test/queries.txt` batch was uploaded as run `92ae43d8-e80e-4254-ba82-fd4c39356f3f`: 28 queries, 25 AIOs, 191 references, and 244 organic results, with no query errors. Full remote data matches the saved native snapshot.

[Run and upload batches](supabase-batch-workflow.md) with the new dedicated workflow. It uses the Management API token for atomic writes and preserves run history. The existing `aio_scraper.py` CLI still writes CSV only. Older CSV artifacts have not been imported, and local SQLite export remains separate work.

## Apply the schema migrations

Apply these files in order when setting up another fresh project:

1. `supabase/migrations/202609300001_results_storage.sql`
2. `supabase/migrations/202609300002_restrict_service_role.sql`

The second migration explicitly clears Supabase's default server-role table grants before granting only SELECT, INSERT, and UPDATE. Merely granting a subset does not remove default DELETE/TRUNCATE privileges. Do not omit this correction.

Choose one setup route:

1. In your project's Supabase Dashboard, open **SQL Editor** and run the full contents of each of the two files above, in order.
2. For agent-assisted deployment, put a Supabase Personal Access Token in `.env` as `SUPABASE_ACCESS_TOKEN`. Create it at https://supabase.com/dashboard/account/tokens. This is separate from `SUPABASE_SECRET_KEY`. Scope it to this project and database access where supported. The agent can then apply the SQL through the Management API. Remove or revoke the management token after setup if it is no longer needed.

Do not paste credentials into chat or commit `.env`. A project `sb_secret_...` key can access data but cannot create tables. Schema deployment and the current atomic batch uploader use `SUPABASE_ACCESS_TOKEN`. Reads through the Data API use `SUPABASE_SECRET_KEY`; the current uploader does not use that key.

The files contain only additive schema creation, indexes, grants, and RLS setup. They do not drop or delete data. Reapplying the unchanged files in order is safe on the intended project, but future schema changes need new migration files. Do not mix manually applied schema changes with Supabase CLI migration tracking without reconciling the history.

## Tables

- `scrape_runs`: UUID, label, source filename, optional capture time, import creation time, allowlisted metadata.
- `queries`: query IDs and order within a run, search text and locale, success/error status. Query IDs are unique within a run, not across all runs.
- `ai_overviews`: full paragraph text plus the complete ordered `text_blocks` JSONB array. No truncation. Store an overview row even if it only has headings, lists, or references.
- `ai_references`: one row per unique source in source-card order. `ordinal` is one-based; generated `reference_index` is zero-based to match inline citations. Insert an overview before its references.
- `organic_results`: titles, full snippets, and links in ranked order.

All children link to their parent run/query through foreign keys. Arrays retain element order in JSONB. SQL table rows have no implicit order; always specify `ORDER BY` when reading results.

## Private remote access

RLS is enabled on every table, with no client policies. Table privileges are revoked from `PUBLIC`, `anon`, and `authenticated`. Only the server-side `service_role` receives SELECT, INSERT, and UPDATE access. It bypasses RLS, so the secret key must stay on trusted machines. It has no DELETE grant in this migration. Dashboard SQL access remains available to authorized project administrators.

Python Data API calls should pass `SUPABASE_SECRET_KEY` in the `apikey` header. Do not send this key to a browser or use it as a user login credential. Future browser access requires a separate authenticated read policy and explicit approval.

Ingestion must parameterize SQL or use JSON Data API requests, validate input, and exclude API keys, proxy credentials, cookies, CAPTCHA tokens, and session URL query parameters from stored metadata and errors. Multi-table ingestion must be atomic so a failed import cannot leave a partial run. The dedicated batch workflow implements validation and atomic parameterized ingestion through the Management API. The DDL itself does not upload scraper output.

## Example read queries

List runs:

```sql
SELECT run_id, label, source_file, created_at
FROM public.scrape_runs
ORDER BY created_at DESC, run_id;
```

For the following queries, replace `<run-id>` with a UUID from that result.

Read references in query order, then source-card order:

```sql
SELECT q.query_id, q.query, r.ordinal, r.reference_index,
       r.title, r.source, r.link
FROM public.queries AS q
JOIN public.ai_references AS r USING (run_id, query_id)
WHERE q.run_id = '<run-id>'::uuid
ORDER BY q.ordinal, r.ordinal;
```

Read organic results in ranked order:

```sql
SELECT q.query_id, q.query, r.ordinal, r.title, r.snippet, r.link
FROM public.queries AS q
JOIN public.organic_results AS r USING (run_id, query_id)
WHERE q.run_id = '<run-id>'::uuid
ORDER BY q.ordinal, r.ordinal;
```

Read structured AIO blocks in their original order:

```sql
SELECT q.query_id, b.block_ordinal, b.block
FROM public.queries AS q
JOIN public.ai_overviews AS a USING (run_id, query_id)
CROSS JOIN LATERAL jsonb_array_elements(a.text_blocks)
    WITH ORDINALITY AS b(block, block_ordinal)
WHERE q.run_id = '<run-id>'::uuid
ORDER BY q.ordinal, b.block_ordinal;
```

## Verification and boundaries

Parse the migration without installing a runtime dependency in this project:

```bash
uv run --no-project --with 'pglast>=7,<9' python -c \
  "from pathlib import Path; from pglast import parse_sql; print([(p.name, len(parse_sql(p.read_text()))) for p in sorted(Path('supabase/migrations').glob('*.sql'))])"
```

Existing scraper tests: `.venv/bin/python -m unittest discover -s tests -v`.

Parser validation does not prove that a migration was executed. Both migrations were applied and recorded remotely. The live verification confirmed:

- All five tables enable RLS. `anon` and `authenticated` have no read/write table privileges.
- `service_role` can SELECT, INSERT, and UPDATE, but cannot DELETE or TRUNCATE.
- The four foreign-key relationships and unique query-ordinal constraint are enforced.
- Query ID `P01` can be reused in different runs.
- Reference ordinals 1 and 2 generate citation indexes 0 and 1.
- A 6,000-character paragraph and ordered JSONB blocks/citation indexes survive insertion without truncation.
- A transaction-scoped write test as `service_role` succeeded; rollback left all five tables empty.
- Secret-key Data API reads work on every table. All 84 current unit tests pass.
- The production batch insert committed all five tables in one statement, and every run-scoped remote row matches the saved normalized snapshot. RLS/grants stayed private after upload.

Local SQLite export and imports of older CSV files have not been implemented. No scraper, browser profile, credentials, existing database, or result files were reset during setup.

## Official documentation

- API key types and secret-key scope: https://supabase.com/docs/guides/api/api-keys
- Management API and personal access tokens: https://supabase.com/docs/reference/api/introduction
- SQL Editor and tables: https://supabase.com/docs/guides/database/tables
- Data API grants and RLS: https://supabase.com/docs/guides/api/securing-your-api
- Migration workflow: https://supabase.com/docs/guides/deployment/database-migrations
