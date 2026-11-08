import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import comparison_storage as db

BATCH = '2fa1838a-b6be-473a-8c38-cbf640461620'


def record(provider='custom', run=1, query_id='P01'):
    return {'schema_version': 1, 'provenance': {
        'snapshot_id': f'{provider}:{run}:{query_id}', 'provider': provider,
        'query_id': query_id, 'query': "what's x", 'run_index': run,
        'captured_at': '2026-10-19 11:08:27 UTC', 'source_file': '/saved/source.json',
        'settings': {'hl': 'en', 'gl': 'us', 'device': 'desktop'}},
        'capture_status': 'ok', 'aio_status': 'present', 'warnings': [],
        'ai_overview': {'blocks': [
            {'type': 'paragraph', 'snippet': 'Full body', 'reference_indexes': [7]},
            {'type': 'list', 'list': [{'snippet': 'Parent', 'list': [{'snippet': 'Child'}]}]},
            {'type': 'table', 'table': [['H', 'V'], ['A', 'B']],
             'detailed': [[{'snippet': 'H', 'colspan': 'undefined'}]],
             'formatted': [{'redundant': 'not stored'}]}],
            'references': [{'index': 7, 'link': 'https://example.com/a', 'title': 'A',
                            'source': 'Example', 'snippet': 'Reference snippet'}]},
        'organic_count': 1, 'organic_results': [{'position': 4, 'title': 'Title',
            'link': 'https://example.com/b', 'snippet': 'Full snippet', 'date': '2 days ago',
            'author': 'Author', 'source': 'Publisher', 'displayed_link': 'example.com',
            'sitelinks': {'inline': [{'title': 'More', 'link': 'https://example.com/c', 'thumbnail': 'drop'}]},
            'rich_snippet': {'top': {'extensions': ['text']}},
            'about_page_serpapi_link': 'https://serpapi.com/helper', 'thumbnail': 'drop'}]}


