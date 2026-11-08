"""Export and upload shared custom/SerpAPI records as complete response rows.

The storage contract has two tables, scrape_runs and queries. Response
validation and projection live in supabase_results; this module retains
the comparison command and its existing Python import paths.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import supabase_results as storage
from supabase_results import (
    BLOCK_KEYS, DROP_SECTIONS, IGNORED, MEDIA_KEYS, STATUSES, SUPPORT_KEYS,
    normalize_records, normalize_response_snapshot as normalize_native_snapshot,
    render_text, response_counts, secrets, execute_sql, sql_export,
)
from serpapi_client import sanitize
from response_sql import TABLE_COLUMNS, UPLOAD_SQL


def upload_records(records, comparison_id, project_id, access_token, label=''):
    """Preserve this import path and its replaceable SQL executor."""
    return storage.upload_records(
        records, comparison_id, project_id, access_token, label, executor=execute_sql,
    )


def main(argv=None):
    from aio_scraper import _load_env_file
    parser = argparse.ArgumentParser(allow_abbrev=False, description=__doc__)
    parser.add_argument('--input', required=True, help='Normalized records.json, never raw API data')
    parser.add_argument('--comparison-id', required=True, help='Stable UUID; persist before uploading')
    parser.add_argument('--output-dir', required=True, help='New directory for SQL and upload receipt')
    parser.add_argument('--label', default='')
    parser.add_argument('--upload', action='store_true')
    args = parser.parse_args(argv)
    try:
        _load_env_file()
        records = json.loads(Path(args.input).read_text())
        tables = normalize_records(records, args.comparison_id, args.label)
        output = Path(args.output_dir)
        output.mkdir(parents=True, exist_ok=False)
        (output/'data.sql').write_text(sql_export(tables))
        (output/'tables.json').write_text(json.dumps(tables, ensure_ascii=False, indent=2) + '\n')
        summary = {'storage_schema_version': 3, 'response_schema_version': 1,
                   'comparison_id': args.comparison_id, 'counts': {t: len(v) for t, v in tables.items()}, 'uploaded': False}
        (output/'manifest.json').write_text(json.dumps(summary, indent=2) + '\n')
        if args.upload:
            receipt = upload_records(records, args.comparison_id, os.getenv('SUPABASE_PROJECT_ID'), os.getenv('SUPABASE_ACCESS_TOKEN'), args.label)
            (output/'upload.json').write_text(json.dumps(receipt, indent=2) + '\n')
            summary['uploaded'] = True
            (output/'manifest.json').write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps(summary))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'error': sanitize(str(exc), secrets())}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
