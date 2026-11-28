# Deploy the public Search Archive to Cloudflare Workers

The Worker hosts the existing HTML/CSS/JavaScript and a read-only Supabase API.
**There is no viewer authentication: anyone can browse every stored run and
query, inspect full raw responses, and download data through the public API.**
Do not deploy this version against data that needs to stay private.

It does not run the Python scraper, require a tunnel, keep your laptop online,
or spend Google/SerpAPI/proxy/CAPTCHA credits. It uses the existing private
`queries` and `scrape_runs` schema; no migrations or public database grants are
added. The Worker, not the browser, holds the Supabase credential.

**Live address: `https://cometlab.srirajkavin.com`.** A subdomain leaves
any existing personal website and email at the main domain alone. Use the same
chosen hostname for `APP_HOSTNAME` and the custom domain.

## What remains protected

- The site and read-only API are deliberately public. No Cloudflare Access
  application, JWT, email login, account or authentication variables are needed.
- The database secret is a **runtime Worker secret**, never a frontend/build
  variable or committed file. It is sent only to the fixed HTTPS Supabase origin;
  upstream redirects are not followed. Do not make the Supabase tables public.
- Only fixed-table/column GET queries are implemented. Arbitrary SQL, URLs,
  mutations and third-party asset fetches are not exposed.
- Only three files in `aio_viewer/static` are uploaded as assets. `.env`, Python
  sources, filesystem captures, private fixtures and documentation are not
  hosted. **Database-stored responses ARE exposed through the public API.**
- HTTPS and the configured hostname are required. `workers.dev` and preview
  URLs stay disabled so the custom domain is the intended public entry point.
  These restrictions are not data access control.
- Responses retain no-store headers, CSP and safe text/link rendering. No CORS
  is enabled and cross-site browser API requests are rejected, but any direct
  HTTP client can read the API. CORS, no-store and unlisted URLs do not make it
  private or prevent copying/indexing.
- The Worker does not log queries or results; automatic Worker observability
  is disabled. Cloudflare retains its own platform/network telemetry and any
  organization-configured logs.
- Public traffic can consume Workers requests, Supabase bandwidth and database
  resources. Monitor usage and configure appropriate plan limits/alerts and
  Cloudflare rate limiting/WAF rules if needed. This code has no built-in
  per-client rate limit or authentication.

## 1. Activate the domain in Cloudflare

If not already active, add `srirajkavin.com` to the Cloudflare account hosting
this Worker. Preserve existing website, MX/email and verification DNS records,
then set the assigned nameservers at your registrar. Wait for the zone to become
**Active**. Domain-registration transfer is not required.

No laptop IP, port forwarding or tunnel is needed.

## 2. Connect this repository as a Worker (not Pages)

In **Workers & Pages → Create application → import/connect a Git repository**:

| Setting | Value |
| --- | --- |
| Repository | `F1lmzy/serp-api-custom` |
| Production branch | `main` |
| Worker/application name | **`search-archive`** (matches `wrangler.jsonc`) |
| Root directory | Repository root (`/`, or leave blank) |
| Build command | `npm ci && npm run check && npm test` |
| Deploy command | `npx wrangler deploy` |
| Node version | `.node-version` pins **22.22.3**; set build `NODE_VERSION=22.22.3` if needed |
| Preview/non-production builds | **Disable** |

Cloudflare may already have installed dependencies; the explicit `npm ci` is
safe and ensures the committed lockfile is used. No frontend framework or
output-directory setting is needed: Wrangler bundles `worker/index.js` and
uploads the three assets.

The tests exercise the actual bundle in Cloudflare's local `workerd` runtime,
using anonymous requests, synthetic database rows and intercepted network
traffic. No production credentials or Cloudflare account are needed for tests.

The initial deploy has **no public route** until you attach the custom domain.
The repository does not create an account, zone, DNS record or hosted deployment
by itself.

## 3. Add production RUNTIME variables and secret

Open **Workers & Pages → search-archive → Settings → Variables & Secrets**.
These are **not** build variables/secrets under Settings → Build.

| Name | Type | Value |
| --- | --- | --- |
| `APP_HOSTNAME` | Plain text | `cometlab.srirajkavin.com` (no scheme, port or path) |
| `SUPABASE_PROJECT_ID` | Plain text | Your existing 20-character project reference |
| `SUPABASE_SECRET_KEY` | **Secret/encrypted** | Your existing server-side Supabase secret/service-role key |

Use the key from Supabase's project API settings, not a publishable/anonymous
key and not the personal `SUPABASE_ACCESS_TOKEN` used by the scraper's upload
commands. The private Data API must be enabled, with the existing read grants
and consolidated JSONB schema already applied.

Save/deploy these runtime settings. `keep_vars: true` preserves dashboard-managed
plain variables across future Wrangler deploys; secrets are stored separately.
Do not commit credentials to Wrangler, `.env`, repository variables or GitHub.
Named environments need their own bindings/variables; they are not automatically
inherited.

### If you followed the earlier authentication instructions

- Remove the Cloudflare Access application protecting this viewer's hostname,
  or remove this hostname from that application. Check for any wildcard Access
  application that still covers it. Do not remove protection from unrelated
  private applications. A dashboard Access policy can still demand login even
  though the Worker no longer authenticates visitors.
- Remove the unused `CF_ACCESS_TEAM_DOMAIN` and `CF_ACCESS_AUD` runtime variables.
- Deploy the current `main` commit; the old Worker version still requires JWTs.
- Verify in a private window without Access cookies. No Zero Trust setup is
  required for this public version.

## 4. Attach the custom domain

