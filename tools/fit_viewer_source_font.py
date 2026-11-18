"""Compare local source-title fonts against one pinned, identical-text raster.

This identifies a nearest available fallback, NOT Google's actual font. Requires
Chromium and ImageMagick. No network, font downloads, .env or production writes.
Outputs research images/measurements to /tmp by default.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.viewer_visual import BROWSER, FIXTURES, fixture_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/viewer-font-fit"))
    parser.add_argument("--browser", type=Path, default=BROWSER)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright
    row = json.loads((FIXTURES / "reference.json").read_text())
    reference = ROOT / "docs/visual/google-2026-11-17/comparisons/source-title-reference-ink.png"
    results = []
    with fixture_server(row) as base, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(args.browser), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(base)
        page.locator(".record").click()
        page.locator("#response").wait_for(state="visible")
        node = page.locator("#sources .source-title").first
        for font in ("Arial", "Noto Sans", "Nimbus Sans", "Ubuntu Sans", "DejaVu Sans", "Liberation Sans"):
            for size in (13, 13.5, 14, 14.5, 15):
                path = args.output / (font.replace(" ", "-") + str(size) + ".png")
                mask = path.with_stem(path.stem + "-mask")
                node.evaluate("(n,args)=>{n.style.fontFamily=args.font;n.style.fontSize=args.size+'px';n.style.lineHeight='28px'}", {"font": font, "size": size})
                node.screenshot(path=str(path), animations="disabled")
                bounds = subprocess.check_output(["magick", str(path), "-colorspace", "Gray", "-threshold", "50%", "-format", "%@", "info:"], text=True).strip()
                subprocess.run(["magick", str(path), "-colorspace", "Gray", "-threshold", "50%", "-crop", bounds, "+repage", "-background", "white", "-gravity", "northwest", "-extent", "652x32", str(mask)], check=True)
                comparison = subprocess.run(["magick", "compare", "-metric", "RMSE", str(reference), str(mask), "null:"], capture_output=True, text=True)
                if comparison.returncode not in (0, 1):
                    raise RuntimeError(comparison.stderr)
                score = float(re.search(r"\(([^)]+)\)", comparison.stderr).group(1))
                results.append({"font": font, "size": size, "score": score, "bbox": bounds})
        browser.close()
    results.sort(key=lambda r: r["score"])
    (args.output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results[:5], indent=2))


if __name__ == "__main__":
    main()
