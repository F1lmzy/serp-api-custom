-- Private scraper results. Additive: never drops tables or existing results.
-- Run once in the Supabase SQL Editor, or via the Management API.
BEGIN;

CREATE TABLE IF NOT EXISTS public.scrape_runs (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    label text NOT NULL DEFAULT '',
    source_file text NOT NULL DEFAULT '',
    captured_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE TABLE IF NOT EXISTS public.queries (
    run_id uuid NOT NULL REFERENCES public.scrape_runs(run_id) ON DELETE CASCADE,
    query_id text NOT NULL CHECK (length(query_id) > 0),
    ordinal integer NOT NULL CHECK (ordinal > 0),
    query text NOT NULL CHECK (length(query) > 0),
    hl text NOT NULL DEFAULT 'en',
    gl text NOT NULL DEFAULT 'us',
    status text NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'error')),
    error_code text,
    error_message text,
    PRIMARY KEY (run_id, query_id),
    UNIQUE (run_id, ordinal)
);

CREATE TABLE IF NOT EXISTS public.ai_overviews (
    run_id uuid NOT NULL,
    query_id text NOT NULL,
    paragraph text NOT NULL DEFAULT '',
    -- Keep complete ordered headings, paragraphs, lists, and citation indexes.
    -- Unlike the reference project's exporter, no text is truncated.
    text_blocks jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(text_blocks) = 'array'),
    PRIMARY KEY (run_id, query_id),
    FOREIGN KEY (run_id, query_id)
        REFERENCES public.queries(run_id, query_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS public.ai_references (
    run_id uuid NOT NULL,
    query_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    -- JSON citation indexes are zero-based; displayed reference order is one-based.
    reference_index integer GENERATED ALWAYS AS (ordinal - 1) STORED,
    title text NOT NULL DEFAULT '',
    source text NOT NULL DEFAULT '',
    link text NOT NULL,
    PRIMARY KEY (run_id, query_id, ordinal),
    FOREIGN KEY (run_id, query_id)
        REFERENCES public.ai_overviews(run_id, query_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS public.organic_results (
    run_id uuid NOT NULL,
    query_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    title text NOT NULL DEFAULT '',
    snippet text NOT NULL DEFAULT '',
    link text NOT NULL,
    PRIMARY KEY (run_id, query_id, ordinal),
    FOREIGN KEY (run_id, query_id)
        REFERENCES public.queries(run_id, query_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS scrape_runs_created_at_idx
    ON public.scrape_runs (created_at DESC);

-- Keep results private. RLS with no policies denies access to client roles.
ALTER TABLE public.scrape_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.queries ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_overviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_references ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.organic_results ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.scrape_runs, public.queries, public.ai_overviews,
    public.ai_references, public.organic_results
    FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA public TO service_role;
GRANT SELECT, INSERT, UPDATE ON TABLE public.scrape_runs, public.queries,
    public.ai_overviews, public.ai_references, public.organic_results
    TO service_role;

-- Refresh the Data API schema cache after the transaction commits.
NOTIFY pgrst, 'reload schema';
COMMIT;
