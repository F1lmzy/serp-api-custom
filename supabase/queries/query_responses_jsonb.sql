-- Requires migration 20261028032336_query_response_jsonb.sql.
-- One database row per complete provider response. No content-child joins.
SELECT q.query_id, q.query, r.provider, r.run_index,
       q.captured_at, q.capture_status, q.aio_status,
       q.aio_response, q.organic_results
FROM scrape_runs r
JOIN queries q ON q.run_id = r.run_id
WHERE r.comparison_id = 'f7384551-5bdc-45f3-8a9a-c1d49cdb58b6'::uuid
ORDER BY q.query_id, r.run_index, r.provider;

-- P01/P02 side by side. Full AIO objects include blocks and references.
SELECT cq.query_id, cq.query,
       cq.aio_response AS custom_aio_response,
       sq.aio_response AS serpapi_aio_response,
       cq.organic_results AS custom_organic_results,
       sq.organic_results AS serpapi_organic_results
FROM scrape_runs cr
JOIN scrape_runs sr ON sr.comparison_id = cr.comparison_id
                   AND sr.run_index = cr.run_index
                   AND sr.provider = 'serpapi'
JOIN queries cq ON cq.run_id = cr.run_id
JOIN queries sq ON sq.run_id = sr.run_id AND sq.query_id = cq.query_id
WHERE cr.provider = 'custom'
  AND cr.comparison_id = 'f7384551-5bdc-45f3-8a9a-c1d49cdb58b6'::uuid
  AND cq.query_id IN ('P01', 'P02')
ORDER BY cq.query_id;
