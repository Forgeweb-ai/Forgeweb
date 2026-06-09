import { createEffect, createMemo, createSignal, For, onCleanup, onMount, Show } from "solid-js"
import { createStore } from "solid-js/store"
import { useNavigate, useParams, useSearchParams } from "@solidjs/router"
import { A } from "@solidjs/router"
import { base64Encode } from "@opencode-ai/core/util/encode"
import { useTheme } from "@opencode-ai/ui/theme/context"
import { useDialog } from "@opencode-ai/ui/context/dialog"
import { useGlobalSync } from "@/context/global-sync"
import { currentUserInfo, fetchCurrentUser, type CurrentUser } from "@/context/forge-api"
import { UserMenu } from "@/components/user-menu"
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

  // Theme toggle (Forge brand rail at the bottom of the collapsed view).
  const theme = useTheme()
  const isDark = createMemo(() => theme.colorScheme() === "dark")
  const toggleTheme = () => theme.setColorScheme(isDark() ? "light" : "dark")

  // Current user — fetched lazily once, with JWT-decoded info as a sync
  // fallback for the first paint. Matches the home page's pattern so the
  // rail's UserMenu shows the same identity as the home profile chip.
  const jwtInfo = currentUserInfo()
  const [me, setMe] = createSignal<CurrentUser | null>(null)
  void fetchCurrentUser().then((m) => { if (m) setMe(m) })

  // Build the CurrentUser the UserMenu expects, using fetched `/me` when it
  // arrives and falling back to JWT fields for the first paint.
  const userForMenu = createMemo<CurrentUser | null>(() => {
    const fetched = me()
    if (fetched) return fetched
    if (!jwtInfo) return null
    return {
      id:                   jwtInfo.sub,
      email:                jwtInfo.email,
      username:             jwtInfo.username,
      created_at:           "",
      email_verified:       true,
      onboarding_completed: true,
      full_name:            null,
      role:                 null,
      company_size:         null,
      theme_pref:           null,
    }
  })

  // Settings dialog opener — same lazy-import + dialog.show pattern as home,
  // so the menu's "Settings" item lands on the same DialogSettings panel.
  const dialog = useDialog()
  function openSettings() {
    void import("@/components/dialog-settings").then((x) => {
      dialog.show(() => <x.DialogSettings />)
    })
  }

  // Publish the panel's current width as a CSS var on <html> so other parts of
  // the layout (notably the .empty-inner hero container on the new-session
  // page) can compensate and stay visually centered against the viewport.
  // Expanded = 244px, collapsed rail = 56px (w-14), hidden (mobile) = 0px.
  createEffect(() => {
    const width = prefs.collapsed ? "56px" : "244px"
    document.documentElement.style.setProperty("--forge-sidebar-width", width)
  })
  onCleanup(() => {
    document.documentElement.style.removeProperty("--forge-sidebar-width")
  })

  return (
    <Show
      when={!prefs.collapsed}
      fallback={
        // ── Collapsed icon rail ────────────────────────────────────────────
        // Vertical navigation rail shown when the sessions list is hidden.
        // Top:    Forge diamond mark · Home · Sessions (expands the panel)
        // Bottom: Theme toggle (light/dark) · Profile avatar (→ /home)
        //
        // The diamond is a static brand mark (decorative). The Sessions icon
        // is the inverse of the chevron — clicking it expands the rail back
        // into the full sessions list. Profile routes to /home where the
        // full UserMenu chip lives; opening the menu inline would require
        // wiring Popover into the rail, which is a bigger lift than this
        // task warranted.
        <div
          class="hidden md:flex shrink-0 h-full w-14 flex-col items-center py-3 gap-1.5"
          style={{
            "background-color": "var(--bg)",
            "border-right": "1px solid var(--hair)",
          }}
          data-component="forge-rail"
        >
          {/* Forge brand diamond — bigger, hits the same visual weight as the
              avatar at the bottom of the rail so the rail reads as balanced. */}
          <div
            data-slot="forge-rail-mark"
            aria-label="Forge"
            role="img"
            style={{
              width: "34px",
              height: "34px",
              "border-radius": "9px",
              background: "var(--v2-orange-900)",
              display: "flex",
              "align-items": "center",
              "justify-content": "center",
              "margin-bottom": "10px",
              "flex-shrink": "0",
            }}
          >
            <div
              style={{
                width: "12px",
                height: "12px",
                background: "var(--bg)",
                transform: "rotate(45deg)",
              }}
            />
          </div>

          {/* Home — back to project picker */}
          <A
            href="/home"
            class="loom-icon-btn"
            title="Home"
            aria-label="Home"
            style={{ width: "38px", height: "38px" }}
          >
            <svg viewBox="0 0 20 20" class="size-[20px]" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 9.5L10 3l7 6.5" />
              <path d="M5 8v8a1 1 0 001 1h3v-4h2v4h3a1 1 0 001-1V8" />
            </svg>
          </A>

          {/* Sessions — expands the panel. Active-state pill so the rail
              reads as "you're in the sessions view." */}
          <button
            type="button"
            aria-label="Show sessions"
            onClick={() => setPrefs("collapsed", false)}
            class="loom-icon-btn"
            title="Show sessions"
            style={{
              width: "38px",
              height: "38px",
              background: "var(--accent-soft)",
              color: "var(--accent)",
            }}
          >
            <svg viewBox="0 0 20 20" class="size-[19px]" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M4 5a2 2 0 012-2h8a2 2 0 012 2v6a2 2 0 01-2 2H9l-3 3v-3H6a2 2 0 01-2-2V5z" />
            </svg>
          </button>

          {/* Spacer pushing theme + profile to the bottom */}
          <div style={{ "flex": "1 1 auto" }} />

          {/* Theme toggle: moon when light → switch to dark, sun when dark → switch to light */}
          <button
            type="button"
            aria-label="Toggle theme"
            onClick={toggleTheme}
            class="loom-icon-btn"
            title={isDark() ? "Switch to light" : "Switch to dark"}
            style={{ width: "38px", height: "38px" }}
          >
            <Show
              when={isDark()}
              fallback={
                <svg viewBox="0 0 20 20" class="size-[20px]" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M16 11.5A6 6 0 018.5 4a6 6 0 107.5 7.5z" />
                </svg>
              }
            >
              <svg viewBox="0 0 20 20" class="size-[20px]" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="10" cy="10" r="3.5" />
                <path d="M10 2v2M10 16v2M2 10h2M16 10h2M4.2 4.2l1.4 1.4M14.4 14.4l1.4 1.4M4.2 15.8l1.4-1.4M14.4 5.6l1.4-1.4" />
              </svg>
            </Show>
          </button>

          {/* Profile — reuses the same UserMenu component as the home page,
              wrapped in `forge-rail-user-menu` so CSS in index.css can:
                · narrow the anchor to just the avatar (hide name + caret)
                · flyout the popover to the RIGHT of the rail instead of
                  above (the popover's default position assumes a wide
                  chip in the home sidebar foot).
              Result: clicking the avatar opens the identical menu (Profile,
              Settings, Appearance, Support, Documentation, Community, Home,
              Sign out) you get on home. */}
          <div class="forge-rail-user-menu" style={{ position: "relative" }}>
            <UserMenu user={userForMenu()} onOpenSettings={openSettings} />
          </div>
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
