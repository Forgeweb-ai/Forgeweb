/**
 * landing.tsx
 * ============
 * Forge public landing page.
 *
 * This page mirrors the "Meet Forge" reference HTML — it embeds the
 * markup, styles and animation script as-is and wires up navigation
 * on the primary CTAs.
 */

import { onCleanup, onMount } from "solid-js"
import { useNavigate } from "@solidjs/router"

// ── Raw CSS (lifted from the reference HTML) ──────────────────────────────────

const STYLES = `
  :root {
    /* base */
    --bg: #f7f3ea;
    --panel: #f1ece1;
    --surface-1: #ffffff;
    --surface-2: #fbf8f1;
    --surface-3: #faf6ec;

    --ink:    #15140f;
    --ink-2:  #2b2a25;
    --on-ink: #ffffff;
    --muted:  #8a8175;
    --muted-2: #6b6358;

    --line:   #e6dfd0;
    --line-2: #f0ead9;
    --line-3: #efe7d4;
    --line-strong: #d4cab2;

    --brand-1: oklch(0.78 0.15 55);
    --brand-2: oklch(0.66 0.19 32);
    --brand-3: oklch(0.58 0.21 18);

    --green:     oklch(0.55 0.14 145);
    --green-bg:  oklch(0.95 0.04 145);
    --green-bd:  oklch(0.85 0.07 145);
    --green-ink: oklch(0.36 0.10 145);

    --editor-bg: #15140f;
    --editor-fg: #c8c0a8;
    --code-c:    #6b6358;

    --shadow:    0 1px 2px rgba(40,30,15,.04), 0 8px 28px -8px rgba(40,30,15,.10);
    --shadow-md: 0 1px 0 rgba(0,0,0,.02), 0 14px 30px -10px rgba(40,30,15,.18);
    --shadow-lg: 0 1px 0 rgba(0,0,0,.02), 0 22px 50px -16px rgba(40,30,15,.22);

    color-scheme: light;
  }

  html[data-theme="dark"] {
    --bg:        #0f0e0a;
    --panel:     #1a1813;
    --surface-1: #1d1b15;
    --surface-2: #161410;
    --surface-3: #221f18;

    --ink:    #f5efe0;
    --ink-2:  #d8d0bd;
    --on-ink: #15140f;
    --muted:  #807968;
    --muted-2: #a39c8a;

    --line:   #2c2920;
    --line-2: #221f18;
    --line-3: #2a2720;
    --line-strong: #3b3729;

    --green-bg:  oklch(0.24 0.05 145);
    --green-bd:  oklch(0.35 0.08 145);
    --green-ink: oklch(0.82 0.13 145);

    --editor-bg: #0a0907;
    --editor-fg: #c8c0a8;
    --code-c:    #9a9385;

    --shadow:    0 1px 2px rgba(0,0,0,.4), 0 10px 30px -8px rgba(0,0,0,.55);
    --shadow-md: 0 1px 0 rgba(0,0,0,.3), 0 14px 30px -10px rgba(0,0,0,.6);
    --shadow-lg: 0 1px 0 rgba(0,0,0,.3), 0 22px 50px -16px rgba(0,0,0,.7);

    color-scheme: dark;
  }

  .forge-landing-root, .forge-landing-root *,
  .forge-landing-root .showcase, .forge-landing-root .prompt, .forge-landing-root .workspace,
  .forge-landing-root .ws-bar, .forge-landing-root .chat, .forge-landing-root .preview, .forge-landing-root .scene,
  .forge-landing-root .res-card, .forge-landing-root .map-node, .forge-landing-root .des-card, .forge-landing-root .tree,
  .forge-landing-root .build-bar, .forge-landing-root .pub-card, .forge-landing-root .sub-chip,
  .forge-landing-root .step h3, .forge-landing-root .step p, .forge-landing-root .step .tick, .forge-landing-root .step .tick .num,
  .forge-landing-root .header, .forge-landing-root .header .btn,
  .forge-landing-root .header .iconbtn, .forge-landing-root .cta-row .btn, .forge-landing-root .ws-status, .forge-landing-root .prompt-text, .forge-landing-root .msg .body,
  .forge-landing-root .msg .body .sub, .forge-landing-root .src, .forge-landing-root .corner, .forge-landing-root .editor, .forge-landing-root .scene-idle, .forge-landing-root .scene-h, .forge-landing-root .scene-h::after,
  .forge-landing-root .res-card .t, .forge-landing-root .res-card .u, .forge-landing-root .res-card .meta, .forge-landing-root .map-node .pill, .forge-landing-root .ln, .forge-landing-root .ln.dark,
  .forge-landing-root .tree .f::before, .forge-landing-root .build-bar .lbl, .forge-landing-root .pub-cta .url, .forge-landing-root .pub-cta .btn,
  .forge-landing-root .chat::before, .forge-landing-root .prompt, .forge-landing-root .prompt-row, .forge-landing-root .chip {
    transition-property: background-color, color, border-color, box-shadow;
    transition-duration: .3s;
    transition-timing-function: ease;
  }
  .forge-landing-root * { box-sizing: border-box; }
  .forge-landing-root {
    margin: 0; padding: 0; background: var(--bg); color: var(--ink);
    font-family: 'Geist', system-ui, sans-serif;
    font-feature-settings: "ss01", "cv11";
    -webkit-font-smoothing: antialiased;
    /* The app shell has body overflow:hidden + a fixed-height #root, so the
       landing page owns its own scroll. height:100% fills the shell and lets
       position:sticky on the header work against this scrolling container. */
    height: 100%;
    overflow-y: auto;
    overflow-x: hidden;
    overscroll-behavior: contain;
  }
  .forge-landing-root .mono { font-family: 'Geist Mono', ui-monospace, monospace; }

  .forge-landing-root .page { max-width: 1280px; margin: 0 auto; padding: 32px 56px 96px; }

  /* HEADER */
  .forge-landing-root .header {
    max-width: 1280px;
    margin: 0 auto;
    padding: 22px 56px;
    display: flex; align-items: center; justify-content: space-between;
    gap: 24px;
    position: sticky; top: 0; z-index: 50;
    background: rgba(247, 243, 234, 0.92);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border-bottom: 1px solid transparent;
    transition: border-color .25s ease, background .25s ease;
  }
  html[data-theme="dark"] .forge-landing-root .header {
    background: rgba(15, 14, 10, 0.92);
  }
  .forge-landing-root .header.scrolled { border-bottom-color: var(--line); }
  .forge-landing-root .logo {
    display: inline-flex; align-items: center; gap: 10px;
    text-decoration: none; color: var(--ink);
    font-weight: 700; font-size: 19px; letter-spacing: -0.02em;
  }
  .forge-landing-root .logo .mark {
    width: 28px; height: 28px; border-radius: 7px;
    background-image: url('/forge-f-light.png');
    background-size: cover;
    background-position: center;
    background-repeat: no-repeat;
  }
  html[data-theme="dark"] .forge-landing-root .logo .mark {
    background-image: url('/forge-f-dark.png');
  }
  .forge-landing-root .header-right { display: flex; align-items: center; gap: 4px; }
  .forge-landing-root .header .btn {
    border: 0; background: transparent;
    font: inherit; cursor: pointer;
    height: 38px; padding: 0 16px;
    border-radius: 10px;
    font-size: 14px; font-weight: 500;
    color: var(--ink);
    display: inline-flex; align-items: center; gap: 6px;
  }
  .forge-landing-root .header .btn.ghost:hover { background: color-mix(in oklch, var(--ink) 6%, transparent); }
  .forge-landing-root .header .btn.primary {
    background: var(--ink); color: var(--on-ink);
    box-shadow: 0 1px 0 rgba(255,255,255,.18) inset, 0 1px 2px rgba(0,0,0,.08);
    transition: transform .15s ease, background .2s;
  }
  .forge-landing-root .header .btn.primary:hover { transform: translateY(-1px); }
  .forge-landing-root .header .iconbtn {
    width: 38px; height: 38px; border-radius: 10px;
    border: 0; background: transparent;
    display: inline-grid; place-items: center;
    cursor: pointer;
    color: var(--ink);
    margin-right: 4px;
  }
  .forge-landing-root .header .iconbtn:hover { background: color-mix(in oklch, var(--ink) 6%, transparent); }
  .forge-landing-root .header .iconbtn svg { width: 18px; height: 18px; }
  .forge-landing-root .header .iconbtn .moon { display: none; }
  html[data-theme="dark"] .forge-landing-root .header .iconbtn .sun { display: none; }
  html[data-theme="dark"] .forge-landing-root .header .iconbtn .moon { display: block; }

  /* CTA row below title */
  .forge-landing-root .cta-row {
    display: flex; align-items: center; gap: 14px;
    margin: 0 0 72px;
    flex-wrap: wrap;
  }
  .forge-landing-root .cta-row .btn {
    border: 0; background: transparent;
    font: inherit; cursor: pointer;
    height: 50px; padding: 0 22px;
    border-radius: 12px;
    font-size: 16px; font-weight: 600;
    display: inline-flex; align-items: center; gap: 8px;
    letter-spacing: -0.005em;
  }
  .forge-landing-root .cta-row .btn.primary {
    background: var(--ink); color: var(--on-ink);
    box-shadow: 0 1px 0 rgba(255,255,255,.18) inset, 0 8px 22px -8px rgba(40,30,15,.35);
    transition: transform .15s ease, box-shadow .2s;
  }
  .forge-landing-root .cta-row .btn.primary:hover {
    transform: translateY(-1px);
    box-shadow: 0 1px 0 rgba(255,255,255,.18) inset, 0 12px 26px -8px rgba(40,30,15,.45);
  }
  .forge-landing-root .cta-row .btn.primary .arrow { width: 14px; height: 14px; transition: transform .2s ease; }
  .forge-landing-root .cta-row .btn.primary:hover .arrow { transform: translateX(3px); }
  .forge-landing-root .cta-row .btn.ghost {
    color: var(--ink);
    border: 1px solid var(--line);
    background: var(--surface-1);
  }
  .forge-landing-root .cta-row .btn.ghost:hover { background: var(--surface-2); }
  .forge-landing-root .cta-row .meta {
    margin-left: 6px;
    font-family: 'Geist Mono', monospace;
    font-size: 12px;
    color: var(--muted);
  }

  /* trust strip */
  .forge-landing-root .logos {
    display: flex; align-items: center; justify-content: center;
    gap: 64px; flex-wrap: wrap;
    color: var(--muted);
    padding-bottom: 64px;
    border-bottom: 1px solid var(--line);
    margin-bottom: 80px;
  }
  .forge-landing-root .logos .tag {
    width: 100%; text-align: center;
    font-size: 14px; letter-spacing: 0.02em;
    margin-bottom: 28px;
    color: #6b6358;
  }
  .forge-landing-root .logos .brand {
    font-weight: 700; font-size: 22px;
    opacity: .55; letter-spacing: -0.01em;
    transition: opacity .3s;
  }
  .forge-landing-root .logos .brand:hover { opacity: .9; }

  .forge-landing-root h1.title {
    font-size: 68px; line-height: 1.02; font-weight: 700;
    letter-spacing: -0.035em;
    margin: 56px 0 48px;
  }

  /* split */
  .forge-landing-root .split {
    display: grid;
    grid-template-columns: 1.32fr 1fr;
    gap: 72px;
    align-items: stretch;
  }

  /* SHOWCASE */
  .forge-landing-root .showcase {
    position: relative;
    aspect-ratio: 4 / 3;
    background: var(--panel);
    border-radius: 24px;
    overflow: hidden;
    box-shadow: var(--shadow);
  }
  .forge-landing-root .showcase::before {
    content: ''; position: absolute; inset: 0;
    background: radial-gradient(120% 80% at 50% -20%, rgba(255,255,255,.6), transparent 60%);
    pointer-events: none;
  }

  /* stage with two layers sharing one cell */
  .forge-landing-root .stage {
    position: absolute; inset: 0;
    display: grid;
    grid-template: 1fr / 1fr;
    place-items: center;
  }
  .forge-landing-root .stage > .prompt,
  .forge-landing-root .stage > .workspace { grid-area: 1 / 1; }

  /* PROMPT card */
  .forge-landing-root .prompt {
    width: 64%;
    max-width: 460px;
    background: var(--surface-1);
    border: 1px solid var(--line);
    border-radius: 18px;
    padding: 18px 18px 12px;
    box-shadow: var(--shadow-md);
    transition: opacity .5s ease, transform .6s cubic-bezier(.2,.7,.2,1);
    z-index: 2;
  }
  .forge-landing-root .prompt-text {
    min-height: 58px; font-size: 16px; line-height: 1.45;
    color: var(--ink); letter-spacing: -0.005em;
  }
  .forge-landing-root .prompt-text .ph { color: var(--muted); }
  .forge-landing-root .prompt-text .kw { color: var(--brand-3); }
  .forge-landing-root .caret {
    display: inline-block; width: 2px; height: 1.05em;
    background: var(--ink); vertical-align: -2px; margin-left: 1px;
    animation: blink 1s steps(2,end) infinite;
  }
  @keyframes blink { 50% { opacity: 0; } }
  .forge-landing-root .prompt-row {
    display: flex; align-items: center; gap: 10px;
    padding-top: 12px; border-top: 1px solid var(--line-2); margin-top: 8px;
  }
  .forge-landing-root .chip {
    display: inline-flex; align-items: center; gap: 6px;
    height: 28px; padding: 0 12px; border-radius: 999px;
    background: var(--surface-3); border: 1px solid var(--line-3);
    font-size: 12.5px; color: var(--muted-2);
  }
  .forge-landing-root .chip.icon { width: 28px; padding: 0; justify-content: center; }
  .forge-landing-root .submit {
    margin-left: auto;
    width: 32px; height: 32px; border-radius: 999px;
    display: grid; place-items: center;
    background: var(--ink); color: var(--on-ink);
    transition: transform .25s, background .25s;
  }
  .forge-landing-root .submit.fire {
    background: linear-gradient(135deg, var(--brand-1), var(--brand-3));
    transform: scale(1.08);
  }
  .forge-landing-root .submit svg { width: 14px; height: 14px; }

  /* WORKSPACE — chat + preview */
  .forge-landing-root .workspace {
    width: calc(100% - 36px);
    height: calc(100% - 36px);
    background: var(--surface-1);
    border-radius: 16px;
    border: 1px solid var(--line);
    box-shadow: var(--shadow-lg);
    display: flex; flex-direction: column;
    overflow: hidden;
    opacity: 0;
    transform: translateY(16px) scale(.97);
    transition: opacity .5s ease, transform .6s cubic-bezier(.2,.7,.2,1);
  }

  .forge-landing-root .ws-bar {
    display: flex; align-items: center; gap: 10px;
    padding: 9px 12px;
    border-bottom: 1px solid var(--line-2);
    background: var(--surface-2);
    font-size: 11px;
  }
  .forge-landing-root .dot { width: 9px; height: 9px; border-radius: 999px; background: var(--line); }
  .forge-landing-root .ws-title {
    display: inline-flex; align-items: center; gap: 8px;
    margin-left: 4px;
    font-family: 'Geist Mono', monospace; font-size: 11px;
    color: var(--muted-2);
  }
  .forge-landing-root .ws-title .glyph {
    width: 12px; height: 12px; border-radius: 3px;
    background: linear-gradient(135deg, var(--brand-1), var(--brand-3));
  }
  .forge-landing-root .ws-status {
    margin-left: auto;
    display: inline-flex; align-items: center; gap: 6px;
    font-family: 'Geist Mono', monospace; font-size: 10.5px;
    color: var(--muted-2);
    padding: 4px 8px; border-radius: 999px;
    background: var(--surface-1); border: 1px solid var(--line-2);
  }
  .forge-landing-root .ws-status .pulse {
    width: 6px; height: 6px; border-radius: 999px;
    background: var(--brand-2);
    box-shadow: 0 0 0 0 rgba(0,0,0,0);
    animation: pulse-fire 1.6s infinite;
  }
  .forge-landing-root .ws-status.live { background: var(--green-bg); border-color: var(--green-bd); color: var(--green-ink); }
  .forge-landing-root .ws-status.live .pulse {
    background: var(--green);
    animation: pulse-green 1.6s infinite;
  }
  @keyframes pulse-fire {
    0% { box-shadow: 0 0 0 0 oklch(0.66 0.19 32 / .55); }
    70% { box-shadow: 0 0 0 7px oklch(0.66 0.19 32 / 0); }
    100% { box-shadow: 0 0 0 0 oklch(0.66 0.19 32 / 0); }
  }
  @keyframes pulse-green {
    0% { box-shadow: 0 0 0 0 oklch(0.55 0.14 145 / .55); }
    70% { box-shadow: 0 0 0 7px oklch(0.55 0.14 145 / 0); }
    100% { box-shadow: 0 0 0 0 oklch(0.55 0.14 145 / 0); }
  }

  .forge-landing-root .ws-body {
    flex: 1; display: grid;
    grid-template-columns: 42% 58%;
    min-height: 0;
  }

  /* CHAT column */
  .forge-landing-root .chat {
    border-right: 1px solid var(--line-2);
    padding: 14px 14px 14px;
    overflow: hidden;
    display: flex; flex-direction: column;
    gap: 12px;
    background: var(--surface-2);
    position: relative;
  }
  .forge-landing-root .chat::before {
    content: ''; position: absolute; left: 0; right: 0; top: 0; height: 28px;
    background: linear-gradient(180deg, var(--surface-2), transparent);
    pointer-events: none; z-index: 2;
  }
  .forge-landing-root .chat-scroll {
    flex: 1; display: flex; flex-direction: column;
    justify-content: flex-end;
    gap: 10px;
    overflow: hidden;
    min-height: 0;
  }
  .forge-landing-root .msg {
    display: grid; grid-template-columns: 20px 1fr; gap: 10px;
    opacity: 0; transform: translateY(8px);
    animation: msgIn .45s cubic-bezier(.2,.7,.2,1) forwards;
  }
  @keyframes msgIn { to { opacity: 1; transform: none; } }
  .forge-landing-root .msg .ava {
    width: 20px; height: 20px; border-radius: 6px;
    margin-top: 1px;
    display: grid; place-items: center;
    font-size: 10px;
    font-family: 'Geist Mono', monospace;
  }
  .forge-landing-root .msg.user .ava { background: var(--ink); color: var(--on-ink); }
  .forge-landing-root .msg.agent .ava {
    background: linear-gradient(135deg, var(--brand-1), var(--brand-3));
    color: #fff;
  }
  .forge-landing-root .msg .body { font-size: 12.5px; line-height: 1.5; color: var(--ink-2); letter-spacing: -0.003em; }
  .forge-landing-root .msg.user .body { color: var(--ink); }
  .forge-landing-root .msg .body .tl { font-weight: 600; color: var(--ink); }
  .forge-landing-root .msg .body .sub { color: var(--muted-2); font-size: 12px; margin-top: 2px; }
  .forge-landing-root .msg .body .sub .kw { color: var(--brand-3); }

  /* compact (completed) message form */
  .forge-landing-root .msg.compact .body .sub,
  .forge-landing-root .msg.compact .body .sources,
  .forge-landing-root .msg.compact .body .plan { display: none; }
  .forge-landing-root .msg.compact .body .tl { font-weight: 500; color: var(--muted-2); font-size: 12px; display: inline-flex; align-items: center; gap: 6px; }
  .forge-landing-root .msg.compact .body .tl::before {
    content: '✓'; display: inline-grid; place-items: center;
    width: 12px; height: 12px; border-radius: 999px;
    background: var(--green); color: #fff;
    font-size: 9px; font-weight: 700;
  }
  .forge-landing-root .msg.compact { opacity: 0.85; }
  .forge-landing-root .msg.compact .ava { opacity: 0.6; }

  /* source chips inside research msg */
  .forge-landing-root .sources { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
  .forge-landing-root .src {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 3px 7px; border-radius: 6px;
    background: var(--surface-1); border: 1px solid var(--line-2);
    font-size: 10.5px; color: var(--muted-2);
    font-family: 'Geist Mono', monospace;
    opacity: 0; transform: translateY(4px);
    animation: srcIn .35s ease forwards;
  }
  .forge-landing-root .src .fav { width: 7px; height: 7px; border-radius: 2px; }
  @keyframes srcIn { to { opacity: 1; transform: none; } }

  /* plan checklist inside plan msg */
  .forge-landing-root .plan {
    margin-top: 8px;
    display: flex; flex-direction: column; gap: 5px;
  }
  .forge-landing-root .plan-item {
    display: grid; grid-template-columns: 14px 1fr; gap: 8px;
    font-size: 11.5px; color: var(--ink-2);
    align-items: center;
    opacity: 0; transform: translateX(-4px);
    animation: planIn .35s ease forwards;
  }
  .forge-landing-root .plan-item .box {
    width: 12px; height: 12px; border-radius: 4px;
    border: 1.5px solid var(--line-strong);
    display: grid; place-items: center;
    background: var(--surface-1);
    transition: background .25s, border-color .25s;
  }
  .forge-landing-root .plan-item.done .box {
    background: var(--ink); border-color: var(--ink);
  }
  .forge-landing-root .plan-item .box svg { width: 8px; height: 8px; color: var(--on-ink); opacity: 0; transition: opacity .2s; }
  .forge-landing-root .plan-item.done .box svg { opacity: 1; }
  @keyframes planIn { to { opacity: 1; transform: none; } }

  /* PREVIEW column scenes */
  .forge-landing-root .preview {
    position: relative;
    background: var(--surface-1);
    overflow: hidden;
  }
  .forge-landing-root .scene {
    position: absolute; inset: 0;
    padding: 16px;
    display: flex; flex-direction: column;
    gap: 10px;
    opacity: 0;
    transition: opacity .45s ease;
    pointer-events: none;
  }
  .forge-landing-root .scene.on { opacity: 1; pointer-events: auto; }
  .forge-landing-root .scene-h {
    font-family: 'Geist Mono', monospace;
    font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase;
    color: var(--muted);
    display: flex; align-items: center; gap: 8px;
  }
  .forge-landing-root .scene-h::after {
    content: ''; flex: 1; height: 1px; background: var(--line-2);
  }

  /* IDLE scene (before submit) */
  .forge-landing-root .scene-idle {
    background:
      radial-gradient(40% 50% at 30% 30%, rgba(255,255,255,.06), transparent 70%),
      linear-gradient(180deg, var(--surface-2), var(--surface-1));
    align-items: center; justify-content: center;
    color: var(--muted);
    font-family: 'Geist Mono', monospace; font-size: 11px;
  }

  /* RESEARCH scene — list of source cards popping in */
  .forge-landing-root .res-list {
    display: flex; flex-direction: column; gap: 8px;
    overflow: hidden;
  }
  .forge-landing-root .res-card {
    display: grid; grid-template-columns: 26px 1fr auto; gap: 10px;
    align-items: center;
    padding: 9px 11px;
    background: var(--surface-2); border: 1px solid var(--line-2);
    border-radius: 9px;
    opacity: 0; transform: translateY(6px);
  }
  .forge-landing-root .scene-research.on .res-card { animation: resIn .4s ease forwards; }
  .forge-landing-root .res-card .ico { width: 26px; height: 26px; border-radius: 6px; }
  .forge-landing-root .res-card .t { font-size: 12px; font-weight: 600; color: var(--ink); letter-spacing: -0.005em; }
  .forge-landing-root .res-card .u { font-size: 10.5px; color: var(--muted); font-family: 'Geist Mono', monospace; margin-top: 1px; }
  .forge-landing-root .res-card .meta { font-size: 10px; color: var(--muted); font-family: 'Geist Mono', monospace; }
  @keyframes resIn { to { opacity: 1; transform: none; } }

  /* PLAN scene — wireframe sitemap */
  .forge-landing-root .plan-map { flex: 1; display: grid; grid-template-rows: auto 1fr auto; gap: 10px; }
  .forge-landing-root .map-row { display: flex; gap: 8px; }
  .forge-landing-root .map-node {
    flex: 1; min-height: 36px;
    border: 1.5px dashed var(--line-strong);
    border-radius: 8px;
    background: var(--surface-2);
    display: flex; align-items: center; gap: 6px;
    padding: 0 10px;
    font-size: 11px; color: var(--ink-2); font-weight: 500;
    opacity: 0; transform: translateY(4px);
  }
  .forge-landing-root .scene-plan.on .map-node { animation: planNodeIn .4s ease forwards; }
  .forge-landing-root .map-node .pill {
    font-family: 'Geist Mono', monospace;
    font-size: 9px;
    padding: 2px 6px; border-radius: 4px;
    background: var(--surface-3); color: var(--muted-2);
  }
  .forge-landing-root .map-root { background: var(--surface-3); border-style: solid; border-color: var(--ink); border-width: 1.5px; color: var(--ink); font-weight: 600; }
  .forge-landing-root .map-root .pill { background: var(--ink); color: var(--on-ink); }
  .forge-landing-root .map-spine { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; align-content: start; }
  @keyframes planNodeIn { to { opacity: 1; transform: none; } }

  /* DESIGN scene — visual draft with placeholder hero + cards */
  .forge-landing-root .des-hero {
    height: 42%;
    border-radius: 10px;
    background: linear-gradient(135deg, var(--brand-1), var(--brand-2) 55%, var(--brand-3));
    position: relative; overflow: hidden;
    display: flex; align-items: flex-end; padding: 14px;
    opacity: 0; transform: scale(.97);
  }
  .forge-landing-root .scene-design.on .des-hero { animation: desIn .5s ease forwards; }
  .forge-landing-root .des-hero::after {
    content: ''; position: absolute; inset: 0;
    background:
      radial-gradient(55% 75% at 22% 22%, rgba(255,255,255,.35), transparent 60%),
      radial-gradient(40% 60% at 88% 90%, rgba(0,0,0,.18), transparent 60%);
  }
  .forge-landing-root .des-hero-t {
    position: relative; z-index: 1; color: #fff;
    font-size: 13px; font-weight: 600;
  }
  .forge-landing-root .des-hero-s {
    position: relative; z-index: 1; color: rgba(255,255,255,.85);
    font-size: 10.5px; margin-top: 2px;
  }
  .forge-landing-root .des-cards { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
  .forge-landing-root .des-card {
    background: var(--surface-2); border: 1px solid var(--line-2);
    border-radius: 8px; padding: 8px;
    display: flex; flex-direction: column; gap: 4px;
    opacity: 0; transform: translateY(6px);
  }
  .forge-landing-root .scene-design.on .des-card { animation: desIn .4s ease forwards; }
  .forge-landing-root .ln { height: 6px; border-radius: 4px; background: var(--line-3); }
  .forge-landing-root .ln.dark { background: var(--line-strong); width: 60%; }
  .forge-landing-root .ln.short { width: 40%; }
  .forge-landing-root .ln.long { width: 80%; }
  @keyframes desIn { to { opacity: 1; transform: none; } }

  .forge-landing-root .des-tokens { display: flex; gap: 6px; align-items: center; margin-top: 2px; }
  .forge-landing-root .swatch { width: 16px; height: 16px; border-radius: 5px; border: 1px solid rgba(0,0,0,.05); opacity: 0; }
  .forge-landing-root .scene-design.on .swatch { animation: desIn .4s ease forwards; }
  .forge-landing-root .des-tokens .lbl { font-family: 'Geist Mono', monospace; font-size: 9.5px; color: var(--muted); margin-left: 4px; }

  /* BUILD scene — code editor + progress */
  .forge-landing-root .build-grid {
    flex: 1; display: grid;
    grid-template-columns: 110px 1fr;
    gap: 10px; min-height: 0;
  }
  .forge-landing-root .tree {
    background: var(--surface-2); border: 1px solid var(--line-2);
    border-radius: 8px; padding: 8px;
    font-family: 'Geist Mono', monospace; font-size: 10px;
    display: flex; flex-direction: column; gap: 4px;
    overflow: hidden;
  }
  .forge-landing-root .tree .f { color: var(--ink-2); display: flex; align-items: center; gap: 5px; opacity: 0; transform: translateX(-4px); }
  .forge-landing-root .scene-build.on .tree .f { animation: planIn .35s ease forwards; }
  .forge-landing-root .tree .f.dim { color: var(--muted); }
  .forge-landing-root .tree .f::before { content: ''; width: 5px; height: 5px; border-radius: 999px; background: var(--line-strong); flex: none; }
  .forge-landing-root .tree .f.active::before { background: var(--brand-2); }

  .forge-landing-root .editor {
    background: var(--editor-bg); border-radius: 8px;
    padding: 10px 12px;
    font-family: 'Geist Mono', monospace; font-size: 10.5px;
    color: var(--editor-fg);
    overflow: hidden;
    display: flex; flex-direction: column; gap: 3px;
    min-height: 0;
  }
  .forge-landing-root .code { white-space: nowrap; opacity: 0; transform: translateY(4px); }
  .forge-landing-root .scene-build.on .code { animation: planIn .3s ease forwards; }
  .forge-landing-root .code .k { color: oklch(0.78 0.15 280); }
  .forge-landing-root .code .s { color: oklch(0.78 0.13 95); }
  .forge-landing-root .code .t { color: oklch(0.74 0.14 160); }
  .forge-landing-root .code .c { color: var(--code-c); }

  .forge-landing-root .build-bar {
    height: 22px; border-radius: 6px;
    background: var(--surface-2); border: 1px solid var(--line-2);
    overflow: hidden; position: relative;
    flex: none;
  }
  .forge-landing-root .build-bar .fill {
    position: absolute; inset: 0;
    background: linear-gradient(90deg, var(--brand-1), var(--brand-3));
    width: 0;
    transition: width .25s linear;
  }
  .forge-landing-root .build-bar .lbl {
    position: relative; z-index: 1;
    font-family: 'Geist Mono', monospace; font-size: 10px;
    color: var(--ink);
    line-height: 22px; padding: 0 8px;
    display: flex; justify-content: space-between;
  }
  .forge-landing-root .build-bar .lbl span:last-child { color: var(--muted-2); }

  /* PUBLISH scene — finished UI */
  .forge-landing-root .pub {
    flex: 1; display: flex; flex-direction: column; gap: 10px;
  }
  .forge-landing-root .pub-hero {
    height: 44%;
    border-radius: 10px;
    background: linear-gradient(135deg, var(--brand-1), var(--brand-2) 55%, var(--brand-3));
    position: relative; overflow: hidden;
    display: flex; align-items: flex-end; padding: 14px;
  }
  .forge-landing-root .pub-hero::after {
    content: ''; position: absolute; inset: 0;
    background:
      radial-gradient(55% 75% at 22% 22%, rgba(255,255,255,.35), transparent 60%),
      radial-gradient(40% 60% at 88% 90%, rgba(0,0,0,.18), transparent 60%);
  }
  .forge-landing-root .pub-hero .h { position: relative; z-index: 1; color: #fff; font-size: 13px; font-weight: 600; }
  .forge-landing-root .pub-hero .s { position: relative; z-index: 1; color: rgba(255,255,255,.85); font-size: 10.5px; margin-top: 2px; }
  .forge-landing-root .pub-row { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; height: 22%; }
  .forge-landing-root .pub-card { background: var(--surface-2); border: 1px solid var(--line-2); border-radius: 8px; padding: 8px; display: flex; flex-direction: column; gap: 4px; }
  .forge-landing-root .pub-cta { display: flex; gap: 8px; align-items: center; }
  .forge-landing-root .pub-cta .btn { height: 28px; padding: 0 14px; border-radius: 7px; font-size: 11px; font-weight: 500; display: inline-flex; align-items: center; }
  .forge-landing-root .pub-cta .btn.solid { background: var(--ink); color: var(--on-ink); }
  .forge-landing-root .pub-cta .btn.ghost { background: transparent; color: var(--ink); border: 1px solid var(--line); }
  .forge-landing-root .pub-cta .url { margin-left: auto; font-family: 'Geist Mono', monospace; font-size: 10.5px; color: var(--green-ink); }

  /* phase visibility */
  .forge-landing-root .stage[data-phase="idle"]    .prompt    { opacity: 1; transform: scale(1); }
  .forge-landing-root .stage[data-phase="typing"]  .prompt    { opacity: 1; transform: scale(1); }
  .forge-landing-root .stage[data-phase="submit"]  .prompt    { opacity: 0; transform: translateY(-12px) scale(.96); pointer-events: none; }
  .forge-landing-root .stage:not([data-phase="idle"]):not([data-phase="typing"]) .prompt { opacity: 0; transform: translateY(-12px) scale(.96); pointer-events: none; }

  .forge-landing-root .stage[data-phase="idle"]   .workspace { opacity: 0; transform: translateY(16px) scale(.97); pointer-events: none; }
  .forge-landing-root .stage[data-phase="typing"] .workspace { opacity: 0; transform: translateY(16px) scale(.97); pointer-events: none; }
  .forge-landing-root .stage:not([data-phase="idle"]):not([data-phase="typing"]) .workspace { opacity: 1; transform: none; }

  /* progress pips — hidden but still wired up under the hood */
  .forge-landing-root .progress { display: none; }

  .forge-landing-root .corner {
    position: absolute; left: 18px; top: 16px;
    display: inline-flex; align-items: center; gap: 8px;
    font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase;
    color: var(--muted); font-family: 'Geist Mono', monospace;
    z-index: 4;
  }
  .forge-landing-root .corner .glyph {
    width: 14px; height: 14px; border-radius: 4px;
    background: linear-gradient(135deg, var(--brand-1), var(--brand-3));
  }
  .forge-landing-root .corner { transition: opacity .3s ease; }
  .forge-landing-root .corner.hidden { opacity: 0; pointer-events: none; }

  /* RIGHT column steps — distributes across full showcase height */
  .forge-landing-root .steps {
    display: flex; flex-direction: column;
    justify-content: space-between;
    height: 100%;
    padding: 8px 0;
  }
  .forge-landing-root .step { cursor: pointer; transition: opacity .35s ease; }
  .forge-landing-root .step h3 {
    font-size: 30px; font-weight: 700; letter-spacing: -0.025em;
    margin: 0 0 10px; color: var(--muted);
    transition: color .35s ease;
  }
  .forge-landing-root .step p {
    margin: 0; font-size: 15.5px; line-height: 1.5;
    color: var(--muted); max-width: 32ch;
    transition: color .35s ease;
  }
  .forge-landing-root .step.active h3 { color: var(--ink); }
  .forge-landing-root .step.active p { color: var(--ink-2); }
  .forge-landing-root .step .tick {
    display: inline-flex; align-items: center; gap: 8px;
    margin-bottom: 6px;
    font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase;
    color: var(--muted);
    font-family: 'Geist Mono', monospace;
  }
  .forge-landing-root .step.active .tick { color: var(--brand-3); }
  .forge-landing-root .step .tick .num {
    width: 18px; height: 18px; border-radius: 5px;
    background: var(--line);
    color: var(--muted);
    display: inline-grid; place-items: center;
    font-size: 10px;
    transition: background .35s, color .35s;
  }
  .forge-landing-root .step.active .tick .num { background: var(--ink); color: var(--on-ink); }

  /* sub-status under step 2 */
  .forge-landing-root .sub-status {
    display: flex; flex-wrap: wrap; gap: 6px;
    margin-top: 12px;
    max-height: 0; overflow: hidden;
    transition: max-height .4s ease;
  }
  .forge-landing-root .step.active .sub-status { max-height: 80px; }
  .forge-landing-root .sub-chip {
    font-family: 'Geist Mono', monospace;
    font-size: 11px;
    padding: 4px 10px;
    border-radius: 999px;
    background: var(--surface-1);
    border: 1px solid var(--line);
    color: var(--muted);
    transition: background .25s, color .25s, border-color .25s, transform .25s;
  }
  .forge-landing-root .sub-chip.active {
    background: var(--ink); color: var(--on-ink); border-color: var(--ink);
    transform: translateY(-1px);
  }
  .forge-landing-root .sub-chip.done {
    background: var(--surface-3); color: var(--muted-2); border-color: var(--line-2);
  }
  .forge-landing-root .sub-chip.done::before {
    content: '✓ ';
    color: var(--green-ink);
  }

  .forge-landing-root .hint {
    margin-top: 40px;
    font-family: 'Geist Mono', monospace;
    font-size: 12px; color: #8a8175;
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  }
  .forge-landing-root .hint kbd {
    display: inline-block; padding: 2px 7px; border-radius: 5px;
    background: #fff; border: 1px solid var(--line);
    font-family: inherit; font-size: 11px; color: var(--ink);
  }

  /* ── Tablet (≤ 900px) ───────────────────────────────────────────────── */
  @media (max-width: 900px) {
    .forge-landing-root .header { padding: 16px 24px; }
    .forge-landing-root .page { padding: 24px 24px 72px; }
    .forge-landing-root h1.title { font-size: 52px; margin: 32px 0 28px; }
    .forge-landing-root .cta-row { margin: 0 0 48px; gap: 10px; }
    .forge-landing-root .cta-row .btn { height: 46px; padding: 0 18px; font-size: 15px; }
    .forge-landing-root .cta-row .meta { width: 100%; margin-left: 0; margin-top: 4px; }

    .forge-landing-root .split {
      grid-template-columns: 1fr;
      gap: 40px;
      align-items: start;
    }
    .forge-landing-root .steps {
      height: auto;
      padding: 0;
      gap: 32px;
    }
  }

  /* ── Phone (≤ 560px) ────────────────────────────────────────────────── */
  @media (max-width: 560px) {
    .forge-landing-root .header { padding: 14px 18px; gap: 12px; }
    .forge-landing-root .header .btn { height: 36px; padding: 0 12px; font-size: 13px; }
    .forge-landing-root .header .btn.ghost { display: none; }
    .forge-landing-root .header .iconbtn { width: 34px; height: 34px; }
    .forge-landing-root .logo { font-size: 17px; }
    .forge-landing-root .logo .mark { width: 24px; height: 24px; border-radius: 6px; }

    .forge-landing-root .page { padding: 16px 18px 56px; }

    .forge-landing-root h1.title {
      font-size: 38px;
      margin: 20px 0 24px;
    }

    .forge-landing-root .cta-row { margin: 0 0 36px; gap: 8px; }
    .forge-landing-root .cta-row .btn { height: 44px; padding: 0 16px; font-size: 14px; flex: 1 1 auto; justify-content: center; }
    .forge-landing-root .cta-row .meta { font-size: 11px; }

    .forge-landing-root .split { gap: 32px; }
    .forge-landing-root .showcase { border-radius: 18px; }

    /* tighten showcase internals so 4:3 stays readable on small screens */
    .forge-landing-root .prompt { width: 80%; padding: 14px 14px 10px; }
    .forge-landing-root .prompt-text { font-size: 14px; min-height: 50px; }
    .forge-landing-root .chip { height: 24px; padding: 0 10px; font-size: 11px; }
    .forge-landing-root .chip.icon { width: 24px; padding: 0; }
    .forge-landing-root .submit { width: 28px; height: 28px; }

    .forge-landing-root .ws-body { grid-template-columns: 50% 50%; }
    .forge-landing-root .ws-bar { padding: 7px 10px; }
    .forge-landing-root .ws-title { display: none; }
    .forge-landing-root .chat { padding: 10px; gap: 8px; }
    .forge-landing-root .msg .body { font-size: 11.5px; }
    .forge-landing-root .scene { padding: 12px; gap: 8px; }
    .forge-landing-root .build-grid { grid-template-columns: 80px 1fr; }

    /* steps */
    .forge-landing-root .steps { gap: 28px; padding: 0; }
    .forge-landing-root .step h3 { font-size: 24px; }
    .forge-landing-root .step p { font-size: 14.5px; }
    .forge-landing-root .sub-chip { font-size: 10.5px; padding: 4px 9px; }
  }

  /* ── Very small phones (≤ 380px) ─────────────────────────────────────── */
  @media (max-width: 380px) {
    .forge-landing-root h1.title { font-size: 34px; }
    .forge-landing-root .cta-row .btn { font-size: 13.5px; padding: 0 14px; }
    .forge-landing-root .step h3 { font-size: 22px; }
  }
`

