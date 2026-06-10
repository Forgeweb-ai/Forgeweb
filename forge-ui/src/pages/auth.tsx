/**
 * auth.tsx
 * =========
 * Forge sign-in / sign-up page.
 *
 * Layout:
 *   Two-column split — auth form on the left, animated gradient with a
 *   typing prompt card on the right. On mobile (≤900px) the right column
 *   hides and the form takes the full width.
 */

import { createSignal, onCleanup, onMount, Show } from "solid-js"
import { useNavigate } from "@solidjs/router"
import { loginWithEmail, postAuthDestination, registerWithEmail } from "@/context/forge-api"
import { showToast } from "@opencode-ai/ui/toast"

type Mode = "signin" | "signup"

// ── Auth-page-wide CSS (scoped via .forge-auth-root) ─────────────────────────

const STYLES = `
  .forge-auth-root {
    height: 100%;
    overflow-y: auto;
    overflow-x: hidden;
    overscroll-behavior: contain;
    background: var(--bg, #fffbf4);
    color: var(--ink, #15140f);
    font-family: 'Geist', system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .forge-auth-root * { box-sizing: border-box; }

  .forge-auth-shell {
    display: grid;
    grid-template-columns: 1fr 1fr;
    min-height: 100%;
    width: 100%;
  }

  /* ── LEFT: form column ─────────────────────────────────────────────── */
  .forge-auth-left {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 56px 56px;
    position: relative;
    min-height: 100%;
  }
  .forge-auth-back {
    position: absolute;
    top: 24px; left: 28px;
    display: inline-flex; align-items: center; gap: 8px;
    background: none; border: 0; cursor: pointer;
    color: var(--muted, #8a8175);
    font-size: 13px; font-family: inherit;
    padding: 6px 10px; border-radius: 8px;
  }
  .forge-auth-back:hover { background: rgba(0,0,0,.04); color: var(--ink, #15140f); }
  .forge-auth-back .mark {
    width: 18px; height: 18px; border-radius: 4px;
    background-image: url('/forge-f-light.png');
    background-size: cover;
    background-position: center;
  }

  .forge-auth-card {
    width: 100%;
    max-width: 380px;
    display: flex;
    flex-direction: column;
  }
  .forge-auth-logo {
    width: 44px; height: 44px;
    border-radius: 12px;
    background-image: url('/forge-f-light.png');
    background-size: cover;
    background-position: center;
    margin-bottom: 22px;
  }
  .forge-auth-h1 {
    font-size: 30px;
    line-height: 1.1;
    font-weight: 700;
    letter-spacing: -0.025em;
    margin: 0 0 6px;
    color: var(--ink, #15140f);
  }
  .forge-auth-sub {
    font-size: 14px;
    color: var(--muted, #8a8175);
    margin: 0 0 28px;
  }

  .forge-auth-input {
    width: 100%;
    padding: 11px 14px;
    border-radius: 10px;
    border: 1px solid var(--line, #e6dfd0);
    background: #fff;
    color: var(--ink, #15140f);
    font-size: 14px;
    outline: none;
    font-family: inherit;
    transition: border-color .15s, box-shadow .15s;
  }
  .forge-auth-input:focus {
    border-color: var(--ink, #15140f);
    box-shadow: 0 0 0 3px rgba(21,20,15,0.06);
  }

  .forge-auth-submit {
    width: 100%;
    background: var(--ink, #15140f);
    color: #fff;
    border: 0;
    padding: 12px 18px;
    border-radius: 10px;
    font-size: 14px; font-weight: 600;
    cursor: pointer; font-family: inherit;
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    transition: opacity .15s, transform .1s;
  }
  .forge-auth-submit:disabled { opacity: 0.5; cursor: not-allowed; }
  .forge-auth-submit:not(:disabled):hover { transform: translateY(-1px); }

  .forge-auth-err {
    font-size: 13px; color: #c62828; margin: 0; padding: 0 2px;
  }

  .forge-auth-toggle {
    text-align: center;
    font-size: 13px; color: var(--muted, #8a8175);
    margin: 24px 0 0;
  }
  .forge-auth-toggle button {
    background: none; border: 0; cursor: pointer; padding: 0;
    color: var(--ink, #15140f); font-weight: 600; font-family: inherit;
    font-size: 13px;
    text-decoration: underline; text-underline-offset: 3px;
  }

  .forge-auth-foot {
    margin-top: 28px;
    font-size: 12px; line-height: 1.55;
    color: var(--muted, #8a8175);
    text-align: left;
  }
  .forge-auth-foot a { color: var(--ink-2, #2b2a25); text-decoration: underline; text-underline-offset: 2px; }

  /* ── RIGHT: plain cream panel with prompt card ─────────────────────── */
  .forge-auth-right {
    position: relative;
    overflow: hidden;
    padding: 24px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg, #f7f3ea);
    border-left: 1px solid var(--line, #e6dfd0);
  }
  .forge-auth-right-inner {
    position: relative; z-index: 1;
    width: 100%;
    max-width: 460px;
    display: flex; flex-direction: column; align-items: center; gap: 18px;
  }

  .forge-auth-prompt {
    width: 100%;
    background: #ffffff;
    border: 1px solid var(--line, #e6dfd0);
    border-radius: 16px;
    padding: 16px 18px;
    display: flex; align-items: center; gap: 12px;
    box-shadow: 0 1px 2px rgba(40,30,15,.04), 0 14px 30px -12px rgba(40,30,15,.12);
  }
  .forge-auth-prompt-text {
    flex: 1;
    font-size: 16px;
    line-height: 1.4;
    color: var(--ink, #15140f);
    letter-spacing: -0.005em;
    min-height: 22px;
  }
  .forge-auth-prompt-text .caret {
    display: inline-block; width: 2px; height: 1.05em;
    background: var(--ink, #15140f); vertical-align: -2px; margin-left: 1px;
    animation: forge-auth-blink 1s steps(2,end) infinite;
  }
  @keyframes forge-auth-blink { 50% { opacity: 0; } }
  .forge-auth-send {
    width: 36px; height: 36px;
    border-radius: 999px;
    background: var(--ink, #15140f); color: #fff;
    border: 0;
    display: grid; place-items: center;
    cursor: pointer; flex: none;
    transition: transform .15s ease;
  }
  .forge-auth-send:hover { transform: translateY(-1px); }
  .forge-auth-send svg { width: 16px; height: 16px; }

  .forge-auth-tagline {
    color: var(--muted-2, #6b6358);
    font-size: 14px; font-weight: 500;
    text-align: center;
    letter-spacing: -0.005em;
    max-width: 380px;
  }

  /* ── Tablet (≤1024px) ─────────────────────────────────────────────── */
  @media (max-width: 1024px) {
    .forge-auth-left { padding: 48px 36px; }
    .forge-auth-shell { grid-template-columns: 1fr 1fr; }
  }

  /* ── Mobile (≤900px) — hide the gradient, form fills the page ──────── */
  @media (max-width: 900px) {
    .forge-auth-shell { grid-template-columns: 1fr; }
    .forge-auth-right { display: none; }
    .forge-auth-left { padding: 80px 28px 48px; }
  }
  @media (max-width: 480px) {
    .forge-auth-left { padding: 72px 20px 40px; }
    .forge-auth-back { top: 18px; left: 18px; }
    .forge-auth-h1 { font-size: 26px; }
  }

  /* ── Dark mode ────────────────────────────────────────────────────────
     Hardcoded #fff surfaces (input, prompt card) collapse
     to white-on-white in dark mode because the text colors use var(--ink)
     which flips to a near-white token. Submit button has the inverse
     problem: bg=var(--ink) goes light + color:#fff stays white. Override
     every fixed light surface here so the auth flow reads as Loom Midnight
     when the user is in dark mode. */
  [data-color-scheme="dark"] .forge-auth-back {
    color: var(--muted, #807968);
  }
  [data-color-scheme="dark"] .forge-auth-back:hover {
    background: rgba(255,255,255,0.06);
    color: var(--ink, #f5efe0);
  }
  [data-color-scheme="dark"] .forge-auth-input {
    background: var(--surface, #232026);
    border-color: var(--hair, #2c2920);
    color: var(--ink, #f5efe0);
  }
  [data-color-scheme="dark"] .forge-auth-input::placeholder {
    color: var(--muted, #807968);
  }
  [data-color-scheme="dark"] .forge-auth-input:focus {
    border-color: var(--accent, oklch(0.75 0.16 75));
    box-shadow: 0 0 0 3px rgba(255,255,255,0.06);
  }
  /* Submit button: invert the light-mode "dark pill" to a light pill on dark
     so the contrast direction stays the same — it still pops against the bg. */
  [data-color-scheme="dark"] .forge-auth-submit {
    background: var(--ink, #f5efe0);
    color: var(--bg, #15140f);
  }
  [data-color-scheme="dark"] .forge-auth-toggle button {
    color: var(--ink, #f5efe0);
  }
  /* Right panel cream → use the dark bg + a tinted border. */
  [data-color-scheme="dark"] .forge-auth-right {
    background: var(--bg, #15140f);
    border-left-color: var(--hair, #2c2920);
  }
  [data-color-scheme="dark"] .forge-auth-prompt {
    background: var(--surface, #232026);
    border-color: var(--hair, #2c2920);
    box-shadow: 0 1px 2px rgba(0,0,0,.5), 0 14px 30px -12px rgba(0,0,0,.6);
  }
  [data-color-scheme="dark"] .forge-auth-prompt-text {
    color: var(--ink, #f5efe0);
  }
  [data-color-scheme="dark"] .forge-auth-foot a {
    color: var(--ink-2, #d8d0bd);
  }
  [data-color-scheme="dark"] .forge-auth-err {
    color: oklch(0.78 0.18 25);
  }
`

