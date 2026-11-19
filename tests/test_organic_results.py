import unittest
from types import SimpleNamespace
from unittest.mock import patch

import aio_scraper
import redirect_resolver
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


GOTO_ORGANIC_HTML = '''<div id="search"><div id="rso">
  <div class="MjjYud"><a href="/goto?url=CAESfirst&amp;source=web">
    <h3>First result</h3></a><div class="VwiC3b">First snippet.</div></div>
  <div class="MjjYud"><a href="https://second.example/"><h3>Second result</h3></a></div>
  <div class="MjjYud"><a href="https://www.google.com/goto?url=CAESthird">
    <h3>Third result</h3></a></div>
  <div class="MjjYud"><a href="/search?q=related"><h3>Related search</h3></a></div>
</div></div>'''

FIRST_GOTO = "https://www.google.com/goto?url=CAESfirst&source=web"
THIRD_GOTO = "https://www.google.com/goto?url=CAESthird"


class _RedirectResponse:
    """Minimal APIResponse stand-in: a status plus a Location header."""

    def __init__(self, status, location=None):
        self.status = status
        self.headers = {"location": location} if location else {}


class _RedirectRequestContext:
    """One recorded request per link, replaying canned per-link outcomes."""

    def __init__(self, outcomes):
        self.outcomes = dict(outcomes)
        self.requests = []

    async def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        outcome = self.outcomes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _resolution_page(request_context, url="https://www.google.com/search?q=q"):
    page = SimpleNamespace(url=url)
    page.context = SimpleNamespace(request=request_context)
    return page


class OrganicRedirectResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolved_goto_links_keep_order_and_originals(self):
        result = aio_scraper.parse_search_html(GOTO_ORGANIC_HTML, "q", "en", "us")
        context = _RedirectRequestContext({
            FIRST_GOTO: _RedirectResponse(302, "https://first.example/article?from=google"),
            THIRD_GOTO: _RedirectResponse(301, "https://third.example/page")})
        await redirect_resolver.resolve_redirect_links(result, _resolution_page(context))
        organic = result["organic_results"]
        self.assertEqual([item["position"] for item in organic], [1, 2, 3])
        self.assertEqual(organic[0]["snippet"], "First snippet.")
        self.assertEqual(organic[0]["link"], "https://first.example/article?from=google")
        self.assertEqual(organic[0]["redirect_link"], FIRST_GOTO)
        self.assertEqual(organic[1]["link"], "https://second.example/")
        self.assertNotIn("redirect_link", organic[1])
        self.assertEqual(organic[2]["link"], "https://third.example/page")
        self.assertEqual(organic[2]["redirect_link"], THIRD_GOTO)
        self.assertEqual(result["extraction_metadata"]["redirect_resolution"],
                         {"attempted": 2, "resolved": 2})

    async def test_sitelinks_resolve_with_the_same_observed_map(self):
        html = '''<div id="search"><div id="rso">
          <div class="MjjYud"><a href="/goto?url=CAESmain"><h3>Main result</h3></a>
            <div class="VwiC3b">Main snippet.</div>
            <div class="sitelinks"><a href="/goto?url=CAESsub">Sub page</a></div>
          </div>
        </div></div>'''
        result = aio_scraper.parse_search_html(html, "q", "en", "us")
        context = _RedirectRequestContext({
            "https://www.google.com/goto?url=CAESmain": _RedirectResponse(302, "https://main.example/page"),
            "https://www.google.com/goto?url=CAESsub": _RedirectResponse(302, "https://main.example/sub")})
        await redirect_resolver.resolve_redirect_links(result, _resolution_page(context))
        item = result["organic_results"][0]
        self.assertEqual(item["link"], "https://main.example/page")
        sitelink = item["sitelinks"]["inline"][0]
        self.assertEqual(sitelink["title"], "Sub page")
        self.assertEqual(sitelink["link"], "https://main.example/sub")
        self.assertEqual(sitelink["redirect_link"], "https://www.google.com/goto?url=CAESsub")

    async def test_unresolved_organic_links_stay_opaque_and_counted(self):
        result = aio_scraper.parse_search_html(GOTO_ORGANIC_HTML, "q", "en", "us")
        context = _RedirectRequestContext({
            FIRST_GOTO: _RedirectResponse(200),
            THIRD_GOTO: TimeoutError("no answer")})
        await redirect_resolver.resolve_redirect_links(result, _resolution_page(context), settle_seconds=0.0)
        self.assertEqual(len(context.requests), 4)  # exactly two observations per link
        organic = result["organic_results"]
        self.assertEqual(organic[0]["link"], FIRST_GOTO)
        self.assertNotIn("redirect_link", organic[0])
        self.assertEqual(organic[2]["link"], THIRD_GOTO)
        self.assertNotIn("redirect_link", organic[2])
        self.assertEqual(result["extraction_metadata"]["redirect_resolution"],
                         {"attempted": 2, "resolved": 0})

    async def test_settle_retry_resolves_organic_links_after_the_interstitial_window(self):
        result = aio_scraper.parse_search_html(GOTO_ORGANIC_HTML, "q", "en", "us")
        outcomes = {FIRST_GOTO: _RedirectResponse(200), THIRD_GOTO: _RedirectResponse(200)}
        destinations = {FIRST_GOTO: "https://first.example/article?from=google",
                        THIRD_GOTO: "https://third.example/page"}
        seen = {}

        class FlippingContext(_RedirectRequestContext):
            async def get(self, url, **kwargs):
                await super().get(url, **kwargs)
                seen[url] = seen.get(url, 0) + 1
                if seen[url] == 1:
                    return outcomes[url]
                return _RedirectResponse(302, destinations[url])

        context = FlippingContext(outcomes)
        await redirect_resolver.resolve_redirect_links(result, _resolution_page(context), settle_seconds=0.0)
        self.assertEqual(len(context.requests), 4)
        organic = result["organic_results"]
        self.assertEqual(organic[0]["link"], "https://first.example/article?from=google")
        self.assertEqual(organic[0]["redirect_link"], FIRST_GOTO)
        self.assertEqual(organic[2]["link"], "https://third.example/page")
        self.assertEqual(result["extraction_metadata"]["redirect_resolution"],
                         {"attempted": 2, "resolved": 2})


class SearchResolutionWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_resolves_redirects_once_after_the_final_attempt(self):
        scraper = aio_scraper.GoogleAIOScraper(engine="chromium",
                                               proxy={"server": "http://proxy.example:823"}, timeout_ms=1000)
        unresolved = {"search_parameters": {"engine": "google", "q": "q", "hl": "en", "gl": "us"},
                      "extraction_metadata": {"aio_status": "absent"}}
        present = {"search_parameters": {"engine": "google", "q": "q", "hl": "en", "gl": "us"},
                   "extraction_metadata": {"aio_status": "present", "organic_count": 0},
                   "ai_overview": {"text_blocks": [{"type": "paragraph", "snippet": "Answer"}],
                                   "references": []}}
        attempts = [aio_scraper._SearchAttempt(unresolved, True), aio_scraper._SearchAttempt(present, True)]

        async def fake_attempt(page, url, query, hl, gl, poll_ms=20000):
            return attempts.pop(0)

        resolved = []

        async def fake_resolve(response, page):
            resolved.append(response)

        scraper._search_attempt = fake_attempt
        with patch.object(aio_scraper, "resolve_redirect_links", fake_resolve):
            result = await scraper.search("q")
        self.assertIs(result, present)
        self.assertEqual(resolved, [present])