// ── Markup (the body content, minus the <script> blocks) ─────────────────────

const MARKUP = `
<header class="header" data-forge-header>
  <a class="logo" href="#">
    <span class="mark"></span>
    <span>Forge</span>
  </a>
  <div class="header-right">
    <button class="iconbtn" data-forge-theme-btn aria-label="Toggle theme" type="button">
      <svg class="sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>
      <svg class="moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
    </button>
    <button class="btn ghost" type="button" data-forge-auth>Log in</button>
    <button class="btn primary" type="button" data-forge-auth>Get started</button>
  </div>
</header>

<div class="page">

  <h1 class="title">Meet Forge</h1>

  <div class="cta-row">
    <button class="btn primary" type="button" data-forge-auth>
      Get started
      <svg class="arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 5l7 7-7 7"/></svg>
    </button>
    <button class="btn ghost" type="button">See how it works</button>
    <span class="meta">Free to try · No credit card</span>
  </div>

  <div class="split">

    <!-- LEFT: animated showcase -->
    <div class="showcase">
      <div class="corner">
        <span class="glyph"></span>
        <span data-forge-phase-label>idle</span>
      </div>

      <div class="stage" data-forge-stage data-phase="idle">

        <!-- PROMPT (only visible idle/typing) -->
        <div class="prompt">
          <div class="prompt-text" data-forge-prompt-text><span class="ph">Describe what you want to build…</span><span class="caret"></span></div>
          <div class="prompt-row">
            <span class="chip icon" title="add">＋</span>
            <span class="chip">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5-5 5 5"/><path d="M12 5v12"/></svg>
              Attach
            </span>
            <span class="chip">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>
              Public
            </span>
            <span class="submit" data-forge-submit>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>
            </span>
          </div>
        </div>

        <!-- WORKSPACE (visible from submit onwards) -->
        <div class="workspace">
          <div class="ws-bar">
            <span class="dot"></span><span class="dot"></span><span class="dot"></span>
            <span class="ws-title"><span class="glyph"></span> forge / feedback-tool</span>
            <span class="ws-status" data-forge-ws-status><span class="pulse"></span><span data-forge-ws-status-text>starting</span></span>
          </div>
          <div class="ws-body">

            <div class="chat">
              <div class="chat-scroll" data-forge-chat></div>
            </div>

            <div class="preview" data-forge-preview>

              <div class="scene scene-idle on">
                <span>○ preview ready · awaiting prompt</span>
              </div>

              <!-- RESEARCH -->
              <div class="scene scene-research">
                <div class="scene-h">research · references</div>
                <div class="res-list" data-forge-res-list></div>
              </div>

              <!-- PLAN -->
              <div class="scene scene-plan">
                <div class="scene-h">plan · information architecture</div>
                <div class="plan-map">
                  <div class="map-row">
                    <div class="map-node map-root" style="animation-delay:.05s"><span class="pill">root</span> /app</div>
                  </div>
                  <div class="map-spine">
                    <div class="map-node" style="animation-delay:.30s"><span class="pill">view</span> Inbox</div>
                    <div class="map-node" style="animation-delay:.55s"><span class="pill">view</span> Insights</div>
                    <div class="map-node" style="animation-delay:.80s"><span class="pill">view</span> Reports</div>
                    <div class="map-node" style="animation-delay:1.05s"><span class="pill">model</span> Thread</div>
                    <div class="map-node" style="animation-delay:1.30s"><span class="pill">model</span> Tag</div>
                    <div class="map-node" style="animation-delay:1.55s"><span class="pill">api</span> /digest</div>
                  </div>
                  <div class="map-row">
                    <div class="map-node" style="animation-delay:1.85s"><span class="pill">auth</span> SSO + roles</div>
                  </div>
                </div>
              </div>

              <!-- DESIGN -->
              <div class="scene scene-design">
                <div class="scene-h">design · draft v1</div>
                <div class="des-hero" style="animation-delay:.05s">
                  <div>
                    <div class="des-hero-t">Feedback, in one inbox.</div>
                    <div class="des-hero-s">Capture, triage, ship — together.</div>
                  </div>
                </div>
                <div class="des-cards">
                  <div class="des-card" style="animation-delay:.30s"><span class="ln dark short"></span><span class="ln long"></span><span class="ln"></span></div>
                  <div class="des-card" style="animation-delay:.50s"><span class="ln dark short"></span><span class="ln long"></span><span class="ln"></span></div>
                  <div class="des-card" style="animation-delay:.70s"><span class="ln dark short"></span><span class="ln long"></span><span class="ln"></span></div>
                </div>
                <div class="des-tokens">
                  <span class="swatch" style="background: var(--brand-1); animation-delay:.95s"></span>
                  <span class="swatch" style="background: var(--brand-2); animation-delay:1.05s"></span>
                  <span class="swatch" style="background: var(--brand-3); animation-delay:1.15s"></span>
                  <span class="swatch" style="background: var(--ink); animation-delay:1.25s"></span>
                  <span class="lbl">tokens · 4 / 4</span>
                </div>
              </div>

              <!-- BUILD -->
              <div class="scene scene-build">
                <div class="scene-h">build · compiling</div>
                <div class="build-grid">
                  <div class="tree">
                    <span class="f"   style="animation-delay:.05s">app.tsx</span>
                    <span class="f"   style="animation-delay:.20s">layout.tsx</span>
                    <span class="f"   style="animation-delay:.35s">inbox.tsx</span>
                    <span class="f active" style="animation-delay:.50s">thread.tsx</span>
                    <span class="f"   style="animation-delay:.65s">insights.tsx</span>
                    <span class="f dim" style="animation-delay:.80s">api.ts</span>
                    <span class="f dim" style="animation-delay:.95s">schema.ts</span>
                    <span class="f dim" style="animation-delay:1.10s">theme.css</span>
                  </div>
                  <div class="editor">
                    <div class="code" style="animation-delay:.10s"><span class="c">// thread.tsx</span></div>
                    <div class="code" style="animation-delay:.25s"><span class="k">export function</span> <span class="t">Thread</span>({ id }) {</div>
                    <div class="code" style="animation-delay:.40s">&nbsp;&nbsp;<span class="k">const</span> data = useThread(id);</div>
                    <div class="code" style="animation-delay:.55s">&nbsp;&nbsp;<span class="k">return</span> (</div>
                    <div class="code" style="animation-delay:.70s">&nbsp;&nbsp;&nbsp;&nbsp;&lt;<span class="t">Panel</span> title=<span class="s">"Thread"</span>&gt;</div>
                    <div class="code" style="animation-delay:.85s">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{data.messages.<span class="t">map</span>(m =&gt;</div>
                    <div class="code" style="animation-delay:1.00s">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&lt;<span class="t">Message</span> {...m} /&gt;)}</div>
                    <div class="code" style="animation-delay:1.15s">&nbsp;&nbsp;&nbsp;&nbsp;&lt;/<span class="t">Panel</span>&gt;</div>
                    <div class="code" style="animation-delay:1.30s">&nbsp;&nbsp;);</div>
                    <div class="code" style="animation-delay:1.45s">}</div>
                  </div>
                </div>
                <div class="build-bar">
                  <div class="fill" data-forge-build-fill></div>
                  <div class="lbl"><span>compiling</span><span data-forge-build-pct>0%</span></div>
                </div>
              </div>

              <!-- PUBLISH -->
              <div class="scene scene-publish">
                <div class="scene-h">live · published</div>
                <div class="pub">
                  <div class="pub-hero">
                    <div>
                      <div class="h">Feedback, in one inbox.</div>
                      <div class="s">Capture, triage, ship — together.</div>
                    </div>
                  </div>
                  <div class="pub-row">
                    <div class="pub-card"><span class="ln dark short"></span><span class="ln long"></span><span class="ln"></span></div>
                    <div class="pub-card"><span class="ln dark short"></span><span class="ln long"></span><span class="ln"></span></div>
                    <div class="pub-card"><span class="ln dark short"></span><span class="ln long"></span><span class="ln"></span></div>
                  </div>
                  <div class="pub-cta">
                    <span class="btn solid">Get started</span>
                    <span class="btn ghost">View demo</span>
                    <span class="url">forge.app/p-94b2</span>
                  </div>
                </div>
              </div>

            </div>
          </div>
        </div>
      </div>

      <div class="progress" data-forge-pips></div>
    </div>

    <!-- RIGHT: 3 steps -->
    <div class="steps" data-forge-steps>
      <div class="step" data-step="0">
        <div class="tick"><span class="num">1</span> Step one</div>
        <h3>Start with an idea</h3>
        <p>Describe the app or site you want to build, or drop in screenshots and docs.</p>
      </div>
      <div class="step" data-step="1">
        <div class="tick"><span class="num">2</span> Step two</div>
        <h3>Watch it come to life</h3>
        <p>Forge researches, plans, designs, and builds — you watch the prototype assemble in real time.</p>
        <div class="sub-status" data-forge-sub-status>
          <span class="sub-chip" data-sub="research">Research</span>
          <span class="sub-chip" data-sub="plan">Plan</span>
          <span class="sub-chip" data-sub="design">Design</span>
          <span class="sub-chip" data-sub="build">Build</span>
        </div>
      </div>
      <div class="step" data-step="2">
        <div class="tick"><span class="num">3</span> Step three</div>
        <h3>Refine and publish</h3>
        <p>Iterate with simple feedback, then deploy to the world with one click.</p>
      </div>
    </div>

  </div>

</div>
`

