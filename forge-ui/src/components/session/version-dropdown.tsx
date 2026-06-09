/**
 * VersionDropdown
 * ----------------
 * Toolbar control showing the session's version history + rollback action.
 *
 * Backing model (v1)
 * ------------------
 * OpenCode already snapshots the workspace as a git repo after every AI
 * turn (see opencode/packages/opencode/src/snapshot/index.ts; the running
 * git dir lives under `~/.local/share/opencode/snapshot/global/<hash>`).
 * Every assistant message carries the snapshot hash on its `step-finish`
 * part, and the SDK exposes:
 *
 *   sdk.client.session.revert({ sessionID, messageID })   ← roll back to
 *                                                            BEFORE messageID
 *   sdk.client.session.unrevert({ sessionID })             ← roll forward
 *
 * One user message ≈ one AI turn ≈ one "version" the user sees. The list
 * is derived directly from the in-memory session sync store — no extra
 * forge-server round-trip needed for v1.
 *
 * The earlier draft of this component called forge.listProjectVersions /
 * restoreProjectVersion (a parallel content-addressed store we built in
 * forge-server). That stays dormant for the multi-tenant scale story;
 * v1 ships on opencode's existing snapshots. See [[forge_versioning_v1]].
 *
 * UX choices, on purpose
 * ----------------------
 *   - Trigger label is bounded to ~220px and truncates. The full prompt
 *     text shows in the dropdown panel.
 *   - "Current" = the user message whose AI turn produced the workspace
 *     we're looking at right now. Determined by session.revert state:
 *       no revert     → current = latest user message
 *       revert set    → current = user message immediately BEFORE the
 *                        message in revert.messageID
 *   - Clicking version V means "make the workspace look like it did
 *     right after the AI turn for V". Implementation:
 *       V is the latest message      → unrevert()
 *       V has a later message Vnext  → revert({ messageID: Vnext.id })
 *     This mirrors the existing restoreMutation in pages/session.tsx.
 */
import { For, Match, Show, Switch, createMemo, createSignal, onCleanup, onMount } from "solid-js"
import { showToast } from "@opencode-ai/ui/toast"
import { Dialog } from "@opencode-ai/ui/dialog"
import { Button } from "@opencode-ai/ui/button"
import { useDialog } from "@opencode-ai/ui/context/dialog"
import { useSDK } from "@/context/sdk"
import { useSync } from "@/context/sync"

type Props = {
  /** Active SDK session ID. Null on the empty-state / home view. */
  sessionID: string | null
  /** Called after a successful restore so the host can refresh the iframe. */
  onRestored?: () => void
}

/** "5m ago" / "2h ago" / "3d ago" — used in the dropdown rows. Cheap, no deps. */
function relativeTime(ms: number): string {
  const dt = Date.now() - ms
  if (dt < 60_000)       return "just now"
  if (dt < 3_600_000)    return `${Math.floor(dt / 60_000)}m ago`
  if (dt < 86_400_000)   return `${Math.floor(dt / 3_600_000)}h ago`
  if (dt < 604_800_000)  return `${Math.floor(dt / 86_400_000)}d ago`
  return new Date(ms).toLocaleDateString()
}

/**
 * Pull the visible prompt text from a user message's `text` parts.
 * `sync.data.part[messageID]` is the canonical parts source; the message
 * row itself doesn't carry the text. Fall back to a generic label if
 * the user uploaded only files / images (no text part).
 */
function userPromptText(parts: ReadonlyArray<any>): string {
  return parts
    .filter((p) => p?.type === "text" && typeof p.text === "string")
    .map((p) => (p.text as string).trim())
    .filter(Boolean)
    .join("\n")
    .trim()
}

/** Shorten for the inline dropdown trigger (220px max-width). Single line. */
function shorten(text: string, max = 60): string {
  if (!text) return "Prompt"
  const single = text.replace(/\s+/g, " ")
  return single.length > max ? `${single.slice(0, max - 1)}…` : single
}

