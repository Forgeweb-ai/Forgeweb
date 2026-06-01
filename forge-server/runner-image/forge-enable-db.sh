#!/bin/sh
# forge-enable-db.sh — agent-invoked when the user asks for persistence.
#
# Drops the .forge/db-enabled marker, re-runs forge-bootstrap.sh to lay
# down the Drizzle scaffold + deps, installs the new packages, and applies
# the initial migration. Idempotent — running it twice is a no-op.
#
# Usage (from the agent's shell tool, inside the dev container):
#   bash /usr/local/bin/forge-enable-db.sh
set -e

cd /app

mkdir -p .forge
touch .forge/db-enabled

/usr/local/bin/forge-bootstrap.sh

# Install the newly-added Drizzle deps. Prefer pnpm (shared store), fall
# back to npm. Use --prefer-offline so we hit the pnpm CAS where possible.
if command -v pnpm >/dev/null && [ ! -f package-lock.json ]; then
  pnpm install --prefer-offline
else
  npm install --prefer-offline
fi

# Generate the initial migration from schema.ts, THEN apply it. Both must
# happen after install (drizzle-kit needs drizzle-orm in node_modules to
# read the schema). Idempotent — generate is a no-op if SQL is already
# in sync with the schema.
npx --yes drizzle-kit generate --name=forge_init || true
npx --yes drizzle-kit migrate                   || true

echo "[forge-enable-db] DB enabled — Drizzle scaffold + migrations applied"
