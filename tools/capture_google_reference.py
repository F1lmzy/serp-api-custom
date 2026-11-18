"""Opt-in direct Google reference capture; no .env, login, proxy or paid API.

This is visual research tooling, NOT part of the scraper or website. CAPTCHA or
consent pages are recorded as unavailable references, never bypassed. Output
may contain public search content; review before sharing.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--browser", type=Path, default=ROOT / ".playwright/chromium-1248/chrome-linux64/chrome")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(args.browser), headless=True)
        for name, size, mobile in (("desktop", {"width": 1440, "height": 1000}, False),
                                   ("mobile", {"width": 390, "height": 844}, True)):
            # Keep UA as the browser default, recording it exactly. This is
            # viewport/touch emulation, not a claim of a real phone reference.
            context = browser.new_context(viewport=size, locale="en-US", timezone_id="America/New_York",
                                          device_scale_factor=1, is_mobile=mobile, has_touch=mobile)
            page = context.new_page()
            metadata = {"captured_at": datetime.now(timezone.utc).isoformat(), "query": args.query,
                        "viewport": size, "browser": browser.version, "locale": "en-US", "hl": "en", "gl": "us",
                        "device_scale_factor": 1, "mobile_viewport_touch_emulation": mobile,
                        "interaction": "initial load; no consent, CAPTCHA, expansion or sign-in clicks",
                        "geography": "direct network; requested gl=us, IP location not verified", "signed_in": False}
            try:
                response = page.goto("https://www.google.com/search?" + urlencode({"q": args.query, "hl": "en", "gl": "us", "pws": "0"}),
                                     wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(6000)
                url = urlsplit(page.url)
                blocked = url.path.startswith("/sorry") or "consent.google" in url.netloc
                metadata.update(status=response.status, landed_on=url.scheme + "://" + url.netloc + url.path,
                                reference_available=not blocked, user_agent=page.evaluate("navigator.userAgent"))
                if blocked:
                    # Exclude challenge identifiers and the client IP footer.
                    page.screenshot(path=str(args.output / f"{name}-unavailable.png"), clip={"x": 0, "y": 0, "width": size["width"], "height": 160})
                else:
                    page.screenshot(path=str(args.output / f"{name}.png"), full_page=True)
                    metadata["dom_measurements"] = page.locator("h3").evaluate_all("""nodes => nodes.slice(0,20).map(n => {
                      const s=getComputedStyle(n),r=n.getBoundingClientRect();
                      return {tag:n.tagName,classes:n.className,text:n.textContent,fontFamily:s.fontFamily,fontSize:s.fontSize,
                              fontWeight:s.fontWeight,lineHeight:s.lineHeight,color:s.color,x:r.x,y:r.y,width:r.width,height:r.height};
                    })""")
                    metadata["aio_visible"] = page.get_by_text("AI Overview", exact=True).count() > 0
            except Exception as exc:
                metadata.update(reference_available=False, error_type=type(exc).__name__)
            (args.output / f"{name}.json").write_text(json.dumps(metadata, indent=2) + "\n")
            context.close()
        browser.close()


if __name__ == "__main__":
    main()
