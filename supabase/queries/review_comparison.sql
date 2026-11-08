-- Current live batch: 28 rows with actual content from both providers.
-- Run in the configured project's Supabase SQL Editor.
-- Replace the comparison UUID only when reviewing another uploaded batch.
SELECT query_id, query,
       custom_captured_at, serpapi_captured_at,
       custom_capture_status, serpapi_capture_status,
       custom_aio_status, serpapi_aio_status,
       custom_aio_text, serpapi_aio_text,
       custom_aio_references, serpapi_aio_references,
       custom_organic_results, serpapi_organic_results,
       custom_warnings, serpapi_warnings
FROM public.search_provider_comparison
WHERE comparison_id = 'f7384551-5bdc-45f3-8a9a-c1d49cdb58b6'::uuid
ORDER BY run_index, query_id;
