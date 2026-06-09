/**
 * onboarding.tsx
 * ===============
 * Four-step new-user flow that runs after email verification:
 *
 *   /onboarding/style    → pick light / dark theme
 *   /onboarding/name     → enter full name
 *   /onboarding/role     → pick role (8 tiles)
 *   /onboarding/company  → pick company size (4 tiles)  → submits, → /home
 *
 * Layout mirrors the Lovable reference (centered, full-bleed, step dots at
 * the bottom) but uses Forge's cream/ink palette — no purple/pink gradient.
 *
 * Cross-step state lives in a module-scoped store (`draft`) so we don't have
 * to thread props through a router. The store is cleared on successful
 * submit so a re-entry into the flow starts fresh.
 *
 * Sits OUTSIDE AppShellProviders; uses the public helpers in forge-api.tsx.
 */

import { createMemo, createSignal, JSX, onMount, Show } from "solid-js"
import { useNavigate } from "@solidjs/router"
import {
  completeOnboarding,
  fetchCurrentUser,
  isAuthenticated,
  logout,
  postAuthDestination,
} from "@/context/forge-api"

// ── Cross-step draft store ───────────────────────────────────────────────────
// Plain mutable object; we don't need reactivity across steps because each
// step navigates fresh and only reads what it cares about.

type Theme       = "light" | "dark"
type Role        =
  | "founder" | "product" | "designer" | "engineer"
  | "consultant" | "marketing-sales" | "operations" | "other"
type CompanySize = "solo" | "2-20" | "21-200" | "200+"

type Draft = {
  theme:        Theme | null
  full_name:    string
  role:         Role | null
  company_size: CompanySize | null
}

const draft: Draft = {
  theme:        null,
  full_name:    "",
  role:         null,
  company_size: null,
}

function resetDraft() {
  draft.theme        = null
  draft.full_name    = ""
  draft.role         = null
  draft.company_size = null
}

// ── Shared styles (scoped via .forge-onboard-root) ───────────────────────────

