# Changelog

This file collects project changes and deployment history previously described in the README. Dates below come from the recorded migration and verification notes; no release version has been assigned to these entries.

## Unreleased

- Custom captures now resolve opaque Google redirect links (`/url`, `/goto`) by observing Google's own redirect response inside the live session — redirects are never followed to destination pages, and saved input is never resolved retroactively (tokens are session-bound and most carry `psig`/`ust` signatures expiring ~24 hours after capture). Each unique link is observed at most twice: fresh in-session `/goto` links answer with a ~24–26s HTML interstitial first (some windows run past 38s), so still-opaque links get one bounded 45s settle retry, mirroring the pipeline's one lazy follow-up doctrine. Resolved records keep the original under `redirect_link` with per-query `attempted`/`resolved` counts in `extraction_metadata.redirect_resolution`; unresolved links stay opaque and explicit, never failing the capture. `opaque_google_redirect_url` now marks only unresolved primary links (organic, reference and snippet links); retained `redirect_link` provenance no longer warns.
- Documented the live public archive at `cometlab.srirajkavin.com` and removed all tailnet deployment details: the local viewer is loopback-only, and the `google-aio-web` `--tailnet-host`/`--tailnet-http-port` options were removed with their tests.
- Reorganized documentation for trust and navigability: the README now leads with installation, credentials and the five installed commands (including `google-aio-web`) with consistent `uv run` examples; `docs/scraping.md` documents the capture pipeline, module responsibilities and hard boundaries; `docs/sql-storage.md` is a full schema reference derived from the four cumulative migrations with JSONB examples and historical-evidence labeling. The dashboard polish notes were consolidated into `docs/visual-fidelity.md` (unslop attribution retained) and `docs/viewer-polish.md` was removed.
- Made AIO observation/reload state attempt-local with pure polling transitions, separated provider effects from paired record conversion, and shared native capture classification with comparison eligibility. Commands, formats, identities and upload/request policies are unchanged; see `docs/state-flow.md`.
- Separately corrected partial browser-startup cleanup and cleared resource ownership on exit. Regression tests cover launch/page/close failures, cancellation, checkpoint retention, failed exports and uncertain uploads. Offline saved-response equivalence and existing limitations are recorded in `docs/refactor-verification.md`.
- Capture, replay and export commands use JSON. CSV output and the alternative five-table formatter are removed from the core workflow.
- Single-query and batch custom captures use the same JSON capture controller, with optional upload or saved-snapshot upload without a new scrape.
- Response normalization, validation, SQL projection and upload policy share one implementation in `supabase_results.py`. `comparison_storage.py` retains its command and compatible Python imports.
- Native and shared-record uploads use one fixed-host, redirect-refusing HTTP implementation. Existing exception types, receipt formats, commands and saved JSON/SQL formats remain compatible.
- Reorganized the README for new users and moved historical notes into this changelog.
- Added uv project management: `.python-version` pins Python 3.12, `uv.lock` records dependencies, and the installation steps use `uv sync`.

## 2026-10-28: JSONB response storage

- Deployed and verified migration `20261028032336_query_response_jsonb.sql` after a fresh backup and PostgreSQL forward/write/rollback execution tests.
- Consolidated storage into `scrape_runs` and `queries`. Each query row contains one complete provider response, with AI Overview blocks/references in `aio_response` and ordered organic results in `organic_results`.
- Removed the `ai_overviews`, `ai_references` and `organic_results` child tables after checking content fidelity. Nested content no longer adds SQL rows.
- Retained both private review-view contracts, table RLS and access restrictions. Client roles cannot access the tables; the server role cannot delete or truncate them.

### Verified dataset at deployment

Comparison UUID: `f7384551-5bdc-45f3-8a9a-c1d49cdb58b6`.

| Provider | Response rows | AI Overviews | References | Organic results |
| --- | ---: | ---: | ---: | ---: |
| custom | 28 | 25 | 170 | 249 |
| serpapi | 28 | 23 | 206 | 246 |
| Total | 56 | 48 | 376 | 495 |

The review views returned 56 provider rows or 28 paired rows. The AI Overview, reference and organic-result counts describe nested JSON content, not additional SQL rows. `supabase/queries/query_responses_jsonb.sql` provides complete-response and P01/P02 side-by-side queries.

Applied migrations and the tested rollback remain in the repository. Do not edit applied migrations or replay old five-table SQL seed files against the consolidated schema. The previously removed historical run remains absent and must not be restored by consolidation. Collected evidence, recovery backups and presentation assets are retained outside the runtime workflow.

See [SQL storage and verification evidence](docs/sql-storage.md) for migration history, the historical-run removal receipt, backup locations and deployment checks.
