/**
 * forge-verify plugin
 * ===================
 * Auto-runs the `verify` subagent after the primary `build` agent finishes a turn.
 *
 * Architecture:
 *   - Loaded as an INTERNAL_PLUGIN, but a no-op unless FORGE_PROJECT_ID is set
 *     in the shell environment (so non-Forge opencode usage is unaffected).
 *   - Listens on the bus (Hooks.event) for v2 session events.
 *   - Tracks the active agent per session via `session.next.step.started`
 *     (Step.Ended doesn't carry the agent name).
 *   - On `session.next.step.ended`, if the just-completed step belonged to the
 *     `build` (primary) agent AND finish reason is "stop" (turn really done,
 *     not mid-tool-call), spawns the verify subagent via the SDK's
 *     `session.promptAsync` — fire-and-forget so we don't block the bus loop.
 *   - Enforces a hard budget: max 5 attempts on the same error signature,
 *     max 8 total attempts per session, max 10 minutes wall clock per session.
 *   - Parses verify's final assistant text for the escalate JSON. If present,
 *     re-prompts the primary `build` agent with the error + suspected files —
 *     verify never invokes the main agent itself; the plugin routes.
 *
 * What this plugin DOES NOT do:
 *   - Sanitize tool/reasoning parts for the FE — that lives in the message
 *     publish layer (see task #4). This plugin only orchestrates WHO runs.
 *   - Modify the user's project files — verify itself does that.
 *   - Mention containers, docker, or commands in any message it could surface —
 *     none of its emissions reach the FE; only the verify agent's own text does.
 */
import type { Hooks, PluginInput } from "@opencode-ai/plugin"
import * as Log from "@opencode-ai/core/util/log"
import { createHmac } from "node:crypto"
import { ForgeRuntimeState } from "../forge/runtime-state"

const log = Log.create({ service: "plugin.forge-verify" })

// Budget — must mirror the limits in verify.txt so the plugin and the agent
// agree on when to stop trying.
const TOTAL_BUDGET = 8
const PER_SIGNATURE_BUDGET = 5
const WALL_CLOCK_MS = 10 * 60 * 1000

// v2 event type strings (from packages/core/src/session-event.ts).
const STEP_STARTED = "session.next.step.started"
const STEP_ENDED = "session.next.step.ended"

const PRIMARY_AGENT = "build"
const VERIFY_AGENT = "verify"

// Forge stores project workspaces at .../projects/<uuid>/workspace —
// extract that UUID from a session's directory.
const PROJECT_ID_FROM_DIR = /\/projects\/([0-9a-f-]{32,40})\/workspace/

/** Parse the project UUID out of a session's workspace directory.
 *  Returns undefined for non-Forge directories (e.g. opencode used outside
 *  Forge against an arbitrary path). */
function projectIDFromDirectory(directory: string | undefined): string | undefined {
  if (!directory) return undefined
  return directory.match(PROJECT_ID_FROM_DIR)?.[1]
}

/** Mirror of forge-server's `_sign_project` in api/internal_routes.py.
 *  HMAC-SHA256(secret, "project:<id>:<minute_bucket>"), hex-encoded.
 *  60s window matches the server's `_TOKEN_WINDOW_SECONDS`. */
function signProjectToken(projectID: string, secret: string): string {
  const bucket = Math.floor(Date.now() / 1000 / 60)
  const msg    = `project:${projectID}:${bucket}`
  return createHmac("sha256", secret).update(msg).digest("hex")
}

/**
 * Capture the post-turn workspace as a new project version.
 *
 * Hits POST /api/internal/projects/{id}/versions/snapshot on forge-server
 * with an HMAC-signed token (FORGE_INTERNAL_SECRET, project-scoped). The
 * server is content-addressed + no-ops on identical manifests, so
 * duplicate calls within a turn are cheap and harmless.
 *
 * Why this auth path (not the user-JWT `/api/projects/{id}/versions`):
 *   - The plugin runs outside any user request. No JWT to forward.
 *   - FORGE_INTERNAL_SECRET + FORGE_API_URL are the only envs reliably
 *     exported to opencode by dev.sh (lines 441-442); we use exactly
 *     those.
 *   - Mirrors the existing forge/agent-model.ts internal-call pattern.
 *
 * Best-effort: a snapshot failure must never affect the verify loop or
 * surface to the user. Log + move on.
 */
