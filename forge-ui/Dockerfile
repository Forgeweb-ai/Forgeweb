# syntax=docker/dockerfile:1
# Forge UI — Vite/SolidJS chat interface.
# ==========================================
# Build context MUST be the repo root (not ./forge-ui), because forge-ui is a
# bun workspace member declared in opencode/package.json and resolves several
# of its dependencies via that workspace + bun catalogs. Building from inside
# ./forge-ui alone makes `bun install` fail on workspace:* and catalog: refs.
#
# Usage from repo root:
#   docker build -f forge-ui/Dockerfile -t forge-ui .
#
# Or via docker-compose.yml (which sets context: . and dockerfile: forge-ui/Dockerfile).
#
# Two-stage build:
#   1. builder — installs the opencode workspace, builds forge-ui → dist/
#   2. runtime — tiny nginx serving the static bundle on :3000
#
# Why nginx and not bun preview / vite preview?
#   - Static files only at runtime; no need to ship the toolchain.
#   - nginx:alpine is ~25MB vs oven/bun:alpine ~80MB.
#   - Better default caching headers + gzip out of the box.

# ── 1. Builder ───────────────────────────────────────────────────────────────
FROM oven/bun:1.3.14 AS builder

# Build deps: forge-ui shares the opencode workspace tree, which pulls tree-
# sitter-* and other native modules compiled by node-gyp at install time.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential python3 git jq patch && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy the opencode workspace manifests first so bun install caches across
# source-only changes. The full opencode/ tree is needed because forge-ui
# depends on workspace:* packages defined inside it.
COPY opencode/package.json     opencode/bun.lock     opencode/bunfig.toml     opencode/turbo.json     opencode/tsconfig.json     ./opencode/
COPY opencode/packages         ./opencode/packages
COPY opencode/patches          ./opencode/patches
COPY forge-ui/package.json     ./forge-ui/

# Drop unused workspace packages (desktop, console, storybook, docs, web), strip patchedDependencies, and strip scripts.prepare
RUN rm -rf opencode/packages/desktop opencode/packages/console opencode/packages/storybook opencode/packages/docs opencode/packages/web
RUN jq 'del(.patchedDependencies) | del(.scripts.prepare)' opencode/package.json > opencode/package.tmp.json && mv opencode/package.tmp.json opencode/package.json

# Strip bunfig.toml's minimumReleaseAge before install.
RUN sed -i '/minimumReleaseAge/d' opencode/bunfig.toml || true

# Install workspace deps cleanly without bun's patch applier engine
RUN --mount=type=tmpfs,target=/root/.bun/install/cache \
    cd opencode && bun install --no-save --linker=hoisted || true

# Apply patches explicitly using POSIX GNU patch
RUN cd opencode \
 && patch -p1 -l -d node_modules/@ai-sdk/xai < patches/@ai-sdk-xai@3.0.82.patch \
 && patch -p1 -l -d node_modules/@npmcli/agent < patches/@npmcli-agent@4.0.0.patch \
 && patch -p1 -l -d node_modules/@silvia-odwyer/photon-node < patches/@silvia-odwyer-photon-node@0.3.4.patch \
 && patch -p1 -l -d node_modules/@standard-community/standard-openapi < patches/@standard-community-standard-openapi@0.2.9.patch \
 && patch -p1 -l -d node_modules/solid-js < patches/solid-js@1.9.10.patch

# Verify what's present so the log gives us a clear before/after for the
# packages bun complained about.
RUN cd opencode && for pkg in @ai-sdk/anthropic @smithy/eventstream-codec @shikijs/transformers minimatch; do \
    if [ -d "node_modules/$pkg" ]; then \
        echo "PRESENT  $pkg"; \
    else \
        echo "MISSING  $pkg"; \
    fi; \
    done

# Now copy forge-ui sources for the actual build step.
COPY forge-ui ./forge-ui

# Symlink opencode/node_modules into forge-ui and export bin path so vite and dependencies are found
RUN ln -s /build/opencode/node_modules /build/forge-ui/node_modules
ENV PATH="/build/opencode/node_modules/.bin:${PATH}"

# Vite reads VITE_* from the environment at BUILD time and inlines them into
# the static bundle. Pass them as build args so docker compose can override
# the dev defaults baked into forge-ui/.env.
ARG VITE_API_URL=http://api.forge.localhost
ARG VITE_PREVIEW_DOMAIN=preview.lvh.me
ARG VITE_OPENCODE_URL=http://opencode:7777
ENV VITE_API_URL=${VITE_API_URL} \
    VITE_PREVIEW_DOMAIN=${VITE_PREVIEW_DOMAIN} \
    VITE_OPENCODE_URL=${VITE_OPENCODE_URL}

# Build. Vite reads index.html and ./src/, emits ./dist/.
RUN cd forge-ui && bun run build

# ── 2. Runtime ───────────────────────────────────────────────────────────────
FROM nginx:1.27-alpine

# Default nginx serves on :80; rebind to :3000 to match docker-compose.yml's
# traefik label (loadbalancer.server.port=3000) and the runner contract.
RUN sed -i 's/listen       80/listen       3000/' /etc/nginx/conf.d/default.conf  \
 && sed -i 's/listen  \[::\]:80/listen  [::]:3000/' /etc/nginx/conf.d/default.conf

# SPA fallback: route any path that isn't a real file to /index.html so
# client-side routes (e.g. /projects/:id) load on direct navigation.
RUN sed -i 's|location / {|location / { try_files $uri $uri/ /index.html;|' /etc/nginx/conf.d/default.conf

COPY --from=builder /build/forge-ui/dist /usr/share/nginx/html

# Source files copied from the host may carry restrictive modes (e.g. PNGs in
# public/ created on a macOS shell where umask 077 lands them at 0600). nginx
# runs as a non-root user inside the container and silently 403s on
# owner-only-readable files. Force everything in the served tree to be world-
# readable; directories also need +x to be traversable.
RUN chmod -R a+rX /usr/share/nginx/html

EXPOSE 3000

# Run nginx in foreground so docker keeps the container alive.
CMD ["nginx", "-g", "daemon off;"]
