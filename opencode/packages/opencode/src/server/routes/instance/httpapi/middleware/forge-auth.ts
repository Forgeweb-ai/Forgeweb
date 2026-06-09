/**
 * forge-auth router middleware
 * =============================
 * Forge BYOK shim. Two responsibilities, gated on the `X-Forge-Auth` header:
 *
 *   1. Flip `Auth.ForgeMode` to true for the duration of the request, so
 *      provider key resolution downstream (provider/provider.ts) ignores
 *      process env vars and config-provider `apiKey`s. Without this, a
 *      brand-new user would inherit whatever platform-level keys are baked
 *      into the shared opencode container.
 *
 *   2. Provide `Auth.Override` (a {providerID → Auth.Info} map decoded from
 *      the base64+JSON header payload) so `Auth.all()` returns the calling
 *      user's keys instead of whatever is on disk in auth.json.
 *
 * The header is set by `forge-server/runner/opencode_proxy.py` on EVERY
 * request that flows through the Forge proxy — even when the user has zero
 * keys saved (the payload is `base64("{}")`). That guarantees `ForgeMode`
 * is on the moment a request is gated by a Forge JWT, which is the
 * semantically correct trigger: "this is a per-user Forge request."
 *
 * No header (CLI, tests, non-Forge deployments) → both refs stay at their
 * defaults and behaviour is unchanged.
 *
 * Header format: base64( JSON.stringify( { "<providerID>": { type: "api",
 * key: "<plaintext>" }, ... } ) ).
 */
import { Auth } from "@/auth"
import { Effect } from "effect"
import { HttpRouter, HttpServerRequest } from "effect/unstable/http"

const HEADER = "x-forge-auth"

function decodeHeader(raw: string): Record<string, unknown> {
  try {
    const text =
      typeof Buffer !== "undefined"
        ? Buffer.from(raw, "base64").toString("utf8")
        : atob(raw)
    const parsed = JSON.parse(text)
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>
    }
  } catch (_) {
    // Malformed header → treat as "Forge request with no keys". We still flip
    // ForgeMode so the user doesn't silently fall back to platform env keys.
    // Don't log the raw value — it contains plaintext keys.
  }
  return {}
}

export const forgeAuthLayer = HttpRouter.middleware<{ handles: unknown }>()((effect) =>
  Effect.gen(function* () {
    const request = yield* HttpServerRequest.HttpServerRequest
    const raw = request.headers[HEADER]
    if (!raw) return yield* effect
    const override = decodeHeader(raw)
    return yield* effect.pipe(
      Effect.provideService(Auth.Override, override),
      Effect.provideService(Auth.ForgeMode, true),
    )
  }),
).layer
