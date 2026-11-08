"""Opt-in, network-isolated Firefox checks: AIO_BROWSER_TESTS=1 python -m unittest discover -s tests -p test_aio_browser.py -v."""
import os
import time
import unittest
from unittest.mock import patch

import aio_scraper as a


START = '''<style>.not-shown {display:none} .gone {display:none}</style>
<div id="search"><div id="rso"><div class="g"><a href="https://example.org/"><h3>Organic</h3></a></div></div>
<div id="eKIzJc"><div data-subtree="mul">Thinking Searching Thinking a little longer</div>
<span class="not-shown" jsname="lGsj1">An AI Overview is not available for this search</span>
<span class="not-shown" jsname="L1sC6e">Can't generate an AI overview right now. Try again later.</span>
<div id="progress" role="progressbar" aria-label="Generating"></div>
<div data-subtree="aimc"><div jsname="KFl8ub" id="answer"></div></div></div></div>'''
ANSWER = '<p>Complete opening.</p><ul><div><li>First point.</li></div><div><li>Second point.</li></div></ul><p>Final conclusion.</p>'


@unittest.skipUnless(os.environ.get('AIO_BROWSER_TESTS') == '1', 'opt-in real Firefox tests')
class RealFirefoxAioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.pw = await async_playwright().start()
        self.browser = await self.pw.firefox.launch(headless=True)
        self.context = await self.browser.new_context()
        await self.context.route('**/*', lambda route: route.abort())
        self.page = await self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.scraper = a.GoogleAIOScraper(engine='firefox', proxy_mode='direct')
        self.scraper.page = self.page

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
        self.assertEqual(self.errors, [])

    async def result(self):
        return a.parse_search_html(await a._rendered_html(self.page), 'q', 'en', 'us')

    async def test_computed_css_hides_notices_without_mutating_the_live_page(self):
        await self.page.set_content(START)
        html = await a._rendered_html(self.page)
        self.assertEqual((await self.result())['extraction_metadata']['aio_status'], 'unresolved')
        self.assertFalse(await self.page.locator('[jsname="lGsj1"]').evaluate('(e) => e.hidden'))
        # Evidence stays correct even when the evidence writer removes style tags.
        soup = a.BeautifulSoup(html, 'html.parser')
        for node in soup.select('style'):
            node.decompose()
        self.assertEqual(a.parse_search_html(str(soup), 'q', 'en', 'us')['extraction_metadata']['aio_status'], 'unresolved')

    async def test_stable_loading_wrapper_does_not_beat_delayed_answer(self):
        await self.page.set_content(START + '''<script>
            setTimeout(() => {
                document.querySelector('#answer').innerHTML = ''' + repr(ANSWER) + ''';
                document.querySelector('#progress').className = 'gone';
            }, 3200);
            </script>''')
        start = time.monotonic()
        self.assertTrue(await self.scraper._wait_for_aio_answer(self.page, max_wait_ms=9000))
        self.assertGreaterEqual(time.monotonic() - start, 5)
        result = await self.result()
        self.assertEqual(result['extraction_metadata']['aio_status'], 'present')
        self.assertEqual(result['ai_overview']['text_blocks'][-1]['snippet'], 'Final conclusion.')
        self.assertEqual(len(result['ai_overview']['text_blocks'][1]['list']), 2)

    async def test_partial_answer_cannot_finish_while_progress_is_visible(self):
        await self.page.set_content(START.replace('id="answer"></div>', 'id="answer"><p>Opening only.</p></div>'))
        self.assertTrue(await self.scraper._wait_for_aio_answer(self.page, max_wait_ms=800))
        self.assertEqual((await self.result())['extraction_metadata']['aio_status'], 'unresolved')

    async def test_hidden_answer_and_prompt_only_metadata_are_not_present(self):
        await self.page.set_content(START.replace('id="progress"', 'class="gone" id="progress"')
                                    .replace('id="answer"></div>', 'class="not-shown" id="answer">' + ANSWER + '</div>'))
        self.assertEqual((await self.result())['extraction_metadata']['aio_status'], 'unresolved')

    async def test_visible_refusal_and_transient_error_remain_distinct(self):
        for name, status in [('lGsj1', 'unavailable'), ('L1sC6e', 'unresolved')]:
            await self.page.set_content(START.replace('id="progress"', 'class="gone" id="progress"')
                                        .replace('class="not-shown" jsname="' + name + '"', 'jsname="' + name + '"'))
            result = await self.result()
            self.assertEqual(result['extraction_metadata']['aio_status'], status)
            if name == 'L1sC6e':
                self.assertEqual(result['extraction_metadata']['aio_reason'], 'generation_failed')

    async def test_delayed_container_is_detected(self):
        ready = START.replace('id="progress"', 'class="gone" id="progress"').replace('id="answer"></div>', 'id="answer">' + ANSWER + '</div>')
        await self.page.set_content('<div id="search"></div><script>setTimeout(() => document.body.innerHTML = '
                                    + repr(ready) + ', 1000)</script>')
        self.assertTrue(await self.scraper._wait_for_aio_answer(self.page, max_wait_ms=6000))
        self.assertEqual((await self.result())['extraction_metadata']['aio_status'], 'present')

    async def test_real_search_reloads_transient_error_and_recovers(self):
        pages = [START.replace('id="progress"', 'class="gone" id="progress"')
                 .replace('class="not-shown" jsname="L1sC6e"', 'jsname="L1sC6e"'),
                 START.replace('id="progress"', 'class="gone" id="progress"')
                 .replace('id="answer"></div>', 'id="answer">' + ANSWER + '</div>')]
        seen = []
        async def serve(route):
            seen.append(route.request.url)
            await route.fulfill(content_type='text/html', body=pages[min(len(seen) - 1, 1)])
        # Requests are fulfilled in-process, never forwarded to Google.
        await self.page.route('https://www.google.com/search?*', serve)
        result = await self.scraper.search('q')
        self.assertEqual(len(seen), 2)
        self.assertEqual(result['extraction_metadata']['aio_status'], 'present')
        self.assertEqual(result['ai_overview']['text_blocks'][-1]['snippet'], 'Final conclusion.')

    async def test_real_search_exhaustion_keeps_loading_as_unresolved(self):
        seen = []
        async def serve(route):
            seen.append(route.request.url)
            await route.fulfill(content_type='text/html', body=START)
        await self.page.route('https://www.google.com/search?*', serve)
        original_wait = self.scraper._wait_for_aio_answer
        async def short_wait(page, *args, **kwargs):
            return await original_wait(page, max_wait_ms=500)
        with patch.object(self.scraper, '_wait_for_aio_answer', side_effect=short_wait):
            result = await self.scraper.search('q')
        self.assertEqual(len(seen), 2)
        self.assertEqual(result['extraction_metadata']['aio_status'], 'unresolved')
        self.assertNotIn('ai_overview', result)

    async def test_no_container_remains_absent_after_observation_window(self):
        await self.page.set_content('<div id="search"><div id="rso"></div></div>')
        self.assertFalse(await self.scraper._wait_for_aio_answer(self.page, max_wait_ms=500))
        self.assertEqual((await self.result())['extraction_metadata']['aio_status'], 'absent')
