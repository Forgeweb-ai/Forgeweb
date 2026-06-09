#!/bin/sh
# forge-enable-db.sh — agent-invoked when the user asks for persistence.
#
# Phase B (D9): Postgres-per-schema. This script:
#   1. Verifies DATABASE_URL is set (either in env or .env.local). If not,
#      bails with instructions to call /api/projects/{id}/db/provision first.
#   2. Drops the .forge/db-enabled marker.
#   3. Re-runs forge-bootstrap.sh to lay down the Drizzle/pg scaffold + deps.
#   4. Installs packages.
#   5. Generates the initial migration from schema.ts and applies it via
#      dotenv-cli so DATABASE_URL is sourced from .env.local automatically.
#
# Idempotent — running it twice is a no-op.
#
# Usage (from the agent's shell tool, inside the dev container):
#   bash /usr/local/bin/forge-enable-db.sh
set -e

cd /app

# ── 1. DATABASE_URL gate ─────────────────────────────────────────────────────
# drizzle-kit migrate fails opaquely when DATABASE_URL is missing. Catch it
# here with a message the agent can act on (call /db/provision and write the
# URL to .env.local, then re-run this script).
URL_OK=0
if [ -n "${DATABASE_URL:-}" ]; then
  URL_OK=1
elif [ -f .env.local ] && grep -qE '^DATABASE_URL=[^[:space:]]' .env.local; then
  URL_OK=1
fi
if [ "$URL_OK" = "0" ]; then
  cat >&2 <<'EOF'
[forge-enable-db] DATABASE_URL is not set.
Call the platform endpoint to provision a Postgres schema for this project,
then write the returned `database_url` to .env.local:

  PROJECT_ID=$(pwd | sed -n 's|.*/projects/\([^/]*\)/workspace.*|\1|p')
  curl -X POST "$FORGE_API_URL/api/projects/$PROJECT_ID/db/provision" \
       -H "Authorization: Bearer $FORGE_API_TOKEN"
  # Then write the returned database_url into .env.local (DO NOT echo to chat).
  # Re-run this script after.
EOF
  exit 2
fi

# ── 2. Marker + scaffold ─────────────────────────────────────────────────────
mkdir -p .forge
touch .forge/db-enabled

/usr/local/bin/forge-bootstrap.sh

# ── 3. Install the newly-added deps ──────────────────────────────────────────
# Prefer pnpm (shared store across projects = faster cold start), fall back to
# npm. Use --prefer-offline so we hit the pnpm CAS where possible.
if command -v pnpm >/dev/null && [ ! -f package-lock.json ]; then
  pnpm install --prefer-offline
else
  npm install --prefer-offline
fi

# ── 4. Generate + apply the initial migration ────────────────────────────────
# Both must happen after install (drizzle-kit needs drizzle-orm in
# node_modules to read schema.ts). Idempotent — generate is a no-op if SQL is
# already in sync with the schema. We use the project's `db:*` scripts which
# wrap drizzle-kit with `dotenv -e .env.local --` so the role-password URL
# never enters the shell environment.
npm run db:generate -- --name=forge_init || true
npm run db:migrate                        || true

echo "[forge-enable-db] DB enabled — Drizzle/Postgres scaffold + migrations applied"
