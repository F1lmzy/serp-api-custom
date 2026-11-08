import contextlib
import io
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import compare_serpapi as c


class JsonComparisonTests(unittest.TestCase):
    def test_missing_section_is_reported_once_with_descendant_inventory(self):
        report = c.compare_json({}, {'ai_overview': {'text_blocks': [{'type': 'paragraph', 'snippet': 'Answer'}]}})
        missing = report['search_details']['missing_fields']
        self.assertEqual([item['path'] for item in missing], ['$.ai_overview'])
        self.assertIn('$.ai_overview.text_blocks[].snippet', missing[0]['available_descendants'])

    def test_all_array_records_contribute_missing_fields_and_coverage(self):
        scraper = {'organic_results': [{'title': 'a'}, {'title': 'b', 'snippet': 'text'}]}
        api = {'organic_results': [{'title': 'c', 'snippet': 'other'},
                                   {'title': 'd', 'snippet': 'another', 'displayed_link': 'example.com'}]}
        report = c.compare_json(scraper, api)['search_details']
        self.assertEqual(report['missing_fields'][0]['path'], '$.organic_results[].displayed_link')
        self.assertEqual(report['missing_fields'][0]['serpapi_coverage'], {'present': 1, 'objects': 2})
        self.assertEqual(report['field_coverage_gaps'][0]['path'], '$.organic_results[].snippet')

    def test_wording_changes_and_array_reordering_are_ignored(self):
        left = {'organic_results': [{'title': 'first', 'position': 1}, {'title': 'second', 'position': 2}],
                'ai_overview': {'text_blocks': [{'type': 'paragraph', 'snippet': 'old'}]}}
        right = {'organic_results': [{'position': 99, 'title': 'different'}, {'position': 12, 'title': 'another'}],
                 'ai_overview': {'text_blocks': [{'type': 'paragraph', 'snippet': 'new'}]}}
        report = c.compare_json(left, right)
        self.assertFalse(any(report['search_details'].values()))

    def test_null_empty_and_boolean_types_are_distinct_from_number(self):
        report = c.compare_json({'answer': None, 'label': '', 'flag': False},
                                {'answer': {'value': 'yes'}, 'label': 'present', 'flag': 1})['search_details']
        self.assertEqual({i['path'] for i in report['type_differences']}, {'$.answer', '$.flag'})
        self.assertEqual({i['path'] for i in report['empty_field_gaps']}, {'$.answer', '$.label'})

    def test_empty_array_reports_count_not_unobserved_item_keys(self):
        report = c.compare_json({'organic_results': []}, {'organic_results': [{'title': 'a', 'link': 'b'}]})
        self.assertEqual(report['search_details']['missing_fields'], [])
        array = report['search_details']['array_count_differences'][0]
        self.assertEqual(array['scraper_lengths'], [0])
        self.assertEqual(array['serpapi_lengths'], [1])
        self.assertIn('$.organic_results[]', report['no_observed_scraper_items'])

    def test_scoped_comparison_keeps_nested_content_without_legacy_metadata_bucket(self):
        api = {'search_metadata': {'id': '123'}, 'search_parameters': {'q': 'q', 'device': 'desktop'},
               'new_module': {'details': [{'extra': 'yes'}]},
               'ai_overview': {'text_blocks': [{'type': 'list', 'list': [{'snippet': 'x', 'list': [{'snippet': 'y'}]}]}]}}
        left = {'search_parameters': {'q': 'q'}, 'ai_overview': {'text_blocks': [{'type': 'paragraph', 'snippet': 'z'}]}}
        report = c.compare_main_body(left, api)
        self.assertNotIn('api_metadata', report)
        self.assertEqual(report['aio_block_types']['serpapi'], {'list': 1})
        self.assertIn('$.ai_overview.text_blocks[].list[].list[].snippet', report['inventories']['serpapi'])
        self.assertNotIn('$.new_module', report['inventories']['serpapi'])
        self.assertNotIn('$.search_metadata', report['inventories']['serpapi'])

    def test_invalid_root_is_rejected(self):
        with self.assertRaises(ValueError):
            c.compare_json([], {})


