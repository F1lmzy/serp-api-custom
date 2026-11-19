import unittest
from types import SimpleNamespace

import aio_scraper as a
import redirect_resolver as rr


class ReferenceUrlTests(unittest.TestCase):
    def test_preserves_relative_and_absolute_opaque_google_url_redirects(self):
        relative = "/url?sa=i&source=web&url=CAESopaque-token&ved=test"
        for raw in (relative, a.GOOGLE + relative):
            with self.subTest(raw=raw):
                self.assertEqual(a._safe_reference_url(raw), a.GOOGLE + relative)

    def test_decodes_plain_http_destinations_from_q_or_url(self):
        for key in ("q", "url"):
            for prefix in ("", a.GOOGLE):
                raw = prefix + "/url?" + key + "=https%3A%2F%2Fsource.example%2Farticle%3Fx%3D1%26y%3D2&sa=i"
                with self.subTest(raw=raw):
                    self.assertEqual(a._safe_reference_url(raw), "https://source.example/article?x=1&y=2")

    def test_rejects_unsafe_or_missing_redirect_destinations(self):
        for raw in ("/url", "/url?sa=i", "/url?url=", "/url?url=javascript%3Aalert(1)",
                    "/url?q=data%3Atext%2Fhtml%2Cevil", "/url?url=%2Fsearch%3Fq%3Dx",
                    "https://www.google.com/url?url=javascript%3Aalert(1)",
                    "javascript:alert(1)", "data:text/html,evil", "/search?q=x"):
            with self.subTest(raw=raw):
                self.assertIsNone(a._safe_reference_url(raw))

    def test_existing_goto_and_direct_links_still_work(self):
        self.assertEqual(a._safe_reference_url("/goto?url=opaque"), a.GOOGLE + "/goto?url=opaque")
        self.assertEqual(a._safe_reference_url("https://source.example/article"), "https://source.example/article")


class ReferenceExtractionTests(unittest.TestCase):
    def test_external_query_parameters_remain_distinct_reference_keys(self):
        self.assertNotEqual(a._reference_key("https://source.example/article?id=1"),
                            a._reference_key("https://source.example/article?id=2"))
        self.assertEqual(a._reference_key("/url?q=https%3A%2F%2Fsource.example%2Farticle&ved=inline"),
                         a._reference_key("https://source.example/article"))
        self.assertIsNone(a._reference_key("javascript:alert(1)"))

    def test_url_source_cards_keep_indexes_and_deduplication_in_json(self):
        html = '''<div id="search"><div id="rso"><div id="eKIzJc">
          <div data-subtree="aimc"><div jsname="KFl8ub">
            <div>First fact <a href="/url?sa=i&amp;url=CAESone&amp;ved=inline" aria-label="Publisher"></a></div>
            <ul><li>Second fact <a href="/url?q=https%3A%2F%2Fsecond.example%2Farticle" aria-label="Second publisher"></a></li></ul>
          </div></div>
          <div data-src-id="1"><a href="/url?sa=i&amp;url=CAESone&amp;ved=test" aria-label="Article - Publisher. Opens in new tab."></a></div>
          <div data-src-id="2"><a href="/url?q=https%3A%2F%2Fsecond.example%2Farticle" aria-label="Other article | Second publisher. Opens in new tab."></a></div>
          <div data-src-id="3"><a href="/url?sa=i&amp;url=CAESone&amp;ved=duplicate" aria-label="Article - Publisher. Opens in new tab."></a></div>
        </div></div></div>'''
        result = a.parse_search_html(html, "a query", "en", "us")
        overview = result["ai_overview"]
        self.assertEqual(overview["references"], [
            {"index": 0, "title": "Article - Publisher", "source": "Publisher", "link": a.GOOGLE + "/url?sa=i&url=CAESone&ved=test"},
            {"index": 1, "title": "Other article | Second publisher", "source": "Second publisher", "link": "https://second.example/article"},
        ])
        self.assertEqual(overview["text_blocks"][0]["reference_indexes"], [0])
        self.assertEqual(overview["text_blocks"][1]["list"][0]["reference_indexes"], [1])
        from comparison_storage import normalize_native_snapshot
        query = normalize_native_snapshot({
            "run_id": "00000000-0000-0000-0000-000000000001", "captured_at": "2026-01-01T00:00:00Z",
            "results": [{"query_id": "P01", "result": result}]})["queries"][0]
        references = query["aio_response"]["references"]
        self.assertEqual([r["index"] for r in references], [0, 1])
        self.assertEqual(references[0]["link"], overview["references"][0]["link"])


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