const STYLES = `
  .forge-onboard-root {
    height: 100%;
    width: 100%;
    overflow-y: auto;
    background: var(--bg, #fffbf4);
    color: var(--ink, #15140f);
    font-family: 'Geist', system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 48px 24px 80px;
    position: relative;
  }
  .forge-onboard-root * { box-sizing: border-box; }

  .forge-onboard-logo {
    width: 44px; height: 44px;
    border-radius: 12px;
    background-image: url('/forge-f-light.png');
    background-size: cover;
    background-position: center;
    margin-bottom: 22px;
  }

  .forge-onboard-h1 {
    font-size: 30px;
    line-height: 1.1;
    font-weight: 700;
    letter-spacing: -0.025em;
    margin: 0 0 24px;
    text-align: center;
    color: var(--ink, #15140f);
  }

  .forge-onboard-err {
    margin: 10px 0 0;
    font-size: 13px;
    color: #c62828;
    text-align: center;
  }

  /* ── Step dots at the bottom ────────────────────────────────────────── */
  .forge-onboard-dots {
    position: absolute;
    left: 0; right: 0;
    bottom: 32px;
    display: flex;
    gap: 6px;
    justify-content: center;
  }
  .forge-onboard-dots span {
    display: inline-block;
    width: 22px;
    height: 4px;
    border-radius: 2px;
    background: var(--line, #e6dfd0);
    transition: background .15s, width .15s;
  }
  .forge-onboard-dots span.active {
    background: var(--ink, #15140f);
    width: 28px;
  }

  /* ── Next / Back buttons ────────────────────────────────────────────── */
  .forge-onboard-actions {
    margin-top: 28px;
    display: flex;
    gap: 12px;
    align-items: center;
    justify-content: center;
  }
  .forge-onboard-back {
    background: none;
    border: 0;
    cursor: pointer;
    padding: 10px 14px;
    color: var(--muted, #8a8175);
    font-family: inherit;
    font-size: 13px;
    border-radius: 999px;
  }
  .forge-onboard-back:hover { color: var(--ink, #15140f); }
  .forge-onboard-next {
    background: var(--ink, #15140f);
    color: #fff;
    border: 0;
    border-radius: 999px;
    padding: 11px 22px;
    font-size: 14px;
    font-weight: 600;
    font-family: inherit;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    transition: opacity .15s, transform .1s;
  }
  .forge-onboard-next:disabled { opacity: 0.4; cursor: not-allowed; }
  .forge-onboard-next:not(:disabled):hover { transform: translateY(-1px); }

  /* ── Style step: two large theme cards ─────────────────────────────── */
  .forge-onboard-theme-row {
    display: flex;
    gap: 22px;
    flex-wrap: wrap;
    justify-content: center;
  }
  .forge-onboard-theme {
    width: 200px;
    cursor: pointer;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 10px;
  }
  .forge-onboard-theme .preview {
    width: 100%;
    aspect-ratio: 5 / 4;
    border-radius: 14px;
    border: 2px solid transparent;
    background: #fff;
    box-shadow: 0 1px 2px rgba(40,30,15,.04), 0 8px 18px -10px rgba(40,30,15,.10);
    padding: 14px;
    transition: border-color .15s, transform .1s;
    position: relative;
    overflow: hidden;
  }
  .forge-onboard-theme.dark .preview { background: #1a1814; }
  .forge-onboard-theme .preview .bar {
    height: 6px;
    border-radius: 3px;
    background: var(--line, #e6dfd0);
    margin-bottom: 6px;
  }
  .forge-onboard-theme.dark .preview .bar { background: #3a342b; }
  .forge-onboard-theme .preview .bar.short { width: 50%; }
  .forge-onboard-theme .preview .mark {
    position: absolute;
    top: 12px; right: 12px;
    width: 14px; height: 14px;
    border-radius: 4px;
    background-image: url('/forge-f-light.png');
    background-size: cover;
  }
  .forge-onboard-theme.selected .preview {
    border-color: var(--ink, #15140f);
    transform: translateY(-2px);
  }
  .forge-onboard-theme .label {
    font-size: 14px;
    font-weight: 500;
    color: var(--ink, #15140f);
  }

  /* ── Name step: full-name input ─────────────────────────────────────── */
  .forge-onboard-form {
    width: 100%;
    max-width: 360px;
  }
  .forge-onboard-label {
    display: block;
    font-size: 13px;
    font-weight: 500;
    color: var(--ink, #15140f);
    margin-bottom: 6px;
  }
  .forge-onboard-input {
    width: 100%;
    padding: 12px 14px;
    border-radius: 10px;
    border: 1px solid var(--line, #e6dfd0);
    background: #fff;
    color: var(--ink, #15140f);
    font-size: 14px;
    outline: none;
    font-family: inherit;
    transition: border-color .15s, box-shadow .15s;
  }
  .forge-onboard-input:focus {
    border-color: var(--ink, #15140f);
    box-shadow: 0 0 0 3px rgba(21,20,15,0.06);
  }

  /* ── Tile grids (role + company) ────────────────────────────────────── */
  .forge-onboard-grid {
    display: grid;
    gap: 14px;
    width: 100%;
  }
  .forge-onboard-grid.cols-4 {
    grid-template-columns: repeat(4, minmax(0, 1fr));
    max-width: 720px;
  }
  .forge-onboard-grid.cols-4-row {
    grid-template-columns: repeat(4, minmax(0, 1fr));
    max-width: 720px;
  }
  .forge-onboard-tile {
    background: #fff;
    border: 1px solid var(--line, #e6dfd0);
    border-radius: 14px;
    padding: 22px 16px;
    cursor: pointer;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 10px;
    color: var(--ink, #15140f);
    font-family: inherit;
    transition: border-color .15s, background .15s, transform .1s;
  }
  .forge-onboard-tile:hover {
    border-color: var(--ink, #15140f);
    transform: translateY(-1px);
  }
  .forge-onboard-tile.selected,
  .forge-onboard-tile:focus-visible {
    border-color: var(--ink, #15140f);
    background: #faf6ec;
    outline: none;
  }
  .forge-onboard-tile svg {
    width: 22px; height: 22px;
    color: var(--ink, #15140f);
  }
  .forge-onboard-tile-label {
    font-size: 13px;
    font-weight: 500;
    text-align: center;
  }

  /* ── Tablet/mobile ──────────────────────────────────────────────────── */
  @media (max-width: 720px) {
    .forge-onboard-grid.cols-4,
    .forge-onboard-grid.cols-4-row { grid-template-columns: repeat(2, minmax(0, 1fr)); max-width: 420px; }
    .forge-onboard-theme { width: 160px; }
  }
  @media (max-width: 480px) {
    .forge-onboard-h1 { font-size: 24px; }
    .forge-onboard-theme-row { gap: 12px; }
    .forge-onboard-theme { width: 140px; }
  }
`

