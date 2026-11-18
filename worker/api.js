import { HTTPError } from "./http.js";

export const QUERY_SUMMARY = "run_id,query_id,ordinal,query,captured_at,status,capture_status,aio_status,hl,gl,device,google_domain";
export const API_PATHS = new Set(["/api/runs", "/api/queries", "/api/response", "/api/comparison-matches"]);
const bad = (message) => { throw new HTTPError(400, message); };
const length = (value) => [...value].length;

function integer(params, name, fallback, maximum) {
  const raw = params[name] ?? String(fallback);
  if (!/^\d+$/.test(raw)) bad(`Invalid ${name}.`);
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value > maximum || (name === "limit" && value === 0)) bad(`Invalid ${name}.`);
  return value;
}
function uuid(value) {
  if (!/^[\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12}$/i.test(value || "")) bad("A valid run_id is required.");
  return value.toLowerCase();
}
function queryID(value) {
  if (!value || length(value) > 256 || /[\u0000-\u001f]/u.test(value)) bad("A valid query_id is required.");
  return value;
}
function dateBoundary(value, name, through) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || Number(value.slice(0, 4)) < 1) bad(`Invalid ${name} date (use YYYY-MM-DD).`);
  const parsed = new Date(`${value}T00:00:00Z`);
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value)
    bad(`Invalid ${name} date (use YYYY-MM-DD).`);
  if (through) parsed.setUTCDate(parsed.getUTCDate() + 1);
  if (parsed.getUTCFullYear() > 9999) bad(`Invalid ${name} date (use YYYY-MM-DD).`);
  return parsed.toISOString().replace(".000Z", "Z");
}

export function queryFilters(params) {
  const filters = {};
  const enums = {
    aio_status: ["present", "absent", "unavailable", "empty", "unresolved", "invalid", "error", "unknown", "not_present"],
    capture_status: ["ok", "error", "unresolved", "unknown"],
    has_warnings: ["yes", "no"], has_organic: ["yes", "no"],
  };
  for (const [key, allowed] of Object.entries(enums)) {
    const value = params[key];
    if (!value) continue;
    if (!allowed.includes(value)) bad(`Invalid ${key} filter.`);
    if (key === "aio_status" && value === "not_present") filters.or = "(aio_status.neq.present,aio_status.is.null)";
    else if (key === "aio_status" || key === "capture_status") filters[key] = value === "unknown" ? "is.null" : `eq.${value}`;
    else {
      const column = key === "has_warnings" ? "warnings" : "organic_results";
      filters[column] = value === "yes" ? "neq.[]" : "eq.[]";
    }
  }
  for (const key of ["hl", "gl", "device"]) {
    const value = (params[key] || "").trim();
    if (!value) continue;
    if (length(value) > 64 || /[\u0000-\u001f]/u.test(value)) bad(`Invalid ${key} filter.`);
    filters[key] = `eq.${value}`;
  }
  const bounds = [];
  for (const key of ["captured_from", "captured_to"]) {
    if (!params[key]) continue;
    const through = key === "captured_to";
    bounds.push(`captured_at.${through ? "lt" : "gte"}.${dateBoundary(params[key], key, through)}`);
  }
  if (params.captured_from && params.captured_to && params.captured_from > params.captured_to)
    bad("Capture start date must not be after the end date.");
  if (bounds.length) filters.and = `(${bounds.join(",")})`;
  return filters;
}

export class SupabaseReader {
  constructor(env, fetcher = globalThis.fetch.bind(globalThis)) {
    const project = String(env.SUPABASE_PROJECT_ID || "");
    if (!/^[a-z0-9]{20}$/.test(project) || !env.SUPABASE_SECRET_KEY) {
      throw new HTTPError(503, "Supabase is not configured. Set the Worker's project ID and secret key.");
    }
    this.base = `https://${project}.supabase.co/rest/v1/`;
    this.key = env.SUPABASE_SECRET_KEY;
    this.fetcher = fetcher;
  }
  async get(table, params) {
    if (!["scrape_runs", "queries"].includes(table)) throw new HTTPError(500, "Unsupported response-store operation.");
    const url = new URL(table, this.base);
    url.search = new URLSearchParams(params).toString();
    let response;
    try {
      response = await this.fetcher(url, {
        method: "GET", redirect: "manual", signal: AbortSignal.timeout(25000),
        headers: { apikey: this.key, Accept: "application/json" },
      });
    } catch {
      throw new HTTPError(502, "Cannot reach Supabase. Please try again.");
    }
    if (!response.ok) {
      throw new HTTPError(502, [401, 403].includes(response.status)
        ? "Supabase denied access. Check the Worker's secret key and database grants."
        : "Supabase could not read the response store. Check the schema and Data API configuration.");
    }
    let rows;
    try { rows = await response.json(); } catch { throw new HTTPError(502, "Supabase returned an invalid response."); }
    const notPlainObject = (row) => !row || typeof row !== "object" || Array.isArray(row);
    if (!Array.isArray(rows) || rows.some(notPlainObject)) throw new HTTPError(502, "Supabase returned an unexpected response.");
    return rows;
  }
}

function page(rows, limit, offset) {
  return { items: rows.slice(0, limit), has_more: rows.length > limit, offset };
}

// Port of the local viewer API. All table, column and operator names are fixed;
// user values are separate URLSearchParams, never SQL or a forwarded URL.
export async function apiResponse(reader, path, params) {
  if (path === "/api/runs" || path === "/api/queries") {
    const limit = integer(params, "limit", 50, 100), offset = integer(params, "offset", 0, 10000000);
    let table, filters;
    if (path === "/api/runs") {
      table = "scrape_runs";
      filters = { select: "run_id,label,provider,run_index,comparison_id,created_at", order: "created_at.desc,run_id.asc" };
    } else {
      table = "queries";
      filters = { select: QUERY_SUMMARY, run_id: `eq.${uuid(params.run_id)}`, order: "ordinal.asc,query_id.asc", ...queryFilters(params) };
      const search = (params.search || "").trim();
      if (length(search) > 300) bad("Search is limited to 300 characters.");
      if (search) filters.query = `ilike.%${search}%`;
    }
    return page(await reader.get(table, { ...filters, limit: limit + 1, offset }), limit, offset);
  }
  if (path === "/api/response") {
    const identifier = queryID(params.query_id);
    const rows = await reader.get("queries", { select: "*", run_id: `eq.${uuid(params.run_id)}`, query_id: `eq.${identifier}`, limit: 1 });
    return rows[0] || null;
  }
  if (path === "/api/comparison-matches") {
    const source = uuid(params.run_id);
    const target = uuid(params.compare_run_id);
    const identifier = queryID(params.query_id);
    const limit = integer(params, "limit", 50, 100), offset = integer(params, "offset", 0, 10000000);
    if (source === target) bad("Choose two different capture runs.");
    const anchor = (await reader.get("queries", { select: "query,hl,gl", run_id: `eq.${source}`, query_id: `eq.${identifier}`, limit: 1 }))[0];
    if (!anchor) return null;
    const filters = { select: QUERY_SUMMARY, run_id: `eq.${target}`, query: `eq.${anchor.query}`, order: "ordinal.asc,query_id.asc", limit: limit + 1, offset };
    for (const key of ["hl", "gl"]) filters[key] = anchor[key] == null ? "is.null" : `eq.${anchor[key]}`;
    return { ...page(await reader.get("queries", filters), limit, offset), match_rule: "Exact query text, language and country; device may differ." };
  }
  throw new HTTPError(404, "Not found.");
}
