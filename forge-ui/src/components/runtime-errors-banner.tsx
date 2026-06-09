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

// Browser clipboard with execCommand fallback for insecure contexts. The
// banner is the user's only practical path to copy errors that are rendered
// inside a cross-origin preview iframe (Next.js dev overlay), where the
// parent document can't read the iframe's text selection. Inlined rather
// than imported to keep the banner a self-contained widget.
const copyToClipboard = async (text: string): Promise<void> => {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const ta = document.createElement("textarea")
    ta.value = text
    ta.style.position = "fixed"
    ta.style.opacity = "0"
    document.body.appendChild(ta)
    ta.select()
    try { document.execCommand("copy") } catch { /* ignore */ }
    document.body.removeChild(ta)
  }
}

const formatError = (e: RuntimeError): string => {
  const where = e.file ? ` (${e.file}${e.line ? `:${e.line}` : ""})` : ""
  const summary = e.detail || e.message || e.signature || "unknown error"
  const ctx = e.url ? ` — ${e.url}${e.status ? ` → ${e.status}` : ""}` : ""
  return `[${e.source}] ${e.signature ?? "error"}: ${summary}${where}${ctx}`
}

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
  // Expanded view shows ALL errors as a selectable <pre>, not just the
  // first-line summary. Without this the user can see "12 errors" in the
  // banner but has no way to read past error #1 short of opening devtools.
  const [expanded, setExpanded] = createSignal(false)

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
    const lines = list.slice(0, 10).map((e) => `- ${formatError(e)}`)
    return [
      `${list.length} runtime error${list.length > 1 ? "s" : ""} detected in this project's preview:`,
      "",
      ...lines,
      "",
      "Diagnose the root cause and fix in-place. After fixing, DELETE the queue via the runtime-errors endpoint.",
    ].join("\n")
  }

  // Raw error text only — no prompt wrapper. Used by the Copy button so
  // the user can paste error text into anywhere (chat, devtools, bug
  // report) without the "Diagnose the root cause…" suffix getting in
  // the way.
  const buildRawErrorText = () => errors().map(formatError).join("\n")

  const handleCopyRaw = async () => {
    const text = buildRawErrorText()
    if (!text) return
    await copyToClipboard(text)
    showToast({ title: `Copied ${errors().length} error${errors().length > 1 ? "s" : ""}` })
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
        class="border-b border-amber-500/30 bg-amber-500/10 text-amber-800 dark:text-amber-200 text-sm"
        role="status"
      >
        <div class="flex items-center gap-3 px-4 py-2">
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
            class="text-xs opacity-70 hover:opacity-100 underline underline-offset-2"
            onClick={() => setExpanded((v) => !v)}
            title={expanded() ? "Hide details" : "Show all errors"}
          >
            {expanded() ? "Hide" : "Details"}
          </button>
          <button
            type="button"
            class="text-xs opacity-70 hover:opacity-100 underline underline-offset-2"
            onClick={() => void handleCopyRaw()}
            title="Copy raw error text to clipboard"
          >
            Copy
          </button>
          <button
            type="button"
            class="px-3 py-1 rounded-md bg-amber-600 text-white text-xs font-medium hover:bg-amber-700 disabled:opacity-50"
            disabled={loading()}
            onClick={() => void handleFix()}
          >
            {loading() ? "Copying…" : "Fix now"}
          </button>
        </div>
        <Show when={expanded()}>
          {/*
            Selectable details panel. Plain <pre> with explicit user-select:text
            so the Forge UI's global rules don't accidentally suppress
            selection. Capped height so a flood of errors doesn't push the
            chat area off-screen.
          */}
          <pre
            class="m-0 mx-4 mb-2 max-h-48 overflow-auto rounded-md border border-amber-500/30 bg-black/40 p-2 text-[11px] leading-relaxed text-amber-100/90 whitespace-pre-wrap break-words"
            style="user-select:text;-webkit-user-select:text"
          >{buildRawErrorText()}</pre>
        </Show>
      </div>
    </Show>
  )
}
