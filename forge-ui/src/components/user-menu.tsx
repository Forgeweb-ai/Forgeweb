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
 * Click opens the popover:
 *   Profile · Settings · Support · Home · ─── · Sign out
 *
 * Support opens a small modal with the contact email (help@forgeweb.ai).
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
import { type CurrentUser, logout, userInitials } from "@/context/forge-api"

type Props = {
  user:           CurrentUser | null
  onOpenSettings: () => void
  /** Optional handlers; if omitted, sensible defaults are used. */
  onOpenProfile?: () => void
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

  /* ── Support modal ──────────────────────────────────────────────── */
  .forge-support-overlay {
    position: fixed;
    inset: 0;
    background: rgba(21,20,15,0.35);
    z-index: 100;
    display: grid;
    place-items: center;
  }
  .forge-support-modal {
    width: min(380px, calc(100vw - 32px));
    background: #fff;
    border: 1px solid var(--line, #e6dfd0);
    border-radius: 14px;
    padding: 22px;
    box-shadow:
      0 1px 2px rgba(40,30,15,.04),
      0 22px 50px -16px rgba(40,30,15,.22);
    font-family: 'Geist', system-ui, sans-serif;
    color: var(--ink, #15140f);
  }
  .forge-support-modal h2 {
    margin: 0 0 8px;
    font-size: 16px;
    font-weight: 700;
  }
  .forge-support-modal p {
    margin: 0 0 14px;
    font-size: 13.5px;
    line-height: 1.5;
    color: var(--muted-2, #6b6358);
  }
  .forge-support-modal a {
    color: var(--ink, #15140f);
    font-weight: 600;
    text-decoration: underline;
    text-underline-offset: 3px;
  }
  .forge-support-close {
    width: 100%;
    margin-top: 4px;
    background: var(--ink, #15140f);
    color: #fff;
    border: 0;
    padding: 10px 16px;
    border-radius: 10px;
    font-size: 13.5px; font-weight: 600;
    cursor: pointer; font-family: inherit;
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
  [data-color-scheme="dark"] .forge-support-modal {
    background: var(--surface, #232026);
    border-color: var(--hair, #2c2920);
    box-shadow:
      0 1px 2px rgba(0,0,0,0.5),
      0 22px 50px -16px rgba(0,0,0,0.6);
  }
  [data-color-scheme="dark"] .forge-support-overlay {
    background: rgba(0,0,0,0.5);
  }
  [data-color-scheme="dark"] .forge-support-modal a {
    color: var(--ink, #f5efe0);
  }
  [data-color-scheme="dark"] .forge-support-close {
    background: var(--ink, #f5efe0);
    color: #15140f;
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

  const [open, setOpen]               = createSignal(false)
  const [supportOpen, setSupportOpen] = createSignal(false)
  let anchorRef:  HTMLButtonElement | undefined
  let popRef:     HTMLDivElement   | undefined

  function close() {
    setOpen(false)
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
    if (e.key !== "Escape") return
    if (supportOpen()) {
      e.stopPropagation()
      setSupportOpen(false)
      return
    }
    if (open()) {
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
  function handleSupport() {
    close()
    setSupportOpen(true)
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
            <MenuButton icon={<HelpIcon />}  label="Support"  onClick={handleSupport} />
            <MenuButton icon={<HomeIcon />}  label="Home"     onClick={handleHome} />
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

      {/* Support modal */}
      <Show when={supportOpen()}>
        <div
          class="forge-support-overlay"
          onClick={(e) => { if (e.target === e.currentTarget) setSupportOpen(false) }}
        >
          <div class="forge-support-modal" role="dialog" aria-modal="true" aria-label="Support">
            <h2>Support</h2>
            <p>
              Questions, bug reports, or feedback — we'd love to hear from you.
              Reach us at <a href="mailto:help@forgeweb.ai">help@forgeweb.ai</a> and
              we'll get back to you as soon as we can.
            </p>
            <p>
              Or join our{" "}
              <a href="https://discord.gg/anpsmJmn2" target="_blank" rel="noopener noreferrer">
                Discord help channel
              </a>{" "}
              for faster answers from the team and community.
            </p>
            <button type="button" class="forge-support-close" onClick={() => setSupportOpen(false)}>
              Close
            </button>
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
function HelpIcon()    { return <svg {...baseProps()}><circle cx="12" cy="12" r="9" /><path d="M9.5 9a2.5 2.5 0 1 1 4.3 1.7c-.7.7-1.8 1.1-1.8 2.3" /><circle cx="12" cy="17" r=".7" fill="currentColor" stroke="none" /></svg> }
function HomeIcon()    { return <svg {...baseProps()}><path d="M3 11l9-8 9 8v9a2 2 0 0 1-2 2h-4v-7H9v7H5a2 2 0 0 1-2-2v-9z" /></svg> }
function LogoutIcon()  { return <svg {...baseProps()}><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="M16 17l5-5-5-5" /><path d="M21 12H9" /></svg> }
