# Search Archive dashboard polish

## Direction

- **Product:** Inspect saved search responses, trace citations, and compare capture runs
- **Character:** Exact, quiet, investigative
- **World:** A research archive, with white reading surfaces, slate navigation, and blue links and selection
- **Moments:** The reconstructed answer and its source panel, with a practical raw JSON inspector
- **Signature:** Monospace saved query IDs in archive records and response metadata

This was a light polish, not a redesign. The existing Arial-based typography,
slate palette, section order, features, and reference-matched results were retained.

## Passes

1. **Spacing:** Normalized archive group gaps and panel padding to the 4px grid.
   Compact controls use a 36px minimum height; mobile sidebar controls use 40px.
   The combined search control is 40px desktop / 44px mobile including its border.
2. **Color:** Consolidated archive text and surfaces into shared slate tokens.
   Selection is a quiet surface step, and read-only metadata no longer looks like
   a separate semantic status. Search-result colors remain reference-specific.
3. **Icons:** Replaced the archive search glyph with a 16px, 1.5-stroke Lucide
   search SVG. Removed the empty-state glyph. The AI Overview mark and citation
   glyphs remain intentionally unchanged as part of the search reconstruction.
4. **Copy:** Used sentence-case section labels and a shorter search placeholder.
   The empty state names the task, and archive records no longer repeat an
   unnecessary “Open saved response” instruction. No claims or saved data changed.
5. **Type:** Raised archive microcopy to at least 12px, removed tracked uppercase
   labels and negative brand tracking, and added monospace query IDs and tabular
   archive figures. Result typography remains unchanged.
6. **Layout:** Kept the desktop archive/sidebar proportions and responsive drawer.
   Compare runs now sits alongside the JSON actions in the preview toolbar rather
   than being absolutely positioned above it. Toolbar actions wrap on mobile.
7. **Details:** Unified archive control radii, softened inspector panel corners,
   added a comparison-table hover state, and retained visible keyboard focus.
   Saved status values remain quiet text rather than decorative badges.
8. **Visuals:** Kept the answer and source panel as the focal content; no generated
   image, placeholder, invented chart, or additional feature was introduced.
9. **Motion:** Kept short color/background/border transitions with a single easing
   curve and respected reduced-motion preferences. No entrance or looping motion.

## Verification

Rendered the offline reference fixture at 1440px desktop and 390px mobile;
inspected the welcome screen, archive drawer, preview, JSON, and comparison views.
The mobile drawer measured 320px wide, with 240px remaining for archive records
at a 390 × 844 viewport. No live database or third-party content was needed.

All 21 viewer tests pass, including browser interaction, keyboard access,
responsive overflow, filtering, comparison races, citation mapping, content
preservation, security boundaries, and exact raster checks:

```sh
AIO_VIEWER_BROWSER="$PWD/.playwright/chromium-1248/chrome-linux64/chrome" \
  uv run python -m unittest discover -s tests -p test_web_viewer.py -v
```

Two clipped AIO raster baselines under
`docs/visual/google-2026-11-17/after/reference-{desktop,mobile}-aio.png` were
refreshed after inspecting their differences. Moving the archive toolbar changes
the results' absolute paint position. The desktop crop differs in only 17 of
502,080 pixels, and the mobile crop in 54 of 231,660 pixels, confined to the last
citation controls' anti-aliased edges. The crops retain their original dimensions,
answer wrapping, source geometry, and content; the strict hash assertion remains.
The historical full-page captures and comparison boards were not regenerated.

## Credit

Polish process adapted from **unslop** by **Damir Sokolovsky (@heydamir)**,
licensed under **CC BY 4.0**. No image generation prompt was needed.
