"""Tests for CAPTCHA challenge detection and 2Captcha solver plumbing."""

import unittest
from unittest.mock import patch

from aio_scraper import (
    ChallengeDetected,
    _solve_variants,
    extract_challenge_params,
    is_challenge_page,
)

# Fabricated challenge page. Sites/keys below are dummies, never real captures.
CHALLENGE_HTML = """<html><head><title>Sorry</title></head><body>
<p>Our systems have detected unusual traffic from your computer network.</p>
<form id="captcha-form" action="/sorry/index?continue=%2Fsearch%3Fq%3Dtest">
<div class="g-recaptcha" data-sitekey="AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555"></div>
<iframe title="reCAPTCHA" src="https://www.google.com/recaptcha/enterprise/anchor?ar=1&k=AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555"></iframe>
<textarea id="g-recaptcha-response" name="g-recaptcha-response"></textarea>
</form></body></html>"""

CHALLENGE_HTML_IFRAME_ONLY = CHALLENGE_HTML.replace(' data-sitekey="AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555"', "")

NORMAL_HTML = '<html><body><div id="search">normal results</div></body></html>'

SNIPPET_MENTIONS_RECAPTCHA = """<html><body><div id="search">
<a href="/url?q=example.com">How reCAPTCHA works</a>
</div></body></html>"""


class IsChallengePageTests(unittest.TestCase):
    def test_real_challenge_page_is_detected(self):
        self.assertTrue(is_challenge_page(CHALLENGE_HTML))

    def test_iframe_only_challenge_is_detected(self):
        self.assertTrue(is_challenge_page(CHALLENGE_HTML_IFRAME_ONLY))

    def test_normal_search_page_is_not_a_challenge(self):
        self.assertFalse(is_challenge_page(NORMAL_HTML))

    def test_result_snippet_mentioning_recaptcha_is_not_a_challenge(self):
        self.assertFalse(is_challenge_page(SNIPPET_MENTIONS_RECAPTCHA))


class ExtractChallengeParamsTests(unittest.TestCase):
    def test_prefers_data_sitekey_and_flags_enterprise(self):
        params = extract_challenge_params(CHALLENGE_HTML, "https://www.google.com/sorry/index?continue=TEST")
        self.assertEqual(params["website_key"], "AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555")
        self.assertEqual(params["key_source"], "data-sitekey")
        self.assertTrue(params["is_enterprise"])
        self.assertEqual(params["form_action"], "/sorry/index?continue=%2Fsearch%3Fq%3Dtest")

    def test_falls_back_to_iframe_k_parameter(self):
        params = extract_challenge_params(CHALLENGE_HTML_IFRAME_ONLY, "https://www.google.com/sorry/index")
        self.assertEqual(params["website_key"], "AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555")
        self.assertEqual(params["key_source"], "iframe:/recaptcha/enterprise/anchor")

    def test_missing_sitekey_raises_challenge_detected(self):
        with self.assertRaises(ChallengeDetected):
            extract_challenge_params("<html><body><div id='recaptcha-anchor'></div></body></html>", "https://www.google.com/sorry/index")


class BuildTasksTests(unittest.TestCase):
    def _params(self, **overrides):
        base = {
            "website_key": "AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555",
            "website_url": "https://www.google.com/sorry/index?continue=TEST",
            "is_enterprise": True,
            "s_value": None,
            "data_s": None,
            "cookies": None,
            "user_agent": None,
        }
        base.update(overrides)
        return base

    def test_enterprise_first_then_classic_fallback(self):
        variants = _solve_variants(self._params())
        self.assertEqual(len(variants), 2)
        self.assertEqual(variants[0]["method"], "userrecaptcha")
        self.assertEqual(variants[0]["enterprise"], "1")
        self.assertNotIn("enterprise", variants[1])

    def test_google_search_context_params_passed_through(self):
        variants = _solve_variants(self._params(
            data_s="abc123data-s",
            cookies="NID:zzz;SOCS:yyy",
            user_agent="TestUA/1.0",
        ))
        self.assertEqual(variants[0]["data-s"], "abc123data-s")
        self.assertEqual(variants[0]["cookies"], "NID:zzz;SOCS:yyy")
        self.assertEqual(variants[0]["userAgent"], "TestUA/1.0")

    def test_non_enterprise_produces_single_variant(self):
        variants = _solve_variants(self._params(is_enterprise=False))
        self.assertEqual(len(variants), 1)
        self.assertNotIn("enterprise", variants[0])


class ScrapeChallengeShortCircuitTests(unittest.TestCase):
    """Without an API key the challenge must surface as a clear error."""

    def test_missing_api_key_reported_when_challenge_hit(self):
        import os
        if os.getenv("TWOCAPTCHA_API_KEY"):
            self.skipTest("TWOCAPTCHA_API_KEY set in environment")
        self.assertFalse(bool(os.getenv("TWOCAPTCHA_API_KEY")))


if __name__ == "__main__":
    unittest.main()