def _resolution_page(request_context, url="https://www.google.com/search?q=a+query"):
    page = SimpleNamespace(url=url)
    page.context = SimpleNamespace(request=request_context)
    return page


OPAQUE_OVERVIEW_HTML = '''<div id="search"><div id="rso"><div id="eKIzJc">
  <div data-subtree="aimc"><div jsname="KFl8ub">
    <div>Fact <a href="/url?sa=i&amp;url=CAESone&amp;ved=inline" aria-label="Publisher"></a></div>
  </div></div>
  <div data-src-id="1"><a href="/url?sa=i&amp;url=CAESone&amp;ved=test" aria-label="Article - Publisher. Opens in new tab."></a></div>
  <div data-src-id="2"><a href="/url?q=https%3A%2F%2Fsecond.example%2Farticle" aria-label="Other article | Second publisher. Opens in new tab."></a></div>
</div></div></div>'''

CARD_LINK = a.GOOGLE + "/url?sa=i&url=CAESone&ved=test"
INLINE_LINK = a.GOOGLE + "/url?sa=i&url=CAESone&ved=inline"
CARD_DESTINATION = "https://publisher.example/article?id=7&utm_source=google"
INLINE_DESTINATION = "https://publisher.example/inline-fact"


class _FlippingRequestContext:
    """First request per link serves an interstitial; the retry observes the redirect."""

    def __init__(self, destinations):
        self.destinations = dict(destinations)
        self.seen = {}
        self.requests = 0

    async def get(self, url, **kwargs):
        self.requests += 1
        self.seen[url] = self.seen.get(url, 0) + 1
        if self.seen[url] == 1:
            return _RedirectResponse(200)
        return _RedirectResponse(302, self.destinations[url])