class FreshCaptureTests(unittest.TestCase):
    def _run(self, args, response=None, scrape_error=False):
        events = []

        class FakeScraper:
            def __init__(self, **kwargs):
                self.options = kwargs
                events.append(('browser', kwargs))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def search(self, query, hl, gl):
                events.append(('scraper', query))
                if scrape_error and query == 'bad':
                    raise c.a.ScrapeError('failed fake-key-123')
                return {'search_parameters': {'q': query, 'hl': hl, 'gl': gl},
                        'organic_results': [{'title': 'scraper title'}]}

        def fetch(query, **kwargs):
            events.append(('serpapi', query))
            return response or {'status': 'ok', 'error': None, 'evidence': {},
                                'response': {'search_parameters': {'q': query, 'hl': kwargs['hl'], 'gl': kwargs['gl']},
                                             'organic_results': [{'title': 'api title', 'displayed_link': 'example.com'}]}}

        with patch.dict(os.environ, {'SERPAPI_API_KEY': 'fake-key-123'}), \
             patch.object(c.a, '_load_env_file'), patch.object(c.a, 'GoogleAIOScraper', FakeScraper), \
             patch.object(c, 'fetch_search', side_effect=fetch), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            status = c.main(args)
        return status, events

    def test_single_query_saves_native_json_and_timed_gap_report(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = str(Path(folder) / 'run')
            status, events = self._run(['query', '--out-prefix', prefix, '--hl', 'fr', '--gl', 'ca', '--delay', '0'])
            self.assertEqual(status, 0)
            left = json.loads(Path(prefix + '.scraper.json').read_text())
            right = json.loads(Path(prefix + '.serpapi.json').read_text())
            report = json.loads(Path(prefix + '.comparison.json').read_text())
            self.assertEqual(left['search_parameters']['hl'], 'fr')
            self.assertEqual(right['search_parameters']['gl'], 'ca')
            self.assertEqual([event[0] for event in events], ['browser', 'scraper', 'serpapi'])
            self.assertEqual(report['comparison']['search_details']['missing_fields'][0]['path'], '$.organic_results[].displayed_link')
            self.assertIn('started_at', report['sources']['scraper']['timing'])
            self.assertIn('elapsed_seconds', report['sources']['serpapi']['timing'])
            self.assertNotIn('fake-key-123', json.dumps(report))

    def test_batch_pairs_queries_and_keeps_other_source_after_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            queries = root / 'queries.txt'
            queries.write_text('# comment\ngood\n\nbad\nlast\n')
            prefix = str(root / 'run')
            status, events = self._run(['--queries', str(queries), '--out-prefix', prefix, '--delay', '0'], scrape_error=True)
            self.assertEqual(status, 1)
            self.assertEqual(events[1:], [('scraper', 'good'), ('serpapi', 'good'), ('scraper', 'bad'),
                                         ('serpapi', 'bad'), ('scraper', 'last'), ('serpapi', 'last')])
            report = json.loads(Path(prefix + '.comparison.json').read_text())
            self.assertEqual(report['comparable_queries'], 2)
            self.assertIsNone(report['queries']['P02']['comparison'])
            self.assertEqual(report['queries']['P02']['sources']['serpapi']['status'], 'ok')
            left = json.loads(Path(prefix + '.scraper.json').read_text())
            self.assertEqual(list(left), ['P01', 'P02', 'P03'])
            self.assertNotIn('fake-key-123', Path(prefix + '.scraper.json').read_text())

    def test_missing_key_invalid_options_and_collisions_fail_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = str(Path(folder) / 'run')
            with patch.object(c.a, '_load_env_file'), patch.dict(os.environ, {'SERPAPI_API_KEY': ''}), \
                 patch.object(c.a, 'GoogleAIOScraper') as browser, patch.object(c, 'fetch_search') as api, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(c.main(['query', '--out-prefix', prefix]), 2)
                browser.assert_not_called()
                api.assert_not_called()
                self.assertFalse(Path(prefix + '.comparison.json').exists())
            for options in (['--delay', 'nan'], ['--timeout', '0'], ['--api-timeout', 'inf'], ['--hl', ' ']):
                status, events = self._run(['q', '--out-prefix', prefix] + options)
                self.assertEqual(status, 2)
                self.assertEqual(events, [])
            Path(prefix + '.serpapi.json').write_text('existing')
            status, events = self._run(['q', '--out-prefix', prefix])
            self.assertEqual(status, 2)
            self.assertEqual(events, [])

    def test_batch_files_are_saved_before_next_query_and_retained_on_browser_exit_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'queries.txt').write_text('first\nsecond\n')
            prefix = str(root / 'run')

            class FakeScraper:
                def __init__(self, **kwargs):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    raise c.a.ScrapeError('browser close failed')

                async def search(self, query, hl, gl):
                    if query == 'second':
                        saved = json.loads(Path(prefix + '.comparison.json').read_text())
                        self_test.assertEqual(saved['completed_queries'], 1)
                        self_test.assertIn('P01', json.loads(Path(prefix + '.serpapi.json').read_text()))
                    return {'search_parameters': {'q': query, 'hl': hl, 'gl': gl}, 'organic_results': []}

            self_test = self
            def fetch(query, **kwargs):
                return {'status': 'ok', 'response': {'search_parameters': {'q': query}, 'organic_results': []},
                        'error': None, 'evidence': {}}

            with patch.dict(os.environ, {'SERPAPI_API_KEY': 'fake'}), patch.object(c.a, '_load_env_file'), \
                 patch.object(c.a, 'GoogleAIOScraper', FakeScraper), patch.object(c, 'fetch_search', side_effect=fetch), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = c.main(['--queries', str(root / 'queries.txt'), '--out-prefix', prefix, '--delay', '0'])
            self.assertEqual(status, 2)
            report = json.loads(Path(prefix + '.comparison.json').read_text())
            self.assertEqual(report['status'], 'aborted')
            self.assertEqual(report['completed_queries'], 2)
            self.assertEqual(report['comparable_queries'], 2)
            self.assertEqual(list(report['queries']), ['P01', 'P02'])

    def test_failed_json_export_can_recover_without_repeating_paid_captures(self):
        real_export = c.export_records
        attempts = []

        def fail_once(*args):
            attempts.append(args)
            if len(attempts) == 1:
                raise OSError('disk full')
            return real_export(*args)

        with tempfile.TemporaryDirectory() as folder:
            prefix = str(Path(folder) / 'run')
            with patch.object(c, 'export_records', side_effect=fail_once):
                status, events = self._run(['q', '--out-prefix', prefix])
            self.assertEqual(status, 2)
            self.assertEqual(len(attempts), 2)
            self.assertEqual([e[0] for e in events], ['browser', 'scraper', 'serpapi'])
            self.assertEqual(len(json.loads(Path(prefix + '.main-body/records.json').read_text())), 2)

    def test_obsolete_native_scope_option_is_rejected_before_capture(self):
        with patch.object(c, 'fetch_search') as api, patch.object(c.a, 'GoogleAIOScraper') as browser, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                c.main(['q', '--scope', 'all', '--out-prefix', 'unused'])
            self.assertEqual(exc.exception.code, 2)
            api.assert_not_called()
            browser.assert_not_called()

    def test_empty_query_file_and_blank_query_fail_without_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'queries.txt').write_text('# comment\n\n')
            for source in (['   '], ['--queries', str(root / 'queries.txt')]):
                status, events = self._run(source + ['--out-prefix', str(root / 'run')])
                self.assertEqual(status, 2)
                self.assertEqual(events, [])

    def test_unresolved_api_is_not_reported_as_a_missing_overview(self):
        unresolved = {'status': 'unresolved', 'response': {'ai_overview': {'page_token': 'TOKENlong'}},
                      'evidence': {'initial_overview': {'page_token': 'TOKENlong'}}, 'error': 'follow up failed'}
        with tempfile.TemporaryDirectory() as folder:
            prefix = str(Path(folder) / 'run')
            status, _ = self._run(['q', '--out-prefix', prefix], response=unresolved)
            self.assertEqual(status, 1)
            text = Path(prefix + '.comparison.json').read_text()
            self.assertIsNone(json.loads(text)['comparison'])
            self.assertNotIn('TOKENlong', text)

    def test_malformed_api_settings_fail_query_not_whole_batch(self):
        invalid = {'status': 'ok', 'response': {'search_parameters': []}, 'evidence': {}, 'error': None}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'queries.txt').write_text('first\nsecond\n')
            prefix = str(root / 'run')
            status, events = self._run(['--queries', str(root / 'queries.txt'), '--out-prefix', prefix,
                                       '--delay', '0'], response=invalid)
            self.assertEqual(status, 1)
            report = json.loads(Path(prefix + '.comparison.json').read_text())
            self.assertEqual(report['completed_queries'], 2)
            self.assertEqual(len(events), 5)

    def test_native_api_error_response_is_saved_instead_of_synthetic_error(self):
        payload = {'error': 'API failed', 'search_metadata': {'id': 'error-id'}}
        failure = {'status': 'error', 'response': None, 'evidence': {'initial_response': payload}, 'error': 'API failed'}
        with tempfile.TemporaryDirectory() as folder:
            prefix = str(Path(folder) / 'run')
            status, _ = self._run(['q', '--out-prefix', prefix], response=failure)
            self.assertEqual(status, 1)
            self.assertEqual(json.loads(Path(prefix + '.serpapi.json').read_text()), payload)

    def test_api_failure_saves_scraper_response_and_evidence(self):
        failure = {'status': 'error', 'response': None, 'evidence': {}, 'error': 'API failed fake-key-123'}
        with tempfile.TemporaryDirectory() as folder:
            prefix = str(Path(folder) / 'run')
            status, _ = self._run(['q', '--out-prefix', prefix], response=failure)
            self.assertEqual(status, 1)
            report = json.loads(Path(prefix + '.comparison.json').read_text())
            self.assertIsNone(report['comparison'])
            self.assertIn('organic_results', json.loads(Path(prefix + '.scraper.json').read_text()))
            self.assertNotIn('fake-key-123', Path(prefix + '.serpapi.json').read_text())


