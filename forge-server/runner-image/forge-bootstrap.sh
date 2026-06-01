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
if [ -f next.config.ts ] || [ -f next.config.js ] || [ -f next.config.mjs ]; then
  node - <<'EOF'
const fs = require("fs")
const CANONICAL = `import type { NextConfig } from "next"

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
  serverExternalPackages: ["better-sqlite3"],
}

export default nextConfig
`
  const target = fs.existsSync("next.config.ts") ? "next.config.ts"
               : fs.existsSync("next.config.mjs") ? "next.config.mjs"
               : "next.config.js"
  const cur = fs.readFileSync(target, "utf8")
  const hasOrigins = /allowedDevOrigins\s*:/.test(cur)
  const hasExternal = /serverExternalPackages\s*:/.test(cur)
  if (hasOrigins && hasExternal) process.exit(0)
  const customKeys = (cur.match(/^\s*\w+\s*:/gm) || []).filter(
    k => !/^(devIndicators|allowedDevOrigins|serverExternalPackages)\s*:/.test(k.trim())
  )
  if (customKeys.length === 0 && target === "next.config.ts") {
    fs.writeFileSync(target, CANONICAL)
    console.log("[forge-bootstrap] next.config.ts patched with mandatory keys")
  } else {
    console.warn("[forge-bootstrap] WARN: next.config has custom keys; cannot")
    console.warn("[forge-bootstrap] WARN: auto-patch. Add allowedDevOrigins and")
    console.warn("[forge-bootstrap] WARN: serverExternalPackages manually (see AGENTS.md).")
  }
EOF
elif is_next_project; then
cat > next.config.ts <<'EOF'
import type { NextConfig } from "next"

const nextConfig: NextConfig = {
  devIndicators: false,
  allowedDevOrigins: [
    "*.preview.lvh.me",
    "*.preview.forge.com",
  ],
  serverExternalPackages: ["better-sqlite3"],
}

export default nextConfig
EOF
  echo "[forge-bootstrap] next.config.ts created with mandatory keys"
fi

# ── 4. instrumentation-client.ts (always for Next projects) ──────────────────
# Captures browser-side errors and postMessages them to window.parent. The
# Forge UI's preview iframe parent listens and forwards to forge-server.
# NOTE: marker bumped v1 → v2 to add the on-demand screenshot handler used by
# the Forge UI "Fix this" button. Existing v1 files will be regenerated to v2
# on next container start; the grep below matches BOTH so old projects upgrade.
INSTR_MARKER="// forge:runtime-error-bridge:v"
if is_next_project; then
  if [ ! -f instrumentation-client.ts ] \
     || grep -q "$INSTR_MARKER" instrumentation-client.ts; then
