import unittest
from unittest.mock import patch

import aio_scraper
from aio_scraper import ScrapeError, parse_search_html


class ParseSearchHtmlTests(unittest.TestCase):
    def test_parses_current_google_answer_marker_and_excludes_source_chips(self):
        html = '''
        <html><body><div id="search"><div id="rso"><div id="eKIzJc">
          <div data-subtree="aimc"><div jsname="KFl8ub">
            <div class="n6owBd">
              <span data-subtree="aimfl">Mount Everest has an official height of </span>
              <span>8,848.86 meters (29,031.7 feet) above sea level.</span>
              <a aria-label="Wikipedia" href="/goto?url=everest-source"></a>
              <button aria-label="Wikipedia (+2) - Related results">Wikipedia</button>
            </div>
            <div role="heading">Key Details</div>
            <ul><li>Latest Measurement: jointly announced in 2020</li><li>Above sea level</li></ul>
            <div>If you'd like, I can share information about:<ul><li>How the height was measured</li><li>Other mountains</li></ul></div>
          </div></div>
          <div data-src-id="1"><a href="/goto?url=everest-source" aria-label="Mount Everest - Wikipedia. Opens in new tab."></a></div>
        </div></div></div></body></html>'''
        result = parse_search_html(html, "how tall is mount everest", "en", "us")
        aio = result["ai_overview"]
        self.assertIn("8,848.86 meters", aio["text_blocks"][0]["snippet"])
        self.assertNotIn("Wikipedia", aio["text_blocks"][0]["snippet"])
        self.assertEqual(aio["text_blocks"][0]["reference_indexes"], [0])
        self.assertEqual(aio["text_blocks"][1], {"type": "heading", "snippet": "Key Details"})
        self.assertEqual(aio["text_blocks"][2]["type"], "list")
        self.assertNotIn("How the height was measured", str(aio["text_blocks"]))

    def test_parses_overview_text_sources_and_ordered_blocks(self):
        html = '''
        <html><body><div id="search"><div id="rso">
          <div id="eKIzJc">
            <div role="heading">AI Overview</div>
            <span jsname="lGsj1">An AI Overview is not available for this search</span>
            <div data-subtree="aimfl,mfl">
              <div role="heading">Quick answer</div>
              <p>Everest is 8,848.86 meters tall. <a href="/goto?url=token-one" aria-label="Wikipedia">citation</a></p>
              <ul><li>Latest measurement</li><li>Above sea level</li></ul>
            </div>
            <div data-src-id="1"><a href="/goto?url=token-one" aria-label="Mount Everest - Wikipedia. Opens in new tab.">Mount Everest - Wikipedia</a></div>
            <div data-src-id="2"><a href="https://www.britannica.com/place/Mount-Everest" aria-label="Mount Everest | Britannica. Opens in new tab.">Mount Everest | Britannica</a></div>
          </div>
        </div></div></body></html>'''
        result = parse_search_html(html, query="height Everest", hl="en", gl="us")

        self.assertEqual(result["search_parameters"], {"engine": "google", "q": "height Everest", "hl": "en", "gl": "us"})
        aio = result["ai_overview"]
        self.assertEqual(aio["text_blocks"][0], {"type": "heading", "snippet": "Quick answer"})
        self.assertTrue(aio["text_blocks"][1]["snippet"].startswith("Everest is 8,848.86 meters tall."))
        self.assertEqual(aio["text_blocks"][2]["type"], "list")
        self.assertNotIn("not available", str(aio).lower())
        self.assertEqual(aio["references"][0]["index"], 0)
        self.assertEqual(aio["references"][0]["source"], "Wikipedia")
        self.assertEqual(aio["references"][0]["link"], "https://www.google.com/goto?url=token-one")
        self.assertEqual(aio["text_blocks"][1]["reference_indexes"], [0])
        self.assertEqual(aio["references"][1]["index"], 1)
        self.assertEqual(aio["references"][1]["link"], "https://www.britannica.com/place/Mount-Everest")

    def test_list_item_citations_map_to_reference_indexes(self):
        html = '''
        <div id="search"><div id="eKIzJc">
          <div data-subtree="aimc"><div jsname="KFl8ub">
            <ul><li>Answer <a href="/goto?url=source">source</a></li></ul>
          </div></div>
          <div data-src-id="1"><a href="/goto?url=source" aria-label="Source - Example. Opens in new tab."></a></div>
        </div></div>'''
        result = parse_search_html(html, "q", "en", "us")
        self.assertEqual(result["ai_overview"]["text_blocks"][0]["list"][0]["reference_indexes"], [0])

    def test_second_final_answer_list_is_preserved(self):
        # A real second answer list must not be dropped just because it is the
        # last visible node and follows another list.
        html = '''
        <div id="search"><div id="eKIzJc">
          <div data-subtree="aimc"><div jsname="KFl8ub">
            <div role="heading">Key Details</div>
            <ul><li>First list item</li></ul>
            <div>Additional details:<ul><li>Second list item A</li><li>Second list item B</li></ul></div>
          </div></div>
        </div></div>'''
        result = parse_search_html(html, "q", "en", "us")
        text = str(result["ai_overview"]["text_blocks"])
        self.assertIn("First list item", text)
        self.assertIn("Second list item A", text)
        self.assertIn("Second list item B", text)

    def test_two_consecutive_answer_lists_are_both_preserved(self):
        html = '''
        <div id="search"><div id="eKIzJc">
          <div data-subtree="aimc"><div jsname="KFl8ub">
            <ul><li>First list item</li></ul>
            <ul><li>Second list item</li></ul>
          </div></div>
        </div></div>'''
        result = parse_search_html(html, "q", "en", "us")
        text = str(result["ai_overview"]["text_blocks"])
        self.assertIn("First list item", text)
        self.assertIn("Second list item", text)

    def test_english_followup_prompt_is_excluded(self):
        html = '''
        <div id="search"><div id="eKIzJc">
          <div data-subtree="aimc"><div jsname="KFl8ub">
            <ul><li>Evidence-backed answer detail</li></ul>
            <div>If you'd like, I can share information about:<ul><li>How the height was measured</li></ul></div>
          </div></div>
        </div></div>'''
        result = parse_search_html(html, "q", "en", "us")
        text = str(result["ai_overview"]["text_blocks"])
        self.assertIn("Evidence-backed answer detail", text)
        self.assertNotIn("If you'd like", text)
        self.assertNotIn("How the height was measured", text)

    def test_proxy_url_credentials_are_percent_decoded(self):
        # urlparse leaves userinfo percent-encoded; Playwright needs the
        # literal credentials ("user@name" / "pa:ss").
        from aio_scraper import _proxy_settings

        encoded = "http://user%40name:pa%3Ass@proxy.example:823"
        with patch.dict("os.environ", {"DATAIMPULSE_PROXY_URL": encoded}, clear=True):
            settings = _proxy_settings("dataimpulse")
        self.assertEqual(settings["server"], "http://proxy.example:823")
        self.assertEqual(settings["username"], "user@name")
        self.assertEqual(settings["password"], "pa:ss")

    def test_engine_cli_uses_the_shared_json_capture_controller(self):
        import run_supabase_batch
        with patch.object(run_supabase_batch, "main", return_value=0) as controller:
            self.assertEqual(aio_scraper.main(["coffee", "--out-prefix", "capture"]), 0)
        controller.assert_called_once_with(["coffee", "--out-prefix", "capture"])

    def test_proxy_url_with_invalid_port_returns_safe_scrape_error(self):
        from unittest.mock import patch
        from aio_scraper import _proxy_settings

        with patch.dict("os.environ", {"DATAIMPULSE_PROXY_URL": "http://user:secret@proxy.example:bad"}, clear=True):
            with self.assertRaisesRegex(ScrapeError, "DATAIMPULSE_PROXY_URL"):
                _proxy_settings("dataimpulse")

    def test_no_overview_requires_a_real_search_results_page(self):
        self.assertNotIn("ai_overview", parse_search_html('<div id="search"><div id="rso"></div></div>', "x", "en", "us"))

    def test_challenge_page_is_an_error_not_no_overview(self):
        with self.assertRaisesRegex(ScrapeError, "blocked"):
            parse_search_html('<html><body>Our systems have detected unusual traffic</body></html>', "x", "en", "us")

    def test_javascript_shell_is_an_error_not_no_overview(self):
        with self.assertRaisesRegex(ScrapeError, "incomplete"):
            parse_search_html('<html><body><noscript>Enable JavaScript</noscript></body></html>', "x", "en", "us")

    def test_aio_not_available_message_is_no_overview(self):
        html = '<div id="search"><div id="rso"><div id="eKIzJc"><span jsname="lGsj1">An AI Overview is not available for this search</span></div></div></div>'
        self.assertNotIn("ai_overview", parse_search_html(html, "x", "en", "us"))

    def test_unloaded_aio_shell_is_no_overview_not_error(self):
        html = '<div id="search"><div id="rso"><div id="eKIzJc"></div></div></div>'
        self.assertNotIn("ai_overview", parse_search_html(html, "x", "en", "us"))


if __name__ == "__main__":
    unittest.main()
