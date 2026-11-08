-- One complete provider/query response per queries row.
-- Requires 20261019152000. Back up source data before applying.
-- Atomic: unexpected dependencies or content mismatches roll everything back.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';
LOCK TABLE public.scrape_runs, public.queries, public.ai_overviews,
    public.ai_references, public.organic_results IN SHARE ROW EXCLUSIVE MODE;

-- Keep an independent transaction-local source for exact backfill checks.
CREATE TEMP TABLE response_consolidation_before ON COMMIT DROP AS
SELECT q.run_id, q.query_id, to_jsonb(q) AS original_query,
       CASE WHEN a.run_id IS NULL THEN NULL ELSE jsonb_build_object(
           'text', COALESCE(a.answer_text, a.paragraph),
           'paragraph', a.paragraph,
           'blocks', a.text_blocks,
           'references', COALESCE((
               SELECT jsonb_agg(jsonb_strip_nulls(
                   (to_jsonb(ref) - 'run_id' - 'query_id' - 'reference_index' - 'observed_index')
                   || jsonb_build_object('index', ref.observed_index)) ORDER BY ref.ordinal)
               FROM public.ai_references ref
               WHERE ref.run_id = q.run_id AND ref.query_id = q.query_id
           ), '[]'::jsonb)
       ) END AS aio_response,
       COALESCE((
           SELECT jsonb_agg(jsonb_strip_nulls(to_jsonb(o) - 'run_id' - 'query_id') ORDER BY o.ordinal)
           FROM public.organic_results o
           WHERE o.run_id = q.run_id AND o.query_id = q.query_id
       ), '[]'::jsonb) AS organic_results
FROM public.queries q
LEFT JOIN public.ai_overviews a ON a.run_id = q.run_id AND a.query_id = q.query_id;
CREATE TEMP TABLE response_runs_before ON COMMIT DROP AS
SELECT run_id, to_jsonb(r) AS original_run FROM public.scrape_runs r;

ALTER TABLE public.queries
    ADD COLUMN response_schema_version integer NOT NULL DEFAULT 1
        CHECK (response_schema_version = 1),
    ADD COLUMN aio_response jsonb,
    ADD COLUMN organic_results jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(organic_results) = 'array'),
    ADD CONSTRAINT queries_aio_response_shape CHECK (
        aio_response IS NULL OR COALESCE(
            jsonb_typeof(aio_response) = 'object'
            AND jsonb_typeof(aio_response -> 'blocks') = 'array'
            AND jsonb_typeof(aio_response -> 'references') = 'array', false));

UPDATE public.queries q
SET aio_response = b.aio_response, organic_results = b.organic_results
FROM response_consolidation_before b
WHERE q.run_id = b.run_id AND q.query_id = b.query_id;

ALTER TABLE public.queries
    ADD CONSTRAINT queries_present_aio_content CHECK (
        aio_status IS DISTINCT FROM 'present'
        OR COALESCE(jsonb_array_length(aio_response -> 'blocks') > 0, false)),
    ADD CONSTRAINT queries_absent_aio_content CHECK (
        aio_status NOT IN ('absent', 'unavailable') OR aio_response IS NULL
        OR jsonb_array_length(aio_response -> 'blocks') = 0);

DO $verify$
BEGIN
    IF (SELECT count(*) FROM public.queries) != (SELECT count(*) FROM response_consolidation_before)
       OR (SELECT count(*) FROM public.scrape_runs) != (SELECT count(*) FROM response_runs_before) THEN
        RAISE EXCEPTION 'Response/run row count changed during consolidation';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.queries q
        JOIN response_consolidation_before b USING (run_id, query_id)
        WHERE q.aio_response IS DISTINCT FROM b.aio_response
           OR q.organic_results IS DISTINCT FROM b.organic_results
           OR (to_jsonb(q) - 'response_schema_version' - 'aio_response' - 'organic_results')
              IS DISTINCT FROM b.original_query
    ) OR EXISTS (
        SELECT 1 FROM public.scrape_runs r JOIN response_runs_before b USING (run_id)
        WHERE to_jsonb(r) IS DISTINCT FROM b.original_run
    ) THEN
        RAISE EXCEPTION 'Consolidated content/order or original metadata does not match source';
    END IF;
    IF (SELECT count(*) FROM public.queries WHERE aio_response IS NOT NULL)
           != (SELECT count(*) FROM public.ai_overviews)
       OR (SELECT COALESCE(sum(jsonb_array_length(COALESCE(aio_response -> 'references', '[]'::jsonb))), 0) FROM public.queries)
           != (SELECT count(*) FROM public.ai_references)
       OR (SELECT COALESCE(sum(jsonb_array_length(organic_results)), 0) FROM public.queries)
           != (SELECT count(*) FROM public.organic_results) THEN
        RAISE EXCEPTION 'Consolidation would lose AIO/reference/organic content';
    END IF;
END $verify$;

-- Preserve the existing view's names, types, order and values for callers.
-- bigint casts retain the old count(*) result types.
CREATE OR REPLACE VIEW public.search_query_review
WITH (security_invoker = true) AS
SELECT r.comparison_id, r.run_id, r.provider, r.run_index,
       q.query_id, q.ordinal, q.query, q.hl, q.gl, q.snapshot_id, q.captured_at,
       q.status, q.capture_status, q.aio_status, q.device, q.google_domain,
       q.error_code, q.error_message, q.warnings,
       q.aio_response ->> 'text' AS aio_text,
       COALESCE(q.aio_response -> 'blocks', '[]'::jsonb) AS aio_blocks,
       jsonb_array_length(COALESCE(q.aio_response -> 'blocks', '[]'::jsonb)) AS aio_block_count,
       COALESCE(q.aio_response -> 'references', '[]'::jsonb) AS aio_references,
       jsonb_array_length(COALESCE(q.aio_response -> 'references', '[]'::jsonb))::bigint AS reference_count,
       q.organic_results,
       jsonb_array_length(q.organic_results)::bigint AS organic_count
FROM public.scrape_runs r
JOIN public.queries q ON q.run_id = r.run_id;
-- search_provider_comparison already selects this stable view contract.

-- No CASCADE: any unknown external dependency must prevent removal.
DROP TABLE public.ai_references;
DROP TABLE public.organic_results;
DROP TABLE public.ai_overviews;

ALTER TABLE public.scrape_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.queries ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.scrape_runs, public.queries,
    public.search_query_review, public.search_provider_comparison
    FROM PUBLIC, anon, authenticated;
REVOKE DELETE, TRUNCATE ON public.scrape_runs, public.queries FROM service_role;
GRANT SELECT, INSERT, UPDATE ON public.scrape_runs, public.queries TO service_role;
REVOKE ALL ON public.search_query_review, public.search_provider_comparison FROM service_role;
GRANT SELECT ON public.search_query_review, public.search_provider_comparison TO service_role;
NOTIFY pgrst, 'reload schema';
COMMIT;
