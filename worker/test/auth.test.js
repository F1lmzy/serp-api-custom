import assert from "node:assert/strict";
import test from "node:test";
import { createLocalJWKSet, generateKeyPair, SignJWT } from "jose";
import { accessConfig, createAccessVerifier } from "../auth.js";
import { createWorker } from "../index.js";
import { BASE, ENV, publicJWK, token, RUN_A } from "./fixtures.js";

const request = (jwt, path = "/") => new Request(`https://${ENV.APP_HOSTNAME}${path}`, { headers: jwt ? { "Cf-Access-Jwt-Assertion": jwt } : {} });
const verifier = () => createAccessVerifier(() => createLocalJWKSet({ keys: [publicJWK] }));

test("Access configuration only permits the configured Cloudflare team issuer", () => {
  assert.deepEqual(accessConfig(ENV), { issuer: `https://${ENV.CF_ACCESS_TEAM_DOMAIN}`, audience: ENV.CF_ACCESS_AUD });
  assert.equal(accessConfig({ ...ENV, CF_ACCESS_TEAM_DOMAIN: `https://${ENV.CF_ACCESS_TEAM_DOMAIN}/` }).issuer, `https://${ENV.CF_ACCESS_TEAM_DOMAIN}`);
  for (const team of ["", "http://evil.test", "user@team.cloudflareaccess.com", "team.cloudflareaccess.com.evil.test", "team.cloudflareaccess.com/path", "team.cloudflareaccess.com:443"]) assert.throws(() => accessConfig({ ...ENV, CF_ACCESS_TEAM_DOMAIN: team }));
  assert.throws(() => accessConfig({ ...ENV, CF_ACCESS_AUD: "" }));
});

test("valid signed tokens verify, and JWKS resolvers are reused", async () => {
  let calls = 0;
  const verify = createAccessVerifier((url) => {
    calls++;
    assert.equal(url.href, `https://${ENV.CF_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs`);
    return createLocalJWKSet({ keys: [publicJWK] });
  });
  await verify(request(await token()), ENV);
  await verify(request(await token()), ENV);
  assert.equal(calls, 1);
});

test("forged, expired, wrong-audience/issuer/key, future and incomplete tokens are denied", async () => {
  const now = Math.floor(Date.now() / 1000), verify = verifier();
  const other = await generateKeyPair("RS256");
  const hmac = await new SignJWT({ sub: "fake" }).setProtectedHeader({ alg: "HS256" }).sign(new Uint8Array(32));
  const tokens = ["not.a.token", "e30.e30.", hmac,
    await token({ exp: now - 100 }), await token({ aud: "wrong" }), await token({ iss: "https://evil.test" }),
    await token({ nbf: now + 3600 }), await token({ exp: undefined }), await token({ sub: undefined }), await token({}, other.privateKey),
  ];
  for (const jwt of tokens) await assert.rejects(() => verify(request(jwt), ENV), (error) => error.status === 401 && !error.message.includes(jwt));
});

test("missing JWT, forged identity headers and cookies cannot fetch assets or Supabase", async () => {
  let assets = 0, database = 0;
  const worker = createWorker({ authenticate: verifier(), fetcher: async () => { database++; return Response.json([]); } });
  const env = { ...ENV, ASSETS: { fetch: async () => { assets++; return new Response("private asset"); } } };
  for (const path of ["/", "/app.js", "/styles.css", "/api/runs", "/api/response", "/api/comparison-matches", "/unknown"]) {
    const req = request(null, path);
    req.headers.set("Cf-Access-Authenticated-User-Email", "owner@example.test"); req.headers.set("Cookie", "CF_Authorization=forged");
    const response = await worker.fetch(req, env);
    assert.equal(response.status, 401); assert.match(response.headers.get("Cache-Control"), /no-store/);
  }
  assert.equal(assets, 0); assert.equal(database, 0);
});

test("missing deployment configuration and wrong host fail closed", async () => {
  const worker = createWorker({ authenticate: verifier() });
  const jwt = await token();
  for (const missing of ["APP_HOSTNAME", "CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD"]) {
    const response = await worker.fetch(request(jwt), { ...ENV, [missing]: "" });
    assert.equal(response.status, 503);
  }
  const response = await worker.fetch(new Request("https://unprotected.workers.dev/", { headers: { "Cf-Access-Jwt-Assertion": jwt } }), ENV);
  assert.equal(response.status, 403);
});

test("authenticated routes keep headers, allowlisted assets, no CORS and no writes", async () => {
  let database = 0, assets = 0;
  const worker = createWorker({ authenticate: verifier(), fetcher: async () => { database++; return Response.json([BASE]); } });
  const env = { ...ENV, ASSETS: { fetch: async () => { assets++; return new Response("static body"); } } };
  const jwt = await token();
  let response = await worker.fetch(request(jwt, `/api/response?run_id=${RUN_A}&query_id=P01`), env);
  assert.deepEqual(await response.json(), BASE); assert.equal(database, 1);
  for (const path of ["/", "/app.js", "/styles.css"]) {
    response = await worker.fetch(request(jwt, path), env); assert.equal(response.status, 200);
    assert.match(response.headers.get("Cache-Control"), /no-store/);
    assert.match(response.headers.get("Content-Security-Policy"), /frame-ancestors 'none'/);
    assert.equal(response.headers.get("Access-Control-Allow-Origin"), null);
  }
  assert.equal(assets, 3);
  for (const path of ["/.env", "/worker/index.js", "/docs/cloudflare-workers.md", "/api/arbitrary-table"]) assert.equal((await worker.fetch(request(jwt, path), env)).status, 404);
  const post = new Request(request(jwt, "/api/runs"), { method: "POST" });
  assert.equal((await worker.fetch(post, env)).status, 405);
  const crossSite = request(jwt, "/api/runs"); crossSite.headers.set("Sec-Fetch-Site", "cross-site");
  assert.equal((await worker.fetch(crossSite, env)).status, 403);
  // A top-level Access login redirect must still be able to reach the HTML.
  const navigation = request(jwt); navigation.headers.set("Sec-Fetch-Site", "cross-site");
  assert.equal((await worker.fetch(navigation, env)).status, 200);
  response = await worker.fetch(new Request(request(jwt), { method: "HEAD" }), env);
  assert.equal(await response.text(), "");
  assert.equal(database, 1);
});
