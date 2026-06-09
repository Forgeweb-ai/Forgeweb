/**
 * user-menu.tsx
 * ==============
 * The profile chip + popover menu shown at the bottom of the home sidebar.
 *
 * Renders:
 *   ┌────────────────────────────┐
 *   │  [U]  User                │   ← clickable chip (closed state)
 *   │       you@example.com      │
 *   └────────────────────────────┘
 *
 * Click opens the popover which mirrors the reference design:
 *   Profile · Settings · Appearance ▸ · Support ▸ · Documentation ▸ ·
 *   Community · Home · ─── · Sign out
 *
 * Appearance opens a nested light/dark/system picker that drives
 * useTheme().setColorScheme — same hook the Settings → General tab uses.
 *
 * The menu is self-positioning relative to its anchor and closes on:
 *   - outside click
 *   - Escape
 *   - any menu item click that navigates / triggers an action
 *
 * Avatar initials follow the rule:
 *   "Jane Doe"  → "JD"
 *   "Jane"      → "J"
 *   no full_name → first letter of username, else "U"
 *
 * (see userInitials() in forge-api.tsx)
 */

import { type Component, createSignal, onCleanup, onMount, Show, JSX } from "solid-js"
import { useNavigate } from "@solidjs/router"
import { useTheme, type ColorScheme } from "@opencode-ai/ui/theme/context"
import { type CurrentUser, logout, userInitials } from "@/context/forge-api"

type Props = {
  user:           CurrentUser | null
  onOpenSettings: () => void
  /** Optional handlers; if omitted, sensible defaults are used. */
  onOpenProfile?:       () => void
  onOpenSupport?:       () => void
  onOpenDocumentation?: () => void
  onOpenCommunity?:     () => void
}

