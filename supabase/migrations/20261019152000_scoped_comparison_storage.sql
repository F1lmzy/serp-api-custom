-- Extend the private five-table store; historical content is never rewritten.
-- PostgreSQL 15+ for security_invoker views. Verified target: PostgreSQL 17.6.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '90s';

ALTER TABLE public.scrape_runs
    ADD COLUMN IF NOT EXISTS comparison_id uuid,
    ADD COLUMN IF NOT EXISTS provider text NOT NULL DEFAULT 'custom'
        CHECK (provider IN ('custom', 'serpapi')),
    ADD COLUMN IF NOT EXISTS run_index integer NOT NULL DEFAULT 1 CHECK (run_index > 0);
CREATE UNIQUE INDEX IF NOT EXISTS scrape_runs_comparison_provider_round_uidx
    ON public.scrape_runs (comparison_id, provider, run_index);

ALTER TABLE public.queries
    ADD COLUMN IF NOT EXISTS snapshot_id text CHECK (snapshot_id IS NULL OR length(snapshot_id) > 0),
    ADD COLUMN IF NOT EXISTS captured_at timestamptz,
    ADD COLUMN IF NOT EXISTS capture_status text CHECK (capture_status IN ('ok', 'error', 'unresolved')),
    ADD COLUMN IF NOT EXISTS aio_status text CHECK (aio_status IN
        ('present', 'absent', 'unavailable', 'empty', 'unresolved', 'invalid', 'error')),
    ADD COLUMN IF NOT EXISTS device text,
    ADD COLUMN IF NOT EXISTS google_domain text,
    ADD COLUMN IF NOT EXISTS warnings jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(warnings) = 'array');
-- Unknown observed locale is NULL, not an invented en/us value. Defaults remain
-- for the legacy writer, which already supplies explicit locale values.
ALTER TABLE public.queries ALTER COLUMN hl DROP NOT NULL, ALTER COLUMN gl DROP NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS queries_snapshot_uidx ON public.queries (run_id, snapshot_id);

ALTER TABLE public.ai_overviews ADD COLUMN IF NOT EXISTS answer_text text;
ALTER TABLE public.ai_references
    ADD COLUMN IF NOT EXISTS observed_index integer CHECK (observed_index >= 0),
    ADD COLUMN IF NOT EXISTS snippet text;
-- Preserve missing optional fields; do not synthesize empty source/title data.
ALTER TABLE public.ai_references ALTER COLUMN title DROP NOT NULL, ALTER COLUMN source DROP NOT NULL;
-- reference_index remains the generated legacy ordinal index. New citations
-- use observed_index, which may be sparse or NULL and is never inferred.

ALTER TABLE public.organic_results
    ADD COLUMN IF NOT EXISTS position integer CHECK (position > 0),
    ADD COLUMN IF NOT EXISTS displayed_link text,
    ADD COLUMN IF NOT EXISTS source text,
    ADD COLUMN IF NOT EXISTS date text,
    ADD COLUMN IF NOT EXISTS author text,
    ADD COLUMN IF NOT EXISTS sitelinks jsonb
        CHECK (jsonb_typeof(sitelinks) IN ('array', 'object')),
    ADD COLUMN IF NOT EXISTS rich_snippet jsonb
        CHECK (jsonb_typeof(rich_snippet) IN ('array', 'object'));
ALTER TABLE public.organic_results ALTER COLUMN title DROP NOT NULL, ALTER COLUMN snippet DROP NOT NULL;

