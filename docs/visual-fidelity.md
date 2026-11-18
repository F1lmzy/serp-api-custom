# Viewer visual fidelity: bounded reference v1

**Reviewed/retrieved 2026-11-17 UTC. This is not a pixel-perfect Google clone.**
The reference is a pinned, published screenshot of a Google result, not the
previous synthetic viewer screenshot and not a verified current Google rollout.

## Evidence and reference contract

Open the [before / reference / after comparison gallery](visual/google-2026-11-17/index.html).
All images, input fixtures, measurements, crops and transformations are local,
versionable files. They are **not** served by the viewer's asset allowlist.

### Attempted live reference

Two new, signed-out direct Chromium contexts searched:

> effects of Federal Reserve interest rate increases on inflation in the United States

- Date: 2026-11-17 05:55 UTC.
- Browser: bundled Chromium **156.0.8078.4**, headless Linux.
- Requested settings: `hl=en`, `gl=us`, `pws=0`, browser locale `en-US`, timezone
  `America/New_York`. IP geography was **not** verified.
- Desktop: 1440×1000 CSS px, DPR 1.
- Mobile attempt: 390×844 CSS px, DPR 1, mobile/touch viewport emulation, but the
  **default desktop Chromium user agent**, not a physical phone.
- State: initial load, no sign-in, consent, CAPTCHA-solving or expansion clicks.
- Result: both redirected to `/sorry/` (CAPTCHA). Cropped evidence images exclude
  IP/challenge identifiers. No usable Google result DOM/computed styles were
  obtained. No proxy, solver credits, paid search API, or personal browser profile
  was used. The production capture pipeline was not changed.

### Chosen desktop reference

