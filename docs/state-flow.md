# Following state through a query

The eight runtime capture/storage modules are unchanged. This is a map of the
existing capture → replay/export → optional upload choices, not a new workflow.
It covers the four capture/storage commands (`google-aio`, `google-aio-compare`,
`google-aio-export`, `google-aio-sql`). The read-only `google-aio-web` viewer and
the hosted Worker browse stored results and do not participate in capture state.

## Ownership and responsibility boundaries

| State | Owner and lifetime | Not equivalent to |
| --- | --- | --- |
| Browser/session resources (`_pw`, `browser`, `page`, profile/cookies) | `GoogleAIOScraper.__aenter__` / `__aexit__`, one context for the batch | Completion of any individual AIO |
| AIO observation history (`seen`, last DOM status, answer/citation key, unchanged-since time) | Immutable `_AioWaitState`, local to one `_wait_for_aio_answer` call | A successful extraction; `seen=True` only establishes a container existed |
| One navigation's response and shell observation | `_SearchAttempt`, returned by `_search_attempt`; `search` owns the one-reload decision | Browser state; `_aio_shell_seen` remains a compatibility mirror, never a decision input |
| Opaque redirect resolutions (`redirect_link`, `redirect_resolution` counts) | The final attempt's response, mutated once in place by `redirect_resolver.resolve_redirect_links` after the reload decision | A property of the live capture session; never applied to saved input or re-derived offline |
| Capture outcome (`ok`, `unresolved`, `error`) | Native extraction adapter or SerpAPI result; paired `sources` also owns timing/evidence | AIO status or database-write success |
| Batch progress | `collect_batch`: planned count and ordered results; `_capture`: report, native responses, records and completed/comparable counts | Durable publication of every in-memory result |
| Persistence outcome | Local save/export return or exception; upload receipt or exception | Capture outcome; an upload failure may mean the server committed |

DOM classification, polling transitions and reload guards are pure functions.
`_wait_for_aio_answer` supplies the monotonic times and Playwright effects.
`_capture_source` owns provider calls/timing; `_pair_records` converts their
responses/outcomes without I/O or mutation of its inputs. `normalize_response`
owns scoped content/provenance, while `supabase_results` owns stricter storage
validation/projection. Neither normalizer fetches destinations or guesses citations.

## Transition tables

### AIO classification: `_aio_state(container)` (pure)

Every DOM sample is classified afresh; this function has no history or clock.
Missing/hidden/generating/answer guards run in that order, before notices.
If multiple exact notices are visible, their DOM order determines the result.

| Event / guard | Classification | Side effect / next decision |
| --- | --- | --- |
| No container | `absent` | No answer; poll history decides whether absence is clean |
| Hidden container | `unresolved`, `render_incomplete` | Not a confirmed absence |
| Visible generating progressbar / `aria-busy=true` | `unresolved`, `generation_in_progress` | Even stable partial answer text cannot complete |
| Selected answer has visible content | `present` | Poller still requires stabilization; parser extracts semantic blocks |
| Visible exact “Can't generate…” notice, no answer | `unresolved`, `generation_failed` | Stop polling; eligible for bounded recovery |
| Visible exact “An AI Overview is not available…” notice, no answer | `unavailable` | Confirmed refusal, distinct from generation failure |
| Other shell / hidden notice templates / prompt-only copy | `unresolved`, `render_incomplete` | Wait, never settle on loading copy |

`parse_search_html` validates a real results page, extracts ordered organic cards,
and calls this classifier. A visible answer producing no semantic blocks becomes
`empty`; it is not clean absence. Blocked/incomplete pages raise `ScrapeError`.

### Observation and recovery

