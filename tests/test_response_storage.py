"""Compatibility checks across native and shared-record response storage."""

import copy
import io
import json
import sys
import unittest
import urllib.error
from unittest.mock import patch

import comparison_storage as comparison
import supabase_results as storage
from search_export import normalize_response


RUN_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
COMPARISON_ID = "2fa1838a-b6be-473a-8c38-cbf640461620"
PROJECT_ID = "abcdefghij0123456789"
TOKEN = "fake-management-token"


def native_capture():
    return {
        "run_id": RUN_ID,
        "label": "Storage compatibility",
        "source_file": "saved/native.json",
        "captured_at": "2026-10-19T11:08:27+00:00",
        "planned_query_count": 1,
        "results": [{"query_id": "P01", "result": {
            "search_parameters": {"q": "same query", "hl": "en", "gl": "us"},
            "ai_overview": {
                "text_blocks": [{"type": "paragraph", "snippet": "Complete answer",
                                 "reference_indexes": [7]}],
                "references": [{"index": 7, "link": "https://example.com/source"}],
            },
            "organic_results": [{"position": 4, "title": "Result",
                                 "link": "https://example.com/result", "snippet": "Full snippet"}],
        }}],
    }


def shared_records():
    capture = native_capture()
    record = normalize_response(
        capture["results"][0]["result"], provider="custom", query_id="P01",
        snapshot_id=f"{RUN_ID}:P01", source_file=capture["source_file"],
        captured_at=capture["captured_at"],
    )
    record["provenance"]["run_index"] = 1
    record["capture_status"] = "ok"
    return [record]


class Response(io.BytesIO):
    pass