1. Open **search-archive → Settings → Domains & Routes → Add → Custom Domain**.
2. Enter **`cometlab.srirajkavin.com`** and confirm.
3. Wait for DNS and the HTTPS certificate to become active.
4. Enable the zone's **Always Use HTTPS** setting if HTTP is not already
   redirected. The Worker itself refuses plaintext HTTP.

Cloudflare creates the domain's Worker DNS record and certificate. Resolve any
existing conflict for **that subdomain only**; do not delete unrelated root or
email records.

There is intentionally no `routes` key in Wrangler, so future deploys preserve
dashboard-managed custom domains. If you manage routes in code later, keep the
custom domain and `APP_HOSTNAME` aligned.

## 5. Verify the public deployment

Open `https://cometlab.srirajkavin.com` in a private browser window:

- The viewer must open **without any login**.
- Browse runs and filters, compare captures and domains, open citations and inspect JSON.
- This unauthenticated request should return run data with status 200:
  `curl -i https://cometlab.srirajkavin.com/api/runs`.
- `workers.dev`, Version URLs and preview addresses remain disabled.
- JSON replies should have `Cache-Control: private, no-store`; this is a caching
  policy, not a claim that the public data is confidential.
- Frontend source/network responses must not contain the Supabase secret.
- POST/PUT/PATCH/DELETE requests must be rejected; the API is read-only.

Account-specific DNS/hosting checks cannot be completed locally before your
Cloudflare setup. Local runtime/browser tests do not prove the hosted domain
is deployed correctly.

### Troubleshooting

| Symptom | Check |
| --- | --- |
| No public URL after initial import | Expected. Add the custom domain and configure runtime settings. |
| 503 requesting `APP_HOSTNAME` or database settings | Missing **runtime** variables; build-only settings do not count. |
| 403 hostname not enabled | Custom domain and `APP_HOSTNAME` must match exactly. |
| Still asked to log in | Remove viewer-specific/wildcard Access coverage and deploy the current public Worker version. |
| Supabase denied access / 502 | Check secret, project ID, private Data API grants and schema. Do not broaden public database access. |
| No same-query match | Exact saved text, language and country must match; a missing capture is not AIO absent. |
| Build application-name mismatch | Use `search-archive`, or change the name in both Wrangler and Cloudflare. |
| Local `npm run dev` cannot load the viewer | Production hostname/HTTPS restrictions still apply. Use the isolated fixture checks below. |

## Viewer capabilities and matching rules

The hosted frontend includes archive pagination, query-text search, AIO status
(including exact absent versus uncertain/failure states), capture status,
language/country/device, inclusive UTC capture dates, warning and organic-result
filters. Filters execute server-side **before** pagination and reset the page.

Open a response and choose **Domain comparison** to compare the saved AI
Overview references and top-level organic results from that capture. Domains
are grouped as shared, AI Overview only, or organic only. Expand one to see its
links, citation indexes, and ranks; positions that weren't saved are labeled as
saved order, not inferred ranks. Matching ignores a leading `www.` but keeps
other subdomains separate. Sitelinks are excluded. The viewer uses saved
redirect destinations or explicit targets in known Google URL wrappers; opaque
redirects and invalid links are omitted and counted, never fetched.

Choose **Compare runs** to compare separate captures instead. Matches use exact text + language +
country, not query ID or row position. Device/domain may differ and are shown.
Multiple target matches require explicit selection, with independent pagination.
Left-hand archive filters do not hide target responses with different statuses.
Both sides retain full saved blocks, lists, tables, source/citation mappings
(including zero/unmapped indexes), organic results, sitelinks and raw JSON.

The change summary compares exact saved JSON (ignoring object-key order but not
array order). URL changes use exact strings and show all duplicate positions;
`order N` means a position was not saved. They are not semantic AI scores, fuzzy
query matches, canonical-URL normalization, or invented Google ranks. Missing
emphasis/media remains missing; the page is not an exact historical screenshot.

## Local verification and optional CLI deployment

```sh
npm ci
npm run check
npm test
npm run build                 # dry run only; does not publish anything
```

Tests cover anonymous assets/API without cookies, JWTs or Access configuration;
fixed read-only transport and credential isolation; route/filter/date validation;
pagination and exact query matching; missing configuration and alternate hosts;
security headers and upstream secrecy; and the bundled public Worker in the
real local Workers runtime. All runtime network traffic is intercepted.

Optional anonymous Chromium end-to-end check (Python Playwright + Chromium):

```sh
python worker/test/browser_check.py --browser /absolute/path/to/chrome
```

This starts a **loopback-only synthetic database bridge** into the production
bundle. No authentication headers are injected. Screenshots go to
`/tmp/cloudflare-worker-desktop.png` and `/tmp/cloudflare-worker-mobile.png`.
Test-only code and Miniflare are not deployed. Unit/runtime checks also run in
GitHub Actions; the existing scraping environment is unrelated.

For manual deployment instead of Git integration:

```sh
npx wrangler login
npx wrangler deploy
npx wrangler secret put SUPABASE_SECRET_KEY
```

Then configure the two plain runtime variables and custom domain as above.
Wrangler's deployer login is unrelated to visitor authentication.
`wrangler secret put` publishes a new Worker version. Do not paste secrets into
shell command arguments, source files or chat.

Future pushes to the connected production branch
trigger Cloudflare builds. Check build results before treating updates as live.

## Official references

- [Workers Builds configuration](https://developers.cloudflare.com/workers/ci-cd/builds/configuration/)
- [Static asset binding and Worker-first routing](https://developers.cloudflare.com/workers/static-assets/binding/)
- [Workers custom domains](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/)
