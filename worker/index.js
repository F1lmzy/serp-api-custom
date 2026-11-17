import { API_PATHS, apiResponse, SupabaseReader } from "./api.js";
import { verifyAccess } from "./auth.js";
import { HTTPError, json, secure } from "./http.js";

const ASSET_PATHS = new Set(["/", "/app.js", "/styles.css"]);

export function createWorker({ authenticate = verifyAccess, fetcher = globalThis.fetch.bind(globalThis) } = {}) {
  return {
    async fetch(request, env) {
      try {
        const hostname = String(env.APP_HOSTNAME || "").trim().toLowerCase();
        if (!/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/.test(hostname)) {
          throw new HTTPError(503, "Set APP_HOSTNAME to the custom domain before using this deployment.");
        }
        const url = new URL(request.url);
        if (url.hostname !== hostname) throw new HTTPError(403, "This hostname is not enabled for the viewer.");
        if (url.protocol !== "https:") throw new HTTPError(400, "Use HTTPS to access this viewer.");
        // Authentication runs before ALL assets and data. There is no env flag,
        // dev cookie or trusted email header that can bypass JWT verification.
        await authenticate(request, env);
        if (!["GET", "HEAD"].includes(request.method)) {
          return secure(new Response(JSON.stringify({ error: "Only GET and HEAD are supported." }), { status: 405, headers: { Allow: "GET, HEAD", "Content-Type": "application/json; charset=utf-8" } }));
        }
        let response;
        if (ASSET_PATHS.has(url.pathname)) {
          response = await env.ASSETS.fetch(request);
        } else if (API_PATHS.has(url.pathname)) {
          // Permit top-level login redirects to the HTML, but never cross-site
          // browser API reads. No CORS headers are enabled on any response.
          if (request.headers.get("Sec-Fetch-Site") === "cross-site") throw new HTTPError(403, "Cross-site API requests are not allowed.");
          const params = Object.fromEntries([...url.searchParams].filter(([, value]) => value !== ""));
          const data = await apiResponse(new SupabaseReader(env, fetcher), url.pathname, params);
          response = data === null ? json({ error: "Response not found." }, 404) : json(data);
        } else response = json({ error: "Not found." }, 404);
        return secure(response, request.method === "HEAD");
      } catch (error) {
        // No request/query/token logging, exception echo, or upstream bodies.
        return secure(json({ error: error instanceof HTTPError ? error.message : "The viewer encountered an unexpected error." }, error instanceof HTTPError ? error.status : 500), request.method === "HEAD");
      }
    },
  };
}

export default createWorker();
