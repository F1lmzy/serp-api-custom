import unittest

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

    def test_preserves_opaque_goto_links_in_ranked_organic_json(self):
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
