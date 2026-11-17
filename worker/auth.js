import { createRemoteJWKSet, jwtVerify } from "jose";
import { HTTPError } from "./http.js";

export function accessConfig(env) {
  const team = String(env.CF_ACCESS_TEAM_DOMAIN || "").trim().replace(/^https:\/\//, "").replace(/\/$/, "").toLowerCase();
  const audience = String(env.CF_ACCESS_AUD || "").trim();
  if (!/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com$/.test(team) || !audience || audience.length > 256 || /[\u0000-\u001f]/.test(audience)) {
    throw new HTTPError(503, "Cloudflare Access is not configured. Ask the deployment owner to complete setup.");
  }
  return { issuer: `https://${team}`, audience };
}

// Dependency injection is for isolated tests only; the exported production
// Worker always uses real remote Cloudflare JWKS and signature validation.
export function createAccessVerifier(jwksFactory = createRemoteJWKSet) {
  const keysets = new Map();
  return async function verifyAccess(request, env) {
    const { issuer, audience } = accessConfig(env);
    const token = request.headers.get("Cf-Access-Jwt-Assertion");
    if (!token || token.length > 16384) throw new HTTPError(401, "Sign in through Cloudflare Access to use this viewer.");
    try {
      if (!keysets.has(issuer)) {
        if (keysets.size >= 4) keysets.clear();
        keysets.set(issuer, jwksFactory(new URL(`${issuer}/cdn-cgi/access/certs`)));
      }
      await jwtVerify(token, keysets.get(issuer), {
        issuer, audience, algorithms: ["RS256"],
        requiredClaims: ["exp", "iat", "sub"], clockTolerance: 5,
      });
    } catch {
      // JWT/JWKS errors must never expose tokens, keys, or fetch details.
      throw new HTTPError(401, "Your Cloudflare Access session could not be verified. Sign in again.");
    }
  };
}

export const verifyAccess = createAccessVerifier();
