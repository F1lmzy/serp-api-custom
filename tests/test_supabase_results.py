import copy
import io
import json
import unittest
import urllib.error
import urllib.request
from unittest import mock

import supabase_results as sr

RUN_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def native_result(query="what is x", hl="en", gl="us"):
    return {
        "search_parameters": {"engine": "google", "q": query, "hl": hl, "gl": gl},
        "organic_results": [
            {"position": 1, "title": "First", "link": "https://first.example/", "snippet": "One"},
            {"position": 2, "title": "Second", "link": "https://second.example/", "snippet": "Two"},
        ],
        "ai_overview": {
            "text_blocks": [
                {"type": "heading", "snippet": "Head"},
                {"type": "paragraph", "snippet": "Body", "reference_indexes": [0]},
                {"type": "list", "list": [{"snippet": "Item", "reference_indexes": [1]}]},
            ],
            "references": [
                {"index": 0, "title": "Ref A", "link": "https://a.example/", "source": "A"},
                {"index": 1, "title": "Ref B", "link": "https://b.example/", "source": "B"},
            ],
        },
    }


def snapshot(results=None, **overrides):
    base = {
        "run_id": RUN_ID,
        "label": "unit test",
        "source_file": "query_test/queries.txt",
        "captured_at": "2026-09-30T14:00:00+00:00",
        "results": results if results is not None else [{"query_id": "P01", "result": native_result()}],
    }
    base.update(overrides)
    return base


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class NormalizeResponseTests(unittest.TestCase):
    def test_only_the_finalized_response_formatter_is_exposed(self):
        self.assertFalse(hasattr(sr, "normalize_snapshot"))

    def test_normalizes_one_complete_response_row(self):
        normalized = sr.normalize_response_snapshot(snapshot())
        self.assertEqual(set(normalized), {"scrape_runs", "queries"})
        self.assertEqual(normalized["scrape_runs"][0]["run_id"], RUN_ID)
        self.assertEqual(normalized["scrape_runs"][0]["source_file"], "queries.txt")
        self.assertEqual(len(normalized["queries"]), 1)
        query = normalized["queries"][0]
        self.assertEqual(query["query_id"], "P01")
        self.assertEqual(query["query"], "what is x")
        self.assertEqual(query["response_schema_version"], 1)
        self.assertEqual(query["aio_response"]["paragraph"], "Body")
        self.assertEqual(query["aio_response"]["blocks"], native_result()["ai_overview"]["text_blocks"])
        self.assertEqual([r["index"] for r in query["aio_response"]["references"]], [0, 1])
        self.assertEqual([r["ordinal"] for r in query["organic_results"]], [1, 2])

    def test_preserves_long_text_without_truncation(self):
        result = native_result()
        result["ai_overview"]["text_blocks"][1]["snippet"] = "x" * 9000
        query = sr.normalize_response_snapshot(snapshot([{"query_id": "P01", "result": result}]))["queries"][0]
        self.assertEqual(query["aio_response"]["paragraph"], "x" * 9000)
        self.assertEqual(query["aio_response"]["blocks"][1]["snippet"], "x" * 9000)

    def test_preserves_block_order_and_list_citations(self):
        blocks = sr.normalize_response_snapshot(snapshot())["queries"][0]["aio_response"]["blocks"]
        self.assertEqual([b["type"] for b in blocks], ["heading", "paragraph", "list"])
        self.assertEqual(blocks[2]["list"][0]["reference_indexes"], [1])

    def test_rejects_duplicate_query_ids_and_empty_snapshots(self):
        duplicates = [{"query_id": "P01", "result": native_result()}] * 2
        for value in (snapshot(duplicates), snapshot([])):
            with self.subTest(value=value), self.assertRaises(sr.SnapshotError):
                sr.normalize_response_snapshot(value)

    def test_sparse_indexes_and_unmapped_citations_are_preserved_not_inferred(self):
        result = native_result()
        result["ai_overview"]["references"][0]["index"] = 7
        query = sr.normalize_response_snapshot(snapshot([{"query_id": "P01", "result": result}]))["queries"][0]
        self.assertEqual(query["aio_response"]["references"][0]["index"], 7)
        self.assertIn("unmapped_citation_index:0", query["warnings"])

    def test_rejects_invalid_reference_and_citation_indexes(self):
        for field in ("reference", "citation"):
            for bad in (-1, True, 1.0, "0"):
                result = native_result()
                if field == "reference":
                    result["ai_overview"]["references"][0]["index"] = bad
                else:
                    result["ai_overview"]["text_blocks"][1]["reference_indexes"] = [bad]
                with self.subTest(field=field, value=bad), self.assertRaises(sr.SnapshotError):
                    sr.normalize_response_snapshot(snapshot([{"query_id": "P01", "result": result}]))

    def test_preserves_returned_rank_separately_from_source_order(self):
        result = native_result()
        result["organic_results"][1]["position"] = 3
        query = sr.normalize_response_snapshot(snapshot([{"query_id": "P01", "result": result}]))["queries"][0]
        self.assertEqual([(r["ordinal"], r["position"]) for r in query["organic_results"]], [(1, 1), (2, 3)])

    def test_rejects_unsafe_links_and_unknown_block_types(self):
        for bad in ("javascript:alert(1)", "data:text/html,x", "/relative/path"):
            result = native_result()
            result["organic_results"][0]["link"] = bad
            with self.subTest(link=bad), self.assertRaises(sr.SnapshotError):
                sr.normalize_response_snapshot(snapshot([{"query_id": "P01", "result": result}]))
        result = native_result()
        result["ai_overview"]["text_blocks"][0]["type"] = "chart"
        with self.assertRaises(sr.SnapshotError):
            sr.normalize_response_snapshot(snapshot([{"query_id": "P01", "result": result}]))

    def test_error_response_is_stored_with_sanitized_message(self):
        result = {"search_parameters": {"q": "what is x", "hl": "en", "gl": "us"},
                  "error": {"code": "CAPTCHA_REQUIRED", "message": "blocked https://google.com/search?q=x key sb_secret_abc123"}}
        query = sr.normalize_response_snapshot(snapshot([{"query_id": "P01", "result": result}]))["queries"][0]
        self.assertEqual(query["status"], "error")
        self.assertEqual(query["error_code"], "CAPTCHA_REQUIRED")
        self.assertNotIn("sb_secret_", query["error_message"])
        self.assertIn("?<redacted>", query["error_message"])
        self.assertIsNone(query["aio_response"])

    def test_metadata_is_allowlisted_and_fingerprint_is_stable_and_sensitive(self):
        original = snapshot(proxy="http://user:pass@proxy.example:823")
        first = sr.normalize_response_snapshot(original)["scrape_runs"][0]["metadata"]
        second = sr.normalize_response_snapshot(original)["scrape_runs"][0]["metadata"]
        self.assertEqual(set(first), {"queries", "ai_overviews", "ai_references", "organic_results", "schema_version", "fingerprint"})
        self.assertEqual(first, second)
        self.assertNotIn("proxy", json.dumps(first))
        self.assertRegex(first["fingerprint"], r"\A[0-9a-f]{64}\Z")
        changed = sr.normalize_response_snapshot(snapshot([{"query_id": "P01", "result": native_result(query="different")}]))
        self.assertNotEqual(first["fingerprint"], changed["scrape_runs"][0]["metadata"]["fingerprint"])

    def test_rejects_nonfinite_values_invalid_ids_and_naive_timestamps(self):
        result = native_result()
        result["ai_overview"]["text_blocks"][1]["score"] = float("inf")
        for value in (snapshot([{"query_id": "P01", "result": result}]),
                      snapshot(run_id="not-a-uuid"), snapshot(captured_at="yesterday"),
                      snapshot(captured_at="2026-01-01T00:00:00")):
            with self.subTest(value=value), self.assertRaises(sr.SnapshotError):
                sr.normalize_response_snapshot(value)

    def test_does_not_mutate_caller_snapshot(self):
        value = snapshot()
        original = copy.deepcopy(value)
        sr.normalize_response_snapshot(value)
        self.assertEqual(value, original)