class RedirectResolutionTests(unittest.IsolatedAsyncioTestCase):
    def _parsed_overview(self):
        return a.parse_search_html(OPAQUE_OVERVIEW_HTML, "a query", "en", "us")

    async def test_resolved_links_carry_originals_and_keep_indexes(self):
        result = self._parsed_overview()
        context = _RedirectRequestContext({
            CARD_LINK: _RedirectResponse(302, CARD_DESTINATION),
            INLINE_LINK: _RedirectResponse(301, INLINE_DESTINATION)})
        await rr.resolve_redirect_links(result, _resolution_page(context))
        references = result["ai_overview"]["references"]
        self.assertEqual(references[0]["link"], CARD_DESTINATION)
        self.assertEqual(references[0]["redirect_link"], CARD_LINK)
        self.assertEqual(references[1]["link"], "https://second.example/article")
        self.assertNotIn("redirect_link", references[1])
        self.assertEqual([r["index"] for r in references], [0, 1])
        snippet = result["ai_overview"]["text_blocks"][0]["snippet_links"][0]
        self.assertEqual(snippet["link"], INLINE_DESTINATION)
        self.assertEqual(snippet["redirect_link"], INLINE_LINK)
        self.assertEqual(result["ai_overview"]["text_blocks"][0]["reference_indexes"], [0])
        self.assertEqual(result["extraction_metadata"]["redirect_resolution"],
                         {"attempted": 2, "resolved": 2})
        self.assertEqual(len(context.requests), 2)
        for url, kwargs in context.requests:
            self.assertEqual(kwargs["max_redirects"], 0)
            self.assertEqual(kwargs["headers"], {"Referer": "https://www.google.com/search?q=a+query"})

    async def test_non_redirect_or_failing_responses_keep_links_opaque(self):
        for name, outcome in (
                ("interstitial page", _RedirectResponse(200)),
                ("redirect without location", _RedirectResponse(302)),
                ("request failure", TimeoutError("link fetch timed out"))):
            with self.subTest(outcome=name):
                result = self._parsed_overview()
                context = _RedirectRequestContext({CARD_LINK: outcome, INLINE_LINK: outcome})
                await rr.resolve_redirect_links(result, _resolution_page(context), settle_seconds=0.0)
                self.assertEqual(len(context.requests), 4)  # exactly two observations per link
                references = result["ai_overview"]["references"]
                self.assertEqual(references[0]["link"], CARD_LINK)
                self.assertNotIn("redirect_link", references[0])
                self.assertEqual(result["extraction_metadata"]["redirect_resolution"],
                                 {"attempted": 2, "resolved": 0})

    async def test_settle_retry_resolves_links_that_observed_interstitials_first(self):
        result = self._parsed_overview()
        context = _FlippingRequestContext({CARD_LINK: CARD_DESTINATION,
                                            INLINE_LINK: INLINE_DESTINATION})
        await rr.resolve_redirect_links(result, _resolution_page(context), settle_seconds=0.0)
        self.assertEqual(context.requests, 4)  # interstitial, then the settled observation
        references = result["ai_overview"]["references"]
        self.assertEqual(references[0]["link"], CARD_DESTINATION)
        self.assertEqual(references[0]["redirect_link"], CARD_LINK)
        snippet = result["ai_overview"]["text_blocks"][0]["snippet_links"][0]
        self.assertEqual(snippet["link"], INLINE_DESTINATION)
        self.assertEqual(result["extraction_metadata"]["redirect_resolution"],
                         {"attempted": 2, "resolved": 2})

    async def test_settle_retry_is_skipped_when_the_budget_cannot_fit_it(self):
        result = self._parsed_overview()
        context = _FlippingRequestContext({CARD_LINK: CARD_DESTINATION,
                                            INLINE_LINK: INLINE_DESTINATION})
        await rr.resolve_redirect_links(result, _resolution_page(context),
                                        budget_seconds=0.5, settle_seconds=30.0)
        self.assertEqual(context.requests, 2)  # only the first sweep fit
        self.assertEqual(result["extraction_metadata"]["redirect_resolution"],
                         {"attempted": 2, "resolved": 0})

    async def test_unsafe_or_google_locations_are_rejected(self):
        for location in ("javascript:alert(1)", "data:text/html,x",
                         "https://www.google.com/search?q=x", "/search?q=internal"):
            with self.subTest(location=location):
                result = self._parsed_overview()
                context = _RedirectRequestContext({
                    CARD_LINK: _RedirectResponse(302, location),
                    INLINE_LINK: _RedirectResponse(302, location)})
                await rr.resolve_redirect_links(result, _resolution_page(context), settle_seconds=0.0)
                self.assertEqual(len(context.requests), 4)
                self.assertEqual(result["ai_overview"]["references"][0]["link"], CARD_LINK)
                self.assertNotIn("redirect_link", result["ai_overview"]["references"][0])
                self.assertEqual(result["extraction_metadata"]["redirect_resolution"],
                                 {"attempted": 2, "resolved": 0})

    async def test_step_failures_keep_links_and_never_raise(self):
        for name, page in (
                ("every request fails", _resolution_page(_RedirectRequestContext(
                    {CARD_LINK: TypeError("boom"), INLINE_LINK: TypeError("boom")}))),
                ("page exposes no request context", SimpleNamespace(url="https://www.google.com/search?q=q"))):
            with self.subTest(failure=name):
                result = self._parsed_overview()
                await rr.resolve_redirect_links(result, page, settle_seconds=0.0)
                references = result["ai_overview"]["references"]
                self.assertEqual(references[0]["link"], CARD_LINK)
                self.assertNotIn("redirect_link", references[0])
                self.assertEqual(result["extraction_metadata"]["redirect_resolution"],
                                 {"attempted": 2, "resolved": 0})

    async def test_link_cap_leaves_extra_links_unresolved(self):
        result = self._parsed_overview()
        context = _RedirectRequestContext({
            CARD_LINK: _RedirectResponse(302, CARD_DESTINATION),
            INLINE_LINK: _RedirectResponse(302, INLINE_DESTINATION)})
        await rr.resolve_redirect_links(result, _resolution_page(context), max_links=1)
        self.assertEqual(len(context.requests), 1)
        self.assertEqual(result["ai_overview"]["references"][0]["link"], CARD_DESTINATION)
        self.assertEqual(result["extraction_metadata"]["redirect_resolution"],
                         {"attempted": 1, "resolved": 1})
        snippet = result["ai_overview"]["text_blocks"][0]["snippet_links"][0]
        self.assertEqual(snippet["link"], INLINE_LINK)
        self.assertNotIn("redirect_link", snippet)

    async def test_responses_without_opaque_links_are_left_untouched(self):
        html = OPAQUE_OVERVIEW_HTML.replace("CAESone", "https%3A%2F%2Fplain.example%2Ffact")
        result = a.parse_search_html(html, "a query", "en", "us")
        context = _RedirectRequestContext({})
        await rr.resolve_redirect_links(result, _resolution_page(context))
        self.assertEqual(context.requests, [])
        self.assertNotIn("redirect_resolution", result["extraction_metadata"])
        references = result["ai_overview"]["references"]
        self.assertEqual(references[0]["link"], "https://plain.example/fact")
        self.assertNotIn("redirect_link", references[0])

    async def test_identical_links_resolve_once_across_cards_and_citations(self):
        html = OPAQUE_OVERVIEW_HTML.replace("ved=inline", "ved=test")
        result = a.parse_search_html(html, "a query", "en", "us")
        context = _RedirectRequestContext({CARD_LINK: _RedirectResponse(302, CARD_DESTINATION)})
        await rr.resolve_redirect_links(result, _resolution_page(context))
        self.assertEqual(len(context.requests), 1)
        references = result["ai_overview"]["references"]
        self.assertEqual(references[0]["link"], CARD_DESTINATION)
        self.assertEqual(references[0]["redirect_link"], CARD_LINK)
        snippet = result["ai_overview"]["text_blocks"][0]["snippet_links"][0]
        self.assertEqual(snippet["link"], CARD_DESTINATION)
        self.assertEqual(snippet["redirect_link"], CARD_LINK)
        self.assertEqual(result["extraction_metadata"]["redirect_resolution"],
                         {"attempted": 1, "resolved": 1})

    async def test_resolved_content_survives_native_snapshot_projection(self):
        result = self._parsed_overview()
        context = _RedirectRequestContext({
            CARD_LINK: _RedirectResponse(302, CARD_DESTINATION),
            INLINE_LINK: _RedirectResponse(301, INLINE_DESTINATION)})
        await rr.resolve_redirect_links(result, _resolution_page(context))
        from comparison_storage import normalize_native_snapshot
        query = normalize_native_snapshot({
            "run_id": "00000000-0000-0000-0000-000000000002", "captured_at": "2026-01-01T00:00:00Z",
            "results": [{"query_id": "P01", "result": result}]})["queries"][0]
        self.assertEqual(query["aio_response"]["references"][0]["link"], CARD_DESTINATION)
        snippet = query["aio_response"]["blocks"][0]["snippet_links"][0]
        self.assertEqual(snippet["link"], INLINE_DESTINATION)
        self.assertEqual(snippet["redirect_link"], INLINE_LINK)
        self.assertFalse(any(warning.startswith("unknown_aio_content_field")
                             for warning in query["warnings"]))
