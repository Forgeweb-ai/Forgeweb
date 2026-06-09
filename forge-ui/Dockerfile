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
FROM oven/bun:1.3.14-alpine AS builder

# Build deps: forge-ui shares the opencode workspace tree, which pulls tree-
# sitter-* and other native modules compiled by node-gyp at install time.
# `make g++` alone is not enough — node-gyp also needs libc-dev, binutils,
# pkgconfig, etc. Alpine's `build-base` meta-package bundles all of them.
# Without this, bun reports "Failed to install N packages" and a cascade of
# FileNotFound errors that are symptoms, not causes.
RUN apk add --no-cache build-base python3 git

WORKDIR /build

# Copy the opencode workspace manifests first so bun install caches across
# source-only changes. The full opencode/ tree is needed because forge-ui
# depends on workspace:* packages defined inside it.
COPY opencode/package.json     opencode/bun.lock     opencode/bunfig.toml     opencode/turbo.json     opencode/tsconfig.json     ./opencode/
COPY opencode/packages         ./opencode/packages
# opencode/patches/ contains bun patchedDependencies referenced in
# opencode/package.json (photon-node, npmcli/agent, standard-openapi, etc.).
# bun install resolves them by relative path; missing => "Couldn't find patch
# file" and the whole install aborts. Copy before the install step.
COPY opencode/patches          ./opencode/patches
COPY forge-ui/package.json     ./forge-ui/

# Drop packages/desktop — its @forge/ui dep would resolve here (forge-ui is
# in context), but desktop also depends on @lydell/node-pty + Electron native
# bits we don't want in a static UI build. Same reasoning as in
# opencode/Dockerfile.forge.
RUN rm -rf opencode/packages/desktop

# Strip bunfig.toml's minimumReleaseAge before install. See
# opencode/Dockerfile.forge for the full reasoning: the 3-day supply-chain
# gate is invisible on the host (cached) but breaks fresh Docker builds for
# every package that got a recent point release.
RUN sed -i '/minimumReleaseAge/d' opencode/bunfig.toml || true

# Install workspace deps from the opencode root. --linker=hoisted matches
# the user's host layout (npm-style flat node_modules).
#
# DIAGNOSTIC: same exit-code-suppression as opencode/Dockerfile.forge.
# Bun reports "Failed to install N packages" with FileNotFound errors for a
# specific subset of transitive deps. Suppressing here lets the Vite build
# proceed; if any of those packages are actually needed, the build step
# below will fail with a clear "Cannot find module" pointing at the real
# culprit instead of bun's misleading summary.
RUN cd opencode && bun install --no-save --linker=hoisted || true

# Verify what's present so the log gives us a clear before/after for the
# packages bun complained about.
RUN cd opencode && for pkg in @ai-sdk/anthropic @smithy/eventstream-codec @shikijs/transformers minimatch; do      if [ -d "node_modules/$pkg" ]; then         echo "PRESENT  $pkg";       else         echo "MISSING  $pkg";       fi;     done

# Now copy forge-ui sources for the actual build step.
COPY forge-ui ./forge-ui

# Vite reads VITE_* from the environment at BUILD time and inlines them into
# the static bundle. Pass them as build args so docker compose can override
# the dev defaults baked into forge-ui/.env.
ARG VITE_API_URL=http://api.forge.localhost
ARG VITE_PREVIEW_DOMAIN=preview.lvh.me
ARG VITE_OPENCODE_URL=http://opencode:7777
ENV VITE_API_URL=${VITE_API_URL}     VITE_PREVIEW_DOMAIN=${VITE_PREVIEW_DOMAIN}     VITE_OPENCODE_URL=${VITE_OPENCODE_URL}

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

EXPOSE 3000

# Run nginx in foreground so docker keeps the container alive.
CMD ["nginx", "-g", "daemon off;"]
