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


def counts(normalized):
    return {f"{k}": len(v) for k, v in normalized.items() if k != "scrape_runs"}


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class NormalizeSnapshotTests(unittest.TestCase):
    def test_normalizes_full_result_losslessly(self):
        normalized = sr.normalize_snapshot(snapshot())
        self.assertEqual(normalized["scrape_runs"][0]["run_id"], RUN_ID)
        self.assertEqual(normalized["scrape_runs"][0]["source_file"], "queries.txt")
        self.assertEqual(normalized["queries"], [{
            "query_id": "P01", "ordinal": 1, "query": "what is x", "hl": "en", "gl": "us",
            "status": "ok", "error_code": None, "error_message": None}])
        self.assertEqual(normalized["ai_overviews"], [{
            "query_id": "P01", "paragraph": "Body", "text_blocks": native_result()["ai_overview"]["text_blocks"]}])
        self.assertEqual(normalized["ai_references"], [
            {"query_id": "P01", "ordinal": 1, "title": "Ref A", "source": "A", "link": "https://a.example/"},
            {"query_id": "P01", "ordinal": 2, "title": "Ref B", "source": "B", "link": "https://b.example/"}])
        self.assertEqual([r["ordinal"] for r in normalized["organic_results"]], [1, 2])
        for row in normalized["ai_references"]:
            self.assertNotIn("reference_index", row)

    def test_preserves_long_text_without_truncation(self):
        long_body = "x" * 9000
        result = native_result()
        result["ai_overview"]["text_blocks"][1]["snippet"] = long_body
        normalized = sr.normalize_snapshot(snapshot([{"query_id": "P01", "result": result}]))
        self.assertEqual(normalized["ai_overviews"][0]["paragraph"], long_body)
        self.assertEqual(normalized["ai_overviews"][0]["text_blocks"][1]["snippet"], long_body)

    def test_preserves_block_order_and_list_citations(self):
        normalized = sr.normalize_snapshot(snapshot())
        blocks = normalized["ai_overviews"][0]["text_blocks"]
        self.assertEqual([b["type"] for b in blocks], ["heading", "paragraph", "list"])
        self.assertEqual(blocks[2]["list"][0]["reference_indexes"], [1])

    def test_rejects_duplicate_query_ids(self):
        results = [{"query_id": "P01", "result": native_result()},
                   {"query_id": "P01", "result": native_result()}]
        with self.assertRaises(sr.SnapshotError):
            sr.normalize_snapshot(snapshot(results))

    def test_rejects_empty_results(self):
        with self.assertRaises(sr.SnapshotError):
            sr.normalize_snapshot(snapshot([]))

    def test_rejects_nonconsecutive_reference_index(self):
        for bad in (True, 1.0, 2):
            result = native_result()
            result["ai_overview"]["references"][0]["index"] = bad
            with self.subTest(index=bad), self.assertRaises(sr.SnapshotError):
                sr.normalize_snapshot(snapshot([{"query_id": "P01", "result": result}]))

    def test_rejects_out_of_range_citation_index(self):
        for bad in (-1, True, 2, "0"):
            result = native_result()
            result["ai_overview"]["text_blocks"][1]["reference_indexes"] = [bad]
            with self.subTest(index=bad), self.assertRaises(sr.SnapshotError):
                sr.normalize_snapshot(snapshot([{"query_id": "P01", "result": result}]))

    def test_rejects_organic_position_mismatch(self):
        result = native_result()
        result["organic_results"][1]["position"] = 3
        with self.assertRaises(sr.SnapshotError):
            sr.normalize_snapshot(snapshot([{"query_id": "P01", "result": result}]))

    def test_rejects_unsafe_links(self):
        for bad in ("javascript:alert(1)", "data:text/html,x", "/relative/path"):
            result = native_result()
            result["organic_results"][0]["link"] = bad
            with self.subTest(link=bad), self.assertRaises(sr.SnapshotError):
                sr.normalize_snapshot(snapshot([{"query_id": "P01", "result": result}]))

    def test_rejects_unknown_block_type(self):
        result = native_result()
        result["ai_overview"]["text_blocks"][0]["type"] = "chart"
        with self.assertRaises(sr.SnapshotError):
            sr.normalize_snapshot(snapshot([{"query_id": "P01", "result": result}]))

    def test_error_result_is_stored_with_sanitized_message(self):
        result = {
            "search_parameters": {"engine": "google", "q": "what is x", "hl": "en", "gl": "us"},
            "error": {"code": "CAPTCHA_REQUIRED",
                      "message": "blocked at https://www.google.com/search?q=x&google_abuse=GOOGLE_ABUSE_EXEMPTION%3Dabc key sb_secret_abc123"},
        }
        normalized = sr.normalize_snapshot(snapshot([{"query_id": "P01", "result": result}]))
        row = normalized["queries"][0]
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error_code"], "CAPTCHA_REQUIRED")
        self.assertIn("?<redacted>", row["error_message"])
        self.assertIn("<redacted>", row["error_message"])
        self.assertNotIn("GOOGLE_ABUSE", row["error_message"])
        self.assertNotIn("sb_secret_", row["error_message"])
        self.assertEqual(row["query"], "what is x")
        self.assertEqual(normalized["ai_overviews"], [])

    def test_metadata_only_contains_allowlisted_keys(self):
        snap = snapshot(proxy="http://user:pass@proxy.example:823")
        snap["results"][0]["extra"] = "ignore me"
        metadata = sr.normalize_snapshot(snap)["scrape_runs"][0]["metadata"]
        self.assertEqual(set(metadata), {"queries", "ai_overviews", "ai_references", "organic_results", "fingerprint"})
        self.assertNotIn("proxy", json.dumps(metadata))

    def test_fingerprint_is_stable_and_content_sensitive(self):
        first = sr.normalize_snapshot(snapshot())["scrape_runs"][0]["metadata"]["fingerprint"]
        second = sr.normalize_snapshot(snapshot())["scrape_runs"][0]["metadata"]["fingerprint"]
        self.assertEqual(first, second)
        self.assertRegex(first, r"\A[0-9a-f]{64}\Z")
        changed = sr.normalize_snapshot(snapshot([{"query_id": "P01", "result": native_result(query="different")}]))
        self.assertNotEqual(first, changed["scrape_runs"][0]["metadata"]["fingerprint"])

    def test_rejects_nonfinite_json_values(self):
        result = native_result()
        result["ai_overview"]["text_blocks"][1]["score"] = float("inf")
        with self.assertRaises(sr.SnapshotError):
            sr.normalize_snapshot(snapshot([{"query_id": "P01", "result": result}]))

    def test_rejects_invalid_run_id_and_timestamp(self):
        with self.assertRaises(sr.SnapshotError):
            sr.normalize_snapshot(snapshot(run_id="not-a-uuid"))
        with self.assertRaises(sr.SnapshotError):
            sr.normalize_snapshot(snapshot(captured_at="yesterday"))

    def test_does_not_mutate_caller_snapshot(self):
        snap = snapshot()
        original = copy.deepcopy(snap)
        sr.normalize_snapshot(snap)
        self.assertEqual(snap, original)


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
        order = [sql.index(name) for name in
                 ("inserted_run", "inserted_queries", "inserted_overviews", "inserted_references")]
        self.assertEqual(order, sorted(order))

    def test_redirect_handler_refuses_redirects(self):
        self.assertTrue(issubclass(sr._NoRedirect, urllib.request.HTTPRedirectHandler))
        handler = sr._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/"))

    def test_http_error_does_not_leak_secrets(self):
        self.mock_open.side_effect = urllib.error.HTTPError(
            "https://api.supabase.com", 401, "Unauthorized", {}, io.BytesIO(b"pat-value sb_secret_abc"))
        with self.assertRaises(sr.UploadError) as ctx:
            sr.upload_snapshot(snapshot(), "abcdefghij0123456789", "pat-value")
        message = str(ctx.exception)
        self.assertNotIn("pat-value", message)
        self.assertNotIn("sb_secret_", message)
        self.assertIsNone(ctx.exception.__cause__)

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
