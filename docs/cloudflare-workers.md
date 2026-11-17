# Deploy Search Archive to Cloudflare Workers

The Worker hosts the existing HTML/CSS/JavaScript and a read-only Supabase API.
It does not run the Python scraper, require a tunnel, keep your laptop online,
or spend Google/SerpAPI/proxy/CAPTCHA credits. It uses the existing private
`queries` and `scrape_runs` schema; no migrations or public grants are added.

**Recommended address: `https://archive.srirajkavin.com`.** A subdomain leaves
any existing personal website and email at the main domain alone. You can use
the apex `srirajkavin.com` instead, but that replaces the website currently
attached to that hostname. Use the same chosen hostname in every step below.

## Security model — read this first

The local Python viewer relies on a private network. The hosted Worker instead
requires **Cloudflare Access on the entire hostname**, plus independent JWT
signature/issuer/audience/expiry validation inside the Worker, before serving
any HTML, JavaScript, CSS or database response. Missing configuration fails
closed. There is no development authentication bypass or trusted-email-header
shortcut.

- `workers.dev` and preview URLs are disabled in `wrangler.jsonc`.
- Only three files in `aio_viewer/static` are uploaded as assets. `.env`, Python
  sources, raw captures, private fixtures and documentation are not hosted.
- The database secret is a **runtime Worker secret**, never a frontend/build
  variable or committed file. It stays on the Worker and is sent only to the
  fixed HTTPS Supabase project origin. Upstream redirects are not followed.
- Only fixed-table/column GET queries are implemented. Arbitrary SQL, URLs,
  mutations and third-party asset fetches are not exposed.
- Responses are private/no-store, without CORS, and retain the viewer's CSP and
  safe text/link rendering. The Worker does not log queries, tokens or results;
  automatic Worker observability is disabled by default. Cloudflare still has
  its own platform/network telemetry and your organization's configured logs.
- Every user permitted by the Access policy can read the entire stored dataset.
  There is no per-run or per-user row authorization in this version. Anyone
  with administrative deployment/secret access must also be trusted.

## 1. Activate the domain in Cloudflare

If not already active, add `srirajkavin.com` to the Cloudflare account that will
host the Worker. Carefully preserve existing website, MX/email and verification
DNS records, then set the assigned nameservers at your registrar. Wait for the
zone to become **Active**. Domain-registration transfer is not required.

Do not add an A record pointing to your laptop, turn on Tailscale Funnel, or
make the Supabase tables public.

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

Cloudflare may have already installed dependencies automatically; the explicit
`npm ci` is safe and ensures the committed lockfile is used. There is no frontend
framework/output-directory setting: Wrangler bundles `worker/index.js` and
uploads the three assets. The tests also build the bundle and exercise it in
Cloudflare's local `workerd` runtime, using synthetic signed tokens/database
rows and intercepted network traffic. No credentials are needed for the build.

The initial deploy intentionally has **no public route**. Do not enable a
`workers.dev` address to work around that. No Cloudflare account ID, domain
ownership, Access application, DNS record or actual hosted deployment is created
by the repository itself.

## 3. Create Cloudflare Access protection BEFORE attaching the domain

In Cloudflare Zero Trust:

1. Open **Access controls → Applications → Add application**.
2. Choose the self-hosted/public-hostname application type (some dashboard
   versions call it **Self-hosted and private**).
3. Enter `archive.srirajkavin.com`. Protect the **entire hostname**, with no path
   restriction. In particular, `/api/*` must not be excluded.
4. Configure an identity provider or email one-time PIN login.
5. Create an **Allow** policy for your exact email address and any explicitly
   approved colleagues. Do not use Everyone, public Bypass, or a domain-wide
   allow rule unless that is genuinely intended.
6. Set a suitable session duration, save, and copy the application's
   **Application Audience (AUD) tag**.
7. Find your Zero Trust **team domain**, such as
   `your-team.cloudflareaccess.com`. This is not your website's custom domain.

Access applications deny by default unless a user matches an Allow policy. The
Worker's JWT check requires the AUD of **this exact application**, not another
application in your account. Do not put the hostname into an Access path field.

## 4. Add production RUNTIME variables and secret

Open **Workers & Pages → search-archive → Settings → Variables & Secrets**.
These are **not** the build variables/secrets under Settings → Build.

| Name | Type | Value |
| --- | --- | --- |
| `APP_HOSTNAME` | Plain text | `archive.srirajkavin.com` (no scheme, port or path) |
| `CF_ACCESS_TEAM_DOMAIN` | Plain text | Your `your-team.cloudflareaccess.com` team hostname |
| `CF_ACCESS_AUD` | Plain text | The Access application's audience tag |
| `SUPABASE_PROJECT_ID` | Plain text | Your existing 20-character project reference |
| `SUPABASE_SECRET_KEY` | **Secret/encrypted** | Your existing server-side Supabase secret/service-role key |

Use the key from Supabase's project API settings, not a publishable/anonymous
key and not the personal `SUPABASE_ACCESS_TOKEN` used by the scraper's upload
commands. The private Data API must be enabled, with the existing read grants
and consolidated JSONB schema already applied.

