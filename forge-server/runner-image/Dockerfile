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
RUN npm install -g \
    vite \
    typescript \
    tsx \
    ts-node \
    @types/node \
    2>/dev/null || true

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
    FORCE_COLOR=1 \
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