// ── Step definitions ─────────────────────────────────────────────────────────

export type OnboardingStep = "style" | "name" | "role" | "company"

const STEP_ORDER: OnboardingStep[] = ["style", "name", "role", "company"]

const ROLES: Array<{ id: Role; label: string; icon: () => JSX.Element }> = [
  { id: "founder",         label: "Founder",          icon: BuildingIcon },
  { id: "product",         label: "Product",          icon: SparklesIcon },
  { id: "designer",        label: "Designer",         icon: CompassIcon  },
  { id: "engineer",        label: "Engineer",         icon: TerminalIcon },
  { id: "consultant",      label: "Consultant",       icon: ChartIcon    },
  { id: "marketing-sales", label: "Marketing / Sales", icon: TargetIcon   },
  { id: "operations",      label: "Operations",       icon: GearIcon     },
  { id: "other",           label: "Other",            icon: UserIcon     },
]

const COMPANY_SIZES: Array<{ id: CompanySize; label: string; icon: () => JSX.Element }> = [
  { id: "solo",   label: "Solo",    icon: () => DotsIcon(1) },
  { id: "2-20",   label: "2 - 20",  icon: () => DotsIcon(2) },
  { id: "21-200", label: "21 - 200", icon: () => DotsIcon(3) },
  { id: "200+",   label: "200+",    icon: () => DotsIcon(4) },
]

// ── Main component ──────────────────────────────────────────────────────────

