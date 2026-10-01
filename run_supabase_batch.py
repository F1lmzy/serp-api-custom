"""Capture a lossless query batch and optionally upload it as a new Supabase run."""

import argparse
import asyncio
import contextlib
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import random
import sys
import uuid

import aio_scraper as a
from playwright.async_api import Error as PlaywrightError
from supabase_results import normalize_snapshot, sanitize_error_message, upload_snapshot

_SECRET_NAMES = (
    'SUPABASE_ACCESS_TOKEN', 'SUPABASE_SECRET_KEY', 'TWOCAPTCHA_API_KEY',
    'DATAIMPULSE_PROXY_PASSWORD', 'DATAIMPULSE_PROXY_USER',
)


def _secrets():
    return tuple(os.environ[name] for name in _SECRET_NAMES if os.environ.get(name))


def _save_snapshot(snapshot, path):
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


class _SafeStderr:
    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        self.stream.write(sanitize_error_message(text, _secrets()))
        return len(text)

    def flush(self):
        self.stream.flush()


async def collect_batch(queries, prefix, *, delay=3.0, timeout_ms=90000,
                        engine='chromium', proxy_mode='direct', hl='en', gl='us'):
    if not queries:
        raise ValueError('No queries to run')
    prefix = Path(prefix)
    csv_path, json_path = prefix.with_suffix('.csv'), prefix.with_suffix('.json')
    prefix.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite prior artifacts, even if their SQL upload failed.
    if csv_path.exists() or json_path.exists():
        raise ValueError('Output already exists; choose a new --out-prefix')
    snapshot = {
        'run_id': str(uuid.uuid4()), 'label': prefix.name,
        'source_file': json_path.name, 'captured_at': datetime.now(timezone.utc).isoformat(),
        'planned_query_count': len(queries), 'results': [],
    }
    _save_snapshot(snapshot, json_path)
    with csv_path.open('w', encoding='utf-8', newline='') as out:
        writer = csv.DictWriter(out, fieldnames=a.CSV_FIELDS)
        writer.writeheader()
        async with a.GoogleAIOScraper(engine=engine, timeout_ms=timeout_ms, proxy_mode=proxy_mode) as scraper:
            for ordinal, query in enumerate(queries, start=1):
                try:
                    result = await scraper.search(query, hl, gl)
                except (a.ChallengeDetected, a.CaptchaSolverError, a.ScrapeError) as exc:
                    code = ('CAPTCHA_REQUIRED' if isinstance(exc, a.ChallengeDetected) else
                            'CAPTCHA_SOLVER_ERROR' if isinstance(exc, a.CaptchaSolverError) else 'SCRAPE_ERROR')
                    result = a._error_result(code, sanitize_error_message(str(exc), _secrets()), query, hl, gl)
                query_id = f'P{ordinal:02d}'
                snapshot['results'].append({'query_id': query_id, 'result': result})
                _save_snapshot(snapshot, json_path)
                writer.writerows(a._csv_rows(result, query_id))
                out.flush()
                print(f"[capture] {ordinal}/{len(queries)} {query_id}: {'error' if 'error' in result else 'ok'}", file=sys.stderr)
                if ordinal < len(queries) and delay:
                    await asyncio.sleep(delay + random.uniform(0, min(2.0, delay)))
    return snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--queries', help='One query per line, as in aio_scraper.py')
    source.add_argument('--upload-file', help='Upload a previously saved native JSON snapshot; never rescrapes')
    parser.add_argument('--out-prefix', help='New output prefix for CSV and JSON; must not already exist')
    parser.add_argument('--upload', action='store_true', help='Upload after capture; --upload-file implies upload')
    parser.add_argument('--delay', type=float, default=3.0)
    parser.add_argument('--timeout', type=int, default=90000, help='Navigation timeout in milliseconds')
    parser.add_argument('--browser', choices=('chromium', 'firefox'), default='chromium')
    parser.add_argument('--proxy', choices=('direct', 'dataimpulse'), default='direct')
    parser.add_argument('--hl', default='en')
    parser.add_argument('--gl', default='us')
    args = parser.parse_args(argv)
    if not math.isfinite(args.delay) or args.delay < 0 or args.timeout <= 0:
        parser.error('--delay must be finite/nonnegative and --timeout positive')
    if args.upload_file and args.out_prefix:
        parser.error('--out-prefix applies only to --queries')
    with contextlib.redirect_stderr(_SafeStderr(sys.stderr)):
        try:
            a._load_env_file()
            if args.upload_file:
                snapshot = json.loads(Path(args.upload_file).read_text(encoding='utf-8'))
            else:
                prefix = args.out_prefix or f'query_test/results_supabase_{datetime.now():%Y%m%d_%H%M%S}'
                snapshot = asyncio.run(collect_batch(
                    a._read_query_file(args.queries), prefix, delay=args.delay, timeout_ms=args.timeout,
                    engine=args.browser, proxy_mode=args.proxy, hl=args.hl, gl=args.gl))
            normalized = normalize_snapshot(snapshot)
            summary = {'run_id': snapshot['run_id'], **normalized['scrape_runs'][0]['metadata']}
            summary.pop('fingerprint')
            summary['errors'] = sum(q['status'] == 'error' for q in normalized['queries'])
            summary['uploaded'] = False
            if args.upload or args.upload_file:
                summary.update(upload_snapshot(snapshot, os.getenv('SUPABASE_PROJECT_ID', '').strip(),
                                               os.getenv('SUPABASE_ACCESS_TOKEN', '').strip()))
                summary['uploaded'] = True
            print(json.dumps(summary))
            return 1 if summary['errors'] else 0
        except (ValueError, OSError, RuntimeError, PlaywrightError) as exc:
            print(json.dumps({'error': sanitize_error_message(str(exc), _secrets()),
                              'note': 'Local snapshots are retained. Uploads are not retried automatically; check the saved run_id before retrying an uncertain upload.'}))
            return 2


if __name__ == '__main__':
    raise SystemExit(main())
