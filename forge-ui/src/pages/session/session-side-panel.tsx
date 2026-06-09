import { For, Match, Show, Switch, createEffect, createMemo, createSignal, on, onCleanup, onMount, type JSX } from "solid-js"
import { createStore } from "solid-js/store"
import { createMediaQuery } from "@solid-primitives/media"
import { Tabs } from "@opencode-ai/ui/tabs"
import { IconButton } from "@opencode-ai/ui/icon-button"
import { Icon } from "@opencode-ai/ui/icon"
import { useSDK } from "@/context/sdk"
import { TooltipKeybind } from "@opencode-ai/ui/tooltip"
import { ResizeHandle } from "@opencode-ai/ui/resize-handle"
import { DragDropProvider, DragDropSensors, DragOverlay, SortableProvider, closestCenter } from "@thisbeyond/solid-dnd"
import type { DragEvent } from "@thisbeyond/solid-dnd"
import type { SnapshotFileDiff, VcsFileDiff } from "@opencode-ai/sdk/v2"
import { ConstrainDragYAxis, getDraggableId } from "@/utils/solid-dnd"
import { useDialog } from "@opencode-ai/ui/context/dialog"
import { useForgeApi, type ContainerStatus } from "@/context/forge-api"
import {
  buildFixPromptText,
  dispatchPrefillPrompt,
  requestPreviewScreenshot,
} from "@/utils/fix-this-screen"
import {
  dispatchVisualEditPrefill,
  enterSelectMode,
  exitSelectMode,
  onSelectPick,
} from "@/utils/visual-edit"
import { showToast } from "@opencode-ai/ui/toast"

import FileTree from "@/components/file-tree"
import { SessionContextUsage } from "@/components/session-context-usage"
import { SessionContextTab, SortableTab, FileVisual, VersionDropdown } from "@/components/session"
import { ForgeMark } from "@/components/forge-logo"
import { useCommand } from "@/context/command"
import { useFile, type SelectedLineRange } from "@/context/file"
import { useLanguage } from "@/context/language"
import { useLayout } from "@/context/layout"
import { usePlatform } from "@/context/platform"
import { useSettings } from "@/context/settings"
import { useSync } from "@/context/sync"
import { createFileTabListSync } from "@/pages/session/file-tab-scroll"
import { FileTabContent } from "@/pages/session/file-tabs"
import { DataPanel } from "@/pages/session/data-panel"
import { createOpenSessionFileTab, createSessionTabs, getTabReorderIndex, type Sizing } from "@/pages/session/helpers"
import { setSessionHandoff } from "@/pages/session/handoff"
import { useSessionLayout } from "@/pages/session/session-layout"

type RenderDiff = (SnapshotFileDiff & { file: string }) | VcsFileDiff

function renderDiff(value: SnapshotFileDiff | VcsFileDiff): value is RenderDiff {
  return typeof value.file === "string"
}