export default function Onboarding(props: { step: OnboardingStep }) {
  const navigate = useNavigate()

  const [error, setError]       = createSignal("")
  const [submitting, setSubmit] = createSignal(false)

  // Local-to-this-render mirrors so the UI reacts to clicks without rerunning
  // onMount. We seed them from the module-level draft so back-navigation
  // restores the previous choice.
  const [theme,    setTheme]    = createSignal<Theme | null>(draft.theme)
  const [name,     setName]     = createSignal(draft.full_name)
  const [role,     setRole]     = createSignal<Role | null>(draft.role)

  onMount(async () => {
    // Guard: must be signed in + verified, and not already onboarded.
    if (!isAuthenticated()) {
      navigate("/auth", { replace: true })
      return
    }
    const me = await fetchCurrentUser()
    if (!me) {
      logout()
      navigate("/auth", { replace: true })
      return
    }
    if (!me.email_verified) {
      navigate("/auth/verify-email", { replace: true })
      return
    }
    if (me.onboarding_completed) {
      navigate("/home", { replace: true })
      return
    }
  })

  const stepIndex = createMemo(() => STEP_ORDER.indexOf(props.step))

  function goNext() {
    const idx = stepIndex()
    if (idx < 0) return
    const next = STEP_ORDER[idx + 1]
    if (next) navigate(`/onboarding/${next}`)
  }

  function goBack() {
    const idx = stepIndex()
    const prev = STEP_ORDER[idx - 1]
    if (prev) navigate(`/onboarding/${prev}`)
  }

  // ── Step handlers ─────────────────────────────────────────────────────────

  function chooseTheme(t: Theme) {
    setTheme(t)
    draft.theme = t
  }
  function chooseRole(r: Role) {
    setRole(r)
    draft.role = r
    goNext()  // auto-advance: matches the Lovable reference
  }
  async function chooseCompany(size: CompanySize) {
    draft.company_size = size

    // Final step — submit everything. Bail early if a previous step's data
    // is missing (back-button / refresh edge case). Send them to the missing
    // step so they can fill it in.
    if (!draft.theme) {
      navigate("/onboarding/style", { replace: true })
      return
    }
    if (!draft.full_name.trim()) {
      navigate("/onboarding/name", { replace: true })
      return
    }
    if (!draft.role) {
      navigate("/onboarding/role", { replace: true })
      return
    }

    setError("")
    setSubmit(true)
    const result = await completeOnboarding({
      full_name:    draft.full_name.trim(),
      role:         draft.role,
      company_size: size,
      theme_pref:   draft.theme,
    })
    setSubmit(false)

    if ("error" in result) {
      setError(result.error)
      return
    }
    resetDraft()
    navigate(postAuthDestination(result), { replace: true })
  }

  function handleNameNext() {
    const trimmed = name().trim()
    if (!trimmed) {
      setError("Please enter your name")
      return
    }
    draft.full_name = trimmed
    setError("")
    goNext()
  }

  // ── Rendering helpers ─────────────────────────────────────────────────────

  function Dots() {
    return (
      <div class="forge-onboard-dots" aria-hidden>
        {STEP_ORDER.map((_, i) => (
          <span class={i === stepIndex() ? "active" : ""} />
        ))}
      </div>
    )
  }

  return (
    <>
      <style innerHTML={STYLES} />
      <div class="forge-onboard-root">
        <div class="forge-onboard-logo" aria-hidden />

        <Show when={props.step === "style"}>
          <h1 class="forge-onboard-h1">Pick your style</h1>
          <div class="forge-onboard-theme-row">
            <button
              type="button"
              class={`forge-onboard-theme light ${theme() === "light" ? "selected" : ""}`}
              onClick={() => chooseTheme("light")}
            >
              <div class="preview">
                <span class="mark" />
                <div class="bar" />
                <div class="bar short" />
                <div class="bar" />
              </div>
              <div class="label">Light</div>
            </button>
            <button
              type="button"
              class={`forge-onboard-theme dark ${theme() === "dark" ? "selected" : ""}`}
              onClick={() => chooseTheme("dark")}
            >
              <div class="preview">
                <span class="mark" />
                <div class="bar" />
                <div class="bar short" />
                <div class="bar" />
              </div>
              <div class="label">Dark</div>
            </button>
          </div>
          <div class="forge-onboard-actions">
            <button class="forge-onboard-next" onClick={goNext} disabled={!theme()}>
              Next <ArrowIcon />
            </button>
          </div>
        </Show>

        <Show when={props.step === "name"}>
          <h1 class="forge-onboard-h1">What's your name?</h1>
          <div class="forge-onboard-form">
            <label class="forge-onboard-label" for="forge-onboard-name">Full name</label>
            <input
              id="forge-onboard-name"
              class="forge-onboard-input"
              type="text"
              placeholder="Enter your name"
              value={name()}
              onInput={(e) => { setName(e.currentTarget.value); setError("") }}
              onKeyDown={(e) => { if (e.key === "Enter") handleNameNext() }}
              autofocus
            />
            <Show when={error()}>
              <p class="forge-onboard-err">{error()}</p>
            </Show>
          </div>
          <div class="forge-onboard-actions">
            <button class="forge-onboard-back" onClick={goBack}>Back</button>
            <button class="forge-onboard-next" onClick={handleNameNext} disabled={!name().trim()}>
              Next <ArrowIcon />
            </button>
          </div>
        </Show>

        <Show when={props.step === "role"}>
          <h1 class="forge-onboard-h1">Which role fits you best?</h1>
          <div class="forge-onboard-grid cols-4">
            {ROLES.map((r) => (
              <button
                type="button"
                class={`forge-onboard-tile ${role() === r.id ? "selected" : ""}`}
                onClick={() => chooseRole(r.id)}
              >
                {r.icon()}
                <span class="forge-onboard-tile-label">{r.label}</span>
              </button>
            ))}
          </div>
          <div class="forge-onboard-actions">
            <button class="forge-onboard-back" onClick={goBack}>Back</button>
          </div>
        </Show>

        <Show when={props.step === "company"}>
          <h1 class="forge-onboard-h1">How many people work at your company?</h1>
          <div class="forge-onboard-grid cols-4-row">
            {COMPANY_SIZES.map((c) => (
              <button
                type="button"
                class="forge-onboard-tile"
                onClick={() => void chooseCompany(c.id)}
                disabled={submitting()}
              >
                {c.icon()}
                <span class="forge-onboard-tile-label">{c.label}</span>
              </button>
            ))}
          </div>
          <Show when={error()}>
            <p class="forge-onboard-err">{error()}</p>
          </Show>
          <div class="forge-onboard-actions">
            <button class="forge-onboard-back" onClick={goBack} disabled={submitting()}>Back</button>
            <Show when={submitting()}>
              <SpinnerIcon />
            </Show>
          </div>
        </Show>

        <Dots />
      </div>
    </>
  )
}

