"""Pure transitions plus resource-failure tests; no browser process is started."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aio_scraper as a
from search_export import capture_status_for_aio


class WaitTransitionTests(unittest.TestCase):
    def advance(self, state, status, now, key=None, reason=None, budget=20):
        return a._advance_aio_wait(state, status=status, reason=reason, answer_key=key,
                                   now=now, elapsed=now, budget=budget)

    def test_delayed_container_and_active_generation_reset_stabilization(self):
        state, stop = self.advance(a._AioWaitState(), 'absent', 7.9)
        self.assertIsNone(stop)
        state, stop = self.advance(state, 'unresolved', 8, reason='generation_in_progress')
        self.assertIsNone(stop)
        self.assertTrue(state.seen)
        state, stop = self.advance(state, 'present', 9, 'answer')
        self.assertIsNone(stop)
        state, stop = self.advance(state, 'unresolved', 10, reason='generation_in_progress')
        self.assertIsNone(state.unchanged_since)
        state, stop = self.advance(state, 'present', 11, 'answer')
        self.assertIsNone(stop)
        state, stop = self.advance(state, 'present', 12.9, 'answer')
        self.assertIsNone(stop)
        state, stop = self.advance(state, 'present', 13, 'answer')
        self.assertIs(stop, True)

    def test_answer_change_restarts_two_second_window_without_mutation(self):
        initial = a._AioWaitState()
        state, _ = self.advance(initial, 'present', 0, 'first')
        changed, stop = self.advance(state, 'present', 1.9, 'complete')
        self.assertIsNone(stop)
        self.assertEqual(state.answer_key, 'first')
        self.assertEqual(initial, a._AioWaitState())
        _, stop = self.advance(changed, 'present', 3.8, 'complete')
        self.assertIsNone(stop)
        _, stop = self.advance(changed, 'present', 4, 'complete')
        self.assertIs(stop, True)

    def test_absence_window_is_eight_seconds_or_smaller_budget(self):
        for budget, now in ((20, 8), (1, 1)):
            state, stop = self.advance(a._AioWaitState(), 'absent', now, budget=budget)
            self.assertIs(stop, False)
            self.assertFalse(a._finish_aio_wait(state))

    def test_visible_refusal_and_transient_failure_stop_but_are_not_equivalent(self):
        for status, reason in (('unavailable', None), ('unresolved', 'generation_failed')):
            state, stop = self.advance(a._AioWaitState(), status, 0, reason=reason)
            self.assertIs(stop, True)
            self.assertEqual(state.status, status)

    def test_deadline_distinguishes_unfinished_missing_and_unstable_answers(self):
        state, _ = self.advance(a._AioWaitState(), 'unresolved', 0)
        self.assertTrue(a._finish_aio_wait(state))  # reload candidate, not completion
        missing, _ = self.advance(state, 'absent', 20)
        with self.assertRaisesRegex(a.ScrapeError, 'disappeared'):
            a._finish_aio_wait(missing)
        unstable, _ = self.advance(state, 'present', 19, 'new')
        with self.assertRaisesRegex(a.ScrapeError, 'stabilize'):
            a._finish_aio_wait(unstable)

    def test_reload_guard_depends_only_on_attempt_not_session_history(self):
        for response, seen, expected in (({}, False, False), ({}, True, True),
                                         ({'ai_overview': {}}, True, False)):
            self.assertEqual(a._should_reload_aio(a._SearchAttempt(response, seen)), expected)

    def test_capture_outcome_does_not_erase_aio_distinctions(self):
        expected = {'present': 'ok', 'absent': 'ok', 'unavailable': 'ok',
                    'empty': 'unresolved', 'unresolved': 'unresolved',
                    'error': 'error', 'invalid': 'error'}
        self.assertEqual({status: capture_status_for_aio(status) for status in expected}, expected)


class BrowserCleanupTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, *, launch_error=None, page_error=None, close_error=None):
        browser = SimpleNamespace(pages=[], new_page=AsyncMock(return_value=object(), side_effect=page_error),
                                  close=AsyncMock(side_effect=close_error))
        launch = AsyncMock(return_value=browser, side_effect=launch_error)
        pw = SimpleNamespace(chromium=SimpleNamespace(launch=launch), stop=AsyncMock())
        factory = SimpleNamespace(start=AsyncMock(return_value=pw))
        return browser, pw, factory

    async def test_launch_failure_stops_playwright_and_keeps_scrape_error_contract(self):
        browser, pw, factory = self.runtime(launch_error=RuntimeError('launch failed'))
        scraper = a.GoogleAIOScraper(engine='chromium', proxy_mode='direct')
        with patch.dict('os.environ', {'GOOGLE_AIO_PROFILE': 'off'}), \
                patch('playwright.async_api.async_playwright', return_value=factory):
            with self.assertRaisesRegex(a.ScrapeError, 'Browser launch failed'):
                await scraper.__aenter__()
        browser.close.assert_not_awaited()
        pw.stop.assert_awaited_once()
        self.assertIsNone(scraper._pw)

    async def test_page_creation_failure_closes_browser_and_preserves_original_error(self):
        error = RuntimeError('page failed')
        browser, pw, factory = self.runtime(page_error=error)
        scraper = a.GoogleAIOScraper(engine='chromium', proxy_mode='direct')
        with patch.dict('os.environ', {'GOOGLE_AIO_PROFILE': 'off'}), \
                patch('playwright.async_api.async_playwright', return_value=factory):
            with self.assertRaises(RuntimeError) as caught:
                await scraper.__aenter__()
        self.assertIs(caught.exception, error)
        browser.close.assert_awaited_once()
        pw.stop.assert_awaited_once()
        self.assertIsNone(scraper.browser)
        self.assertIsNone(scraper.page)

    async def test_close_failure_still_stops_playwright_and_clears_ownership(self):
        browser, pw, _ = self.runtime(close_error=RuntimeError('close failed'))
        scraper = a.GoogleAIOScraper(proxy_mode='direct')
        scraper.browser, scraper.page, scraper._pw = browser, object(), pw
        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            await scraper.__aexit__(None, None, None)
        pw.stop.assert_awaited_once()
        self.assertIsNone(scraper.browser)
        self.assertIsNone(scraper.page)
        self.assertIsNone(scraper._pw)
        await scraper.__aexit__(None, None, None)
        pw.stop.assert_awaited_once()  # cleanup is safe to repeat

    async def test_persistent_context_reuses_existing_page_and_closes_once(self):
        page = object()
        context = SimpleNamespace(pages=[page], new_page=AsyncMock(), close=AsyncMock())
        launcher = SimpleNamespace(launch_persistent_context=AsyncMock(return_value=context))
        pw = SimpleNamespace(firefox=launcher, stop=AsyncMock())
        factory = SimpleNamespace(start=AsyncMock(return_value=pw))
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict('os.environ', {'GOOGLE_AIO_PROFILE': str(Path(tmp) / 'profile')}), \
                patch('playwright.async_api.async_playwright', return_value=factory):
            async with a.GoogleAIOScraper(engine='firefox', proxy_mode='direct') as scraper:
                self.assertIs(scraper.page, page)
                self.assertIs(scraper.browser, context)
            context.new_page.assert_not_awaited()
            context.close.assert_awaited_once()
            pw.stop.assert_awaited_once()
            self.assertTrue(launcher.launch_persistent_context.call_args.kwargs['firefox_user_prefs'][
                'network.proxy.allow_hijacking_localhost'])

    async def test_startup_cleanup_failure_does_not_replace_page_error(self):
        error = RuntimeError('page failed')
        browser, pw, factory = self.runtime(page_error=error, close_error=OSError('close failed'))
        with patch.dict('os.environ', {'GOOGLE_AIO_PROFILE': 'off'}), \
                patch('playwright.async_api.async_playwright', return_value=factory):
            with self.assertRaises(RuntimeError) as caught:
                await a.GoogleAIOScraper(engine='chromium', proxy_mode='direct').__aenter__()
        self.assertIs(caught.exception, error)
        browser.close.assert_awaited_once()
        pw.stop.assert_awaited_once()

    async def test_cancellation_in_session_closes_browser_and_stops_playwright(self):
        browser, pw, factory = self.runtime()
        with patch.dict('os.environ', {'GOOGLE_AIO_PROFILE': 'off'}), \
                patch('playwright.async_api.async_playwright', return_value=factory):
            with self.assertRaises(asyncio.CancelledError):
                async with a.GoogleAIOScraper(engine='chromium', proxy_mode='direct'):
                    raise asyncio.CancelledError()
        browser.close.assert_awaited_once()
        pw.stop.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
