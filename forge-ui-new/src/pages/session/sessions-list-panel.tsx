import { createEffect, createMemo, For, onCleanup, onMount, Show } from "solid-js"
import { createStore } from "solid-js/store"
import { useNavigate, useParams, useSearchParams } from "@solidjs/router"
import { A } from "@solidjs/router"
import { base64Encode } from "@opencode-ai/core/util/encode"
import { useGlobalSync } from "@/context/global-sync"
import { sortedRootSessions } from "@/pages/layout/helpers"
import { sessionTitle } from "@/utils/session-title"
import { decode64 } from "@/utils/base64"
import { Persist, persisted } from "@/utils/persist"

/**
 * Left side panel that lists this project's sessions.
 *
 * Styled to match the Loom "Atelier" design system used by the rest of Forge:
 *   - Background = --bg (same warm cream as the page)
 *   - Hairline right border (--hair) for separation
 *   - Header label = Geist Mono uppercase like `.kicker` / `.suggest-label .meta`
 *   - Row text = Geist (--font-ui)
 *   - Active row = --surface card on cream, ink text
 *   - Hover = --surface-2 wash, ink-2 text
 */
export function SessionsListPanel() {
  const params = useParams<{ dir?: string; id?: string }>()
  const navigate = useNavigate()
  const globalSync = useGlobalSync()
  const [searchParams] = useSearchParams()

  const [prefs, setPrefs] = persisted(
    Persist.global("sessions-list-panel"),
    createStore({ collapsed: false }),
  )

  const directory = decode64(params.dir)
  if (!directory) return null
  const [store] = globalSync.child(directory, { bootstrap: false })

  const sessions = createMemo(() => sortedRootSessions(store, Date.now()))
  const encoded = base64Encode(directory)
  const newSessionHref = `/${encoded}/session?new=1`

  // When arriving from the home page (from=home), auto-collapse the sidebar so
  // the chat starts full-width. The user can expand it manually with the ‹ toggle.
  onMount(() => {
    if (searchParams.from === "home") {
      setPrefs("collapsed", true)
    }
  })

  // Publish the panel's current width as a CSS var on <html> so other parts of
  // the layout (notably the .empty-inner hero container on the new-session
  // page) can compensate and stay visually centered against the viewport.
  // Expanded = 244px, collapsed = 36px, hidden (mobile) = 0px.
  createEffect(() => {
    const width = prefs.collapsed ? "36px" : "244px"
    document.documentElement.style.setProperty("--forge-sidebar-width", width)
  })
  onCleanup(() => {
    document.documentElement.style.removeProperty("--forge-sidebar-width")
  })

  return (
    <Show
      when={!prefs.collapsed}
      fallback={
        <div
          class="hidden md:flex shrink-0 h-full w-9 flex-col items-center pt-3 gap-1"
          style={{
            "background-color": "var(--bg)",
            "border-right": "1px solid var(--hair)",
          }}
        >
          {/* Home — always visible so the user can jump back to the project
              picker without having to expand the sidebar first. Mirrors the
              Home link rendered in the expanded header below. */}
          <A
            href="/home"
            class="loom-icon-btn"
            title="Back to home"
            aria-label="Back to home"
          >
            <svg viewBox="0 0 20 20" class="size-[15px]" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 9.5L10 3l7 6.5" />
              <path d="M5 8v8a1 1 0 001 1h3v-4h2v4h3a1 1 0 001-1V8" />
            </svg>
          </A>
          <button
            type="button"
            aria-label="Show sessions"
            onClick={() => setPrefs("collapsed", false)}
            class="loom-icon-btn"
            title="Show sessions"
          >
            <svg viewBox="0 0 20 20" class="size-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="square">
              <path d="M8 15L13 10L8 5" />
            </svg>
          </button>
        </div>
      }
    >
      <aside
        data-component="sessions-list-panel"
        class="hidden md:flex shrink-0 flex-col h-full w-[244px] overflow-hidden"
        aria-label="Sessions"
        style={{
          "background-color": "var(--bg)",
          "border-right": "1px solid var(--hair)",
          "font-family": "var(--font-ui)",
        }}
      >
        {/* Header */}
        <div
          class="flex items-center justify-between gap-2 px-3 shrink-0"
          style={{ height: "44px" }}
        >
          {/* Home button — always visible, takes user back to project list */}
          <A
            href="/home"
            class="loom-icon-btn"
            title="Back to home"
            aria-label="Back to home"
            style={{ "flex-shrink": "0" }}
          >
            <svg viewBox="0 0 20 20" class="size-[15px]" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 9.5L10 3l7 6.5" />
              <path d="M5 8v8a1 1 0 001 1h3v-4h2v4h3a1 1 0 001-1V8" />
            </svg>
          </A>

          <div class="flex items-center gap-1 ml-auto">
            <button
              type="button"
              aria-label="New session"
              onClick={() => navigate(newSessionHref, { replace: true })}
              class="loom-icon-btn"
              title="New session"
            >
              <svg viewBox="0 0 20 20" class="size-[14px]" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="square">
                <path d="M10 4v12M4 10h12" />
              </svg>
            </button>
            <button
              type="button"
              aria-label="Hide sessions panel"
              onClick={() => setPrefs("collapsed", true)}
              class="loom-icon-btn"
              title="Hide sessions"
            >
              <svg viewBox="0 0 20 20" class="size-[14px]" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="square">
                <path d="M12 5L7 10L12 15" />
              </svg>
            </button>
          </div>
        </div>

        {/* List */}
        <div class="flex-1 min-h-0 overflow-y-auto px-2 pb-3">
          <Show
            when={sessions().length > 0}
            fallback={
              <div
                class="px-2 pt-4"
                style={{
                  "font-size": "12.5px",
                  "line-height": "1.5",
                  color: "var(--muted)",
                  "font-family": "var(--font-ui)",
                }}
              >
                No threads yet.
                <br />
                Hit + to start one.
              </div>
            }
          >
            <For each={sessions()}>
              {(session) => {
                const title = createMemo(() => sessionTitle(session.title) || session.id)
                const active = createMemo(() => session.id === params.id)
                return (
                  <button
                    type="button"
                    classList={{ "loom-session-row": true, "is-active": active() }}
                    onClick={() => navigate(`/${encoded}/session/${session.id}`, { replace: true })}
                  >
                    <svg
                      viewBox="0 0 20 20"
                      class="size-[13px] shrink-0"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="1.5"
                      stroke-linecap="square"
                      style={{ opacity: active() ? "0.7" : "0.55" }}
                    >
                      <path d="M14.5 12H17.9V3H5.6V6.2M14.5 6.2H2V15.4H5V17.5L8.7 15.4H14.5V6.2Z" />
                    </svg>
                    <span class="truncate">{title()}</span>
                  </button>
                )
              }}
            </For>
          </Show>
        </div>
      </aside>
    </Show>
  )
}
