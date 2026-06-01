/**
 * fix-this-screen.ts
 * ==================
 * One-click "the preview is broken, fix it" orchestrator used by the
 * preview toolbar (desktop + mobile).
 *
 * Why this exists separately from `runtime-errors-banner`:
 *   The banner is reactive — it polls the runtime-errors ring and only
 *   surfaces when something has already been captured by the in-iframe
 *   bridge or the docker log watcher. It MISSES things like:
 *     - visual layout breakage (no JS error, just looks wrong)
 *     - blank-screen / hung dev-server (bridge never loaded)
 *     - container-only errors the watcher hasn't gotten to yet
 *   This helper is the user's manual "use what you see + what's in the
 *   container right now" lever.
 *
 * What it does, in order (parallel where safe):
 *   1. Ask the preview iframe for a screenshot via postMessage. The bridge
 *      lazy-loads html2canvas from a CDN inside the iframe (same-origin to
 *      its own DOM) and replies with a JPEG dataURL. We time out after 4s —
 *      if the bridge isn't there or the page is hung, we proceed without it.
 *   2. POST /api/projects/{id}/verify — the existing endpoint that tails
 *      docker logs, parses error signatures, and probes endpoints. This
 *      already does the "watch docker logs and extract errors" work the
 *      user asked for; no new backend endpoint needed.
 *   3. Build a compact prompt (screenshot attachment + error summary) and
 *      dispatch `forge:prefill-prompt` so the prompt-input fills itself.
 *
 * Cost shape (per CLAUDE.md §2 + §3):
 *   - Per container: zero. html2canvas is only loaded inside the iframe
 *     when the user clicks; no cold-start tax across 100k containers.
 *   - Per click: 1 verify call (existing), 1 screenshot (~100–300KB JPEG).
 *     Image tokens are non-trivial — that's why this is opt-in, not auto.
 */

const SCREENSHOT_TIMEOUT_MS = 4_000

export type VerifyLogError = {
  signature: string
  detail:    string
  line:      string
}

export type VerifyEndpointProbe = {
  path:         string
  status:       number
  body_snippet: string
  error?:       string | null
}

export type VerifyReport = {
  container_status: string
  preview_url:      string
  health_ok:        boolean
  endpoint_probes:  VerifyEndpointProbe[]
  log_errors:       VerifyLogError[]
  fatal:            boolean
  summary:          string
}

export type FixPrefillPayload = {
  text:  string
  image?: {
    filename: string
    mime:     string
    dataUrl:  string
  }
}

/**
 * Request a screenshot from the in-iframe bridge. Resolves to a JPEG dataURL
 * or null if the bridge doesn't reply within the timeout (page hung, bridge
 * not present, cross-origin postMessage refused, etc.).
 *
 * We don't throw on timeout — a missing screenshot still leaves the user
 * with useful log-derived context.
 */
export function requestPreviewScreenshot(
  iframe: HTMLIFrameElement | undefined,
  timeoutMs: number = SCREENSHOT_TIMEOUT_MS,
): Promise<string | null> {
  return new Promise((resolve) => {
    const win = iframe?.contentWindow
    if (!iframe || !win) {
      resolve(null)
      return
    }

    const requestId = `forge-shot-${Date.now()}-${Math.random().toString(16).slice(2)}`
    let settled = false

    const cleanup = () => {
      window.removeEventListener("message", onMessage)
      clearTimeout(timer)
    }

    const onMessage = (ev: MessageEvent) => {
      const data = ev.data
      if (!data || typeof data !== "object") return
      if (data.type !== "forge:screenshot-response") return
      if (data.requestId !== requestId) return
      if (settled) return
      settled = true
      cleanup()
      const url = typeof data.dataUrl === "string" ? data.dataUrl : null
      resolve(url)
    }

    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      cleanup()
      resolve(null)
    }, timeoutMs)

    window.addEventListener("message", onMessage)

    try {
      win.postMessage(
        { type: "forge:screenshot-request", requestId },
        // We can't tighten this to the preview origin without re-parsing it
        // every call; the bridge ignores anything that isn't a known type, so
        // "*" is safe here. The bridge gates the *reply* on event.source.
        "*",
      )
    } catch {
      if (!settled) {
        settled = true
        cleanup()
        resolve(null)
      }
    }
  })
}

/**
 * Build the prompt text the agent receives. Compact on purpose — per §3,
 * every token re-bills against the user's BYOK key on every subsequent turn.
 * Top 5 errors only; raw log lines truncated to 240 chars.
 */
export function buildFixPromptText(report: VerifyReport | null): string {
  const intro =
    "The preview is broken. I've attached a screenshot of what I'm seeing." +
    " Please diagnose the root cause and fix it in-place."

  if (!report) return intro

  const bad =
    report.endpoint_probes?.filter(
      (p) => p.status === 0 || p.status >= 500,
    ) ?? []
  const errs = (report.log_errors ?? []).slice(0, 5)

  const lines: string[] = [intro, ""]

  lines.push(`Container: ${report.container_status} • dev server ${report.health_ok ? "responding" : "NOT responding"}`)
  if (report.summary) lines.push(`Summary:   ${report.summary}`)

  if (errs.length > 0) {
    lines.push("", `Container log errors (${errs.length}):`)
    for (const e of errs) {
      const detail = e.detail ? `: ${e.detail}` : ""
      const tail = e.line ? `  ↳ ${e.line.slice(0, 240)}` : ""
      lines.push(`  • ${e.signature}${detail}`)
      if (tail) lines.push(tail)
    }
  }

  if (bad.length > 0) {
    lines.push("", `Endpoint probes failing:`)
    for (const p of bad.slice(0, 3)) {
      const why = p.status === 0 ? (p.error ?? "no response") : `HTTP ${p.status}`
      lines.push(`  • ${p.path} → ${why}`)
    }
  }

  return lines.join("\n")
}

/**
 * Dispatch the prefill event the prompt-input listens for. Separate so the
 * caller can preview the payload (e.g. in tests) before sending.
 */
export function dispatchPrefillPrompt(payload: FixPrefillPayload): void {
  window.dispatchEvent(
    new CustomEvent<FixPrefillPayload>("forge:prefill-prompt", { detail: payload }),
  )
}