type VersionRow = {
  messageID:    string
  /** Truncated single-line label for the dropdown row + trigger. */
  label:        string
  /** Full untruncated prompt text for the confirm dialog. */
  fullPrompt:   string
  createdMs:    number
  isCurrent:    boolean
  /** Message that comes after this one (used to compute the revert target). */
  nextID:       string | undefined
}

/**
 * Forge-branded rollback confirmation dialog. Rendered via the shared
 * useDialog() stack so it inherits the app's overlay, focus trap,
 * escape-to-close and animation behavior — same as DialogFork,
 * DialogConnectProvider, etc. We pass it the label and a confirm
 * callback; the dialog handles its own UI + dismiss flow.
 *
 * Why a separate component instead of inline JSX in restore():
 *   - Keeps the dropdown's render tree small (the dialog only mounts
 *     when actually shown via useDialog().show()).
 *   - Lets the dialog manage its own "running" state for the Restore
 *     button's spinner without coupling to the dropdown's createSignal.
 */
function ConfirmRollbackDialog(props: {
  fullPrompt: string
  onConfirm:  () => Promise<void>
}): ReturnType<typeof Dialog> {
  const dialog = useDialog()
  const [running, setRunning] = createSignal(false)

  const confirm = async () => {
    if (running()) return
    setRunning(true)
    try {
      await props.onConfirm()
      dialog.close()
    } finally {
      setRunning(false)
    }
  }

  // Why these specific overrides:
  //   - `fit` removes the 280px min-height the design-system dialog applies
  //     for list-style dialogs (DialogFork etc.); a confirm has way less to
  //     show and should hug its content.
  //   - `width: 100%` on the inner wrapper makes content fill the body,
  //     because dialog-content sets `align-items: flex-start` — without 100%,
  //     a narrower div sits left-aligned inside a wider body and the modal
  //     looks lopsided ("everything aligned to left").
  //   - We supply our own horizontal padding (20px) because `dialog-body`
  //     itself has none — the design system expects each consumer to choose
  //     a padding that matches its content density.
  return (
    <Dialog title="Roll back this version?" fit>
      <div
        class="flex flex-col"
        style={{
          width:   "100%",
          padding: "4px 20px 20px",
        }}
      >
        <p
          class="text-sm leading-relaxed"
          style={{ color: "var(--text-base)", "margin-bottom": "14px" }}
        >
          Forge will restore your workspace to the state from right after this turn:
        </p>

        {/* Quote block — full prompt, wraps, capped height with scroll so a
            paragraph-long prompt doesn't push the buttons off-screen. The
            left-border accent keeps it visually distinct from the body text
            without an outlined chip that drifts from the surrounding cards. */}
        <div
          class="text-sm leading-relaxed"
          style={{
            background:      "var(--surface-soft, rgba(0,0,0,0.04))",
            "border-left":   "3px solid var(--accent-base, #4f8cff)",
            "border-radius": "0 6px 6px 0",
            padding:         "10px 14px",
            "max-height":    "180px",
            "overflow-y":    "auto",
            "white-space":   "pre-wrap",
            "word-break":    "break-word",
            color:           "var(--text-strong)",
          }}
        >
          {props.fullPrompt || "Prompt"}
        </div>

        <p
          class="text-xs leading-relaxed"
          style={{
            "margin-top": "14px",
            color:        "var(--text-muted, rgba(0,0,0,0.6))",
          }}
        >
          Newer turns stay in your history — you can roll forward to them
          anytime. Forge uses a git-backed snapshot under the hood, so
          nothing is lost.
        </p>

        <div
          class="flex items-center justify-end gap-2"
          style={{ "margin-top": "20px" }}
        >
          <Button
            variant="secondary"
            disabled={running()}
            onClick={() => dialog.close()}
          >
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={running()}
            onClick={() => void confirm()}
          >
            <Show when={running()} fallback={<>Roll back</>}>
              <span class="flex items-center gap-2">
                <span
                  class="forge-css-spinner"
                  style={{ width: "10px", height: "10px", "border-width": "2px" }}
                />
                Rolling back…
              </span>
            </Show>
          </Button>
        </div>
      </div>
    </Dialog>
  )
}

