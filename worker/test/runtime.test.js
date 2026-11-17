import assert from "node:assert/strict";
import test from "node:test";
import { BASE, ENV, RUN_A, RUN_B } from "./fixtures.js";
import { createRuntime } from "./runtime-fixture.js";

test("bundled public Worker in workerd: anonymous assets, filters, comparison and secret isolation", async (t) => {
  const runtime = await createRuntime();
  t.after(() => runtime.close());
  const url = (path) => `https://${ENV.APP_HOSTNAME}${path}`;
  for (const path of ["/", "/app.js", "/styles.css"]) {
    const response = await runtime.mf.dispatchFetch(url(path));
    assert.equal(response.status, 200);
    const body = await response.text();
    assert.ok(!body.includes(ENV.SUPABASE_SECRET_KEY));
    if (path === "/") assert.match(body, /Search Archive/);
    assert.match(response.headers.get("Cache-Control"), /no-store/);
  }
  assert.equal(runtime.network.length, 0);
  assert.equal(runtime.assets.length, 3);
  let response = await runtime.mf.dispatchFetch(url("/api/runs"));
  assert.equal(response.status, 200); assert.equal((await response.json()).items.length, 2);
  response = await runtime.mf.dispatchFetch(url(`/api/queries?run_id=${RUN_A}&aio_status=absent&offset=50`));
  assert.deepEqual((await response.json()).items.map((row) => row.query_id), ["A51"]);
  response = await runtime.mf.dispatchFetch(url(`/api/response?run_id=${RUN_A}&query_id=P01`));
  assert.deepEqual(await response.json(), BASE);
  response = await runtime.mf.dispatchFetch(url(`/api/comparison-matches?run_id=${RUN_A}&query_id=P01&compare_run_id=${RUN_B}`));
  assert.deepEqual((await response.json()).items.map((row) => row.query_id), ["different-id"]);
  const calls = runtime.network.length;
  response = await runtime.mf.dispatchFetch(url(`/api/queries?run_id=${RUN_A}&captured_from=2026-02-30`));
  assert.equal(response.status, 400);
  for (const path of ["/.env", "/worker/index.js", "/api/arbitrary-table"]) assert.equal((await runtime.mf.dispatchFetch(url(path))).status, 404);
  assert.equal((await runtime.mf.dispatchFetch(url("/api/runs"), { method: "POST" })).status, 405);
  assert.equal(runtime.network.length, calls);
  for (const target of runtime.network) assert.equal(new URL(target).origin, `https://${ENV.SUPABASE_PROJECT_ID}.supabase.co`);
  response = await runtime.mf.dispatchFetch(url("/api/runs"), { headers: { "Cf-Access-Jwt-Assertion": "stale.token.ignored" } });
  assert.equal(response.status, 200);
  assert.ok(!(await response.text()).includes(ENV.SUPABASE_SECRET_KEY));
});