class SanitizeErrorTests(unittest.TestCase):
    def test_redacts_url_query_strings(self):
        cleaned = sr.sanitize_error_message("failed at https://www.google.com/sorry/index?continue=abc&x=1 next")
        self.assertEqual(cleaned, "failed at https://www.google.com/sorry/index?<redacted> next")
        self.assertEqual(sr.sanitize_error_message("stayed on /sorry/index?continue=abc"),
                         "stayed on /sorry/index?<redacted>")
        self.assertEqual(sr.sanitize_error_message("is 2? yes"), "is 2? yes")

    def test_redacts_known_and_provided_secrets(self):
        message = "token sb_secret_abc123 pat sbp_deadbeef google GOOGLE_ABUSE_EXEMPTION%3Dabc supplied hunter2"
        cleaned = sr.sanitize_error_message(message, secrets=["hunter2"])
        self.assertNotIn("sb_secret_", cleaned)
        self.assertNotIn("sbp_", cleaned)
        self.assertNotIn("GOOGLE_ABUSE", cleaned)
        self.assertNotIn("hunter2", cleaned)

    def test_leaves_ordinary_text_unchanged(self):
        self.assertEqual(sr.sanitize_error_message("Google search request failed or timed out"),
                         "Google search request failed or timed out")


class UploadSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.captured = []

        def fake_open(request, timeout):
            self.captured.append((request, timeout))
            body = json.dumps([{"run_id": RUN_ID, "queries": 1, "ai_overviews": 1,
                                "ai_references": 2, "organic_results": 2}])
            return FakeResponse(body.encode("utf-8"))

        self.open_patch = mock.patch.object(sr, "_open", side_effect=fake_open)
        self.mock_open = self.open_patch.start()
        self.addCleanup(self.open_patch.stop)

    def sent_payload(self):
        request, _ = self.captured[0]
        return json.loads(request.data.decode("utf-8"))

    def test_rejects_invalid_configuration_before_network(self):
        for project, token in (("SHORT", "token"), ("valid-ref-0000000000", ""), ("", "token")):
            with self.subTest(project=project), self.assertRaises(sr.ConfigurationError):
                sr.upload_snapshot(snapshot(), project, token)
        self.mock_open.assert_not_called()

    def test_rejects_invalid_snapshot_before_network(self):
        with self.assertRaises(sr.SnapshotError):
            sr.upload_snapshot(snapshot([]), "abcdefghij0123456789", "token")
        self.mock_open.assert_not_called()

    def test_posts_parameterized_single_statement(self):
        summary = sr.upload_snapshot(snapshot(), "abcdefghij0123456789", "pat-value", timeout=45)
        request, timeout = self.captured[0]
        self.assertEqual(timeout, 45)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://api.supabase.com/v1/projects/abcdefghij0123456789/database/query")
        self.assertEqual(request.get_header("Authorization"), "Bearer pat-value")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        sent = self.sent_payload()
        self.assertEqual(sent["query"], sr.UPLOAD_SQL)
        self.assertEqual(len(sent["parameters"]), 1)
        self.assertEqual(json.loads(sent["parameters"][0])["scrape_runs"][0]["run_id"], RUN_ID)
        self.assertEqual(summary, {"run_id": RUN_ID, "queries": 1, "ai_overviews": 1,
                                   "ai_references": 2, "organic_results": 2})

    def test_native_upload_stores_expanded_content_in_one_response_row(self):
        result = native_result()
        result['ai_overview']['text_blocks'].append({'type': 'table', 'table': [['A', 'B']]})
        result['ai_overview']['references'][0].update(index=7, snippet='Reference ' + 'x' * 9000)
        result['organic_results'][0].update(position=4, author='Author', date='2 days ago')
        sr.upload_snapshot(snapshot([{'query_id': 'P01', 'result': result}]), 'abcdefghij0123456789', 'pat')
        payload = json.loads(self.sent_payload()['parameters'][0])
        self.assertEqual(set(payload), {'scrape_runs', 'queries'})
        self.assertEqual(payload['scrape_runs'][0]['run_id'], RUN_ID)
        self.assertIsNone(payload['scrape_runs'][0]['comparison_id'])
        self.assertEqual(len(payload['queries']), 1)
        q = payload['queries'][0]
        self.assertEqual(q['run_id'], RUN_ID)
        self.assertEqual(q['aio_response']['blocks'][-1]['type'], 'table')
        self.assertEqual(q['aio_response']['references'][0]['index'], 7)
        self.assertEqual(q['aio_response']['references'][0]['snippet'], 'Reference ' + 'x' * 9000)
        self.assertEqual(q['organic_results'][0]['position'], 4)
        self.assertEqual(q['organic_results'][0]['author'], 'Author')

    def test_current_native_formatter_distinguishes_absent_from_extraction_failures(self):
        for aio_status, capture_status, status in (
                ('absent', 'ok', 'ok'), ('unavailable', 'ok', 'ok'),
                ('empty', 'unresolved', 'error'), ('unresolved', 'unresolved', 'error'),
                ('invalid', 'error', 'error')):
            with self.subTest(aio_status=aio_status):
                result = native_result()
                result.pop('ai_overview')
                result['extraction_metadata'] = {'aio_status': aio_status}
                q = sr.normalize_response_snapshot(snapshot([{'query_id': 'P01', 'result': result}]))['queries'][0]
                self.assertEqual((q['aio_status'], q['capture_status'], q['status']), (aio_status, capture_status, status))
                self.assertIsNone(q['aio_response'])

    def test_current_native_formatter_preserves_order_and_unknown_locale(self):
        result = native_result()
        result['search_parameters'] = {'q': 'q'}
        snap = snapshot([{'query_id': 'P02', 'result': result}, {'query_id': 'P01', 'result': result}])
        rows = sr.normalize_response_snapshot(snap)['queries']
        self.assertEqual([q['query_id'] for q in rows], ['P02', 'P01'])
        self.assertEqual([q['ordinal'] for q in rows], [1, 2])
        self.assertTrue(all(q['hl'] is None and q['gl'] is None for q in rows))

    def test_native_upload_rejects_incomplete_snapshots_before_network(self):
        with self.assertRaises(sr.SnapshotError):
            sr.upload_snapshot(snapshot(planned_query_count=2), 'abcdefghij0123456789', 'pat')
        self.mock_open.assert_not_called()

    def test_sql_injection_like_query_stays_in_parameter_json(self):
        result = native_result(query="x'); DROP TABLE public.queries;--")
        sr.upload_snapshot(snapshot([{"query_id": "P01", "result": result}]), "abcdefghij0123456789", "pat")
        sent = self.sent_payload()
        self.assertNotIn("DROP TABLE", sent["query"])
        self.assertIn("DROP TABLE", sent["parameters"][0])

    def test_atomic_sql_dependency_shape(self):
        sql = sr.UPLOAD_SQL
        self.assertNotIn(";", sql)
        self.assertNotIn("BEGIN", sql)
        self.assertNotIn("COMMIT", sql)
        self.assertNotIn("DELETE", sql)
        self.assertNotIn("ON CONFLICT", sql)
        self.assertIn("$1::jsonb", sql)
        self.assertIn("jsonb_to_recordset", sql)
        self.assertNotIn("reference_index", sql)
        self.assertIn('aio_response', sql)
        self.assertIn('organic_results', sql)
        self.assertEqual(sql.count('INSERT INTO public.'), 2)
        for table in ('ai_overviews', 'ai_references', 'organic_results'):
            self.assertNotIn('INSERT INTO public.' + table, sql)

    def test_redirect_handler_refuses_redirects(self):
        self.assertTrue(issubclass(sr._NoRedirect, urllib.request.HTTPRedirectHandler))
        handler = sr._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/"))

    def test_http_error_does_not_leak_secrets(self):
        body = io.BytesIO(b"pat-value sb_secret_abc")
        self.mock_open.side_effect = urllib.error.HTTPError(
            "https://api.supabase.com", 401, "Unauthorized", {}, body)
        with self.assertRaises(sr.UploadError) as ctx:
            sr.upload_snapshot(snapshot(), "abcdefghij0123456789", "pat-value")
        message = str(ctx.exception)
        self.assertNotIn("pat-value", message)
        self.assertNotIn("sb_secret_", message)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(body.closed)

    def test_network_failure_is_generic(self):
        self.mock_open.side_effect = urllib.error.URLError("boom pat-value")
        with self.assertRaises(sr.UploadError) as ctx:
            sr.upload_snapshot(snapshot(), "abcdefghij0123456789", "pat-value")
        self.assertNotIn("pat-value", str(ctx.exception))

    def test_rejects_mismatched_response_counts(self):
        self.mock_open.side_effect = None
        self.mock_open.return_value = FakeResponse(json.dumps(
            [{"run_id": RUN_ID, "queries": 99, "ai_overviews": 1,
              "ai_references": 2, "organic_results": 2}]).encode())
        with self.assertRaises(sr.UploadError):
            sr.upload_snapshot(snapshot(), "abcdefghij0123456789", "pat")


if __name__ == "__main__":
    unittest.main()
