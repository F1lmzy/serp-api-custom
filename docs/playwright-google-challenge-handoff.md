# Handoff: Playwright Google Search and the CAPTCHA challenge

Observation date: 2026-09-28. All findings below come from ad-hoc diagnostics in this session, not from a committed test suite. Treat them as observations with a timestamp, not guarantees about Google's behavior.

## What the scraper does today

`aio_scraper.py` runs one query per invocation with headless Playwright Chromium:

- Navigation URL is built in `scrape()` (`aio_scraper.py:245`):
  `https://www.google.com/search?q={quote_plus(query)}&hl={quote_plus(hl)}&gl={quote_plus(gl)}`
- Navigation timeout defaults to 30000 ms (`aio_scraper.py:232`, `--timeout`).
- It waits `min(1500, timeout_ms // 4)` ms, optionally clicks a "Show more AI Overview" button, then reads `page.content()`.
- Parsing is handled by `parse_search_html` (`aio_scraper.py:78`).

Expected markers on a genuine results page:

| Marker | Meaning |
| --- | --- |
| `#search`, `#rso` | Real SERP container |
| `#eKIzJc` | AI Overview container |
| `[data-subtree="aimc"] [jsname="KFl8ub"]` | Primary answer body |
| `[data-subtree="aimfl,mfl"]` | Fallback answer marker seen in older captures |

The scraper has no CAPTCHA solving and no retry logic. It does not load `.env` itself; proxy settings come from exported environment variables (`_proxy_settings`, `aio_scraper.py:202`).

## The challenge page we hit

A one-off headless, direct (no proxy) Playwright Chromium request for an Everest query returned **HTTP 200** but landed on `https://www.google.com/sorry/index` rather than `/search`. Only origin and path were recorded; challenge query parameters were not captured.

DOM facts from that page:

- `#captcha-form` present (`captchaForm: true`).
- reCAPTCHA with two iframes observed initially (`recaptchaIframes: 2`).
- Iframe metadata:

| title | host | path |
| --- | --- | --- |
| `reCAPTCHA` | `www.google.com` | `/recaptcha/enterprise/anchor` |
| `null` | `null` | `blank` (transient) |
| `recaptcha challenge expires in two minutes` | `www.google.com` | `/recaptcha/enterprise/bframe` |

The iframe paths (`/recaptcha/**enterprise**/anchor` and `/bframe`) plus the visible checkbox indicate a reCAPTCHA **Enterprise** widget. This is the interactive checkbox flow, not reCAPTCHA v3.

## Sitekey observation

- A 40-character sitekey was read from `[data-sitekey]` (`keySource: 'data-sitekey'`).
- Only a mask was retained: prefix `6LdLLI`, suffix `LqRO`. **The full key was never captured or stored, and must not be reconstructed or guessed.**
- The key belongs to the challenge page. A sitekey alone does not show that any third-party solver can clear this challenge.

## Limited audio-challenge diagnostic

Clicked `#recaptcha-anchor` in the anchor frame, then `#recaptcha-audio-button` in the bframe:

- Checkbox click succeeded; audio button was present, visible, and clicked.
- `#audio-source` never appeared (wait raised `TimeoutError`).
- Challenge messages rendered: `Try again later`, and `Your computer or network may be sending automated queries. To protect our users, we can't process your request right now. For more details visit our help page.`
- Network responses observed: `200 www.gstatic.com /recaptcha/api2/audio_2x.png` and `200 www.google.com /recaptcha/enterprise/payload`.

Interpretation: the audio control was reachable, but **no usable audio source was served**. The `payload` 200 is not evidence of a usable audio challenge. No answer was submitted, no third-party solver was called, and no credits were spent.

## Transports are not interchangeable

These are different setups; do not mix their results:

| Setup | Observed outcome |
| --- | --- |
| Headless Playwright Chromium via `aio_scraper.py` CLI, direct | HTTP 200 `/sorry/index` challenge (facts above) |
| Playwright Chromium through DataImpulse proxy | Google navigation timed out before a response |
| Playwright Firefox through DataImpulse proxy | Reached Google, received HTTP 429 `/sorry/index` |
| Visible installed Chrome, Playwright-controlled, direct, isolated profile | 100-query trial, queries 1-30: 30 AIO extracted, then challenge at index 31 |
| Visible installed Chrome, same isolated direct profile, driven by Apple Events for the continuation | Queries 32-73: 42 AIO extracted, then challenge at index 74 |

The two visible-Chrome rows are the same isolated, direct profile; only the control channel differs (Playwright for the first run, Apple Events for the continuation). Together they produced 72 live AI Overviews, with challenges at indexes 31 and 74. The 30 Playwright-controlled extractions are the only ones here where Playwright drove a live AI Overview capture; the headless `aio_scraper.py` CLI never reached a real SERP, so it still has no end-to-end live extraction.

## Solver-contract caveat

- 2Captcha's v2 Enterprise page describes `RecaptchaV2EnterpriseTaskProxyless` / `RecaptchaV2EnterpriseTask` with `websiteURL`, `websiteKey`, and optional `enterprisePayload`: https://2captcha.com/api-docs/recaptcha-v2-enterprise
- The observed widget is the Enterprise checkbox flow, so the v3 task type does not match it. The provider documents returning `gRecaptchaResponse`, but **nothing in those docs shows that a returned token clears this Google `/sorry` challenge.** That remains unverified and should not be assumed.

