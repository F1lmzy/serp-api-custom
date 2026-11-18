"""Offline before/after oracle for local, untracked research evidence.

Run from the project root with PYTHONPATH=. and --save /tmp/baseline.json
BEFORE editing runtime code, then --check /tmp/baseline.json afterwards.
Never imports operational upload scripts, reads .env, or sends requests.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from unittest.mock import patch

from search_export import load_input
from supabase_results import normalize_records, normalize_response_snapshot, sql_export

COMPARISON_ID = '2fa1838a-b6be-473a-8c38-cbf640461620'
SOURCES = (
    ('pap-300-final.json', 'custom', True),
    ('pap-300-direct.json', 'custom', True),
    ('pap-300-retry.json', 'custom', True),
    ('pap-300-retry2.json', 'custom', True),
    ('pap-300-serpapi.native.json', 'serpapi', False),
)


def documents():
    evidence = {}
    # Ban transport and browser entry even if an offline function regresses.
    with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')), \
            patch('aio_scraper.GoogleAIOScraper.__aenter__', side_effect=AssertionError('browser forbidden')), \
            patch.dict('os.environ', {}, clear=True):
        for name, provider, native in SOURCES:
            path = Path('query_test') / name
            raw = path.read_bytes()
            records = load_input([path], provider=provider)
            rejected = []
            projectable = []
            for record in records:
                try:
                    normalize_records([record], COMPARISON_ID)
                except ValueError as exc:
                    rejected.append({'query_id': record['provenance']['query_id'],
                                     'exception': type(exc).__name__, 'message': str(exc)})
                else:
                    projectable.append(record)
            tables = normalize_records(projectable, COMPARISON_ID)
            seed = sql_export(tables)
            encoded = re.search(r"decode\('([0-9a-f]+)', 'hex'\)", seed)[1]
            assert json.loads(bytes.fromhex(encoded)) == tables, name
            evidence[name] = {
                'source_sha256': hashlib.sha256(raw).hexdigest(),
                'records': records, 'tables': tables, 'sql': seed, 'projection_rejections': rejected,
                'native_tables': normalize_response_snapshot(json.loads(raw)) if native else None,
            }
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--save', type=Path)
    mode.add_argument('--check', type=Path)
    args = parser.parse_args()
    actual = documents()
    if args.save:
        # Exclusive creation prevents accidentally replacing the before oracle.
        with args.save.open('x') as file:
            json.dump(actual, file, ensure_ascii=False, sort_keys=True)
    else:
        expected = json.loads(args.check.read_text())
        assert actual.keys() == expected.keys()
        for name in actual:
            for field, value in actual[name].items():
                assert value == expected[name][field], f'{name}: changed {field}'
    for name, value in actual.items():
        records = value['records']
        print(name, len(records), dict(Counter(r['aio_status'] for r in records)),
              'full records, provenance, tables, fingerprints and SQL checked',
              'projection rejections:', value['projection_rejections'])
    final = actual['pap-300-final.json']['native_tables']['scrape_runs'][0]['metadata']
    assert {key: final[key] for key in ('queries', 'ai_overviews', 'ai_references', 'organic_results')} == {
        'queries': 300, 'ai_overviews': 292, 'ai_references': 2705, 'organic_results': 2684}
    assert final['fingerprint'] == 'b648d2fcc853d161710176c0241b282595268bb0d1ddb230c5f31d60c7425115'
    print('Historical 300-response counts AND fingerprint match (offline cross-check only).')


if __name__ == '__main__':
    main()
