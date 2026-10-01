import unittest

import aio_scraper as a


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

    def test_url_source_cards_keep_indexes_deduplication_and_csv_rows(self):
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
        rows = a._csv_rows(result, "P01")
        references = [r for r in rows if r["Type"] == "aio_reference"]
        self.assertEqual([r["Position"] for r in references], [1, 2])
        self.assertEqual(references[0]["Link"], overview["references"][0]["link"])
        self.assertEqual(rows[0]["Reference Indexes"], "0")
        self.assertEqual(rows[1]["Reference Indexes"], "1")
