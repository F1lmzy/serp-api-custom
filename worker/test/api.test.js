import assert from "node:assert/strict";
import test from "node:test";
import { apiResponse, queryFilters, SupabaseReader } from "../api.js";
import { HTTPError } from "../http.js";
import { BASE, ENV, RUN_A, RUN_B, fixtureReader } from "./fixtures.js";

const invalid = (fn) => assert.rejects(fn, (error) => error instanceof HTTPError && error.status === 400);

test("query lists stay lightweight, paginate after filtering, and preserve saved order", async () => {
  const reader = fixtureReader();
  const params = { run_id: RUN_A, aio_status: "absent" };
  const first = await apiResponse(reader, "/api/queries", params);
  assert.equal(first.items.length, 50); assert.equal(first.has_more, true);
  const last = await apiResponse(reader, "/api/queries", { ...params, offset: "50" });
  assert.deepEqual(last.items.map((row) => row.query_id), ["A51"]); assert.equal(last.has_more, false);
  assert.ok(!reader.calls[0].params.select.includes("aio_response"));
  assert.ok(!reader.calls[0].params.select.includes("organic_results"));
  const present = await apiResponse(reader, "/api/queries", { run_id: RUN_A, aio_status: "present" });
  assert.deepEqual(present.items.map((row) => row.query_id), ["P01"]);
});

test("all filters combine and dates include the entire end day in UTC", async () => {
  const filters = queryFilters({ aio_status: "absent", capture_status: "ok", hl: "en", gl: "us", device: "mobile", has_warnings: "yes", has_organic: "no", captured_from: "2026-11-12", captured_to: "2026-11-13" });
  assert.deepEqual(filters, { aio_status: "eq.absent", capture_status: "eq.ok", hl: "eq.en", gl: "eq.us", device: "eq.mobile", warnings: "neq.[]", organic_results: "eq.[]", and: "(captured_at.gte.2026-11-12T00:00:00Z,captured_at.lt.2026-11-14T00:00:00Z)" });
  assert.equal(queryFilters({ captured_to: "2028-02-29" }).and, "(captured_at.lt.2028-03-01T00:00:00Z)");
  assert.equal(queryFilters({ captured_from: "0001-01-01" }).and, "(captured_at.gte.0001-01-01T00:00:00Z)");
  assert.equal(queryFilters({ aio_status: "not_present" }).or, "(aio_status.neq.present,aio_status.is.null)");
  assert.equal(queryFilters({ aio_status: "unknown", capture_status: "unknown" }).aio_status, "is.null");
  const reader = fixtureReader();
  const filtered = await apiResponse(reader, "/api/queries", { run_id: RUN_A, aio_status: "absent", has_warnings: "yes", has_organic: "no", captured_from: "2026-11-13", captured_to: "2026-11-13" });
  assert.equal(filtered.items.length, 50); assert.equal(filtered.has_more, true);
});

test("invalid filters, dates, IDs, search and pagination reject before database requests", async () => {
  const reader = fixtureReader();
  for (const extra of [
    { run_id: "bad" }, { limit: "0" }, { limit: "101" }, { offset: "-1" }, { offset: "10000001" }, { limit: "2.5" }, { search: "x".repeat(301) },
    { aio_status: "eq.present" }, { capture_status: "complete" }, { has_warnings: "maybe" }, { has_organic: "true" }, { device: "a\nb" }, { hl: "x".repeat(65) },
    { captured_from: "2026-02-30" }, { captured_to: "9999-12-31" }, { captured_from: "0000-01-01" }, { captured_from: "2026-12-01", captured_to: "2026-11-01" },
    { captured_to: "2026-11-12),or=(query.neq.null" },
  ]) await invalid(() => apiResponse(reader, "/api/queries", { run_id: RUN_A, ...extra }));
  for (const query_id of ["", "a\nb", "x".repeat(257)]) await invalid(() => apiResponse(reader, "/api/response", { run_id: RUN_A, query_id }));
  assert.equal(reader.calls.length, 0);
});

