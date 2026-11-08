import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from search_export import normalize_response, export_records, load_input, main


class SearchExportTests(unittest.TestCase):
    def sample(self):
        return {
            "search_parameters": {"q": "query", "hl": "en", "gl": "us", "device": "desktop"},
            "search_metadata": {"id": "search-1", "created_at": "2026-01-01T00:00:00Z", "api_key": "secret"},
            "ai_overview": {"ai_overview": {"text_blocks": [
                {"type": "heading", "snippet": "=Heading", "reference_indexes": [0],
                 "snippet_links": [{"text": "source", "link": "https://s.test"}],
                 "future_nested": {"items": [{"text": "future text", "details": {"more": "kept"}, "thumbnail": "omit"}],
                                   "ads": ["omit"]}},
                {"type": "list", "list": [{"snippet": "full " + "x" * 6000, "reference_indexes": [0]}]},
                {"type": "table", "table": [["A", "B"], ["C", "D"]]},
                {"type": "future_block", "arbitrary": {"nested": ["preserve"]}},
            ], "references": [{"index": 0, "link": "https://s.test", "title": "Source", "source": "s.test", "snippet": "citation text"}]}},
            "organic_results": [{"position": 3, "title": "Result", "link": "https://r.test", "displayed_link": "r.test", "snippet": "snippet", "source": "r.test", "date": "today", "author": "A", "sitelinks": {"inline": [{"title": "Sub", "link": "https://r.test/sub"}]}, "rich_snippet": {"top": {"extensions": ["pages"]}}, "snippet_highlighted_words": ["snippet"], "thumbnail": "https://img.test"}],
            "ads": [{"title": "omit"}], "related_questions": [{"question": "omit"}],
        }

    def test_normalizes_full_nested_content_provenance_and_excludes_noise(self):
        r = normalize_response(self.sample(), provider="serpapi", snapshot_id="snap-1", source_file="input.json")
        self.assertEqual(r["schema_version"], 1)
        self.assertEqual(r["provenance"]["search_id"], "search-1")
        self.assertEqual(r["provenance"]["query"], "query")
        self.assertIsNone(r["provenance"]["topic"])
        self.assertEqual(r["organic_count"], 1)
        self.assertEqual([b["type"] for b in r["ai_overview"]["blocks"]], ["heading", "list", "table", "future_block"])
        self.assertEqual(len(r["ai_overview"]["blocks"][1]["list"][0]["snippet"]), 6005)
        self.assertEqual(r["ai_overview"]["blocks"][0]["reference_indexes"], [0])
        self.assertEqual(r["ai_overview"]["blocks"][0]["future_nested"]["items"][0]["details"]["more"], "kept")
        self.assertNotIn("thumbnail", r["ai_overview"]["blocks"][0]["future_nested"]["items"][0])
        self.assertIn("unknown_aio_block_type:future_block", r["warnings"])
        self.assertEqual(r["ai_overview"]["references"][0]["snippet"], "citation text")
        self.assertEqual(r["organic_results"][0]["position"], 3)
        self.assertNotIn("thumbnail", r["organic_results"][0])
        self.assertNotIn("ads", json.dumps(r))
        self.assertNotIn("secret", json.dumps(r))

    def test_rejects_invalid_identifiers_and_response_shapes(self):
        for kwargs in ({"provider": "other", "snapshot_id": "s"},
                       {"provider": "custom", "query_id": "", "snapshot_id": "s"},
                       {"provider": "custom", "query_id": "P1", "snapshot_id": ""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                normalize_response(self.sample(), **kwargs)
        for change in ("params", "metadata", "blocks", "references", "organic"):
            value = self.sample()
            if change == "params": value["search_parameters"] = []
            elif change == "metadata": value["search_metadata"] = []
            elif change == "blocks": value["ai_overview"]["ai_overview"]["text_blocks"] = {}
            elif change == "references": value["ai_overview"]["ai_overview"]["references"] = "bad"
            else: value["organic_results"] = {}
            result = normalize_response(value, provider="custom", snapshot_id="s")
            self.assertEqual(result["aio_status"], "invalid")
            self.assertTrue(any(w.startswith("invalid_") for w in result["warnings"]))

    def test_statuses_use_custom_metadata_and_collection_before_redacted_token(self):
        base = {"search_parameters": {"q": "q"}}
        self.assertEqual(normalize_response(base, provider="custom", snapshot_id="1")["aio_status"], "absent")
        for status in ("present", "empty", "absent", "unavailable", "unresolved"):
            response = {**base, "extraction_metadata": {"aio_status": status}}
            self.assertEqual(normalize_response(response, provider="custom", snapshot_id="x")["aio_status"], status)
        redacted = {**base, "ai_overview": {"page_token": "[REDACTED]"}}
        self.assertEqual(normalize_response(redacted, provider="serpapi", snapshot_id="2")["aio_status"], "unresolved")
        wrapped = {**base, "collection": {"overview_status": "unresolved"}, "ai_overview": {"page_token": "[REDACTED]"}}
        self.assertEqual(normalize_response(wrapped, provider="serpapi", snapshot_id="3")["aio_status"], "unresolved")
        error = {**base, "google": {"error": {"code": "X", "message": "bad"}}}
        result = normalize_response(error, provider="custom", snapshot_id="4")
        self.assertEqual(result["aio_status"], "error")
        self.assertEqual(result["error"]["code"], "X")

    def test_bad_and_unmapped_citation_indexes_and_missing_mappings_warn(self):
        value = self.sample()
        blocks = value["ai_overview"]["ai_overview"]["text_blocks"]
        blocks[0]["reference_indexes"] = [True, -1, "0", 42, 0]
        value["ai_overview"]["ai_overview"]["references"].append({"index": 0, "link": "https://duplicate.test"})
        r = normalize_response(value, provider="serpapi", snapshot_id="s")
        self.assertTrue(any(w.startswith("invalid_citation_index") for w in r["warnings"]))
        self.assertIn("unmapped_citation_index:42", r["warnings"])
        self.assertIn("duplicate_reference_index:0", r["warnings"])
        value = self.sample()
        for block in value["ai_overview"]["ai_overview"]["text_blocks"]:
            block.pop("reference_indexes", None)
            for item in block.get("list", []):
                item.pop("reference_indexes", None)
        result = normalize_response(value, provider="serpapi", snapshot_id="s")
        self.assertIn("references_without_citation_mappings", result["warnings"])

    def test_redirect_warning_distinguishes_opaque_from_decoded_and_checks_organic(self):
        value = self.sample()
        value["ai_overview"]["ai_overview"]["references"][0]["link"] = "https://www.google.com/url?q=https%3A%2F%2Fsource.test%2Farticle"
        value["organic_results"][0]["link"] = "https://www.google.com/goto?url=opaque-token"
        result = normalize_response(value, provider="serpapi", snapshot_id="s")
        self.assertIn("opaque_google_redirect_url", result["warnings"])
        value["organic_results"][0]["link"] = "https://www.google.com/url?q=https%3A%2F%2Fr.test%2Farticle"
        result = normalize_response(value, provider="serpapi", snapshot_id="s")
        self.assertNotIn("opaque_google_redirect_url", result["warnings"])

    def test_pagination_metadata_preserves_actual_rank_and_counts(self):
        value = self.sample()
        value["google_extra_pages"] = [{"search_metadata": {"id": "page2"}, "search_parameters": {"start": 10}, "organic_results": [{"position": 11}]}]
        result = normalize_response(value, provider="serpapi", snapshot_id="s")
        self.assertEqual(result["organic_count"], 1)
        self.assertTrue(result["pagination"]["merged_pages"])
        self.assertEqual(result["pagination"]["extra_pages"][0]["start"], 10)
        self.assertEqual(result["organic_results"][0]["position"], 3)
        self.assertNotIn("organic_missing_fields:link,snippet,title", result["warnings"])

    def test_export_writes_only_full_sanitized_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            source = self.sample()
            source["search_parameters"]["q"] = "=formula"
            record = normalize_response(source, provider="custom", snapshot_id="one")
            record["ai_overview"]["references"][0]["snippet"] = "https://user:password@example.test"
            export_records([record], out)
            saved = json.loads((out / "records.json").read_text())
            self.assertEqual(saved[0]["organic_results"][0]["position"], 3)
            self.assertEqual([p.name for p in out.iterdir()], ['records.json'])
            self.assertEqual(saved[0]['provenance']['query'], '=formula')
            self.assertEqual(saved[0]['ai_overview']['blocks'][0]['snippet'], '=Heading')
            self.assertEqual(saved[0]['ai_overview']['references'][0]['index'], 0)
            self.assertNotIn('password', json.dumps(saved))
            with self.assertRaises(FileExistsError):
                export_records([record], out)

    def test_refuses_duplicate_snapshot_ids_before_creating_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            record = normalize_response(self.sample(), provider="custom", snapshot_id="duplicate")
            with self.assertRaises(ValueError):
                export_records([record, record], out)
            self.assertFalse(out.exists())

    def test_loader_accepts_native_batches_lists_and_sibling_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "batch.json"
            p.write_text(json.dumps({"P01": self.sample(), "P02": self.sample()}))
            records = load_input([p], provider="custom")
            self.assertEqual(len(records), 2)
            self.assertEqual([r["provenance"]["query_id"] for r in records], ["P01", "P02"])
            snapshots = Path(tmp) / "snapshots.json"
            snapshots.write_text(json.dumps([{"prompt": "query", "google": self.sample(), "collection": {"query_id": "P07", "overview_status": "inline"}}]))
            loaded = load_input([snapshots], provider="serpapi")
            self.assertEqual(loaded[0]["provenance"]["query_id"], "P07")
            self.assertEqual(loaded[0]["provenance"]["query"], "query")

    def test_loader_rejects_invalid_entries_and_reuses_query_identity_for_repeats(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i in range(2):
                p = Path(tmp) / f"{i}.json"
                p.write_text(json.dumps(self.sample()))
                paths.append(p)
            records = load_input(paths, provider="custom")
            self.assertEqual(records[0]["provenance"]["query_id"], records[1]["provenance"]["query_id"])
            invalid = Path(tmp) / "invalid.json"
            invalid.write_text('{"P01": null}')
            with self.assertRaises(ValueError):
                load_input([invalid], provider="custom")

    def test_null_aio_and_wrapped_pagination_have_correct_status_and_provenance(self):
        result = normalize_response({'prompt': 'q', 'google': {'organic_results': []},
                                     'ai_overview': None,
                                     'google_extra_pages': [{'search_parameters': {'start': 10}, 'organic_results': []}]},
                                    provider='serpapi', snapshot_id='s')
        self.assertEqual(result['aio_status'], 'absent')
        self.assertEqual(result['pagination']['extra_pages'][0]['start'], 10)

    def test_loader_accepts_existing_custom_run_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'run.json'
            p.write_text(json.dumps({'run_id': 'r1', 'captured_at': '2026-01-01T00:00:00Z',
                                    'results': [{'query_id': 'P07', 'result': self.sample()}]}))
            result = load_input([p], provider='custom')[0]
            self.assertEqual(result['provenance']['query_id'], 'P07')
            self.assertEqual(result['provenance']['captured_at'], '2026-01-01T00:00:00Z')

    def test_malformed_wrapper_is_invalid_not_a_crash(self):
        for response in ({'google': 'wrong'}, {'collection': 'wrong'}, {'ai_overview': None},
                         {'google': {'organic_results': [None]}}):
            result = normalize_response(response, provider='serpapi', snapshot_id='s')
            self.assertIn(result['aio_status'], ('invalid', 'absent'))

    def test_raw_sensitive_values_are_redacted_even_when_key_is_out_of_scope(self):
        value = self.sample()
        value['search_parameters']['api_key'] = 'SUPERSECRET123'
        value['organic_results'][0]['snippet'] = 'error SUPERSECRET123'
        result = normalize_response(value, provider='serpapi', snapshot_id='s')
        self.assertNotIn('SUPERSECRET123', json.dumps(result))

    def test_loader_aligns_later_standalone_repeats_with_explicit_batch_query_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp) / 'batch.json'
            repeat = Path(tmp) / 'repeat.json'
            batch.write_text(json.dumps({'P10': self.sample()}))
            repeat.write_text(json.dumps(self.sample()))
            loaded = load_input([batch, repeat], provider='serpapi')
            self.assertEqual([r['provenance']['query_id'] for r in loaded], ['P10', 'P10'])
            batch.write_text(json.dumps({'P10_run2': self.sample()}))
            loaded = load_input([batch], provider='custom')
            self.assertEqual(loaded[0]['provenance']['query_id'], 'P10')
            self.assertEqual(loaded[0]['provenance']['run_index'], 2)

    def test_failed_export_does_not_leave_a_partial_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'export'
            record = normalize_response(self.sample(), provider='custom', snapshot_id='s')
            with patch('search_export._write_export_files', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    export_records([record], out)
            self.assertFalse(out.exists())
            export_records([record], out)
            self.assertEqual(list(out.iterdir()), [out / 'records.json'])

    def test_cli_offline_handles_input_errors_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "export"
            self.assertEqual(main(["--provider", "custom", "--input", str(Path(tmp) / "missing.json"), "--output-dir", str(out)]), 2)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
