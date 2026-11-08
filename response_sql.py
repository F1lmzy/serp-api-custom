"""Shared two-table PostgreSQL write contract for comparison and native uploads."""

TABLE_COLUMNS = {
    'scrape_runs': 'run_id:uuid comparison_id:uuid provider:text run_index:integer label:text source_file:text captured_at:timestamptz metadata:jsonb',
    'queries': 'run_id:uuid query_id:text ordinal:integer query:text hl:text gl:text status:text error_code:text error_message:text snapshot_id:text captured_at:timestamptz capture_status:text aio_status:text device:text google_domain:text warnings:jsonb response_schema_version:integer aio_response:jsonb organic_results:jsonb',
}


def _upload_sql(native=False):
    ctes = ['payload AS (SELECT $1::jsonb AS data)']
    for table, specification in TABLE_COLUMNS.items():
        columns = [item.split(':') for item in specification.split()]
        names = ', '.join('"' + name + '"' for name, _ in columns)
        types = ', '.join('"' + name + '" ' + kind for name, kind in columns)
        ctes.append(f"input_{table} AS (SELECT t.* FROM payload CROSS JOIN LATERAL jsonb_to_recordset(data -> '{table}') AS t({types}))")
        source = f'input_{table} i'
        values = 'i.*'
        returned = 'run_id'
        if table == 'queries':
            # A missing parent must fail, not silently drop a response row.
            source += ' LEFT JOIN saved_scrape_runs p ON p.run_id = i.run_id'
            values = 'p.run_id, ' + ', '.join('i."' + name + '"' for name, _ in columns[1:])
            returned += ', query_id, aio_response, organic_results'
        ctes.append(f'saved_{table} AS (INSERT INTO public.{table} ({names}) SELECT {values} FROM {source} RETURNING {returned})')
    if native:
        receipt = """(SELECT run_id FROM saved_scrape_runs) AS run_id,
            (SELECT count(*) FROM saved_queries) AS queries,
            (SELECT count(*) FROM saved_queries WHERE aio_response IS NOT NULL) AS ai_overviews,
            (SELECT coalesce(sum(jsonb_array_length(coalesce(aio_response -> 'references', '[]'::jsonb))), 0) FROM saved_queries) AS ai_references,
            (SELECT coalesce(sum(jsonb_array_length(organic_results)), 0) FROM saved_queries) AS organic_results"""
    else:
        receipt = """(SELECT (data -> 'scrape_runs' -> 0 ->> 'comparison_id')::uuid FROM payload) AS comparison_id,
            (SELECT count(*) FROM saved_scrape_runs) AS scrape_runs,
            (SELECT count(*) FROM saved_queries) AS queries"""
    return 'WITH ' + ',\n'.join(ctes) + '\nSELECT ' + receipt


UPLOAD_SQL = _upload_sql()
RUN_UPLOAD_SQL = _upload_sql(native=True)