class StorageTests(unittest.TestCase):
    def test_preserves_content_and_filters_nonessential_fields(self):
        r = record()
        original = copy.deepcopy(r)
        data = db.normalize_records([r], BATCH)
        self.assertEqual(r, original)
        q = data['queries'][0]
        self.assertEqual(q['captured_at'], '2026-10-19T11:08:27+00:00')
        self.assertIsNone(q['google_domain'])
        self.assertEqual(set(data), {'scrape_runs', 'queries'})
        self.assertEqual(len(data['queries']), 1)
        self.assertEqual(q['response_schema_version'], 1)
        a = q['aio_response']
        self.assertIn('Parent\n  • Child', a['text'])
        self.assertIn('H | V', a['text'])
        self.assertNotIn('formatted', a['blocks'][2])
        self.assertNotIn('colspan', a['blocks'][2]['detailed'][0][0])
        self.assertEqual(a['references'][0]['index'], 7)
        self.assertEqual(a['references'][0]['snippet'], 'Reference snippet')
        self.assertNotIn('reference_index', a['references'][0])
        organic = q['organic_results'][0]
        self.assertEqual((organic['position'], organic['ordinal']), (4, 1))
        self.assertEqual(organic['date'], '2 days ago')
        self.assertNotIn('thumbnail', json.dumps(organic))
        self.assertNotIn('serpapi.com/helper', json.dumps(data))

    def test_new_aio_content_fields_are_preserved_with_a_warning(self):
        r = record()
        r['ai_overview']['blocks'][0]['additional_text'] = {'detail': 'Keep this new content', 'thumbnail': 'drop'}
        out = db.normalize_records([r], BATCH)
        q = out['queries'][0]
        self.assertEqual(q['aio_response']['blocks'][0]['additional_text'], {'detail': 'Keep this new content'})
        self.assertIn('unknown_aio_content_field:additional_text', q['warnings'])
        self.assertIn('unknown_aio_content_field:detail', q['warnings'])

    def test_many_blocks_and_results_still_take_one_response_row(self):
        r = record()
        r['ai_overview']['blocks'] = [
            {'type': 'paragraph', 'snippet': f'Paragraph {i}'} for i in range(200)]
        r['organic_results'] = [
            {**r['organic_results'][0], 'position': i + 1, 'title': f'Result {i}'}
            for i in range(200)]
        r['organic_count'] = 200
        out = db.normalize_records([r], BATCH)
        self.assertEqual(set(out), {'scrape_runs', 'queries'})
        self.assertEqual(len(out['queries']), 1)
        q = out['queries'][0]
        self.assertEqual(len(q['aio_response']['blocks']), 200)
        self.assertEqual(len(q['organic_results']), 200)
        self.assertEqual(q['organic_results'][-1]['title'], 'Result 199')
        self.assertEqual(out['scrape_runs'][0]['metadata']['organic_results'], 200)

    def test_provider_round_and_query_id_separation_is_stable(self):
        records = [record(), record('serpapi'), record(run=2)]
        first = db.normalize_records(records, BATCH)
        second = db.normalize_records(list(reversed(records)), BATCH)
        self.assertEqual(first, second)
        self.assertEqual(len(first['scrape_runs']), 3)
        self.assertEqual(len({r['run_id'] for r in first['scrape_runs']}), 3)
        with self.assertRaises(ValueError):
            db.normalize_records([record(), record()], BATCH)

    def test_absence_and_failures_are_not_fabricated_as_overviews(self):
        r = record()
        r.update(aio_status='absent', ai_overview=None)
        out = db.normalize_records([r], BATCH)
        self.assertIsNone(out['queries'][0]['aio_response'])
        self.assertEqual(out['queries'][0]['aio_status'], 'absent')
        r.update(capture_status='error', aio_status='error', error='network failed', organic_results=[], organic_count=0)
        out = db.normalize_records([r], BATCH)
        self.assertEqual(out['queries'][0]['status'], 'error')
        self.assertEqual(out['queries'][0]['error_message'], 'network failed')

    def test_missing_indexes_are_not_inferred_and_bad_mappings_warn(self):
        r = record()
        del r['ai_overview']['references'][0]['index']
        out = db.normalize_records([r], BATCH)
        self.assertNotIn('index', out['queries'][0]['aio_response']['references'][0])
        self.assertIn('unmapped_citation_index:7', out['queries'][0]['warnings'])

    def test_secrets_are_sanitized_before_projection(self):
        r = record()
        r['api_key'] = 'secret-123'
        r['ai_overview']['blocks'][0]['snippet'] = 'echo secret-123'
        out = db.normalize_records([r], BATCH)
        self.assertNotIn('secret-123', json.dumps(out))

    def test_same_query_id_cannot_pair_different_query_text(self):
        right = record('serpapi')
        right['provenance']['query'] = 'unrelated query'
        with self.assertRaises(ValueError):
            db.normalize_records([record(), right], BATCH)

    def test_invalid_input_fails_before_upload(self):
        variants = []
        for mutate in [lambda r: r['provenance'].update(run_index=True),
                       lambda r: r['provenance'].update(captured_at='yesterday'),
                       lambda r: r.update(organic_count=2),
                       lambda r: r.update(organic_count=True),
                       lambda r: r.update(schema_version=True),
                       lambda r: r.update(aio_status='absent'),
                       lambda r: r['ai_overview']['references'].__setitem__(0, 'bad'),
                       lambda r: r['organic_results'].__setitem__(0, 'bad'),
                       lambda r: r['organic_results'][0].update(link='javascript:alert(1)'),
                       lambda r: r['ai_overview']['blocks'].append({'type': 'chart', 'snippet': 'lost'})]:
            r = record(); mutate(r); variants.append(r)
        for r in variants:
            with self.subTest(record=r), patch.object(db, 'execute_sql') as network:
                with self.assertRaises(ValueError):
                    db.upload_records([r], BATCH, 'a' * 20, 'token')
                network.assert_not_called()

    def test_migration_checks_content_before_retiring_child_tables(self):
        sql = (Path(__file__).resolve().parents[1] / 'supabase/migrations/20261028032336_query_response_jsonb.sql').read_text()
        self.assertIn('ADD COLUMN aio_response jsonb', sql)
        self.assertIn("ADD COLUMN organic_results jsonb NOT NULL DEFAULT '[]'::jsonb", sql)
        self.assertIn('IS DISTINCT FROM b.original_query', sql)
        self.assertIn('q.aio_response IS DISTINCT FROM b.aio_response', sql)
        self.assertIn('q.organic_results IS DISTINCT FROM b.organic_results', sql)
        self.assertIn('security_invoker = true', sql)
        self.assertIn('queries_present_aio_content', sql)
        self.assertNotIn('DROP TABLE public.queries', sql)
        self.assertNotIn('DROP TABLE public.scrape_runs', sql)
        self.assertNotIn('CASCADE;', sql)
        self.assertLess(sql.index('UPDATE public.queries'), sql.index('DROP TABLE public.ai_references'))
        self.assertLess(sql.index('CREATE OR REPLACE VIEW'), sql.index('DROP TABLE public.ai_references'))
        self.assertTrue(sql.rstrip().endswith('COMMIT;'))

    def test_sql_export_contains_no_unquoted_user_sql(self):
        r = record(); r['provenance']['query'] = "x'); DROP TABLE public.queries;--"
        out = db.normalize_records([r], BATCH)
        seed = db.sql_export(out)
        self.assertNotIn('DROP TABLE', seed)
        self.assertIn("decode('", seed)
        self.assertIn('BEGIN;', seed)
        self.assertNotIn('$1', seed)
        self.assertNotIn('ON CONFLICT', db.UPLOAD_SQL)
        self.assertNotIn('DELETE', db.UPLOAD_SQL)

    def test_offline_cli_exports_only_two_tables_with_schema_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'records.json'
            source.write_text(json.dumps([record()]))
            output = Path(tmp) / 'sql'
            with patch('aio_scraper._load_env_file'), patch.object(db, 'execute_sql') as net, contextlib.redirect_stdout(io.StringIO()):
                code = db.main(['--input', str(source), '--comparison-id', BATCH, '--output-dir', str(output)])
            self.assertEqual(code, 0)
            net.assert_not_called()
            manifest = json.loads((output / 'manifest.json').read_text())
            self.assertEqual(manifest['storage_schema_version'], 3)
            self.assertEqual(manifest['response_schema_version'], 1)
            self.assertEqual(manifest['counts'], {'scrape_runs': 1, 'queries': 1})
            self.assertFalse(manifest['uploaded'])
            tables = json.loads((output / 'tables.json').read_text())
            self.assertEqual(set(tables), {'scrape_runs', 'queries'})
            seed = (output / 'data.sql').read_text()
            encoded = seed.split("decode('", 1)[1].split("'", 1)[0]
            self.assertEqual(json.loads(bytes.fromhex(encoded)), tables)

    def test_receipt_must_match_all_expected_counts(self):
        expected = {'comparison_id': BATCH, 'scrape_runs': 1, 'queries': 1}
        with patch.object(db, 'execute_sql', return_value=[expected]) as net:
            self.assertEqual(db.upload_records([record()], BATCH, 'a' * 20, 'token'), expected)
            self.assertEqual(net.call_args.args[0], db.UPLOAD_SQL)
        for bad_count in (0, True, 1.0):
            with self.subTest(count=bad_count), patch.object(db, 'execute_sql', return_value=[{**expected, 'queries': bad_count}]):
                with self.assertRaisesRegex(ValueError, 'receipt'):
                    db.upload_records([record()], BATCH, 'a' * 20, 'token')


if __name__ == '__main__':
    unittest.main()