Save/deploy these runtime settings. `keep_vars: true` preserves dashboard-managed
plain variables across future Wrangler deploys; secrets are stored separately.
Do not commit values to `wrangler.jsonc`, `.env`, repository variables, or GitHub.
If you use named environments later, configure their values and Access policies
separately—bindings/variables are not automatically inherited across environments.

## 5. Attach the custom domain

After Access and runtime settings are ready:

1. Open **search-archive → Settings → Domains & Routes → Add → Custom Domain**.
2. Enter **`archive.srirajkavin.com`** and confirm.
3. Wait for DNS and the HTTPS certificate to become active.
4. Enable the zone's **Always Use HTTPS** setting if HTTP is not already
   redirected to HTTPS. The Worker refuses plaintext HTTP requests itself.

Cloudflare creates the domain's Worker DNS record and certificate. Resolve any
existing DNS record conflict for **that subdomain only**; do not delete unrelated
root-domain or email records. No laptop IP, port forward or tunnel is needed.

The repository intentionally has no `routes` key, so subsequent Wrangler deploys
do not overwrite dashboard-managed custom domains. If you later manage routes
in code instead, coordinate the hostname, Access application and `APP_HOSTNAME`
and do not accidentally remove protection.

## 6. Verify before sharing

Open `https://archive.srirajkavin.com` in a private browser window:

- You must reach the Cloudflare Access login, not the dataset.
- An approved account can browse runs, filter queries, render AIO/search results,
  compare runs, open each side's citations and inspect raw JSON.
- An unapproved account is denied.
- An unauthenticated direct request to `/api/runs` must not return run data:
  `curl -i https://archive.srirajkavin.com/api/runs` (do not add Access cookies).
- `workers.dev`, Version URLs and preview addresses remain disabled.
- Check that JSON replies are `Cache-Control: private, no-store`. Do not add
  Cache Everything/cache overrides for this hostname or any API response.
- Frontend source/network responses must not contain the Supabase secret.

These account-specific checks cannot be completed locally before you configure
Cloudflare. Local cryptographic/runtime/browser tests are not proof that your
production Access policy or DNS has been set up correctly.

### Troubleshooting

| Symptom | Check |
| --- | --- |
| No public URL after the first import | Expected. Finish Access setup and add the custom domain. |
| 503 requesting `APP_HOSTNAME` or Access configuration | Missing **runtime** variables; build-only settings do not count. |
| 403 hostname not enabled | Custom domain and `APP_HOSTNAME` must match exactly. |
| 401 session cannot be verified after Access login | Wrong team domain/AUD, stale login, or unavailable JWKS. Sign out/in after correcting settings; never disable verification. |
| Supabase denied access / 502 | Correct secret key, project ID, private Data API grants and schema. Do not broaden public database access. |
| No same-query comparison match | Exact saved query text, language and country must match; a missing capture is not AIO absent. |
| Build application-name mismatch | Use `search-archive`, or intentionally change the name in Wrangler and Cloudflare together. |
| Local `npm run dev` returns an error | Expected without Access settings and a real signed JWT on the configured HTTPS hostname. Use the isolated fixture tests; there is no public dev bypass. |

## Viewer capabilities and matching rules

The hosted frontend includes archive pagination, query-text search, AIO status
(including exact absent versus uncertain/failure states), capture status,
language/country/device, inclusive UTC capture dates, warning and organic-result
filters. Filters execute server-side **before** pagination and reset the page.

Open a response and choose **Compare runs**. Matches use exact text + language +
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

`npm test` covers safe route/filter/date validation, pagination and exact query
matching; forged/expired/wrong-issuer/audience/algorithm JWT rejection; missing
configuration and alternate hosts; static/API protection; upstream secrecy and
fixed read transport; and the bundled production Worker in the real local
Workers runtime. All network traffic in runtime tests is intercepted.

Optional Chromium end-to-end check (requires Python Playwright, plus Chromium):

```sh
python worker/test/browser_check.py --browser /absolute/path/to/chrome
```

This starts a **loopback-only synthetic test bridge**, not an authentication
bypass in the deployable Worker. Screenshots go to
`/tmp/cloudflare-worker-desktop.png` and `/tmp/cloudflare-worker-mobile.png`.
The test-only code and Miniflare are not part of the deployed bundle. Unit/runtime
checks also run in GitHub Actions; your existing scraping environment is unrelated.

If deploying manually instead of using Git integration:

```sh
npx wrangler login
npx wrangler deploy
npx wrangler secret put SUPABASE_SECRET_KEY
```

Then configure the four plain runtime variables, Access application and custom
domain as above. `wrangler secret put` publishes a new Worker version. Do not
paste secret values into shell command arguments, source files or chat.

Your old tailnet service is independent and can remain running as a fallback.
This deployment does not change it. Future pushes to the connected production
branch trigger Cloudflare builds. Review GitHub and Cloudflare build results
before treating an update as deployed.

## Official references

- [Workers Builds configuration](https://developers.cloudflare.com/workers/ci-cd/builds/configuration/)
- [Static asset binding and Worker-first authentication](https://developers.cloudflare.com/workers/static-assets/binding/)
- [Workers custom domains](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/)
- [Access self-hosted applications](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)
- [Access JWT verification](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)
