import asyncio
import json
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aio_scraper as a
import run_supabase_batch as batch
import supabase_results as storage


class BatchCaptureTests(unittest.TestCase):
    def test_saves_lossless_results_and_explicit_sanitized_errors_in_order(self):
        class FakeScraper:
            def __init__(self, **kwargs):
                self.config = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def search(self, query, hl, gl):
                if query == 'bad':
                    raise a.ChallengeDetected('Rejected https://www.google.com/sorry/?token=secret-value')
                if query == 'plain':
                    return {'search_parameters': {'q': query, 'hl': hl, 'gl': gl}, 'organic_results': []}
                return {
                    'search_parameters': {'q': query, 'hl': hl, 'gl': gl},
                    'ai_overview': {'text_blocks': [{'type': 'paragraph', 'snippet': '=Complete answer ' + 'x' * 6000}], 'references': []},
                    'organic_results': [],
                }

        with tempfile.TemporaryDirectory() as tmp, patch.object(batch.a, 'GoogleAIOScraper', FakeScraper):
            prefix = Path(tmp) / 'output'
            snapshot = asyncio.run(batch.collect_batch(['good', 'plain', 'bad'], prefix, delay=0))
            saved = json.loads(prefix.with_suffix('.json').read_text())
            self.assertEqual(saved, snapshot)
            self.assertEqual([r['query_id'] for r in saved['results']], ['P01', 'P02', 'P03'])
            self.assertEqual(saved['planned_query_count'], 3)
            self.assertNotIn('secret-value', saved['results'][2]['result']['error']['message'])
            text = saved['results'][0]['result']['ai_overview']['text_blocks'][0]['snippet']
            self.assertTrue(text.startswith('=Complete'))
            self.assertGreater(len(text), 6000)
            self.assertFalse(prefix.with_suffix('.csv').exists())
            normalized = storage.normalize_response_snapshot(saved)
            self.assertEqual(len(normalized['queries']), 3)
            self.assertEqual(normalized['queries'][2]['status'], 'error')
            self.assertIsNone(normalized['queries'][1]['aio_response'])
            self.assertEqual(normalized['queries'][0]['aio_response']['paragraph'], text)

    def test_json_checkpoints_preserve_harmless_urls_full_text_and_structure(self):
        snapshot = {'run_id': '00000000-0000-0000-0000-000000000001', 'results': [
            {'query_id': 'P01', 'result': {
                'search_parameters': {'q': '=Cities 城市', 'hl': 'en', 'gl': 'us'},
                'ai_overview': {'text_blocks': [
                    {'type': 'paragraph', 'snippet': 'Complete body ' + 'x' * 8000,
                     'snippet_links': [{'text': 'source', 'link': 'https://example.com/a?topic=social+security&n=2#part'}]},
                    {'type': 'table', 'table': [['H', 'V'], ['A', 'B']]}],
                    'references': [{'index': 7, 'link': 'https://www.google.com/goto?url=opaque&source=web'}]},
                'organic_results': [{'position': 4, 'link': 'https://example.com/search?q=public+data',
                                     'title': 'Original title', 'snippet': 'Full snippet'}]}}]}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(batch.os.environ, {}, clear=True):
            path = Path(tmp) / 'capture.json'
            batch._save_snapshot(snapshot, path)
            self.assertEqual(json.loads(path.read_text()), snapshot)

    def test_json_checkpoints_redact_solver_and_proxy_credentials(self):
        snapshot = {'results': [{'message': 'echo SOLVER_SECRET123 https://user:proxy-password@proxy.example/'}]}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(batch.os.environ, {'TWOCAPTCHA_API_KEY': 'SOLVER_SECRET123'}):
            path = Path(tmp) / 'capture.json'
            batch._save_snapshot(snapshot, path)
            content = path.read_text()
            self.assertNotIn('SOLVER_SECRET123', content)
            self.assertNotIn('proxy-password', content)
            self.assertIn('SOLVER_SECRET123', snapshot['results'][0]['message'])

    def test_incomplete_snapshot_is_not_uploadable(self):
        class BrokenScraper:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                raise a.ScrapeError('Cannot start browser')

            async def __aexit__(self, *args):
                pass

        with tempfile.TemporaryDirectory() as tmp, patch.object(batch.a, 'GoogleAIOScraper', BrokenScraper):
            prefix = Path(tmp) / 'partial'
            with self.assertRaises(a.ScrapeError):
                asyncio.run(batch.collect_batch(['good'], prefix, delay=0))
            snapshot = json.loads(prefix.with_suffix('.json').read_text())
            with self.assertRaises(storage.SnapshotError):
                storage.normalize_response_snapshot(snapshot)

    def test_partial_nonempty_snapshot_is_rejected(self):
        snapshot = {'run_id': '00000000-0000-0000-0000-000000000001', 'label': '',
                    'source_file': 'partial.json', 'captured_at': '2026-09-30T00:00:00Z',
                    'planned_query_count': 2, 'results': [{'query_id': 'P01', 'result': {
                        'search_parameters': {'q': 'only first query', 'hl': 'en', 'gl': 'us'}}}]}
        with self.assertRaises(storage.SnapshotError):
            storage.normalize_response_snapshot(snapshot)


