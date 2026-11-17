// Test-only loopback bridge into the real workerd bundle. Tokens and database
// rows are synthetic; this file is not imported by the deployed Worker.
import { createServer } from "node:http";
import { createRuntime } from "./runtime-fixture.js";
import { ENV, token } from "./fixtures.js";

const runtime = await createRuntime();
const jwt = await token();
const server = createServer(async (req, res) => {
  try {
    const headers = new Headers();
    for (const [key, value] of Object.entries(req.headers)) if (key !== "host" && value) headers.set(key, Array.isArray(value) ? value.join(",") : value);
    headers.set("Cf-Access-Jwt-Assertion", jwt);
    const response = await runtime.mf.dispatchFetch(new URL(req.url, `https://${ENV.APP_HOSTNAME}`).href, { method: req.method, headers });
    res.writeHead(response.status, Object.fromEntries(response.headers));
    res.end(Buffer.from(await response.arrayBuffer()));
  } catch { res.writeHead(500); res.end("Fixture failed"); }
});
server.listen(0, "127.0.0.1", () => console.log(`http://127.0.0.1:${server.address().port}`));
async function close() { server.close(); server.closeAllConnections(); await runtime.close(); process.exit(0); }
process.once("SIGTERM", close);
process.once("SIGINT", close);