test("comparison uses exact query and nullable locale, not reused IDs or device", async () => {
  const reader = fixtureReader();
  const params = { run_id: RUN_A, query_id: "P01", compare_run_id: RUN_B };
  const matches = await apiResponse(reader, "/api/comparison-matches", params);
  assert.deepEqual(matches.items.map((row) => row.query_id), ["different-id"]);
  assert.equal(matches.items[0].device, "mobile"); assert.equal(matches.items[0].aio_status, "absent");
  const target = reader.calls[1].params;
  assert.equal(target.query, "eq." + BASE.query); assert.equal(target.hl, "eq.en"); assert.equal(target.gl, "eq.us");
  assert.ok(!("query_id" in target)); assert.ok(!target.select.includes("aio_response"));
  assert.deepEqual(await apiResponse(reader, "/api/response", { run_id: RUN_A, query_id: "P01" }), BASE);
  assert.equal(await apiResponse(reader, "/api/response", { run_id: RUN_A, query_id: "missing" }), null);
  reader.rows[0].gl = null; reader.rows[1].gl = null;
  await apiResponse(reader, "/api/comparison-matches", params);
  assert.equal(reader.calls.at(-1).params.gl, "is.null");
});

test("ambiguous comparisons paginate without guessing, and missing anchors stay missing", async () => {
  const reader = fixtureReader();
  reader.rows.push({ ...structuredClone(reader.rows[1]), query_id: "another-id", ordinal: 4 });
  const params = { run_id: RUN_A, query_id: "P01", compare_run_id: RUN_B, limit: "1" };
  const first = await apiResponse(reader, "/api/comparison-matches", params);
  const second = await apiResponse(reader, "/api/comparison-matches", { ...params, offset: "1" });
  assert.equal(first.has_more, true); assert.equal(second.has_more, false);
  assert.notEqual(first.items[0].query_id, second.items[0].query_id);
  assert.equal(await apiResponse(reader, "/api/comparison-matches", { ...params, query_id: "missing" }), null);
  await invalid(() => apiResponse(reader, "/api/comparison-matches", { ...params, compare_run_id: RUN_A }));
});

test("Supabase transport uses fixed HTTPS origin, GET, encoded values, key header and no redirects", async () => {
  let captured;
  const reader = new SupabaseReader(ENV, async (url, init) => { captured = { url, init }; return Response.json([]); });
  await reader.get("queries", { query: "eq." + BASE.query, limit: 1 });
  assert.equal(captured.url.hostname, `${ENV.SUPABASE_PROJECT_ID}.supabase.co`);
  assert.equal(captured.url.searchParams.get("query"), "eq." + BASE.query);
  assert.equal(captured.url.searchParams.get("limit"), "1");
  assert.equal(captured.init.method, "GET"); assert.equal(captured.init.redirect, "manual");
  assert.equal(captured.init.headers.apikey, ENV.SUPABASE_SECRET_KEY);
  assert.ok(captured.init.signal instanceof AbortSignal);
  await assert.rejects(() => reader.get("private-table", {}), HTTPError);
  for (const project of ["", "https://evil.test", "../secret"]) assert.throws(() => new SupabaseReader({ ...ENV, SUPABASE_PROJECT_ID: project }), HTTPError);
});

test("upstream failures never echo response bodies, secrets or exception details", async () => {
  const sensitive = "SENSITIVE_UPSTREAM_RESPONSE";
  for (const code of [301, 401, 403, 404, 500]) {
    const reader = new SupabaseReader(ENV, async () => new Response(sensitive + ENV.SUPABASE_SECRET_KEY, { status: code }));
    await assert.rejects(() => reader.get("queries", {}), (error) => error.status === 502 && !error.message.includes(sensitive) && !error.message.includes(ENV.SUPABASE_SECRET_KEY));
  }
  for (const value of [null, {}, [null], [["not an object"]]]) {
    const reader = new SupabaseReader(ENV, async () => Response.json(value));
    await assert.rejects(() => reader.get("queries", {}), (error) => error.status === 502);
  }
  const reader = new SupabaseReader(ENV, async () => { throw new Error(sensitive); });
  await assert.rejects(() => reader.get("queries", {}), (error) => error.status === 502 && !error.message.includes(sensitive));
});