// ── Animation / phase script ─────────────────────────────────────────────────
// Lifted from the reference HTML, scoped to a root element, and exposes a
// cleanup function the SolidJS component can call on unmount.

function startForgeAnimations(root: HTMLElement): () => void {
  const cleanups: Array<() => void> = []

  // theme toggle
  const themeBtn = root.querySelector<HTMLButtonElement>("[data-forge-theme-btn]")
  if (themeBtn) {
    const onClick = () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark"
      document.documentElement.dataset.theme = next
      try { localStorage.setItem("forge.theme", next) } catch {}
    }
    themeBtn.addEventListener("click", onClick)
    cleanups.push(() => themeBtn.removeEventListener("click", onClick))
  }

  // header scroll state — the landing root is the scrolling container
  // (body has overflow:hidden in the app shell), so we listen on root.
  const hdr = root.querySelector<HTMLElement>("[data-forge-header]")
  if (hdr) {
    const onScroll = () => hdr.classList.toggle("scrolled", root.scrollTop > 6)
    onScroll()
    root.addEventListener("scroll", onScroll, { passive: true })
    cleanups.push(() => root.removeEventListener("scroll", onScroll))
  }

  // showcase animation
  const stage = root.querySelector<HTMLElement>("[data-forge-stage]")!
  const corner = root.querySelector<HTMLElement>(".showcase .corner")!
  const promptText = root.querySelector<HTMLElement>("[data-forge-prompt-text]")!
  const submit = root.querySelector<HTMLElement>("[data-forge-submit]")!
  const phaseLabel = root.querySelector<HTMLElement>("[data-forge-phase-label]")!
  const wsStatus = root.querySelector<HTMLElement>("[data-forge-ws-status]")!
  const wsStatusText = root.querySelector<HTMLElement>("[data-forge-ws-status-text]")!
  const stepEls = [...root.querySelectorAll<HTMLElement>("[data-forge-steps] .step")]
  const pipsEl = root.querySelector<HTMLElement>("[data-forge-pips]")!
  const subChips = [...root.querySelectorAll<HTMLElement>("[data-forge-sub-status] .sub-chip")]
  const chat = root.querySelector<HTMLElement>("[data-forge-chat]")!
  const preview = root.querySelector<HTMLElement>("[data-forge-preview]")!
  const buildFill = root.querySelector<HTMLElement>("[data-forge-build-fill]")!
  const buildPct = root.querySelector<HTMLElement>("[data-forge-build-pct]")!
  const resList = root.querySelector<HTMLElement>("[data-forge-res-list]")!

  type Phase = {
    key: string
    label: string
    step: number
    sub: string | null
    dwell: number
  }
  const PHASES: Phase[] = [
    { key: "idle",     label: "idle",             step: 0, sub: null,       dwell: 1200 },
    { key: "typing",   label: "composing prompt", step: 0, sub: null,       dwell: 3400 },
    { key: "submit",   label: "submitting",       step: 1, sub: null,       dwell: 800  },
    { key: "research", label: "researching",      step: 1, sub: "research", dwell: 3400 },
    { key: "plan",     label: "planning",         step: 1, sub: "plan",     dwell: 3400 },
    { key: "design",   label: "designing",        step: 1, sub: "design",   dwell: 3400 },
    { key: "build",    label: "building",         step: 1, sub: "build",    dwell: 3800 },
    { key: "publish",  label: "live · deployed",  step: 2, sub: null,       dwell: 3000 },
  ]
  const SUB_ORDER = ["research", "plan", "design", "build"]

  // pips
  PHASES.forEach((p, i) => {
    const d = document.createElement("span")
    d.className = "pdot"
    d.dataset.i = String(i)
    d.title = p.label
    d.addEventListener("click", () => { setPhase(i); reschedule() })
    pipsEl.appendChild(d)
  })
  const pips = [...pipsEl.children] as HTMLElement[]

  // prompt
  const SCRIPT = "Build a customer "
  const KW = "feedback tool"
  const TAIL = " with AI-powered analytics"

  function setPromptEmpty() {
    promptText.innerHTML = '<span class="ph">Describe what you want to build…</span><span class="caret"></span>'
  }
  function setPromptFull() {
    promptText.innerHTML = 'Build a customer <span class="kw">feedback tool</span> with AI-powered analytics'
  }
  let typingTimer: ReturnType<typeof setTimeout> | null = null
  function startTyping() {
    if (typingTimer) clearTimeout(typingTimer)
    promptText.innerHTML = '<span class="caret"></span>'
    const full = SCRIPT + KW + TAIL
    const kwStart = SCRIPT.length, kwEnd = SCRIPT.length + KW.length
    let i = 0
    function step() {
      if (currentPhase !== 1) return
      i++
      const pre = full.slice(0, Math.min(i, kwStart))
      const mid = full.slice(kwStart, Math.min(i, kwEnd))
      const post = full.slice(kwEnd, i)
      promptText.innerHTML =
        esc(pre) +
        (mid ? '<span class="kw">' + esc(mid) + '</span>' : '') +
        (post ? esc(post) : '') +
        '<span class="caret"></span>'
      if (i < full.length) {
        const ch = full[i - 1]
        const d = ch === " " ? 70 : (Math.random() < 0.08 ? 130 : 36 + Math.random() * 38)
        typingTimer = setTimeout(step, d)
      }
    }
    step()
  }
  function esc(s: string) {
    return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]!))
  }

  // chat
  function clearChat() { chat.innerHTML = "" }
  function addMsg(role: "user" | "agent", html: string, delay = 0) {
    const m = document.createElement("div")
    m.className = "msg " + role
    m.style.animationDelay = delay + "ms"
    const ava = role === "user" ? "me" : "F"
    m.innerHTML = `<div class="ava">${ava}</div><div class="body">${html}</div>`
    chat.appendChild(m)
    return m
  }

  // scenes
  const scenes: Record<string, HTMLElement> = {
    idle:     preview.querySelector(".scene-idle")!,
    research: preview.querySelector(".scene-research")!,
    plan:     preview.querySelector(".scene-plan")!,
    design:   preview.querySelector(".scene-design")!,
    build:    preview.querySelector(".scene-build")!,
    publish:  preview.querySelector(".scene-publish")!,
  }
  function showScene(name: string) {
    for (const k of Object.keys(scenes)) scenes[k].classList.toggle("on", k === name)
  }

  const SOURCES = [
    { t: "Feedback inbox · anatomy",  u: "practice.design/inbox", c: "#7aa6ff", meta: "4 min" },
    { t: "Tagging taxonomies",        u: "support-handbook.io",   c: "#ff9a5a", meta: "6 min" },
    { t: "Triage flows · case study", u: "ux-quarterly.dev",      c: "#b07cff", meta: "8 min" },
    { t: "Sentiment benchmarks",      u: "research.note/2403",    c: "#5ec99a", meta: "paper" },
    { t: "Onboarding examples",       u: "field-notes.co",        c: "#e85a8a", meta: "12 ex" },
  ]
  function renderResearch() {
    resList.innerHTML = ""
    SOURCES.forEach((s, i) => {
      const c = document.createElement("div")
      c.className = "res-card"
      c.style.animationDelay = (i * 280 + 80) + "ms"
      c.innerHTML = `
        <span class="ico" style="background: linear-gradient(135deg, ${s.c}, ${s.c}cc)"></span>
        <span><span class="t">${s.t}</span><div class="u">${s.u}</div></span>
        <span class="meta">${s.meta}</span>`
      resList.appendChild(c)
    })
  }

  // build progress
  let buildAnim: number | null = null
  function startBuild() {
    if (buildAnim) cancelAnimationFrame(buildAnim)
    const start = performance.now()
    const duration = PHASES[6].dwell - 400
    function tick(t: number) {
      const k = Math.min(1, (t - start) / duration)
      const pct = Math.round(k * 100)
      buildFill.style.width = pct + "%"
      buildPct.textContent = pct + "%"
      if (k < 1 && currentPhase === 6) buildAnim = requestAnimationFrame(tick)
    }
    buildAnim = requestAnimationFrame(tick)
  }
  function resetBuild() {
    if (buildAnim) cancelAnimationFrame(buildAnim)
    buildFill.style.width = "0%"
    buildPct.textContent = "0%"
  }

  function rerunScene(scene: HTMLElement) {
    scene.classList.remove("on")
    void scene.offsetWidth
    scene.classList.add("on")
  }

  const AGENT_MSGS: Record<string, { title: string; done: string; body: string }> = {
    research: {
      title: "Researching references",
      done:  "Researched 5 sources",
      body: `
        <div class="sub">Scanning <span class="kw">5 sources</span> on inbox patterns &amp; tagging.</div>
        <div class="sources">
          <span class="src" style="animation-delay:.15s"><span class="fav" style="background:#7aa6ff"></span>practice.design</span>
          <span class="src" style="animation-delay:.35s"><span class="fav" style="background:#ff9a5a"></span>support-handbook</span>
          <span class="src" style="animation-delay:.55s"><span class="fav" style="background:#b07cff"></span>ux-quarterly</span>
          <span class="src" style="animation-delay:.75s"><span class="fav" style="background:#5ec99a"></span>research.note</span>
          <span class="src" style="animation-delay:.95s"><span class="fav" style="background:#e85a8a"></span>field-notes</span>
        </div>`,
    },
    plan: {
      title: "Drafting a plan",
      done:  "Plan ready · 4 items",
      body: `
        <div class="sub">Three views, two models, SSO from day one.</div>
        <div class="plan">
          <div class="plan-item done" style="animation-delay:.10s"><span class="box"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M3 8.5l3 3 7-7"/></svg></span>Inbox · triage queue</div>
          <div class="plan-item done" style="animation-delay:.30s"><span class="box"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M3 8.5l3 3 7-7"/></svg></span>Insights · sentiment + tags</div>
          <div class="plan-item done" style="animation-delay:.50s"><span class="box"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M3 8.5l3 3 7-7"/></svg></span>Reports · weekly digests</div>
          <div class="plan-item done" style="animation-delay:.70s"><span class="box"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M3 8.5l3 3 7-7"/></svg></span>Auth · SSO + role-based access</div>
        </div>`,
    },
    design: {
      title: "Drafting the interface",
      done:  "Draft v1 ready",
      body:  `<div class="sub">Warm neutrals, ember accents, one inbox view.</div>`,
    },
    build: {
      title: "Building components",
      done:  "Built 8 files",
      body:  `<div class="sub">Wiring <span class="kw">thread.tsx</span>, <span class="kw">insights.tsx</span>, schema, theme…</div>`,
    },
    publish: {
      title: "✓ Deployed",
      done:  "Deployed",
      body:  `<div class="sub">Live at <span class="kw">forge.app/p-94b2</span> — share or keep iterating.</div>`,
    },
  }
  const AGENT_ORDER = ["research", "plan", "design", "build", "publish"]

  function rebuildChat() {
    clearChat()
    const p = PHASES[currentPhase].key
    if (p === "idle" || p === "typing") return

    addMsg("user", 'Build a customer <span class="tl">feedback tool</span> with AI-powered analytics.', 0)
    if (p === "submit") return

    const activeIdx = AGENT_ORDER.indexOf(p)
    if (activeIdx < 0) return

    AGENT_ORDER.forEach((k, i) => {
      if (i > activeIdx) return
      const cfg = AGENT_MSGS[k]
      if (i < activeIdx) {
        const m = addMsg("agent", `<span class="tl">${cfg.done}</span>`, 0)
        m.classList.add("compact")
      } else {
        addMsg("agent", `<span class="tl">${cfg.title}</span>${cfg.body}`, 60)
      }
    })
  }

  let currentPhase = 0
  function setPhase(p: number) {
    currentPhase = ((p % PHASES.length) + PHASES.length) % PHASES.length
    const cfg = PHASES[currentPhase]
    stage.dataset.phase = cfg.key
    phaseLabel.textContent = cfg.label
    wsStatusText.textContent = cfg.label
    wsStatus.classList.toggle("live", cfg.key === "publish")
    corner.classList.toggle("hidden", !(cfg.key === "idle" || cfg.key === "typing"))

    stepEls.forEach((s, i) => s.classList.toggle("active", i === cfg.step))
    pips.forEach((d, i) => d.classList.toggle("on", i === currentPhase))
    submit.classList.toggle("fire", cfg.key === "submit")

    subChips.forEach((c) => {
      const idx = SUB_ORDER.indexOf(c.dataset.sub!)
      const curIdx = cfg.sub ? SUB_ORDER.indexOf(cfg.sub) : -1
      c.classList.toggle("active", cfg.sub === c.dataset.sub)
      c.classList.toggle("done", curIdx === -1 ? cfg.step > 1 : idx < curIdx)
    })

    if (cfg.key === "idle") setPromptEmpty()
    else if (cfg.key === "typing") startTyping()
    else setPromptFull()

    rebuildChat()

    if (cfg.key === "idle" || cfg.key === "typing" || cfg.key === "submit") {
      showScene("idle")
    } else if (cfg.key === "research") {
      renderResearch()
      showScene("research"); rerunScene(scenes.research)
    } else if (cfg.key === "plan") {
      showScene("plan"); rerunScene(scenes.plan)
    } else if (cfg.key === "design") {
      showScene("design"); rerunScene(scenes.design)
    } else if (cfg.key === "build") {
      showScene("build"); rerunScene(scenes.build)
      startBuild()
    } else if (cfg.key === "publish") {
      showScene("publish")
    }

    if (cfg.key !== "build") resetBuild()
  }

  let playing = true
  let scheduleTimer: ReturnType<typeof setTimeout> | null = null
  function reschedule() {
    if (scheduleTimer) clearTimeout(scheduleTimer)
    if (!playing) return
    scheduleTimer = setTimeout(() => {
      setPhase((currentPhase + 1) % PHASES.length)
      reschedule()
    }, PHASES[currentPhase].dwell)
  }

  setPhase(0)
  reschedule()

  stepEls.forEach((s, i) => {
    const onClick = () => {
      const first = PHASES.findIndex((p) => p.step === i)
      if (first >= 0) { setPhase(first); reschedule() }
    }
    s.addEventListener("click", onClick)
    cleanups.push(() => s.removeEventListener("click", onClick))
  })

  const onKey = (e: KeyboardEvent) => {
    if (e.code === "Space") {
      e.preventDefault()
      playing = !playing
      if (playing) reschedule()
      else if (scheduleTimer) clearTimeout(scheduleTimer)
    }
    if (e.code === "ArrowRight") { setPhase(currentPhase + 1); reschedule() }
    if (e.code === "ArrowLeft")  { setPhase(currentPhase - 1); reschedule() }
  }
  document.addEventListener("keydown", onKey)
  cleanups.push(() => document.removeEventListener("keydown", onKey))

  cleanups.push(() => {
    if (typingTimer) clearTimeout(typingTimer)
    if (scheduleTimer) clearTimeout(scheduleTimer)
    if (buildAnim) cancelAnimationFrame(buildAnim)
  })

  return () => cleanups.forEach((f) => { try { f() } catch {} })
}

// ── Main component ──────────────────────────────────────────────────────────

export default function Landing() {
  const navigate = useNavigate()
  let rootRef: HTMLDivElement | undefined

  onMount(() => {
    // initial theme from storage / system pref
    try {
      const stored = localStorage.getItem("forge.theme")
      const next = stored ?? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
      document.documentElement.dataset.theme = next
    } catch {}

    // wire CTAs to /auth
    const root = rootRef!
    const ctas = [...root.querySelectorAll<HTMLElement>("[data-forge-auth]")]
    const onAuth = () => navigate("/auth")
    ctas.forEach((el) => el.addEventListener("click", onAuth))

    const stop = startForgeAnimations(root)

    onCleanup(() => {
      ctas.forEach((el) => el.removeEventListener("click", onAuth))
      stop()
    })
  })

  return (
    <>
      <link rel="preconnect" href="https://fonts.googleapis.com" />
      <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin="anonymous" />
      <link
        href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700;800&family=Geist+Mono:wght@400;500&display=swap"
        rel="stylesheet"
      />
      <style innerHTML={STYLES} />
      <div ref={rootRef} class="forge-landing-root" innerHTML={MARKUP} />
    </>
  )
}
