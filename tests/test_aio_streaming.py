"""A streamed AIO must complete and settle, not merely stop updating its spinner."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aio_scraper as a


def shell(body='', generating=False):
    return ('<div id="eKIzJc"><div data-subtree="mul">Thinking Searching</div>'
            '<span jsname="lGsj1" style="display:none">An AI Overview is not available for this search</span>'
            + ('<div role="progressbar" aria-label="Generating"></div>' if generating else '')
            + '<div data-subtree="aimc"><div jsname="KFl8ub">' + body + '</div></div></div>')


class AioStreamingTests(unittest.IsolatedAsyncioTestCase):
    def page(self, html):
        clock = [0.0]
        # Also emulate the old query_selector/inner_text interface so the
        # loading-wrapper regression fails on timing, not a missing fake API.
        async def query(selector):
            soup = a.BeautifulSoup(html(clock[0]), 'html.parser')
            node = soup.select_one(selector)
            if node is None:
                return None
            return SimpleNamespace(inner_text=AsyncMock(return_value=node.get_text(' ', strip=True)))
        async def sleep(milliseconds):
            clock[0] += milliseconds / 1000
        page = SimpleNamespace(query_selector=AsyncMock(side_effect=query),
                               evaluate=AsyncMock(side_effect=lambda *args: html(clock[0])),
                               wait_for_timeout=AsyncMock(side_effect=sleep))
        return page, clock

    async def test_waits_for_delayed_answer_instead_of_settling_on_status_wrapper(self):
        page, clock = self.page(lambda t: shell('', True) if t < 4 else shell('<p>Full answer.</p>'))
        with patch.object(a.time, 'monotonic', side_effect=lambda: clock[0]):
            self.assertTrue(await a.GoogleAIOScraper()._wait_for_aio_answer(page, max_wait_ms=8000))
        self.assertGreaterEqual(clock[0], 6)

    async def test_waits_for_the_full_answer_to_stop_changing(self):
        page, clock = self.page(lambda t: shell('<p>Opening.</p>' if t < 1 else '<p>Opening. Full list and conclusion.</p>'))
        with patch.object(a.time, 'monotonic', side_effect=lambda: clock[0]):
            self.assertTrue(await a.GoogleAIOScraper()._wait_for_aio_answer(page))
        self.assertGreaterEqual(clock[0], 3)

    async def test_active_generation_blocks_stable_partial_answer(self):
        page, clock = self.page(lambda t: shell('<p>Same partial answer.</p>', t < 5))
        with patch.object(a.time, 'monotonic', side_effect=lambda: clock[0]):
            self.assertTrue(await a.GoogleAIOScraper()._wait_for_aio_answer(page, max_wait_ms=9000))
        self.assertGreaterEqual(clock[0], 7)

    async def test_growing_answer_is_not_silently_accepted_at_timeout(self):
        page, clock = self.page(lambda t: shell(f'<p>Answer keeps growing {t}</p>'))
        with patch.object(a.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(a.ScrapeError, 'stabilize'):
                await a.GoogleAIOScraper()._wait_for_aio_answer(page, max_wait_ms=3000)

    async def test_empty_shell_uses_full_budget_then_remains_for_reload_recovery(self):
        page, clock = self.page(lambda t: shell('', True))
        with patch.object(a.time, 'monotonic', side_effect=lambda: clock[0]):
            self.assertTrue(await a.GoogleAIOScraper()._wait_for_aio_answer(page, max_wait_ms=4000))
        self.assertGreaterEqual(clock[0], 4)

    async def test_container_can_arrive_after_initial_page_render(self):
        page, clock = self.page(lambda t: '' if t < 1.5 else shell('<p>Delayed answer.</p>'))
        with patch.object(a.time, 'monotonic', side_effect=lambda: clock[0]):
            self.assertTrue(await a.GoogleAIOScraper()._wait_for_aio_answer(page, max_wait_ms=6000))
        self.assertGreaterEqual(clock[0], 3.5)

    async def test_no_container_is_observed_for_a_bounded_window(self):
        page, clock = self.page(lambda t: '')
        with patch.object(a.time, 'monotonic', side_effect=lambda: clock[0]):
            self.assertFalse(await a.GoogleAIOScraper()._wait_for_aio_answer(page, max_wait_ms=1000))
        self.assertGreaterEqual(clock[0], 1)

    async def test_disappearing_shell_is_not_reclassified_as_clean_absence(self):
        page, clock = self.page(lambda t: shell('', True) if t < 1 else '')
        with patch.object(a.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(a.ScrapeError):
                await a.GoogleAIOScraper()._wait_for_aio_answer(page, max_wait_ms=3000)

    async def test_search_reloads_transient_failure_once_and_returns_recovered_answer(self):
        scraper = a.GoogleAIOScraper()
        scraper.page = object()
        failure = {'extraction_metadata': {'aio_status': 'unresolved', 'aio_reason': 'generation_failed'}}
        success = {'extraction_metadata': {'aio_status': 'present'},
                   'ai_overview': {'text_blocks': [{'type': 'paragraph', 'snippet': 'Recovered'}]}}
        scraper._aio_shell_seen = True
        with patch.object(scraper, '_search_once', AsyncMock(side_effect=[failure, success])) as once:
            self.assertEqual(await scraper.search('q'), success)
        self.assertEqual(once.await_count, 2)

    async def test_exhausted_loading_shell_remains_unresolved_after_bounded_reload(self):
        scraper = a.GoogleAIOScraper()
        scraper.page = object()
        unresolved = {'extraction_metadata': {'aio_status': 'unresolved', 'aio_reason': 'render_incomplete'}}
        scraper._aio_shell_seen = True
        with patch.object(scraper, '_search_once', AsyncMock(return_value=unresolved)) as once:
            self.assertEqual(await scraper.search('q'), unresolved)
        self.assertEqual(once.await_count, 2)
