export const RUN_A = "12345678-1234-4234-8234-123456789abc";
export const RUN_B = "22345678-1234-4234-8234-123456789abc";
export const ENV = {
  APP_HOSTNAME: "archive.example.test",
  SUPABASE_PROJECT_ID: "a".repeat(20), SUPABASE_SECRET_KEY: "test-only-not-a-real-secret",
};
export const BASE = {
  run_id: RUN_A, query_id: "P01", ordinal: 1,
  query: 'A saved query, with "quotes" & <script>window.hacked=true</script>',
  hl: "en", gl: "us", device: "desktop", google_domain: "google.com",
  status: "ok", capture_status: "ok", aio_status: "present", captured_at: "2026-11-12T14:00:00Z", warnings: [],
  aio_response: { blocks: [
    { type: "paragraph", snippet: "A complete saved answer.", reference_indexes: [0, 7] },
    { type: "list", ordered: true, start: 3, list: [{ snippet: "Saved list item" }] },
    { type: "table", detailed: [[{ snippet: "Heading", is_header: true }, { snippet: "Cell" }]] },
  ], references: [{ index: 0, title: "Saved source", link: "https://example.com/source", source: "Example", snippet: "Full reference text" }] },
  organic_results: [
    { position: 1, title: "Saved result", link: "https://example.com/result", snippet: "Full saved organic snippet" },
    { position: 3, title: "Unsafe saved link", link: "javascript:window.hacked=true", snippet: "<img src=x onerror=window.hacked=true>" },
  ],
};

export function fixtureReader() {
  const right = { ...structuredClone(BASE), run_id: RUN_B, query_id: "different-id", device: "mobile", aio_status: "absent", aio_response: null, captured_at: "2026-11-13T14:00:00Z", organic_results: [{ ...BASE.organic_results[0], position: 5 }, { position: 1, title: "Added result", link: "https://new.example/", snippet: "Added snippet" }] };
  const rows = [structuredClone(BASE), right,
    { ...structuredClone(right), query_id: "wrong-locale", ordinal: 2, hl: "fr" },
    { ...structuredClone(right), query_id: "P01", ordinal: 3, query: "A different query with reused ID" },
    ...Array.from({ length: 51 }, (_, i) => ({ ...structuredClone(BASE), query_id: `A${i + 1}`, ordinal: i + 2, query: `Other saved search ${i + 1}`, aio_status: "absent", aio_response: null, organic_results: [], warnings: ["saved warning"], device: "mobile", captured_at: "2026-11-13T14:00:00Z" })),
  ];
  const calls = [];
  return {
    rows, calls,
    async get(table, params) {
      calls.push({ table, params: structuredClone(params) });
      let selected;
      if (table === "scrape_runs") selected = [{ run_id: RUN_A, provider: "custom", label: "Baseline", run_index: 1 }, { run_id: RUN_B, provider: "serpapi", label: "Comparison", run_index: 2 }];
      else {
        selected = [...rows].sort((a, b) => a.ordinal - b.ordinal || a.query_id.localeCompare(b.query_id));
        for (const key of ["run_id", "query_id", "query", "hl", "gl", "device", "aio_status", "capture_status"]) {
          const filter = params[key];
          if (!filter) continue;
          if (filter === "is.null") selected = selected.filter((row) => row[key] == null);
          else if (filter.startsWith("eq.")) selected = selected.filter((row) => row[key] === filter.slice(3));
          else if (filter.startsWith("ilike.")) selected = selected.filter((row) => row[key].toLowerCase().includes(filter.slice(6).replace(/^%|%$/g, "").toLowerCase()));
        }
        if (params.or) selected = selected.filter((row) => row.aio_status !== "present");
        for (const key of ["warnings", "organic_results"]) {
          if (params[key]) selected = selected.filter((row) => Boolean(row[key]?.length) === (params[key] === "neq.[]"));
        }
        if (params.and) for (const bound of params.and.slice(1, -1).split(",")) {
          const [, operator, value] = bound.match(/^captured_at\.(gte|lt)\.(.+)$/);
          selected = selected.filter((row) => row.captured_at && (operator === "gte" ? row.captured_at >= value : row.captured_at < value));
        }
      }
      const offset = Number(params.offset || 0), limit = Number(params.limit || 100);
      selected = selected.slice(offset, offset + limit);
      if (table !== "scrape_runs" && params.select !== "*") selected = selected.map((row) => Object.fromEntries(params.select.split(",").map((key) => [key, row[key] ?? null])));
      return structuredClone(selected);
    },
  };
}
