# forge-runner:latest
# ===================
# Base image for all project dev-server containers.
#
# What's in it:
#   - Node 20 LTS + npm + npx + pnpm (via corepack)
#   - Python 3 + build tools (for full-stack projects with a Python BE)
#   - Common global tools: vite, next, tsx, ts-node
#
# What's NOT in it:
#   - Any project files (mounted at /app at runtime)
#   - node_modules (mounted from per-project named volume at /app/node_modules)
#   - The pnpm package store (mounted from a SHARED named volume at /forge-store)
#
# Disk-savings model (see [[forge_storage_architecture]]):
#   Every container shares the /forge-store content-addressable store. Each
#   project's node_modules is a tree of hardlinks/symlinks into that store.
#   100 projects all importing React 18.3.1 → one copy of React on disk, 100
#   pointers. When a new package is installed by any project, pnpm transparently
#   downloads it into the shared store and links it. Subsequent installs by
#   other projects find it already there.
#
# Key env vars set here (can be overridden at container create time):
#   BROWSER=none        — never open a browser tab
#   CI=true             — disables interactive prompts
#   PORT=3000           — all frameworks use this
#   PNPM_HOME           — pnpm's own dir (binaries, config)
#   PNPM_STORE_DIR      — content-addressable store; MUST match the
#                         mount path of the shared volume in container_manager.py

FROM node:20-alpine

# Install Python + build tools for native modules. git for any deps that pull
# from VCS at install time.
RUN apk add --no-cache python3 py3-pip gcc musl-dev g++ make git

# pnpm: install via corepack (built into Node 20+). Pin a version so behavior
# is reproducible across rebuilds.
RUN corepack enable && corepack prepare pnpm@9.12.0 --activate

# Global Node tools — kept for projects that don't use pnpm yet (legacy npm
# install fallback in the CMD below).
#
# Babel parser/traverse/generator/types are installed globally so the Forge
# source-stamp loader (visual edits — see forge-source-stamp/) can require()
# them without polluting every generated project's package.json. They live
# under /usr/local/lib/node_modules and resolve from the loader's location.
RUN npm install -g \
    vite \
    typescript \
    tsx \
    ts-node \
    @types/node \
    @babel/parser \
    @babel/traverse \
    @babel/generator \
    @babel/types \
    2>/dev/null || true

# ── Forge source-stamp loader (visual edits) ─────────────────────────────────
# Baked into the image at a stable absolute path so every project's
# next.config.ts can reference it directly. Zero per-project file copy,
# zero node_modules pollution, image rebuild = upgrade across all containers.
COPY forge-source-stamp/ /usr/local/lib/forge-source-stamp/

WORKDIR /app

# ── Forge bootstrap ──────────────────────────────────────────────────────────
# At every container start, lays down the Forge-owned files:
#   - canonical Tailwind v4 setup    (always — fights model hallucinations)
#   - canonical next.config.ts keys  (always — required for preview iframe)
#   - browser-side error bridge      (always — feeds the runtime-errors store)
#   - Drizzle DB scaffold            (OPT-IN — only when .forge/db-enabled
#                                     exists, set by forge-enable-db.sh)
COPY forge-bootstrap.sh /usr/local/bin/forge-bootstrap.sh
COPY forge-enable-db.sh /usr/local/bin/forge-enable-db.sh
RUN chmod +x /usr/local/bin/forge-bootstrap.sh /usr/local/bin/forge-enable-db.sh

# ── Package guard ────────────────────────────────────────────────────────────
# Wraps npm / pnpm / npx and rejects any install of Forge-banned packages
# (better-sqlite3, prisma, sequelize, typeorm, mongoose, raw sqlite, etc.).
# The wrapper points the model at the right recipe (/forge-skills/db.md +
# /db/provision) so the error is self-healing on the next turn.
#
# Mechanism: rename the real binaries to _real_<cmd>, drop the wrapper at
# the original path. PATH-order doesn't change, so every existing script,
# the bootstrap, and the runtime CMD all transparently go through the
# wrapper. The wrapper invokes the real binary on the allow path.
#
# Why this needs the rename rather than a separate PATH dir: corepack's pnpm
# shim is also at /usr/local/bin/pnpm, and shadowing it from /usr/local/sbin
# is fragile across alpine PATH conventions. Direct binary swap is the only
# approach that works for both npm (node-installed) and pnpm (corepack shim)
# without surprises.
COPY forge-pkg-guard.sh /usr/local/bin/forge-pkg-guard
RUN chmod +x /usr/local/bin/forge-pkg-guard && \
    for cmd in npm pnpm npx; do \
      if [ -f "/usr/local/bin/$cmd" ] && [ ! -L "/usr/local/bin/$cmd" ]; then \
        mv "/usr/local/bin/$cmd" "/usr/local/bin/_real_$cmd"; \
      elif [ -L "/usr/local/bin/$cmd" ]; then \
        cp -P "/usr/local/bin/$cmd" "/usr/local/bin/_real_$cmd" && rm "/usr/local/bin/$cmd"; \
      fi; \
      ln -sf /usr/local/bin/forge-pkg-guard "/usr/local/bin/$cmd"; \
    done

