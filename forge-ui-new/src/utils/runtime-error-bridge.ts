/**
 * runtime-error-bridge
 * ====================
 * One global window listener that picks up `forge:runtime-error` postMessages
 * coming from preview iframes (any project, any panel) and forwards each one
 * to forge-server's runtime-errors endpoint.
 *
 * Why a single global listener (not per-iframe):
 *   The preview iframe appears in several places (session side-panel,
 *   mobile-preview-panel, home, design-view). Wiring a listener at each
 *   site invites drift; installing once at app boot guarantees consistent
 *   capture regardless of which panel hosts the iframe.
 *
 * Why we extract project_id from event.origin:
 *   The bridge script inside the iframe runs on the preview origin
 *   (e.g. http://abc123.preview.lvh.me). It doesn't know the Forge
 *   project_id by name — only its own host — so the parent derives it
 *   from event.origin. This also IS the trust boundary: we only forward
 *   if the origin matches the *.preview.<domain> pattern.
 *
 * Failure modes (intentional, not bugs):
 *   - If forge-server is unreachable, the POST fails silently. The next
 *     error will retry. We don't queue, because a queue here is a memory
 *     leak waiting to happen (a render-loop error fires hundreds/sec).
 *   - If origin doesn't match the preview pattern, the message is dropped.
 *     This stops random pages from spoofing errors into projects.
 */

const PREVIEW_HOST_RE = /^(?:https?:)?\/\/([0-9a-f-]{8,})\.preview\.(lvh\.me|forge\.com|forge\.localhost)(?::\d+)?$/i

const FORGE_API_URL: string =
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ((import.meta as any).env?.VITE_FORGE_API_URL as string | undefined) ?? ""

type BridgePayload = {
  source: "browser"
  signature?: string
  message: string
  detail?: string
  file?: string
  line?: number
  column?: number
  stack?: string
  url?: string
  status?: number
  userAgent?: string
}

let installed = false

export function installRuntimeErrorBridge(): void {
  if (installed) return
  if (typeof window === "undefined") return
  installed = true

  window.addEventListener("message", (ev: MessageEvent) => {
    const data = ev.data
    if (!data || typeof data !== "object") return
    if (data.type !== "forge:runtime-error") return

    const m = PREVIEW_HOST_RE.exec(ev.origin)
    if (!m) return
    const projectId = m[1]

    const payload = data.payload as BridgePayload | undefined
    if (!payload || typeof payload.message !== "string") return

    // Fire-and-forget. If forge-server is down we lose the breadcrumb —
    // acceptable for v1; the watcher will still catch server-side errors.
    void fetch(`${FORGE_API_URL}/api/projects/${projectId}/runtime-errors`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // Bridge POST is unauthenticated; forge-server gates on Origin header.
      body: JSON.stringify(payload),
      credentials: "omit",
      keepalive: true,
    }).catch(() => {
      /* swallow — see file header */
    })
  })
}
