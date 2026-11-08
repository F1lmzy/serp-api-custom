import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import compare_serpapi as c


def response(query):
    return {'search_parameters': {'q': query, 'hl': 'en', 'gl': 'us'},
            'organic_results': [{'position': 1, 'title': 'Title', 'link': 'https://example.org/', 'snippet': 'Full text'}]}


class MainBodyComparisonTests(unittest.TestCase):
    def test_scoped_gap_comparison_ignores_media_and_ancillary_modules(self):
        left = response('q')
        right = {**response('q'), 'ads': [{'title': 'Ad'}], 'related_questions': [{'question': 'Question'}],
                 'inline_videos': [{'title': 'Video'}]}
        right['organic_results'][0]['thumbnail'] = 'image'
        right['organic_results'][0]['date'] = 'Today'
        report = c.compare_main_body(left, right)
        self.assertEqual([r['path'] for r in report['search_details']['missing_fields']],
                         ['$.organic_results[].date'])
        self.assertNotIn('$.ads', report['inventories']['serpapi'])

    def test_fresh_capture_exports_only_common_json_and_preserves_repeats(self):
        events = []

        class Scraper:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def search(self, query, hl, gl):
                events.append(query)
                return response(query)

        def fetch(query, **kwargs):
            return {'status': 'ok', 'response': {**response(query), 'ads': [{'title': 'Ad'}]},
                    'error': None, 'evidence': {}}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queries = root / 'queries.txt'
            queries.write_text('q1\nq2\n')
            with patch.dict(os.environ, {'SERPAPI_API_KEY': 'FAKESECRET'}), \
                    patch.object(c.a, '_load_env_file'), patch.object(c.a, 'GoogleAIOScraper', Scraper), \
                    patch.object(c, 'fetch_search', side_effect=fetch), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = c.main(['--queries', str(queries), '--runs', '2', '--delay', '0', '--out-prefix', str(root / 'paired')])
            self.assertEqual(status, 0)
            self.assertEqual(events, ['q1', 'q2', 'q1', 'q2'])
            records = json.loads((root / 'paired.main-body' / 'records.json').read_text())
            self.assertEqual(len(records), 8)
            self.assertEqual([r['provenance']['query_id'] for r in records], ['P01', 'P01', 'P02', 'P02'] * 2)
            self.assertEqual(len({(r['provenance']['provider'], r['provenance']['snapshot_id']) for r in records}), 8)
            self.assertTrue(all(r['provenance']['captured_at'] for r in records))
            self.assertNotIn('"ads"', json.dumps(records))
            self.assertEqual([p.name for p in (root / 'paired.main-body').iterdir()], ['records.json'])
            report = json.loads((root / 'paired.comparison.json').read_text())
            self.assertEqual(report['scope'], 'main-body')
            self.assertEqual(report['completed_queries'], 4)
            self.assertEqual(len(report['queries']), 4)
            self.assertIn('ads', json.loads((root / 'paired.serpapi.json').read_text())['P01_run1'])

    def test_export_collision_fails_before_any_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / 'run'
            Path(str(prefix) + '.main-body').mkdir()
            with patch.object(c.a, 'GoogleAIOScraper') as browser, patch.object(c, 'fetch_search') as api, \
                    patch.object(c.a, '_load_env_file'), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(c.main(['q', '--out-prefix', str(prefix)]), 2)
            browser.assert_not_called()
            api.assert_not_called()

    def test_unresolved_custom_shell_skips_missing_field_claims(self):
        self.assertEqual(c._status({'extraction_metadata': {'aio_status': 'unresolved'}}), 'unresolved')

    def test_invalid_or_error_content_is_not_compared_as_empty_results(self):
        for broken in ({'ai_overview': {'text_blocks': 'bad'}},
                       {'extraction_metadata': {'aio_status': 'invalid'}},
                       {'extraction_metadata': {'aio_status': 'error'}},
                       {'organic_results': [None]}):
            self.assertEqual(c._status(broken), 'error')
            with self.assertRaises(ValueError):
                c.compare_main_body(broken, response('q'))


if __name__ == '__main__':
    unittest.main()