const STYLES = `
  /* ── Anchor button (the chip in the sidebar foot) ─────────────────── */
  .forge-user-anchor {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 10px;
    border-radius: 10px;
    background: transparent;
    border: 0;
    cursor: pointer;
    text-align: left;
    font-family: inherit;
    color: inherit;
    transition: background .12s ease;
  }
  .forge-user-anchor:hover  { background: rgba(21,20,15,0.05); }
  .forge-user-anchor.open   { background: rgba(21,20,15,0.06); }

  .forge-user-avatar {
    width: 32px; height: 32px;
    border-radius: 999px;
    background: var(--forge-avatar-bg, #2f7bff);
    color: #fff;
    font-weight: 700;
    font-size: 13px;
    letter-spacing: 0.02em;
    display: grid;
    place-items: center;
    flex: none;
    user-select: none;
  }

  .forge-user-who {
    flex: 1;
    min-width: 0;
    font-size: 13px;
    line-height: 1.2;
    color: var(--ink, #15140f);
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .forge-user-who .sub {
    display: block;
    font-weight: 400;
    color: var(--muted, #8a8175);
    font-size: 11.5px;
  }

  .forge-user-caret {
    flex: none;
    color: var(--muted-2, #6b6358);
    transition: transform .15s ease;
  }
  .forge-user-anchor.open .forge-user-caret { transform: rotate(180deg); }

  /* ── Popover ─────────────────────────────────────────────────────── */
  .forge-user-pop {
    position: absolute;
    /* Anchored above-right of its parent (.fh-side-foot has position:relative). */
    left: 6px;
    right: 6px;
    bottom: calc(100% + 8px);
    background: #fff;
    border: 1px solid var(--line, #e6dfd0);
    border-radius: 14px;
    box-shadow:
      0 1px 2px rgba(40,30,15,.04),
      0 22px 50px -16px rgba(40,30,15,.22);
    overflow: hidden;
    z-index: 50;
    font-family: 'Geist', system-ui, sans-serif;
    color: var(--ink, #15140f);
  }

  .forge-user-pop-head {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 14px 14px 12px;
    border-bottom: 1px solid var(--line, #e6dfd0);
  }
  .forge-user-pop-head .forge-user-avatar { width: 38px; height: 38px; font-size: 14px; }
  .forge-user-pop-head .who {
    min-width: 0;
    line-height: 1.2;
  }
  .forge-user-pop-head .who .name {
    font-size: 14px;
    font-weight: 700;
    color: var(--ink, #15140f);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .forge-user-pop-head .who .email {
    font-size: 12px;
    color: var(--muted, #8a8175);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .forge-user-pop-section { padding: 6px; }
  .forge-user-pop hr {
    border: 0;
    border-top: 1px solid var(--line, #e6dfd0);
    margin: 0;
  }

  .forge-user-item {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 9px 10px;
    border-radius: 8px;
    background: transparent;
    border: 0;
    cursor: pointer;
    font-family: inherit;
    font-size: 14px;
    color: var(--ink, #15140f);
    text-align: left;
    transition: background .1s ease;
  }
  .forge-user-item:hover,
  .forge-user-item:focus-visible {
    background: rgba(21,20,15,0.05);
    outline: none;
  }
  .forge-user-item svg.icon {
    width: 18px; height: 18px;
    color: var(--ink-2, #2b2a25);
    flex: none;
  }
  .forge-user-item .label { flex: 1; }
  .forge-user-item .meta  {
    font-size: 12px;
    color: var(--muted, #8a8175);
    font-family: 'JetBrains Mono', ui-monospace, monospace;
  }
  .forge-user-item svg.chev {
    width: 14px; height: 14px;
    color: var(--muted, #8a8175);
    flex: none;
  }
  .forge-user-item.danger { color: #c62828; }
  .forge-user-item.danger svg.icon { color: #c62828; }

  /* ── Submenu (Appearance) ───────────────────────────────────────── */
  .forge-user-submenu {
    position: absolute;
    left: calc(100% + 8px);
    top: 0;
    min-width: 180px;
    background: #fff;
    border: 1px solid var(--line, #e6dfd0);
    border-radius: 12px;
    padding: 6px;
    box-shadow:
      0 1px 2px rgba(40,30,15,.04),
      0 18px 36px -14px rgba(40,30,15,.2);
  }
  /* If we'd overflow the right edge, flip to the left side. */
  .forge-user-submenu.flip-left {
    left: auto;
    right: calc(100% + 8px);
  }
  .forge-user-submenu .forge-user-item.active {
    background: rgba(21,20,15,0.06);
    font-weight: 600;
  }
  .forge-user-submenu .forge-user-item.active svg.icon { color: var(--ink, #15140f); }

  /* On narrow viewports the submenu would hang off the side — stack it
     below the parent item instead. */
  @media (max-width: 480px) {
    .forge-user-submenu {
      position: static;
      margin: 4px 0 0 28px;
      box-shadow: none;
      border-color: transparent;
      padding: 2px 0;
      min-width: 0;
    }
  }

  /* ── Dark mode ─────────────────────────────────────────────────────
     The hardcoded white popover bg + var(--ink) text colors collapse to
     light-on-light in dark mode (--ink flips to a near-white token in
     Loom Midnight). Override every fixed light surface and shadow tone
     so the menu reads as a Loom Midnight card. */
  [data-color-scheme="dark"] .forge-user-pop {
    background: var(--surface, #232026);
    border-color: var(--hair, #2c2920);
    box-shadow:
      0 1px 2px rgba(0,0,0,0.5),
      0 22px 50px -16px rgba(0,0,0,0.6);
  }
  [data-color-scheme="dark"] .forge-user-pop-head {
    border-bottom-color: var(--hair, #2c2920);
  }
  [data-color-scheme="dark"] .forge-user-pop hr {
    border-top-color: var(--hair, #2c2920);
  }
  [data-color-scheme="dark"] .forge-user-item:hover,
  [data-color-scheme="dark"] .forge-user-item:focus-visible {
    background: rgba(255,255,255,0.06);
  }
  [data-color-scheme="dark"] .forge-user-submenu {
    background: var(--surface, #232026);
    border-color: var(--hair, #2c2920);
    box-shadow:
      0 1px 2px rgba(0,0,0,0.5),
      0 18px 36px -14px rgba(0,0,0,0.55);
  }
  [data-color-scheme="dark"] .forge-user-submenu .forge-user-item.active {
    background: rgba(255,255,255,0.10);
  }
  /* Danger (sign out) is hand-coded red — make sure it stays legible on dark
     bg too; the default #c62828 is fine but bump on hover for affordance. */
  [data-color-scheme="dark"] .forge-user-item.danger {
    color: oklch(0.78 0.18 25);
  }
  [data-color-scheme="dark"] .forge-user-item.danger svg.icon {
    color: oklch(0.78 0.18 25);
  }
  [data-color-scheme="dark"] .forge-user-item.danger:hover {
    background: rgba(220, 50, 50, 0.14);
  }
  /* Anchor chip (the trigger at the bottom of the home sidebar) needs the
     same hover/open contrast as the menu items. */
  [data-color-scheme="dark"] .forge-user-anchor:hover { background: rgba(255,255,255,0.05); }
  [data-color-scheme="dark"] .forge-user-anchor.open  { background: rgba(255,255,255,0.08); }
`

