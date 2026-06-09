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

  .forge-auth-btn-row { display: flex; flex-direction: column; gap: 10px; }
  .forge-auth-social {
    width: 100%;
    display: inline-flex; align-items: center; justify-content: center;
    gap: 10px;
    padding: 11px 16px;
    border-radius: 10px;
    border: 1px solid var(--line, #e6dfd0);
    background: #fff;
    color: var(--ink-2, #2b2a25);
    font-size: 14px; font-weight: 500;
    cursor: pointer; font-family: inherit;
    transition: border-color .15s, background .15s, transform .1s;
  }
  .forge-auth-social:hover { border-color: var(--line-strong, #d4cab2); background: #fafaf6; }
  .forge-auth-social:active { transform: translateY(1px); }

  .forge-auth-divider {
    display: flex; align-items: center; gap: 12px;
    margin: 18px 0 10px;
    font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase;
    color: var(--muted, #8a8175);
  }
  .forge-auth-divider::before,
  .forge-auth-divider::after { content: ''; flex: 1; height: 1px; background: var(--line, #e6dfd0); }

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

  .forge-auth-back-step {
    background: none; border: 0; cursor: pointer; padding: 0;
    color: var(--muted, #8a8175); font-size: 12px; font-family: inherit;
    display: inline-flex; align-items: center; gap: 5px;
    margin-bottom: 14px;
  }
  .forge-auth-back-step:hover { color: var(--ink, #15140f); }

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
     Hardcoded #fff surfaces (social button, input, prompt card) collapse
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
  [data-color-scheme="dark"] .forge-auth-social {
    background: var(--surface, #232026);
    border-color: var(--hair, #2c2920);
    color: var(--ink, #f5efe0);
  }
  [data-color-scheme="dark"] .forge-auth-social:hover {
    background: var(--surface-2, #2c2920);
    border-color: var(--hair-2, #3a342a);
  }
  [data-color-scheme="dark"] .forge-auth-divider::before,
  [data-color-scheme="dark"] .forge-auth-divider::after {
    background: var(--hair, #2c2920);
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
  const [step, setStep]       = createSignal<"social" | "email">("social")
  const [email, setEmail]     = createSignal("")
  const [password, setPassword] = createSignal("")
  const [loading, setLoading] = createSignal(false)

  const isSignIn = () => mode() === "signin"

  function resetForm() {
    setEmail("")
    setPassword("")
    setStep("social")
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
    //   • fresh signup    → /auth/verify-email
    //   • verified, no onboarding → /onboarding/style
    //   • fully set up    → /home
    navigate(postAuthDestination(result), { replace: true })
  }

  function handleGoogle() {
    // TODO: wire up Google OAuth redirect
  }

  function handleGitHub() {
    // TODO: wire up GitHub OAuth redirect
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

              {/* Social step */}
              <Show when={step() === "social"}>
                <div class="forge-auth-btn-row">
                  <button class="forge-auth-social" onClick={handleGoogle}>
                    <GoogleIcon />
                    {isSignIn() ? "Continue with Google" : "Sign up with Google"}
                  </button>
                  <button class="forge-auth-social" onClick={handleGitHub}>
                    <GitHubIcon />
                    {isSignIn() ? "Continue with GitHub" : "Sign up with GitHub"}
                  </button>
                </div>

                <div class="forge-auth-divider">or</div>

                <button class="forge-auth-social" onClick={() => setStep("email")}>
                  <MailIcon />
                  Continue with email
                </button>
              </Show>

              {/* Email step */}
              <Show when={step() === "email"}>
                <button class="forge-auth-back-step" onClick={() => setStep("social")}>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round">
                    <path d="M19 12H5M12 5l-7 7 7 7" />
                  </svg>
                  All sign-in options
                </button>

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
              </Show>

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

function GoogleIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4" />
      <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853" />
      <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l3.66-2.84z" fill="#FBBC05" />
      <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335" />
    </svg>
  )
}

function GitHubIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z" />
    </svg>
  )
}

function MailIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <rect x="2" y="4" width="20" height="16" rx="2" />
      <path d="m22 7-10 7L2 7" />
    </svg>
  )
}

function SpinnerIcon() {
  return <span class="forge-css-spinner" style={{ width: "14px", height: "14px", "border-width": "2px" }} />
}
