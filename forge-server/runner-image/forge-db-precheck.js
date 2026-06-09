#!/usr/bin/env node
/**
 * forge-db-precheck — runs before drizzle-kit commands via npm run db:*.
 *
 * Why this exists:
 *   The bootstrap force-rewrites drizzle.config.ts on every container START
 *   (it's Forge-owned). But the AI can edit the file mid-session (e.g. to
 *   set dialect: "sqlite" + dbCredentials: { url: "data.db" }) and then
 *   run drizzle-kit against that wrong config — the bootstrap doesn't
 *   re-run until container restart. That gap is how the SQLite freelance
 *   path keeps slipping through.
 *
 *   This script closes the gap. It runs as the FIRST step of every
 *   `npm run db:generate` / `db:migrate` / `db:studio` invocation (wired
 *   into the scripts by the bootstrap's package.json patch). At that
 *   moment, it:
 *
 *     1. Reads drizzle.config.ts.
 *     2. Checks for `dialect: "postgresql"` AND absence of SQLite markers
 *        (`dialect: "sqlite"`, `better-sqlite3`, `data.db`, `sqlite:`).
 *     3. If correct → exit 0 silently, drizzle-kit proceeds.
 *     4. If wrong → auto-rewrites the file to the canonical Postgres
 *        config AND exits 1 with a clear stderr message naming exactly
 *        what was wrong and what to fix in schema.ts. AI sees the error,
 *        adjusts on the next turn, re-runs the script.
 *
 * Defense layers above this one (preventative):
 *   - Bootstrap force-writes the canonical config at container start
 *   - Bootstrap chmod 444's the file so Edit/Write tool gets EACCES
 *   - opencode "edit" perm denies write/edit on drizzle.config.ts
 *   - opencode "bash" perm denies chmod/rm/mv on drizzle.config.ts
 *
 * This precheck is the reactive layer — even if all four above fail, the
 * model running `db:generate` against bad config triggers the auto-heal.
 */

const fs = require("fs")
const path = require("path")

const CWD = process.cwd()
const CONFIG_PATH = path.join(CWD, "drizzle.config.ts")

// Locate the schema file the user's project actually has. Bootstrap auto-
// detects no-src vs src/ layout; mirror that here so the rewritten config
// points at the right place. Falls back to lib/db/schema.ts if neither
// exists yet (precheck is also the safety net for brand-new projects).
function locateSchema() {
  for (const rel of ["./lib/db/schema.ts", "./src/lib/db/schema.ts"]) {
    if (fs.existsSync(path.join(CWD, rel))) return rel
  }
  return "./lib/db/schema.ts"
}

const SCHEMA_REL = locateSchema()

const CANONICAL = `// Forge-owned. Do NOT edit — bootstrap + db:* scripts re-assert this on
// every container boot AND on every db:generate / db:migrate invocation.
// Any non-postgresql dialect here is reset automatically; SQLite is gone.
import type { Config } from "drizzle-kit"

export default {
  schema:  "${SCHEMA_REL}",
  out:     "./drizzle",
  dialect: "postgresql",
  dbCredentials: {
    // DATABASE_URL is set by Forge's /db/provision flow. Carries a role
    // password — never log, never echo to chat.
    url: process.env.DATABASE_URL!,
  },
} satisfies Config
`

let current = ""
try {
  current = fs.readFileSync(CONFIG_PATH, "utf8")
} catch {
  // File doesn't exist. Treat as "wrong" → fall through to auto-write.
}

