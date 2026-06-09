/**
 * Forge per-user agent-model resolver
 * =====================================
 * Runtime resolver for the `__FORGE_USER_SETTING__` sentinel in opencode.json
 * agent definitions.
 *
 * Problem this solves: opencode.json's `agent.<name>.model` is read once at
 * boot and frozen. In Forge's multi-tenant deployment users pick the
 * design-analyst model per account; the value must be resolved per task
 * dispatch, not per process boot. The earlier dev.sh stopgap baked the most-
 * recently-saved value across ALL users into the shared opencode config,
 * which leaked the wrong model into the wrong session (see [[forge_overview]]
 * + dev.sh:378–383 for the multi-tenancy note).
 *
 * Architecture:
 *   - opencode-side middleware (`forge-user.ts`) verifies the per-request
 *     X-Forge-User-Id + X-Forge-Internal-Token HMAC and exposes Forge.UserId
 *     + Forge.InternalToken via Context for the request scope.
 *   - This module reads those, calls forge-server's
 *     /api/internal/agent-model?agent=<name>, and returns the resolved
 *     `{providerID, modelID}` pair.
 *   - tool/task.ts invokes resolveForgeAgentModel BEFORE locking the dispatch
 *     model in, only when the agent's static-config model has the sentinel
 *     providerID.
 *
 * Failure semantics (per the founder-engineer call): on ANY failure (no
 * UserId, missing FORGE_API_URL, network error, non-2xx, unmapped agent),
 * return `undefined` so task.ts falls through to its existing
 * "use parent message model" path. We log a warning per failure so opaque
 * regressions show up in operator output, but we NEVER fail the user's turn
 * just because the resolver hiccuped. Token is never logged.
 *
 * Cost shape: at most 1 outbound HTTP per (sessionID, agentName) per turn;
 * design-analyst + design-critic share `(sessionID, "design_model")` via the
 * forge-server-side cache. Flat per container; outbound endpoint is in the
 * same docker network as opencode.
 */
import { Effect } from "effect"

import { UserId, InternalToken } from "./user-id"

/** Sentinel that opencode.json places in `agent.<name>.model`. The matching
 * detection happens in tool/task.ts against the *parsed* providerID — see the
 * note in agent/agent.ts:322 where parseModel splits "providerID/modelID". */
export const SENTINEL_PROVIDER_ID = "__FORGE_USER_SETTING__"

export interface ResolvedModel {
  providerID: string
  modelID: string
}

function forgeApiBase(): string | undefined {
  return process.env.FORGE_API_URL || process.env.FORGE_INTERNAL_API_URL
}

function parse(value: string): ResolvedModel | undefined {
  const idx = value.indexOf("/")
  if (idx <= 0 || idx === value.length - 1) return undefined
  return { providerID: value.slice(0, idx), modelID: value.slice(idx + 1) }
}

/**
 * Resolve the model for the given agent name from the current request's
 * Forge user-id. Returns `undefined` (NOT an error) on any failure so the
 * caller can fall through to its default.
 */
export const resolveForgeAgentModel = Effect.fn("Forge.resolveAgentModel")(function* (agentName: string) {
  const userId = yield* UserId
  const token  = yield* InternalToken
  const base   = forgeApiBase()

  if (!userId || !token || !base) return undefined

  // node-fetch is built-in on Bun/Node 18+; no extra dependency to ship.
  const url = `${base.replace(/\/$/, "")}/api/internal/agent-model?agent=${encodeURIComponent(agentName)}`

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
      console.warn(`[forge.agent-model] network failure for agent=${agentName}: ${String(cause)}`)
      return Effect.succeed(undefined)
    }),
  )
  if (!response || !response.ok) {
    if (response) {
      // eslint-disable-next-line no-console
      console.warn(`[forge.agent-model] forge-server returned ${response.status} for agent=${agentName}`)
    }
    return undefined
  }

  const body = yield* Effect.tryPromise({ try: () => response.json() as Promise<{ model: string | null }>, catch: (e) => e }).pipe(
    Effect.catch(() => Effect.succeed(undefined)),
  )
  if (!body || !body.model) return undefined
  return parse(body.model)
})
