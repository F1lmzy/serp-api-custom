import contextlib
import csv
import io
import unittest
from unittest.mock import patch

import aio_scraper
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


class OrganicResultsTests(unittest.TestCase):
    def test_extracts_only_ranked_organic_cards_in_dom_order_without_an_overview(self):
        html = '''<div id="search"><div id="tads"><div class="g">
          <a href="https://ads.example/"><h3>Ad</h3></a></div></div>
          <div id="rso">
            <div id="eKIzJc"><div class="g"><a href="https://aio.example/"><h3>AI citation</h3></a></div></div>
            <div class="MjjYud"><div class="g">
              <a href="https://first.example/a"><h3>First result</h3></a>
              <div class="VwiC3b">The <b>first</b> snippet.</div>
              <a href="https://other.example/"><h3>Nested sitelink</h3></a>
            </div></div>
            <div class="MjjYud"><div class="g">
              <a href="/url?q=https%3A%2F%2Fsecond.example%2Fb%3Fx%3D1&amp;sa=U"><h3>Second result</h3></a>
              <div class="VwiC3b">Second snippet.</div>
            </div></div>
            <div class="MjjYud"><div class="g"><a href="/search?q=related"><h3>Related search</h3></a></div></div>
          </div></div>'''
        result = aio_scraper.parse_search_html(html, "a query", "en", "us")
        self.assertNotIn("ai_overview", result)
        self.assertEqual(result["organic_results"], [
            {"position": 1, "title": "First result", "link": "https://first.example/a", "snippet": "The first snippet."},
            {"position": 2, "title": "Second result", "link": "https://second.example/b?x=1", "snippet": "Second snippet."},
        ])

    def test_excludes_aio_cards_that_are_result_wrappers(self):
        for aio_markup in (
            '<div id="eKIzJc" class="g"><a href="https://aio.example/"><h3>AI citation</h3></a></div>',
            '<div class="g"><div id="eKIzJc"><a href="https://aio.example/"><h3>AI citation</h3></a></div></div>',
        ):
            with self.subTest(aio_markup=aio_markup):
                html = ('<div id="search"><div id="rso">' + aio_markup +
                        '<div class="g"><a href="//real.example/page"><h3>Real</h3></a>'
                        '<div class="VwiC3b">Snippet</div></div></div></div>')
                result = aio_scraper.parse_search_html(html, "q", "en", "us")
                self.assertEqual(result["organic_results"], [
                    {"position": 1, "title": "Real", "link": "https://real.example/page", "snippet": "Snippet"}
                ])

    def test_extracts_unclassed_organic_cards_and_ignores_aio_and_sitelinks(self):
        html = '''<div id="search"><div id="rso">
          <div class="MjjYud" id="eKIzJc">
            <a href="https://citation.example/"><h3>AI source</h3></a>
            <div data-subtree="aimfl,mfl"><p>Answer</p></div>
          </div>
          <div class="MjjYud" data-hveid="one">
            <a href="https://first.example/a"><h3>First result</h3></a>
            <div class="VwiC3b">First snippet.</div>
            <a href="https://first.example/deeper"><h3>Sitelink</h3></a>
          </div>
          <div class="MjjYud" data-hveid="two">
            <a href="/url?q=https%3A%2F%2Fsecond.example%2Fb"><h3>Second result</h3></a>
            <div class="IsZvec">Second snippet.</div>
          </div>
          <div class="MjjYud"><a href="/search?q=related"><h3>Search suggestion</h3></a></div>
        </div></div>'''
        result = aio_scraper.parse_search_html(html, "q", "en", "us")
        self.assertEqual(result["organic_results"], [
            {"position": 1, "title": "First result", "link": "https://first.example/a", "snippet": "First snippet."},
            {"position": 2, "title": "Second result", "link": "https://second.example/b", "snippet": "Second snippet."},
        ])

    def test_preserves_opaque_goto_links_in_ranked_organic_and_csv_rows(self):
        html = '''<div id="search"><div id="rso">
          <div class="MjjYud"><a href="/goto?url=CAESfirst&amp;source=web">
            <h3>First result</h3></a><div class="VwiC3b">First snippet.</div></div>
          <div class="MjjYud"><a href="https://second.example/"><h3>Second result</h3></a></div>
          <div class="MjjYud"><a href="https://www.google.com/goto?url=CAESthird">
            <h3>Third result</h3></a></div>
          <div class="MjjYud"><a href="/search?q=related"><h3>Related search</h3></a></div>
        </div></div>'''
        result = aio_scraper.parse_search_html(html, "q", "en", "us")
        self.assertEqual(result["organic_results"], [
            {"position": 1, "title": "First result", "link": "https://www.google.com/goto?url=CAESfirst&source=web", "snippet": "First snippet."},
            {"position": 2, "title": "Second result", "link": "https://second.example/", "snippet": ""},
            {"position": 3, "title": "Third result", "link": "https://www.google.com/goto?url=CAESthird", "snippet": ""},
        ])
        rows = aio_scraper._csv_rows(result, "P01")
        self.assertEqual([row["Type"] for row in rows], ["organic"] * 3)
        self.assertEqual(rows[0]["Link"], result["organic_results"][0]["link"])

    def test_goto_support_does_not_admit_unsafe_or_internal_google_links(self):
        for raw in ("javascript:alert(1)", "data:text/html,x", "/search?q=x",
                    "/goto", "/goto?url=", "/goto?other=x", "/goto/other?url=x",
                    "/url?q=javascript%3Aalert(1)", "https://www.google.com/maps"):
            with self.subTest(raw=raw):
                self.assertIsNone(aio_scraper._organic_url(raw))
        self.assertEqual(aio_scraper._organic_url("//google.com/goto?url=opaque"),
                         "https://google.com/goto?url=opaque")

    def test_overview_and_organic_results_are_both_returned(self):
        html = '''<div id="search"><div id="rso">
          <div id="eKIzJc"><div data-subtree="aimfl,mfl"><p>Answer</p></div></div>
          <div class="g"><a href="https://example.org/"><h3>Organic</h3></a>
          <div class="VwiC3b">Snippet</div></div>
        </div></div>'''
        result = aio_scraper.parse_search_html(html, "q", "en", "us")
        self.assertEqual(result["ai_overview"]["text_blocks"][0]["snippet"], "Answer")
        self.assertEqual(result["organic_results"][0]["title"], "Organic")


class NavigationDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_chromium_navigation_timeout_reports_configuration(self):
        class Page:
            async def goto(self, *args, **kwargs):
                raise PlaywrightTimeoutError("Page.goto: Timeout exceeded")

        scraper = aio_scraper.GoogleAIOScraper(engine="chromium", proxy={"server": "http://proxy.example:823"},
                                               timeout_ms=15000)
        with self.assertRaisesRegex(aio_scraper.ScrapeError, "navigation.*15000ms.*GOOGLE_AIO_BROWSER=firefox"):
            await scraper._search_once(Page(), "https://www.google.com/search?q=test", "test", "en", "us")


class CsvOutputTests(unittest.TestCase):
    def test_batch_writes_header_ordered_rows_and_query_ids_even_on_errors(self):
        class FakeScraper:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def search(self, query, hl, gl):
                if query == "bad":
                    raise aio_scraper.ScrapeError("failed")
                return {
                    "search_parameters": {"engine": "google", "q": query, "hl": hl, "gl": gl},
                    "ai_overview": {
                        "text_blocks": [
                            {"type": "heading", "snippet": "Head"},
                            {"type": "list", "list": [
                                {"snippet": "First item", "reference_indexes": [0]},
                                {"snippet": "Second item"},
                            ]},
                        ],
                        "references": [{"index": 0, "title": "Source", "link": "https://source.example/", "source": "Publisher"}],
                    },
                    "organic_results": [
                        {"position": 1, "title": "First", "link": "https://first.example/", "snippet": "One"},
                        {"position": 2, "title": "Second", "link": "https://second.example/", "snippet": "Two"},
                    ],
                } if query == "good" else {
                    "search_parameters": {"engine": "google", "q": query, "hl": hl, "gl": gl},
                    "organic_results": [{"position": 1, "title": "Only", "link": "https://only.example/", "snippet": "Text"}],
                }

        output = io.StringIO()
        with patch.object(aio_scraper, "GoogleAIOScraper", FakeScraper), contextlib.redirect_stderr(io.StringIO()):
            status = __import__("asyncio").run(aio_scraper._run_batch(["good", "plain", "bad"], "en", "us", 1000, 0, output))
        rows = list(csv.DictReader(io.StringIO(output.getvalue())))
        self.assertEqual(status, 1)
        self.assertEqual(list(rows[0])[0], "Query")
        self.assertEqual([row["Query"] for row in rows], ["P01"] * 6 + ["P02", "P03"])
        self.assertEqual([row["Type"] for row in rows],
                         ["aio_heading", "aio_list_item", "aio_list_item", "aio_reference", "organic", "organic", "organic", "error"])
        self.assertEqual([row["Position"] for row in rows], ["1", "2.1", "2.2", "1", "1", "2", "1", ""])
        self.assertEqual(rows[1]["Reference Indexes"], "0")
        self.assertEqual(rows[5]["Link"], "https://second.example/")
        self.assertEqual(rows[7]["Error Code"], "SCRAPE_ERROR")
        self.assertEqual(rows[7]["Search Query"], "bad")

    def test_csv_escapes_spreadsheet_formulas_from_search_content(self):
        result = {"search_parameters": {"q": "=1+1", "hl": "en", "gl": "us"},
                  "organic_results": [{"position": 1, "title": "+SUM(1,2)",
                                       "link": "https://example.com/", "snippet": "@cmd"}]}
        row = aio_scraper._csv_rows(result, "P01")[0]
        self.assertEqual(row["Search Query"], "'=1+1")
        self.assertEqual(row["Title"], "'+SUM(1,2)")
        self.assertEqual(row["Snippet"], "'@cmd")

    def test_empty_serp_still_has_a_query_row(self):
        rows = aio_scraper._csv_rows({"search_parameters": {"q": "empty", "hl": "en", "gl": "us"}}, "P01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Query"], "P01")
        self.assertEqual(rows[0]["Type"], "no_results")