async function snapshotProjectVersion(
  input: PluginInput,
  sessionID: string,
): Promise<void> {
  const base   = process.env.FORGE_API_URL?.replace(/\/$/, "")
  const secret = process.env.FORGE_INTERNAL_SECRET
  if (!base || !secret) {
    log.warn("snapshot skipped: missing FORGE_API_URL or FORGE_INTERNAL_SECRET")
    return
  }

  // Resolve the project ID from the session's workspace directory. We
  // do NOT rely on process.env.FORGE_PROJECT_ID because dev.sh does not
  // export it to opencode (opencode serves many projects from one process).
  // SDK call shape mirrors packages/opencode/src/cli/cmd/tui/context/sync.tsx
  // — flat `{ sessionID }`, throwOnError true, response is `{ data, ... }`.
  // The previous `{ path: { id } }` shape was wrong; hey-api ignored it and
  // returned undefined, silently failing the regex below.
  let directory: string | undefined
  try {
    const resp = await input.client.session.get({ sessionID }, { throwOnError: true })
    directory  = (resp as any)?.data?.directory
  } catch (err) {
    log.warn("snapshot skipped: session.get failed", { sessionID, error: String(err) })
    return
  }
  if (!directory) {
    log.warn("snapshot skipped: session.directory empty", { sessionID })
    return
  }
  const projectID = projectIDFromDirectory(directory)
  if (!projectID) {
    log.info("snapshot skipped: directory is not a Forge workspace", { directory })
    return
  }
  log.info("snapshot: resolved project from session", { sessionID, projectID })

  const token = signProjectToken(projectID, secret)
  try {
    const resp = await fetch(
      `${base}/api/internal/projects/${projectID}/versions/snapshot`,
      {
        method: "POST",
        headers: {
          "X-Forge-Internal-Token": token,
          "Content-Type":           "application/json",
        },
      },
    )
    if (!resp.ok) {
      log.warn("post-turn snapshot returned non-2xx", {
        status: resp.status, projectID,
      })
      return
    }
    const body = (await resp.json().catch(() => null)) as
      | { id: string; is_no_op: boolean }
      | null
    log.info("post-turn snapshot ok", {
      projectID,
      versionID: body?.id,
      noOp:      body?.is_no_op,
    })
  } catch (err) {
    log.warn("post-turn snapshot failed", { projectID, error: String(err) })
  }
}

interface VerifyRunState {
  startedAt: number
  totalAttempts: number
  signatureAttempts: Map<string, number>
  lastErrorSignature?: string
}

interface EscalateMessage {
  escalate: true
  error: string
  suspected_files: string[]
  attempts: number
}

/**
 * Per-session budget bookkeeping for the verify loop. Agent-tracking lives in
 * the shared ForgeRuntimeState module so the SSE filter sees the same view.
 */
const runs = new Map<string /* sessionID */, VerifyRunState>()

function shouldSkipForBudget(state: VerifyRunState): string | null {
  if (state.totalAttempts >= TOTAL_BUDGET) return "total budget exhausted"
  if (Date.now() - state.startedAt > WALL_CLOCK_MS) return "wall clock exhausted"
  if (state.lastErrorSignature) {
    const n = state.signatureAttempts.get(state.lastErrorSignature) ?? 0
    if (n >= PER_SIGNATURE_BUDGET) {
      return `signature budget exhausted (${state.lastErrorSignature})`
    }
  }
  return null
}