## Secrets and artifacts

- `.gitignore` ignores `.env` and `.env.*` (with `!.env.example`), so real credentials stay out of git.
- Do not put real keys, full sitekeys, cookies, or challenge URLs into docs, logs, or test fixtures. Use masks like the one above.

## Open questions for whoever picks this up

1. Is the challenge stable across queries and sessions, or query/timing dependent?
2. Does the challenge page expose an `enterprisePayload` (`s` value) or other required fields? Not captured.
3. Does any documented solver token actually clear Google's `/sorry`? Unverified.

## Resolution log (2026-09-28, follow-up session)

The 2Captcha integration is implemented in `aio_scraper.py` and verified live.

### What was built

- `.env` auto-loading (`_load_env_file`); exported vars still win.
- Challenge detection (`is_challenge_page`), parameter extraction
  (`extract_challenge_params`: sitekey from `[data-sitekey]` or anchor iframe `?k=`,
  Enterprise flag, `data-s`, `s` value).
- 2Captcha client mirroring the official `2captcha-python` lib:
  `in.php` with `method=userrecaptcha`, `enterprise=1`, `googlekey`, `pageurl`,
  **plus the Google-Search-specific context params**: `data-s`, `cookies`
  (our browser cookies), `userAgent`. `res.php` (json=1) returns the token
  and optionally the worker's cookies, which we adopt into the browser
  context before submitting.
- Token injection into `#g-recaptcha-response` inside `#captcha-form`, then
  direct form submit (widget callback intentionally not fired; it can reset
  the textarea).
- Engine selection: `GOOGLE_AIO_BROWSER=chromium|firefox`. Playwright
  **Chromium reproducibly hangs through DataImpulse**; Firefox works.
  `GOOGLE_AIO_HEADLESS=0` for a visible window.
- Persistent profile (`GOOGLE_AIO_PROFILE`, default `.aio-profile/`,
  gitignored): Google session cookies survive across runs, so one solved
  challenge covers subsequent searches — runs 2+ in the verified session
  hit no challenge at all. Set `GOOGLE_AIO_PROFILE=off` for a fresh session
  per run.

### Verified results (live runs)

- Challenge served on every headless navigation (curl on the same IP got a
  real SERP; the challenge is fingerprint-triggered, not purely IP).
- Tokens from earlier attempts without cookie/UA alignment were rejected
  (POST `/sorry/index` returned **429** and re-served the challenge).
- After cookie/UA alignment: direct (VPN off) solved 2/3 queries; via
  DataImpulse + Firefox solved 3/3 queries, each landing on the real SERP
  with AI Overview extracted.

### Answers to the open questions

1. The challenge appears on every headless navigation in this environment.
2. `data-s`/`s` extraction is implemented; the captured challenge pages did
   not expose an `s` value (both empty). Works without them.
3. Yes — **with session-context alignment**. A bare enterprise token is
   rejected; sending our cookies + UA to the worker (official-lib params)
   makes tokens clear the challenge.

### Cost note

Every solve attempt costs 2Captcha credit even when Google rejects the
token afterwards. Failed-attempt diagnostics above spent ~10 solves.

### Batch usage

```bash
# one query per line; # comments allowed
google-aio --queries queries.txt --out results.csv --delay 2.0 --proxy direct
```

One browser session is reused across all queries. The CLI now exports CSV
with AI Overview and organic rows. Query failures emit an `error` row and
the run continues, with exit code 1 if any query failed. See `README.md` for
the current CSV columns and proxy selection options.

## Proxy and organic extraction fixes, 2026-09-30

The configured Chromium run timed out after 900,000 ms through DataImpulse.
Keeping the same profile and code but disabling the proxy reached a real
SERP in 77.2 seconds, including one CAPTCHA solve. A warm-session repeat
took 3.5 seconds. This isolated the navigation failure to the proxy path;
the existing profile did not need deleting.

Connections now default to direct even with saved DataImpulse credentials.
Use `GOOGLE_AIO_PROXY=dataimpulse` or `--proxy dataimpulse` to opt in.
`--proxy direct` overrides the environment without deleting credentials.
Chromium plus proxy logs a warning; there is no silent engine or transport
fallback. Engine-specific `.aio-profile*` directories are gitignored.

The observed organic result links used opaque Google `/goto?url=...`
redirects. The parser now preserves these URLs instead of rejecting them
as internal Google links. It does not follow redirects or invent destination
URLs. The saved live page yielded eight organic results after the fix.

Verification with unchanged `.env`, headless Chromium, and the existing
`.aio-profile` directory:

- `query_test/results_fixed_20260930_033348.csv`: 28 queries, zero errors,
  25 with AI Overviews, all 28 with organic results, 245 organic rows.
- Wall time: 169.4 seconds, exit 0, no CAPTCHA solves logged during this batch.
- All 45 unit tests pass, including `/goto` extraction and single/batch
  proxy-selection precedence.
