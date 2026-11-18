# Offline refactor verification

**Historical record.** This page is a snapshot of one offline refactor
verification, performed before the current documentation reorganization.
It records evidence observed at that time — the test suite has grown since,
and current counts are reported by running the suite as shown in the README.
It is retained as evidence of that refactor, not as a statement of current
verification or remote health.

## Scope

Runtime remains eight modules with `google-aio`, `google-aio-compare`,
`google-aio-export` and `google-aio-sql`. No dependencies, migrations, storage
schema, saved captures or operational upload scripts changed. Existing README /
changelog edits, Python/uv files and untracked pilot inputs were preserved.
The [state-flow map](state-flow.md) ties responsibilities, guards, budgets,
terminal outcomes and side effects to the actual functions.

## Tests, before and after

Command: `.venv/bin/python -m unittest discover -s tests -v`.
All commands here were bounded to 30 seconds; none timed out.

| Stage | Tests run | Passed | Skipped | Failures/errors | Observed duration |
| --- | ---: | ---: | ---: | ---: | ---: |
| Original runtime/tests | 224 | 215 | 9 | 0 | 0.878s |
| Added characterization, before moving responsibilities | 10 focused | 10 | 0 | 0 | 0.253s |
| Structural refactor plus characterization | 234 | 225 | 9 | 0 | 0.983s |
| Final, including separate resource-cleanup correction | 254 | 245 | 9 | 0 | 1.096s |

There were **no existing suite failures**. The missing-file JSON message printed
by `test_search_export` is an intentional error-path fixture, not a test failure.
The nine skips are the opt-in `RealFirefoxAioTests`: computed visibility, delayed
container, hidden/prompt-only content, no-container window, active generation,
reload exhaustion, transient-error recovery, stable loading, refusal distinction.
They were **not run**; permission is required even though they block external
network access.

The new cleanup tests were first run against the old lifecycle implementation:
three failed (launch did not stop Playwright, page creation did not close the
browser, exit retained `_pw` ownership). Those failures were separately corrected,
without changing capture/reload policy or exception types. Startup cleanup does
not replace the original error if cleanup itself fails.

### Transition and fault-injection evidence

- `tests/test_aio_streaming.py`: existing synthetic DOM / monotonic-clock tests
  cover delayed containers, partial active generation, stabilization, growing
  answers, disappearing containers and reload exhaustion. Added citation-key
  changes and a stale-session mirror check. Two existing mocks now return an
  explicit `_SearchAttempt` instead of preloading browser-session state.
- `tests/test_state_transitions.py`: pure transition guards, immutable state,
  generation resetting stabilization, 8s/smaller absence window, deadline
  outcomes and reload guard; mocked Playwright launch/page/close failures,
  cancellation, repeated cleanup and persistent-context page reuse.
- `tests/test_state_flow.py`: synthetic full search uses exactly two navigations
  and 20s/30s poll budgets on exhaustion; generation recovery followed by clean
  next-query absence; disappearing shell/challenge failure never reloads;
  characterized confirmed-unavailability reload policy. Tests also assert native
  and paired interrupted-checkpoint retention, session exit, no upload of partial
  envelopes, failed replacement retaining prior JSON, pure pair record conversion,
  persistent export failures without paid recapture, and uncertain native/shared
  uploads retaining the original file, UUID, SQL, manifest and no success receipt.
- `tests/test_serpapi_client.py`: one initial request plus at most one lazy
  follow-up, transport/shape/API failures, single-pass error-evidence redaction,
  and no cross-query follow-up state after failure. `tests/test_captcha.py` pins two token/form attempts,
  missing-key short circuit, explicit solver failure and no polling on challenge
  failure at the search boundary.
- `tests/test_response_storage.py` / existing storage tests: mocked HTTP and SQL
  executor assert one whole-upload statement, unchanged exceptions/receipts,
  redirect refusal, no automatic retries after uncertain responses, and explicit
  HTTP 413 refusal without a chunk fallback. Existing tests retain identity,
  order, redaction, full nested content and two-table contracts.

Temporary session logs: `/tmp/aio-refactor-before.log`,
`/tmp/aio-characterization.log`, `/tmp/aio-refactor-structural.log`,
`/tmp/aio-cleanup-before-fix.log`, `/tmp/aio-refactor-after.log`.
These are ephemeral verification artifacts, not committed evidence or backups.