export const UserMenu: Component<Props> = (props) => {
  const navigate = useNavigate()
  const theme    = useTheme()

  const [open, setOpen]               = createSignal(false)
  const [submenu, setSubmenu]         = createSignal<null | "appearance">(null)
  let anchorRef:  HTMLButtonElement | undefined
  let popRef:     HTMLDivElement   | undefined

  function close() {
    setOpen(false)
    setSubmenu(null)
  }

  // ── Outside-click + Escape ───────────────────────────────────────────────
  function onDocClick(e: MouseEvent) {
    if (!open()) return
    const t = e.target as Node
    if (anchorRef?.contains(t)) return
    if (popRef?.contains(t))    return
    close()
  }
  function onKey(e: KeyboardEvent) {
    if (e.key === "Escape" && open()) {
      e.stopPropagation()
      close()
      anchorRef?.focus()
    }
  }
  onMount(() => {
    document.addEventListener("mousedown", onDocClick)
    document.addEventListener("keydown",   onKey)
  })
  onCleanup(() => {
    document.removeEventListener("mousedown", onDocClick)
    document.removeEventListener("keydown",   onKey)
  })

  // ── Display strings ──────────────────────────────────────────────────────
  const displayName = () => {
    const u = props.user
    return (u?.full_name?.trim() || u?.username || "Account")
  }
  const displayEmail = () => props.user?.email ?? "Signed in"
  const initials     = () => userInitials(props.user?.full_name, props.user?.username)

  // ── Actions ──────────────────────────────────────────────────────────────
  function handleProfile() {
    close()
    if (props.onOpenProfile) props.onOpenProfile()
    else props.onOpenSettings()  // sensible default — Settings has profile info
  }
  function handleSettings() {
    close()
    props.onOpenSettings()
  }
  function handleAppearance(scheme: ColorScheme) {
    theme.setColorScheme(scheme)
    close()
  }
  function handleSupport() {
    close()
    if (props.onOpenSupport) props.onOpenSupport()
    else window.open("https://forge.dev/support", "_blank", "noopener,noreferrer")
  }
  function handleDocs() {
    close()
    if (props.onOpenDocumentation) props.onOpenDocumentation()
    else window.open("https://forge.dev/docs", "_blank", "noopener,noreferrer")
  }
  function handleCommunity() {
    close()
    if (props.onOpenCommunity) props.onOpenCommunity()
    else window.open("https://forge.dev/community", "_blank", "noopener,noreferrer")
  }
  function handleHome() {
    close()
    navigate("/home")
  }
  function handleSignOut() {
    close()
    logout()
    navigate("/auth", { replace: true })
  }

  return (
    <>
      <style innerHTML={STYLES} />

      <button
        type="button"
        ref={(el) => (anchorRef = el)}
        class={`forge-user-anchor ${open() ? "open" : ""}`}
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open()}
      >
        <div class="forge-user-avatar" aria-hidden>{initials()}</div>
        <div class="forge-user-who">
          {displayName()}
          <span class="sub">{displayEmail()}</span>
        </div>
        <svg class="forge-user-caret" width="14" height="14" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>

      <Show when={open()}>
        <div
          class="forge-user-pop"
          role="menu"
          ref={(el) => (popRef = el)}
          onMouseLeave={() => setSubmenu(null)}
        >
          {/* Head — full identity */}
          <div class="forge-user-pop-head">
            <div class="forge-user-avatar" aria-hidden>{initials()}</div>
            <div class="who">
              <div class="name">{displayName()}</div>
              <div class="email">{displayEmail()}</div>
            </div>
          </div>

          {/* Body — actions */}
          <div class="forge-user-pop-section">
            <MenuButton icon={<UserIcon />}  label="Profile"  onClick={handleProfile} />
            <MenuButton icon={<GearIcon />}  label="Settings" meta="⌘." onClick={handleSettings} />

            {/* Appearance: hover/click to open submenu */}
            <div style={{ position: "relative" }}>
              <MenuButton
                icon={<ContrastIcon />}
                label="Appearance"
                chev
                onClick={() => setSubmenu(submenu() === "appearance" ? null : "appearance")}
                onMouseEnter={() => setSubmenu("appearance")}
              />
              <Show when={submenu() === "appearance"}>
                <div class="forge-user-submenu" role="menu">
                  <MenuButton
                    icon={<SunIcon />}
                    label="Light"
                    active={theme.colorScheme() === "light"}
                    onClick={() => handleAppearance("light")}
                  />
                  <MenuButton
                    icon={<MoonIcon />}
                    label="Dark"
                    active={theme.colorScheme() === "dark"}
                    onClick={() => handleAppearance("dark")}
                  />
                  <MenuButton
                    icon={<MonitorIcon />}
                    label="System"
                    active={theme.colorScheme() === "system"}
                    onClick={() => handleAppearance("system")}
                  />
                </div>
              </Show>
            </div>

            <MenuButton icon={<HelpIcon />}  label="Support"       chev onClick={handleSupport} />
            <MenuButton icon={<BookIcon />}  label="Documentation" chev onClick={handleDocs} />
            <MenuButton icon={<UsersIcon />} label="Community"          onClick={handleCommunity} />
            <MenuButton icon={<HomeIcon />}  label="Home"               onClick={handleHome} />
          </div>

          <hr />

          {/* Sign out — separate section so it's harder to mis-click. */}
          <div class="forge-user-pop-section">
            <MenuButton
              icon={<LogoutIcon />}
              label="Sign out"
              danger
              onClick={handleSignOut}
            />
          </div>
        </div>
      </Show>
    </>
  )
}