| State / event | Guard / timing | Allowed next state or terminal result | Effects / actual function |
| --- | --- | --- | --- |
| No shell yet → another absent sample | Less than `min(8s, poll budget)` elapsed | Continue; after that window stop with shell-seen `False` | `_advance_aio_wait`; driver sleeps 400ms between samples |
| Any sample → shell first appears | Any non-absent classification | `seen=True` for remainder of this attempt | `_advance_aio_wait`; no reset across temporary disappearance |
| Shell → present answer | New semantic block/citation key | Start/restart unchanged-since timer | `_aio_answer_key`, `_advance_aio_wait`; no I/O |
| Present → same key | At least 2s unchanged, no active generation | Stop with shell-seen `True` | `_advance_aio_wait` |
| Present → non-present sample | Generation, hidden shell, or missing container | Clear stabilization history, continue (unless visible terminal notice) | `_advance_aio_wait` |
| Visible unavailable / generation-failed notice | Classifier priority above applies | Stop with shell-seen `True`, **not** a claim of answer completion | `_advance_aio_wait` |
| Poll deadline, never saw shell | No prior container | Stop with `False` | `_finish_aio_wait` |
| Poll deadline, last sample absent after shell | `seen=True` | `ScrapeError`: disappeared | `_finish_aio_wait`; no automatic reload of exceptions |
| Poll deadline, last sample still changing answer | Answer key remains set | `ScrapeError`: did not stabilize | `_finish_aio_wait`; no automatic reload of exceptions |
| Poll deadline, unfinished shell | No stable answer, shell seen | Stop with `True`; native parser retains unresolved status | `_finish_aio_wait` |
| Final attempt parsed, opaque links present | Cap 50 unique links, 75s budget, 4 concurrent, 10s/link, `max_redirects=0`, at most two observations per link | Each 3xx with a non-Google HTTP(S) `Location` replaces `link` verbatim and keeps the original as `redirect_link`; anything else stays as parsed | `redirect_resolver.resolve_redirect_links` after the reload decision; `attempted`/`resolved` recorded in `extraction_metadata.redirect_resolution` |
| Still-opaque links after the first sweep | Budget can fit one 45s settle | Pause, then re-observe each once (fresh in-session `/goto` links serve a ~24–26s interstitial first, some windows run past 38s); never a third attempt | Second `observe_redirects` pass inside `redirect_resolver.resolve_redirect_links` |
| Resolution failure of any kind | Never retried beyond the one settle pass | Links stay as parsed; capture/AIO status unchanged | Per-link catches and step-level guard log and keep every link opaque |
| First attempt returns response | Shell seen AND no `ai_overview` key | One more navigation, then return that attempt's response regardless of outcome | `_should_reload_aio`, `search`; 20s first poll budget, 30s second; no loop |
| First attempt has overview, or no shell | Guard false | Return response without reload | `search` |
| Navigation/render/challenge exception | Any attempt | Raise, never reclassify as absence | `_search_attempt`; known failures become native error responses in `_capture_query` |

Polling budgets begin when polling starts, **not** when a delayed container arrives.
They are not whole-query wall-clock deadlines: DOM operations and 400ms sampling
can cross the boundary. Navigation has its own `timeout_ms`; the initial page wait
is `min(1500ms, timeout_ms/4)`. Optional “show more” has a 1500ms click timeout and
500ms wait. CAPTCHA handling is separate: at most two token/form attempts, with
enterprise/classic task variants and default 5s polling / 180s wait per variant
(30s HTTP timeout). These are potentially paid solver tasks, not SerpAPI retries.

**Preserved policy caveat:** the reload guard also includes a confirmed
`unavailable` response because it has a shell but no `ai_overview`. An `empty`
overview object, conversely, does not trigger reload. Tests characterize both
key-presence and shell guards. Changing this policy is not part of the structural
refactor. The final full-page serialization is a later DOM observation than the
poller's last sample; this refactor does not redesign that race.

### Capture, progress and persistence

