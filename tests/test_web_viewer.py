"""Viewer transport/route tests and an opt-in real-browser smoke test (no remote calls)."""
import copy
import io
import hashlib
from pathlib import Path
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse
import urllib.request

from aio_viewer.server import NoRedirect, SupabaseReader, ViewerError, ViewerHandler, api_response

RUN_ID = "12345678-1234-4234-8234-123456789abc"
ROW = {
    "run_id": RUN_ID, "query_id": "P01", "ordinal": 1, "query": "A saved search <script>window.hacked=true</script>",
    "aio_status": "present", "capture_status": "complete", "status": "success", "hl": "en", "gl": "us",
    "captured_at": "2026-11-12T14:00:00Z", "warnings": ["unmapped_citation_index:7"],
    "aio_response": {"blocks": [
        {"type": "heading", "snippet": "Saved answer"},
        {"type": "paragraph", "snippet": "Read the guide.", "reference_indexes": [0, 7],
         "snippet_links": [{"text": "guide", "link": "https://example.com/guide"}]},
        {"type": "list", "ordered": True, "start": 3, "list": [{"snippet": "Item one", "list": [{"snippet": "Nested item"}]}]},
        {"type": "table", "detailed": [[{"snippet": "Header", "is_header": True}, {"snippet": "Value"}],
                                        [{"snippet": "Cell", "colspan": 2, "reference_indexes": [0]}]]},
    ], "references": [{"index": 0, "title": "A source", "source": "Example", "link": "https://example.com", "snippet": "Full source snippet"}]},
    "organic_results": [{"position": 3, "title": "A safe result", "source": "Example", "link": "https://example.com/result",
                         "snippet": "A full saved snippet", "sitelinks": {"inline": [{"title": "Guide", "link": "https://example.com/guide"}]}},
                        {"position": 4, "title": "Unsafe URL", "link": "javascript:window.hacked=true", "snippet": "<img src=x onerror=window.hacked=true>"}],
}


class FixtureReader:
    def get(self, table, params):
        if table == "scrape_runs":
            return [{"run_id": RUN_ID, "label": "Synthetic fixture", "provider": "custom", "run_index": 1}]
        if params["select"] == "*":
            return [copy.deepcopy(ROW)]
        if "query" in params and "missing" in params["query"]:
            return []
        return [{key: ROW[key] for key in ("query_id", "query", "ordinal", "aio_status")}]


SECOND_RUN = "22345678-1234-4234-8234-123456789abc"
THIRD_RUN = "32345678-1234-4234-8234-123456789abc"


class ComparisonReader:
    """Small deterministic Data API stand-in; no credentials or remote calls."""
    def __init__(self):
        self.calls = []
        base = {**copy.deepcopy(ROW), "capture_status": "ok", "status": "ok", "device": "desktop", "warnings": []}
        absent = {**copy.deepcopy(base), "run_id": SECOND_RUN, "query_id": "B-absent", "ordinal": 1, "aio_status": "absent", "aio_response": None, "device": "mobile", "captured_at": "2026-11-13T14:00:00Z", "organic_results": [{**base["organic_results"][0], "position": 5}, {"position": 1, "title": "New result", "link": "https://new.example/", "snippet": "New saved snippet"}]}
        other = {**copy.deepcopy(base), "run_id": SECOND_RUN, "query_id": "B-present", "ordinal": 2}
        other["aio_response"]["references"][0]["title"] = "Second-run source"
        other["aio_response"]["blocks"][0]["snippet"] = "Second-run answer"
        self.rows = [base, absent, other,
                     {**copy.deepcopy(other), "query_id": "B-french", "ordinal": 3, "hl": "fr"},
                     {**copy.deepcopy(other), "query_id": "P01", "ordinal": 4, "query": "Different query with reused ID"},
                     {**copy.deepcopy(base), "run_id": THIRD_RUN, "query": "Not the selected query"}]
        self.rows.extend({**copy.deepcopy(base), "query_id": f"A{i}", "ordinal": i + 1, "query": f"Other saved search {i}", "aio_status": "absent", "aio_response": None, "organic_results": [], "device": "mobile", "warnings": ["example warning"], "captured_at": "2026-11-13T14:00:00Z"} for i in range(1, 52))

    def get(self, table, params):
        self.calls.append((table, dict(params)))
        if table == "scrape_runs":
            rows = [{"run_id": rid, "provider": "custom", "label": label} for rid, label in ((RUN_ID, "Baseline"), (SECOND_RUN, "Second capture"), (THIRD_RUN, "No match"))]
        else:
            rows = sorted(self.rows, key=lambda row: (row["ordinal"], row["query_id"]))
            for key in ("run_id", "query_id", "query", "hl", "gl", "device", "aio_status", "capture_status"):
                if key not in params:
                    continue
                value = params[key]
                if value == "is.null":
                    rows = [row for row in rows if row.get(key) is None]
                elif value.startswith("eq."):
                    value = value[3:]
                    rows = [row for row in rows if row.get(key) == value]
                elif value.startswith("ilike."):
                    needle = value[6:].strip("%").lower()
                    rows = [row for row in rows if needle in row[key].lower()]
            if "or" in params:
                rows = [row for row in rows if row.get("aio_status") != "present"]
            for key in ("warnings", "organic_results"):
                if key in params:
                    rows = [row for row in rows if bool(row.get(key)) == (params[key] == "neq.[]")]
            if "and" in params:
                for bound in params["and"].strip("()").split(","):
                    _, operator, value = bound.split(".", 2)
                    rows = [row for row in rows if row.get("captured_at") and (row["captured_at"] >= value if operator == "gte" else row["captured_at"] < value)]
        offset = params.get("offset", 0)
        rows = rows[offset:offset + params.get("limit", 100)]
        if table != "scrape_runs" and params["select"] != "*":
            rows = [{key: row.get(key) for key in params["select"].split(",")} for row in rows]
        return copy.deepcopy(rows)