-- A read model, not duplicate stored data. Correlated aggregates avoid multiplying
-- references by organic results. SQL rows/arrays always use explicit source order.
-- https://supabase.com/docs/guides/database/postgres/row-level-security#views
CREATE OR REPLACE VIEW public.search_query_review
WITH (security_invoker = true) AS
SELECT r.comparison_id, r.run_id, r.provider, r.run_index,
       q.query_id, q.ordinal, q.query, q.hl, q.gl, q.snapshot_id, q.captured_at,
       q.status, q.capture_status, q.aio_status, q.device, q.google_domain,
       q.error_code, q.error_message, q.warnings,
       COALESCE(a.answer_text, a.paragraph) AS aio_text,
       COALESCE(a.text_blocks, '[]'::jsonb) AS aio_blocks,
       jsonb_array_length(COALESCE(a.text_blocks, '[]'::jsonb)) AS aio_block_count,
       COALESCE((SELECT jsonb_agg(
           jsonb_strip_nulls((to_jsonb(ref) - 'run_id' - 'query_id' - 'reference_index' - 'observed_index')
               || jsonb_build_object('index', ref.observed_index)) ORDER BY ref.ordinal)
           FROM public.ai_references ref WHERE ref.run_id = q.run_id AND ref.query_id = q.query_id), '[]'::jsonb) AS aio_references,
       (SELECT count(*) FROM public.ai_references ref WHERE ref.run_id = q.run_id AND ref.query_id = q.query_id) AS reference_count,
       COALESCE((SELECT jsonb_agg(jsonb_strip_nulls(to_jsonb(o) - 'run_id' - 'query_id') ORDER BY o.ordinal)
           FROM public.organic_results o WHERE o.run_id = q.run_id AND o.query_id = q.query_id), '[]'::jsonb) AS organic_results,
       (SELECT count(*) FROM public.organic_results o WHERE o.run_id = q.run_id AND o.query_id = q.query_id) AS organic_count
FROM public.scrape_runs r
JOIN public.queries q ON q.run_id = r.run_id
LEFT JOIN public.ai_overviews a ON a.run_id = q.run_id AND a.query_id = q.query_id;

CREATE OR REPLACE VIEW public.search_provider_comparison
WITH (security_invoker = true) AS
WITH custom AS (
    SELECT * FROM public.search_query_review WHERE provider = 'custom' AND comparison_id IS NOT NULL
), serpapi AS (
    SELECT * FROM public.search_query_review WHERE provider = 'serpapi' AND comparison_id IS NOT NULL
)
SELECT COALESCE(c.comparison_id, s.comparison_id) AS comparison_id,
       COALESCE(c.run_index, s.run_index) AS run_index,
       COALESCE(c.query_id, s.query_id) AS query_id,
       COALESCE(c.query, s.query) AS query,
       c.run_id AS custom_run_id, s.run_id AS serpapi_run_id,
       c.query AS custom_query, s.query AS serpapi_query,
       c.captured_at AS custom_captured_at, s.captured_at AS serpapi_captured_at,
       c.capture_status AS custom_capture_status, s.capture_status AS serpapi_capture_status,
       c.aio_status AS custom_aio_status, s.aio_status AS serpapi_aio_status,
       c.aio_text AS custom_aio_text, s.aio_text AS serpapi_aio_text,
       c.aio_blocks AS custom_aio_blocks, s.aio_blocks AS serpapi_aio_blocks,
       c.aio_references AS custom_aio_references, s.aio_references AS serpapi_aio_references,
       c.organic_results AS custom_organic_results, s.organic_results AS serpapi_organic_results,
       c.reference_count AS custom_reference_count, s.reference_count AS serpapi_reference_count,
       c.organic_count AS custom_organic_count, s.organic_count AS serpapi_organic_count,
       c.warnings AS custom_warnings, s.warnings AS serpapi_warnings
FROM custom c FULL JOIN serpapi s ON c.comparison_id = s.comparison_id
    AND c.run_index = s.run_index AND c.query_id = s.query_id;

-- Explicitly keep existing tables and new views private, including default grants.
ALTER TABLE public.scrape_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.queries ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_overviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_references ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.organic_results ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.scrape_runs, public.queries, public.ai_overviews,
    public.ai_references, public.organic_results, public.search_query_review,
    public.search_provider_comparison FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.search_query_review, public.search_provider_comparison FROM service_role;
GRANT SELECT ON public.search_query_review, public.search_provider_comparison TO service_role;
NOTIFY pgrst, 'reload schema';
COMMIT;