export function SessionSidePanel(props: {
  canReview: () => boolean
  diffs: () => (SnapshotFileDiff | VcsFileDiff)[]
  diffsReady: () => boolean
  empty: () => string
  hasReview: () => boolean
  reviewCount: () => number
  reviewPanel: () => JSX.Element
  activeDiff?: string
  focusReviewDiff: (path: string) => void
  reviewSnap: boolean
  size: Sizing
}) {
  const layout = useLayout()
  const platform = usePlatform()
  const settings = useSettings()
  const sync = useSync()
  const file = useFile()
  const language = useLanguage()
  const command = useCommand()
  const dialog = useDialog()
  const { sessionKey, tabs, view, params } = useSessionLayout()

  const sdk    = useSDK()
  const forge  = useForgeApi()
  const [device, setDevice] = createSignal<"desktop" | "tablet" | "mobile">("desktop")

  const [searchQuery, setSearchQuery] = createSignal("")
  const [matchedFiles, setMatchedFiles] = createSignal<string[] | undefined>(undefined)

  createEffect(() => {
    const query = searchQuery().trim()
    if (!query) {
      setMatchedFiles(undefined)
      return
    }
    file.searchFiles(query).then((results) => {
      setMatchedFiles(results)
    })
  })
  const [containerStatus, setContainerStatus] = createSignal<ContainerStatus>("not_found")
  const [previewUrl, setPreviewUrl] = createSignal<string | null>(null)
  const [containerLogs, setContainerLogs] = createSignal<string>("")
  const [showLogs, setShowLogs] = createSignal(false)
  // True from version-revert click until the iframe has been hard-reloaded.
  // Drives a Forge-branded overlay on the preview so the user doesn't see
  // a blank flash or a 502 mid-restart. Kept narrow (no other code paths
  // set it) to avoid blocking the preview on unrelated container events.
  const [isReverting, setIsReverting] = createSignal(false)
  const [isShowcased, setIsShowcased] = createSignal(false)
  const [showcasing, setShowcasing] = createSignal(false)
  // Showcase save modal state
  const [showShowcaseModal, setShowShowcaseModal] = createSignal(false)
  const [showcaseName, setShowcaseName] = createSignal("")
  const [showcaseDesc, setShowcaseDesc] = createSignal("")
  const [showcaseSaving, setShowcaseSaving] = createSignal(false)
  // Thumbnail upload state
  const [thumbnailDataUrl, setThumbnailDataUrl] = createSignal<string | null>(null)
  let thumbnailInputRef: HTMLInputElement | undefined
  let iframeRef: HTMLIFrameElement | undefined

  // HMR WebSocket is now proxied by forge-server, so Next.js/Vite push file-change
  // events directly to the iframe — no manual reload needed.
  // We keep a fallback reload for cases where HMR can't reconnect (e.g. the
  // container restarted while the preview was open).
  createEffect(() => {
    if (activeTab() !== "preview") return
    if (containerStatus() !== "running") return

    let reloadTimer: ReturnType<typeof setTimeout> | undefined

    const unsub = sdk.event.listen((e: any) => {
      if (e.type !== "file.edited" && e.type !== "file.watcher.updated") return
      // Debounce: if HMR is working, Next.js will hot-reload before this fires.
      // If HMR is not connected (e.g. container restarted), do a hard reload.
      clearTimeout(reloadTimer)
      reloadTimer = setTimeout(() => {
        // Only reload if HMR didn't already update the page
        try {
          iframeRef?.contentWindow?.location.reload()
        } catch {
          if (iframeRef) { const s = iframeRef.src; iframeRef.src = ""; iframeRef.src = s }
        }
      }, 3000)  // longer delay — gives HMR time to do its job first
    })

    onCleanup(() => {
      unsub()
      clearTimeout(reloadTimer)
    })
  })

  /**
   * The canonical Forge project ID, extracted from the workspace path.
   *
   * OpenCode's project ID is "global" for the default workspace — not a useful
   * container identifier. Forge stores the real UUID in the workspace path:
   *   .../forge-data/users/{user_id}/projects/{uuid}/workspace
   * We extract that UUID so every container/SSE call uses the correct ID.
   */
  const forgeProjectId = createMemo((): string | null => {
    const dir = sdk.directory
    if (!dir) return null
    const m = dir.match(/\/projects\/([a-f0-9-]{8,}[a-f0-9])\/workspace/)
    return m?.[1] ?? null
  })

  // Reset container state whenever the workspace (project) changes so the new
  // project's container is started fresh (auto-ensure will fire on "not_found").
  createEffect(on(
    forgeProjectId,
    () => {
      setContainerStatus("not_found")
      setPreviewUrl(null)
    },
    { defer: true },
  ))

  // Derived helpers
  const isRunning   = () => containerStatus() === "running"
  const isStarting  = () => ["starting", "creating", "installing"].includes(containerStatus())
  const statusLabel = () => {
    switch (containerStatus()) {
      case "starting":   return "Starting…"
      case "creating":   return "Creating container…"
      case "installing": return "Installing dependencies…"
      case "running":    return "Running"
      case "crashed":    return "Crashed"
      case "sleeping":   return "Sleeping"
      default:           return "Not started"
    }
  }

  // SSE subscription — connect whenever the preview tab is active
  createEffect(() => {
    if (activeTab() !== "preview") return
    const projectId = forgeProjectId()
    if (!projectId) return

    // Subscribe to live status updates
    const unsub = forge.subscribeStatus(
      projectId,
      (event) => {
        setContainerStatus(event.status)
      },
      (err) => console.warn("forge SSE error", err),
    )

    // Ping every 2 minutes while preview is open to prevent sleep
    const pingInterval = setInterval(() => {
      void forge.ping(projectId)
    }, 2 * 60 * 1000)

    onCleanup(() => {
      unsub()
      clearInterval(pingInterval)
    })
  })

  // Auto-ensure on first open of preview tab.
  // We first fetch the real backend status so we don't re-launch a container
  // that is already running (which happens if containerStatus is "not_found"
  // only because we haven't heard from the SSE stream yet).
  createEffect(() => {
    if (activeTab() !== "preview") return
    const projectId = forgeProjectId()
    if (!projectId) return

    const s = containerStatus()
    // "starting/creating/installing" = already in-flight, do nothing
    if (s === "starting" || s === "creating" || s === "installing") return
    // "running" = already up, iframe is showing, do nothing
    if (s === "running") return

    if (s === "not_found") {
      // Fetch real backend status before deciding to launch — avoids re-launching
      // a running container on every tab re-enter before SSE has responded.
      void forge.getStatus(projectId).then((live) => {
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

    // "stopped", "sleeping", "crashed" → user-visible retry states, don't auto-launch
    // (user should press Retry explicitly for "crashed"; sleeping/stopped auto-wake is fine)
    if (s === "stopped" || s === "sleeping") {
      void handleRunApp()
    }
  })

  let _ensureInFlight = false
  const handleRunApp = async () => {
    if (_ensureInFlight) return   // prevent double-fire from reactive re-runs
    const projectId = forgeProjectId()
    if (!projectId) return

    _ensureInFlight = true
    setContainerStatus("starting")
    try {
      const result = await forge.ensure(
        projectId,
        sdk.directory ?? undefined,
        sync.project?.name ?? undefined,
      )
      setContainerStatus(result.status as ContainerStatus)
      if (result.preview_url) {
        const pu = result.preview_url
        setPreviewUrl(pu.startsWith("/") ? `${forge.baseUrl}${pu}` : pu)
      }
    } catch (e) {
      console.error("Failed to start app", e)
      setContainerStatus("crashed")
    } finally {
      _ensureInFlight = false
    }
  }

  const openShowcaseModal = () => {
    // Pre-fill with project name / description
    setShowcaseName(sync.project?.name ?? "")
    setShowcaseDesc("")
    setThumbnailDataUrl(null)
    if (thumbnailInputRef) thumbnailInputRef.value = ""
    setShowShowcaseModal(true)
  }

  const handleSaveShowcase = async () => {
    const projectId = forgeProjectId()
    if (!projectId || showcaseSaving()) return
    setShowcaseSaving(true)
    try {
      // 1. Upload thumbnail if the user selected one
      const thumb = thumbnailDataUrl()
      if (thumb) {
        try {
          await forge.uploadThumbnail(projectId, thumb)
        } catch (thumbErr) {
          console.warn("Thumbnail upload failed, continuing:", thumbErr)
        }
      }

      // 2. Save showcase metadata
      await forge.showcase(projectId, {
        name:        showcaseName().trim() || undefined,
        description: showcaseDesc().trim() || undefined,
      })
      setIsShowcased(true)
      setThumbnailDataUrl(null)
      setShowShowcaseModal(false)
    } catch (e) {
      console.error("Showcase save failed", e)
    } finally {
      setShowcaseSaving(false)
    }
  }

  const handleRefresh = async () => {
    const projectId = forgeProjectId()
    if (!projectId) return
    try {
      const s = await forge.getStatus(projectId)
      setContainerStatus(s.status as ContainerStatus)
      if (s.preview_url) {
        const pu = s.preview_url
        setPreviewUrl(pu.startsWith("/") ? `${forge.baseUrl}${pu}` : pu)
      }
    } catch {
      // ignore
    }
    // Also hard-reload the iframe in case the page is showing a stale error
    try {
      iframeRef?.contentWindow?.location.reload()
    } catch {
      if (iframeRef) { const s = iframeRef.src; iframeRef.src = ""; iframeRef.src = s }
    }
  }

  /**
   * Post-revert hook. After opencode's `session.revert` finishes, files on
   * disk change via `git checkout` — HMR sees those as one bulk mtime bump
   * (not the granular edits it's built for), so the preview iframe shows
   * stale UI. We re-ensure the container so the new workspace is actually
   * served, then reload the iframe.
   *
   * Why we have to wait before reloading: `forge.ensure` returns when the
   * ensure call is in flight, NOT when the container is healthy. Reloading
   * the iframe immediately hits Traefik → upstream not ready → 502
   * Bad Gateway (the symptom we saw in testing). We poll getStatus until
   * the container reports `running`, then reload — bounded by a hard
   * timeout so a stuck container can't hang the handler forever.
   */
  const handleVersionRestored = async () => {
    const projectId = forgeProjectId()
    if (!projectId) return

    setIsReverting(true)
    try {
    // 1. Re-ensure — wakes sleeping containers, restarts crashed ones,
    //    no-ops on healthy ones. Updates preview URL state in the result.
    try {
      const result = await forge.ensure(
        projectId,
        sdk.directory ?? undefined,
        sync.project?.name ?? undefined,
      )
      setContainerStatus(result.status as ContainerStatus)
      if (result.preview_url) {
        const pu = result.preview_url
        setPreviewUrl(pu.startsWith("/") ? `${forge.baseUrl}${pu}` : pu)
      }
    } catch (e) {
      console.error("ensure after revert failed", e)
    }

    // 2. Wait for the container to actually be `running`. We poll instead
    //    of trusting the ensure result because:
    //      - `ensure` returns synchronously after kicking off start; the
    //        dev server inside may need a few seconds to bind to its port
    //      - on a checkout that changed package.json, opencode may have
    //        triggered a pnpm install loop that we shouldn't iframe-load
    //        through
    //    Bounded at ~20s; if the container isn't up by then, reload
    //    anyway and let the user retry — better than hanging the handler.
    const POLL_MS    = 600
    const TIMEOUT_MS = 20_000
    const deadline   = Date.now() + TIMEOUT_MS
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, POLL_MS))
      try {
        const s = await forge.getStatus(projectId)
        setContainerStatus(s.status as ContainerStatus)
        if (s.preview_url) {
          const pu = s.preview_url
          setPreviewUrl(pu.startsWith("/") ? `${forge.baseUrl}${pu}` : pu)
        }
        if (s.status === "running") break
        // crashed = unrecoverable on its own; let user fix from chat
        if (s.status === "crashed") break
      } catch {
        // transient — keep polling
      }
    }

    // 3. Hard-reload the iframe. Both methods are tried because some
    //    cross-origin iframe configs block contentWindow access; the
    //    src rotation fallback always works.
    try {
      iframeRef?.contentWindow?.location.reload()
    } catch {
      if (iframeRef) { const s = iframeRef.src; iframeRef.src = ""; iframeRef.src = s }
    }
    } finally {
      // Give the iframe a beat to actually start fetching before we tear
      // down the overlay — otherwise the user sees the bad-gateway HTML
      // flash through for a frame. 400ms matches the fade-out timing the
      // build-orb uses elsewhere so the transition feels intentional.
      setTimeout(() => setIsReverting(false), 400)
    }
  }

  const fetchLogs = async () => {
    const projectId = forgeProjectId()
    if (!projectId) return
    try {
      const res = await fetch(
        `${forge.baseUrl}/api/dev/logs?project_id=${projectId}&tail=80`,
        { headers: { Authorization: `Bearer ${localStorage.getItem("forge_jwt") ?? ""}` } },
      )
      if (res.ok) {
        const data = (await res.json()) as { logs: string }
        setContainerLogs(data.logs || "(no output yet)")
      }
    } catch { /* ignore */ }
  }

  // One-click copy of the current log tail. Without this the user has to
  // either screenshot the panel (wastes image tokens when pasting to the
  // agent) or hand-drag-select inside a scrolling overflow:auto <pre>,
  // which loses the bottom lines as they keep streaming in.
  const handleCopyLogs = async () => {
    const text = containerLogs()
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
      showToast({ title: "Logs copied" })
    } catch {
      // Clipboard blocked (insecure context) — fall back to a hidden
      // textarea + execCommand. Old API but works everywhere the modern
      // clipboard does not.
      const ta = document.createElement("textarea")
      ta.value = text
      ta.style.position = "fixed"
      ta.style.opacity = "0"
      document.body.appendChild(ta)
      ta.select()
      try { document.execCommand("copy") } catch { /* ignore */ }
      document.body.removeChild(ta)
      showToast({ title: "Logs copied" })
    }
  }

  // Poll container logs while starting so the user can see what's happening
  createEffect(() => {
    if (!isStarting() && containerStatus() !== "crashed") return
    if (activeTab() !== "preview") return
    const projectId = forgeProjectId()
    if (!projectId) return

    void fetchLogs()
    const iv = setInterval(() => void fetchLogs(), 3000)
    onCleanup(() => clearInterval(iv))
  })

  const handleRestart = async () => {
    const projectId = forgeProjectId()
    if (!projectId) return
    setContainerStatus("starting")
    try {
      await forge.stop(projectId)
    } catch { /* ignore */ }
    void handleRunApp()
  }

  /**
   * "Fix this" — manual handoff to the agent for things the watcher misses
   * (visual bugs, blank screens, hung dev server). One click =
   *   1. screenshot from the in-iframe bridge (best-effort, 4s timeout)
   *   2. POST /verify → docker log errors + endpoint probes
   *   3. prefill the prompt with both, ready for the user to add detail
   *      and send.
   *
   * We run #1 and #2 in parallel; if either errors we still surface the
   * other rather than block.
   */
  /**
   * Visual-edit Select mode. Toggling the button posts forge:select-enter
   * to the iframe bridge, which renders the hover overlay and captures the
   * next click. On pick, we exit select mode, dispatch a prefill so the
   * prompt opens with the file:line anchor, and show a toast.
   *
   * Cascade safety (CLAUDE.md §3.2): the overlay lives ENTIRELY inside the
   * iframe (instrumentation-client.ts) with inline `all: initial` styles —
   * zero possibility of CSS bleed into the user's app OR into the Forge UI.
   *
   * Token shape: each pick = one prefill (~30–90 tokens of leading anchor
   * text). Flat per turn; does not grow with conversation length.
   */
  const [selectActive, setSelectActive] = createSignal(false)

  // Listener is installed once on mount; the iframe may not exist yet when
  // the user first toggles, but enterSelectMode is a no-op then anyway —
  // the listener still catches the pick when it eventually fires.
  const unsubscribePick = onSelectPick((pick) => {
    setSelectActive(false)
    dispatchVisualEditPrefill(pick)
    showToast({
      title: pick.source
        ? `Selected <${pick.tag}>`
        : `Selected <${pick.tag}> (source not stamped)`,
      description: "Type your change and send.",
    })
  })
  onCleanup(unsubscribePick)

  const handleSelectToggle = () => {
    if (selectActive()) {
      setSelectActive(false)
      exitSelectMode(iframeRef)
      return
    }
    setSelectActive(true)
    enterSelectMode(iframeRef)
  }

  /**
   * Overflow menu state — pops a small panel containing the toolbar icons
   * that don't fit when the side panel is narrow (Restart, Open-in-tab,
   * Download). Click-outside closes it.
   *
   * Why the three picked: they're the least-frequent of the seven right-
   * icons. Refresh/Fix-this/Select stay pinned (high-frequency); the rest
   * tuck under "...". Trades one tap for guaranteed visibility on any
   * panel width down to ~300px.
   */
  const [overflowOpen, setOverflowOpen] = createSignal(false)
  let overflowRef: HTMLDivElement | undefined
  onMount(() => {
    const onDocDown = (e: MouseEvent) => {
      if (!overflowOpen()) return
      if (overflowRef && overflowRef.contains(e.target as Node)) return
      setOverflowOpen(false)
    }
    document.addEventListener("mousedown", onDocDown, true)
    onCleanup(() => document.removeEventListener("mousedown", onDocDown, true))
  })

  const [fixLoading, setFixLoading] = createSignal(false)
  const handleFixThis = async () => {
    if (fixLoading()) return
    const projectId = forgeProjectId()
    if (!projectId) return
    setFixLoading(true)
    try {
      const [shot, report] = await Promise.all([
        requestPreviewScreenshot(iframeRef).catch(() => null),
        forge.verify(projectId).catch((e) => {
          console.warn("forge verify failed", e)
          return null
        }),
      ])

      const text = buildFixPromptText(report)
      dispatchPrefillPrompt({
        text,
        image: shot
          ? { filename: "preview.jpg", mime: "image/jpeg", dataUrl: shot }
          : undefined,
      })

      showToast({
        title: shot
          ? "Captured preview + container errors"
          : "Captured container errors (screenshot unavailable)",
        description: "Review the prompt, add detail if needed, then send.",
      })
    } finally {
      setFixLoading(false)
    }
  }

  // The iframe src — preview_url from the API is an absolute http(s) URL
  // (http://{project_id}.{PREVIEW_DOMAIN}/) routed by Traefik directly to
  // the project container. No proxy hop through forge-server.
  const iframeSrc = () => previewUrl() || null

  const isWorking = createMemo(() => {
    const id = forgeProjectId() || params.id
    if (!id) return false
    return sync.data.session_working(id)
  })

  // Auto-open review panel when AI starts writing, auto-switch to preview when done
  createEffect(on(
    isWorking,
    (working, prevWorking) => {
      // Building started — slide open the review panel so diffs are visible,
      // but DON'T force-switch the active tab (user may be on preview watching live).
      if (working && prevWorking === false) {
        if (!view().reviewPanel.opened()) view().reviewPanel.open()
      }
      // Building finished — switch to preview so user sees the result.
      // Only do this if we came from a confirmed working state (not SSE init flicker).
      if (!working && prevWorking === true && hasCode()) {
        tabs().open("preview")
        tabs().setActive("preview")
      }
    },
    { defer: true }
  ))

  const isDesktop = createMediaQuery("(min-width: 768px)")
  const reviewTab = createMemo(() => isDesktop())

  const normalizeTab = (tab: string) => {
    if (!tab.startsWith("file://")) return tab
    return file.tab(tab)
  }

  const tabState = createSessionTabs({
    tabs,
    pathFromTab: file.pathFromTab,
    normalizeTab,
    review: reviewTab,
    hasReview: props.canReview,
  })
  const contextOpen = tabState.contextOpen
  const openedTabs = tabState.openedTabs
  const activeTab = tabState.activeTab
  const activeFileTab = tabState.activeFileTab

  const openReviewPanel = () => {
    if (!view().reviewPanel.opened()) view().reviewPanel.open()
  }

  const openTab = createOpenSessionFileTab({
    normalizeTab,
    openTab: tabs().open,
    pathFromTab: file.pathFromTab,
    loadFile: file.load,
    openReviewPanel,
    setActive: tabs().setActive,
  })

  const handleTabChange = (value: string) => {
    if (value === "preview" || value === "data") {
      tabs().open(value)
      tabs().setActive(value)
      return
    }
    openTab(value)
  }

  const shown = createMemo(
    () =>
      platform.platform !== "desktop" ||
      import.meta.env.VITE_OPENCODE_CHANNEL !== "beta" ||
      settings.general.showFileTree(),
  )

  const reviewOpen = createMemo(() => isDesktop() && view().reviewPanel.opened())
  const fileOpen = createMemo(() => isDesktop() && shown() && (layout.fileTree.opened() || activeTab() === "review" || activeTab().startsWith("file://")))
  // The right <aside> is mounted only when the user has the review panel
  // toggled open. Outer <Show> at the return statement also gates on this so
  // the element actually unmounts (not just goes inert) when closed.
  const open = createMemo(() => isDesktop() && view().reviewPanel.opened())
  const panelWidth = createMemo(() => {
    if (!isDesktop()) return "0px"
    return `calc(100% - ${layout.session.width()}px)`
  })
  const treeWidth = createMemo(() => (fileOpen() ? `${layout.fileTree.width()}px` : "0px"))

  // Files that should never appear in the Code view or diff count
  const HIDDEN_FILES = new Set(["AGENTS.md", "CLAUDE.md", ".claude", "opencode.json"])

  const diffs = createMemo(() =>
    props.diffs().filter(renderDiff).filter((d) => {
      const name = d.file.split("/").pop() ?? d.file
      return !HIDDEN_FILES.has(name)
    }),
  )
  const diffFiles = createMemo(() => diffs().map((d) => d.file))

  // Preview is only usable once the agent has finished building AND there is code.
  // We use the file tree state directly — diffFiles only tracks session diffs and
  // can be 0 even when workspace files exist.
  const hasCode = createMemo(() => {
    const state = file.tree.state("")
    if (!state?.loaded) return false  // tree not loaded yet — assume empty
    const children = file.tree.children("").filter((n) => !["AGENTS.md", "CLAUDE.md", "opencode.json"].includes(n.name))
    return children.length > 0 || isRunning()
  })
  // Disable preview only when there is genuinely no code yet (first load, empty project).
  // Do NOT disable while AI is working — the auto-switch effect bypasses this anyway,
  // and keeping it disabled while working caused the tab to stay greyed out after finishing
  // due to reactive-graph settling order.
  const previewDisabled = createMemo(() => !hasCode() && !isWorking())

  const kinds = createMemo(() => {
    const merge = (a: "add" | "del" | "mix" | undefined, b: "add" | "del" | "mix") => {
      if (!a) return b
      if (a === b) return a
      return "mix" as const
    }

    const normalize = (p: string) => p.replaceAll("\\\\", "/").replace(/\/+$/, "")

    const out = new Map<string, "add" | "del" | "mix">()
    for (const diff of diffs()) {
      const file = normalize(diff.file)
      const kind = diff.status === "added" ? "add" : diff.status === "deleted" ? "del" : "mix"

      out.set(file, kind)

      const parts = file.split("/")
      for (const [idx] of parts.slice(0, -1).entries()) {
        const dir = parts.slice(0, idx + 1).join("/")
        if (!dir) continue
        out.set(dir, merge(out.get(dir), kind))
      }
    }
    return out
  })

  const empty = (msg: string) => (
    <div class="h-full flex flex-col">
      <div class="h-6 shrink-0" aria-hidden />
      <div class="flex-1 pb-64 flex items-center justify-center text-center">
        <div class="text-12-regular text-text-weak">{msg}</div>
      </div>
    </div>
  )

  const nofiles = createMemo(() => {
    const state = file.tree.state("")
    if (!state?.loaded) return false
    return file.tree.children("").length === 0
  })

  const fileTreeTab = () => layout.fileTree.tab()

  const setFileTreeTabValue = (value: string) => {
    if (value !== "changes" && value !== "all") return
    layout.fileTree.setTab(value)
  }

  const showAllFiles = () => {
    if (fileTreeTab() !== "changes") return
    layout.fileTree.setTab("all")
  }

  const [store, setStore] = createStore({
    activeDraggable: undefined as string | undefined,
  })

  const handleDragStart = (event: unknown) => {
    const id = getDraggableId(event)
    if (!id) return
    setStore("activeDraggable", id)
  }

  const handleDragOver = (event: DragEvent) => {
    const { draggable, droppable } = event
    if (!draggable || !droppable) return

    const currentTabs = tabs().all()
    const toIndex = getTabReorderIndex(currentTabs, draggable.id.toString(), droppable.id.toString())
    if (toIndex === undefined) return
    tabs().move(draggable.id.toString(), toIndex)
  }

  const handleDragEnd = () => {
    setStore("activeDraggable", undefined)
  }

  createEffect(() => {
    if (!file.ready()) return

    setSessionHandoff(sessionKey(), {
      files: tabs()
        .all()
        .reduce<Record<string, SelectedLineRange | null>>((acc, tab) => {
          const path = file.pathFromTab(tab)
          if (!path) return acc

          const selected = file.selectedLines(path)
          acc[path] =
            selected && typeof selected === "object" && "start" in selected && "end" in selected
              ? (selected as SelectedLineRange)
              : null

          return acc
        }, {}),
    })
  })

  return (
    <Show when={isDesktop() && view().reviewPanel.opened() && !(import.meta.env.VITE_OPENCODE_CHANNEL !== "prod" && !params.id)}>
      <>
      <aside
        id="review-panel"
        aria-label={language.t("session.panel.reviewAndFiles")}
        aria-hidden={!open()}
        inert={!open()}
        class="relative min-w-0 h-full flex shrink-0 overflow-hidden bg-background-base"
        classList={{
          "pointer-events-none": !open(),
          "transition-[width] duration-[240ms] ease-[cubic-bezier(0.22,1,0.36,1)] will-change-[width] motion-reduce:transition-none":
            !props.size.active() && !props.reviewSnap,
        }}
        style={{ width: panelWidth() }}
      >

        <Show when={open()}>
          <div class="size-full flex flex-col border-l border-border-weaker-base">
            <DragDropProvider
              onDragStart={handleDragStart}
              onDragEnd={handleDragEnd}
              onDragOver={handleDragOver}
              collisionDetector={closestCenter}
            >
              <DragDropSensors />
              <ConstrainDragYAxis />
              <Tabs value={activeTab()} onChange={handleTabChange} class="h-full flex flex-col overflow-hidden">
                
                {/* UNIFIED HEADER BAR
                    compact-toolbar = scoped CSS in index.css that shrinks
                    .icon-btn from 26→22px and .tab-seg/.device-tog buttons
                    from 32→26px within this toolbar only. Frees ~70px so
                    the 7–8 right icons (Showcase/Select/Fix/Refresh/Restart/
                    Open/Download) fit on narrow panels without overflow.
                    gap-1 (was gap-2) and px-2 (was px-3) free ~16px more. */}
                <div class="compact-toolbar h-11 border-b border-border-weaker-base bg-background-base flex items-center gap-1 px-2 shrink-0 select-none">
                  {/* Segmented Tab Control (Globe / Code / Data) */}
                  <div class="tab-seg">
                    {/* Preview (Globe) Tab */}
                    <button
                      type="button"
                      classList={{ "active": activeTab() === "preview" }}
                      onClick={() => handleTabChange("preview")}
                      title={isWorking() ? "Building…" : "Preview"}
                    >
                      <Show when={isWorking()} fallback={
                        <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                          <circle cx="12" cy="12" r="10"/>
                          <path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/>
                          <path d="M2 12h20"/>
                        </svg>
                      }>
                        <span class="forge-css-spinner" style={{ width: "14px", height: "14px", "border-width": "2px" }} />
                      </Show>
                    </button>

                    {/* Code Tab */}
                    <button
                      type="button"
                      classList={{ "active": activeTab() === "review" || activeTab().startsWith("file://") }}
                      onClick={() => handleTabChange("review")}
                      title="Code"
                    >
                      <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <polyline points="16 18 22 12 16 6"/>
                        <polyline points="8 6 2 12 8 18"/>
                      </svg>
                    </button>

                    {/* Data Tab */}
                    <button
                      type="button"
                      classList={{ "active": activeTab() === "data" }}
                      onClick={() => handleTabChange("data")}
                      title="Data"
                    >
                      <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <ellipse cx="12" cy="5" rx="8" ry="3"/>
                        <path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/>
                        <path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>
                      </svg>
                    </button>
                  </div>

                  {/* Vertical Divider */}
                  <div class="w-px h-5 bg-border-weaker-base shrink-0" />

                  {/* VERSION DROPDOWN — session-scoped, shown on every tab.
                      Backed by opencode's built-in snapshots (one per AI turn);
                      list is derived from sync.data.message[sessionID] in the
                      component itself, no forge-server round-trip. See
                      [[forge_versioning_v1]]. The forge-server versions code
                      stays dormant for the multi-tenant scale story.
                      Slot: between the tab-seg divider and the tab-conditional
                      controls block. shrink-0 + max-width 220px so it never
                      starves the icon group on the right. No parent-cascade
                      conflicts: parent compact-toolbar is `flex items-center
                      gap-1`, our root adds `relative shrink-0` only.
                      See CLAUDE.md §3.2. */}
                  <VersionDropdown
                    sessionID={params.id ?? null}
                    onRestored={() => { void handleVersionRestored() }}
                  />

                  {/* Vertical Divider after the version dropdown */}
                  <div class="w-px h-5 bg-border-weaker-base shrink-0" />

                  {/* PREVIEW CONTROLS — flex with explicit shrink priority.
                      Why not a grid:
                        Grid columns can overflow their tracks when the parent
                        is narrower than the columns' min-content. The aside
                        wrapper has overflow-hidden (line 676), so any overflow
                        gets clipped from the right — which is exactly what was
                        eating the Download icon.
                      Shrink contract:
                        - Device toggle:  shrink-0  (essential, fixed)
                        - URL bar:        shrink + min-w-0 (the ONLY victim;
                                          collapses to 0 under pressure, max
                                          capped at 200px inline so it never
                                          eats slack the icons could use)
                        - Icon group:     shrink-0  (essential, fixed)
                      The wrapping div around the URL bar uses flex-1 + justify-
                      center so the URL bar stays visually centered when there's
                      slack, and gracefully disappears (shrinks the wrapper to 0)
                      when there isn't. pr-1 gives the last icon a hair of
                      breathing room from the panel edge. */}
                  <div
                    class="flex-1 min-w-0 flex items-center gap-2 pr-1"
                    style={{ display: activeTab() === "preview" ? "flex" : "none" }}
                  >
                    {/* LEFT — device toggle. shrink-0: essential, never shrinks.
                        toolbar-device-tog = container-query target so it can be
                        hidden when toolbar < 400px (last-resort space saver). */}
                    <div class="device-tog toolbar-device-tog shrink-0">
                      <button class={device() === "desktop" ? "active" : ""} onClick={() => setDevice("desktop")} aria-label="Desktop View">
                        <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg>
                      </button>
                      <button class={device() === "tablet" ? "active" : ""} onClick={() => setDevice("tablet")} aria-label="Tablet View">
                        <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="3" width="14" height="18" rx="2"/><path d="M11 18h2"/></svg>
                      </button>
                      <button class={device() === "mobile" ? "active" : ""} onClick={() => setDevice("mobile")} aria-label="Mobile View">
                        <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="3" width="10" height="18" rx="2"/><path d="M11 18h2"/></svg>
                      </button>
                    </div>

                    {/* URL bar removed from this toolbar.
                        Why: it was decorative-only (the actual URL is reachable
                        via the Open-in-new-tab action in the overflow menu) and
                        it kept fighting the icon group for space on narrower
                        panels — every container-query / shrink-priority fix
                        added another layer of complexity without fully solving
                        it. Removing it gives the icons ~200px of guaranteed
                        room. If we want the URL bar back later, the right home
                        for it is inside the preview iframe's chrome, not the
                        toolbar that holds actions. */}

                    {/* RIGHT — icon group. shrink-0: never shrinks. ml-auto
                        pushes the group to the right. pr-14 (56px) keeps the
                        last icon clear of *something* at the panel's right
                        edge that clips it otherwise — empirically validated
                        by DevTools. Likely the ResizeHandle/scrollbar gutter
                        of an ancestor; the original code had margin-right:
                        30px for the same reason but undershot. gap-0: icons
                        sit flush; each .icon-btn (22px in compact-toolbar)
                        has its own hover bg for separation. */}
                    <div class="flex items-center shrink-0 ml-auto pr-14">
                      <Show when={!isRunning()}>
                        <button
                          class="showcase-start-btn"
                          title="Start preview"
                          disabled={isStarting()}
                          onClick={() => void handleRunApp()}
                        >
                          <svg class="size-3" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
                          <span>{isStarting() ? "Starting…" : "Start"}</span>
                        </button>
                      </Show>

                      <Show when={isShowcased()}
                        fallback={
                          <button class="icon-btn" title="Save to showcase" onClick={openShowcaseModal}>
                            <svg class="size-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                              <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z"/>
                            </svg>
                          </button>
                        }
                      >
                        <button
                          class="icon-btn"
                          style={{ color: "rgb(234 179 8)" }}
                          title="Remove from showcase"
                          onClick={async () => {
                            const projectId = forgeProjectId()
                            if (!projectId) return
                            await forge.unshowcase(projectId)
                            setIsShowcased(false)
                          }}
                        >
                          <svg class="size-3.5" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="0.5" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z"/>
                          </svg>
                        </button>
                      </Show>

                      {/* Select — visual edit. Click → enter pick mode in the
                          preview, hover any element, click to anchor a prompt
                          to that exact source location (file:line). Active
                          state is colored so it's obvious the cursor inside
                          the iframe is now in pick mode. Disabled when there's
                          nothing to point at. */}
                      <button
                        class="icon-btn"
                        title={selectActive() ? "Cancel selection (Esc)" : "Select an element to edit"}
                        disabled={!isRunning()}
                        style={selectActive() ? { color: "rgb(79, 140, 255)" } : undefined}
                        onClick={handleSelectToggle}
                      >
                        <svg class="size-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                          {/* Pointer / crosshair icon — signals "pick something on screen". */}
                          <path d="M13 13l6 6"/>
                          <path d="M3 3l7 17 2.5-7.5L20 10z"/>
                        </svg>
                      </button>

                      {/* Fix this — screenshot + container errors → prompt.
                          Disabled until the preview is actually running, since
                          there's nothing meaningful to capture before then. */}
                      <button
                        class="icon-btn"
                        title="Send screenshot + container errors to chat"
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
                      <button class="icon-btn" title="Refresh page" onClick={() => void handleRefresh()}>
                        <svg class="size-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 16-5.7L21 8M21 3v5h-5M21 12a9 9 0 0 1-16 5.7L3 16M3 21v-5h5"/></svg>
                      </button>

                      {/* OVERFLOW MENU — Restart / Open-in-tab / Download.
                          These three are the least-frequent right-icons; folding
                          them under "..." saves ~50px in the toolbar row while
                          keeping every action one click away (vs. truly hiding
                          them). Click-outside closes via the document listener
                          installed in onMount above. */}
                      <div class="relative" ref={(el) => (overflowRef = el)}>
                        <button
                          class="icon-btn hover:bg-gray-100"
                          title="More actions"
                          aria-haspopup="menu"
                          aria-expanded={overflowOpen()}
                          style={overflowOpen() ? { background: "rgb(229 231 235)" } : undefined}
                          onClick={(e) => {
                            e.stopPropagation()
                            setOverflowOpen((v) => !v)
                          }}
                        >
                          <svg class="size-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                            <circle cx="5"  cy="12" r="1.2"/>
                            <circle cx="12" cy="12" r="1.2"/>
                            <circle cx="19" cy="12" r="1.2"/>
                          </svg>
                        </button>
                        <Show when={overflowOpen()}>
                          <div
                            role="menu"
                            class="absolute right-0 top-full mt-1 flex flex-col gap-0.5 p-1 rounded-md border border-border-weaker-base bg-background-base shadow-lg"
                            style={{ "z-index": 50, "min-width": "180px" }}
                          >
                            <button
                              class="flex items-center gap-2 px-2 py-1.5 rounded text-xs text-left text-black cursor-pointer hover:bg-gray-200 transition-colors w-full"
                              onClick={() => { setOverflowOpen(false); void handleRestart() }}
                            >
                              <svg class="size-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/></svg>
                              <span>Restart container</span>
                            </button>
                            <button
                              class="flex items-center gap-2 px-2 py-1.5 rounded text-xs text-left text-black cursor-pointer hover:bg-gray-200 transition-colors w-full"
                              onClick={() => {
                                setOverflowOpen(false)
                                const s = iframeSrc()
                                if (s) platform.openLink(s)
                              }}
                            >
                              <svg class="size-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3h7v7M21 3l-9 9M19 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h6"/></svg>
                              <span>Open in new tab</span>
                            </button>
                            <button
                              class="flex items-center gap-2 px-2 py-1.5 rounded text-xs text-left text-black cursor-pointer hover:bg-gray-200 transition-colors w-full"
                              onClick={async () => {
                                setOverflowOpen(false)
                                const projectId = forgeProjectId()
                                if (!projectId) return
                                try { await forge.downloadProject(projectId, sync.project?.name) }
                                catch (e) { console.error("Download failed", e) }
                              }}
                            >
                              <svg class="size-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                                <polyline points="7 10 12 15 17 10"/>
                                <line x1="12" y1="15" x2="12" y2="3"/>
                              </svg>
                              <span>Download as ZIP</span>
                            </button>
                          </div>
                        </Show>
                      </div>
                    </div>
                  </div>

                  {/* FILE TABS — shown in code/file views, hidden in preview and data */}
                  <div
                    class="flex-1 min-w-0 h-full"
                    style={{ display: (activeTab() === "preview" || activeTab() === "data") ? "none" : "flex" }}
                  >
                    <Tabs.List
                      class="flex-1 min-w-0 h-full flex items-center bg-transparent border-b-0"
                      ref={(el: HTMLDivElement) => {
                        const stop = createFileTabListSync({ el, contextOpen })
                        onCleanup(stop)
                      }}
                    >
                      <Show when={contextOpen()}>
                        <Tabs.Trigger
                          value="context"
                          closeButton={
                            <TooltipKeybind
                              title={language.t("common.closeTab")}
                              keybind={command.keybind("tab.close")}
                              placement="bottom"
                              gutter={10}
                            >
                              <IconButton
                                icon="close-small"
                                variant="ghost"
                                class="h-5 w-5"
                                onClick={() => tabs().close("context")}
                                aria-label={language.t("common.closeTab")}
                              />
                            </TooltipKeybind>
                          }
                          hideCloseButton
                          onMiddleClick={() => tabs().close("context")}
                        >
                          <div class="flex items-center gap-2">
                            <SessionContextUsage variant="indicator" />
                            <div>{language.t("session.tab.context")}</div>
                          </div>
                        </Tabs.Trigger>
                      </Show>
                      <SortableProvider ids={openedTabs()}>
                        <For each={openedTabs()}>{(tab) => <SortableTab tab={tab} onTabClose={tabs().close} />}</For>
                      </SortableProvider>
                    </Tabs.List>
                  </div>
                </div>

                {/* MAIN SPLIT PANE AREA */}
                <div class="flex-1 flex min-h-0 overflow-hidden">
                  
                  {/* Left: File Tree Sidebar */}
                  <Show when={shown()}>
                    <div
                      id="file-tree-panel"
                      aria-hidden={!fileOpen()}
                      inert={!fileOpen()}
                      class="relative min-w-0 h-full shrink-0 overflow-hidden bg-background-stronger border-r border-border-weaker-base"
                      classList={{
                        "pointer-events-none": !fileOpen(),
                        "transition-[width] duration-200 ease-[cubic-bezier(0.22,1,0.36,1)] will-change-[width] motion-reduce:transition-none":
                          !props.size.active(),
                      }}
                      style={{ width: treeWidth() }}
                    >
                      <div class="h-full flex flex-col overflow-hidden group/filetree">
                        {/* Search box */}
                        <div class="px-3 pt-3 pb-2 shrink-0">
                          <div class="relative flex items-center">
                            <svg class="absolute left-2.5 size-3.5 text-muted pointer-events-none" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                              <circle cx="11" cy="11" r="8"/>
                              <path d="m21 21-4.3-4.3"/>
                            </svg>
                            <input
                              type="text"
                              placeholder="Search code"
                              value={searchQuery()}
                              onInput={(e) => setSearchQuery(e.currentTarget.value)}
                              class="w-full h-8 pl-8 pr-3 rounded-lg border border-hair bg-surface text-12-regular text-ink placeholder:text-muted outline-none focus:border-accent transition-colors"
                            />
                          </div>
                        </div>

                        {/* Directory Tree */}
                        <div class="flex-1 overflow-y-auto px-3">
                          <Switch>
                            <Match when={nofiles()}>
                              {empty(language.t("session.files.empty"))}
                            </Match>
                            <Match when={true}>
                              <FileTree
                                path=""
                                class="pt-1 pb-4"
                                allowed={matchedFiles()}
                                modified={diffFiles()}
                                kinds={kinds()}
                                onFileClick={(node) => openTab(file.tab(node.path))}
                              />
                            </Match>
                          </Switch>
                        </div>
                      </div>
                      <Show when={fileOpen()}>
                        <div onPointerDown={() => props.size.start()}>
                          <ResizeHandle
                            direction="horizontal"
                            edge="start"
                            size={layout.fileTree.width()}
                            min={200}
                            max={480}
                            onResize={(width) => {
                              props.size.touch()
                              layout.fileTree.resize(width)
                            }}
                          />
                        </div>
                      </Show>
                    </div>
                  </Show>

                  {/* Right: Content Area (Editor / Preview / Data) — always interactive */}
                  <div
                    class="relative min-w-0 h-full flex-1 overflow-hidden bg-background-base"
                  >
                    <div class="size-full min-w-0 h-full bg-background-base">
                      <Show when={reviewTab()}>
                        <Tabs.Content value="review" class="flex flex-col h-full overflow-hidden contain-strict bg-background-stronger">
                          <Show when={activeTab() === "review"}>
                            <div class="relative pt-2 flex-1 min-h-0 overflow-hidden">
                              <div class="h-full px-6 pb-42 -mt-4 flex flex-col items-center justify-center text-center gap-6">
                                <div class="forge-f-mark size-14 opacity-25" aria-label="Forge" role="img" />
                                <div class="text-14-regular text-text-weak max-w-56">
                                  {language.t("session.files.selectToOpen")}
                                </div>
                              </div>
                            </div>
                          </Show>
                        </Tabs.Content>
                      </Show>

                      <Tabs.Content value="empty" class="flex flex-col h-full overflow-hidden contain-strict">
                        <Show when={activeTab() === "empty"}>
                          <div class="relative pt-2 flex-1 min-h-0 overflow-hidden">
                            <div class="h-full px-6 pb-42 -mt-4 flex flex-col items-center justify-center text-center gap-6">
                              <div class="forge-f-mark size-14 opacity-25" aria-label="Forge" role="img" />
                              <div class="text-14-regular text-text-weak max-w-56">
                                {language.t("session.files.selectToOpen")}
                              </div>
                            </div>
                          </div>
                        </Show>
                      </Tabs.Content>

                      <Show when={contextOpen()}>
                        <Tabs.Content value="context" class="flex flex-col h-full overflow-hidden contain-strict">
                          <Show when={activeTab() === "context"}>
                            <div class="relative pt-2 flex-1 min-h-0 overflow-hidden">
                              <SessionContextTab />
                            </div>
                          </Show>
                        </Tabs.Content>
                      </Show>

                      {/* Preview — rendered directly, NOT via Kobalte Tabs.Content.
                          Kobalte only shows Tabs.Content when a matching Tabs.Trigger
                          is registered. Since we use custom buttons for Preview/Data,
                          we bypass Kobalte and drive visibility with a plain Show. */}
                      <Show when={activeTab() === "preview"}>
                        <div class="flex flex-col h-full overflow-hidden bg-background-base" style="position:absolute;inset:0">
                          <div class="ws-stage">
                            <Switch>
                              <Match when={isRunning() && iframeSrc()}>
                                <div class={`ws-frame ${device()}`}>
                                  <iframe
                                    ref={iframeRef}
                                    src={iframeSrc()!}
                                    class="w-full h-full border-0 bg-white"
                                    title="App Preview"
                                    allow="clipboard-read; clipboard-write"
                                  />
                                </div>
                              </Match>
                              <Match when={isStarting()}>
                                <div class="building-stage" style="justify-content:flex-start;padding:16px;overflow:hidden">
                                  <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-shrink:0">
                                    <div class="build-orb" style="margin:0" />
                                    <span class="build-label" style="margin:0">{statusLabel()}</span>
                                    <Show when={showLogs() && containerLogs()}>
                                      <button
                                        style="font-size:11px;opacity:0.5;background:none;border:none;cursor:pointer;color:inherit;text-decoration:underline"
                                        onClick={() => void handleCopyLogs()}
                                        title="Copy logs to clipboard"
                                      >Copy</button>
                                    </Show>
                                    <button
                                      style="margin-left:auto;font-size:11px;opacity:0.5;background:none;border:none;cursor:pointer;color:inherit;text-decoration:underline"
                                      onClick={() => { setShowLogs(v => !v); void fetchLogs() }}
                                    >{showLogs() ? "Hide logs" : "Show logs"}</button>
                                  </div>
                                  <Show when={!showLogs()}>
                                    <div class="build-sub">This usually takes 15–30 seconds</div>
                                  </Show>
                                  <Show when={showLogs()}>
                                    <pre style="flex:1;overflow:auto;background:#0a0a0a;border:1px solid #222;border-radius:6px;padding:10px;font-size:11px;line-height:1.5;color:#aaa;white-space:pre-wrap;word-break:break-all;text-align:left;width:100%;min-height:0;user-select:text;-webkit-user-select:text">{containerLogs() || "Waiting for output…"}</pre>
                                  </Show>
                                </div>
                              </Match>
                              <Match when={containerStatus() === "crashed"}>
                                <div class="building-stage" style="justify-content:flex-start;padding:16px;overflow:hidden">
                                  <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-shrink:0">
                                    <span class="build-label" style="margin:0;color:#f87171">Container crashed</span>
                                    <Show when={showLogs() && containerLogs()}>
                                      <button
                                        style="font-size:11px;opacity:0.5;background:none;border:none;cursor:pointer;color:inherit;text-decoration:underline"
                                        onClick={() => void handleCopyLogs()}
                                        title="Copy logs to clipboard"
                                      >Copy</button>
                                    </Show>
                                    <button
                                      style="margin-left:auto;font-size:11px;opacity:0.5;background:none;border:none;cursor:pointer;color:inherit;text-decoration:underline"
                                      onClick={() => { setShowLogs(v => !v); void fetchLogs() }}
                                    >{showLogs() ? "Hide logs" : "Show logs"}</button>
                                  </div>
                                  <Show when={showLogs()}>
                                    <pre style="flex:1;overflow:auto;background:#0a0a0a;border:1px solid #3f1f1f;border-radius:6px;padding:10px;font-size:11px;line-height:1.5;color:#aaa;white-space:pre-wrap;word-break:break-all;text-align:left;width:100%;min-height:0;margin-bottom:10px;user-select:text;-webkit-user-select:text">{containerLogs() || "(no output)"}</pre>
                                  </Show>
                                  <Show when={!showLogs()}>
                                    <div class="build-sub mb-4">Something went wrong starting the dev server</div>
                                  </Show>
                                  <button class="twk-btn" style="flex-shrink:0" onClick={() => void handleRunApp()}>Retry</button>
                                </div>
                              </Match>
                              <Match when={true}>
                                <div class="building-stage">
                                  <div class="build-label">Ready when you are</div>
                                  <div class="build-sub mb-4">Press Run to spin up a live preview</div>
                                  <button class="twk-btn" onClick={() => void handleRunApp()}>Run App</button>
                                </div>
                              </Match>
                            </Switch>
                          </div>

                          {/* REVERT OVERLAY — covers the preview area only while
                              we're waiting for the container to come back up
                              after a version rollback. Lives inside the same
                              absolute-positioned panel so it sits ON TOP of
                              whatever Match branch is showing (iframe, building
                              state, crashed state). Uses Forge mark + the same
                              `build-orb` animation pattern as the regular
                              starting state for visual consistency. Click +
                              pointer events are blocked so the user can't fire
                              a second revert mid-transition.
                              Cascade check (CLAUDE.md §3.2): parent is
                              `position:absolute inset:0`. We add another
                              absolute layer on top with z-index 5; no inherited
                              flex/grid rule reaches it. The bg is a Forge cream
                              with 92% opacity so the preview "ghosts" through. */}
                          <Show when={isReverting()}>
                            <div
                              style={{
                                position:        "absolute",
                                inset:           "0",
                                "z-index":       5,
                                background:      "color-mix(in srgb, var(--bg) 92%, transparent)",
                                "backdrop-filter": "blur(2px)",
                                display:         "flex",
                                "flex-direction": "column",
                                "align-items":   "center",
                                "justify-content": "center",
                                gap:             "16px",
                                "pointer-events": "auto",
                                "user-select":   "none",
                                animation:       "fadeIn 180ms ease-out",
                              }}
                            >
                              {/* Orb wrapper — pulsing soft halo around the
                                  Forge mark. Reuses the existing `build-orb`
                                  animation keyframes (orbpulse, orbring)
                                  defined in index.css so we don't ship new
                                  CSS for one consumer. */}
                              <div class="build-orb" style="margin:0;display:flex;align-items:center;justify-content:center;">
                                <ForgeMark
                                  class="text-text-strong"
                                  style={{ width: "28px", height: "28px", opacity: 0.9 }}
                                />
                              </div>
                              <div class="build-label" style="margin:0">Rolling back…</div>
                              <div class="build-sub" style="margin:0;max-width:280px;text-align:center">
                                Restoring your workspace from the version you picked
                              </div>
                            </div>
                          </Show>
                        </div>
                      </Show>

                      {/* Data — live SQLite explorer. DataPanel renders its own
                          empty state ("No store, no sweat") when the project has
                          no tables yet, so we just mount it unconditionally. */}
                      <Show when={activeTab() === "data"}>
                        <div class="flex flex-col h-full overflow-hidden bg-background-base" style="position:absolute;inset:0">
                          <DataPanel />
                        </div>
                      </Show>

                      <Show when={activeFileTab()} keyed>
                        {(tab) => <FileTabContent tab={tab} />}
                      </Show>
                    </div>
                  </div>

                </div>

              </Tabs>
              <DragOverlay>
                <Show when={store.activeDraggable} keyed>
                  {(tab) => {
                    const path = file.pathFromTab(tab)
                    return (
                      <div data-component="tabs-drag-preview">
                        <Show when={path}>{(p) => <FileVisual active path={p()} />}</Show>
                      </div>
                    )
                  }}
                </Show>
              </DragOverlay>
            </DragDropProvider>
          </div>
        </Show>
      </aside>

      {/* ── Showcase save modal ─────────────────────────────────────────── */}
      <Show when={showShowcaseModal()}>
        <div
          class="fixed inset-0 z-[9999] flex items-center justify-center"
          style={{ background: "rgba(0,0,0,0.55)", "backdrop-filter": "blur(4px)" }}
          onClick={(e) => { if (e.target === e.currentTarget) setShowShowcaseModal(false) }}
        >
          <div
            class="relative flex flex-col gap-4 rounded-xl border border-border-weaker-base bg-background-stronger shadow-2xl"
            style={{ width: "360px", padding: "24px" }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* Header */}
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <svg class="size-4 text-yellow-400" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z"/>
                </svg>
                <span class="text-14-medium text-text-base">Save to Showcase</span>
              </div>
              <button
                class="icon-btn"
                title="Close"
                onClick={() => setShowShowcaseModal(false)}
              >
                <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M18 6 6 18M6 6l12 12"/>
                </svg>
              </button>
            </div>

            {/* Name input */}
            <div class="flex flex-col gap-1.5">
              <label class="text-12-regular text-text-weak">Template name</label>
              <input
                type="text"
                placeholder="My awesome app"
                value={showcaseName()}
                onInput={(e) => setShowcaseName(e.currentTarget.value)}
                class="w-full rounded-lg border border-border-weaker-base bg-background-base px-3 py-2 text-13-regular text-text-base placeholder:text-text-weaker outline-none focus:border-border-base transition-colors"
              />
            </div>

            {/* Description textarea */}
            <div class="flex flex-col gap-1.5">
              <label class="text-12-regular text-text-weak">Description <span class="text-text-weaker">(optional)</span></label>
              <textarea
                placeholder="What does this app do?"
                rows={3}
                value={showcaseDesc()}
                onInput={(e) => setShowcaseDesc(e.currentTarget.value)}
                class="w-full rounded-lg border border-border-weaker-base bg-background-base px-3 py-2 text-13-regular text-text-base placeholder:text-text-weaker outline-none focus:border-border-base transition-colors resize-none"
              />
            </div>

            {/* Thumbnail upload */}
            <div class="flex flex-col gap-1.5">
              <label class="text-12-regular text-text-weak">
                Thumbnail <span class="text-text-weaker">(optional)</span>
              </label>
              {/* Hidden file input */}
              <input
                ref={thumbnailInputRef}
                type="file"
                accept="image/png,image/jpeg,image/webp,image/gif"
                style={{ display: "none" }}
                onChange={(e) => {
                  const file = e.currentTarget.files?.[0]
                  if (!file) return
                  const reader = new FileReader()
                  reader.onload = (ev) => {
                    const result = ev.target?.result
                    if (typeof result === "string") setThumbnailDataUrl(result)
                  }
                  reader.readAsDataURL(file)
                }}
              />
              <Show
                when={thumbnailDataUrl()}
                fallback={
                  <button
                    type="button"
                    class="w-full rounded-lg border-2 border-dashed border-border-weaker-base bg-background-base hover:border-border-base hover:bg-background-weaker transition-colors flex flex-col items-center justify-center gap-1.5 py-5 cursor-pointer"
                    onClick={() => thumbnailInputRef?.click()}
                  >
                    <svg class="size-5 text-text-weaker" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                      <rect x="3" y="3" width="18" height="18" rx="2"/>
                      <circle cx="8.5" cy="8.5" r="1.5"/>
                      <path d="M21 15l-5-5L5 21"/>
                    </svg>
                    <span class="text-12-regular text-text-weaker">Click to upload a thumbnail</span>
                    <span class="text-11-regular text-text-weaker opacity-60">PNG, JPG, WebP — shown in gallery cards</span>
                  </button>
                }
              >
                {(src) => (
                  <div class="relative rounded-lg overflow-hidden border border-border-weaker-base" style={{ height: "120px" }}>
                    <img src={src()} alt="Thumbnail preview" class="w-full h-full object-cover object-top" />
                    <button
                      type="button"
                      class="absolute top-1.5 right-1.5 flex items-center justify-center w-6 h-6 rounded-full bg-black/60 text-white hover:bg-black/80 transition-colors"
                      title="Remove thumbnail"
                      onClick={() => { setThumbnailDataUrl(null); if (thumbnailInputRef) thumbnailInputRef.value = "" }}
                    >
                      <svg class="size-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round">
                        <path d="M18 6L6 18M6 6l12 12"/>
                      </svg>
                    </button>
                    <button
                      type="button"
                      class="absolute bottom-1.5 right-1.5 flex items-center gap-1 text-[10px] font-medium text-white px-2 py-1 rounded-md bg-black/50 hover:bg-black/70 transition-colors"
                      onClick={() => thumbnailInputRef?.click()}
                    >
                      Change
                    </button>
                  </div>
                )}
              </Show>
            </div>

            {/* Actions */}
            <div class="flex items-center gap-2 justify-end pt-1">
              <button
                class="showcase-badge-btn"
                onClick={() => setShowShowcaseModal(false)}
                disabled={showcaseSaving()}
              >
                Cancel
              </button>
              <button
                class="twk-btn flex items-center gap-1.5"
                onClick={() => void handleSaveShowcase()}
                disabled={showcaseSaving() || !showcaseName().trim()}
              >
                <Show when={showcaseSaving()}
                  fallback={
                    <>
                      <svg class="size-3" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z"/>
                      </svg>
                      Save &amp; Capture
                    </>
                  }
                >
                  <span class="forge-css-spinner" style={{ width: "12px", height: "12px", "border-width": "2px" }} />
                  Saving…
                </Show>
              </button>
            </div>
          </div>
        </div>
      </Show>
      </>
    </Show>
  )
}