// Strip JS line + block comments before pattern-matching. Otherwise the
// canonical's own header comments (which mention the dialect names by
// design) trip the SQLite/MySQL detectors and we false-positive a healthy
// file on every invocation. The strip is intentionally simple — string
// literals containing `//` would be miscounted as comments, but no
// drizzle.config.ts in the wild has `//` inside a string literal, so this
// is safe for the use case.
const code = current
  .replace(/\/\*[\s\S]*?\*\//g, "")   // /* block */
  .replace(/\/\/[^\n]*/g, "")          // // line

// Whitespace-tolerant patterns. Catch both single and double quotes; tolerate
// trailing commas, etc. Run against `code` (comments stripped) not `current`.
const HAS_PG_DIALECT      = /dialect\s*:\s*['"]postgresql['"]/.test(code)
const HAS_SQLITE_DIALECT  = /dialect\s*:\s*['"]sqlite['"]/.test(code)
const HAS_BETTER_SQLITE   = /better-sqlite3/.test(code)
const HAS_DATA_DB         = /['"]data\.db['"]|url\s*:\s*['"][^'"]*\.db['"]/.test(code)
const HAS_MYSQL_DIALECT   = /dialect\s*:\s*['"]mysql['"]/.test(code)

const isCorrect = HAS_PG_DIALECT
  && !HAS_SQLITE_DIALECT
  && !HAS_BETTER_SQLITE
  && !HAS_DATA_DB
  && !HAS_MYSQL_DIALECT

// Second check: DATABASE_URL in .env.local must exist and look real.
// Models keep writing placeholder values like `postgres://user:password@...`
// then asking the user to "provide a valid DATABASE_URL" — which the user
// cannot do (no shell, no .env access). The actual fix is to call the
// Forge platform endpoint /db/provision. Detect the placeholder and bail
// with a stderr message that names that endpoint explicitly.
const ENV_LOCAL = path.join(CWD, ".env.local")
let envLocalText = ""
try { envLocalText = fs.readFileSync(ENV_LOCAL, "utf8") } catch { /* missing — fall through */ }

const dbUrlMatch  = envLocalText.match(/^\s*DATABASE_URL\s*=\s*(.+?)\s*$/m)
const dbUrlValue  = dbUrlMatch ? dbUrlMatch[1].replace(/^['"]|['"]$/g, "") : ""

// Placeholders the model reaches for from training data. Add to this set
// as new variants show up. The pattern `user:password` (the literal word
// "password") is the strongest signal — no real generated password contains
// the word "password". Empty string is also rejected.
const PLACEHOLDER_PATTERNS = [
  /^$/,                                    // empty
  /:password@/i,                            // user:password placeholder
  /:secret@/i,                              // user:secret placeholder
  /:changeme/i,                             // user:changeme
  /^postgresql?:\/\/user@/i,                // bare "user" role (will hit "role user does not exist")
  /^postgresql?:\/\/postgres@/i,            // bare "postgres" superuser (banned in Forge anyway)
  /your[_-]?(?:db|database|password)/i,     // "your_database", "your-password"
  /\b(?:placeholder|example|todo|fixme)\b/i,
  /\$\{[^}]*DATABASE_URL[^}]*\}/,           // unresolved template like ${DATABASE_URL}
]

const isPlaceholderUrl = PLACEHOLDER_PATTERNS.some((p) => p.test(dbUrlValue))
const isMissingUrl     = !dbUrlMatch || dbUrlValue.length === 0
const isProvisionedUrl = /^postgresql?:\/\/app_[0-9a-f]{8}:/i.test(dbUrlValue)

const dbUrlOk = isProvisionedUrl || (!isPlaceholderUrl && !isMissingUrl)

if (isCorrect && dbUrlOk) {
  // Happy path — drizzle.config.ts is canonical AND DATABASE_URL is real.
  // Drizzle-kit runs next in the script chain. Exit silently.
  process.exit(0)
}

// DATABASE_URL failed validation? Fail loud with the platform-endpoint
// instruction so the AI knows exactly what to call. This case is separate
// from the drizzle.config.ts rewrite below — handle it first because if
// the URL is missing/placeholder, fixing drizzle.config.ts doesn't help.
if (!dbUrlOk) {
  const reason = isMissingUrl
    ? "DATABASE_URL is missing from .env.local"
    : `DATABASE_URL in .env.local is a placeholder (${
        dbUrlValue.length > 60 ? dbUrlValue.slice(0, 60) + "..." : dbUrlValue
      })`
  console.error("")
  console.error(`[forge-db-precheck] ${reason}.`)
  console.error("")
  console.error("  You CANNOT ask the user to provide a DATABASE_URL — they have no shell,")
  console.error("  no terminal, no .env access. The Forge platform provisions it. Call:")
  console.error("")
  console.error("    PROJECT_ID=$(pwd | sed -n 's|.*/projects/\\([^/]*\\)/workspace.*|\\1|p')")
  console.error("    RESP=$(curl -sS -X POST \"$FORGE_API_URL/api/projects/$PROJECT_ID/db/provision\" \\")
  console.error("                 -H \"Authorization: Bearer $FORGE_API_TOKEN\")")
  console.error("    URL=$(echo \"$RESP\" | python3 -c \"import json,sys; print(json.load(sys.stdin)['database_url'])\")")
  console.error("    printf 'DATABASE_URL=%s\\n' \"$URL\" >> .env.local")
  console.error("")
  console.error("  NEVER echo $URL or $RESP in chat — the URL contains a role password.")
  console.error("  After writing .env.local, re-run this command.")
  console.error("")
  console.error("  If this error appeared because /api/projects/.../runtime-errors showed")
  console.error("  'role \"X\" does not exist' or 'database \"X\" does not exist', that")
  console.error("  error has the same fix: the URL is a placeholder, call /db/provision.")
  console.error("")
  process.exit(1)
}

// Diagnose what was wrong, then rewrite. Diagnosis goes in the error so the
// AI knows precisely what its previous edit got wrong.
const wrongSignals = []
if (HAS_SQLITE_DIALECT)  wrongSignals.push(`dialect: "sqlite" (Forge dropped SQLite — D9)`)
if (HAS_BETTER_SQLITE)   wrongSignals.push("references to better-sqlite3 (banned)")
if (HAS_DATA_DB)         wrongSignals.push(`dbCredentials.url pointing at a .db file (Forge uses Postgres only)`)
if (HAS_MYSQL_DIALECT)   wrongSignals.push(`dialect: "mysql" (Forge is Postgres only)`)
if (!HAS_PG_DIALECT && wrongSignals.length === 0) {
  wrongSignals.push(current.trim() === ""
    ? "drizzle.config.ts is empty or missing"
    : `dialect: "postgresql" is missing or malformed`)
}

// Rewrite. Defeat any chmod 444 the bootstrap may have applied (we own
// this file — relaxing perm then forcing perm back is fine).
try {
  if (fs.existsSync(CONFIG_PATH)) {
    try { fs.chmodSync(CONFIG_PATH, 0o644) } catch { /* fs may not support */ }
  }
  fs.writeFileSync(CONFIG_PATH, CANONICAL)
  try { fs.chmodSync(CONFIG_PATH, 0o444) } catch { /* belt-and-braces */ }
} catch (err) {
  console.error(`[forge-db-precheck] FATAL: cannot rewrite drizzle.config.ts (${err.message})`)
  console.error("[forge-db-precheck] The file may be locked at the filesystem level. Restart the container.")
  process.exit(2)
}

// Fail loud so the AI sees the correction and fixes downstream code.
// stderr is shown verbatim in the tool-output panel — the model reads it.
console.error("")
console.error("[forge-db-precheck] drizzle.config.ts was wrong — auto-corrected.")
console.error("")
console.error("  What was wrong:")
for (const s of wrongSignals) {
  console.error(`    - ${s}`)
}
console.error("")
console.error("  What's now in drizzle.config.ts:")
console.error(`    dialect: "postgresql"`)
console.error(`    schema:  "${SCHEMA_REL}"`)
console.error(`    dbCredentials: { url: process.env.DATABASE_URL! }`)
console.error("")
console.error("  Forge uses Drizzle + node-postgres ONLY (LAUNCH_PLAN D9 — SQLite is gone).")
console.error("  Your schema file MUST import from drizzle-orm/pg-core and use pgTable(),")
console.error("  NOT drizzle-orm/sqlite-core / sqliteTable(). If you have SQLite imports in")
console.error(`  ${SCHEMA_REL}, convert them now:`)
console.error("")
console.error("    sqliteTable → pgTable")
console.error("    integer(\"id\").primaryKey({ autoIncrement: true }) → serial(\"id\").primaryKey()")
console.error("    integer(\"x\", { mode: \"boolean\" }) → boolean(\"x\")")
console.error("    text(\"created_at\").default(sql`(datetime('now'))`) → timestamp(\"created_at\", { withTimezone: true }).default(sql`CURRENT_TIMESTAMP`)")
console.error("")
console.error("  Then re-run this command.")
console.error("")

process.exit(1)
