/**
 * Forge runtime state — shared between the forge-verify plugin and the SSE
 * event-stream handler so they agree on which agent is currently active in
 * each session AND on which event types should never reach the FE.
 *
 * Filtering policy (Forge projects only — non-Forge users see everything):
 *
 *   ALWAYS hidden (every agent, every turn):
 *     - shell.started / shell.ended    → raw commands + output (leak $FORGE_*,
 *                                         /forge-skills/ paths, container internals)
 *     - reasoning.started/delta/ended  → chain-of-thought, often verbose, off-brand
 *     - agent.switched                 → leaks skill / subagent names like
 *                                         "ui-ux-pro-max" as section headers
 *     - model.switched                 → internal routing, no user value
 *     - retried                        → noise from transient provider hiccups
 *     - tool.* events for shell-class
 *       tools (shell, bash, Bash)      → same leak surface as raw shell events,
 *                                         just delivered via the generic tool path
 *
 *   ONLY hidden during the verify subagent's run (in addition to the above):
 *     - tool.input.* / tool.called / tool.progress / tool.success / tool.failed
 *       for ALL tools — verify is an internal post-completion check and the
 *       user should never see its work, only its single sanitized status line.
 *
 *   ALWAYS visible:
 *     - text.* (the actual chat)
 *     - step.started/ended/failed (so the spinner + error states work)
 *     - prompted (user message echo)
 *     - tool.* for file-writing tools (Write/Edit) during the build agent —
 *       the FE renders these as the file-tree update, which is core Forge UX.
 *     - compaction.* (low risk, useful signal)
 *
 * Failure mode is conservative: if we don't know the active agent (map empty,
 * race, restart), we still apply ALWAYS_HIDDEN. Only the verify-specific
 * extras require known state.
 */

const VERIFY_AGENT = "verify"

// Active agent per session. Set on Step.Started, cleared only when the turn
// fully ends (Step.Ended with finish === "stop") so multi-step verify runs
// keep the same active-agent flag throughout.
const activeAgent = new Map<string /* sessionID */, string>()

// Hidden in EVERY Forge session, regardless of which agent is active.
// These types either leak container/path internals or are pure noise.
const ALWAYS_HIDDEN_EVENT_TYPES = new Set<string>([
  "session.next.shell.started",
  "session.next.shell.ended",
  "session.next.reasoning.started",
  "session.next.reasoning.delta",
  "session.next.reasoning.ended",
  "session.next.agent.switched",
  "session.next.model.switched",
  "session.next.retried",
])

// Hidden ONLY while the verify subagent is active. These types are useful
// for the build agent (FE renders file writes from tool.* events) but verify
// must be invisible.
const VERIFY_ONLY_HIDDEN_EVENT_TYPES = new Set<string>([
  "session.next.tool.input.started",
  "session.next.tool.input.delta",
  "session.next.tool.input.ended",
  "session.next.tool.called",
  "session.next.tool.progress",
  "session.next.tool.success",
  "session.next.tool.failed",
])

// Tool names that count as "shell-class". A tool.* event whose `tool` field
// matches any of these is treated like a raw shell event and hidden always.
// Conservative list — `Write`/`Edit`/`Read`/`Glob`/`Grep` are NOT here.
const SHELL_CLASS_TOOLS = new Set<string>([
  "shell", "Shell", "bash", "Bash",
])

// Event types that carry a `tool` field on their payload properties.
const TOOL_EVENT_TYPES = new Set<string>([
  "session.next.tool.called",
  "session.next.tool.input.started",
  "session.next.tool.input.delta",
  "session.next.tool.input.ended",
  "session.next.tool.progress",
  "session.next.tool.success",
  "session.next.tool.failed",
])

