/**
 * Forge user-id Context
 * ======================
 * Request-scoped user-id signal for the shared opencode process.
 *
 * The opencode process is multi-tenant: forge-server's opencode_proxy forwards
 * every browser request with two headers — `X-Forge-User-Id` and an HMAC
 * `X-Forge-Internal-Token`. The `forge-user.ts` router middleware verifies the
 * HMAC against `$FORGE_INTERNAL_SECRET` and provides this Context.Reference
 * for the lifetime of the request. Anything reading `UserId` after the
 * middleware ran sees the verified caller; anything outside that scope (CLI,
 * tests, non-Forge deployments) gets the default `undefined`.
 *
 * Why a separate file (not auth/index.ts): keeps the BYOK auth surface and
 * the per-user metadata signal independent. A future change to UserId
 * semantics (e.g. per-org rather than per-user) shouldn't ripple through
 * Auth.Override / Auth.ForgeMode.
 */
import { Context } from "effect"

/**
 * Verified per-request user-id, or `undefined` when:
 *   - the request didn't pass through the Forge proxy (CLI, tests, etc.),
 *   - the HMAC headers were missing, or
 *   - the HMAC failed verification.
 *
 * Consumers MUST treat `undefined` as "this is not a Forge request" and fall
 * back to whatever their non-Forge default is. Never use it as a permission
 * decision on its own — the per-request `Auth.ForgeMode` is the authoritative
 * "is this a Forge request" flag.
 */
export const UserId = Context.Reference<string | undefined>(
  "@forge/UserId",
  { defaultValue: () => undefined },
)

/**
 * The internal HMAC token the proxy minted for this request. Forwarded
 * verbatim by the opencode-side resolver when it calls back into
 * forge-server's /api/internal/* — forge-server re-verifies the same HMAC,
 * so we don't need a separate credential or a token-exchange step.
 *
 * Never log this value.
 */
export const InternalToken = Context.Reference<string | undefined>(
  "@forge/InternalToken",
  { defaultValue: () => undefined },
)
