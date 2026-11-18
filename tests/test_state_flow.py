"""Offline characterization of state ownership and failure boundaries."""
import asyncio
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import urllib.error

import aio_scraper as a
import compare_serpapi as comparison
import comparison_storage
import run_supabase_batch as batch
import supabase_results as storage
from test_aio_streaming import shell
from test_response_storage import native_capture, shared_records, COMPARISON_ID, PROJECT_ID, TOKEN


def search_page(documents):
    """Navigation selects a synthetic document; sleeps advance only our clock."""
    clock = [0.0]
    navigations = []

    def html(*args):
        document = documents[min(len(navigations) - 1, len(documents) - 1)]
        return document(clock[0]) if callable(document) else document

    async def navigate(url, **kwargs):
        navigations.append(url)

    async def sleep(milliseconds):
        clock[0] += milliseconds / 1000

    page = SimpleNamespace(
        url='https://www.google.com/search?q=q', goto=AsyncMock(side_effect=navigate),
        content=AsyncMock(side_effect=html), evaluate=AsyncMock(side_effect=html),
        wait_for_timeout=AsyncMock(side_effect=sleep),
        get_by_role=lambda *args, **kwargs: SimpleNamespace(count=AsyncMock(return_value=0)),
    )
    return page, clock, navigations


def serp(query='q'):
    return {'status': 'ok', 'error': None, 'evidence': {},
            'response': {'search_parameters': {'q': query}}}


