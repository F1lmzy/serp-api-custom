"""Tests for CAPTCHA challenge detection and 2Captcha solver plumbing."""

import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aio_scraper as scraper

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


class ChallengeSolvingTests(unittest.IsolatedAsyncioTestCase):
    def fake_page(self):
        return SimpleNamespace(
            context=SimpleNamespace(cookies=AsyncMock(return_value=[{'name': 'CONSENT', 'value': 'browser-cookie'}]),
                                    add_cookies=AsyncMock()),
            content=AsyncMock(side_effect=[CHALLENGE_HTML, CHALLENGE_HTML, NORMAL_HTML]),
            evaluate=AsyncMock(side_effect=['FakeBrowserAgent', None]),
            wait_for_selector=AsyncMock(), eval_on_selector=AsyncMock(),
            wait_for_url=AsyncMock(), wait_for_timeout=AsyncMock(),
            url='https://www.google.com/search?q=fake')

    async def test_solver_preserves_session_context_and_submits_token(self):
        page = self.fake_page()
        used = {}
        with patch.dict('os.environ', {'TWOCAPTCHA_API_KEY': 'FAKE_SOLVER_KEY'}), \
                patch.object(scraper, 'solve_recaptcha_v2', new_callable=AsyncMock,
                             return_value={'token': 'fake-token', 'cookies': 'CONSENT=worker-cookie'}) as solve, \
                contextlib.redirect_stderr(io.StringIO()):
            html = await scraper._maybe_solve_challenge(page, 'https://www.google.com/sorry/index', CHALLENGE_HTML, 1000, used)
        self.assertEqual(html, NORMAL_HTML)
        solve.assert_awaited_once()
        key, params = solve.call_args.args
        self.assertEqual(key, 'FAKE_SOLVER_KEY')
        self.assertEqual(params['user_agent'], 'FakeBrowserAgent')
        self.assertEqual(params['cookies'], 'CONSENT:browser-cookie')
        self.assertTrue(params['is_enterprise'])
        page.context.add_cookies.assert_awaited_once_with([
            {'name': 'CONSENT', 'value': 'worker-cookie', 'domain': '.google.com', 'path': '/'}])
        self.assertEqual(page.eval_on_selector.call_args.args[0], '#g-recaptcha-response')
        self.assertEqual(page.eval_on_selector.call_args.args[2], 'fake-token')
        page.wait_for_url.assert_awaited_once()
        matcher = page.wait_for_url.call_args.args[0]
        self.assertEqual(page.wait_for_url.call_args.kwargs, {'timeout': 1000})
        self.assertTrue(matcher('https://www.google.com/search?q=fake'))
        self.assertFalse(matcher('https://www.google.com/sorry/index?continue=https://www.google.com/search?q=fake'))
        self.assertFalse(matcher('https://example.com/search?q=fake'))
        self.assertTrue(used['token'])

    async def test_challenge_remaining_after_navigation_is_not_returned_as_success(self):
        page = self.fake_page()
        page.content = AsyncMock(return_value=CHALLENGE_HTML)
        page.goto = AsyncMock()
        page.evaluate = AsyncMock(return_value='FakeBrowserAgent')
        with patch.dict('os.environ', {'TWOCAPTCHA_API_KEY': 'FAKE_SOLVER_KEY'}), \
                patch.object(scraper, 'solve_recaptcha_v2', new_callable=AsyncMock,
                             return_value={'token': 'fake-token'}) as solve, \
                patch.object(scraper.asyncio, 'sleep', new_callable=AsyncMock), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(ChallengeDetected):
                await scraper._maybe_solve_challenge(page, 'https://www.google.com/sorry/index', CHALLENGE_HTML, 1000, {})
        self.assertEqual(solve.await_count, 2)

    async def test_missing_key_does_not_call_a_paid_solver(self):
        page = self.fake_page()
        with patch.dict('os.environ', {'TWOCAPTCHA_API_KEY': ''}), \
                patch.object(scraper, 'solve_recaptcha_v2', new_callable=AsyncMock) as solve:
            with self.assertRaises(ChallengeDetected):
                await scraper._maybe_solve_challenge(page, 'https://www.google.com/sorry/index', CHALLENGE_HTML, 1000, {})
        solve.assert_not_awaited()

    async def test_solver_failure_is_an_explicit_challenge_failure(self):
        page = self.fake_page()
        with patch.dict('os.environ', {'TWOCAPTCHA_API_KEY': 'FAKE_SOLVER_KEY'}), \
                patch.object(scraper, 'solve_recaptcha_v2', new_callable=AsyncMock,
                             side_effect=scraper.CaptchaSolverError('solver failed')), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(ChallengeDetected, 'solver failed'):
                await scraper._maybe_solve_challenge(page, 'https://www.google.com/sorry/index', CHALLENGE_HTML, 1000, {})
        page.eval_on_selector.assert_not_awaited()


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