class RouteTests(unittest.TestCase):
    def test_filters_are_allowlisted_and_applied_before_pagination(self):
        reader = Mock()
        reader.get.return_value = []
        api_response(reader, "/api/queries", {"run_id": RUN_ID, "aio_status": "absent", "capture_status": "ok", "hl": "en", "gl": "us", "device": "mobile", "has_warnings": "yes", "has_organic": "no", "captured_from": "2026-11-12", "captured_to": "2026-11-13", "limit": "2", "offset": "50"})
        params = reader.get.call_args.args[1]
        for key, value in {"aio_status": "eq.absent", "capture_status": "eq.ok", "hl": "eq.en", "gl": "eq.us", "device": "eq.mobile", "warnings": "neq.[]", "organic_results": "eq.[]", "limit": 3, "offset": 50}.items():
            self.assertEqual(params[key], value)
        self.assertEqual(params["and"], "(captured_at.gte.2026-11-12T00:00:00Z,captured_at.lt.2026-11-14T00:00:00Z)")
        api_response(reader, "/api/queries", {"run_id": RUN_ID, "aio_status": "not_present", "capture_status": "unknown"})
        self.assertEqual(reader.get.call_args.args[1]["or"], "(aio_status.neq.present,aio_status.is.null)")
        self.assertEqual(reader.get.call_args.args[1]["capture_status"], "is.null")
        reader.reset_mock()
        for extra in ({"aio_status": "eq.present"}, {"capture_status": "complete"}, {"has_warnings": "maybe"}, {"has_organic": "true"}, {"device": "x\nx"}, {"gl": "x" * 65}, {"captured_from": "2026-02-30"}, {"captured_to": "9999-12-31"}, {"captured_from": "2026-12-01", "captured_to": "2026-11-01"}, {"captured_to": "2026-11-12),or=(id.gt.0"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                api_response(reader, "/api/queries", {"run_id": RUN_ID, **extra})
        reader.get.assert_not_called()

    def test_comparison_matches_exact_text_and_locale_not_query_id(self):
        reader = Mock()
        query = '\"Quoted\", query (with punctuation) & query_id=anything'
        reader.get.side_effect = [[{"query": query, "hl": "en", "gl": None}], [{"query_id": "different-id"}, {"query_id": "another-id"}]]
        target = "22345678-1234-4234-8234-123456789abc"
        result = api_response(reader, "/api/comparison-matches", {"run_id": RUN_ID, "query_id": "P01", "compare_run_id": target, "limit": "1", "offset": "3"})
        self.assertEqual(result["items"], [{"query_id": "different-id"}])
        self.assertTrue(result["has_more"])
        self.assertEqual(reader.get.call_args_list[0].args[1]["query_id"], "eq.P01")
        params = reader.get.call_args.args[1]
        self.assertEqual(params["query"], "eq." + query)
        self.assertEqual(params["hl"], "eq.en")
        self.assertEqual(params["gl"], "is.null")
        self.assertEqual(params["run_id"], "eq." + target)
        self.assertNotIn("query_id", params)
        self.assertNotIn("aio_response", params["select"])
        reader.reset_mock()
        for extra in ({"compare_run_id": RUN_ID}, {"compare_run_id": "bad"}, {"query_id": ""}, {"offset": "-1"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                api_response(reader, "/api/comparison-matches", {"run_id": RUN_ID, "query_id": "P01", "compare_run_id": target, **extra})
        reader.get.assert_not_called()
        reader.get.side_effect = None
        reader.get.return_value = []
        self.assertIsNone(api_response(reader, "/api/comparison-matches", {"run_id": RUN_ID, "query_id": "missing", "compare_run_id": target}))
        self.assertEqual(reader.get.call_count, 1)

    def test_list_is_lightweight_and_paginates(self):
        reader = Mock()
        reader.get.return_value = [{"query_id": "1"}, {"query_id": "2"}, {"query_id": "3"}]
        result = api_response(reader, "/api/queries", {"run_id": RUN_ID, "limit": "2", "offset": "10", "search": "a&limit=999"})
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(result["has_more"])
        table, params = reader.get.call_args.args
        self.assertEqual(table, "queries")
        self.assertEqual(params["limit"], 3)
        self.assertEqual(params["offset"], 10)
        self.assertEqual(params["run_id"], "eq." + RUN_ID)
        self.assertEqual(params["query"], "ilike.%a&limit=999%")
        self.assertNotIn("aio_response", params["select"])
        self.assertNotIn("organic_results", params["select"])

    def test_filtered_and_matching_pagination_use_the_whole_run(self):
        reader = ComparisonReader()
        first = api_response(reader, "/api/queries", {"run_id": RUN_ID, "aio_status": "absent"})
        self.assertEqual(len(first["items"]), 50)
        self.assertTrue(first["has_more"])
        last = api_response(reader, "/api/queries", {"run_id": RUN_ID, "aio_status": "absent", "offset": "50"})
        self.assertEqual([row["query_id"] for row in last["items"]], ["A51"])
        self.assertFalse(last["has_more"])
        params = {"run_id": RUN_ID, "query_id": "P01", "compare_run_id": SECOND_RUN, "limit": "1"}
        first_match = api_response(reader, "/api/comparison-matches", params)
        self.assertEqual([row["query_id"] for row in first_match["items"]], ["B-absent"])
        self.assertTrue(first_match["has_more"])
        second_match = api_response(reader, "/api/comparison-matches", {**params, "offset": "1"})
        self.assertEqual([row["query_id"] for row in second_match["items"]], ["B-present"])
        self.assertFalse(second_match["has_more"])
        # A reused query ID with different text and a French-language duplicate
        # were excluded, and missing captures are not fabricated as absent AIOs.
        missing = api_response(reader, "/api/comparison-matches", {**params, "compare_run_id": THIRD_RUN})
        self.assertEqual(missing["items"], [])
        null = {**copy.deepcopy(reader.rows[0]), "query_id": "legacy", "ordinal": 99, "aio_status": None}
        reader.rows.append(null)
        unknown = api_response(reader, "/api/queries", {"run_id": RUN_ID, "aio_status": "unknown"})
        self.assertEqual([row["query_id"] for row in unknown["items"]], ["legacy"])

    def test_detail_and_missing_row(self):
        reader = Mock()
        reader.get.return_value = [ROW]
        self.assertEqual(api_response(reader, "/api/response", {"run_id": RUN_ID, "query_id": "P01"}), ROW)
        self.assertEqual(reader.get.call_args.args[1]["query_id"], "eq.P01")
        reader.get.return_value = []
        self.assertIsNone(api_response(reader, "/api/response", {"run_id": RUN_ID, "query_id": "P01"}))

    def test_validation_before_network(self):
        reader = Mock()
        for params in ({}, {"run_id": "not-a-uuid"}, {"run_id": RUN_ID, "limit": "0"},
                       {"run_id": RUN_ID, "limit": "101"}, {"run_id": RUN_ID, "offset": "-1"},
                       {"run_id": RUN_ID, "search": "x" * 301}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                api_response(reader, "/api/queries", params)
        with self.assertRaises(ValueError):
            api_response(reader, "/api/response", {"run_id": RUN_ID})
        reader.get.assert_not_called()
        with self.assertRaises(KeyError):
            api_response(reader, "/api/arbitrary-table", {})

    def test_config_restricts_host(self):
        for project in (None, "", "https://evil.test", "../" + "a" * 20):
            with self.subTest(project=project), self.assertRaises(ViewerError):
                SupabaseReader(project, "secret")
        with self.assertRaises(ViewerError):
            SupabaseReader("a" * 20, "")

    @patch("aio_viewer.server.urllib.request.build_opener")
    def test_private_data_api_read(self, build):
        response = io.BytesIO(b'[{"query_id":"P01"}]')
        build.return_value.open.return_value = response
        reader = SupabaseReader("a" * 20, "sb_secret_TEST")
        self.assertEqual(reader.get("queries", {"query": "ilike.%a&limit=999%"}), [{"query_id": "P01"}])
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Apikey"), "sb_secret_TEST")
        parsed = urllib.parse.urlsplit(request.full_url)
        self.assertEqual(parsed.hostname, "a" * 20 + ".supabase.co")
        self.assertEqual(urllib.parse.parse_qs(parsed.query), {"query": ["ilike.%a&limit=999%"]})
        self.assertIs(build.call_args.args[0], NoRedirect)
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test"))

    @patch("aio_viewer.server.urllib.request.build_opener")
    def test_errors_never_expose_upstream_body_or_key(self, build):
        for code in (401, 403, 404, 500):
            build.return_value.open.side_effect = urllib.error.HTTPError("https://private.test", code, "sb_secret_TEST", {}, None)
            with self.subTest(code=code), self.assertRaises(ViewerError) as error:
                SupabaseReader("a" * 20, "sb_secret_TEST").get("queries", {})
            self.assertNotIn("sb_secret_TEST", str(error.exception))


class ProjectionFixtureTests(unittest.TestCase):
    def test_fixtures_replay_requested_original_inputs_without_writes(self):
        from supabase_results import normalize_records, normalize_response_snapshot
        root = Path(__file__).resolve().parents[1]
        custom = root / "query_test/pap-300-final.json"
        serpapi = root / "query_test/pap-300-serpapi.records.json"
        if not custom.exists() or not serpapi.exists():
            self.skipTest("Original private capture artifacts are not included in every checkout")
        with patch.dict(os.environ, {}, clear=True):
            native = json.loads(custom.read_text())
            native["results"] = native["results"][:1]
            native["planned_query_count"] = 1
            native_row = normalize_response_snapshot(native)["queries"][0]
            shared = json.loads(serpapi.read_text())[:1]
            shared_row = normalize_records(shared, RUN_ID)["queries"][0]
        fixtures = root / "tests/fixtures/viewer"
        self.assertEqual(native_row, json.loads((fixtures / "pap-custom.json").read_text()))
        self.assertEqual(shared_row, json.loads((fixtures / "pap-serpapi.json").read_text()))
        self.assertIn("snippet_highlighted_words", json.dumps(shared))
        def keys(value):
            if isinstance(value, dict):
                yield from value
                for child in value.values():
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)
        self.assertNotIn("snippet_highlighted_words", set(keys(shared_row)))
        self.assertNotIn("favicon", set(keys(shared_row)))
        self.assertEqual([r["position"] for r in native_row["organic_results"]], list(range(1, 10)))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ViewerHandler)
        self.server.reader = FixtureReader()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_assets_and_headers(self):
        for path in ("/", "/app.js", "/styles.css"):
            with urllib.request.urlopen(self.base + path) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                self.assertNotIn("Access-Control-Allow-Origin", response.headers)
                self.assertTrue(response.read())

    def test_api_and_closed_file_server(self):
        with urllib.request.urlopen(self.base + "/api/runs") as response:
            self.assertEqual(json.load(response)["items"][0]["run_id"], RUN_ID)
        for path, status in (("/.env", 404), ("/../.env", 404), ("/api/queries", 400)):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.base + path)
            self.assertEqual(error.exception.code, status)

    def test_rebinding_and_cross_site_rejected(self):
        for headers in ({"Host": "evil.test"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers), self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(self.base + "/api/runs", headers=headers))
            self.assertEqual(error.exception.code, 403)

    @unittest.skipUnless(os.environ.get("AIO_VIEWER_BROWSER"), "Set AIO_VIEWER_BROWSER to a Chromium executable for browser checks")
    def test_browser_preview_json_and_search(self):
        from playwright.sync_api import expect, sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=os.environ["AIO_VIEWER_BROWSER"], headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(self.base)
            page.locator(".record").click()
            page.locator("#response").wait_for(state="visible")
            self.assertIn("A saved search <script>", page.locator("#query-title").inner_text())
            self.assertEqual(page.locator("#aio-content ol").get_attribute("start"), "3")
            self.assertEqual(page.locator("#aio-content th").inner_text(), "Header")
            self.assertEqual(page.locator("td[colspan='2']").inner_text(), "Cell")
            self.assertEqual(page.locator("#aio-content button.citation").count(), 2)
            self.assertEqual(page.locator("#aio-content span.citation").inner_text(), "? 7")
            self.assertEqual(json.loads(page.locator("#aio-content button.citation").first.get_attribute("data-reference-indexes")), [0])
            page.locator("#aio-content button.citation").first.click()
            expect(page.locator("#sources-dialog")).to_be_visible()
            self.assertEqual(page.locator("#sources-dialog .source-index").inner_text(), "Citation index 0")
            page.keyboard.press("Escape")
            expect(page.locator("#sources-dialog")).not_to_be_visible()
            self.assertEqual(page.locator("#organic-results h3 a").count(), 1)
            self.assertEqual(page.locator("#organic-results script, #organic-results img").count(), 0)
            self.assertIsNone(page.evaluate("window.hacked"))
            page.locator("#aio-json").click()
            self.assertEqual(json.loads(page.locator("#json-content").inner_text()), ROW["aio_response"])
            page.locator("#json-scope").select_option("response")
            self.assertEqual(json.loads(page.locator("#json-content").inner_text()), ROW)
            for status in ("absent", "unavailable", "unresolved", "empty", "invalid", "error"):
                changed = {**ROW, "aio_status": status, "aio_response": None, "organic_results": []}
                page.evaluate("row => renderResponse(row)", changed)
                self.assertIn(status, page.locator("#aio-state").inner_text())
                self.assertIn("No organic results", page.locator("#organic-results").inner_text())
            page.evaluate("row => renderResponse(row)", ROW)
            page.locator("#preview-tab").click()
            page.set_viewport_size({"width": 390, "height": 844})
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            page.locator("#mobile-sources").click()
            expect(page.locator("#sources-dialog")).to_be_visible()
            page.locator("#sources-close").click()
            page.locator("#archive-toggle").click()
            page.locator("#search").fill("missing")
            page.locator("#search").press("Enter")
            expect(page.locator("#list-status")).to_have_text("No matching responses.")
            self.assertTrue(page.locator("#response").is_hidden())
            self.assertFalse(errors)
            browser.close()

    @unittest.skipUnless(os.environ.get("AIO_VIEWER_BROWSER"), "Set AIO_VIEWER_BROWSER for archive chrome checks")
    def test_browser_archive_chrome_and_keyboard_access(self):
        from playwright.sync_api import expect, sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ["AIO_VIEWER_BROWSER"], headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
            page.goto(self.base)
            expect(page.locator("#welcome h1")).to_have_text("Inspect a saved search")
            expect(page.locator(".record")).to_have_count(1)
            self.assertEqual(page.locator(".record-action").count(), 0)
            page.locator(".record").focus()
            page.keyboard.press("Enter")
            expect(page.locator("#response")).to_be_visible()
            self.assertEqual(page.locator(".metadata-id").inner_text(), ROW["query_id"])
            for selector in (".record-id", ".metadata-id"):
                self.assertIn("monospace", page.locator(selector).evaluate("node => getComputedStyle(node).fontFamily"))
            for selector in (".eyebrow", ".archive-note", ".text-button", ".pagination", ".filter-grid label"):
                self.assertGreaterEqual(page.locator(selector).first.evaluate("node => parseFloat(getComputedStyle(node).fontSize)"), 12)
            self.assertEqual(page.locator("#compare-open").evaluate("node => getComputedStyle(node).position"), "static")
            for selector in ("#run", "#search", "#filter-aio", ".filter-actions button", "#compare-open"):
                self.assertGreaterEqual(page.locator(selector).first.bounding_box()["height"], 36)
            page.locator("#preview-tab").focus()
            page.keyboard.press("ArrowRight")
            expect(page.locator("#domains-tab")).to_be_focused()
            expect(page.locator("#domains-panel")).to_be_visible()
            page.keyboard.press("ArrowRight")
            expect(page.locator("#json-tab")).to_be_focused()
            expect(page.locator("#json-panel")).to_be_visible()
            page.keyboard.press("ArrowRight")
            expect(page.locator("#preview-tab")).to_be_focused()
            self.assertEqual(page.locator("#preview-tab").evaluate("node => getComputedStyle(node).outlineStyle"), "solid")
            for width in (1024, 768, 390):
                page.set_viewport_size({"width": width, "height": 844})
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
            page.locator("#archive-toggle").click()
            expect(page.locator("#archive-sidebar")).to_be_visible()
            for selector in ("#run", "#search", "#filter-aio", ".filter-actions button"):
                self.assertGreaterEqual(page.locator(selector).first.bounding_box()["height"], 40)
            page.locator("#search").focus()
            page.keyboard.press("Escape")
            expect(page.locator("#archive-sidebar")).to_be_hidden()
            expect(page.locator("#archive-toggle")).to_be_focused()
            browser.close()

    @unittest.skipUnless(os.environ.get("AIO_VIEWER_BROWSER"), "Set AIO_VIEWER_BROWSER for navigation checks")
    def test_browser_navigation_and_intermediate_widths(self):
        from playwright.sync_api import expect, sync_playwright
        second_run = "22345678-1234-4234-8234-123456789abc"
        class PagedReader:
            def get(self, table, params):
                if table == "scrape_runs":
                    return [{"run_id": value, "provider": "custom", "label": label} for value, label in ((RUN_ID, "A"), (second_run, "B"))]
                prefix = "A" if params["run_id"] == "eq." + RUN_ID else "B"
                if params["select"] == "*":
                    return [{**copy.deepcopy(ROW), "query_id": params["query_id"][3:], "query": params["query_id"][3:]}]
                values = [{"query_id": f"{prefix}{i}", "query": f"{prefix}{i}", "aio_status": "present"} for i in range(1, 52)]
                return values[params["offset"]:params["offset"] + params["limit"]]
        self.server.reader = PagedReader()
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ["AIO_VIEWER_BROWSER"], headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(self.base)
            expect(page.locator(".record")).to_have_count(50)
            page.locator("#next").click()
            expect(page.locator(".record")).to_have_count(1)
            page.locator(".record").click()
            expect(page.locator("#query-title")).to_have_text("A51")
            page.locator("#prev").click()
            expect(page.locator(".record")).to_have_count(50)
            page.locator("#run").select_option(second_run)
            page.locator(".record").first.click()
            expect(page.locator("#query-title")).to_have_text("B1")
            for width in (1024, 768, 390):
                page.set_viewport_size({"width": width, "height": 900})
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
            browser.close()

    @unittest.skipUnless(os.environ.get("AIO_VIEWER_BROWSER"), "Set AIO_VIEWER_BROWSER for comparison checks")
    def test_browser_filters_and_two_run_comparison(self):
        from playwright.sync_api import expect, sync_playwright
        reader = ComparisonReader()
        self.server.reader = reader
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ["AIO_VIEWER_BROWSER"], headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(self.base)
            expect(page.locator(".record")).to_have_count(50)
            page.locator("#filter-aio").select_option("absent")
            expect(page.locator(".record")).to_have_count(50)
            page.locator("#next").click()
            expect(page.locator(".record")).to_have_count(1)
            expect(page.locator(".record-query")).to_have_text("Other saved search 51")
            page.locator("#filter-aio").select_option("present")
            expect(page.locator(".record")).to_have_count(1)
            expect(page.locator("#prev")).to_be_disabled()
            page.locator(".filter-details summary").click()
            page.locator("#filter-hl").fill("en")
            page.locator("#filter-gl").fill("us")
            page.locator("#filter-device").fill("desktop")
            page.locator("#filter-from").fill("2026-11-12")
            page.locator("#filter-to").fill("2026-11-12")
            page.locator("#filter-capture").select_option("ok")
            page.locator("#filter-warnings").select_option("no")
            page.locator("#filter-organic").select_option("yes")
            page.get_by_role("button", name="Apply filters", exact=True).click()
            expect(page.locator(".record")).to_have_count(1)
            self.assertTrue(any(params.get("and") == "(captured_at.gte.2026-11-12T00:00:00Z,captured_at.lt.2026-11-13T00:00:00Z)" and params.get("organic_results") == "neq.[]" for _, params in reader.calls))
            page.locator("#filters-clear").click()
            expect(page.locator(".record")).to_have_count(50)
            page.locator(".record").first.click()
            expect(page.locator("#response")).to_be_visible()
            page.locator("#compare-open").click()
            expect(page.locator("#compare-run option")).to_have_count(2)
            self.assertNotIn(RUN_ID, page.locator("#compare-run option").evaluate_all("nodes=>nodes.map(n=>n.value)"))
            page.locator("#compare-load").click()
            expect(page.locator("#compare-match option")).to_have_count(3)
            expect(page.locator(".comparison-pane")).to_have_count(0)
            page.locator("#compare-match").select_option("0")
            expect(page.locator(".comparison-pane")).to_have_count(2)
            expect(page.locator("#compare-side-b .comparison-pane-status")).to_contain_text("AIO: absent")
            expect(page.locator("#compare-side-b .comparison-pane-meta")).to_contain_text("mobile")
            page.locator(".comparison-ranks summary").click()
            self.assertEqual(page.locator(".comparison-ranks tbody tr").count(), 3)
            self.assertIn("Moved", page.locator(".comparison-ranks tbody").inner_text())
            self.assertIn("Added", page.locator(".comparison-ranks tbody").inner_text())
            self.assertIn("Removed", page.locator(".comparison-ranks tbody").inner_text())
            self.assertEqual(page.locator("#compare-side-a ol").get_attribute("start"), "3")
            self.assertEqual(page.locator("#compare-side-a .citation.unmapped").inner_text(), "? 7")
            page.locator("#compare-side-a .comparison-json summary").click()
            self.assertEqual(json.loads(page.locator("#compare-side-a .comparison-json pre").inner_text()), reader.rows[0])
            page.locator("#compare-side-a .comparison-json summary").click()
            self.assertEqual(page.locator("#compare-panes script, #compare-panes img").count(), 0)
            self.assertIsNone(page.evaluate("window.hacked"))
            page.locator("#comparison").screenshot(path="/tmp/viewer-comparison-desktop.png")
            page.locator("#compare-match").select_option("1")
            expect(page.locator("#compare-side-b .aio-content")).to_contain_text("Second-run answer")
            page.locator("#compare-side-b button.citation").first.click()
            expect(page.locator("#sources-dialog .source-title")).to_have_text("Second-run source")
            page.keyboard.press("Escape")
            for width in (1024, 768, 390):
                page.set_viewport_size({"width": width, "height": 900})
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
            page.locator("#comparison").screenshot(path="/tmp/viewer-comparison-mobile.png")
            page.locator("#compare-run").select_option(THIRD_RUN)
            expect(page.locator(".comparison-pane")).to_have_count(0)
            page.locator("#compare-load").click()
            expect(page.locator("#compare-status")).to_contain_text("No exact query")
            page.locator("#compare-close").click()
            expect(page.locator("#response")).to_be_visible()
            expect(page.locator("#comparison")).to_be_hidden()
            self.assertFalse(errors)
            browser.close()

    @unittest.skipUnless(os.environ.get("AIO_VIEWER_BROWSER"), "Set AIO_VIEWER_BROWSER for comparison race checks")
    def test_browser_comparison_discards_stale_detail(self):
        from playwright.sync_api import expect, sync_playwright
        reader = ComparisonReader()
        self.server.reader = reader
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ["AIO_VIEWER_BROWSER"], headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(self.base)
            page.locator(".record").first.click()
            page.locator("#compare-open").click()
            expect(page.locator("#compare-load")).to_be_enabled()
            page.locator("#compare-load").click()
            expect(page.locator("#compare-match option")).to_have_count(3)
            held = []
            def delay(route):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(route.request.url).query)
                if query.get("query_id") == ["B-absent"]:
                    held.append(route)
                else:
                    route.continue_()
            page.route("**/api/response?*", delay)
            page.locator("#compare-match").select_option("0")
            page.wait_for_timeout(100)
            self.assertEqual(len(held), 1)
            page.locator("#compare-match").select_option("1")
            expect(page.locator("#compare-side-b .aio-content")).to_contain_text("Second-run answer")
            held.pop().fulfill(status=200, json=reader.rows[1])
            page.wait_for_timeout(100)
            expect(page.locator("#compare-side-b .aio-content")).to_contain_text("Second-run answer")
            page.locator("#compare-match").select_option("0")
            page.wait_for_timeout(100)
            self.assertEqual(len(held), 1)
            page.locator("#compare-close").click()
            held.pop().fulfill(status=200, json=reader.rows[1])
            page.wait_for_timeout(100)
            expect(page.locator("#comparison")).to_be_hidden()
            expect(page.locator(".comparison-pane")).to_have_count(0)
            browser.close()

    @unittest.skipUnless(os.environ.get("AIO_VIEWER_BROWSER"), "Set AIO_VIEWER_BROWSER for visual regression checks")
    def test_browser_visual_regression_and_content_preservation(self):
        from playwright.sync_api import expect, sync_playwright
        from tools.viewer_visual import FIXTURES, MEASURE, ROOT, VisualReader
        reference = json.loads((FIXTURES / "reference.json").read_text())
        self.server.reader = VisualReader(reference)
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ["AIO_VIEWER_BROWSER"], headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US", timezone_id="UTC", device_scale_factor=1, reduced_motion="reduce")
            page = context.new_page()
            external = []
            page.on("request", lambda request: external.append(request.url) if not request.url.startswith(self.base) else None)
            page.goto(self.base)
            page.locator(".record").first.click()
            page.locator("#response").wait_for(state="visible")
            for name, size in (("desktop", {"width": 1440, "height": 1000}), ("mobile", {"width": 390, "height": 844})):
                page.set_viewport_size(size)
                metrics = page.evaluate(MEASURE)
                self.assertEqual(metrics[".aio"]["backgroundImage"], "none")
                self.assertEqual(metrics[".aio"]["borderRadius"], "0px")
                self.assertEqual(metrics[".search-result h3"]["fontSize"], "20px")
                self.assertEqual(metrics[".result-snippet"]["lineHeight"], "22px")
                self.assertEqual(metrics["#aio-content"]["fontSize"], "14px" if name == "desktop" else "16px")
                self.assertEqual(metrics[".source-title"]["fontSize"], "13px")
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                if name == "desktop":
                    self.assertEqual(metrics["#preview"]["width"], 960)
                    self.assertEqual(metrics["#sources"]["width"], 360)
                    self.assertEqual(metrics["#sources"]["x"] - metrics[".aio"]["x"], 584)
                    self.assertEqual(metrics["#sources"]["y"] - metrics[".aio"]["y"], 56)
                    self.assertEqual(metrics["#sources"]["backgroundColor"], "rgb(241, 244, 255)")
                    self.assertEqual(metrics["#aio-content"]["width"], 544)
                else:
                    self.assertEqual(metrics["#aio-content"]["width"], 358)
                golden_dir = ROOT / "docs/visual/google-2026-11-17/after"
                golden_metrics = json.loads((golden_dir / f"reference-{name}.json").read_text())
                self.assertEqual(browser.version, golden_metrics["browser"], "Use the pinned Chromium for raster comparisons; recapture/review references before changing browser versions")
                if browser.version == golden_metrics["browser"]:
                    actual = page.locator(".aio").screenshot(animations="disabled")
                    expected = (golden_dir / f"reference-{name}-aio.png").read_bytes()
                    if actual != expected:
                        Path(f"/tmp/viewer-regression-{name}.png").write_bytes(actual)
                    self.assertEqual(hashlib.sha256(actual).hexdigest(), hashlib.sha256(expected).hexdigest(),
                                     f"Pinned Chromium raster changed; inspect /tmp/viewer-regression-{name}.png before accepting a new baseline")
            self.assertEqual(page.locator(".answer-block-title").all_text_contents(), ["No Inventory:", "Third-Party Fulfillment:", "Forwarding Orders:"])
            for fixture in ("pap-custom", "pap-serpapi"):
                row = json.loads((FIXTURES / f"{fixture}.json").read_text())
                original = copy.deepcopy(row)
                page.evaluate("row => renderResponse(row)", row)
                self.assertEqual(page.locator("#organic-results h3").all_text_contents(), [r["title"] for r in row["organic_results"]])
                self.assertEqual(page.locator("#organic-results h3 a").evaluate_all("nodes => nodes.map(n => n.getAttribute('href'))"), [r["link"] for r in row["organic_results"]])
                self.assertEqual(page.locator("#sources .source-title").all_text_contents(), [r["title"] for r in row["aio_response"]["references"]])
                self.assertEqual(page.locator("#sources .source-snippet").all_text_contents(), [r["snippet"] for r in row["aio_response"]["references"] if r.get("snippet")])
                for result, rendered in zip(row["organic_results"], page.locator("#organic-results .search-result").all()):
                    if result.get("snippet"):
                        self.assertIn(result["snippet"], rendered.locator(".result-snippet").text_content())
                def snippets(value):
                    if isinstance(value, dict):
                        if isinstance(value.get("snippet"), str):
                            yield value["snippet"]
                        for key, child in value.items():
                            if key != "references":
                                yield from snippets(child)
                    elif isinstance(value, list):
                        for child in value:
                            yield from snippets(child)
                def citation_groups(value):
                    if isinstance(value, dict):
                        if value.get("reference_indexes"):
                            yield value["reference_indexes"]
                        for key in ("list", "items", "table", "detailed"):
                            yield from citation_groups(value.get(key))
                    elif isinstance(value, list):
                        for child in value:
                            yield from citation_groups(child)
                actual_groups = page.locator("#aio-content [data-citation-indexes]").evaluate_all("nodes => nodes.map(n => JSON.parse(n.dataset.citationIndexes))")
                self.assertEqual(actual_groups, list(citation_groups(row["aio_response"]["blocks"])))
                saved_text = page.locator("#aio-content").text_content()
                for snippet in snippets(row["aio_response"]["blocks"]):
                    self.assertIn(snippet, saved_text)
                # No guessed bold ranges for the colon-delimited plain text.
                self.assertEqual(page.locator("#aio-content strong").count(), 0)
                page.locator("#json-tab").click()
                self.assertEqual(json.loads(page.locator("#json-content").inner_text()), original)
                page.locator("#preview-tab").click()
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.locator("#sources .sources-toggle").click()
                self.assertEqual(page.locator("#sources .source-card:visible").count(), len(row["aio_response"]["references"]))
                page.locator("#sources .sources-toggle").click()
                self.assertEqual(page.locator("#sources .source-card:visible").count(), 3)
            changed = copy.deepcopy(ROW)
            changed["organic_results"][0]["sitelinks"]["expanded"] = [{"title": "More details", "link": "https://example.com/details", "snippet": "A complete expanded sitelink snippet"}]
            page.evaluate("row => renderResponse(row)", changed)
            self.assertEqual(page.locator(".sitelink-expanded p").inner_text(), "A complete expanded sitelink snippet")
            self.assertFalse(external)
            browser.close()


if __name__ == "__main__":
    unittest.main()
