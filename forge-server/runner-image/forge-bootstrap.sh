#!/bin/sh
# forge-bootstrap.sh — runs at every dev container start.
#
# Idempotent: every section checks state before writing. Safe to re-run.
#
# Sections (in order):
#   1. Layout detection            — Next.js root vs src/
#   2. Tailwind v4 (always)        — canonical postcss + globals.css @import
#                                    + layout import + devDeps. Closes the
#                                    "design isn't applying" hallucination.
#   3. next.config.ts (always)     — Forge-mandatory keys
#   4. instrumentation-client.ts   — browser-side runtime-error bridge
#   5. DB scaffolding (OPT-IN)     — only runs when .forge/db-enabled exists
#                                    OR an existing data.db / drizzle/*.sql
#                                    is present (back-compat for old projects).
#
# DB is OPT-IN. Projects do not get Drizzle / better-sqlite3 / drizzle-kit
# unless the agent (or user) has explicitly enabled persistence — usually by
# running `bash /usr/local/bin/forge-enable-db.sh` after the user asks for
# data storage. See AGENTS.md §DB-opt-in.
set -e

cd /app

# Only bootstrap real Node projects.
[ -f package.json ] || exit 0

is_next_project() {
  grep -q '"next"[[:space:]]*:' package.json 2>/dev/null
}

# ── 1. Layout detection ──────────────────────────────────────────────────────
# Next.js supports both root-level (app/, pages/) and src/-prefixed layouts.
# The scaffold MUST land in the same tree the app actually uses, otherwise
# every generated file is dead code and the agent freelances raw SQL.
if [ -d app ] || [ -d pages ]; then
  ROOT="."
elif [ -d src/app ] || [ -d src/pages ]; then
  ROOT="src"
else
  ROOT="."
fi

if [ "$ROOT" = "." ]; then
  SCHEMA_REL="./lib/db/schema.ts"
else
  SCHEMA_REL="./src/lib/db/schema.ts"
fi

# ── 2. Tailwind v4 (always for Next projects) ────────────────────────────────
# We OWN three artefacts to stop weaker models hallucinating Tailwind setup:
#
#   a) postcss.config.mjs              — must use "@tailwindcss/postcss"
#   b) {ROOT}/app/globals.css          — must START with @import "tailwindcss";
#                                        and never contain v3 @tailwind directives
#   c) {ROOT}/app/layout.tsx           — must import the globals.css above
#
# Plus tailwindcss + @tailwindcss/postcss in devDependencies. Common failure
# modes we explicitly defuse here:
#   - model writes `@tailwind base; @tailwind components; @tailwind utilities;`
#     (v3 syntax — silently ignored in v4 → "my classes don't apply")
#   - model writes `import "tailwindcss/tailwind.css"` (wrong path → 500)
#   - model adds `tailwindcss` (not @tailwindcss/postcss) to the postcss
#     config → build fails with "Cannot find module"
#   - model deletes the globals.css import from layout.tsx → page is unstyled
#
# Skipped for non-Next projects (Vite/Remix have different conventions).
if is_next_project; then

  # 2a. postcss.config.mjs — Forge-owned, always canonical
  cat > postcss.config.mjs <<'EOF'
// Forge-owned. Do not edit — bootstrap regenerates this file at every
// container start. Tailwind v4 uses the @tailwindcss/postcss plugin, NOT
// the v3 `tailwindcss` plugin. Using the wrong one is the single most
// common Tailwind-doesn't-work cause we see, so we lock it.
const config = {
  plugins: ["@tailwindcss/postcss"],
}

