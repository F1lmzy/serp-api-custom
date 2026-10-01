-- Supabase default privileges can grant ALL to service_role on new tables.
-- Granting a subset does not remove those default DELETE/TRUNCATE privileges.
-- Explicitly reset only this integration's table grants, then allow runtime DML.
BEGIN;

REVOKE ALL ON TABLE public.scrape_runs, public.queries, public.ai_overviews,
    public.ai_references, public.organic_results
    FROM service_role;
GRANT SELECT, INSERT, UPDATE ON TABLE public.scrape_runs, public.queries,
    public.ai_overviews, public.ai_references, public.organic_results
    TO service_role;

COMMIT;
