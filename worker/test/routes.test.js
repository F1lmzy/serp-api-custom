import assert from "node:assert/strict";
import test from "node:test";
import { createWorker } from "../index.js";
import { BASE, ENV, RUN_A } from "./fixtures.js";

const request = (path = "/", init) => new Request(`https://${ENV.APP_HOSTNAME}${path}`, init);

function fixture() {
  const calls = { assets: 0, database: 0 };
  const worker = createWorker({ fetcher: async () => { calls.database++; return Response.json([BASE]); } });
  const env = { ...ENV, ASSETS: { fetch: async () => { calls.assets++; return new Response("static body"); } } };
  return { worker, env, calls };
}

test("anonymous visitors can fetch every public asset and the read-only API without Access configuration", async () => {
  const { worker, env, calls } = fixture();
  for (const path of ["/", "/app.js", "/styles.css"]) {
    const response = await worker.fetch(request(path), env);
    assert.equal(response.status, 200);
    assert.equal(await response.text(), "static body");
  }
  const response = await worker.fetch(request(`/api/response?run_id=${RUN_A}&query_id=P01`), env);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), BASE);
  assert.deepEqual(calls, { assets: 3, database: 1 });
});

test("stale or malformed Access headers and cookies neither gate public data nor reach Supabase", async () => {
  let database = 0;
  const worker = createWorker({ fetcher: async (_url, init) => {
    database++;
    const headers = new Headers(init.headers);
    assert.equal(headers.get("Cf-Access-Jwt-Assertion"), null);
    assert.equal(headers.get("Cookie"), null);
    assert.equal(headers.get("apikey"), ENV.SUPABASE_SECRET_KEY);
    return Response.json([BASE]);
  } });
  const response = await worker.fetch(request(`/api/response?run_id=${RUN_A}&query_id=P01`, {
    headers: { "Cf-Access-Jwt-Assertion": "not.a.token", Cookie: "CF_Authorization=stale" },
  }), ENV);
  assert.equal(response.status, 200);
  const body = await response.text();
  for (const secret of [ENV.SUPABASE_SECRET_KEY, "not.a.token", "CF_Authorization"]) assert.ok(!body.includes(secret));
  assert.equal(database, 1);
});

test("missing hostname, alternate hosts and plaintext HTTP cannot fetch assets or data", async () => {
  const { worker, env, calls } = fixture();
  for (const APP_HOSTNAME of ["", "https://archive.example.test", "bad host"]) {
    assert.equal((await worker.fetch(request(), { ...env, APP_HOSTNAME })).status, 503);
  }
  for (const host of ["unconfigured.workers.dev", "preview.example.test"]) {
    assert.equal((await worker.fetch(new Request(`https://${host}/`), env)).status, 403);
  }
  assert.equal((await worker.fetch(new Request(`http://${ENV.APP_HOSTNAME}/`), env)).status, 400);
  assert.deepEqual(calls, { assets: 0, database: 0 });
  // Static files do not need database credentials; API calls do.
  const missing = { ...env, SUPABASE_SECRET_KEY: "" };
  assert.equal((await worker.fetch(request(), missing)).status, 200);
  assert.equal((await worker.fetch(request("/api/runs"), missing)).status, 503);
  assert.equal(calls.database, 0);
});

test("public routes retain security headers, asset allowlists, no CORS and no writes", async () => {
  const { worker, env, calls } = fixture();
  for (const path of ["/", "/app.js", "/styles.css", "/api/runs"]) {
    const response = await worker.fetch(request(path), env);
    assert.equal(response.status, 200);
    assert.match(response.headers.get("Cache-Control"), /no-store/);
    assert.match(response.headers.get("Content-Security-Policy"), /frame-ancestors 'none'/);
    assert.equal(response.headers.get("Access-Control-Allow-Origin"), null);
  }
  for (const path of ["/.env", "/worker/index.js", "/docs/cloudflare-workers.md", "/api/arbitrary-table"]) {
    assert.equal((await worker.fetch(request(path), env)).status, 404);
  }
  for (const method of ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"]) {
    const response = await worker.fetch(request("/api/runs", { method }), env);
    assert.equal(response.status, 405);
    assert.equal(response.headers.get("Allow"), "GET, HEAD");
  }
  const crossSite = { headers: { "Sec-Fetch-Site": "cross-site" } };
  assert.equal((await worker.fetch(request("/api/runs", crossSite), env)).status, 403);
  // This is not authentication: normal direct API reads are intentionally public.
  assert.equal((await worker.fetch(request("/api/runs"), env)).status, 200);
  assert.equal((await worker.fetch(request("/", crossSite), env)).status, 200);
  const head = await worker.fetch(request("/", { method: "HEAD" }), env);
  assert.equal(head.status, 200);
  assert.equal(await head.text(), "");
  assert.equal(calls.database, 2);
});
