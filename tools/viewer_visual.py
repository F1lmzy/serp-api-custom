"""Offline, deterministic viewer screenshots and computed geometry; never reads .env.

uv run python tools/viewer_visual.py --output /tmp/viewer-visual --fixture reference
Use --browser /path/to/chrome when the bundled executable is elsewhere.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import subprocess
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aio_viewer.server import ViewerHandler

BROWSER = ROOT / ".playwright/chromium-1248/chrome-linux64/chrome"
FIXTURES = ROOT / "tests/fixtures/viewer"


class VisualReader:
    def __init__(self, row):
        self.row = row

    def get(self, table, params):
        if table == "scrape_runs":
            return [{"run_id": self.row["run_id"], "provider": "fixture", "label": "Offline visual fixture", "run_index": 1}]
        if params["select"] == "*":
            return [self.row]
        return [{k: self.row[k] for k in ("query_id", "query", "aio_status")}]


@contextmanager
def fixture_server(row):
    with ThreadingHTTPServer(("127.0.0.1", 0), ViewerHandler) as server:
        server.reader = VisualReader(row)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


MEASURE = """() => {
 const result = {};
 for (const selector of ['#preview', '.aio', '#aio-title', '#aio-content', '#aio-content > .answer-block:first-child p', '#sources', '.source-card', '.source-title', '.source-snippet', '.source-name', '.organic', '.search-result', '.search-result h3', '.result-snippet', '.site-name', '.display-url', '.citation']) {
  const node = document.querySelector(selector);
  if (!node) continue;
  const s = getComputedStyle(node), r = node.getBoundingClientRect();
  result[selector] = {x:r.x, y:r.y, width:r.width, height:r.height, fontFamily:s.fontFamily, fontSize:s.fontSize, fontWeight:s.fontWeight, lineHeight:s.lineHeight, color:s.color, backgroundColor:s.backgroundColor, backgroundImage:s.backgroundImage, border:s.border, borderRadius:s.borderRadius, padding:s.padding, margin:s.margin, display:s.display};
 }
 return result;
}"""


def capture(browser_path, output, fixture):
    from playwright.sync_api import sync_playwright
    row = json.loads((FIXTURES / f"{fixture}.json").read_text())
    output.mkdir(parents=True, exist_ok=True)
    fonts = {}
    for requested in ("Arial", "Noto Sans"):
        try:
            family, path = subprocess.check_output(["fc-match", requested, "-f", "%{family}\\n%{file}"], text=True).splitlines()
            fonts[requested] = {"family": family, "file": Path(path).name, "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    with fixture_server(row) as base, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(browser_path), headless=True)
        for name, viewport in (("desktop", {"width": 1440, "height": 1000}), ("mobile", {"width": 390, "height": 844})):
            context = browser.new_context(viewport=viewport, locale="en-US", timezone_id="UTC", device_scale_factor=1,
                                          reduced_motion="reduce", color_scheme="light")
            page = context.new_page()
            # A network assertion: the fixture should never fetch a favicon,
            # linked article, Google font, or live database record.
            external = []
            page.on("request", lambda request: external.append(request.url) if not request.url.startswith(base) else None)
            page.goto(base)
            if name == "mobile":
                page.locator("#archive-toggle").click()
            page.locator(".record").first.click()
            page.locator("#response").wait_for(state="visible")
            page.evaluate("document.fonts.ready")
            page.screenshot(path=str(output / f"{fixture}-{name}.png"), full_page=True)
            page.locator("#preview").screenshot(path=str(output / f"{fixture}-{name}-results.png"), animations="disabled")
            page.locator(".aio").screenshot(path=str(output / f"{fixture}-{name}-aio.png"), animations="disabled")
            page.locator(".search-result").first.screenshot(path=str(output / f"{fixture}-{name}-organic.png"), animations="disabled")
            metrics = {"viewport": viewport, "browser": browser.version, "platform": sys.platform, "resolved_fonts": fonts,
                       "dpr": 1, "locale": "en-US", "timezone": "UTC",
                       "fixture_sha256": hashlib.sha256((FIXTURES / f"{fixture}.json").read_bytes()).hexdigest(),
                       "styles": page.evaluate(MEASURE), "overflow": page.evaluate("document.documentElement.scrollWidth > innerWidth"),
                       "external_requests": external}
            (output / f"{fixture}-{name}.json").write_text(json.dumps(metrics, indent=2) + "\n")
            if external:
                raise AssertionError(f"Unexpected external requests: {external}")
            print(f"{fixture} {name}: saved screenshots and measured styles to {output}")
            context.close()
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/viewer-visual"))
    parser.add_argument("--fixture", choices=("reference", "pap-serpapi", "pap-custom"), default="reference")
    parser.add_argument("--browser", type=Path, default=BROWSER)
    args = parser.parse_args()
    capture(args.browser, args.output, args.fixture)