[SerpAPI's AI Overview documentation](https://serpapi.com/ai-overview#api-examples-typical-result)
publishes a screenshot and associated example JSON for **“drop shipping.”**
The pinned original is `reference/aio-desktop.webp` (960×818 raster pixels).

- Observed state: expanded AIO text with a right-hand panel of three supporting
  sources and a “Show all” button; inline link-glyph citation controls.
- Language: English. Actual Google locale, original capture date, viewport, DPR,
  browser, fonts and experiment ID are **not published/verified**. Retrieval date
  must not be mistaken for original capture date.
- Native image-space anchors: white surface; left gutter 16 px; source panel
  x=584, y=56, width=360 px; inferred text column 544 px with a 24 px gap.
  Source background sampled at (800,60): RGB(241,244,255); button at (800,440):
  RGB(212,228,254). WebP compression and image scaling limit precision.
- The source title “Drop shipping - Wikipedia” has a thresholded dark-foreground
  bounding box of **155×13 raster px** in the pinned crop. This is a raster
  measurement, **not** a claimed Google CSS font declaration.

[SerpAPI's organic-results documentation](https://serpapi.com/organic-results#api-examples-organic-results-overview)
provides a separate **“coffee”** result screenshot with annotations. We use the
second result's matching title/snippet as a limited organic reference, not the
unrelated Maps “Web Result” card layout. Its original date/device/locale/DPR/font
are also unknown. For the title-only comparison, the magnified annotated image
is explicitly resized **62.5%**; that is an image-space normalization, not proof
of a 1.6 browser DPR. The annotation-free title crop is recorded in the tool.

### Mobile scope

**No verified mobile Google screenshot was available.** The 390×844 layout is
an accessible, responsive adaptation of this bounded desktop reconstruction,
not a certified Google mobile match. Google's [2024-08-15 announcement](https://blog.google/products/search/new-ways-to-connect-to-the-web-with-ai-overviews/)
describes a right-hand desktop link display and tap-to-open mobile sources;
it supports that interaction pattern, not our mobile pixel values. Its abstract
illustration is not used as a screenshot baseline.

## What changed

- Archive navigation has its own neutral background, compact toolbar and optional
  desktop sidebar/mobile drawer. Metadata, diagnostics and JSON controls are
  outside the result surface. There is no fake Google logo, search field or
  nonfunctional search tab bar.
- AIO's invented 17 px rounded gradient enclosure is removed. The desktop result
  surface is 960 px wide, with a 544 px answer / 24 px gap / 360 px source panel,
  and 16 px outer gutters. The source panel starts 56 px below the AIO top.
- Desktop answer typography targets the reference raster: Arial fallback,
  14/20 px. Source titles use a locally measured Noto Sans fallback at 13/18,
  snippets 12/18, source names 11/16. A six-family / five-size raster fit selected
  Noto Sans 13 as the nearest installed source-title fallback; all 30 candidates
  are recorded in `comparisons/font-fit.json`. These are implementation targets
  chosen from the screenshot, **not retrieved Google computed styles**. This does
  not identify Google's actual font. If Noto Sans is unavailable, the browser
  falls back to Arial/sans-serif and results can differ. No fonts are downloaded.
- Link-glyph controls replace numbered citation chips. Each control retains its
  **exact saved index group** in `data-reference-indexes`, its accessible name
  and tooltip. It opens a keyboard-dismissable dialog containing only matching
  saved sources in source order, including duplicate observed indexes if present.
  Index zero is not treated as missing. Unmapped indexes remain visibly marked
  `? index`, never guessed from source position.
- The source panel initially shows three complete source cards. “Show all”
  exposes the rest, and “Show fewer” collapses them. All sources are also
  reachable from the mobile source button. Full snippets are not line-clamped.
- Explicit retained list-item `title` values now render; plain-text colon labels
  do **not** get fabricated emphasis. Nested lists, ordered-list starts, tables,
  header cells and stored inline links remain supported.
- Organic results use a 652 px text column, 20/26 px blue titles, 14/22 px snippets,
  14/20 px source names, 12/18 px displayed URLs, and separated inline/expanded
  sitelinks. Expanded sitelink snippets are no longer discarded. Fake letter
  favicons are removed; only their empty alignment space is reserved.

## Data audit: CSS cannot restore missing evidence

Representative first occurrences from the two requested saved inputs were read
and replayed **offline** through the existing normalizers into
`tests/fixtures/viewer/pap-custom.json` and `pap-serpapi.json`:

| Stage | Retained / lost |
| --- | --- |
| `pap-300-final.json` native custom response | First occurrence has 9 organic highlight arrays, 6 AIO blocks, 9 references and 9 organic results. No favicon/thumbnail keys in this occurrence. |
| `pap-300-serpapi.records.json` shared export | First occurrence has 10 highlight arrays across AIO/organic content, 6 AIO blocks, 7 references and 9 organic results. This is already a scoped shared record, not untouched SerpAPI HTML. |
| `search_export._scoped` / `MEDIA_KEYS` | Excludes favicons, source icons, thumbnails, images/media and out-of-scope modules. Highlight arrays survive shared export. Their absence in these representative records does not prove they were originally collected. |
| `supabase_results.IGNORED` / `_project` | Drops `snippet_highlighted_words` and `formatted` recursively. Keeps explicit snippet/link/citation/list/table content. Unknown substantive block fields such as `title` survive with warnings. |
| `_normalize_organic_results` | Keeps ordinal independently of returned position, title, link, snippet, displayed link, source, date, author, structured sitelinks and textual rich snippets. Does not persist emphasis ranges or media. |
| `queries` row | Complete scoped `aio_response` and ordered `organic_results`, but no captured screenshot, font metrics, original viewport, original expansion state, favicon bytes, exact inline citation offsets, or Google CSS. |

Consequences:

1. **Emphasis cannot be recovered by CSS.** No keyword/colon heuristics or inferred
   bolding were added. Explicit headings/titles are distinct retained information.
2. **Missing images are not replaced** with unrelated assets, generated brand
   letters or newly fetched favicons. Missing AIO thumbnails change paragraph
   wrapping; missing source thumbnails change card geometry.
3. Some custom `snippet_links` contain only a URL, without anchor text/offsets.
   They remain in raw JSON, but cannot honestly be placed at an invented phrase.
4. Citation groups are preserved, but the source DOM's precise citation insertion
   positions and its original icon assets were not retained.
5. References in the example documentation contain ellipses and omit index 3
   and later list items. The comparison fixture is explicitly an **excerpt**.
   Its organic section belongs to the separate coffee example, not an asserted
   real combined “drop shipping” SERP. Its final organic fixture contains only
   the three sitelink URLs published in the JSON. The reference screenshot and
   initial before crop show an additional “Coffee Sourcing” label; that is a
   **text/data difference**, not a styling regression, and its URL is not guessed.
6. The screenshot truncates reference snippets and contains selected/bold text.
   Our full saved snippets and lack of fabricated emphasis necessarily differ.

No schema, stored records, normalizer, scraper, database grants, ingestion behavior
or production API were changed for this work.

## Reproducible comparisons

Pinned files: `docs/visual/google-2026-11-17/manifest.json` records image/input
hashes, origin URLs, unavailable metadata and the rendering environment. `before/`
contains immutable pre-change captures, including the original synthetic
`/tmp/search-archive-preview.png`. It is **viewer baseline evidence only**.

After screenshots use synthetic public documentation excerpts and offline
projections of the representative saved records, not live-changing Supabase data.
They are captured at 1440×1000 and 390×844, DPR 1, locale `en-US`, timezone UTC,
light mode, reduced motion. The actual local Arial and Noto Sans font file hashes
and Chromium version are recorded in each after measurement JSON. No external requests are
allowed by the capture script.

```sh
uv run python tools/viewer_visual.py \
  --fixture reference --output /tmp/viewer-reference-check
uv run python tools/viewer_visual.py \
  --fixture pap-serpapi --output /tmp/viewer-pap-check
uv run python tools/viewer_visual.py \
  --fixture pap-custom --output /tmp/viewer-custom-check
# Rebuild overlays/differences from the pinned before/reference/after images:
uv run python tools/compare_viewer_visual.py
# Reproduce the local source-title font fit (30 candidates, no downloads):
uv run python tools/fit_viewer_source_font.py --output /tmp/viewer-font-fit
```

The comparison script requires the already installed ImageMagick `magick`.
It produces a before/reference/after board, full-AIO overlay, matching-title
foreground crops, overlays, absolute differences, bounding boxes and RMSE values.
No global pixel score is reported: unequal content would make it misleading.

### Measured result and remaining differences

- The desktop source panel's **x=584 / y=56 / width=360** in AIO-local coordinates
  matches the reference raster anchors. Before, its width was about 275 px.
- The source-title foreground box changes from **135×10** (before) to **156×13**
  (after), against **155×13** in the reference. The local font fit reduces aligned
  binary-ink RMSE from **0.16073 to 0.129889**. That is a measured improvement for
  this identical-text crop, **not the same raster**, proof of the original font,
  or a whole-page fidelity score. Font shape and antialiasing still differ.
- The normalized organic-title foreground is **347×20** in the reference,
  **318×19** before and **317×19** after; RMSE **0.297533 → 0.308758**. No organic
  title pixel-fidelity improvement is claimed. The 62.5% normalization and unknown
  reference font/device make these diagnostic values, not an acceptance threshold.
- The source panel is taller because complete source snippets are retained and
  its missing media is not fabricated. The intro wraps differently without the
  original thumbnail. The screenshot's later list content is absent from the
  published JSON excerpt, so the whole-AIO overlay is **qualitative only**.
- Mobile spacing, source dialog, archive drawer, nested tables and expanded
  sitelinks are regression-tested adaptations, not verified Google experiments.

These are material limitations. A direct matching-content Google screenshot plus
its rendered DOM/font metrics and retained emphasis/media is needed to certify
closer text-shape/wrapping or mobile equivalence. A newer real reference should
be introduced as a **new reference version**, not silently overwrite these files.

### Automated checks

```sh
AIO_VIEWER_BROWSER="$PWD/.playwright/chromium-1248/chrome-linux64/chrome" \
  uv run python -m unittest discover -s tests -p test_web_viewer.py -v
```

Checks include pinned **viewer** raster snapshots (not asserted Google equality),
computed font/line-height/background values and column bounding boxes at both
sizes; overflow at 390/768/1024/1440; explicit list titles and expanded sitelinks;
source expansion/dialogs, sparse/zero/unmapped citations; full saved snippet,
result order and href preservation for both provider fixtures; raw JSON round
trips; query pagination/run navigation; unsafe HTML/URLs; and no external asset
requests. Raster checks require the recorded Chromium/font environment and fail
with an artifact in `/tmp/viewer-regression-*.png` on a difference. Do not bless
new screenshots merely to make a failing test pass.

To attempt a **new** live reference (two direct searches; no paid requests):

```sh
uv run python tools/capture_google_reference.py \
  --query 'effects of Federal Reserve interest rate increases on inflation in the United States' \
  --output /tmp/new-google-reference
```

If access is blocked, the script records that limitation instead of solving or
bypassing the challenge. It does not participate in website rendering.

## Dashboard polish

A light polish pass over the archive chrome (not a redesign) set the
character — **exact, quiet, investigative** — with white reading surfaces,
slate navigation, blue links and monospace saved query IDs. The retained
Arial-based result typography, palette, section order and reference-matched
results were kept. The pass covered, in order: normalized spacing on a 4px
grid, shared slate color tokens, a 16px Lucide search SVG (citation glyphs
unchanged), sentence-case labels and shorter placeholder copy, 12px minimum
microcopy with tabular figures, the preview toolbar layout with wrapping
mobile actions, unified radii and comparison-table hover, and reduced-motion
respects. No generated imagery, invented charts or new features were added;
no saved data or claims changed.

Verification rendered the offline reference fixture at 1440px desktop and
390px mobile. All 21 viewer tests pass, including browser interaction,
keyboard access, responsive overflow, filtering, comparison races, citation
mapping, content preservation, security boundaries and exact raster checks:

```sh
AIO_VIEWER_BROWSER="$PWD/.playwright/chromium-1248/chrome-linux64/chrome" \
  uv run python -m unittest discover -s tests -p test_web_viewer.py -v
```

Two clipped AIO raster baselines under
`docs/visual/google-2026-11-17/after/reference-{desktop,mobile}-aio.png`
were refreshed after inspecting their differences: moving the archive toolbar
changes the results' absolute paint position, and the desktop crop differs
in only 17 of 502,080 pixels, the mobile crop in 54 of 231,660 pixels,
confined to the last citation controls' anti-aliased edges. The crops retain
their original dimensions, answer wrapping, source geometry and content;
the strict hash assertion remains. The historical full-page captures and
comparison boards were not regenerated.

Polish process adapted from **unslop** by **Damir Sokolovsky
(@heydamir)**, licensed under **CC BY 4.0**. No image generation prompt was
needed. *(This section consolidates the former `viewer-polish.md`.)*

## Deployment boundary

The backend/service configuration is unchanged:
`google-aio-viewer.service` → `127.0.0.1:8000`. The unrelated port-8080
proxy is untouched. Frontend assets are read from disk per request, so
deploying these static changes does not require stopping/restarting the
service. Credentials remain server-only;
DOM construction uses text nodes and validated HTTP(S) links; no inline script,
external font/image/CSS dependency, new API, framework or CSP relaxation is added.