| State / event | Guard / budget | Next / terminal outcome and effects | Functions |
| --- | --- | --- | --- |
| SerpAPI start → first-page result | Valid configuration | Exactly one fixed-endpoint search; shape/API/transport failure → `error` with redacted evidence | `fetch_search`, `_request_json` |
| First page → lazy AIO | Page token and no text blocks | One follow-up only; usable nonempty overview → `ok`; invalid/error/transport/no blocks → `unresolved`, retaining initial organic data/evidence | `_lazy_overview`, `_resolved_overview`, `_capture_result`; per-request timeout, no retries/pagination |
| Custom extraction → capture classification | Normalized AIO status | present/absent/unavailable → `ok`; empty/unresolved → `unresolved`; invalid/error → `error` | `capture_status_for_aio`; used by native adapter and comparison eligibility |
| Pair captures finish | Both source outcomes `ok`, alignment valid | Compare scoped structural inventories, not scalar wording/rank identities; otherwise no gap claims | `_capture`, `compare_main_body`; alignment/comparison errors retain legacy attribution to SerpAPI source |
| Query/pair finishes → progress advances | Native response(s) retained | Append query occurrence, then checkpoint before next query | `collect_batch`, `_capture`; IDs are positional, repeat rounds reuse query IDs but not snapshot IDs |
| Native save fails/interrupted | Replacement not completed | Prior JSON retained; incomplete envelope cannot upload | `_save_snapshot`; first file created exclusively, updates use temp + replace (failed temp may remain) |
| Pair checkpoint | Three native/report files | Each file atomic, **not** a transaction across all three | `_write_json`; saved checkpoints can differ if interrupted between files |
| Pair normal exit | All queries processed | Report `complete` or `partial_failure`, then atomic records export | `_capture`, `export_records`; one provider record per observation |
| Ordinary pair run exception | `Exception`, not cancellation/KeyboardInterrupt | Report `aborted`; checkpoint and try local export; no paid recapture | `_capture`; failed final export gets existing local-only second attempt |
| Cancellation / KeyboardInterrupt | Not caught as per-query failure | Last checkpoint remains; pair report can remain `running`; browser context unwinds | Both controllers; no automatic resume |
| Native snapshot → storage | Complete planned count, valid IDs/content | Preserve run UUID and input order; incomplete/invalid → `SnapshotError` | `normalize_response_snapshot` → `normalize_records` → `_response_row` |
| Shared records → storage | Valid provider/repeat/query/snapshot identities | UUID5 provider/repeat runs, lexical query-ID order; explicit capture status retained | `normalize_records`; missing capture status keeps existing shared-record default, not native inference |
| Stored query projection | Capture `ok` AND AIO present/absent/unavailable | `status=ok`; otherwise `status=error`, preserving separate capture/AIO fields | `_response_row`; only `scrape_runs` / `queries` rows |
| Local replay/export | Valid saved input, new output paths | No browser, API or database calls; full ordered JSON, SQL and manifest as applicable | `load_input`, `export_records`, `comparison_storage.main`, `sql_export` |
| Optional upload | Complete validated payload | One parameterized atomic statement for **whole upload**, then receipt validation | `upload_snapshot` / `upload_records` → `_post_sql`; native exceptions vs comparison `ValueError` unchanged |
| Upload response missing/invalid/mismatched or HTTP failure (including 413) | One request attempted | Failure/uncertain commit; retain artifacts/UUID; never retry or fall back to chunks | Receipt validators and command adapters |
| Browser normal exit / query failure / cancellation | Resources owned | Close browser/context, always stop Playwright | `__aexit__` |
| Browser launch/page creation fails | Partial startup ownership | Clean up, then re-raise original contract | `__aenter__`; separate resource-leak correction, not capture-policy change |

Native errors use stronger URL-query stripping in diagnostics than comparison
content redaction. The controllers retain those distinct error envelopes and JSON
publishers rather than unifying them into a generic persistence layer. Command
exit codes remain 0 clean, 1 retained query failures, 2 ordinary configuration,
validation, I/O or aborted-run failures. Interrupts retain their existing Python
exception behavior. Native upload receipts use logical content counts; shared
upload receipts use table-row counts. An `uploaded=False` manifest is not proof
that a failed remote request rolled back.

## Two short walkthroughs

**Successful query:** follow `collect_batch` → `_capture_query` → `search` →
`_search_attempt`. `_aio_state` sees an answer; `_advance_aio_wait` waits for two
seconds of stable blocks/citations. `parse_search_html` returns `present`, so no
reload. The session then observes one redirect response per unique opaque link,
keeping originals as `redirect_link`; unresolved links stay opaque and counted.
The native response is checkpointed with its positional query ID.
`_capture_summary` validates the complete envelope; optional `upload_snapshot`
projects the entire response into one `queries` row and checks its receipt.
For paired capture, follow `_capture_source` for each provider, the comparison
guard, `_pair_records`, checkpoint, then `export_records`. Saved records can
instead go through `google-aio-sql` without recapture.

**Unresolved query:** a generating shell consumes the 20s polling budget;
`_finish_aio_wait` returns shell-seen `True`, while the parser returns
`unresolved`. `search` navigates once more with a 30s poll budget. If unfinished
again, that response is retained, not rewritten as absence. Native storage sets
`capture_status=unresolved`, `aio_status=unresolved`, `status=error`; a complete
batch can persist this failed observation and exits 1. Paired capture still calls
SerpAPI once (plus its optional lazy follow-up), skips comparison for the failed
pair, and exports both observations. A disappearing shell or challenge failure
instead raises a capture error. In every case, upload is optional and never
retries; uncertain upload recovery starts by checking the retained UUID, not by
scraping again or inventing another ID.

## The HTTP 413 operational exception

`query_test/upload_pap_chunked.py` is a one-off operational workaround, **not**
the core upload implementation. Its docstring and `pap-300-final.upload.json`
record the whole-upload 413. `pap-300-final.chunked-upload.log` records a separate
run insert and 30 chunks of 10 query rows. Earlier chunks stay committed if a
later one fails; reruns select existing query IDs and send missing ones. Only
each statement/chunk is atomic. The final verification log reports 300 responses,
292 AIOs, 2705 references, 2684 organic results and zero errors. That historical
receipt is a cross-check, not current service verification or field-level proof.
No chunking, resume, overwrite or retry policy has been moved into core uploads.