class BatchCliTests(unittest.TestCase):
    def test_upload_file_wiring_and_exit_codes_for_ok_and_error_runs(self):
        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as tmp:
                result = {'search_parameters': {'q': 'q', 'hl': 'en', 'gl': 'us'}}
                if failed:
                    result['error'] = {'code': 'SCRAPE_ERROR', 'message': 'failed'}
                snapshot = {'run_id': '00000000-0000-0000-0000-000000000001', 'label': '',
                            'source_file': 'saved.json', 'captured_at': '2026-09-30T00:00:00Z',
                            'planned_query_count': 1, 'results': [{'query_id': 'P01', 'result': result}]}
                path = Path(tmp) / 'saved.json'
                path.write_text(json.dumps(snapshot))
                summary = {'run_id': snapshot['run_id'], 'queries': 1, 'ai_overviews': 0,
                           'ai_references': 0, 'organic_results': 0}
                output = io.StringIO()
                with patch.object(batch.a, '_load_env_file'), patch.dict(batch.os.environ, {
                        'SUPABASE_PROJECT_ID': 'a' * 20, 'SUPABASE_ACCESS_TOKEN': 'sbp_test'}), \
                        patch.object(batch, 'upload_snapshot', return_value=summary) as upload, \
                        contextlib.redirect_stdout(output):
                    status = batch.main(['--upload-file', str(path)])
                self.assertEqual(status, 1 if failed else 0)
                upload.assert_called_once_with(snapshot, 'a' * 20, 'sbp_test')
                self.assertTrue(json.loads(output.getvalue())['uploaded'])
                self.assertTrue(path.exists())

    def test_retired_csv_out_option_is_not_an_alias_for_json_output(self):
        snapshot = {'run_id': '00000000-0000-0000-0000-000000000001', 'captured_at': '2026-01-01T00:00:00Z',
                    'results': [{'query_id': 'P01', 'result': {'search_parameters': {'q': 'q'}, 'organic_results': []}}]}
        with patch.object(batch.a, '_load_env_file'), \
                patch.object(batch, 'collect_batch', new_callable=AsyncMock, return_value=snapshot) as capture, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                batch.main(['q', '--out', 'legacy.csv'])
        self.assertEqual(error.exception.code, 2)
        capture.assert_not_awaited()

    def test_env_error_is_reported_without_traceback(self):
        output = io.StringIO()
        with patch.object(batch.a, '_load_env_file', side_effect=a.ScrapeError('Cannot load env')), \
                contextlib.redirect_stdout(output):
            status = batch.main(['--upload-file', 'unused.json'])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue())['error'], 'Cannot load env')
