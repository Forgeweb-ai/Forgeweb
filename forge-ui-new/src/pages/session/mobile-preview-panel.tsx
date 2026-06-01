/**
 * MobilePreviewPanel
 * ------------------
 * Self-contained preview iframe for the mobile "Changes" tab.
 * Manages its own forge container state (start / SSE / ping).
 */
import { Match, Show, Switch, createEffect, createMemo, createSignal, onCleanup } from "solid-js"
import { useForgeApi, type ContainerStatus } from "@/context/forge-api"
import { useSDK } from "@/context/sdk"
import { useSync } from "@/context/sync"
import { showToast } from "@opencode-ai/ui/toast"
import {
  buildFixPromptText,
  dispatchPrefillPrompt,
  requestPreviewScreenshot,
} from "@/utils/fix-this-screen"

export function MobilePreviewPanel() {
  const forge  = useForgeApi()
  const sdk    = useSDK()
  const sync   = useSync()

  const [containerStatus, setContainerStatus] = createSignal<ContainerStatus>("not_found")
  const [previewUrl, setPreviewUrl]           = createSignal<string | null>(null)
  const [logs, setLogs]                       = createSignal("")
  const [showLogs, setShowLogs]               = createSignal(false)
  let iframeRef: HTMLIFrameElement | undefined

  const forgeProjectId = createMemo((): string | null => {
    const dir = sdk.directory
    if (!dir) return null
    const m = dir.match(/\/projects\/([a-f0-9-]{8,}[a-f0-9])\/workspace/)
    return m?.[1] ?? null
  })

  const isRunning  = () => containerStatus() === "running"
  const isStarting = () => ["starting", "creating", "installing"].includes(containerStatus())

  const statusLabel = () => {
    switch (containerStatus()) {
      case "starting":   return "Starting…"
      case "creating":   return "Creating container…"
      case "installing": return "Installing dependencies…"
      default:           return "Starting…"
    }
  }

  // preview_url is an absolute http(s) URL — Traefik routes by host header.
  const iframeSrc = createMemo(() => previewUrl() || null)

  let _ensureInFlight = false
  const handleRunApp = async () => {
    if (_ensureInFlight) return
    const pid = forgeProjectId()
    if (!pid) return
    _ensureInFlight = true
    setContainerStatus("starting")
    try {
      const result = await forge.ensure(pid, sdk.directory ?? undefined, sync.project?.name ?? undefined)
      setContainerStatus(result.status as ContainerStatus)
      if (result.preview_url) {
        const pu = result.preview_url
        setPreviewUrl(pu.startsWith("/") ? `${forge.baseUrl}${pu}` : pu)
      }
    } catch {
      setContainerStatus("crashed")
    } finally {
      _ensureInFlight = false
    }
  }

  const fetchLogs = async () => {
    const pid = forgeProjectId()
    if (!pid) return
    try {
      const res = await fetch(
        `${forge.baseUrl}/api/dev/logs?project_id=${pid}&tail=60`,
        { headers: { Authorization: `Bearer ${localStorage.getItem("forge_jwt") ?? ""}` } },
      )
      if (res.ok) {
        const data = (await res.json()) as { logs: string }
        setLogs(data.logs || "(no output yet)")
      }
    } catch { /* ignore */ }
  }

  // SSE subscription
  createEffect(() => {
    const pid = forgeProjectId()
    if (!pid) return
    const unsub = forge.subscribeStatus(
      pid,
      (e) => setContainerStatus(e.status),
      (err) => console.warn("forge SSE error", err),
    )
    const pingInterval = setInterval(() => void forge.ping(pid), 2 * 60 * 1000)
    onCleanup(() => { unsub(); clearInterval(pingInterval) })
  })

  // Auto-ensure on mount / status change
  createEffect(() => {
    const pid = forgeProjectId()
    if (!pid) return
    const s = containerStatus()
    if (s === "starting" || s === "creating" || s === "installing" || s === "running") return
    if (s === "not_found") {
      void forge.getStatus(pid).then((live) => {
        setContainerStatus(live.status as ContainerStatus)
        if (live.preview_url) {
          const pu = live.preview_url
          setPreviewUrl(pu.startsWith("/") ? `${forge.baseUrl}${pu}` : pu)
        }
        if (live.status === "not_found" || live.status === "stopped" || live.status === "sleeping") {
          void handleRunApp()
        }
      }).catch(() => void handleRunApp())
      return
    }
    if (s === "stopped" || s === "sleeping") void handleRunApp()
  })

  // Poll logs while starting
  createEffect(() => {
    if (!isStarting() && containerStatus() !== "crashed") return
    const pid = forgeProjectId()
    if (!pid) return
    void fetchLogs()
    const iv = setInterval(() => void fetchLogs(), 3000)
    onCleanup(() => clearInterval(iv))
  })

  const handleRefresh = () => {
    try { iframeRef?.contentWindow?.location.reload() }
    catch { if (iframeRef) { const s = iframeRef.src; iframeRef.src = ""; iframeRef.src = s } }
  }

  // Mirrors handleFixThis in session-side-panel.tsx. Kept duplicated rather
  // than lifted to a hook because the two panels live in different layout
  // contexts and the local iframeRef is the only meaningful shared input.
  const [fixLoading, setFixLoading] = createSignal(false)
  const handleFixThis = async () => {
    if (fixLoading()) return
    const pid = forgeProjectId()
    if (!pid) return
    setFixLoading(true)
    try {
      const [shot, report] = await Promise.all([
        requestPreviewScreenshot(iframeRef).catch(() => null),
        forge.verify(pid).catch(() => null),
      ])
      const text = buildFixPromptText(report)
      dispatchPrefillPrompt({
        text,
        image: shot
          ? { filename: "preview.jpg", mime: "image/jpeg", dataUrl: shot }
          : undefined,
      })
      showToast({
        title: shot ? "Captured preview + errors" : "Captured errors (no screenshot)",
        description: "Open chat, review, and send.",
      })
    } finally {
      setFixLoading(false)
    }
  }

  return (
    <div class="flex flex-col h-full bg-background-base overflow-hidden">
      {/* Mini toolbar */}
      <div class="h-11 border-b border-border-weaker-base bg-background-base flex items-center gap-2 px-3 shrink-0">
        <div class="urlbar flex-1 min-w-0">
          <div class="dot-row"><span/><span/><span/></div>
          <span class="truncate">{sync.project?.name || "app"}.preview.forge.app</span>
        </div>
        <Show when={!isRunning()}>
          <button
            class="showcase-start-btn"
            disabled={isStarting()}
            onClick={() => void handleRunApp()}
          >
            <svg class="size-3" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
            <span>{isStarting() ? "Starting…" : "Start"}</span>
          </button>
        </Show>
        <button
          class="icon-btn"
          title="Send screenshot + errors to chat"
          disabled={fixLoading() || !isRunning()}
          onClick={() => void handleFixThis()}
        >
          <Show
            when={!fixLoading()}
            fallback={<span class="forge-css-spinner" style={{ width: "12px", height: "12px", "border-width": "2px" }} />}
          >
            <svg class="size-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 8v4M12 16h.01"/>
              <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
            </svg>
          </Show>
        </button>
        <button class="icon-btn" title="Refresh" onClick={handleRefresh}>
          <svg class="size-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
            <path d="M3 12a9 9 0 0 1 16-5.7L21 8M21 3v5h-5M21 12a9 9 0 0 1-16 5.7L3 16M3 21v-5h5"/>
          </svg>
        </button>
      </div>

      {/* Content */}
      <div class="flex-1 min-h-0 overflow-hidden relative">
        <Switch>
          <Match when={isRunning() && iframeSrc()}>
            <iframe
              ref={iframeRef}
              src={iframeSrc()!}
              class="w-full h-full border-0 bg-white"
              title="App Preview"
              sandbox="allow-scripts allow-same-origin allow-forms allow-modals allow-popups"
            />
          </Match>
          <Match when={isStarting()}>
            <div class="building-stage" style="justify-content:flex-start;padding:16px;overflow:hidden">
              <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-shrink:0">
                <div class="build-orb" style="margin:0" />
                <span class="build-label" style="margin:0">{statusLabel()}</span>
                <button
                  style="margin-left:auto;font-size:11px;opacity:0.5;background:none;border:none;cursor:pointer;color:inherit;text-decoration:underline"
                  onClick={() => { setShowLogs(v => !v); void fetchLogs() }}
                >{showLogs() ? "Hide logs" : "Show logs"}</button>
              </div>
              <Show when={!showLogs()}>
                <div class="build-sub">This usually takes 15–30 seconds</div>
              </Show>
              <Show when={showLogs()}>
                <pre style="flex:1;overflow:auto;background:#0a0a0a;border:1px solid #222;border-radius:6px;padding:10px;font-size:11px;line-height:1.5;color:#aaa;white-space:pre-wrap;word-break:break-all;text-align:left;width:100%;min-height:0">{logs() || "Waiting for output…"}</pre>
              </Show>
            </div>
          </Match>
          <Match when={containerStatus() === "crashed"}>
            <div class="building-stage">
              <span class="build-label" style="color:#f87171">Container crashed</span>
              <div class="build-sub mb-4">Something went wrong starting the dev server</div>
              <button class="twk-btn" onClick={() => void handleRunApp()}>Retry</button>
            </div>
          </Match>
          <Match when={true}>
            <div class="building-stage">
              <div class="build-label">Ready when you are</div>
              <div class="build-sub mb-4">Press Start to spin up a live preview</div>
              <button class="twk-btn" onClick={() => void handleRunApp()}>Start App</button>
            </div>
          </Match>
        </Switch>
      </div>
    </div>
  )
}
