"""Optional anonymous Chromium check of the public Worker with a synthetic DB.

Requires the repository's Python Playwright installation and `npm ci`.
No production credentials, Cloudflare account, or external fetches are used.
"""
import argparse
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", type=Path, default=ROOT / ".playwright/chromium-1248/chrome-linux64/chrome")
    args = parser.parse_args()
    from playwright.sync_api import expect, sync_playwright
    server = subprocess.Popen(["node", "worker/test/serve-browser-fixture.js"], cwd=ROOT, stdout=subprocess.PIPE, text=True)
    try:
        base = server.stdout.readline().strip()
        if not base.startswith("http://127.0.0.1:"):
            raise RuntimeError("Worker fixture failed to start")
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(args.browser), headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors, external = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: external.append(request.url) if not request.url.startswith(base) else None)
            page.goto(base)
            expect(page.locator(".record")).to_have_count(50)
            page.locator("#filter-aio").select_option("absent")
            expect(page.locator(".record")).to_have_count(50)
            page.locator("#next").click()
            expect(page.locator(".record")).to_have_count(1)
            expect(page.locator(".record-query")).to_have_text("Other saved search 51")
            page.locator("#filter-aio").select_option("present")
            expect(page.locator(".record")).to_have_count(1)
            page.locator(".record").click()
            expect(page.locator("#response")).to_be_visible()
            assert page.locator("#aio-content ol").get_attribute("start") == "3"
            assert page.locator("#aio-content .citation.unmapped").inner_text() == "? 7"
            assert json.loads(page.locator("#aio-content button.citation").get_attribute("data-reference-indexes")) == [0]
            assert page.locator("#response script, #response img").count() == 0
            assert page.evaluate("window.hacked") is None
            page.locator("#compare-open").click()
            expect(page.locator("#compare-load")).to_be_enabled()
            page.locator("#compare-load").click()
            expect(page.locator(".comparison-pane")).to_have_count(2)
            expect(page.locator("#compare-side-b .comparison-pane-status")).to_contain_text("AIO: absent")
            rows = [json.loads(value) for value in page.locator(".comparison-json pre").all_text_contents()]
            assert rows[0]["query"] == rows[1]["query"]
            assert rows[0]["query_id"] != rows[1]["query_id"]
            assert rows[0]["run_id"] != rows[1]["run_id"]
            page.locator("#compare-side-a button.citation").click()
            expect(page.locator("#sources-dialog .source-title")).to_have_text("Saved source")
            page.keyboard.press("Escape")
            page.locator(".comparison-ranks summary").click()
            assert page.locator(".comparison-ranks tbody tr").count() == 3
            page.screenshot(path="/tmp/cloudflare-worker-desktop.png", full_page=True)
            for width in (1024, 768, 390):
                page.set_viewport_size({"width": width, "height": 844})
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            page.screenshot(path="/tmp/cloudflare-worker-mobile.png", full_page=True)
            page.locator("#compare-close").click()
            expect(page.locator("#response")).to_be_visible()
            page.locator("#json-tab").click()
            assert json.loads(page.locator("#json-content").inner_text()) == rows[0]
            # Unexpected upstream HTML/redirects must not become an opaque
            # JSON parse error or be inserted into the result surface.
            page.route("**/api/queries?*", lambda route: route.fulfill(status=200, content_type="text/html", body="<html>Unexpected upstream page</html>"))
            page.evaluate("loadQueries()")
            expect(page.locator("#notice")).to_contain_text("unexpected response")
            page.unroute("**/api/queries?*")
            page.route("**/api/queries?*", lambda route: route.fulfill(status=302, headers={"Location": "/unexpected-redirect"}))
            page.evaluate("loadQueries()")
            expect(page.locator("#notice")).to_contain_text("Check your connection and try again")
            assert not errors, errors
            assert not external, external
            browser.close()
        print("Worker Chromium check passed: filters, pagination, zero/unmapped citations, cross-run comparison, JSON, mobile and unsafe-content handling.")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


if __name__ == "__main__":
    main()