function tryParseEscalate(text: string): EscalateMessage | null {
  // verify emits the handoff as a JSON object somewhere in its final message.
  // Be lenient: find the first {...} containing "escalate": true.
  const match = text.match(/\{[\s\S]*?"escalate"\s*:\s*true[\s\S]*?\}/)
  if (!match) return null
  try {
    const obj = JSON.parse(match[0])
    if (obj && obj.escalate === true && typeof obj.error === "string") {
      return obj as EscalateMessage
    }
  } catch {
    /* not valid JSON — drop */
  }
  return null
}

async function readVerifyFinalText(
  client: PluginInput["client"],
  sessionID: string,
): Promise<string> {
  // Pull the most recent assistant message from the verify run.
  // promptAsync is fire-and-forget, so we read messages after a settle delay.
  // 5s should be plenty for verify to finish a tight check on a healthy app;
  // if it's still working we'll just see partial text and try again next loop.
  try {
    const res = await client.session.messages({ path: { id: sessionID } })
    const messages = (res as any)?.data ?? []
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i]
      if (m?.info?.role !== "assistant") continue
      const parts = m.parts ?? []
      const text = parts
        .filter((p: any) => p?.type === "text")
        .map((p: any) => p.text ?? "")
        .join("\n")
      if (text) return text
    }
  } catch (err) {
    log.warn("failed to read verify final text", { sessionID, error: String(err) })
  }
  return ""
}

