"""Compare JSON details present in SerpAPI and missing from the custom scraper."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import contextlib
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any

import aio_scraper as a
from serpapi_client import fetch_search, sanitize
from search_export import export_records, normalize_response

def _type(value: Any) -> str:
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, (int, float)):
        return 'number'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, dict):
        return 'object'
    raise ValueError('Input contains a non-JSON value')


def _empty(value: Any) -> bool:
    return value is None or value == '' or value == [] or value == {}


def _child(path: str, key: str) -> str:
    return path + ('.' + key if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', key) else '[' + json.dumps(key) + ']')


def _inventory(value: dict) -> dict:
    nodes = {}

    def walk(item, path='$', parent=None, field=False):
        node = nodes.setdefault(path, {'types': Counter(), 'occurrences': 0, 'empty': 0,
                                       'array_lengths': [], 'parent': parent, 'field': field})
        node['types'][_type(item)] += 1
        node['occurrences'] += 1
        node['empty'] += int(_empty(item))
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError('JSON object keys must be strings')
                walk(child, _child(path, key), path, True)
        elif isinstance(item, list):
            node['array_lengths'].append(len(item))
            for child in item:
                walk(child, path + '[]', path)

    walk(value)
    for path, node in nodes.items():
        node['types'] = dict(sorted(node['types'].items()))
        node['array_lengths'].sort()
        if node['field']:
            node['coverage'] = {'present': node['occurrences'],
                                'objects': nodes[node['parent']]['types'].get('object', 0)}
    return nodes


def _block_types(response: dict) -> dict:
    overview = response.get('ai_overview')
    blocks = overview.get('text_blocks') if isinstance(overview, dict) else None
    if not isinstance(blocks, list):
        return {}
    return dict(sorted(Counter(block.get('type', '<missing>') if isinstance(block.get('type'), str)
                               else '<invalid>' for block in blocks if isinstance(block, dict)).items()))


def compare_json(scraper: dict, serpapi: dict) -> dict:
    """Compare observed schemas, never scalar wording or positional identities."""
    if not isinstance(scraper, dict) or not isinstance(serpapi, dict):
        raise ValueError('Both responses must be JSON objects')
    left, right = _inventory(scraper), _inventory(serpapi)
    kinds = ('missing_fields', 'type_differences', 'empty_field_gaps',
             'field_coverage_gaps', 'array_count_differences')
    report = {'search_details': {kind: [] for kind in kinds},
              'no_observed_scraper_items': [],
              'aio_block_types': {'scraper': _block_types(scraper), 'serpapi': _block_types(serpapi)},
              'inventories': {'scraper': left, 'serpapi': right}}
    blocked = set()

    def ancestors(path):
        parent = right[path]['parent']
        while parent:
            yield parent
            parent = right[parent]['parent']

    for path, api in right.items():
        if path == '$' or any(parent in blocked for parent in ancestors(path)):
            continue
        bucket = report['search_details']
        local = left.get(path)
        if local is None:
            parent = left.get(api['parent'])
            if path.endswith('[]') and parent and 'array' in parent['types']:
                report['no_observed_scraper_items'].append(path)
                blocked.add(path)
                continue
            descendants = [child for child in right if path in ancestors(child)]
            finding = {'path': path, 'serpapi_types': api['types'], 'available_descendants': descendants}
            if 'coverage' in api:
                finding['serpapi_coverage'] = api['coverage']
            bucket['missing_fields'].append(finding)
            blocked.add(path)
            continue
        new_types = set(api['types']) - set(local['types'])
        if new_types:
            bucket['type_differences'].append({'path': path, 'scraper_types': local['types'],
                                               'serpapi_types': api['types']})
        if local['empty'] * api['occurrences'] > api['empty'] * local['occurrences']:
            bucket['empty_field_gaps'].append({'path': path,
                'scraper': {'empty': local['empty'], 'observed': local['occurrences']},
                'serpapi': {'empty': api['empty'], 'observed': api['occurrences']}})
        if 'coverage' in api and 'coverage' in local:
            a, b = local['coverage'], api['coverage']
            if a['present'] * b['objects'] < b['present'] * a['objects']:
                bucket['field_coverage_gaps'].append({'path': path, 'scraper': a, 'serpapi': b})
        if 'array' in api['types'] and 'array' in local['types']:
            if local['array_lengths'] != api['array_lengths']:
                bucket['array_count_differences'].append({'path': path,
                    'scraper_lengths': local['array_lengths'], 'serpapi_lengths': api['array_lengths'],
                    'note': 'Sorted observed lengths, not matched array identities.'})
        if new_types and not set(api['types']) & set(local['types']):
            blocked.add(path)
    return report


def _read_object(path: str) -> dict:
    with open(path, encoding='utf-8') as file:
        value = json.load(file)
    if not isinstance(value, dict) or not value:
        raise ValueError('Expected a nonempty native single-query response object')
    native_keys = {'search_parameters', 'search_metadata', 'organic_results', 'ai_overview', 'error'}
    is_batch = (not native_keys.intersection(value) and all(
        isinstance(item, dict) and native_keys.intersection(item) for item in value.values()))
    if is_batch or ('prompt' in value and isinstance(value.get('google'), dict)):
        raise ValueError('Expected a native single-query object, not a batch or wrapped response')
    if 'search_parameters' in value and not isinstance(value['search_parameters'], dict):
        raise ValueError('search_parameters must be an object')
    return value


def compare_main_body(left: dict, right: dict) -> dict:
    """Compare only scoped AIO/organic content, not service metadata or modules."""
    def payload(response, provider):
        record = normalize_response(response, provider=provider, snapshot_id="comparison")
        if record['aio_status'] in ('invalid', 'error', 'unresolved', 'empty'):
            raise ValueError(f"{provider} content is not comparable: {record['aio_status']}")
        core = {"organic_results": record["organic_results"]}
        if record["ai_overview"] is not None:
            core["ai_overview"] = {"text_blocks": record["ai_overview"]["blocks"],
                                   "references": record["ai_overview"]["references"]}
        return core
    return compare_json(payload(left, "custom"), payload(right, "serpapi"))


def _validate_alignment(left: dict, right: dict) -> None:
    for response in (left, right):
        if not isinstance(response.get('search_parameters', {}), dict):
            raise ValueError('Response search_parameters must be an object')
    for key in ('q', 'hl', 'gl'):
        a = left.get('search_parameters', {}).get(key)
        b = right.get('search_parameters', {}).get(key)
        if a is not None and b is not None and a != b:
            raise ValueError(f'Response search_parameters.{key} do not match')


def _status(response: dict) -> str:
    status = normalize_response(response, provider='custom', snapshot_id='status')['aio_status']
    if status in ('invalid', 'error'):
        return 'error'
    if status in ('unresolved', 'empty'):
        return 'unresolved'
    return 'ok'


def _write_json(path: Path, value: Any, *, replace: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            json.dump(value, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write('\n')
        if replace:
            os.replace(temporary, path)
        else:
            # Publish atomically without overwriting a concurrent writer's file.
            os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _paths(prefix: str, *, offline: bool = False) -> dict[str, Path]:
    names = ('comparison',) if offline else ('scraper', 'serpapi', 'comparison')
    paths = {name: Path(f'{prefix}.{name}.json') for name in names}
    paths['main_body'] = Path(f'{prefix}.main-body')
    if any(path.exists() for path in paths.values()):
        raise ValueError('Output already exists; choose a new --out-prefix')
    return paths


def _secrets() -> tuple[str, ...]:
    names = ('SERPAPI_API_KEY', 'TWOCAPTCHA_API_KEY', 'DATAIMPULSE_PROXY_USER',
             'DATAIMPULSE_PROXY_PASSWORD', 'DATAIMPULSE_PROXY_URL', 'SUPABASE_SECRET_KEY',
             'SUPABASE_ACCESS_TOKEN')
    return tuple(os.environ[name] for name in names if os.environ.get(name))


class _SafeStderr:
    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        self.stream.write(sanitize(text, _secrets()))
        return len(text)

    def flush(self):
        self.stream.flush()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _failure(query: str, hl: str, gl: str, message: str) -> dict:
    return {'search_parameters': {'engine': 'google', 'q': query, 'hl': hl, 'gl': gl},
            'error': {'code': 'CAPTURE_ERROR', 'message': message}}


async def _capture(queries: list[str], args, paths: dict[str, Path], api_key: str) -> int:
    batch = bool(args.queries) or args.runs > 1
    left, right = {}, {}
    records = []
    export_saved = False
    report = {'schema_version': 1, 'mode': 'fresh', 'scope': 'main-body',
              'started_at': _now(), 'status': 'running',
              'planned_query_count': len(queries), 'completed_queries': 0, 'comparable_queries': 0,
              'note': 'Observed JSON gaps, not proof of scraper defects. Scalar wording is ignored. '
                      'Matching hl/gl does not match IP, sessions or personalization.'}
    if batch:
        report['queries'] = {}
    saved = False

    def save():
        nonlocal saved
        for name, value in (('scraper', left), ('serpapi', right), ('comparison', report)):
            _write_json(paths[name], sanitize(value, _secrets()), replace=saved)
        saved = True

    def save_export():
        nonlocal export_saved
        if not export_saved:
            export_records(records, paths['main_body'])
            export_saved = True

    save()
    try:
        async with a.GoogleAIOScraper(engine=args.browser, proxy_mode=args.proxy, timeout_ms=args.timeout) as scraper:
            for index, query in enumerate(queries, 1):
                sources = {}
                responses = {}
                for source in ('scraper', 'serpapi'):
                    started_at, start = _now(), time.perf_counter()
                    try:
                        if source == 'scraper':
                            response = await scraper.search(query, args.hl, args.gl)
                            if not isinstance(response, dict) or not response:
                                raise ValueError('Scraper returned an invalid response object')
                            status, error, evidence = _status(response), response.get('error'), {}
                        else:
                            capture = await asyncio.to_thread(fetch_search, query, api_key=api_key,
                                hl=args.hl, gl=args.gl, timeout=args.api_timeout)
                            status, error, evidence = capture['status'], capture.get('error'), capture['evidence']
                            response = capture['response']
                            if response is None:
                                original = evidence.get('initial_response')
                                response = (original if isinstance(original, dict) else
                                            _failure(query, args.hl, args.gl, error or 'Request failed'))
                        sources[source] = {'status': status, 'error': error, 'evidence': evidence}
                    except Exception as exc:
                        error = sanitize(str(exc), _secrets())
                        response = _failure(query, args.hl, args.gl, error)
                        sources[source] = {'status': 'error', 'error': error, 'evidence': {}}
                    sources[source]['timing'] = {'started_at': started_at, 'finished_at': _now(),
                                                'elapsed_seconds': round(time.perf_counter() - start, 6)}
                    responses[source] = response
                comparison = None
                if all(source['status'] == 'ok' for source in sources.values()):
                    try:
                        _validate_alignment(responses['scraper'], responses['serpapi'])
                        comparison = compare_main_body(responses['scraper'], responses['serpapi'])
                    except (ValueError, RecursionError) as exc:
                        sources['serpapi']['status'] = 'error'
                        sources['serpapi']['error'] = sanitize(str(exc), _secrets())
                query_id = f'P{(index - 1) % args.query_count + 1:02d}'
                run_index = (index - 1) // args.query_count + 1
                key = f'{query_id}_run{run_index}' if args.runs > 1 else query_id
                pair = {'query': query, 'query_id': query_id, 'run_index': run_index,
                        'hl': args.hl, 'gl': args.gl, 'sources': sources, 'comparison': comparison}
                for source, provider in (('scraper', 'custom'), ('serpapi', 'serpapi')):
                    record = normalize_response(responses[source], provider=provider, query_id=query_id,
                        snapshot_id=f'{Path(args.out_prefix).name}:{key}',
                        source_file=str(paths[source]), captured_at=sources[source]['timing']['started_at'])
                    record['provenance']['run_index'] = run_index
                    record['capture_status'] = sources[source]['status']
                    if sources[source]['status'] != 'ok':
                        record['aio_status'] = 'unresolved' if sources[source]['status'] == 'unresolved' else 'error'
                        record['error'] = sources[source]['error']
                    records.append(sanitize(record, _secrets()))
                if batch:
                    left[key], right[key] = responses['scraper'], responses['serpapi']
                    report['queries'][key] = pair
                else:
                    left, right = responses['scraper'], responses['serpapi']
                    report.update(pair)
                report['completed_queries'] = index
                report['comparable_queries'] += int(comparison is not None)
                save()
                print(f'[comparison] {query_id}: scraper={sources["scraper"]["status"]}, '
                      f'serpapi={sources["serpapi"]["status"]}', file=sys.stderr)
                if index < len(queries) and args.delay:
                    await asyncio.sleep(args.delay)
        report['status'] = 'complete' if report['comparable_queries'] == len(queries) else 'partial_failure'
        report['finished_at'] = _now()
        save()
        save_export()
        print(json.dumps({'outputs': {name: str(path) for name, path in paths.items()},
                          'completed_queries': len(queries), 'comparable_queries': report['comparable_queries']}))
        return 0 if report['status'] == 'complete' else 1
    except Exception as exc:
        report.update(status='aborted', run_error=sanitize(str(exc), _secrets()), finished_at=_now())
        save()
        save_export()
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False, description=__doc__)
    parser.add_argument('query', nargs='?')
    parser.add_argument('--queries', help='One query per nonblank/noncomment line')
    parser.add_argument('--scraper-json', help='Offline native single-query scraper response')
    parser.add_argument('--serpapi-json', help='Offline native single-query SerpAPI response')
    parser.add_argument('--out-prefix', required=True)
    parser.add_argument('--runs', type=int, default=1, help='Fresh repeat rounds per input query (default: 1)')
    parser.add_argument('--browser', choices=('chromium', 'firefox'), help='Override GOOGLE_AIO_BROWSER')
    parser.add_argument('--proxy', choices=('direct', 'dataimpulse'), help='Override GOOGLE_AIO_PROXY; otherwise defaults to direct')
    parser.add_argument('--hl', default='en')
    parser.add_argument('--gl', default='us')
    parser.add_argument('--delay', type=float, default=3.0)
    parser.add_argument('--timeout', type=int, default=90000, help='Browser navigation timeout in milliseconds')
    parser.add_argument('--api-timeout', type=float, default=30.0, help='Each SerpAPI request timeout in seconds')
    args = parser.parse_args(argv)
    try:
        offline = bool(args.scraper_json or args.serpapi_json)
        if offline:
            if not args.scraper_json or not args.serpapi_json or args.query or args.queries or args.runs != 1:
                raise ValueError('Offline mode requires both JSON files and no query inputs')
            left, right = _read_object(args.scraper_json), _read_object(args.serpapi_json)
            _validate_alignment(left, right)
            statuses = {'scraper': _status(left), 'serpapi': _status(right)}
            report = {'schema_version': 1, 'mode': 'offline', 'scope': 'main-body', 'statuses': statuses,
                      'comparison': compare_main_body(left, right) if all(s == 'ok' for s in statuses.values()) else None,
                      'note': 'Observed JSON gaps, not proof of scraper defects. Scalar wording is ignored.'}
            paths = _paths(args.out_prefix, offline=True)
            records = [normalize_response(value, provider=provider, snapshot_id=Path(args.out_prefix).name,
                                          source_file=source_file)
                       for value, provider, source_file in ((left, 'custom', args.scraper_json),
                                                           (right, 'serpapi', args.serpapi_json))]
            export_records(records, paths['main_body'])
            _write_json(paths['comparison'], sanitize(report))
            print(json.dumps({'output': str(paths['comparison']), 'main_body': str(paths['main_body']), 'statuses': statuses}))
            return 0 if report['comparison'] is not None else 1
        if bool(args.query) == bool(args.queries):
            raise ValueError('Supply exactly one query or --queries file')
        if args.runs < 1:
            raise ValueError('--runs must be at least 1')
        if (not math.isfinite(args.delay) or args.delay < 0 or args.timeout <= 0
                or not math.isfinite(args.api_timeout) or args.api_timeout <= 0):
            raise ValueError('Delay must be finite/nonnegative and timeouts finite/positive')
        if not args.hl.strip() or not args.gl.strip():
            raise ValueError('Language and country must be nonempty')
        queries = a._read_query_file(args.queries) if args.queries else [args.query.strip()]
        if not queries or any(not query for query in queries):
            raise ValueError('No nonempty queries to run')
        args.query_count = len(queries)
        queries = queries * args.runs
        paths = _paths(args.out_prefix)
        a._load_env_file()
        api_key = os.getenv('SERPAPI_API_KEY', '').strip()
        if not api_key:
            raise ValueError('Set SERPAPI_API_KEY in .env or the environment before fresh capture')
        with contextlib.redirect_stderr(_SafeStderr(sys.stderr)):
            print(f'[comparison] {len(queries)} fresh query pairs; up to {2 * len(queries)} SerpAPI requests '
                  '(one search plus an optional AI Overview follow-up per query). No pagination or API retries.',
                  file=sys.stderr)
            return asyncio.run(_capture(queries, args, paths, api_key))
    except Exception as exc:
        print(json.dumps({'error': sanitize(str(exc), _secrets())}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