class ResponseStorageCompatibilityTests(unittest.TestCase):
    def upload(self, kind):
        if kind == "native":
            return storage.upload_snapshot(native_capture(), PROJECT_ID, TOKEN)
        return comparison.upload_records(shared_records(), COMPARISON_ID, PROJECT_ID, TOKEN)

    def receipt(self, kind):
        if kind == "native":
            return {"run_id": RUN_ID, "queries": 1, "ai_overviews": 1,
                    "ai_references": 1, "organic_results": 1}
        return {"comparison_id": COMPARISON_ID, "scrape_runs": 1, "queries": 1}

    def error_type(self, kind):
        return storage.UploadError if kind == "native" else ValueError

    def test_native_import_path_preserves_the_same_response_and_errors(self):
        capture = native_capture()
        original = copy.deepcopy(capture)
        self.assertEqual(comparison.normalize_native_snapshot(capture),
                         storage.normalize_response_snapshot(capture))
        self.assertEqual(capture, original)
        for normalize in (comparison.normalize_native_snapshot, storage.normalize_response_snapshot):
            with self.subTest(normalize=normalize), self.assertRaises(storage.SnapshotError):
                normalize({**capture, "planned_query_count": 2})

    def test_storage_works_without_importing_the_comparison_command(self):
        records = shared_records()
        expected = self.receipt("shared")
        with patch.dict(sys.modules, {"comparison_storage": None}), patch.object(
                storage, "_open", return_value=Response(json.dumps([expected]).encode())) as open_request:
            self.assertEqual(storage.normalize_response_snapshot(native_capture())["queries"][0]["query"], "same query")
            self.assertEqual(storage.upload_records(records, COMPARISON_ID, PROJECT_ID, TOKEN), expected)
            open_request.assert_called_once()

    def test_native_and_shared_paths_preserve_the_same_response_content(self):
        native = storage.normalize_response_snapshot(native_capture())["queries"][0]
        shared = comparison.normalize_records(shared_records(), COMPARISON_ID)["queries"][0]
        for field in ("query", "aio_response", "organic_results", "aio_status",
                      "capture_status", "status", "warnings", "response_schema_version"):
            with self.subTest(field=field):
                self.assertEqual(native[field], shared[field])
        self.assertEqual(native["aio_response"]["references"][0]["index"], 7)
        self.assertEqual(shared["organic_results"][0]["position"], 4)

    def test_each_upload_sends_one_fixed_host_parameterized_write(self):
        for kind in ("native", "shared"):
            expected = self.receipt(kind)
            with self.subTest(kind=kind), patch.object(
                    storage, "_open", return_value=Response(json.dumps([expected]).encode())) as open_request:
                self.assertEqual(self.upload(kind), expected)
                open_request.assert_called_once()
                request, timeout = open_request.call_args.args
                self.assertEqual(timeout, 90)
                self.assertEqual(request.full_url,
                                 f"https://api.supabase.com/v1/projects/{PROJECT_ID}/database/query")
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
                payload = json.loads(request.data)
                self.assertFalse(payload["read_only"])
                self.assertEqual(len(payload["parameters"]), 1)
                self.assertEqual(set(json.loads(payload["parameters"][0])), {"scrape_runs", "queries"})
                self.assertEqual(payload["query"], storage.UPLOAD_SQL if kind == "native" else comparison.UPLOAD_SQL)

    def test_http_errors_keep_the_original_exception_types_and_close_the_body(self):
        for kind in ("native", "shared"):
            body = Response(f"{TOKEN} sb_secret_fake".encode())
            error = urllib.error.HTTPError("https://api.supabase.com", 409, TOKEN, {}, body)
            with self.subTest(kind=kind), patch.object(storage, "_open", side_effect=error) as open_request:
                with self.assertRaises(self.error_type(kind)) as caught:
                    self.upload(kind)
                self.assertIs(type(caught.exception), self.error_type(kind))
                self.assertIn("409", str(caught.exception))
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(body.closed)
                open_request.assert_called_once()

    def test_both_uploads_use_the_redirect_refusing_http_adapter(self):
        for kind in ("native", "shared"):
            expected = self.receipt(kind)
            with self.subTest(kind=kind), patch.object(storage.urllib.request, "build_opener") as build:
                opener = build.return_value
                opener.open.return_value = Response(json.dumps([expected]).encode())
                self.assertEqual(self.upload(kind), expected)
                build.assert_called_once_with(storage._NoRedirect)
                handler = build.call_args.args[0]()
                self.assertIsNone(handler.redirect_request(
                    None, None, 307, "Redirect", {}, "https://untrusted.example/",
                ))
                opener.open.assert_called_once()

    def test_receipt_count_types_remain_distinct_between_input_paths(self):
        for kind in ("native", "shared"):
            receipt = {key: str(value) if isinstance(value, int) else value
                       for key, value in self.receipt(kind).items()}
            with self.subTest(kind=kind), patch.object(
                    storage, "_open", return_value=Response(json.dumps([receipt]).encode())):
                if kind == "native":
                    self.assertEqual(self.upload(kind), self.receipt(kind))
                else:
                    with self.assertRaises(ValueError):
                        self.upload(kind)
        for kind in ("native", "shared"):
            receipt = {**self.receipt(kind), "queries": True}
            with self.subTest(kind=kind), patch.object(
                    storage, "_open", return_value=Response(json.dumps([receipt]).encode())):
                with self.assertRaises(self.error_type(kind)):
                    self.upload(kind)

    def test_uncertain_network_failure_is_never_retried(self):
        for kind in ("native", "shared"):
            with self.subTest(kind=kind), patch.object(
                    storage, "_open", side_effect=urllib.error.URLError(TOKEN)) as open_request:
                with self.assertRaises(self.error_type(kind)) as caught:
                    self.upload(kind)
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                open_request.assert_called_once()

    def test_invalid_json_and_receipt_shapes_do_not_trigger_a_second_write(self):
        for kind in ("native", "shared"):
            for body in (b"not JSON", b"{}", b"[]", b"[null]"):
                with self.subTest(kind=kind, body=body), patch.object(
                        storage, "_open", return_value=Response(body)) as open_request:
                    with self.assertRaises(self.error_type(kind)):
                        self.upload(kind)
                    open_request.assert_called_once()

    def test_mismatched_receipts_keep_the_original_exception_types(self):
        for kind in ("native", "shared"):
            receipt = {**self.receipt(kind), "queries": 99}
            with self.subTest(kind=kind), patch.object(
                    storage, "_open", return_value=Response(json.dumps([receipt]).encode())) as open_request:
                with self.assertRaises(self.error_type(kind)):
                    self.upload(kind)
                open_request.assert_called_once()

    def test_configuration_and_snapshot_errors_still_precede_network_writes(self):
        with patch.object(storage, "_open") as open_request:
            with self.assertRaises(storage.ConfigurationError):
                storage.upload_snapshot(native_capture(), "bad-project", TOKEN)
            with self.assertRaises(storage.SnapshotError):
                storage.upload_snapshot({"results": []}, PROJECT_ID, TOKEN)
            with self.assertRaisesRegex(ValueError, "SUPABASE_PROJECT_ID") as caught:
                comparison.upload_records(shared_records(), COMPARISON_ID, "bad-project", TOKEN)
            self.assertIs(type(caught.exception), ValueError)
            with self.assertRaises(ValueError):
                comparison.upload_records([], COMPARISON_ID, PROJECT_ID, TOKEN)
            open_request.assert_not_called()

    def test_comparison_sql_execution_preserves_timeout_and_read_only_options(self):
        with patch.object(storage, "_open", return_value=Response(b"[]")) as open_request:
            self.assertEqual(comparison.execute_sql(
                "SELECT $1", [3], project_id=PROJECT_ID, access_token=TOKEN,
                read_only=True, timeout=12,
            ), [])
            request, timeout = open_request.call_args.args
            self.assertEqual(timeout, 12)
            self.assertEqual(json.loads(request.data),
                             {"query": "SELECT $1", "parameters": [3], "read_only": True})
        for invalid_timeout in (True, 0, -1, float("inf"), "12"):
            with self.subTest(timeout=invalid_timeout), patch.object(storage, "_open") as open_request:
                with self.assertRaisesRegex(ValueError, "timeout"):
                    comparison.execute_sql("SELECT 1", project_id=PROJECT_ID,
                                           access_token=TOKEN, timeout=invalid_timeout)
                open_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