// ── Prompt cycle for the right panel ─────────────────────────────────────────

const PROMPTS = [
  "Ask Forge to build interactive prototypes.",
  "Build a customer feedback tool with AI analytics.",
  "Design a dashboard for monitoring SaaS metrics.",
  "Create a landing page for my startup.",
  "Build a Notion-style note-taking app.",
]

// ── Main component ──────────────────────────────────────────────────────────

export default function Auth() {
  const navigate  = useNavigate()
  const [mode, setMode]       = createSignal<Mode>("signin")
  const [email, setEmail]     = createSignal("")
  const [password, setPassword] = createSignal("")
  const [loading, setLoading] = createSignal(false)

  const isSignIn = () => mode() === "signin"

  function resetForm() {
    setEmail("")
    setPassword("")
  }

  function toggleMode() {
    setMode(isSignIn() ? "signup" : "signin")
    resetForm()
  }

  async function handleSubmit(e: Event) {
    e.preventDefault()
    const em = email().trim()
    const pw = password()
    if (!em || !pw) return
    setLoading(true)

    const result = isSignIn()
      ? await loginWithEmail(em, pw)
      : await registerWithEmail(em, pw)

    setLoading(false)

    if ("error" in result) {
      showToast({ variant: "error", title: result.error })
      return
    }

    // Route based on the user's auth/onboarding state:
    //   • fresh signup / no onboarding → /onboarding/style
    //     (email verification is currently disabled — BE registers accounts
    //     with email_verified=true, so /auth/verify-email is skipped)
    //   • fully set up → /home
    navigate(postAuthDestination(result), { replace: true })
  }

  // Typing animation for the right-side prompt card
  let promptRef: HTMLDivElement | undefined
  onMount(() => {
    if (!promptRef) return
    const el = promptRef
    let promptIdx = 0
    let charIdx = 0
    let phase: "typing" | "holding" | "deleting" = "typing"
    let timer: ReturnType<typeof setTimeout> | null = null

    function render(text: string) {
      el.innerHTML = escapeHtml(text) + '<span class="caret"></span>'
    }
    function tick() {
      const target = PROMPTS[promptIdx]
      if (phase === "typing") {
        charIdx++
        render(target.slice(0, charIdx))
        if (charIdx >= target.length) {
          phase = "holding"
          timer = setTimeout(tick, 2200)
          return
        }
        const ch = target[charIdx - 1]
        const d = ch === " " ? 60 : (Math.random() < 0.08 ? 130 : 30 + Math.random() * 40)
        timer = setTimeout(tick, d)
      } else if (phase === "holding") {
        phase = "deleting"
        timer = setTimeout(tick, 30)
      } else {
        charIdx--
        render(target.slice(0, charIdx))
        if (charIdx <= 0) {
          phase = "typing"
          promptIdx = (promptIdx + 1) % PROMPTS.length
          timer = setTimeout(tick, 400)
          return
        }
        timer = setTimeout(tick, 18)
      }
    }
    tick()

    onCleanup(() => { if (timer) clearTimeout(timer) })
  })

  return (
    <>
      <style innerHTML={STYLES} />
      <div class="forge-auth-root">
        <div class="forge-auth-shell">

          {/* ── LEFT: form column ─────────────────────────────────────── */}
          <div class="forge-auth-left">
            <button class="forge-auth-back" onClick={() => navigate("/")}>
              <span class="mark" />
              Forge
            </button>

            <div class="forge-auth-card">
              <div class="forge-auth-logo" aria-hidden />
              <h1 class="forge-auth-h1">
                {isSignIn() ? "Welcome back" : "Create your account"}
              </h1>
              <p class="forge-auth-sub">
                {isSignIn()
                  ? "Sign in to keep building."
                  : "Start shipping in seconds — no credit card."}
              </p>

              <form onSubmit={(e) => void handleSubmit(e)}>
                  <div style={{ display: "flex", "flex-direction": "column", gap: "10px" }}>
                    <input
                      class="forge-auth-input"
                      type="email"
                      placeholder="Email address"
                      value={email()}
                      onInput={(e) => setEmail(e.currentTarget.value)}
                      autofocus
                    />
                    <input
                      class="forge-auth-input"
                      type="password"
                      placeholder={isSignIn() ? "Password" : "Create a password"}
                      value={password()}
                      onInput={(e) => setPassword(e.currentTarget.value)}
                    />

                    <button
                      type="submit"
                      class="forge-auth-submit"
                      disabled={loading() || !email().trim() || !password()}
                    >
                      <Show when={loading()}>
                        <SpinnerIcon />
                      </Show>
                      {loading()
                        ? (isSignIn() ? "Signing in…" : "Creating account…")
                        : (isSignIn() ? "Sign in" : "Create account")}
                    </button>
                  </div>
                </form>

              <p class="forge-auth-toggle">
                {isSignIn() ? "New to Forge? " : "Already have an account? "}
                <button onClick={toggleMode}>
                  {isSignIn() ? "Create a free account" : "Log in"}
                </button>
              </p>

              <p class="forge-auth-foot">
                By continuing you agree to Forge's{" "}
                <a href="#">Terms of Service</a> and <a href="#">Privacy Policy</a>.
              </p>
            </div>
          </div>

          {/* ── RIGHT: plain cream panel with the prompt card ─────────── */}
          <div class="forge-auth-right" aria-hidden>
            <div class="forge-auth-right-inner">
              <div class="forge-auth-prompt">
                <div ref={promptRef} class="forge-auth-prompt-text">
                  <span class="caret" />
                </div>
                <button class="forge-auth-send" type="button" aria-label="Send">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M12 19V5M5 12l7-7 7 7" />
                  </svg>
                </button>
              </div>
              <div class="forge-auth-tagline">
                Describe what you want to build — Forge ships the rest.
              </div>
            </div>
          </div>

        </div>
      </div>
    </>
  )
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function escapeHtml(s: string) {
  return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]!))
}

// ── Icons ───────────────────────────────────────────────────────────────────

function SpinnerIcon() {
  return <span class="forge-css-spinner" style={{ width: "14px", height: "14px", "border-width": "2px" }} />
}
