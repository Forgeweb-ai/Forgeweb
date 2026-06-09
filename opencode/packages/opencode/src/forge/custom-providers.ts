/**
 * Forge per-user custom-provider resolver
 * ========================================
 * Runtime resolver for per-user custom provider definitions saved via the
 * Forge UI's "Custom provider" dialog.
 *
 * Problem this solves: opencode's provider map is built from the on-disk
 * `opencode.json` / `config.json` files in xdgConfig. In Forge's multi-tenant
 * setup those files are platform-owned (forge-opencode-config) and writes
 * are either lost on `dev.sh restart` or land in the wrong tenant's space.
 * Custom providers must be per-user, persisted in Postgres, and resolved at
 * session-time — same shape as `agent-model.ts` does for the design model.
 *
 * Architecture:
 *   - opencode-side middleware (`forge-user.ts`) verifies the per-request
 *     X-Forge-User-Id + X-Forge-Internal-Token HMAC and exposes Forge.UserId
 *     + Forge.InternalToken via Context for the request scope.
 *   - This module reads those, calls forge-server's
 *     /api/internal/custom-providers, and returns `{ providers: {...} }`
 *     where each entry is the opencode provider config shape
 *     (name/npm/options/models/headers — apiKey NEVER included).
 *   - The caller merges these into opencode's effective provider list for
 *     that session. API keys come via the existing X-Forge-Auth path
 *     (encrypted at rest in user_provider_keys, decrypted per-request).
 *
 * Failure semantics: on ANY failure (no UserId, missing FORGE_API_URL,
 * network error, non-2xx, malformed body) return an empty map so the
 * session falls back to whatever providers the platform config already
 * defines. We warn so silent regressions show up in operator output, but
 * we NEVER fail the session just because the resolver hiccuped.
 *
 * Cost shape: at most 1 outbound HTTP per session start (forge-server has
 * a 60s in-process cache on the read). Flat per container; endpoint is on
 * the same docker network as opencode.
 */
import { Effect } from "effect"

import { UserId, InternalToken } from "./user-id"

export type CustomProviderConfig = {
  name?:    string
  npm?:     string
  options?: Record<string, unknown>
  models?:  Record<string, unknown>
  headers?: Record<string, unknown>
}

export type CustomProviderMap = Record<string, CustomProviderConfig>

export type CustomProvidersResolution = {
  /** providerID → opencode provider config (no apiKey) */
  providers: CustomProviderMap
  /** providerIDs the user has a stored key for (includes platform IDs they
   *  added a key for, not just customs). Used to mark connected state. */
  keyed: Set<string>
}

const EMPTY: CustomProvidersResolution = { providers: {}, keyed: new Set() }

function forgeApiBase(): string | undefined {
  return process.env.FORGE_API_URL || process.env.FORGE_INTERNAL_API_URL
}

/**
 * Resolve the calling Forge user's custom provider map. Returns the empty
 * resolution (NOT an error) on any failure so the caller can merge
 * unconditionally:
 *   effectiveProviders = { ...platformProviders, ...resolution.providers }
 */
export const resolveForgeCustomProviders = Effect.fn("Forge.resolveCustomProviders")(function* () {
  const userId = yield* UserId
  const token  = yield* InternalToken
  const base   = forgeApiBase()

  if (!userId || !token || !base) return EMPTY

  const url = `${base.replace(/\/$/, "")}/api/internal/custom-providers`

  const response = yield* Effect.tryPromise({
    try: () =>
      fetch(url, {
        headers: {
          "x-forge-user-id":        userId,
          "x-forge-internal-token": token,
        },
      }),
    catch: (e) => e,
  }).pipe(
    Effect.catch((cause) => {
      // eslint-disable-next-line no-console
      console.warn(`[forge.custom-providers] network failure: ${String(cause)}`)
      return Effect.succeed(undefined)
    }),
  )
  if (!response || !response.ok) {
    if (response) {
      // eslint-disable-next-line no-console
      console.warn(`[forge.custom-providers] forge-server returned ${response.status}`)
    }
    return EMPTY
  }

  const body = yield* Effect.tryPromise({
    try: () => response.json() as Promise<{ providers: CustomProviderMap | null; keyed?: string[] | null }>,
    catch: (e) => e,
  }).pipe(Effect.catch(() => Effect.succeed(undefined)))

  if (!body || !body.providers || typeof body.providers !== "object") {
    return EMPTY
  }
  return {
    providers: body.providers,
    keyed:     new Set(Array.isArray(body.keyed) ? body.keyed : []),
  }
})
