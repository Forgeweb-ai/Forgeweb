/**
 * verify-email.tsx
 * =================
 * Post-signup "Check your inbox" screen.
 *
 * For now no real email is sent — this page exists so the auth flow has the
 * verification step baked in. Clicking "I've verified my email" hits
 * /api/auth/verify-email which flips `email_verified=true`, then we route
 * onward to the onboarding flow.
 *
 * Layout: centered card on a cream backdrop. Matches the Lovable reference
 * shape but uses the Forge cream/ink palette (no purple/pink gradient).
 *
 * Sits OUTSIDE AppShellProviders (it's a public-ish route — auth state lives
 * in localStorage), so it uses the public auth helpers from forge-api.tsx
 * rather than the useForgeApi() hook.
 */

import { createSignal, onMount, Show } from "solid-js"
import { useNavigate } from "@solidjs/router"
import {
  fetchCurrentUser,
  isAuthenticated,
  logout,
  postAuthDestination,
  verifyEmail,
  type CurrentUser,
} from "@/context/forge-api"

const STYLES = `
  .forge-verify-root {
    height: 100%;
    width: 100%;
    overflow-y: auto;
    background: var(--bg, #fffbf4);
    color: var(--ink, #15140f);
    font-family: 'Geist', system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 48px 20px;
  }
  .forge-verify-root * { box-sizing: border-box; }

  .forge-verify-card {
    width: 100%;
    max-width: 460px;
    display: flex;
    flex-direction: column;
    gap: 18px;
  }

  .forge-verify-logo {
    width: 44px; height: 44px;
    border-radius: 12px;
    background-image: url('/forge-f-light.png');
    background-size: cover;
    background-position: center;
    margin-bottom: 4px;
  }

  .forge-verify-h1 {
    font-size: 28px;
    line-height: 1.1;
    font-weight: 700;
    letter-spacing: -0.025em;
    margin: 0;
    color: var(--ink, #15140f);
  }
  .forge-verify-sub {
    font-size: 14px;
    line-height: 1.5;
    color: var(--muted, #8a8175);
    margin: 0 0 6px;
  }
  .forge-verify-sub b {
    color: var(--ink, #15140f);
    font-weight: 600;
  }

  .forge-verify-btn-row {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .forge-verify-btn {
    width: 100%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    padding: 12px 16px;
    border-radius: 10px;
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    font-family: inherit;
    transition: border-color .15s, background .15s, transform .1s, opacity .15s;
  }
  .forge-verify-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  .forge-verify-btn-secondary {
    background: #fff;
    border: 1px solid var(--line, #e6dfd0);
    color: var(--ink-2, #2b2a25);
  }
  .forge-verify-btn-secondary:not(:disabled):hover {
    border-color: var(--line-strong, #d4cab2);
    background: #fafaf6;
  }

  .forge-verify-btn-primary {
    background: var(--ink, #15140f);
    color: #fff;
    border: 0;
    font-weight: 600;
  }
  .forge-verify-btn-primary:not(:disabled):hover { transform: translateY(-1px); }

  .forge-verify-foot {
    margin-top: 4px;
    font-size: 13px;
    color: var(--muted, #8a8175);
    display: flex;
    gap: 10px;
    align-items: center;
  }
  .forge-verify-resend {
    background: none; border: 0; cursor: pointer; padding: 0;
    color: var(--ink, #15140f);
    font-weight: 600;
    font-family: inherit;
    font-size: 13px;
    text-decoration: underline;
    text-underline-offset: 3px;
  }
  .forge-verify-resend:disabled { color: var(--muted, #8a8175); cursor: default; text-decoration: none; }

  .forge-verify-err  { font-size: 13px; color: #c62828; margin: 0; }
  .forge-verify-note { font-size: 12px; color: var(--muted, #8a8175); margin: 0; }

  .forge-verify-signout {
    margin-top: 18px;
    padding-top: 18px;
    border-top: 1px solid var(--line, #e6dfd0);
    font-size: 12px;
    color: var(--muted, #8a8175);
  }
  .forge-verify-signout button {
    background: none; border: 0; cursor: pointer; padding: 0;
    color: var(--ink-2, #2b2a25);
    font-family: inherit;
    font-size: 12px;
    text-decoration: underline;
    text-underline-offset: 2px;
  }
`

