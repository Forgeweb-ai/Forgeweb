/**
 * forge-user router middleware
 * =============================
 * Per-request user-id signal for the shared opencode process.
 *
 * Sibling of forge-auth.ts. Reads two headers set by forge-server's
 * `runner/opencode_proxy.py` on every browser→opencode call:
 *
 *   X-Forge-User-Id            — caller's user-id (string)
 *   X-Forge-Internal-Token     — HMAC-SHA256( FORGE_INTERNAL_SECRET,
 *                                              `${user_id}:${minute_bucket}` )
 *
 * On success, provides Forge.UserId + Forge.InternalToken to the request
 * scope so handlers (notably the agent-model resolver in tool/task.ts) can
 * (a) know who's asking and (b) forward the same HMAC verbatim when calling
 * back into forge-server's /api/internal/agent-model.
 *
 * Verification mirrors the verifier in forge_server/api/internal_routes.py:
 * accept the current minute or the previous minute to tolerate clock skew.
 * Increase the window only if real drift is observed — it's the replay
 * surface.
 *
 * Failure modes (header missing, bad HMAC, missing secret) all leave the
 * defaults intact — request runs as "non-Forge" — so a misconfigured
 * opencode in CLI / test mode still works. The proxy can't be tricked into
 * forwarding a forged identity because it sets the header from the verified
 * JWT itself; we're only re-verifying the proxy's own signature.
 */
import { Effect } from "effect"
import { HttpRouter, HttpServerRequest } from "effect/unstable/http"
import { createHmac, timingSafeEqual } from "node:crypto"

import { UserId, InternalToken } from "@/forge/user-id"

const USER_HEADER  = "x-forge-user-id"
const TOKEN_HEADER = "x-forge-internal-token"
const WINDOW_SECONDS = 60

function sign(userId: string, bucket: number, secret: string): string {
  return createHmac("sha256", secret).update(`${userId}:${bucket}`).digest("hex")
}

function verify(userId: string, token: string, secret: string): boolean {
  const nowBucket = Math.floor(Date.now() / 1000 / WINDOW_SECONDS)
  for (const bucket of [nowBucket, nowBucket - 1]) {
    const expected = sign(userId, bucket, secret)
    if (expected.length !== token.length) continue
    try {
      if (timingSafeEqual(Buffer.from(expected, "hex"), Buffer.from(token, "hex"))) {
        return true
      }
    } catch {
      // Buffer.from('not-hex','hex') yields a shorter buffer; comparison
      // throws on length mismatch above. Fall through.
    }
  }
  return false
}

export const forgeUserLayer = HttpRouter.middleware<{ handles: unknown }>()((effect) =>
  Effect.gen(function* () {
    const secret = process.env.FORGE_INTERNAL_SECRET
    if (!secret) return yield* effect

    const request = yield* HttpServerRequest.HttpServerRequest
    const userId  = request.headers[USER_HEADER]
    const token   = request.headers[TOKEN_HEADER]
    if (!userId || !token) return yield* effect

    if (!verify(userId, token, secret)) {
      // Don't log the token. Log the user-id only so a bad clock or wrong
      // secret shows up in operator output without leaking the credential.
      // eslint-disable-next-line no-console
      console.warn(`[forge-user] rejected request: bad HMAC for user_id=${userId}`)
      return yield* effect
    }

    return yield* effect.pipe(
      Effect.provideService(UserId, userId),
      Effect.provideService(InternalToken, token),
    )
  }),
).layer