cat > instrumentation-client.ts <<'EOF'
// forge:runtime-error-bridge:v2
// Forge-owned. Auto-regenerated by forge-bootstrap if missing or unchanged.
// If you need custom client instrumentation, add it BELOW the bridge block —
// do not remove the bridge.
(() => {
  if (typeof window === "undefined") return
  if ((window as unknown as { __forgeBridgeInstalled?: boolean }).__forgeBridgeInstalled) return
  ;(window as unknown as { __forgeBridgeInstalled?: boolean }).__forgeBridgeInstalled = true

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

  const send = (p: Payload) => {
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
})()

export {}
EOF
    echo "[forge-bootstrap] instrumentation-client.ts (re)generated"
  fi
fi

# ── 5. DB scaffolding (OPT-IN) ───────────────────────────────────────────────
# DB is NOT a default. Forge no longer assumes every app needs persistence —
# the agent enables DB only when the user explicitly asks for it.
#
# DB is enabled if ANY of these are true:
#   - .forge/db-enabled marker file exists (set by the agent on first DB ask)
#   - data.db already exists (back-compat for old projects)
#   - drizzle/*.sql migrations exist (back-compat)
#
# When disabled (default): we write no DB files, add no DB deps, run no
# drizzle-kit migrate. The user's app is a plain Next + Tailwind project.
DB_ENABLED=0
if [ -f .forge/db-enabled ] || [ -f data.db ] || ls drizzle/*.sql >/dev/null 2>&1; then
  DB_ENABLED=1
fi

if [ "$DB_ENABLED" = "1" ]; then
  mkdir -p "$ROOT/lib/db" drizzle

  # 5a. FORGE_DB.md (workspace marker the agent sees)
  if [ ! -f FORGE_DB.md ]; then
cat > FORGE_DB.md <<'EOF'
# HOW TO BUILD A DATA APP IN THIS WORKSPACE

The plumbing is already done. Your job is to COPY the existing `items`
example and rename it to whatever the user asked for. Follow this recipe
exactly — it works every time.

---

## RECIPE: User wants a "tracker / list / app for managing X"

(Replace `X` with the actual resource — books, tasks, contacts, expenses, etc.)

### Step 1 — Add the table to `lib/db/schema.ts`

The file already exports an `items` table. Add a new `sqliteTable("X", {...})`
right below it with whatever fields the user described. **Do not delete the
`items` table** — leave it as a reference.

### Step 2 — Generate + apply the migration

```bash
npx drizzle-kit generate
npx drizzle-kit migrate
```

Both are safe to re-run.

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
- ❌ `import Database from "better-sqlite3"` anywhere except `client.ts`
- ❌ Raw `CREATE TABLE` DDL
- ❌ Prisma / TypeORM / Sequelize / Mongoose / raw `pg`

If you write any of the above, the Data tab will be empty and "Migrate to
Supabase" won't work.

**Use Drizzle. Copy `items`. Rename. Done.**
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

  # 5b. drizzle.config.ts
  if [ ! -f drizzle.config.ts ]; then
cat > drizzle.config.ts <<EOF
import type { Config } from "drizzle-kit"

export default {
  schema:  "${SCHEMA_REL}",
  out:     "./drizzle",
  dialect: "sqlite",
  dbCredentials: { url: "./data.db" },
} satisfies Config
EOF
  fi

  # 5c. {ROOT}/lib/db/schema.ts
  if [ ! -f "$ROOT/lib/db/schema.ts" ]; then
cat > "$ROOT/lib/db/schema.ts" <<'EOF'
// Forge schema — one source of truth.
import { sqliteTable, integer, text } from "drizzle-orm/sqlite-core"
import { sql } from "drizzle-orm"

export const items = sqliteTable("items", {
  id:        integer("id").primaryKey({ autoIncrement: true }),
  name:      text("name").notNull(),
  notes:     text("notes"),
  done:      integer("done", { mode: "boolean" }).notNull().default(false),
  createdAt: text("created_at").notNull().default(sql`(datetime('now'))`),
  updatedAt: text("updated_at").notNull().default(sql`(datetime('now'))`),
})

export type Item    = typeof items.$inferSelect
export type ItemNew = typeof items.$inferInsert
EOF
  fi

  # 5d. {ROOT}/app/api/items/route.ts (canonical CRUD example)
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
    updatedAt: new Date().toISOString(),
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

  # 5e. {ROOT}/lib/db/client.ts
  if [ ! -f "$ROOT/lib/db/client.ts" ]; then
cat > "$ROOT/lib/db/client.ts" <<'EOF'
import Database from "better-sqlite3"
import { drizzle } from "drizzle-orm/better-sqlite3"
import path from "path"
import * as schema from "./schema"

const sqlite = new Database(path.join(process.cwd(), "data.db"))
sqlite.pragma("journal_mode = WAL")
sqlite.pragma("foreign_keys = ON")

export const db = drizzle(sqlite, { schema })
export { schema }
EOF
  fi

  # 5f. package.json: add Drizzle deps and scripts if missing
  node - <<'EOF'
const fs = require("fs")
const pj = JSON.parse(fs.readFileSync("package.json", "utf8"))
pj.dependencies = pj.dependencies || {}
pj.devDependencies = pj.devDependencies || {}
pj.scripts = pj.scripts || {}
const ensure = (o, k, v) => { if (!o[k]) o[k] = v }
ensure(pj.dependencies, "drizzle-orm",     "^0.44.0")
ensure(pj.dependencies, "better-sqlite3",  "^12.0.0")
ensure(pj.devDependencies, "drizzle-kit",  "^0.31.0")
ensure(pj.devDependencies, "@types/better-sqlite3", "^7.6.13")
ensure(pj.devDependencies, "tsx",          "^4.19.0")
ensure(pj.scripts, "db:generate", "drizzle-kit generate")
ensure(pj.scripts, "db:migrate",  "drizzle-kit migrate")
ensure(pj.scripts, "db:studio",   "drizzle-kit studio")
fs.writeFileSync("package.json", JSON.stringify(pj, null, 2) + "\n")
EOF

  # NOTE: drizzle-kit generate/migrate is NOT run here. drizzle-kit needs
  # node_modules to exist, and that doesn't happen until the install step
  # in the Dockerfile CMD (or forge-enable-db.sh). Running generate here
  # against an empty workspace just emits the noisy "Please install latest
  # version of drizzle-orm" error the user kept seeing. generate/migrate
  # belong AFTER install — see forge-enable-db.sh.

  echo "[forge-bootstrap] DB scaffolding ready (Drizzle/SQLite)"
else
  echo "[forge-bootstrap] DB disabled — skipping Drizzle scaffold. Touch .forge/db-enabled to enable."
fi

echo "[forge-bootstrap] done"