export function VersionDropdown(props: Props): ReturnType<typeof Show> {
  const sdk    = useSDK()
  const sync   = useSync()
  const dialog = useDialog()

  const [open, setOpen]           = createSignal(false)
  const [restoring, setRestoring] = createSignal<string | null>(null)
  let rootRef: HTMLDivElement | undefined

  // Click-outside to close. Installed once.
  onMount(() => {
    const handler = (e: MouseEvent) => {
      if (!open()) return
      if (rootRef && !rootRef.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener("mousedown", handler)
    onCleanup(() => document.removeEventListener("mousedown", handler))
  })

  /**
   * Derive the version list from session sync state.
   *
   * Reactive — re-runs whenever messages or the session's revert pointer
   * change. That's the right shape: a new AI turn lands → store updates
   * → dropdown re-renders.
   */
  const versions = createMemo<VersionRow[]>(() => {
    const sid = props.sessionID
    if (!sid) return []
    const messages = sync.data.message[sid] ?? []
    const userMsgs = messages.filter((m) => m.role === "user")
    if (userMsgs.length === 0) return []

    // Session revert state. The SDK Session type carries `revert?: { messageID, ... }`.
    // sync.data.session is the canonical source; fall back to scanning if
    // the shape isn't where we expect (defensive against SDK churn).
    const sessionInfo = (sync.data as any).session?.find?.((s: any) => s?.id === sid)
    const revertMsgID: string | undefined = sessionInfo?.revert?.messageID

    // Determine the "current" message: latest non-reverted user message.
    // Linear chain assumption — opencode's revert is monotonic.
    const currentIdx = revertMsgID
      ? userMsgs.findIndex((m) => m.id >= revertMsgID) - 1
      : userMsgs.length - 1
    const currentID = currentIdx >= 0 ? userMsgs[currentIdx].id : undefined

    // Build rows newest-first. Each carries the NEXT user message id so
    // restore knows which message to revert to.
    return userMsgs
      .map((m, i): VersionRow => {
        const parts      = sync.data.part[m.id] ?? []
        const fullPrompt = userPromptText(parts)
        return {
          messageID:  m.id,
          label:      shorten(fullPrompt),
          fullPrompt,
          createdMs:  m.time?.created ?? 0,
          isCurrent:  m.id === currentID,
          nextID:     userMsgs[i + 1]?.id,
        }
      })
      .reverse()
  })

  const current = createMemo(() => versions().find((v) => v.isCurrent) ?? versions()[0])

  /**
   * Actually performs the SDK revert/unrevert. Called by the confirm
   * dialog's onConfirm — separated from the click handler so the dialog
   * owns the running-state UX while this function owns the side effects.
   */
  async function performRestore(row: VersionRow): Promise<void> {
    const sid = props.sessionID
    if (!sid) return

    setRestoring(row.messageID)
    try {
      if (row.nextID) {
        // Revert to BEFORE the next user message — workspace ends up in
        // the state THIS row's AI turn produced.
        await sdk.client.session.revert({ sessionID: sid, messageID: row.nextID })
      } else {
        // row is the latest; "roll back to here" means undo any prior revert.
        await sdk.client.session.unrevert({ sessionID: sid })
      }
      showToast({ title: "Restored", description: row.label, variant: "success" })
      props.onRestored?.()
      setOpen(false)
    } catch (e) {
      console.error("revert failed", e)
      showToast({ title: "Restore failed", description: String(e), variant: "error" })
      throw e  // let the dialog stay open so the user can retry or cancel
    } finally {
      setRestoring(null)
    }
  }

  /**
   * Click handler on a version row. Gates on current/in-flight then
   * opens the Forge-branded confirm modal via the shared dialog stack.
   * The native window.confirm was loud (white-on-dark, "localhost:3000
   * says…") and broke the Forge look — this matches the rest of the app.
   */
  function restore(row: VersionRow): void {
    if (!props.sessionID || restoring()) return
    if (row.isCurrent) { setOpen(false); return }

    // Close the dropdown so the dialog has the user's full attention.
    setOpen(false)

    void dialog.show(() => (
      <ConfirmRollbackDialog
        fullPrompt={row.fullPrompt}
        onConfirm={() => performRestore(row)}
      />
    ))
  }

  // Outer Show keeps the toolbar slot collapsed when there's no session
  // (home view, between-sessions transitions) — avoids a "Loading…"
  // flicker before the session attaches.
  return (
    <Show when={props.sessionID}>
      <div
        ref={(el) => (rootRef = el)}
        class="relative shrink-0"
        style={{ "max-width": "220px" }}
      >
        <button
          type="button"
          class="flex items-center gap-1.5 h-7 px-2 rounded border border-border-weaker-base bg-background-base hover:bg-gray-100 text-xs text-black transition-colors"
          style={{ "max-width": "220px" }}
          aria-haspopup="menu"
          aria-expanded={open()}
          title="Version history"
          onClick={(e) => {
            e.stopPropagation()
            setOpen((v) => !v)
          }}
        >
          {/* Branch / fork icon — signals "history / versions". */}
          <svg class="size-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="6"  cy="6"  r="2"/>
            <circle cx="6"  cy="18" r="2"/>
            <circle cx="18" cy="12" r="2"/>
            <path d="M6 8v8"/>
            <path d="M6 12c0 4 4 4 4 4h4"/>
            <path d="M6 12c0-4 4-4 4-4h4"/>
          </svg>
          <span class="truncate min-w-0">
            <Switch>
              <Match when={current()}>{(c) => <>{c().label}</>}</Match>
              <Match when={true}><span class="opacity-60">No versions yet</span></Match>
            </Switch>
          </span>
          <svg class="size-3 shrink-0 opacity-60" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="6 9 12 15 18 9"/>
          </svg>
        </button>

        <Show when={open()}>
          <div
            role="menu"
            class="absolute left-0 top-full mt-1 rounded-md border border-border-weaker-base bg-background-base shadow-lg overflow-hidden"
            style={{ "z-index": 50, "min-width": "280px", "max-width": "360px", "max-height": "360px", "overflow-y": "auto" }}
          >
            <Switch>
              <Match when={versions().length === 0}>
                <div class="px-3 py-2 text-xs opacity-60">
                  No versions yet. A version is captured after each AI change.
                </div>
              </Match>
              <Match when={true}>
                <For each={versions()}>{(row) => (
                  <button
                    type="button"
                    class="flex items-start gap-2 w-full px-3 py-2 text-left text-xs border-b border-border-weaker-base last:border-b-0 hover:bg-gray-100 disabled:opacity-50 cursor-pointer disabled:cursor-default"
                    style={row.isCurrent ? { background: "rgba(79, 140, 255, 0.08)" } : undefined}
                    disabled={!!restoring() || row.isCurrent}
                    onClick={() => void restore(row)}
                    title={row.isCurrent ? "Current version" : "Click to roll back"}
                  >
                    <div class="flex flex-col min-w-0 flex-1">
                      <div class="flex items-center gap-2">
                        <span class="truncate font-medium text-black">{row.label}</span>
                        <Show when={row.isCurrent}>
                          <span class="text-[10px] uppercase tracking-wide opacity-60 shrink-0">Current</span>
                        </Show>
                      </div>
                      <Show when={row.createdMs > 0}>
                        <span class="text-[11px] opacity-60">{relativeTime(row.createdMs)}</span>
                      </Show>
                    </div>
                    <Show when={restoring() === row.messageID}>
                      <span class="forge-css-spinner shrink-0" style={{ width: "10px", height: "10px", "border-width": "2px" }} />
                    </Show>
                  </button>
                )}</For>
              </Match>
            </Switch>
          </div>
        </Show>
      </div>
    </Show>
  )
}
