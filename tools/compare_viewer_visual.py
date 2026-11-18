"""Rebuild the pinned before/reference/after boards and bounded raster comparisons.
Requires ImageMagick (`magick`), not a new Python/runtime dependency.
No whole-page fidelity score: the reference omits content and assets we retain/drop.
"""
from pathlib import Path
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "docs/visual/google-2026-11-17"
OUT = BASE / "comparisons"


def image(*args):
    return subprocess.run(["magick", *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()


def ink_bounds(path):
    bounds = image(path, "-colorspace", "Gray", "-threshold", "50%", "-format", "%@", "info:")
    match = re.fullmatch(r"(\d+)x(\d+)\+(\d+)\+(\d+)", bounds)
    if not match:
        raise ValueError(f"Cannot measure foreground: {path}: {bounds}")
    return bounds, {k: int(v) for k, v in zip(("width", "height", "x", "y"), match.groups())}


def compare_text(name, reference, ref_crop, ref_scale, selector, root_selector, source_suffix):
    ref = OUT / f"{name}-reference.png"
    image(reference, "-crop", ref_crop, "+repage", "-resize", ref_scale, ref)
    paths = {"reference": ref}
    report = {"reference_roi": ref_crop, "reference_resize": ref_scale,
              "scope": "identical title text only; no missing thumbnails/emphasis/snippets in this ROI"}
    for phase in ("before", "after"):
        styles = json.loads((BASE / phase / "reference-desktop.json").read_text())["styles"]
        node, root = styles[selector], styles[root_selector]
        x, y = round(node["x"] - root["x"]), round(node["y"] - root["y"])
        target = OUT / f"{name}-{phase}.png"
        width, height = (336, 20) if name == "source-title" else (min(round(root["width"]), 652), 28)
        image(BASE / phase / f"reference-desktop-{source_suffix}.png", "-crop", f"{width}x{height}+{x}+{y}", "+repage", target)
        paths[phase] = target
    for phase, path in paths.items():
        bounds, dims = ink_bounds(path)
        report[phase] = {"dark_foreground_bbox": dims}
        # Compare only foreground, top-left aligned; explicitly discard any
        # background-color difference to isolate letter size/shape.
        mask = OUT / f"{name}-{phase}-ink.png"
        image(path, "-colorspace", "Gray", "-threshold", "50%", "-crop", bounds, "+repage",
              "-background", "white", "-gravity", "northwest", "-extent", "652x32", mask)
    for phase in ("before", "after"):
        ref_ink = OUT / f"{name}-reference-ink.png"
        actual = OUT / f"{name}-{phase}-ink.png"
        result = subprocess.run(["magick", "compare", "-metric", "RMSE", str(ref_ink), str(actual),
                                 str(OUT / f"{name}-{phase}-difference.png")], capture_output=True, text=True)
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr)
        report[phase]["ink_rmse"] = result.stderr.strip()
        image(ref_ink, actual, "-define", "compose:args=50,50", "-compose", "blend", "-composite", OUT / f"{name}-{phase}-overlay.png")
    return report


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {}
    report["source_title"] = compare_text("source-title", BASE / "reference/aio-desktop.webp", "336x24+596+68", "100%",
                                          ".source-title", ".aio", "aio")
    report["organic_title"] = compare_text("organic-title", BASE / "reference/organic-annotated.jpg", "960x44+40+535", "62.5%",
                                           ".search-result h3", ".search-result", "organic")
    report["organic_title"]["scale_caveat"] = "0.625 is an explicit image-space normalization of a magnified annotated screenshot, NOT a verified browser DPR or CSS scale."
    for name, src in (("reference", BASE / "reference/aio-desktop.webp"), ("before", BASE / "before/reference-desktop-aio.png"), ("after", BASE / "after/reference-desktop-aio.png")):
        image(src, "-resize", "960x818>", "-background", "white", "-gravity", "northwest", "-extent", "960x818", OUT / f"aio-{name}-canvas.png")
    image(OUT / "aio-before-canvas.png", OUT / "aio-reference-canvas.png", OUT / "aio-after-canvas.png", "+append", OUT / "aio-before-reference-after.png")
    image(OUT / "aio-reference-canvas.png", OUT / "aio-after-canvas.png", "-define", "compose:args=50,50", "-compose", "blend", "-composite", OUT / "aio-overlay.png")
    report["whole_aio_overlay"] = "Qualitative ONLY: missing thumbnail and emphasis, omitted later example blocks, unmapped index 3, full source text vs reference truncation, and unknown fonts cause legitimate text/wrapping differences."
    (OUT / "measurements.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