class QueryStateCharacterizationTests(unittest.IsolatedAsyncioTestCase):
    async def search(self, documents, queries=('q',)):
        scraper = a.GoogleAIOScraper(proxy_mode='direct')
        scraper.page, clock, navigations = search_page(documents)
        results = []
        with patch.object(a.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(a, '_maybe_solve_challenge', AsyncMock(return_value=None)), \
                contextlib.redirect_stderr(io.StringIO()):
            for query in queries:
                results.append(await scraper.search(query))
        return results, navigations, clock[0]

    async def test_exhausted_shell_uses_20_then_30_seconds_and_only_two_navigations(self):
        results, navigations, elapsed = await self.search(['<div id="search">' + shell('', True) + '</div>'])
        self.assertEqual(len(navigations), 2)
        self.assertGreaterEqual(elapsed, 53)  # two initial 1.5s waits plus rendering
        self.assertLess(elapsed, 54)
        self.assertEqual(results[0]['extraction_metadata']['aio_status'], 'unresolved')
        self.assertNotIn('ai_overview', results[0])

    async def test_generation_failure_recovers_then_next_query_has_clean_absence(self):
        failed = '<div id="search"><div id="eKIzJc"><span jsname="L1sC6e">' + \
            "Can't generate an AI overview right now. Try again later.</span></div></div>"
        results, navigations, _ = await self.search([
            failed, '<div id="search">' + shell('<p>Recovered.</p>') + '</div>',
            '<div id="search"></div>'], queries=('first', 'second'))
        self.assertEqual(len(navigations), 3)
        self.assertEqual([r['extraction_metadata']['aio_status'] for r in results], ['present', 'absent'])
        self.assertIn('q=second', navigations[-1])
        self.assertEqual(results[0]['ai_overview']['text_blocks'], [{'type': 'paragraph', 'snippet': 'Recovered.'}])

    async def test_visible_unavailability_currently_also_gets_one_reload(self):
        document = '<div id="search"><div id="eKIzJc">An AI Overview is not available for this search</div></div>'
        results, navigations, _ = await self.search([document])
        self.assertEqual(len(navigations), 2)  # characterize policy, not a new recommendation
        self.assertEqual(results[0]['extraction_metadata']['aio_status'], 'unavailable')

    async def test_disappearing_container_fails_without_reload(self):
        scraper = a.GoogleAIOScraper(proxy_mode='direct')
        scraper.page, clock, navigations = search_page([
            lambda t: '<div id="search">' + (shell('', True) if t < 3 else '') + '</div>'])
        with patch.object(a.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(a, '_maybe_solve_challenge', AsyncMock(return_value=None)):
            with self.assertRaisesRegex(a.ScrapeError, 'AI Overview rendering') as caught:
                await scraper.search('q')
        self.assertIn('disappeared', str(caught.exception.__cause__))
        self.assertEqual(len(navigations), 1)

    async def test_challenge_failure_does_not_poll_or_reload(self):
        scraper = a.GoogleAIOScraper(proxy_mode='direct')
        scraper.page, _, navigations = search_page(['challenge'])
        with patch.object(a, '_maybe_solve_challenge', AsyncMock(side_effect=a.ChallengeDetected('blocked'))), \
                patch.object(scraper, '_wait_for_aio_answer', AsyncMock()) as wait:
            with self.assertRaises(a.ChallengeDetected):
                await scraper.search('q')
        wait.assert_not_awaited()
        self.assertEqual(len(navigations), 1)


class PersistenceFaultTests(unittest.TestCase):
    def test_interruption_keeps_last_complete_checkpoint_and_closes_session(self):
        session = SimpleNamespace(search=AsyncMock(side_effect=[
            {'search_parameters': {'q': 'first'}}, KeyboardInterrupt()]))
        manager = SimpleNamespace(__aenter__=AsyncMock(return_value=session), __aexit__=AsyncMock(return_value=False))
        # Special methods must live on the type for async with.
        class Manager:
            __aenter__ = manager.__aenter__
            __aexit__ = manager.__aexit__
        with tempfile.TemporaryDirectory() as tmp, patch.object(a, 'GoogleAIOScraper', return_value=Manager()), \
                contextlib.redirect_stderr(io.StringIO()):
            prefix = Path(tmp) / 'capture'
            with self.assertRaises(KeyboardInterrupt):
                asyncio.run(batch.collect_batch(['first', 'second'], prefix, delay=0))
            saved = json.loads(prefix.with_suffix('.json').read_text())
            self.assertEqual([r['query_id'] for r in saved['results']], ['P01'])
            self.assertEqual(saved['planned_query_count'], 2)
            with self.assertRaises(storage.SnapshotError):
                storage.normalize_response_snapshot(saved)
        manager.__aexit__.assert_awaited_once()

    def test_failed_checkpoint_replace_retains_previous_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'capture.json'
            batch._save_snapshot({'results': ['first']}, path, create=True)
            with patch.object(Path, 'replace', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    batch._save_snapshot({'results': ['first', 'second']}, path)
            self.assertEqual(json.loads(path.read_text()), {'results': ['first']})

    def test_failed_pair_exports_retain_native_checkpoints_without_more_requests(self):
        manager = AsyncMock()
        manager.__aenter__.return_value.search.return_value = {'search_parameters': {'q': 'q'}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(a, '_load_env_file'), \
                patch.dict('os.environ', {'SERPAPI_API_KEY': 'fake'}), \
                patch.object(a, 'GoogleAIOScraper', return_value=manager), \
                patch.object(comparison, 'fetch_search', return_value=serp()) as fetch, \
                patch.object(comparison, 'export_records', side_effect=OSError('disk full')) as export, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            prefix = str(Path(tmp) / 'pair')
            self.assertEqual(comparison.main(['q', '--out-prefix', prefix]), 2)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(export.call_count, 2)  # existing local-only recovery attempt
            manager.__aenter__.return_value.search.assert_awaited_once()
            manager.__aexit__.assert_awaited_once()
            report = json.loads(Path(prefix + '.comparison.json').read_text())
            self.assertEqual((report['status'], report['completed_queries']), ('aborted', 1))
            self.assertEqual(json.loads(Path(prefix + '.scraper.json').read_text())['search_parameters']['q'], 'q')
            self.assertTrue(Path(prefix + '.serpapi.json').exists())
            self.assertFalse(Path(prefix + '.main-body').exists())

    def test_cancelled_pair_retains_previous_pair_checkpoint_and_closes_browser(self):
        manager = AsyncMock()
        manager.__aexit__.return_value = False
        manager.__aenter__.return_value.search.side_effect = [
            {'search_parameters': {'q': 'first'}}, asyncio.CancelledError()]
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(a, 'GoogleAIOScraper', return_value=manager), \
                patch.object(comparison, 'fetch_search', return_value=serp('first')) as fetch, \
                contextlib.redirect_stderr(io.StringIO()):
            prefix = str(Path(tmp) / 'pair')
            args = SimpleNamespace(queries='queries.txt', runs=1, query_count=2, browser=None,
                                   proxy='direct', timeout=1000, hl='en', gl='us', api_timeout=1,
                                   out_prefix=prefix, delay=0)
            paths = comparison._paths(prefix)
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(comparison._capture(['first', 'second'], args, paths, 'fake'))
            report = json.loads(paths['comparison'].read_text())
            self.assertEqual((report['status'], report['completed_queries']), ('running', 1))
            self.assertEqual(list(json.loads(paths['scraper'].read_text())), ['P01'])
            self.assertEqual(list(json.loads(paths['serpapi'].read_text())), ['P01'])
            fetch.assert_called_once()
            manager.__aexit__.assert_awaited_once()

    def test_pair_record_conversion_is_pure_and_retains_uncertain_content(self):
        responses = {'scraper': {'search_parameters': {'q': 'q'},
                                'extraction_metadata': {'aio_status': 'unresolved'}},
                     'serpapi': {'search_parameters': {'q': 'q'},
                                'ai_overview': {'page_token': '[REDACTED]'},
                                'organic_results': [{'position': 4, 'link': 'https://example.com/',
                                                     'title': 'Full title', 'snippet': 'Complete snippet'}]}}
        sources = {source: {'status': 'unresolved', 'error': 'not finished',
                            'timing': {'started_at': '2026-01-01T00:00:00Z'}} for source in responses}
        original = copy.deepcopy((responses, sources))
        records = comparison._pair_records(responses, sources, query_id='P02', run_index=2,
            snapshot_id='capture:P02_run2', paths={source: Path(source + '.json') for source in responses},
            secrets=())
        self.assertEqual((responses, sources), original)
        self.assertEqual([r['aio_status'] for r in records], ['unresolved', 'unresolved'])
        self.assertEqual([r['capture_status'] for r in records], ['unresolved', 'unresolved'])
        self.assertEqual(records[1]['organic_results'], responses['serpapi']['organic_results'])
        self.assertEqual(records[1]['provenance']['run_index'], 2)
        self.assertEqual(records[1]['provenance']['snapshot_id'], 'capture:P02_run2')
        self.assertEqual(records[1]['error'], 'not finished')

    def test_uncertain_native_upload_keeps_original_file_and_run_id(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(a, '_load_env_file'), \
                patch.dict('os.environ', {'SUPABASE_PROJECT_ID': PROJECT_ID, 'SUPABASE_ACCESS_TOKEN': TOKEN}), \
                patch.object(storage, '_open', side_effect=urllib.error.URLError('connection lost')) as request, \
                patch.object(batch, 'collect_batch', AsyncMock()) as capture, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            path = Path(tmp) / 'saved.json'
            path.write_text(json.dumps(native_capture()))
            original = path.read_bytes()
            self.assertEqual(batch.main(['--upload-file', str(path)]), 2)
            self.assertEqual(path.read_bytes(), original)
            request.assert_called_once()
            capture.assert_not_awaited()
            self.assertIn('uncertain upload', json.loads(output.getvalue())['note'])

    def test_uncertain_shared_upload_retains_sql_manifest_and_no_receipt(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(a, '_load_env_file'), \
                patch.object(comparison_storage, 'execute_sql', side_effect=ValueError('uncertain write')) as execute, \
                contextlib.redirect_stdout(io.StringIO()):
            source, output = Path(tmp) / 'records.json', Path(tmp) / 'sql'
            source.write_text(json.dumps(shared_records()))
            self.assertEqual(comparison_storage.main(['--input', str(source), '--comparison-id', COMPARISON_ID,
                                                     '--output-dir', str(output), '--upload']), 2)
            execute.assert_called_once()
            self.assertTrue((output / 'data.sql').exists())
            self.assertTrue((output / 'tables.json').exists())
            self.assertFalse(json.loads((output / 'manifest.json').read_text())['uploaded'])
            self.assertFalse((output / 'upload.json').exists())


if __name__ == '__main__':
    unittest.main()