export const ForgeRuntimeState = {
  isForgeProject(): boolean {
    return Boolean(process.env.FORGE_PROJECT_ID)
  },

  /** Called from the plugin's event hook on Step.Started. */
  recordStepStarted(sessionID: string, agent: string | undefined): void {
    if (!agent) return
    activeAgent.set(sessionID, agent)
  },

  /**
   * Called from the plugin's event hook on Step.Ended.
   * Only clears the active-agent record when the turn truly ended (`stop`).
   * Multi-step turns keep their flag so the SSE filter behaves consistently
   * across all steps.
   */
  recordStepEnded(sessionID: string, finish: string | undefined): void {
    if (finish === "stop") activeAgent.delete(sessionID)
  },

  getActiveAgent(sessionID: string): string | undefined {
    return activeAgent.get(sessionID)
  },

  /**
   * The predicate the SSE handler calls per event. Returns true if the event
   * should be DROPPED before being written to the FE stream.
   *
   * Order of checks (cheapest first; SSE is hot path):
   *   1. Non-Forge session → never drop (preserves opencode behavior for
   *      everyone else).
   *   2. Type in ALWAYS_HIDDEN → drop (no session lookup needed).
   *   3. Tool event for a shell-class tool → drop.
   *   4. Type in VERIFY_ONLY_HIDDEN AND active agent is verify → drop.
   *   5. Otherwise → pass.
   *
   * Safe failure: if the session map doesn't know the active agent, we
   * still apply ALWAYS_HIDDEN. Only the verify-extras require state.
   */
  shouldHideEventFromFE(payload:
    | { type?: string; properties?: { sessionID?: string; tool?: string } }
    | undefined
  ): boolean {
    if (!payload) return false
    if (!this.isForgeProject()) return false
    const type = payload.type
    if (!type) return false

    // 1. Always-hidden types — fastest path, no session lookup.
    if (ALWAYS_HIDDEN_EVENT_TYPES.has(type)) return true

    // 2. Shell-class tool events — hide regardless of agent.
    if (TOOL_EVENT_TYPES.has(type)) {
      const tool = payload.properties?.tool
      if (tool && SHELL_CLASS_TOOLS.has(tool)) return true
    }

    // 3. Verify-only hidden — requires we know the active agent.
    if (!VERIFY_ONLY_HIDDEN_EVENT_TYPES.has(type)) return false
    const sessionID = payload.properties?.sessionID
    if (!sessionID) return false
    return activeAgent.get(sessionID) === VERIFY_AGENT
  },

  /**
   * Sanitize a list of persisted session messages before returning to the FE.
   *
   * The SSE filter only catches live events; on page refresh the FE re-fetches
   * via `session.messages` and would otherwise see every shell command, every
   * reasoning chain, and every "agent-switched" header that ever happened in
   * the session — leaking $FORGE_PROJECT_ID, /forge-skills/ paths, skill names
   * like "ui-ux-pro-max", and the chain-of-thought.
   *
   * Rules (mirror shouldHideEventFromFE):
   *   - Drop whole messages of type "shell" | "agent-switched" | "model-switched"
   *   - For type "assistant", filter content[] to drop:
   *       • parts of type "reasoning"
   *       • parts of type "tool" whose name is shell-class
   *   - Everything else passes unchanged.
   *
   * No-op for non-Forge sessions. Accepts unknown shape so callers can pass
   * either schema-typed arrays or jsonUnsafe-wrapped payloads.
   */
  sanitizeMessages<T extends { type?: string; content?: unknown }>(
    messages: ReadonlyArray<T>,
  ): T[] {
    if (!this.isForgeProject()) return messages as T[]
    const out: T[] = []
    for (const m of messages) {
      const t = m?.type
      if (t === "shell" || t === "agent-switched" || t === "model-switched") continue
      if (t === "assistant" && Array.isArray((m as any).content)) {
        const filtered = (m as any).content.filter((part: any) => {
          if (!part) return false
          if (part.type === "reasoning") return false
          if (part.type === "tool" && SHELL_CLASS_TOOLS.has(part.name)) return false
          return true
        })
        // Preserve every other field; only replace content.
        out.push({ ...(m as any), content: filtered })
        continue
      }
      out.push(m)
    }
    return out
  },
} as const