## Saved native inputs: complete before/after equality

Before editing runtime code, `tests/verify_saved_equivalence.py` saved an oracle
under `/tmp/aio-refactor-equivalence-before.json`. After editing, it recomputed
and compared entire JSON documents and SQL strings, **not just counts/hashes**.
No `.env` is loaded; environment credentials are cleared and browser/HTTP entry
points are patched to fail. Outputs stay under `/tmp`; inputs are read only.

| Local saved file under `query_test/` | Observations | Normalized AIO statuses | SQL projection |
| --- | ---: | --- | --- |
| `pap-300-final.json` | 300 | 292 present, 8 absent | All accepted; native and shared paths |
| `pap-300-direct.json` | 300 | 281 present, 8 absent, 2 unresolved, 9 error | All accepted; failures retained |
| `pap-300-retry.json` | 11 | 7 present, 4 unresolved | All accepted |
| `pap-300-retry2.json` | 4 | 4 present | All accepted |
| `pap-300-serpapi.native.json` | 300 | 287 present, 13 absent | 299 accepted, same existing rejection below |

These are 915 observations across files, including repeated query occurrences,
not 915 independent queries. Equality covers:

- Every normalized field, full text, recursive lists/tables, citation indexes,
  references, organic metadata and array order.
- Complete provenance (source names, query/snapshot/provider/repeat identity,
  time/settings), statuses, errors and warning arrays.
- All 914 accepted shared projections and all 615 native snapshot response rows,
  their complete run metadata/fingerprints, and SQL text. SQL hex payloads are
  decoded and checked against the complete two-table documents too.
- Original source-byte SHA256 values, so an edited input cannot mask a mismatch.
- The exact exception type/message/query ID of rejected projections, rather than
  dropping them silently.

Reproduction with new temporary output (the `--save` step must precede runtime
changes to serve as a before/after oracle):

```sh
PYTHONPATH=. .venv/bin/python tests/verify_saved_equivalence.py --save /tmp/NEW-aio-before.json
# After the change:
PYTHONPATH=. .venv/bin/python tests/verify_saved_equivalence.py --check /tmp/NEW-aio-before.json
```

Saved evidence is local/untracked and may not exist in a fresh clone, so this is
an explicit verification utility, not an automatically skipped unit test. Its
latest output is `/tmp/aio-refactor-equivalence-after.log`.

The final native projection also matches the historical verification log's
300 queries / 292 AIOs / 2705 references / 2684 organic results and fingerprint
`b648d2fcc853d161710176c0241b282595268bb0d1ddb230c5f31d60c7425115`.
That is only an offline cross-check, **not** a fresh database read.

## Additional checks and limits

- All eight runtime modules compiled with `compileall`; bytecode output used
  `/tmp/aio-refactor-pyc`.
- All four installed `.venv/bin/google-aio*` commands returned successfully for
  `--help`, with help text redirected to temporary files.
- `git diff --check` passed.
- No live Google, CAPTCHA, SerpAPI or database requests, data uploads, migrations,
  credential edits, saved-capture edits or real-browser checks were performed.
  PostgreSQL execution and current remote state are **unverified** by this work.

Known behavior kept separate from structural changes:

1. SerpAPI `PAP19-05-V2` in the saved native file already fails SQL projection
   with `SnapshotError: content link must be an absolute http(s) URL`. The
   normalized record is compared completely; the rejection is unchanged. The
   verifier projects the other 299 records separately for equivalence, **not** as
   a recommendation to upload a silently reduced dataset. The full input is not
   claimed uploadable. No URL repair/data loss was introduced.
2. Confirmed `unavailable` still gets the existing one reload; an `empty` object
   does not. A later full-page serialization can observe a DOM different from
   the poller's final sample. Neither policy/race was silently changed.
3. Native checkpoints use a fixed temporary path for replacement (which may
   remain after a failed write). Paired files are atomic individually, not as a
   group. Cancellation can leave the last report marked `running`. No new
   resume/recovery orchestration was added.
4. Core whole-upload atomicity remains different from the operational 413
   workaround's per-chunk atomicity. HTTP/receipt failure remains uncertain;
   `uploaded=False` is not proof of server rollback. No retry/chunk redesign.
