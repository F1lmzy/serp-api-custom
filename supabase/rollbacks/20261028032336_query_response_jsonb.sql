-- Optional rollback, NOT part of forward migration history.
-- Stop writers and restore the pre-consolidation application before using this.
-- Reconstructs from CURRENT JSONB rows, not a stale backup. Exact round-trip
-- checks abort on JSON extensions the old schema cannot represent.
-- Repair the forward migration's history entry separately after a rollback.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';
LOCK TABLE public.scrape_runs, public.queries IN SHARE ROW EXCLUSIVE MODE;

CREATE TABLE public.ai_overviews (
    run_id uuid NOT NULL, query_id text NOT NULL,
    paragraph text NOT NULL DEFAULT '',
    text_blocks jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(text_blocks) = 'array'),
    answer_text text,
    PRIMARY KEY (run_id, query_id),
    FOREIGN KEY (run_id, query_id) REFERENCES public.queries(run_id, query_id) ON DELETE CASCADE
);
CREATE TABLE public.ai_references (
    run_id uuid NOT NULL, query_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    reference_index integer GENERATED ALWAYS AS (ordinal - 1) STORED,
    title text, source text, link text NOT NULL,
    observed_index integer CHECK (observed_index >= 0), snippet text,
    PRIMARY KEY (run_id, query_id, ordinal),
    FOREIGN KEY (run_id, query_id) REFERENCES public.ai_overviews(run_id, query_id) ON DELETE CASCADE
);
CREATE TABLE public.organic_results (
    run_id uuid NOT NULL, query_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    title text, snippet text, link text NOT NULL,
    position integer CHECK (position > 0), displayed_link text, source text,
    date text, author text,
    sitelinks jsonb CHECK (jsonb_typeof(sitelinks) IN ('array', 'object')),
    rich_snippet jsonb CHECK (jsonb_typeof(rich_snippet) IN ('array', 'object')),
    PRIMARY KEY (run_id, query_id, ordinal),
    FOREIGN KEY (run_id, query_id) REFERENCES public.queries(run_id, query_id) ON DELETE CASCADE
);
INSERT INTO public.ai_overviews (run_id, query_id, paragraph, text_blocks, answer_text)
SELECT run_id, query_id, COALESCE(aio_response ->> 'paragraph', ''),
       aio_response -> 'blocks', aio_response ->> 'text'
FROM public.queries WHERE aio_response IS NOT NULL;
INSERT INTO public.ai_references (run_id, query_id, ordinal, observed_index, title, source, link, snippet)
SELECT q.run_id, q.query_id, ref.ordinal, ref."index", ref.title, ref.source, ref.link, ref.snippet
FROM public.queries q CROSS JOIN LATERAL jsonb_to_recordset(q.aio_response -> 'references')
AS ref(ordinal integer, "index" integer, title text, source text, link text, snippet text);
INSERT INTO public.organic_results (run_id, query_id, ordinal, position, title, snippet, link,
    displayed_link, source, date, author, sitelinks, rich_snippet)
SELECT q.run_id, q.query_id, o.ordinal, o.position, o.title, o.snippet, o.link,
       o.displayed_link, o.source, o.date, o.author, o.sitelinks, o.rich_snippet
FROM public.queries q CROSS JOIN LATERAL jsonb_to_recordset(q.organic_results)
AS o(ordinal integer, position integer, title text, snippet text, link text,
     displayed_link text, source text, date text, author text, sitelinks jsonb, rich_snippet jsonb);

DO $verify$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.queries q LEFT JOIN public.ai_overviews a USING (run_id, query_id)
        WHERE q.aio_response IS DISTINCT FROM CASE WHEN a.run_id IS NULL THEN NULL ELSE jsonb_build_object(
            'text', a.answer_text, 'paragraph', a.paragraph, 'blocks', a.text_blocks,
            'references', COALESCE((SELECT jsonb_agg(jsonb_strip_nulls(
                (to_jsonb(ref) - 'run_id' - 'query_id' - 'reference_index' - 'observed_index')
                || jsonb_build_object('index', ref.observed_index)) ORDER BY ref.ordinal)
                FROM public.ai_references ref WHERE ref.run_id = q.run_id AND ref.query_id = q.query_id), '[]'::jsonb)) END
           OR q.organic_results IS DISTINCT FROM COALESCE((
               SELECT jsonb_agg(jsonb_strip_nulls(to_jsonb(o) - 'run_id' - 'query_id') ORDER BY o.ordinal)
               FROM public.organic_results o WHERE o.run_id = q.run_id AND o.query_id = q.query_id), '[]'::jsonb)
    ) THEN
        RAISE EXCEPTION 'Rollback cannot represent current JSON exactly; no changes committed';
    END IF;
END $verify$;

CREATE OR REPLACE VIEW public.search_query_review WITH (security_invoker = true) AS
SELECT r.comparison_id, r.run_id, r.provider, r.run_index,
       q.query_id, q.ordinal, q.query, q.hl, q.gl, q.snapshot_id, q.captured_at,
       q.status, q.capture_status, q.aio_status, q.device, q.google_domain,
       q.error_code, q.error_message, q.warnings,
       COALESCE(a.answer_text, a.paragraph) AS aio_text,
       COALESCE(a.text_blocks, '[]'::jsonb) AS aio_blocks,
       jsonb_array_length(COALESCE(a.text_blocks, '[]'::jsonb)) AS aio_block_count,
       COALESCE((SELECT jsonb_agg(jsonb_strip_nulls(
           (to_jsonb(ref) - 'run_id' - 'query_id' - 'reference_index' - 'observed_index')
           || jsonb_build_object('index', ref.observed_index)) ORDER BY ref.ordinal)
           FROM public.ai_references ref WHERE ref.run_id = q.run_id AND ref.query_id = q.query_id), '[]'::jsonb) AS aio_references,
       (SELECT count(*) FROM public.ai_references ref WHERE ref.run_id = q.run_id AND ref.query_id = q.query_id) AS reference_count,
       COALESCE((SELECT jsonb_agg(jsonb_strip_nulls(to_jsonb(o) - 'run_id' - 'query_id') ORDER BY o.ordinal)
           FROM public.organic_results o WHERE o.run_id = q.run_id AND o.query_id = q.query_id), '[]'::jsonb) AS organic_results,
       (SELECT count(*) FROM public.organic_results o WHERE o.run_id = q.run_id AND o.query_id = q.query_id) AS organic_count
FROM public.scrape_runs r JOIN public.queries q ON q.run_id = r.run_id
LEFT JOIN public.ai_overviews a ON a.run_id = q.run_id AND a.query_id = q.query_id;

ALTER TABLE public.queries DROP COLUMN aio_response, DROP COLUMN organic_results,
    DROP COLUMN response_schema_version;
ALTER TABLE public.ai_overviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_references ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.organic_results ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.ai_overviews, public.ai_references, public.organic_results
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.ai_overviews, public.ai_references, public.organic_results FROM service_role;
GRANT SELECT, INSERT, UPDATE ON public.ai_overviews, public.ai_references, public.organic_results TO service_role;
NOTIFY pgrst, 'reload schema';
COMMIT;