class OfflineCliTests(unittest.TestCase):
    def test_offline_output_requires_no_key_browser_or_http(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            left, right, prefix = root / 'left.json', root / 'right.json', root / 'out'
            left.write_text(json.dumps({'search_parameters': {'q': 'q'}, 'organic_results': [{'title': 'a'}]}))
            right.write_text(json.dumps({'search_parameters': {'q': 'q'}, 'organic_results': [{'title': 'b', 'link': 'url'}]}))
            with patch('serpapi_client._request_json', side_effect=AssertionError('network forbidden')), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = c.main(['--scraper-json', str(left), '--serpapi-json', str(right), '--out-prefix', str(prefix)])
            self.assertEqual(status, 0)
            report = json.loads((root / 'out.comparison.json').read_text())
            self.assertEqual(report['comparison']['search_details']['missing_fields'][0]['path'], '$.organic_results[].link')
            self.assertFalse((root / 'out.scraper.json').exists())

    def test_mismatched_queries_and_output_collisions_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            left, right, prefix = root / 'left.json', root / 'right.json', root / 'out'
            left.write_text(json.dumps({'search_parameters': {'q': 'one'}}))
            right.write_text(json.dumps({'search_parameters': {'q': 'two'}}))
            args = ['--scraper-json', str(left), '--serpapi-json', str(right), '--out-prefix', str(prefix)]
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(c.main(args), 2)
            self.assertFalse((root / 'out.comparison.json').exists())
            right.write_text(left.read_text())
            output = root / 'out.comparison.json'
            output.write_text('existing')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(c.main(args), 2)
            self.assertEqual(output.read_text(), 'existing')

    def test_offline_rejects_batch_dictionaries_and_wrapped_reference_results(self):
        native = {'search_parameters': {'q': 'q'}, 'organic_results': []}
        for wrapped in ({'P01': native}, {'prompt': 'q', 'google': native, 'ai_overview': None}):
            with self.subTest(wrapped=wrapped), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / 'left.json').write_text(json.dumps(native))
                (root / 'right.json').write_text(json.dumps(wrapped))
                with contextlib.redirect_stderr(io.StringIO()):
                    status = c.main(['--scraper-json', str(root / 'left.json'),
                                     '--serpapi-json', str(root / 'right.json'), '--out-prefix', str(root / 'out')])
                self.assertEqual(status, 2)
                self.assertFalse((root / 'out.comparison.json').exists())

    def test_offline_errors_and_unresolved_overviews_skip_gap_analysis(self):
        for api in ({'error': 'API failed'}, {'ai_overview': {'page_token': 'short-lived-token'}}):
            with self.subTest(api=api), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / 'left.json').write_text(json.dumps({'organic_results': []}))
                (root / 'right.json').write_text(json.dumps(api))
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    status = c.main(['--scraper-json', str(root / 'left.json'), '--serpapi-json', str(root / 'right.json'),
                                     '--out-prefix', str(root / 'out')])
                self.assertEqual(status, 1)
                text = (root / 'out.comparison.json').read_text()
                self.assertIsNone(json.loads(text)['comparison'])
                self.assertNotIn('short-lived-token', text)
