# Search Archive web viewer

A local, read-only website for the existing Supabase `scrape_runs` and `queries`
tables. It reconstructs a Google-style results page from stored JSONB, without
scraping or fetching linked pages. It is not an exact historical Google screenshot.

For the archive chrome's visual direction, dashboard polish and verification,
see [Dashboard polish](visual-fidelity.md#dashboard-polish).

## Start

From the repository directory:

```sh
uv sync
uv run google-aio-web
```

Open **http://127.0.0.1:8000**. Use `uv run google-aio-web --port 8080` if that
port is occupied. `uv run python -m aio_viewer` is equivalent. Stop with Ctrl+C.

The server loads `.env` next to `aio_scraper.py`, with the same precedence as the
capture commands: exported variables win, and the last duplicate file entry wins.
It requires:

```dotenv
SUPABASE_PROJECT_ID=your_20_character_project_reference
SUPABASE_SECRET_KEY=your_server_only_project_secret_key
```

Use the secret key for the Supabase Data API, **not** a publishable/anonymous key
or `SUPABASE_ACCESS_TOKEN`. The Management API token is not used by the viewer.
The database must have the existing consolidated JSONB schema and service-role
read grants; see [SQL storage](sql-storage.md). No schema changes or public RLS
policies are needed. If the Data API is disabled, enable it for the project's
existing private schema access before using this viewer.

## Browse

1. Select a capture run. Each option identifies its provider, label, repeat round,
   and shortened run UUID. Run pagination gives access to older captures.
2. Search stored **query text** (case-insensitive); press Enter or the search
   button. Clearing the input and submitting removes the text restriction;
   **Clear all** restores the complete run, including resetting other filters.
   Supabase wildcard patterns such as `%` and `_` are supported.
3. Select a query entry (or focus it and press Enter). The page opens the rendered
   preview for that exact `(run_id, query_id)`, not another provider's response.
4. Explore the AI Overview, inline links, nested lists/tables, sources and organic
   results in their saved order. Link-glyph citation controls open a dialog for
   that exact saved citation group. Original zero/sparse indexes are available in
   the dialog, accessible names, tooltips and DOM data; unmapped indexes remain
   visibly marked without inventing a source. The desktop source panel shows the
   first three cards, with explicit expansion to all sources. On mobile, use the
   Sources button; Browse archive opens the separate navigation drawer.
5. Use **AIO JSON**, **Results JSON**, or the **Raw JSON** tab to inspect and copy
   the original complete row, `aio_response`, or `organic_results`.

Runs and query lists are paginated in groups of 50. Lists do not download answer
payloads: the full response is fetched only when selected. Captured timestamps,
provider, locale, capture status, AIO status and warnings remain visible. Absent,
unavailable, empty, invalid, unresolved and failed AIOs are not presented as
successful answers. Saved text is not truncated. Organic title links and sitelinks
open in a new tab. Logos/images are not fetched from third parties.

## Filter the archive

The always-visible **AI Overview** dropdown filters the selected run immediately.
**Absent** means the stored status is exactly `absent`. **Not marked present**
also includes unavailable/empty/unresolved/invalid/error and legacy null statuses;
it must not be interpreted as proof that Google returned no AIO. Each status can
also be selected individually.

Expand **More filters** for:

- Capture status: OK, error, unresolved, or unknown/legacy.
- Language (`hl`), country (`gl`), and device: exact stored values, e.g. `en`,
  `us`, `desktop`, `mobile`. Empty means no restriction, not “unknown”.
- Capture date from/through: inclusive **UTC calendar days**. The end date is
  implemented as strictly before the following UTC midnight. Rows without a
  capture timestamp do not match a date range. Reversed/invalid dates are rejected.
- Has/no warnings and has/no organic results, based on the saved JSON arrays.

Dropdowns apply immediately; press **Apply filters** for text/date changes.
Query search combines with all active filters using AND. Filters run in Supabase
**before** pagination, not just over the 50 visible entries. Changes reset query
pagination; filters persist when switching runs. **Clear all** resets query text
and every filter. An empty list means no matching rows, not an ingestion error.

## Compare the same query across runs

1. Open a saved response, then choose **Compare runs** in the toolbar below the preview tabs.
2. Select a different run and click **Find matching query**. Its independent
   run pagination lets you reach older runs without changing the left selection.
3. One unambiguous match opens automatically. When there are multiple captures,
   choose one explicitly; matching-capture pagination retains access to all of
   them. The selector shows its query ID, AIO status, device and capture time.
4. Compare **Run A** (the response you opened) with **Run B** side by side on
   desktop, or as stacked, jump-linked panels on mobile. Full saved AIO blocks,
   citations, sources, organic results, sitelinks and each row's raw JSON remain
   available. Each side's citations open **that side's** source mappings.
5. **What changed** shows AIO status, source/result counts, and whether the AIO
   and organic JSON differ. Expand **Organic URL changes** for added/removed URLs
   and position changes. **Back to response** returns to the original response.

### Matching and comparison rules

- Match **exact query text + language + country**, not query ID or row position.
  This works with different IDs across runs and avoids silently comparing reused
  IDs with different questions. Case/whitespace differences do not count as exact
  text matches. Null locale fields match null fields, not an assumed locale.
- Device and Google domain may differ; their saved values are shown in each pane.
  This enables comparisons across devices/providers, but is not a claim of a
  controlled same-device experiment. Run A is not necessarily chronologically
  earlier than Run B.
- Archive filters restrict the left selection only. They do **not** hide target
  matches with a different AIO/capture status—that would prevent present/absent
  comparisons. A missing target capture is shown as **no match**, never “absent”.
- JSON comparisons ignore object-key order but preserve array order and all
  values. “Changed” is an exact saved-data check, not an AI/semantic similarity
  score or a highlighted word diff.
- URL comparisons use exact saved strings, without stripping tracking parameters,
  normalizing domains, or guessing canonical links. Duplicate URLs retain all
  observed positions. Where position is missing, `order N` identifies saved array
  order, not an invented Google rank. Results lacking URLs stay in the panes/raw
  JSON but are excluded from the URL membership table.
- Only lightweight match metadata is listed. The chosen right-hand JSON payload
  is read on demand. Stale requests are discarded when switching capture, run,
  filter or view. No comparison data is written back to Supabase.

The added API is `GET /api/comparison-matches` with `run_id`, `query_id`,
`compare_run_id`, and optional bounded `limit`/`offset`. It reads the left query
and locale from storage, then lists matching target metadata. The browser still
uses `GET /api/response` to read the chosen capture. Both remain fixed-table,
fixed-column, validated SELECT-only operations; no schema changes are required.

## Security and deployment boundary

- The server binds **only to `127.0.0.1`**. It has no public-host option and no
  application login. Anyone able to access the local port can read the dataset.
  Do not forward the port publicly or place it behind a proxy; keep the local
  machine and browser session private.
- Keys remain in the server process. Only allowlisted GET queries are made to
  the fixed project Data API host; redirects are refused. No insert, update,
  delete, arbitrary SQL, or arbitrary remote URL endpoint is exposed.
- Host checks reject DNS rebinding, cross-site browser requests are rejected,
  CORS is not enabled, and responses have `no-store`, `nosniff`, no-referrer and a
  restrictive Content Security Policy. Only the three bundled frontend assets
  are served; repository files and `.env` are never served.
- Stored snippets and JSON are rendered as text, never injected as HTML. Only
  absolute HTTP(S) links without embedded credentials become external links.
  Database content itself can be sensitive, so keep the local browser/session
  private. As with other offline datasets, review what you copy or share.
- Upstream errors use generic messages, never upstream response bodies or keys.
  HTTP access logging is disabled to avoid logging query text and record IDs.

For a public hosted website or team access, add real application
authentication, authorization, HTTPS, request limits, and an appropriate
authenticated server framework **before** exposing it beyond this machine. Do
not move the secret key into browser JavaScript or make the current private
tables public to bypass access errors.

## Tests

The default suite tests API allowlists/validation, read transport, safe errors,
static asset isolation, security headers, and local request restrictions, without
connecting to Supabase:

```sh
uv run python -m unittest discover -s tests -p test_web_viewer.py -v
```

An optional real Chromium smoke test serves synthetic records and checks rendered
blocks, nested lists/tables, zero/unmapped citations, JSON switching, status/empty
states, search, mobile layout, and hostile HTML/URLs:

```sh
AIO_VIEWER_BROWSER=/absolute/path/to/chrome \
  uv run python -m unittest discover -s tests -p test_web_viewer.py -v
```

### Visual reference and regression evidence

The frontend uses a bounded, versioned desktop screenshot reference. Live Google
was CAPTCHA-blocked, and no mobile Google screenshot was available. Typography,
source-panel geometry, missing emphasis/media, and remaining pixel discrepancies
are documented rather than claimed as exact matches. See the
[visual-fidelity report](visual-fidelity.md) and
[before/reference/after gallery](visual/google-2026-11-17/index.html).

The opt-in Chromium tests also cover pinned viewer rasters, measured geometry,
content/order/citation preservation for both representative providers, source
expansion/dialogs, expanded sitelinks, query pagination/run switching, and
390/768/1024/1440px overflow. Additional tests cover combined server-side filters,
UTC end-date boundaries, exact cross-run matching with different IDs, ambiguous
and missing matches, source isolation, URL movements, stale comparison requests,
and responsive comparison panels. Reproduction commands and environment requirements
are in the report. Visual-research tools never read `.env` or use paid APIs.

There are no new runtime dependencies or frontend build steps. The HTML, CSS and
JavaScript are included as Python package data.
