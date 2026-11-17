// Real workerd runtime and actual public Worker bundle; no login credentials.
// All outbound traffic is intercepted: no Cloudflare account or Supabase calls.
import { execFileSync } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { ENV, fixtureReader } from "./fixtures.js";

export async function createRuntime() {
  const output = await mkdtemp(join(tmpdir(), "search-archive-worker-"));
  let mf;
  try {
    execFileSync(process.execPath, ["node_modules/wrangler/bin/wrangler.js", "deploy", "--dry-run", "--outdir", output], {
      cwd: resolve(import.meta.dirname, "../.."), stdio: "pipe", env: { ...process.env, WRANGLER_SEND_METRICS: "false" },
    });
    const reader = fixtureReader(), network = [], assets = [];
    const files = { "/": ["index.html", "text/html"], "/app.js": ["app.js", "text/javascript"], "/styles.css": ["styles.css", "text/css"] };
    mf = new Miniflare(convertV4MiniflareOptions({
      rootPath: output, modules: true, scriptPath: join(output, "index.js"), compatibilityDate: "2026-11-17",
      bindings: ENV,
      serviceBindings: { ASSETS: async (request) => {
        const path = new URL(request.url).pathname; assets.push(path);
        if (!files[path]) return new Response("Not found", { status: 404 });
        const [file, type] = files[path];
        return new Response(await readFile(resolve(import.meta.dirname, "../../aio_viewer/static", file)), { headers: { "Content-Type": type } });
      } },
      outboundService: async (request) => {
        const url = new URL(request.url); network.push(url.href);
        if (url.origin !== `https://${ENV.SUPABASE_PROJECT_ID}.supabase.co` || request.method !== "GET" || request.headers.get("apikey") !== ENV.SUPABASE_SECRET_KEY) throw new Error("Unexpected test outbound request");
        const table = url.pathname.replace("/rest/v1/", "");
        if (!["queries", "scrape_runs"].includes(table)) throw new Error("Unexpected test table");
        return Response.json(await reader.get(table, Object.fromEntries(url.searchParams)));
      },
    }));
    await mf.ready;
    return { mf, reader, network, assets, async close() { await mf.dispose(); await rm(output, { recursive: true, force: true }); } };
  } catch (error) {
    await mf?.dispose();
    await rm(output, { recursive: true, force: true });
    throw error;
  }
}
