"""No-paid-service end-to-end checks for the single retained storage workflow."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import aio_scraper as custom
import compare_serpapi as capture
import comparison_storage as storage


class FinalizedWorkflowTests(unittest.TestCase):
    def test_proxy_capture_both_providers_repeats_json_and_one_row_upload(self):
        original_session = custom.GoogleAIOScraper
        session_options = []
        html = '''<div id="search"><div id="rso">
          <div id="eKIzJc"><div data-subtree="aimfl,mfl">
            <p>Full answer <a href="https://source.example/a">source</a></p>
            <ul><li>Parent<ul><li>Child</li></ul></li></ul>
            <table><tr><td>H</td><td>V</td></tr></table>
          </div><div data-src-id="1"><a href="https://source.example/a" aria-label="Source - Publisher"></a></div></div>
          <div class="g"><a href="https://organic.example/a"><h3>Result</h3></a>
            <div class="VwiC3b">Full organic snippet</div></div>
        </div></div>'''

        class Session:
            def __init__(self, **kwargs):
                configured = original_session(**kwargs)
                session_options.append((configured.engine, configured.proxy))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def search(self, query, hl, gl):
                return custom.parse_search_html(html, query, hl, gl)

        def api_response(query, **kwargs):
            return {'status': 'ok', 'error': None, 'evidence': {}, 'response': {
                'search_parameters': {'q': query, 'hl': kwargs['hl'], 'gl': kwargs['gl']},
                'ai_overview': {'text_blocks': [{'type': 'paragraph', 'snippet': 'API answer', 'reference_indexes': [7]}],
                    'references': [{'index': 7, 'link': 'https://source.example/b', 'snippet': 'Complete reference snippet'}]},
                'organic_results': [{'position': 4, 'title': 'Result', 'link': 'https://organic.example/b',
                    'snippet': 'Complete API snippet', 'date': '2 days ago', 'thumbnail': 'omit'}]}}

        env = {'SERPAPI_API_KEY': 'FAKE_SERP_KEY', 'TWOCAPTCHA_API_KEY': 'FAKE_SOLVER_KEY',
               'DATAIMPULSE_PROXY_USER': 'proxy-user', 'DATAIMPULSE_PROXY_PASSWORD': 'proxy-password',
               'SUPABASE_PROJECT_ID': 'a' * 20, 'SUPABASE_ACCESS_TOKEN': 'fake-management-token'}
        batch = '2fa1838a-b6be-473a-8c38-cbf640461620'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queries = root / 'queries.txt'
            queries.write_text('same query\nsame query\n')
            prefix = root / 'capture'
            with patch.dict(os.environ, env, clear=True), patch.object(custom, '_load_env_file'), \
                    patch.object(custom, 'GoogleAIOScraper', Session), \
                    patch.object(capture, 'fetch_search', side_effect=api_response) as api, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = capture.main(['--queries', str(queries), '--runs', '2', '--delay', '0',
                    '--browser', 'firefox', '--proxy', 'dataimpulse', '--out-prefix', str(prefix)])
            self.assertEqual(status, 0)
            self.assertEqual(api.call_count, 4)
            self.assertEqual(session_options[0][0], 'firefox')
            self.assertEqual(session_options[0][1]['username'], 'proxy-user')
            self.assertEqual(session_options[0][1]['password'], 'proxy-password')
            records_path = root / 'capture.main-body' / 'records.json'
            records = json.loads(records_path.read_text())
            self.assertEqual(len(records), 8)
            self.assertEqual({r['provenance']['query_id'] for r in records}, {'P01', 'P02'})
            self.assertEqual({r['provenance']['run_index'] for r in records}, {1, 2})
            receipt = {'comparison_id': batch, 'scrape_runs': 4, 'queries': 8}
            output = root / 'sql'
            with patch.dict(os.environ, env, clear=True), patch.object(custom, '_load_env_file'), \
                    patch.object(storage, 'execute_sql', return_value=[receipt]) as database, \
                    contextlib.redirect_stdout(io.StringIO()):
                status = storage.main(['--input', str(records_path), '--comparison-id', batch,
                                       '--output-dir', str(output), '--upload'])
            self.assertEqual(status, 0)
            database.assert_called_once()
            tables = json.loads(database.call_args.args[1][0])
            self.assertEqual(set(tables), {'scrape_runs', 'queries'})
            self.assertEqual(len(tables['queries']), 8)
            for query in tables['queries']:
                self.assertIsInstance(query['aio_response']['blocks'], list)
                self.assertIsInstance(query['aio_response']['references'], list)
                self.assertEqual(len(query['organic_results']), 1)
            api_rows = [q for q in tables['queries'] if q['aio_response']['text'] == 'API answer']
            self.assertEqual(len(api_rows), 4)
            self.assertEqual(api_rows[0]['aio_response']['references'][0]['index'], 7)
            self.assertEqual(api_rows[0]['organic_results'][0]['position'], 4)
            self.assertNotIn('thumbnail', json.dumps(tables))
            self.assertFalse(list(root.rglob('*.csv')))
            self.assertTrue(json.loads((output / 'manifest.json').read_text())['uploaded'])
            self.assertEqual(json.loads((output / 'upload.json').read_text()), receipt)


if __name__ == '__main__':
    unittest.main()