export default config
EOF

  # 2b. globals.css — must begin with @import "tailwindcss"; strip v3 directives.
  GCSS="$ROOT/app/globals.css"
  mkdir -p "$ROOT/app"
  if [ ! -f "$GCSS" ]; then
    printf '@import "tailwindcss";\n' > "$GCSS"
  else
    # Strip v3 directives and any prior @import "tailwindcss"; line, then
    # prepend the canonical import. Preserves the user's @theme / custom CSS.
    TMP="$(mktemp)"
    # shellcheck disable=SC2016
    sed -E \
      -e '/^[[:space:]]*@tailwind[[:space:]]+(base|components|utilities)[[:space:]]*;?[[:space:]]*$/d' \
      -e '/^[[:space:]]*@import[[:space:]]+["'\'']tailwindcss["'\''][[:space:]]*;?[[:space:]]*$/d' \
      "$GCSS" > "$TMP"
    {
      printf '@import "tailwindcss";\n'
      cat "$TMP"
    } > "$GCSS"
    rm -f "$TMP"
  fi

  # 2c. layout.tsx — ensure it imports the globals.css we manage.
  LAYOUT="$ROOT/app/layout.tsx"
  if [ -f "$LAYOUT" ]; then
    if ! grep -qE 'import[[:space:]]+["'\''"][^"'\''"]*globals\.css["'\''"]' "$LAYOUT"; then
      TMP="$(mktemp)"
      { printf 'import "./globals.css"\n'; cat "$LAYOUT"; } > "$TMP"
      mv "$TMP" "$LAYOUT"
      echo "[forge-bootstrap] re-added missing globals.css import to layout.tsx"
    fi
  fi

  # 2d. package.json — ensure Tailwind v4 devDeps present.
  node - <<'EOF'
const fs = require("fs")
const pj = JSON.parse(fs.readFileSync("package.json", "utf8"))
pj.devDependencies = pj.devDependencies || {}
const ensure = (k, v) => { if (!pj.devDependencies[k]) pj.devDependencies[k] = v }
ensure("tailwindcss",          "^4")
ensure("@tailwindcss/postcss", "^4")
fs.writeFileSync("package.json", JSON.stringify(pj, null, 2) + "\n")
EOF
fi

# ── 3. next.config.ts (always) ───────────────────────────────────────────────
# Without these, the preview looks broken in ways that take an hour to
# diagnose (HMR ws fails, fetches hang, page renders but isn't clickable).
# Forge owns this file — the agent should NOT customize it. See AGENTS.md.

# ── 3a. ONE next.config file. .ts is canonical; delete .mjs/.js siblings.
# Models freelance a second next.config.mjs alongside the .ts, then Next 15
# picks whichever it picks and the user gets one of: Turbopack "duplicate
# config" errors, the wrong allowedDevOrigins (preview iframe blocked), or
# the source-stamp loader not running (visual edits broken). Bootstrap
# resolves this deterministically: if .ts exists, .mjs / .js / .cjs siblings
# are removed. Run BEFORE the patch step so we don't accidentally patch a
# duplicate. If only .mjs/.js exists (no .ts), leave it — the patch step
# below handles it as legacy.
if [ -f next.config.ts ]; then
  for sibling in next.config.mjs next.config.js next.config.cjs; do
    if [ -f "$sibling" ]; then
      echo "[forge-bootstrap] removing duplicate $sibling (canonical is next.config.ts)"
      rm -f "$sibling" 2>/dev/null || true
    fi
  done
fi

if [ -f next.config.ts ] || [ -f next.config.js ] || [ -f next.config.mjs ]; then
  node - <<'EOF'
const fs = require("fs")
// Source-stamp loader — baked into the runner image. GUARD: only wire it
// into the config when the file actually exists on disk. A config that
// references a missing loader hard-500s EVERY page of the dev server
// ("Cannot find module .../loader.js"), which is strictly worse than
// degraded visual edits. Missing file = stale forge-runner image (built
// before the loader was added); rebuild the image to restore visual edits.
const STAMP_PATH = "/usr/local/lib/forge-source-stamp/loader.js"
const stampOk = fs.existsSync(STAMP_PATH)
if (!stampOk) {
  console.warn("[forge-bootstrap] WARN: " + STAMP_PATH + " not found in this")
  console.warn("[forge-bootstrap] WARN: container — writing next.config WITHOUT the source-stamp")
  console.warn("[forge-bootstrap] WARN: loader (visual edits disabled). Rebuild forge-runner:latest")
  console.warn("[forge-bootstrap] WARN: to restore visual edits.")
}

// NOTE: deliberately NO turbopack.rules entry for the stamp loader.
// Wiring the loader through turbopack rules has produced doubled-extension
// module names in runner containers ("./components/Hero.tsx.tsx — Module
// not found", import-graph-wide, dev server hard-500) that we could not
// reproduce outside the runner, while delivering no observed stamping under
// turbopack either. Until that is understood upstream, the loader runs via
// the webpack hook only (verified working); under `next dev --turbopack`
// visual-edit stamping is degraded but previews never break.
const STAMP_BLOCKS = `  webpack: (config: any, { dev }: { dev: boolean }) => {
    if (dev) {
      config.module.rules.push({
        test: /\\.(jsx|tsx)$/,
        exclude: /node_modules|\\.next|\\.forge/,
        enforce: "pre",
        use: [{ loader: FORGE_SOURCE_STAMP }],
      })
    }
    return config
  },
`

const CANONICAL = `import type { NextConfig } from "next"
${stampOk ? `
// Forge source-stamp loader (visual edits). Baked into the runner image at
// /usr/local/lib/forge-source-stamp/loader.js — see runner-image/Dockerfile.
// Stamps every JSX intrinsic with data-forge-source="path:line:col" in dev
// only, so the Forge UI's Select tool can map clicks back to source.
//
// Wired through the webpack hook ONLY — wiring it through turbopack.rules
// has produced doubled-extension module failures ("Hero.tsx.tsx") in
// runner containers. Under \`next dev --turbopack\` stamping is degraded,
// but previews never break. Do not add a turbopack rule for this loader.
const FORGE_SOURCE_STAMP = "/usr/local/lib/forge-source-stamp/loader.js"
` : ""}
const nextConfig: NextConfig = {
  devIndicators: false,
  // Forge preview: <projectId>.preview.{lvh.me|forge.com}. Next 15+ rejects
  // cross-origin dev requests (HMR ws, server actions, RSC) without this.
  allowedDevOrigins: [
    "*.preview.lvh.me",
    "*.preview.forge.com",
  ],
  // Native modules must be externalized or the route module fails to load
  // and /api/* requests hang in dev. Add any other native deps you use.
  // serverExternalPackages: ["pg"] — only needed if pg is bundled by Next.
  // Drizzle's node-postgres adapter resolves cleanly without it. Empty for now.
${stampOk ? STAMP_BLOCKS : ""}}

export default nextConfig
`
  const target = fs.existsSync("next.config.ts") ? "next.config.ts"
               : fs.existsSync("next.config.mjs") ? "next.config.mjs"
               : "next.config.js"
  const cur = fs.readFileSync(target, "utf8")
  const hasOrigins   = /allowedDevOrigins\s*:/.test(cur)
  const hasOldTurbo     = /experimental\s*:[\s\S]*?turbo\s*:/.test(cur)
  // The stamp loader must NOT be wired through turbopack rules — that path
  // has produced import-graph-wide doubled-extension failures in runner
  // containers ("./Hero.tsx.tsx — Module not found", dev server hard-500).
  // Any turbopack rule referencing the loader is treated as needs-patching
  // so existing projects shed it on next container boot.
  const hasTurboStampRule = /turbopack\s*:[\s\S]*?forge-source-stamp/.test(cur)
  // `as: "*.tsx"` renames matched modules — same failure family.
  const hasAsTsx        = /as\s*:\s*['"]\*\.tsx['"]/.test(cur)
  // forge-source-stamp (webpack hook) is mandatory — without it, visual
  // edits in the UI can't map clicks back to source. Treat its absence the
  // same as a missing mandatory key so existing projects auto-upgrade.
  const hasStamp = /forge-source-stamp/.test(cur)
  // Config is already in the desired shape for this container?
  //   loader present  → webpack hook must reference it, turbopack must NOT
  //   loader MISSING  → must NOT reference it anywhere (dangling reference
  //                     500s the whole dev server) but still needs origins.
  const settled = stampOk
    ? (hasOrigins && hasStamp && !hasOldTurbo && !hasTurboStampRule && !hasAsTsx)
    : (hasOrigins && !hasStamp && !hasOldTurbo)
  if (settled) process.exit(0)
  const customKeys = (cur.match(/^\s*\w+\s*:/gm) || []).filter(
    k => !/^(devIndicators|allowedDevOrigins|serverExternalPackages|turbopack|experimental|webpack)\s*:/.test(k.trim())
  )
  if (customKeys.length === 0 && target === "next.config.ts") {
    fs.writeFileSync(target, CANONICAL)
    console.log("[forge-bootstrap] next.config.ts patched with mandatory keys" + (stampOk ? " (incl. source-stamp)" : " (source-stamp SKIPPED — loader missing)"))
  } else {
    console.warn("[forge-bootstrap] WARN: next.config has custom keys; cannot")
    console.warn("[forge-bootstrap] WARN: auto-patch. Add allowedDevOrigins,")
    console.warn("[forge-bootstrap] WARN: serverExternalPackages, and the source-stamp")
    console.warn("[forge-bootstrap] WARN: loader manually (see AGENTS.md).")
  }
EOF
elif is_next_project; then
  # Same guard as the patch path above: never reference the source-stamp
  # loader unless it actually exists in this container (stale forge-runner
  # image) — a dangling loader path 500s every page of the dev server.
  if [ -f /usr/local/lib/forge-source-stamp/loader.js ]; then
cat > next.config.ts <<'EOF'
import type { NextConfig } from "next"

// Forge source-stamp loader (visual edits) — baked at this absolute path in
// the runner image. Dev-only; stripped from production builds.
const FORGE_SOURCE_STAMP = "/usr/local/lib/forge-source-stamp/loader.js"

const nextConfig: NextConfig = {
  devIndicators: false,
  allowedDevOrigins: [
    "*.preview.lvh.me",
    "*.preview.forge.com",
  ],
  // serverExternalPackages: ["pg"] — only needed if pg is bundled by Next.
  // Drizzle's node-postgres adapter resolves cleanly without it. Empty for now.
  //
  // Source-stamp loader is wired through the webpack hook ONLY. Do NOT add
  // a turbopack rule for it — that path has produced doubled-extension
  // module failures ("Hero.tsx.tsx — Module not found") that 500 the whole
  // dev server.
  webpack: (config: any, { dev }: { dev: boolean }) => {
    if (dev) {
      config.module.rules.push({
        test: /\.(jsx|tsx)$/,
        exclude: /node_modules|\.next|\.forge/,
        enforce: "pre",
        use: [{ loader: FORGE_SOURCE_STAMP }],
      })
    }
    return config
  },
}

export default nextConfig
EOF
    echo "[forge-bootstrap] next.config.ts created with mandatory keys (incl. source-stamp)"
  else
cat > next.config.ts <<'EOF'
import type { NextConfig } from "next"

const nextConfig: NextConfig = {
  devIndicators: false,
  allowedDevOrigins: [
    "*.preview.lvh.me",
    "*.preview.forge.com",
  ],
}

export default nextConfig
EOF
    echo "[forge-bootstrap] WARN: source-stamp loader missing in this container (stale forge-runner image?)"
    echo "[forge-bootstrap] next.config.ts created WITHOUT source-stamp (visual edits disabled)"
  fi
fi

# ── 4. instrumentation-client.ts (always for Next projects) ──────────────────
# Captures browser-side errors and postMessages them to window.parent. The
# Forge UI's preview iframe parent listens and forwards to forge-server.
# NOTE: marker bumped v3 → v4 to add (a) signature inference so errors like
# "Unexpected token '<' … is not valid JSON" get tagged json_parse_error
# instead of the generic console_error, and (b) a MutationObserver that
# scrapes the Next.js dev-overlay DOM — errors Next intercepts before our
# console.error shim runs (Turbopack ConsoleError, React errorBoundary)
# would otherwise never reach Forge. The grep below matches the v-prefix
# only, so any prior version auto-upgrades on next container start.
INSTR_MARKER="// forge:runtime-error-bridge:v"
if is_next_project; then
  if [ ! -f instrumentation-client.ts ] \
     || grep -q "$INSTR_MARKER" instrumentation-client.ts; then
cat > instrumentation-client.ts <<'EOF'
// forge:runtime-error-bridge:v4
// Forge-owned. Auto-regenerated by forge-bootstrap if missing or unchanged.
// If you need custom client instrumentation, add it BELOW the bridge block —
// do not remove the bridge.
(() => {
  if (typeof window === "undefined") return
  // Version-scoped install guard. Using a v4-specific name guarantees v4
  // initialization is never silently skipped by an older sibling whose
  // guard variable was scoped to its own version.
  if ((window as unknown as { __forgeBridgeV4Installed?: boolean }).__forgeBridgeV4Installed) return
  ;(window as unknown as { __forgeBridgeV4Installed?: boolean }).__forgeBridgeV4Installed = true
  // eslint-disable-next-line no-console
  console.log("[forge-bridge] v4 installed (runtime errors + Next overlay scrape + screenshot + visual edits)")

  type Payload = {
    source:    "browser"
    signature?: string
    message:   string
    detail?:   string
    file?:     string
    line?:     number
    column?:   number
    stack?:    string
    url?:      string
    status?:   number
    userAgent?: string
  }

  const recent = new Map<string, number>()
  const DEDUP_MS = 2000

  // Signature inference. Without this, every error from console.error /
  // unhandledrejection / window.error gets the same generic signature
  // (console_error, unhandled_rejection, window_error), and the agent has
  // to grep the message to figure out what's actually wrong. Inferring
  // signatures up-front lets the agent (and the FE banner) branch on a
  // stable key — e.g. json_parse_error → "check what your fetch is
  // returning and confirm the route exists". Pure string sniffing, no
  // perf cost worth talking about. Order matters: most-specific first.
  const inferSignature = (msg: string, fallback: string): string => {
    const m = msg || ""
    if (/Unexpected token .* is not valid JSON|JSON\.parse|SyntaxError.*JSON/i.test(m)) return "json_parse_error"
    if (/Hydration failed|did not match.*server|Text content does not match/i.test(m)) return "hydration_mismatch"
    if (/Maximum update depth exceeded|Too many re-renders/i.test(m)) return "react_render_loop"
    if (/Cannot find module|Module not found.*resolve/i.test(m)) return "missing_module"
    if (/TypeError: Cannot read prop(?:erty|erties).* of (?:undefined|null)/i.test(m)) return "null_property_access"
    if (/ChunkLoadError|Loading chunk \d+ failed/i.test(m)) return "chunk_load_error"
    return fallback
  }

  const send = (p: Payload) => {
    // Re-tag based on the message so payloads from generic listeners get
    // a useful signature instead of "console_error" forever.
    const inferred = inferSignature(p.message, p.signature ?? "browser_error")
    p.signature = inferred
    const fp = `${p.signature ?? ""}|${p.message}|${p.file ?? ""}|${p.line ?? ""}|${p.status ?? ""}`
    const now = Date.now()
    const last = recent.get(fp) ?? 0
    if (now - last < DEDUP_MS) return
    recent.set(fp, now)
    if (recent.size > 100) {
      for (const [k, t] of recent) if (now - t > 60000) recent.delete(k)
    }
    try {
      window.parent.postMessage(
        { type: "forge:runtime-error", payload: { ...p, userAgent: navigator.userAgent } },
        "*",
      )
    } catch { /* parent unreachable — silent */ }
  }

  window.addEventListener("error", (ev) => {
    send({
      source:    "browser",
      signature: "window_error",
      message:   String(ev.message ?? "Unknown error"),
      file:      ev.filename,
      line:      ev.lineno,
      column:    ev.colno,
      stack:     ev.error?.stack,
    })
  })

  window.addEventListener("unhandledrejection", (ev) => {
    const reason = ev.reason
    send({
      source:    "browser",
      signature: "unhandled_rejection",
      message:   String(reason?.message ?? reason ?? "Unhandled promise rejection"),
      stack:     reason?.stack,
    })
  })

  const origError = console.error.bind(console)
  console.error = (...args: unknown[]) => {
    try {
      const msg = args
        .map((a) => (a instanceof Error ? a.stack ?? a.message : typeof a === "string" ? a : JSON.stringify(a)))
        .join(" ")
        .slice(0, 500)
      if (msg) {
        send({ source: "browser", signature: "console_error", message: msg })
      }
    } catch { /* never let logging break logging */ }
    origError(...args)
  }

  const origFetch = window.fetch.bind(window)
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const res = await origFetch(input as RequestInfo, init)
    if (!res.ok) {
      const url = typeof input === "string" ? input : (input as Request).url ?? String(input)
      send({
        source:    "browser",
        signature: "fetch_not_ok",
        message:   `fetch ${url} → ${res.status}`,
        url,
        status:    res.status,
      })
    }
    return res
  }

  // ── Screenshot-on-request ─────────────────────────────────────────────────
  // Parent (Forge UI) posts { type: "forge:screenshot-request", requestId }.
  // We lazy-load html2canvas from a CDN on FIRST request — bundling it would
  // add ~45KB gzipped to every Next start across every container, even for
  // users who never click the button. On-demand keeps the cold-start tax at
  // zero. Reply with { type: "forge:screenshot-response", requestId, dataUrl }
  // or, on failure, an empty dataUrl so the parent can fall back gracefully.
  const HTML2CANVAS_CDN =
    "https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"
  let h2cPromise: Promise<unknown> | null = null
  const loadHtml2Canvas = (): Promise<unknown> => {
    const w = window as unknown as { html2canvas?: unknown }
    if (w.html2canvas) return Promise.resolve(w.html2canvas)
    if (h2cPromise) return h2cPromise
    h2cPromise = new Promise((resolve, reject) => {
      const s = document.createElement("script")
      s.src = HTML2CANVAS_CDN
      s.async = true
      s.crossOrigin = "anonymous"
      s.onload = () => {
        const fn = (window as unknown as { html2canvas?: unknown }).html2canvas
        if (fn) resolve(fn)
        else reject(new Error("html2canvas missing after load"))
      }
      s.onerror = () => reject(new Error("html2canvas CDN load failed"))
      document.head.appendChild(s)
    })
    return h2cPromise
  }

  const reply = (target: MessageEventSource | null, requestId: string, dataUrl: string) => {
    try {
      // Reply to the exact frame that asked, not "*". The parent gates the
      // response on event.origin matching the preview host pattern anyway,
      // but tighter routing here means no other listener can see the dataURL.
      ;(target as Window | null)?.postMessage(
        { type: "forge:screenshot-response", requestId, dataUrl },
        "*",
      )
    } catch { /* parent unreachable — silent */ }
  }

  window.addEventListener("message", async (ev: MessageEvent) => {
    const data = ev.data
    if (!data || typeof data !== "object") return
    if (data.type !== "forge:screenshot-request") return
    const requestId = typeof data.requestId === "string" ? data.requestId : ""
    if (!requestId) return

    try {
      const h2c = await loadHtml2Canvas() as (el: Element, opts?: unknown) => Promise<HTMLCanvasElement>
      // Cap dimensions — a 4K canvas → dataURL is ~6MB of base64, which
      // makes the parent prompt enormous and the next agent turn expensive.
      // 1280px wide is plenty for visual diagnosis at chat density.
      const MAX_W = 1280
      const scale = Math.min(1, MAX_W / Math.max(1, document.documentElement.clientWidth))
      const canvas = await h2c(document.body, {
        scale,
        useCORS:        true,
        allowTaint:     false,
        backgroundColor: "#ffffff",
        logging:        false,
      })
      // JPEG quality 0.7 — visually fine for layout debugging, ~3-4x smaller
      // than PNG. Quality knob is the right place to tune token cost later.
      const dataUrl = canvas.toDataURL("image/jpeg", 0.7)
      reply(ev.source, requestId, dataUrl)
    } catch {
      reply(ev.source, requestId, "")
    }
  })

  // ── Visual edits: select-mode ─────────────────────────────────────────────
  // Parent posts { type: "forge:select-enter" } → we attach a hover overlay
  // and a click-capture handler. On click we walk up from event.target to
  // the nearest element with data-forge-source (stamped by the source-stamp
  // loader at dev compile time) and post back:
  //   { type: "forge:select-pick", source, tag, text }
  // Esc, scroll, or `forge:select-exit` from parent ends select mode.
  //
  // Cascade safety (CLAUDE.md §3.2): the overlay is a single absolutely-
  // positioned div with INLINE styles only. We start with `all: initial` to
  // wipe every inherited property, then set the specific styles we need.
  // No class names, no global selectors, no shared CSS. The user's app
  // can't override us (max z-index, !important via setProperty priority),
  // and we can't bleed into the user's app (no shared selector surface).
  //
  // pointer-events: none on the overlay so it never swallows hover/clicks
  // — the underlying element still receives the event we listen for.
  let selectActive = false
  let selectOverlay: HTMLDivElement | null = null
  let selectLabel:   HTMLDivElement | null = null
  let selectTarget:  Element | null = null
  let selectSource:  MessageEventSource | null = null

  const overlayStyle = (el: HTMLDivElement) => {
    // Wipe inheritance, then set only what we need.
    el.style.cssText = "all: initial;"
    el.setAttribute("data-forge-overlay", "1")
    // setProperty with "important" beats any !important rule the user's
    // app might inject via a wildcard selector.
    const set = (k: string, v: string) => el.style.setProperty(k, v, "important")
    set("position",        "fixed")
    set("pointer-events",  "none")
    set("z-index",         "2147483647")
    set("border",          "2px solid #4f8cff")
    set("background",      "rgba(79, 140, 255, 0.12)")
    set("box-shadow",      "0 0 0 1px rgba(255, 255, 255, 0.6) inset")
    set("transition",      "all 80ms ease-out")
    set("box-sizing",      "border-box")
    set("display",         "none")
  }

  // Small tag-name pill anchored above the highlighted element. Same
  // cascade-safety treatment as the outline overlay (all: initial, inline
  // !important, unique data attribute, pointer-events: none).
  const labelStyle = (el: HTMLDivElement) => {
    el.style.cssText = "all: initial;"
    el.setAttribute("data-forge-overlay-label", "1")
    const set = (k: string, v: string) => el.style.setProperty(k, v, "important")
    set("position",        "fixed")
    set("pointer-events",  "none")
    set("z-index",         "2147483647")
    set("background",      "#4f8cff")
    set("color",           "#ffffff")
    set("font",            "600 11px/1 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif")
    set("padding",         "3px 6px")
    set("border-radius",   "3px 3px 0 0")
    set("white-space",     "nowrap")
    set("display",         "none")
  }

  const ensureOverlay = (): HTMLDivElement => {
    if (selectOverlay && selectOverlay.isConnected) return selectOverlay
    const el = document.createElement("div")
    overlayStyle(el)
    document.documentElement.appendChild(el)
    selectOverlay = el
    return el
  }

  const ensureLabel = (): HTMLDivElement => {
    if (selectLabel && selectLabel.isConnected) return selectLabel
    const el = document.createElement("div")
    labelStyle(el)
    document.documentElement.appendChild(el)
    selectLabel = el
    return el
  }

  const positionOverlay = (target: Element | null) => {
    const ov = selectOverlay
    const lb = selectLabel
    if (!ov || !lb) return
    if (!target) {
      ov.style.setProperty("display", "none", "important")
      lb.style.setProperty("display", "none", "important")
      return
    }
    const r = target.getBoundingClientRect()
    if (r.width === 0 && r.height === 0) {
      ov.style.setProperty("display", "none", "important")
      lb.style.setProperty("display", "none", "important")
      return
    }
    ov.style.setProperty("display", "block", "important")
    ov.style.setProperty("top",    r.top + "px",    "important")
    ov.style.setProperty("left",   r.left + "px",   "important")
    ov.style.setProperty("width",  r.width + "px",  "important")
    ov.style.setProperty("height", r.height + "px", "important")

    // Label content: <tag> + truncated text snippet. Cheap textContent
    // read — no innerHTML, no XSS surface.
    const tag = (target.tagName || "").toLowerCase()
    const text = (target.textContent || "").trim().replace(/\s+/g, " ").slice(0, 40)
    lb.textContent = text ? `<${tag}>  ${text}` : `<${tag}>`

    // Label sits above the box; if the box is too close to the top of the
    // viewport, flip it inside the top edge so it stays visible.
    lb.style.setProperty("display", "block", "important")
    const labelH = 18 // approx, matches font + padding
    const labelTop = r.top - labelH < 2 ? r.top + 2 : r.top - labelH
    lb.style.setProperty("top",  labelTop + "px", "important")
    lb.style.setProperty("left", r.left + "px",   "important")
  }

  // Walk up from `start` to the nearest ancestor with data-forge-source,
  // OR the element itself if it has it. Returns null if none — caller
  // falls back to the clicked element directly.
  const findStamped = (start: Element | null): Element | null => {
    let cur: Element | null = start
    let hops = 0
    while (cur && hops < 12) {
      if (cur.hasAttribute && cur.hasAttribute("data-forge-source")) return cur
      cur = cur.parentElement
      hops++
    }
    return null
  }

  const onMove = (ev: MouseEvent) => {
    if (!selectActive) return
    const target = ev.target as Element | null
    // Don't highlight the overlay itself.
    if (!target || target === selectOverlay) return
    selectTarget = target
    positionOverlay(target)
  }

  const onClick = (ev: MouseEvent) => {
    if (!selectActive) return
    // Capture-phase: stop the app from receiving this click. We must use
    // stopImmediatePropagation because the user's app may have its own
    // capture-phase listeners; plain stopPropagation isn't enough.
    ev.preventDefault()
    ev.stopImmediatePropagation()

    const clicked = ev.target as Element | null
    const target  = clicked && clicked !== selectOverlay ? clicked : selectTarget
    const stamped = findStamped(target)
    const picked  = stamped ?? target
    const source  = stamped?.getAttribute("data-forge-source") ?? null
    const tag     = (picked?.tagName ?? "").toLowerCase()
    const text    = (picked?.textContent ?? "").trim().replace(/\s+/g, " ").slice(0, 80)

    try {
      ;(selectSource as Window | null)?.postMessage(
        { type: "forge:select-pick", source, tag, text },
        "*",
      )
    } catch { /* parent unreachable — silent */ }

    exitSelect()
  }

  const onKey = (ev: KeyboardEvent) => {
    if (!selectActive) return
    if (ev.key === "Escape") {
      ev.preventDefault()
      try {
        ;(selectSource as Window | null)?.postMessage({ type: "forge:select-cancelled" }, "*")
      } catch { /* silent */ }
      exitSelect()
    }
  }

  const enterSelect = (src: MessageEventSource | null) => {
    if (selectActive) { selectSource = src; return }
    selectActive = true
    selectSource = src
    ensureOverlay()
    ensureLabel()
    // Capture phase so we beat the app's own handlers to the click.
    document.addEventListener("mousemove", onMove,  true)
    document.addEventListener("click",     onClick, true)
    document.addEventListener("keydown",   onKey,   true)
    // Crosshair cursor signals select mode without injecting a CSS class
    // that could collide with the user's styles.
    document.documentElement.style.setProperty("cursor", "crosshair", "important")
    // eslint-disable-next-line no-console
    console.log("[forge-bridge] select-mode ENTERED")
  }

  const exitSelect = () => {
    if (!selectActive) return
    selectActive = false
    selectTarget = null
    document.removeEventListener("mousemove", onMove,  true)
    document.removeEventListener("click",     onClick, true)
    document.removeEventListener("keydown",   onKey,   true)
    document.documentElement.style.removeProperty("cursor")
    if (selectOverlay) selectOverlay.style.setProperty("display", "none", "important")
    if (selectLabel)   selectLabel.style.setProperty("display", "none", "important")
    // eslint-disable-next-line no-console
    console.log("[forge-bridge] select-mode EXITED")
  }

  window.addEventListener("message", (ev: MessageEvent) => {
    const data = ev.data
    if (!data || typeof data !== "object") return
    if (data.type === "forge:select-enter") {
      enterSelect(ev.source)
    } else if (data.type === "forge:select-exit") {
      exitSelect()
    }
  })

  // ── Next.js dev-overlay scraper ───────────────────────────────────────────
  // Why this exists: Next 15 + Turbopack intercepts many runtime errors and
  // renders them in the Console/Build overlay BEFORE our console.error shim
  // runs (the overlay's wrapper runs at module-init time). For those errors
  // the bridge would otherwise never fire — exactly the failure mode the
  // user keeps hitting ("Forge isn't picking this up"). We watch the
  // <nextjs-portal> element Next mounts for the overlay, scrape the visible
  // error text out of its shadow root on mutation, and post it like any
  // other browser error. Dedup is the same map send() already uses, so a
  // single error that's also shown in console + overlay only ships once.
  //
  // Cost shape: one MutationObserver scoped to document.body, only on
  // childList. Re-scopes to the portal's shadowRoot the moment the portal
  // appears — much cheaper than polling. No work at all when there's no
  // error overlay rendered.
  const scrapeOverlay = (root: ParentNode): void => {
    // Next.js dialog selectors. They have changed across major versions;
    // we try the v13/v14/v15 shapes in order. First non-empty wins.
    const sels = [
      "[data-nextjs-dialog-body]",
      "[data-nextjs-dialog]",
      "[data-nextjs-toast-errors-parent]",
      ".nextjs-container-errors-body",
      ".nextjs-container-errors-header",
      "nextjs-portal",
    ]
    for (const sel of sels) {
      const el = root.querySelector(sel)
      if (!el) continue
      // textContent strips style; we only want the human-readable error.
      const text = (el.textContent || "").trim().slice(0, 600)
      if (!text || text.length < 6) continue
      send({
        source:    "browser",
        signature: "next_overlay_error",
        message:   text,
      })
      return
    }
  }

  const attachOverlayObserver = (): void => {
    // Find the portal lazily. Next mounts it the first time it has anything
    // to show; before that, document.body is the right level to watch.
    let portalRoot: ShadowRoot | null = null
    const tryAttachPortal = (): boolean => {
      const portal = document.querySelector("nextjs-portal")
      if (!portal || !(portal as HTMLElement).shadowRoot) return false
      portalRoot = (portal as HTMLElement).shadowRoot
      const inner = new MutationObserver(() => {
        if (portalRoot) scrapeOverlay(portalRoot)
      })
      inner.observe(portalRoot, { childList: true, subtree: true, characterData: true })
      // Initial scrape — overlay may already be rendered when we attach.
      scrapeOverlay(portalRoot)
      return true
    }
    if (tryAttachPortal()) return
    const outer = new MutationObserver(() => {
      if (tryAttachPortal()) outer.disconnect()
    })
    outer.observe(document.body, { childList: true, subtree: false })
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", attachOverlayObserver, { once: true })
  } else {
    attachOverlayObserver()
  }
})()

export {}
EOF
    echo "[forge-bootstrap] instrumentation-client.ts (re)generated"
  fi
fi

# ── 5. DB scaffolding (OPT-IN, Postgres-per-schema) ──────────────────────────
# DB is NOT a default. Forge scaffolds DB code only after the agent explicitly
# enables it (via forge-enable-db.sh), which in turn requires the schema to
# have been provisioned via POST /api/projects/{id}/db/provision (or BYO
# Supabase). The connection string lives in `.env.local` (gitignored, excluded
# from the project zip download — see projects.py _ZIP_EXCLUDE_FILES).
#
# Phase B (D9 in LAUNCH_PLAN): SQLite is gone. Drizzle + node-postgres only.
# The `items` example uses `pgTable` with serial PK, timestamptz defaults, and
# the connection comes from `process.env.DATABASE_URL`. Same Drizzle shape
# works in both modes — local-self-host (Forge's Postgres) and hosted (BYO
# Supabase). The only thing that changes between modes is who sets the URL.
#
# DB is enabled if ANY of these are true (back-compat with SQLite-era marker):
#   - .forge/db-enabled marker file exists (set by the agent on first DB ask)
#   - drizzle/*.sql migrations already exist (project that was previously set
#     up; we don't want to re-bootstrap and clobber custom schema)
DB_ENABLED=0
if [ -f .forge/db-enabled ] || ls drizzle/*.sql >/dev/null 2>&1; then
  DB_ENABLED=1
fi

if [ "$DB_ENABLED" = "1" ]; then
  mkdir -p "$ROOT/lib/db" drizzle

  # 5a. FORGE_DB.md (workspace marker the agent sees)
  if [ ! -f FORGE_DB.md ]; then
cat > FORGE_DB.md <<'EOF'
# HOW TO BUILD A DATA APP IN THIS WORKSPACE

The plumbing is already done. Drizzle + node-postgres are scaffolded and
`DATABASE_URL` is in `.env.local`. Your job is to COPY the existing `items`
example and rename it to whatever the user asked for. Follow this recipe
exactly — it works every time.

---

## RECIPE: User wants a "tracker / list / app for managing X"

(Replace `X` with the actual resource — books, tasks, contacts, expenses, etc.)

### Step 1 — Add the table to `lib/db/schema.ts`

The file already exports an `items` table. Add a new `pgTable("X", {...})`
right below it with whatever fields the user described. **Do not delete the
`items` table** — leave it as a reference.

### Step 2 — Generate + apply the migration

```bash
npm run db:generate
npm run db:migrate
```

Both are safe to re-run. `db:generate` diffs `schema.ts` against `drizzle/`
and writes a new SQL file; `db:migrate` applies any new files.

### Step 3 — Copy the API routes

```
cp app/api/items/route.ts          app/api/X/route.ts
cp app/api/items/[id]/route.ts     app/api/X/[id]/route.ts
```

In both new files, find/replace `items` → `X`.

### Step 4 — Build the page

Use a client component that fetches from `"api/X"` (no leading slash) and
calls POST / PATCH / DELETE on the same prefix.

---

## HARD BANS

- ❌ `localStorage` / `sessionStorage`
- ❌ Hardcoded data arrays as the source of truth
- ❌ Writing to `.json` files with `fs`
- ❌ `better-sqlite3` / `sqlite` — Forge dropped SQLite (LAUNCH_PLAN D9)
- ❌ Raw `pg` / `Pool` / `Client` outside `client.ts`
- ❌ Raw `CREATE TABLE` DDL — always go through `schema.ts` + drizzle-kit
- ❌ Prisma / TypeORM / Sequelize / Mongoose
- ❌ Echoing `DATABASE_URL` or any `.env` content in chat messages — the
  URL contains a role password
- ❌ `cat .env*` in tool calls that print to the conversation

If you write any of the above, the Data tab will not see your tables.

**Use Drizzle + pgTable. Copy `items`. Rename. Done.**
EOF
    if [ "$ROOT" = "src" ]; then
      sed -i \
        -e 's|`lib/db/|`src/lib/db/|g' \
        -e 's|`app/|`src/app/|g' \
        -e 's| lib/db/| src/lib/db/|g' \
        -e 's| app/api/| src/app/api/|g' \
        FORGE_DB.md
    fi
  fi

  # 5b. drizzle.config.ts — Postgres dialect, URL from env.
  #     ALWAYS rewritten (not gated on existence) because models keep
  #     freelancing this file back to SQLite. The AI can edit it during a
  #     session and it'll get reset on the next container boot — but the
  #     boot-time rewrite is the durable backstop. If the AI's freelanced
  #     SQLite config breaks the build, the user restarts the container and
  #     Postgres comes back. Same idea as postcss.config.mjs being
  #     Forge-owned above (Tailwind section).
cat > drizzle.config.ts <<EOF
// Forge-owned. Do NOT edit — bootstrap regenerates this file at every
// container start. Forge dropped SQLite (LAUNCH_PLAN D9) and any
// \`dialect: "sqlite"\` here gets reset to "postgresql" on next boot.
import type { Config } from "drizzle-kit"

export default {
  schema:  "${SCHEMA_REL}",
  out:     "./drizzle",
  dialect: "postgresql",
  dbCredentials: {
    // DATABASE_URL is set by Forge's /db/provision flow (local-self-host) or
    // the BYO Supabase connect flow (hosted). It carries a role password —
    // never log this, never echo to chat.
    url: process.env.DATABASE_URL!,
  },
  // The role from /db/provision has schema-scoped grants; \`search_path\` in
  // the URL itself makes Drizzle write to the right schema with no prefix.
} satisfies Config
EOF

  # 5c. {ROOT}/lib/db/schema.ts — Postgres column types.
  #     serial PK = autoincrement integer. timestamp({ withTimezone: true })
  #     for created/updated. Default via SQL CURRENT_TIMESTAMP.
  if [ ! -f "$ROOT/lib/db/schema.ts" ]; then
cat > "$ROOT/lib/db/schema.ts" <<'EOF'
// Forge schema — one source of truth (Drizzle + Postgres).
import { pgTable, serial, text, boolean, timestamp } from "drizzle-orm/pg-core"
import { sql } from "drizzle-orm"

export const items = pgTable("items", {
  id:        serial("id").primaryKey(),
  name:      text("name").notNull(),
  notes:     text("notes"),
  done:      boolean("done").notNull().default(false),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().default(sql`CURRENT_TIMESTAMP`),
  updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().default(sql`CURRENT_TIMESTAMP`),
})

export type Item    = typeof items.$inferSelect
export type ItemNew = typeof items.$inferInsert
EOF
  fi

  # 5d. {ROOT}/app/api/items/route.ts (canonical CRUD example)
  #     Drizzle pg syntax — `.returning()` works the same, `desc` import same.
  mkdir -p "$ROOT/app/api/items"
  if [ ! -f "$ROOT/app/api/items/route.ts" ]; then
cat > "$ROOT/app/api/items/route.ts" <<'EOF'
import { NextRequest, NextResponse } from "next/server"
import { db } from "@/lib/db/client"
import { items } from "@/lib/db/schema"
import { desc } from "drizzle-orm"

export async function GET() {
  const rows = await db.select().from(items).orderBy(desc(items.createdAt))
  return NextResponse.json(rows)
}

export async function POST(req: NextRequest) {
  const body = await req.json()
  const [row] = await db.insert(items).values(body).returning()
  return NextResponse.json(row, { status: 201 })
}
EOF
  fi

  mkdir -p "$ROOT/app/api/items/[id]"
  if [ ! -f "$ROOT/app/api/items/[id]/route.ts" ]; then
cat > "$ROOT/app/api/items/[id]/route.ts" <<'EOF'
import { NextRequest, NextResponse } from "next/server"
import { db } from "@/lib/db/client"
import { items } from "@/lib/db/schema"
import { eq } from "drizzle-orm"

export async function PATCH(req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params
  const body = await req.json()
  const [row] = await db.update(items).set({
    ...body,
    // Postgres `timestamp({ withTimezone: true })` accepts Date — Drizzle
    // serializes for us. Don't stringify here; the SQLite-era code did.
    updatedAt: new Date(),
  }).where(eq(items.id, Number(id))).returning()
  return NextResponse.json(row)
}

export async function DELETE(_req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params
  await db.delete(items).where(eq(items.id, Number(id)))
  return NextResponse.json({ ok: true })
}
EOF
  fi

  # 5e. {ROOT}/lib/db/client.ts — pg Pool + Drizzle node-postgres adapter.
  #     ALWAYS rewritten (not gated on existence). Same reason as the
  #     drizzle.config.ts above: models will rewrite this back to
  #     better-sqlite3 given half a chance. Bootstrap re-asserts the pg
  #     client on every boot. The AI is free to add tables to schema.ts
  #     (5c, gated below) but cannot permanently change the driver.
cat > "$ROOT/lib/db/client.ts" <<'EOF'
// Forge-owned. Do NOT edit — bootstrap regenerates this file at every
// container start. Forge uses node-postgres (pg) only; any better-sqlite3
// / sqlite imports here get reset on next boot.
import { Pool } from "pg"
import { drizzle } from "drizzle-orm/node-postgres"
import * as schema from "./schema"

// HMR-safe singleton — Next.js dev mode reloads modules; reusing the Pool
// via globalThis prevents connection-pool fanout on every save. In prod the
// module loads once and the guard is a no-op.
const g = globalThis as unknown as { __forgePgPool?: Pool }
const pool = g.__forgePgPool ?? new Pool({
  connectionString: process.env.DATABASE_URL,
  // Small pool — each container gets at most this many concurrent queries.
  // Tuned for the local-self-host case where many runner containers share
  // one Postgres. Override via PGPOOL_MAX if you need more headroom.
  max: Number(process.env.PGPOOL_MAX ?? 5),
})
if (process.env.NODE_ENV !== "production") g.__forgePgPool = pool

export const db = drizzle(pool, { schema })
export { schema }
EOF

  # 5f. .env.local — placeholder so drizzle-kit doesn't crash on
  #     "DATABASE_URL is not defined". The real URL is written by the agent
  #     after calling POST /api/projects/{id}/db/provision (or after BYO
  #     Supabase connect). NEVER commit .env.local — already in the default
  #     Next.js .gitignore.
  if [ ! -f .env.local ]; then
cat > .env.local <<'EOF'
# DATABASE_URL is set by Forge after the agent calls /db/provision.
# Until then, drizzle-kit migrate will refuse to run. Run:
#   curl -X POST $FORGE_API_URL/api/projects/$PROJECT_ID/db/provision \
#        -H "Authorization: Bearer $FORGE_API_TOKEN"
# and write the returned `database_url` here, then `npm run db:migrate`.
#DATABASE_URL=
EOF
  fi

  # 5g. .env.example — committed-safe template for the project zip download.
  #     Documents the env contract for self-deployers (no secrets, no Forge-
  #     internal URLs). Whoever downloads the project sees this and knows
  #     what to set.
  if [ ! -f .env.example ]; then
cat > .env.example <<'EOF'
# Required. The Postgres connection string for your app's database.
# Local dev with Forge: this is set automatically by /db/provision.
# Self-deploy: point at your own Supabase (or any Postgres) instance:
#   postgresql://user:pass@host:5432/db?options=-csearch_path%3Dapp_<id>
DATABASE_URL=

# Optional. Tune the per-instance connection pool max. Default 5.
#PGPOOL_MAX=10
EOF
  fi

  # 5h. package.json: Postgres deps + Drizzle kit scripts.
  #
  #     Versions are FORCED, not ensured. Models keep installing old
  #     drizzle-orm (0.30.x) / drizzle-kit (0.20.x) which still understand
  #     SQLite — those versions accept `dialect: "sqlite"`, accept
  #     `generate:sqlite` subcommand, accept better-sqlite3 driver. Pinning
  #     a recent floor ELIMINATES the SQLite freelance path: drizzle-kit
  #     0.31+ uses the unified `generate` command (no `:sqlite` suffix to
  #     reach for) and drizzle-orm 0.44+ has clearer pg-only ergonomics in
  #     the example we ship.
  #
  #     Force (not ensure) — overrides any version the AI installed.
  #     Bootstrap then runs `pnpm install` which reconciles to these.
  #
  #     Drop better-sqlite3 + @types/better-sqlite3 if they're present from
  #     an old SQLite-era project — ~40MB image bloat + a tempting import
  #     for the model.
  node - <<'EOF'
const fs = require("fs")
const pj = JSON.parse(fs.readFileSync("package.json", "utf8"))
pj.dependencies = pj.dependencies || {}
pj.devDependencies = pj.devDependencies || {}
pj.scripts = pj.scripts || {}
const force = (o, k, v) => { o[k] = v }   // overwrite — not "if missing"
const remove = (o, k) => { delete o[k] }
// FORCE: Postgres-era drizzle + pg + dotenv. The exact versions are picked
// to (a) post-date the `generate:<dialect>` subcommand syntax, (b) match
// the bootstrap's pgTable example, (c) be released and stable as of this
// writing. Bumping the floor is a deliberate platform decision — coordinate
// with db.md skill content if you change these.
force(pj.dependencies, "drizzle-orm",     "^0.44.0")
force(pj.dependencies, "pg",              "^8.13.0")
force(pj.devDependencies, "drizzle-kit",  "^0.31.0")
force(pj.devDependencies, "@types/pg",    "^8.11.10")
force(pj.devDependencies, "dotenv-cli",   "^7.4.4")
force(pj.devDependencies, "tsx",          "^4.19.0")
// Remove SQLite-era deps if present (Phase B cleanup, repeated here as a
// boot-time backstop — pkg-guard rejects new installs, this purges old
// declarations that might survive from a freelance episode).
remove(pj.dependencies, "better-sqlite3")
remove(pj.devDependencies, "@types/better-sqlite3")
remove(pj.dependencies, "sqlite3")
remove(pj.dependencies, "@libsql/client")
remove(pj.dependencies, "prisma")
remove(pj.devDependencies, "prisma")
remove(pj.dependencies, "@prisma/client")
// db:* scripts: precheck → dotenv → drizzle-kit. The precheck script
// (installed at /usr/local/lib/forge-db-precheck.js by the runner image)
// validates drizzle.config.ts is Postgres-shaped and auto-rewrites it if
// the AI overrode it mid-session. Exit-1 from precheck halts the chain
// with a clear stderr message so the AI sees what's wrong.
//
// FORCE these (overwrite, not "ensure") — models love to overwrite the
// scripts with raw drizzle-kit invocations that bypass both dotenv AND
// the precheck. The force rewrites them on every container boot.
force(pj.scripts, "db:generate", "node /usr/local/lib/forge-db-precheck.js && dotenv -e .env.local -- drizzle-kit generate")
force(pj.scripts, "db:migrate",  "node /usr/local/lib/forge-db-precheck.js && dotenv -e .env.local -- drizzle-kit migrate")
force(pj.scripts, "db:studio",   "node /usr/local/lib/forge-db-precheck.js && dotenv -e .env.local -- drizzle-kit studio")
fs.writeFileSync("package.json", JSON.stringify(pj, null, 2) + "\n")
EOF

  # 5i. Lock the Forge-owned DB files read-only. The AI can still try to
  # edit them (Edit/Write tool gets EACCES; bash redirect fails the same
  # way; `chmod` and `rm` on these paths are denied in opencode's bash
  # permissions). The precheck script above is the reactive backstop if
  # any of these locks ever fail. Bootstrap runs every container start so
  # the perms are re-applied even if something stripped them.
  chmod 444 drizzle.config.ts                     2>/dev/null || true
  chmod 444 "$ROOT/lib/db/client.ts"              2>/dev/null || true
  [ -f postcss.config.mjs ] && chmod 444 postcss.config.mjs 2>/dev/null || true
  [ -f next.config.ts ] && chmod 444 next.config.ts 2>/dev/null || true

  # NOTE: drizzle-kit generate/migrate is NOT run here. drizzle-kit needs
  # node_modules + DATABASE_URL to exist. Both happen later via
  # forge-enable-db.sh after the agent has called /db/provision and written
  # the URL to .env.local. Running blind here just emits noisy errors.

  echo "[forge-bootstrap] DB scaffolding ready (Drizzle/Postgres)"
else
  echo "[forge-bootstrap] DB disabled — skipping Drizzle scaffold. Touch .forge/db-enabled to enable."
fi

echo "[forge-bootstrap] done"
