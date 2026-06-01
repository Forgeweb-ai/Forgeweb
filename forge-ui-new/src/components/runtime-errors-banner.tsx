/**
 * RuntimeErrorsBanner
 * ===================
 * Polls forge-server's runtime-errors queue for a project and surfaces a
 * compact banner when non-empty. One-click "Fix now" copies a pre-filled
 * prompt to the clipboard so the user can paste-and-send to the agent.
 *
 * Why not auto-inject into the prompt context?
 *   The user opted for "Banner + one-click" — no token spend without
 *   confirmation. Pasting is intentional friction; it's the consent step.
 *
 * Why poll every 10s instead of subscribing to SSE?
 *   The watcher already broadcasts via SSE (/api/dev/stream), but wiring
 *   that into a top-level component requires a stream subscription per
 *   project. v1 keeps it simple: cheap GET, dedup is server-side, the
 *   queue is at most 50 entries.
 *
 * Lifecycle:
 *   - Mounts when session opens
 *   - Polls on mount, then every POLL_MS
 *   - Refetches on window focus (so refresh / tab-switch picks up new errors)
 *   - Unmounts cleanly via onCleanup
 */
import { createSignal, onMount, onCleanup, Show, For } from "solid-js"
import { showToast } from "@opencode-ai/ui/toast"
import { authedFetch } from "@/context/forge-api"

const POLL_MS = 10_000

type RuntimeError = {
  fingerprint: string
  ts: number
  source: "server" | "browser"
  signature?: string
  detail?: string
  message?: string
  file?: string
  line?: number
  url?: string
  status?: number
}

export function RuntimeErrorsBanner(props: { projectId: string }) {
  const [errors, setErrors] = createSignal<RuntimeError[]>([])
  const [loading, setLoading] = createSignal(false)

  let interval: ReturnType<typeof setInterval> | undefined
  let focusHandler: (() => void) | undefined

  const fetchOnce = async () => {
    try {
      const res = await authedFetch(`/api/projects/${props.projectId}/runtime-errors`)
      if (!res.ok) return
      const data = (await res.json()) as RuntimeError[]
      setErrors(Array.isArray(data) ? data : [])
    } catch {
      /* swallow — banner just stays at last-known state */
    }
  }

  onMount(() => {
    void fetchOnce()
    interval = setInterval(() => void fetchOnce(), POLL_MS)
    focusHandler = () => void fetchOnce()
    window.addEventListener("focus", focusHandler)
  })

  onCleanup(() => {
    if (interval) clearInterval(interval)
    if (focusHandler) window.removeEventListener("focus", focusHandler)
  })

  const buildPrompt = () => {
    const list = errors()
    if (list.length === 0) return ""
    const lines = list.slice(0, 10).map((e) => {
      const where = e.file ? ` (${e.file}${e.line ? `:${e.line}` : ""})` : ""
      const summary = e.detail || e.message || e.signature || "unknown error"
      const ctx = e.url ? ` — ${e.url}${e.status ? ` → ${e.status}` : ""}` : ""
      return `- [${e.source}] ${e.signature ?? "error"}: ${summary}${where}${ctx}`
    })
    return [
      `${list.length} runtime error${list.length > 1 ? "s" : ""} detected in this project's preview:`,
      "",
      ...lines,
      "",
      "Diagnose the root cause and fix in-place. After fixing, DELETE the queue via the runtime-errors endpoint.",
    ].join("\n")
  }

  const handleFix = async () => {
    setLoading(true)
    try {
      const text = buildPrompt()
      if (!text) return
      try {
        await navigator.clipboard.writeText(text)
        showToast({
          title: "Errors copied to chat",
          description: "Paste with ⌘V (or Ctrl+V) and send to fix.",
        })
      } catch {
        // Clipboard blocked (e.g. non-secure context). Fall back to a
        // window event the prompt-input can opt into later.
        window.dispatchEvent(
          new CustomEvent("forge:prefill-prompt", { detail: { text } }),
        )
        showToast({ title: "Errors ready — open chat to paste" })
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <Show when={errors().length > 0}>
      <div
        class="flex items-center gap-3 px-4 py-2 border-b border-amber-500/30 bg-amber-500/10 text-amber-800 dark:text-amber-200 text-sm"
        role="status"
      >
        <span aria-hidden="true">⚠</span>
        <div class="flex-1 min-w-0">
          <strong class="font-medium">
            {errors().length} runtime error{errors().length > 1 ? "s" : ""} in preview
          </strong>
          <span class="ml-2 opacity-80 truncate">
            <For each={errors().slice(0, 1)}>
              {(e) => (
                <span>
                  {e.signature ?? "error"}: {e.detail || e.message || ""}
                </span>
              )}
            </For>
          </span>
        </div>
        <button
          type="button"
          class="px-3 py-1 rounded-md bg-amber-600 text-white text-xs font-medium hover:bg-amber-700 disabled:opacity-50"
          disabled={loading()}
          onClick={() => void handleFix()}
        >
          {loading() ? "Copying…" : "Fix now"}
        </button>
      </div>
    </Show>
  )
}