export default function VerifyEmail() {
  const navigate = useNavigate()

  const [user, setUser]         = createSignal<CurrentUser | null>(null)
  const [error, setError]       = createSignal("")
  const [verifying, setVerifying] = createSignal(false)
  const [resendCooldown, setResendCooldown] = createSignal(0)

  onMount(async () => {
    // No token = nothing to verify. Bounce to sign-in.
    if (!isAuthenticated()) {
      navigate("/auth", { replace: true })
      return
    }
    const me = await fetchCurrentUser()
    if (!me) {
      // Token was rejected — clear and start over.
      logout()
      navigate("/auth", { replace: true })
      return
    }
    // Already verified? Skip ahead to wherever they belong.
    if (me.email_verified) {
      navigate(postAuthDestination(me), { replace: true })
      return
    }
    setUser(me)
  })

  async function handleVerified() {
    setError("")
    setVerifying(true)
    const result = await verifyEmail()
    setVerifying(false)
    if ("error" in result) {
      setError(result.error)
      return
    }
    navigate(postAuthDestination(result), { replace: true })
  }

  function handleOpenGmail() {
    // Opens Gmail in a new tab — best-effort, matches the reference design.
    window.open("https://mail.google.com/", "_blank", "noopener,noreferrer")
  }

  function handleResend() {
    // No real email sending yet — show a brief cooldown so the user gets
    // feedback. When SMTP is wired in, this becomes a POST to a /resend
    // endpoint.
    setResendCooldown(30)
    const id = setInterval(() => {
      setResendCooldown((s) => {
        if (s <= 1) { clearInterval(id); return 0 }
        return s - 1
      })
    }, 1000)
  }

  function handleSignOut() {
    logout()
    navigate("/auth", { replace: true })
  }

  return (
    <>
      <style innerHTML={STYLES} />
      <div class="forge-verify-root">
        <div class="forge-verify-card">
          <div class="forge-verify-logo" aria-hidden />

          <h1 class="forge-verify-h1">Check your inbox</h1>

          <p class="forge-verify-sub">
            <Show
              when={user()?.email}
              fallback="Click the link we sent to your email to finish your account setup."
            >
              Click the link we sent to <b>{user()!.email}</b> to finish your account setup.
            </Show>
          </p>

          <div class="forge-verify-btn-row">
            <button
              type="button"
              class="forge-verify-btn forge-verify-btn-secondary"
              onClick={handleOpenGmail}
            >
              <GoogleIcon />
              Open Gmail
            </button>

            <button
              type="button"
              class="forge-verify-btn forge-verify-btn-primary"
              disabled={verifying() || !user()}
              onClick={() => void handleVerified()}
            >
              <Show when={verifying()}><SpinnerIcon /></Show>
              {verifying() ? "Verifying…" : "I've verified my email"}
            </button>
          </div>

          <Show when={error()}>
            <p class="forge-verify-err">{error()}</p>
          </Show>

          <div class="forge-verify-foot">
            <span>Didn't receive an email?</span>
            <button
              class="forge-verify-resend"
              onClick={handleResend}
              disabled={resendCooldown() > 0}
            >
              {resendCooldown() > 0 ? `Resend in ${resendCooldown()}s` : "Resend"}
            </button>
          </div>

          <p class="forge-verify-note">
            Email delivery is being set up — for now, just click <b>I've verified my email</b> to continue.
          </p>

          <div class="forge-verify-signout">
            Not <Show when={user()?.email} fallback={<>this account</>}>{user()!.email}</Show>?{" "}
            <button onClick={handleSignOut}>Sign out</button>
          </div>
        </div>
      </div>
    </>
  )
}

// ── Icons (inlined to match auth.tsx style) ───────────────────────────────────

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

function SpinnerIcon() {
  return <span class="forge-css-spinner" style={{ width: "14px", height: "14px", "border-width": "2px" }} />
}