// ── MenuButton ────────────────────────────────────────────────────────────────

const MenuButton: Component<{
  icon:    JSX.Element
  label:   string
  meta?:   string
  chev?:   boolean
  active?: boolean
  danger?: boolean
  onClick: () => void
  onMouseEnter?: () => void
}> = (props) => {
  return (
    <button
      type="button"
      class={`forge-user-item ${props.danger ? "danger" : ""} ${props.active ? "active" : ""}`}
      role="menuitem"
      onClick={props.onClick}
      onMouseEnter={props.onMouseEnter}
    >
      <span class="icon-wrap" style={{ display: "inline-flex" }}>{props.icon}</span>
      <span class="label">{props.label}</span>
      <Show when={props.meta}>
        <span class="meta">{props.meta}</span>
      </Show>
      <Show when={props.chev}>
        <svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M9 6l6 6-6 6" />
        </svg>
      </Show>
    </button>
  )
}

// ── Icons (inlined, matching auth.tsx style) ─────────────────────────────────

function baseProps() {
  return {
    class:           "icon",
    viewBox:         "0 0 24 24",
    fill:            "none",
    stroke:          "currentColor",
    "stroke-width":  "1.8",
    "stroke-linecap":  "round",
    "stroke-linejoin": "round",
  } as const
}

function UserIcon()    { return <svg {...baseProps()}><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" /><circle cx="12" cy="7" r="4" /></svg> }
function GearIcon()    {
  return (
    <svg {...baseProps()}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
    </svg>
  )
}
function ContrastIcon(){ return <svg {...baseProps()}><circle cx="12" cy="12" r="9" /><path d="M12 3v18" fill="currentColor" stroke="none" /><path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor" stroke="none" /></svg> }
function HelpIcon()    { return <svg {...baseProps()}><circle cx="12" cy="12" r="9" /><path d="M9.5 9a2.5 2.5 0 1 1 4.3 1.7c-.7.7-1.8 1.1-1.8 2.3" /><circle cx="12" cy="17" r=".7" fill="currentColor" stroke="none" /></svg> }
function BookIcon()    { return <svg {...baseProps()}><path d="M4 4h12a3 3 0 0 1 3 3v13H7a3 3 0 0 1-3-3V4z" /><path d="M4 17h15" /></svg> }
function UsersIcon()   { return <svg {...baseProps()}><path d="M17 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /><circle cx="9.5" cy="7" r="3.5" /><path d="M22 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" /></svg> }
function HomeIcon()    { return <svg {...baseProps()}><path d="M3 11l9-8 9 8v9a2 2 0 0 1-2 2h-4v-7H9v7H5a2 2 0 0 1-2-2v-9z" /></svg> }
function LogoutIcon()  { return <svg {...baseProps()}><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="M16 17l5-5-5-5" /><path d="M21 12H9" /></svg> }
function SunIcon()     { return <svg {...baseProps()}><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.93 4.93l1.4 1.4M17.66 17.66l1.4 1.4M2 12h2M20 12h2M4.93 19.07l1.4-1.4M17.66 6.34l1.4-1.4" /></svg> }
function MoonIcon()    { return <svg {...baseProps()}><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" /></svg> }
function MonitorIcon() { return <svg {...baseProps()}><rect x="3" y="4" width="18" height="13" rx="2" /><path d="M8 21h8M12 17v4" /></svg> }