// ── Icons ────────────────────────────────────────────────────────────────────
// Stroke-only SVGs to keep the file dependency-light. Sized via the parent
// `.forge-onboard-tile svg` rule.

function svgProps() {
  return {
    viewBox:        "0 0 24 24",
    fill:           "none",
    stroke:         "currentColor",
    "stroke-width": "1.8",
    "stroke-linecap":  "round",
    "stroke-linejoin": "round",
  } as const
}

function BuildingIcon() {
  return (
    <svg {...svgProps()}>
      <rect x="4" y="3" width="16" height="18" rx="1.5" />
      <path d="M8 7h.01M12 7h.01M16 7h.01M8 11h.01M12 11h.01M16 11h.01M8 15h.01M12 15h.01M16 15h.01" />
      <path d="M10 21v-3a2 2 0 0 1 4 0v3" />
    </svg>
  )
}
function SparklesIcon() {
  return (
    <svg {...svgProps()}>
      <path d="M12 3l1.6 4.4L18 9l-4.4 1.6L12 15l-1.6-4.4L6 9l4.4-1.6L12 3z" />
      <path d="M19 14l.8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8L19 14z" />
    </svg>
  )
}
function CompassIcon() {
  return (
    <svg {...svgProps()}>
      <circle cx="12" cy="12" r="9" />
      <path d="M14.8 9.2L13 13l-3.8 1.8L11 11l3.8-1.8z" />
    </svg>
  )
}
function TerminalIcon() {
  return (
    <svg {...svgProps()}>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M7 9l3 3-3 3M13 15h4" />
    </svg>
  )
}
function ChartIcon() {
  return (
    <svg {...svgProps()}>
      <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />
    </svg>
  )
}
function TargetIcon() {
  return (
    <svg {...svgProps()}>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="5" />
      <circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none" />
    </svg>
  )
}
function GearIcon() {
  return (
    <svg {...svgProps()}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
    </svg>
  )
}
function UserIcon() {
  return (
    <svg {...svgProps()}>
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </svg>
  )
}

// "DotsIcon(n)" renders n dots in an arc-ish layout — used for company-size
// tiles. Mirrors the reference Lovable design without making four bespoke
// icons.
function DotsIcon(n: number) {
  const positions: Array<[number, number]> = [
    [12, 8],
    [9, 12], [15, 12],
    [9, 12], [12, 8],  [15, 12],
    [8, 13], [12, 8],  [16, 13], [12, 16],
  ]
  let pts: Array<[number, number]> = []
  if (n === 1) pts = [[12, 12]]
  if (n === 2) pts = [[9, 12], [15, 12]]
  if (n === 3) pts = positions.slice(3, 6)
  if (n === 4) pts = positions.slice(6, 10)
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" stroke="none">
      {n === 1 && <circle cx="12" cy="12" r="6" fill="none" stroke="currentColor" stroke-width="1.6" />}
      {n > 1 && pts.map(([x, y]) => <circle cx={x} cy={y} r="1.6" />)}
    </svg>
  )
}

function ArrowIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M5 12h14M13 5l7 7-7 7" />
    </svg>
  )
}

function SpinnerIcon() {
  return <span class="forge-css-spinner" style={{ width: "16px", height: "16px", "border-width": "2px" }} />
}