# ── DB precheck (auto-heal drizzle.config.ts at db:* invocation) ─────────────
# Runs as the first step of every `npm run db:generate` / `db:migrate` /
# `db:studio` command (wired into package.json by the bootstrap). Validates
# drizzle.config.ts is Postgres-shaped and rewrites it back if the AI
# freelanced it into SQLite mid-session. This is the reactive layer: the
# bootstrap re-asserts the file at container START, opencode perms + chmod
# 444 prevent the AI from editing it during a session, and this precheck
# catches it at the moment of execution if all four preventative layers
# somehow fail. Auto-rewrites + exits 1 with a clear stderr message so the
# AI sees the correction and adjusts its schema.ts on the next turn.
COPY forge-db-precheck.js /usr/local/lib/forge-db-precheck.js
RUN chmod +r /usr/local/lib/forge-db-precheck.js

# ── Explicitly NOT installing postgresql-client ──────────────────────────────
# `psql` / `createdb` / `dropdb` would let the model freelance Postgres (we've
# seen it try `PGPASSWORD=forgedev psql -U postgres -d sales_pipeline` instead
# of calling /db/provision). The runner image keeps these binaries OUT so the
# freelance path doesn't exist. The only Postgres access is via the role + URL
# returned by /db/provision, written to .env.local, and consumed by
# Drizzle/pg from the user's app code.
#
# (Defense in depth: the bash deny rules in forge-opencode-config/opencode.json
# also reject `psql *` / `createdb *` / `dropdb *` so the same applies on the
# host side in dev mode where the AI's shell runs outside the container.)
#
# If you find yourself wanting psql in this image — stop. The right answer is
# almost always either (a) update db.md so the agent uses the proper recipe,
# or (b) call /api/projects/{id}/db/sql via curl (Forge proxies it to the
# scoped role).

# Dev-server env. PNPM_STORE_DIR is the path the shared volume mounts at —
# container_manager.py wires the mount, this var tells pnpm to use it.
ENV BROWSER=none \
    CI=true \
    NO_OPEN=true \
    VITE_NO_OPEN=true \
    PORT=3000 \
    VITE_PORT=3000 \
    HOST=0.0.0.0 \
    HOSTNAME=0.0.0.0 \
    NODE_ENV=development \
    FORCE_COLOR=0 \
    NO_COLOR=1 \
    PNPM_HOME=/root/.local/share/pnpm \
    PNPM_STORE_DIR=/forge-store

# Entrypoint: bootstrap Forge scaffolding, install deps, then run dev server.
# Prefer pnpm (uses shared store); fall back to npm for projects without
# pnpm-lock.yaml or that opted out by committing package-lock.json.
#
# drizzle-kit migrate ONLY runs when DB is opt-in enabled (marker file or
# legacy data.db/drizzle artefacts). Without the gate, drizzle-kit crashes
# on every Tailwind-only project boot. See forge-bootstrap.sh §5.
CMD ["sh", "-c", "if [ -f package.json ]; then \
        /usr/local/bin/forge-bootstrap.sh || true; \
        DB_ENABLED=0; \
        if [ -f .forge/db-enabled ] || [ -f data.db ] || ls drizzle/*.sql >/dev/null 2>&1; then DB_ENABLED=1; fi; \
        if command -v pnpm >/dev/null && [ ! -f package-lock.json ]; then \
          pnpm install --prefer-offline 2>&1 && \
          ([ \"$DB_ENABLED\" = \"1\" ] && npx --yes drizzle-kit migrate 2>&1 || true) && \
          pnpm run dev 2>&1; \
        else \
          npm install --prefer-offline 2>&1 && \
          ([ \"$DB_ENABLED\" = \"1\" ] && npx --yes drizzle-kit migrate 2>&1 || true) && \
          npm run dev 2>&1; \
        fi; \
      else \
        echo 'No package.json found'; \
      fi"]
