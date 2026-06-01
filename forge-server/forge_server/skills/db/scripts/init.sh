#!/usr/bin/env bash
# Idempotently scaffold the Drizzle skeleton inside a Forge workspace.
# Safe to run multiple times — won't overwrite existing files.
set -euo pipefail

if [[ ! -f package.json ]]; then
  echo "init.sh: no package.json — run this at the workspace root" >&2
  exit 1
fi

mkdir -p src/lib/db drizzle

# ── drizzle.config.ts ────────────────────────────────────────────────────────
if [[ ! -f drizzle.config.ts ]]; then
cat > drizzle.config.ts <<'EOF'
import type { Config } from "drizzle-kit"

export default {
  schema:  "./src/lib/db/schema.ts",
  out:     "./drizzle",
  dialect: "sqlite",
  dbCredentials: { url: "./data.db" },
} satisfies Config
EOF
fi

# ── src/lib/db/schema.ts ─────────────────────────────────────────────────────
if [[ ! -f src/lib/db/schema.ts ]]; then
cat > src/lib/db/schema.ts <<'EOF'
// All Forge schema lives here. One source of truth.
// Use ONLY Drizzle's dialect-agnostic column builders — they let Forge
// translate this schema to Postgres when the user clicks "Migrate to Supabase".
import { sqliteTable, integer, text } from "drizzle-orm/sqlite-core"
import { sql } from "drizzle-orm"

// Placeholder so `drizzle-kit generate` has something to work with on a
// fresh project. Delete or replace when the agent adds real tables.
export const _forge_meta = sqliteTable("_forge_meta", {
  id:        integer("id").primaryKey({ autoIncrement: true }),
  key:       text("key").notNull().unique(),
  value:     text("value"),
  createdAt: text("created_at").notNull().default(sql`(datetime('now'))`),
})
EOF
fi

# ── src/lib/db/client.ts ─────────────────────────────────────────────────────
if [[ ! -f src/lib/db/client.ts ]]; then
cat > src/lib/db/client.ts <<'EOF'
// Single shared Drizzle client. Do NOT instantiate better-sqlite3 elsewhere.
// Forge's "Migrate to Supabase" tool rewrites THIS file (and only this file)
// to swap the driver — everything else in the app keeps working untouched.
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

# ── src/lib/db/migrate.ts ────────────────────────────────────────────────────
if [[ ! -f src/lib/db/migrate.ts ]]; then
cat > src/lib/db/migrate.ts <<'EOF'
// Apply pending drizzle migrations at boot. Imported by client.ts callers
// that want migrations auto-applied (e.g. dev server startup hooks).
import Database from "better-sqlite3"
import { drizzle } from "drizzle-orm/better-sqlite3"
import { migrate } from "drizzle-orm/better-sqlite3/migrator"
import path from "path"

const sqlite = new Database(path.join(process.cwd(), "data.db"))
const db = drizzle(sqlite)
migrate(db, { migrationsFolder: path.join(process.cwd(), "drizzle") })
sqlite.close()
console.log("[forge-db] migrations applied")
EOF
fi

# ── package.json deps ────────────────────────────────────────────────────────
# Add deps if missing. We don't run npm install — the container handles that.
node - <<'EOF'
const fs = require("fs")
const pj = JSON.parse(fs.readFileSync("package.json", "utf8"))
pj.dependencies = pj.dependencies || {}
pj.devDependencies = pj.devDependencies || {}
const dep = (o, k, v) => { if (!o[k]) o[k] = v }
dep(pj.dependencies, "drizzle-orm",     "^0.44.0")
dep(pj.dependencies, "better-sqlite3",  "^12.0.0")
dep(pj.devDependencies, "drizzle-kit",  "^0.31.0")
dep(pj.devDependencies, "@types/better-sqlite3", "^7.6.13")
dep(pj.devDependencies, "tsx",          "^4.19.0")
pj.scripts = pj.scripts || {}
dep(pj.scripts, "db:generate", "drizzle-kit generate")
dep(pj.scripts, "db:migrate",  "drizzle-kit migrate")
dep(pj.scripts, "db:studio",   "drizzle-kit studio")
fs.writeFileSync("package.json", JSON.stringify(pj, null, 2) + "\n")
EOF

echo "init.sh: Drizzle scaffold ready"