export const ForgeVerifyPlugin = async (input: PluginInput): Promise<Hooks> => {
  // Plugin-level gate: we need *process-level* signals that dev.sh actually
  // exports to opencode. Originally this was ForgeRuntimeState.isForgeProject()
  // which reads FORGE_PROJECT_ID — but opencode is a single host process
  // serving N projects, so dev.sh can't set a single project id on it.
  // FORGE_API_URL + FORGE_INTERNAL_SECRET are the right Forge-context signals:
  // both are exported in dev.sh:441-442, neither leaks to non-Forge opencode.
  //
  // We still call isForgeProject() per session inside the handler (via the
  // session.directory regex) so per-project gating downstream is unchanged.
  if (!process.env.FORGE_API_URL || !process.env.FORGE_INTERNAL_SECRET) {
    log.info("forge-verify: Forge env not present, plugin idle")
    return {}
  }

  log.info("forge-verify plugin active", {
    apiBase: process.env.FORGE_API_URL,
  })

  return {
    event: async ({ event }) => {
      const e = event as any
      const type: string | undefined = e?.type
      const props = e?.properties ?? {}
      const sessionID: string | undefined = props.sessionID
      if (!type || !sessionID) return

      // Track which agent each step belongs to — shared with the SSE filter.
      if (type === STEP_STARTED) {
        ForgeRuntimeState.recordStepStarted(sessionID, props.agent)
        return
      }

      if (type !== STEP_ENDED) return

      // Read the active agent BEFORE recordStepEnded clears it on finish=stop.
      const agent = ForgeRuntimeState.getActiveAgent(sessionID)
      const finish: string | undefined = props.finish
      ForgeRuntimeState.recordStepEnded(sessionID, finish)

      log.info("STEP_ENDED received", { sessionID, agent, finish })

      // Only follow the primary agent's turns. If the ending step was verify
      // itself, or any other subagent, we MUST skip — otherwise infinite loop.
      if (agent !== PRIMARY_AGENT) {
        log.info("gate: agent is not primary, skipping", { agent })
        return
      }

      // Only fire when the agent is truly done with its turn — not mid-tool-call.
      // AI-SDK finish reasons: "stop" = done, "tool-calls" = more steps coming,
      // "length" / "content-filter" / "error" = abort cases we shouldn't verify.
      if (finish !== "stop") {
        log.info("gate: finish is not 'stop', skipping", { finish })
        return
      }

      // ── Snapshot fallback (independent of verify) ────────────────────────
      // The verify subagent's lifecycle has many failure modes (budget
      // exhausted, timeout, escalation, the subagent not loading at all in
      // some setups). We don't want versions to depend on verify being
      // healthy — that's a separate concern. Snapshot the post-turn state
      // RIGHT HERE, before we even try to spawn verify. If verify later
      // makes auto-fix changes and we re-snapshot, the manifest-equality
      // no-op on the server elides the duplicate.
      log.info("fallback snapshot: primary turn ended cleanly", { sessionID })
      void snapshotProjectVersion(input, sessionID)

      const state =
        runs.get(sessionID) ?? {
          startedAt: Date.now(),
          totalAttempts: 0,
          signatureAttempts: new Map(),
        }
      runs.set(sessionID, state)

      const skipReason = shouldSkipForBudget(state)
      if (skipReason) {
        log.warn("verify run blocked by budget", { sessionID, reason: skipReason })
        return
      }

      state.totalAttempts += 1
      log.info("spawning verify subagent", {
        sessionID,
        attempt: state.totalAttempts,
        budget: TOTAL_BUDGET,
      })

      try {
        // Fire-and-forget the verify run on this same session.
        // The verify agent's system prompt + permissions are baked into the
        // fork (see src/agent/prompt/verify.txt and src/agent/agent.ts).
        await input.client.session.promptAsync({
          path: { id: sessionID },
          body: {
            agent: VERIFY_AGENT,
            parts: [
              {
                type: "text",
                text:
                  "Verify the current project state. " +
                  "Run the checks defined in your system prompt. " +
                  "If everything is clean, emit nothing. " +
                  "If you fix something, emit only the one final user-facing line.",
              },
            ],
          },
        })
      } catch (err) {
        log.error("verify promptAsync failed", { sessionID, error: String(err) })
        return
      }

      // Poll for verify's final assistant text to detect escalation.
      // We give verify up to ~30s of wall clock; if it's still going, we'll
      // see the result on the next Step.Ended cycle.
      const deadline = Date.now() + 30_000
      let text = ""
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 2000))
        text = await readVerifyFinalText(input.client, sessionID)
        if (text) break
      }
      if (!text) {
        // Verify emitted nothing within the window → per verify.txt step 7,
        // "stay silent if the first check passed." That's a clean state and
        // is the most common path for a non-broken AI turn. Snapshot it.
        void snapshotProjectVersion(input, sessionID)
        return
      }

      const escalate = tryParseEscalate(text)
      if (!escalate) {
        // Verify emitted a "Fixed it" message but no escalation JSON →
        // it found something, auto-fixed it, and confirmed the app is up.
        // The CURRENT files reflect both the primary agent's intent + the
        // verify fixes; that's the snapshot we want.
        void snapshotProjectVersion(input, sessionID)
        return
      }

      // Track per-signature attempts. We hash the error message (first 80 chars)
      // so "same error keeps reappearing" gets blocked by PER_SIGNATURE_BUDGET.
      const signature = escalate.error.slice(0, 80)
      state.lastErrorSignature = signature
      state.signatureAttempts.set(
        signature,
        (state.signatureAttempts.get(signature) ?? 0) + 1,
      )

      // Hand the error back to the main build agent — never to verify again,
      // never to any other subagent.
      log.info("escalating to main agent", {
        sessionID,
        signature,
        suspected: escalate.suspected_files,
      })
      try {
        await input.client.session.promptAsync({
          path: { id: sessionID },
          body: {
            agent: PRIMARY_AGENT,
            parts: [
              {
                type: "text",
                text:
                  `The post-completion check found an issue I couldn't auto-fix: ${escalate.error}\n` +
                  `Suspected files: ${escalate.suspected_files.join(", ") || "(unknown)"}\n` +
                  `Please address this and confirm when done. Do not announce that a check ran — ` +
                  `the user already saw the sanitized status message.`,
              },
            ],
          },
        })
      } catch (err) {
        log.error("escalation promptAsync failed", { sessionID, error: String(err) })
      }
    },
  }
}

export default ForgeVerifyPlugin
