#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# dev.sh — Start all Forge services for local development
#
# Usage:
#   ./dev.sh
#
# This script is fully self-contained:
#   - Installs bun deps (opencode monorepo + forge-ui workspace)
#   - Creates a Python venv for forge-server and installs deps
#   - Starts all three services and opens the browser
#
# Services:
#   :7777  OpenCode server  (AI coding agent backend)
#   :8000  forge-server     (FastAPI — project CRUD, auth, config)
#   :3000  forge-ui     (Vite/SolidJS — the Forge UI)
#   :54321 Supabase API (boots automatically — Postgres on 54322, Studio on 54323)
#
# Prerequisites (must be installed manually):
#   - bun       (https://bun.sh)
#   - python3   (3.11+)
#   - docker    (https://docs.docker.com/get-docker/)
#   - supabase  (https://supabase.com/docs/guides/local-development/cli/getting-started)
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()  { echo -e "${CYAN}[forge]${RESET} $*"; }
ok()    { echo -e "${GREEN}[forge]${RESET} $*"; }
warn()  { echo -e "${YELLOW}[forge]${RESET} $*"; }
die()   { echo -e "${RED}[forge] ERROR:${RESET} $*" >&2; exit 1; }

# ── Kill all children on Ctrl-C / exit ───────────────────────────────────────
PIDS=()
TRAEFIK_CONTAINER="forge-traefik-dev"
cleanup() {
  echo ""
  warn "Shutting down all services…"
  for pid in "${PIDS[@]+"${PIDS[@]}"}"; do
    kill "$pid" 2>/dev/null || true
  done
  # Stop Traefik (best-effort, ignore errors if not running)
  docker rm -f "$TRAEFIK_CONTAINER" >/dev/null 2>&1 || true
  wait 2>/dev/null || true
  ok "All services stopped."
}
trap cleanup EXIT INT TERM

# ── Colour-coded log prefix ───────────────────────────────────────────────────
pipe_prefix() {
  local prefix="$1" colour="$2"
  while IFS= read -r line; do
    echo -e "${colour}[${prefix}]${RESET} ${line}"
  done
}

# ── Required tools ────────────────────────────────────────────────────────────
check_tool() { command -v "$1" >/dev/null 2>&1 || die "'$1' not found. Install it from: $2"; }
check_tool bun      "https://bun.sh"
check_tool python3  "https://python.org (3.11+)"
check_tool docker   "https://docs.docker.com/get-docker/"
# `npx` is only needed if we have to BOOT Supabase. Checked lazily below so
# users who already have Postgres on :54322 don't need Node installed.
need_npx() {
  command -v npx >/dev/null 2>&1 || die "npx not found (needed to boot the \
local Supabase stack). Install Node.js 20+ from https://nodejs.org or via \
'brew install node' (macOS) / your distro's nodejs package (Linux), then re-run."
}

# ── Free ports ────────────────────────────────────────────────────────────────
free_port() {
  local port="$1"
  local pids
  pids=$(lsof -t -i tcp:"$port" 2>/dev/null) || true
  if [[ -n "$pids" ]]; then
    warn "Port $port already in use — killing stale process(es)…"
    for pid in $pids; do kill -9 "$pid" 2>/dev/null || true; done
    sleep 0.5
  fi
}
free_port 7777
free_port 8000
free_port 3000

# ── Ports (overridable via env) ───────────────────────────────────────────────
OPENCODE_PORT="${OPENCODE_PORT:-7777}"
BE_PORT="${BE_PORT:-8000}"
FE_PORT="${FE_PORT:-3000}"

# ── Workspace & database paths ────────────────────────────────────────────────
FORGE_DATA_ROOT="${FORGE_DATA_ROOT:-$ROOT/forge-data}"
mkdir -p "$FORGE_DATA_ROOT"

# Postgres lives in the local Supabase stack we boot below (port 54322).
# Forge's metadata moved off SQLite — see [[forge_storage_architecture]].
DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://postgres:postgres@127.0.0.1:54322/postgres}"

# Shared HMAC secret between forge-server's opencode_proxy and opencode's
# forge-user middleware. Both processes MUST see the same value or the
# per-user agent-model resolver fails closed (warns + falls back to parent
# chat model). Dev default is fine for local; production must set
# FORGE_INTERNAL_SECRET in the environment.
FORGE_INTERNAL_SECRET="${FORGE_INTERNAL_SECRET:-dev-only-internal-secret-do-not-ship}"

# Where opencode's resolver calls back into forge-server. Localhost in dev;
# the docker-network DNS name in prod.
FORGE_API_URL="${FORGE_API_URL:-http://127.0.0.1:${BE_PORT:-8000}}"

# Supabase service role key for storage operations. dev.sh picks it up from
# Supabase CLI status output below; you can also set it manually via env or
# forge-server/.env. Empty value = snapshot worker no-ops cleanly.
SUPABASE_SERVICE_ROLE_KEY="${SUPABASE_SERVICE_ROLE_KEY:-}"

# ── Step 1: Bun deps (opencode monorepo — includes forge-ui workspace) ───
info "Checking bun dependencies…"
# forge-ui is a workspace in opencode/package.json, so install from there.
if [[ ! -d "$ROOT/opencode/node_modules" ]]; then
  info "Running bun install in opencode/ (first run — may take a minute)…"
  bun install --cwd "$ROOT/opencode" 2>&1 | pipe_prefix "bun-install" '\033[0;35m'
else
  ok "bun deps already installed (opencode/node_modules exists)"
fi

# ── Step 2: Python venv + deps for forge-server ───────────────────────────────
info "Checking Python dependencies…"
VENV="$ROOT/forge-server/.venv"
if [[ ! -d "$VENV" ]]; then
  info "Creating Python virtualenv at forge-server/.venv …"
  python3 -m venv "$VENV" || die "python3 -m venv failed — install Python 3.11+ first"
fi

# Activate venv and install deps.
# Use a hash of requirements-dev.txt as a sentinel — re-install whenever the file changes.
# shellcheck disable=SC1090
source "$VENV/bin/activate"
REQ_HASH_FILE="$VENV/.req_hash"
REQ_HASH="$(md5 -q "$ROOT/forge-server/requirements-dev.txt" 2>/dev/null || md5sum "$ROOT/forge-server/requirements-dev.txt" | cut -d' ' -f1)"
STORED_HASH="$(cat "$REQ_HASH_FILE" 2>/dev/null || echo '')"
if [[ "$REQ_HASH" != "$STORED_HASH" ]]; then
  warn "Installing/updating Python dependencies into .venv…"
  pip install -r "$ROOT/forge-server/requirements-dev.txt" -q \
    || die "pip install failed — check forge-server/requirements-dev.txt"
  echo "$REQ_HASH" > "$REQ_HASH_FILE"
fi
deactivate

ok "All dependencies ready."

# ── Step 2b: Docker setup (network + runner image for app preview) ─────────────
info "Checking Docker setup…"
# Ensure the forge-net bridge network exists (containers attach to it)
if ! docker network inspect forge-net >/dev/null 2>&1; then
  info "Creating Docker network 'forge-net'…"
  docker network create forge-net 2>&1 | pipe_prefix "docker-net" '\033[0;34m' \
    || die "docker network create failed — is Docker running?"
else
  ok "Docker network 'forge-net' already exists."
fi

# Ensure the shared pnpm store volume exists. Every project container mounts
# this read-write at /forge-store — pnpm dedupes packages across all projects
# into one content-addressable directory. Survives docker volume prune unless
# explicitly removed.
if ! docker volume inspect forge-pnpm-store >/dev/null 2>&1; then
  info "Creating Docker volume 'forge-pnpm-store' (shared pnpm package store)…"
  docker volume create forge-pnpm-store 2>&1 | pipe_prefix "docker-vol" '\033[0;34m'
else
  ok "Docker volume 'forge-pnpm-store' already exists."
fi
# Build the runner image if missing OR if its source files changed.
# We hash everything under runner-image/ — Dockerfile, forge-bootstrap.sh, any
# new asset — and rebuild when the hash drifts from the last successful build.
RUNNER_HASH_FILE="$ROOT/forge-server/runner-image/.build_hash"
RUNNER_HASH="$(find "$ROOT/forge-server/runner-image" -type f -not -name '.build_hash' \
  -exec sha256sum {} \; 2>/dev/null | sort | sha256sum | cut -d' ' -f1)"
# macOS uses `shasum -a 256`; fall back if `sha256sum` isn't installed.
if [[ -z "$RUNNER_HASH" ]]; then
  RUNNER_HASH="$(find "$ROOT/forge-server/runner-image" -type f -not -name '.build_hash' \
    -exec shasum -a 256 {} \; 2>/dev/null | sort | shasum -a 256 | cut -d' ' -f1)"
fi
STORED_RUNNER_HASH="$(cat "$RUNNER_HASH_FILE" 2>/dev/null || echo '')"

if ! docker image inspect forge-runner:latest >/dev/null 2>&1; then
  info "Building forge-runner:latest (first run — may take a minute)…"
  docker build -t forge-runner:latest "$ROOT/forge-server/runner-image/" \
    2>&1 | pipe_prefix "docker-build" '\033[0;34m' \
    || die "docker build failed — check forge-server/runner-image/Dockerfile"
  echo "$RUNNER_HASH" > "$RUNNER_HASH_FILE"
  ok "forge-runner:latest built successfully."
elif [[ "$RUNNER_HASH" != "$STORED_RUNNER_HASH" ]]; then
  info "runner-image/ changed — rebuilding forge-runner:latest…"
  docker build -t forge-runner:latest "$ROOT/forge-server/runner-image/" \
    2>&1 | pipe_prefix "docker-build" '\033[0;34m' \
    || die "docker build failed — check forge-server/runner-image/Dockerfile"
  echo "$RUNNER_HASH" > "$RUNNER_HASH_FILE"
  ok "forge-runner:latest rebuilt."
else
  ok "forge-runner:latest up-to-date — skipping build."
fi

# ── Step 2b2: Traefik (host routing for *.preview.lvh.me) ─────────────────────
# Browser-facing entry point for project previews. lvh.me wildcard-resolves to
# 127.0.0.1, so http://<id>.preview.lvh.me/ hits port 80 here, then Traefik
# routes by Host header to forge-proj-<id>:3000 via the forge-net network.
# Container labels (set by container_manager._traefik_labels) drive the routing
# — no per-project config needed in Traefik itself.
info "Starting Traefik on :80 / :443 / :8080 (dashboard)…"

# Refuse to start if something else owns port 80 — Traefik will fail noisily
# anyway, but a clear early message saves debugging time.
if lsof -nP -iTCP:80 -sTCP:LISTEN 2>/dev/null | grep -qv '^COMMAND'; then
  warn "Port 80 is already bound by another process — Traefik may fail to start."
  warn "Run: lsof -nP -iTCP:80 -sTCP:LISTEN  to find the culprit."
fi

# Remove any stale instance from a previous run that crashed without cleanup
docker rm -f "$TRAEFIK_CONTAINER" >/dev/null 2>&1 || true

docker run -d \
  --name "$TRAEFIK_CONTAINER" \
  --network forge-net \
  -p 80:80 \
  -p 443:443 \
  -p 8080:8080 \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v "$ROOT/traefik:/etc/traefik:ro" \
  traefik:v3.0 \
    --api.insecure=true \
    --providers.docker=true \
    --providers.docker.exposedbydefault=false \
    --providers.docker.network=forge-net \
    --providers.file.directory=/etc/traefik/dynamic \
    --providers.file.watch=true \
    --entrypoints.web.address=:80 \
    --entrypoints.websecure.address=:443 \
    --log.level=INFO \
  >/dev/null \
  || die "Failed to start Traefik. Is port 80 free? (lsof -nP -iTCP:80 -sTCP:LISTEN)"

ok "Traefik running — previews at http://<project-id>.preview.lvh.me/"

# ── Step 2c: Local Supabase (Postgres + Auth + Storage + Studio) ──────────────
# We boot Supabase via the CLI rather than the docker-compose service. The CLI
# uses fixed default ports (54321 API / 54322 Postgres / 54323 Studio / 54324
# Mailpit) so only one Supabase stack can run per machine — that's fine because
# Forge's metadata + (later) user-app data both live in this one stack.
info "Checking local Supabase…"

# Postgres-first detection: if something is already listening on :54322, we
# assume it's a usable Supabase stack (or any Postgres compatible with our
# schema) and just use it. The `supabase` CLI is only needed when we have to
# boot a stack from scratch — so users running Supabase via npx, a different
# project directory, or a custom docker-compose don't need the CLI installed.
SUPABASE_RUNNING=false
if lsof -t -i tcp:54322 >/dev/null 2>&1; then
  ok "Postgres already listening on :54322 — reusing existing stack."
  SUPABASE_RUNNING=true
else
  # No Postgres yet — we'll need the CLI to boot one.
  need_npx
  info "Starting Supabase via npx (first run pulls ~6 Docker images — may take a few minutes)…"
  # supabase init creates supabase/config.toml on first run. Safe to skip if it already exists.
  if [[ ! -f "$ROOT/supabase/config.toml" ]]; then
    info "Initialising Supabase project in $ROOT/supabase/…"
    (cd "$ROOT" && npx supabase init 2>&1 | pipe_prefix "supabase-init" '\033[0;32m') \
      || die "npx supabase init failed"
  fi
  (cd "$ROOT" && npx supabase start 2>&1 | pipe_prefix "supabase-start" '\033[0;32m') \
    || die "npx supabase start failed — is Docker running and disk space available?"
  SUPABASE_RUNNING=true
fi

# Capture the service role key for the snapshot worker.
#
# `supabase status -o env` prints lines like
#   SERVICE_ROLE_KEY="eyJhbGciOiJIUzI1NiIs..."
# After `cut -d= -f2-` the value still carries the surrounding double quotes.
# Exporting the quoted value makes the runtime send `Bearer "eyJ..."` which
# Supabase rejects as "JWS Protected Header is invalid" — pydantic-settings
# does NOT auto-strip quotes from environment variables, only from .env file
# lines. We strip them here at capture so downstream code sees a clean JWT.
if [[ -z "$SUPABASE_SERVICE_ROLE_KEY" ]]; then
  # `npx supabase status` can hang indefinitely when npx is fetching the CLI,
  # when Docker is slow, or when the CLI hits an interactive prompt. Cap it at
  # 10s so a wedged status call never blocks the rest of dev boot.
  if SUPABASE_SERVICE_ROLE_KEY="$(cd "$ROOT" && timeout 10 npx supabase status -o env 2>/dev/null | grep -E '^SERVICE_ROLE_KEY=' | cut -d= -f2-)"; then
    # Strip surrounding " or ' and any stray whitespace.
    SUPABASE_SERVICE_ROLE_KEY="$(echo -n "$SUPABASE_SERVICE_ROLE_KEY" | sed -E 's/^[[:space:]]*"?(.*[^[:space:]"])"?[[:space:]]*$/\1/')"
    if [[ -n "$SUPABASE_SERVICE_ROLE_KEY" ]]; then
      ok "Picked up SUPABASE_SERVICE_ROLE_KEY from Supabase"
      export SUPABASE_SERVICE_ROLE_KEY
    else
      warn "Couldn't fetch SUPABASE_SERVICE_ROLE_KEY (timed out or empty) — snapshot worker will be unauthenticated."
      warn "Set it manually: export SUPABASE_SERVICE_ROLE_KEY=... before re-running."
    fi
  else
    warn "\`npx supabase status\` failed or timed out after 10s — skipping service-role-key capture."
  fi
fi

# ── Step 2c.5: Wait for Postgres to actually accept connections ───────────────
# `npx supabase start` returns when the CONTAINERS report healthy, not when
# Postgres-inside-the-container has finished init_db + bound :54322 on the
# host. After a Mac restart this window can be 5–30s; alembic firing during
# that window gets weird errors (EADDRNOTAVAIL on macOS, not just refused).
# A short bounded wait turns a flaky boot into a deterministic one.
wait_for_postgres() {
  local timeout=45
  local elapsed=0
  while (( elapsed < timeout )); do
    # asyncpg-level check — same driver alembic uses, so if THIS succeeds,
    # alembic will too. The "+asyncpg" SQLAlchemy suffix is stripped because
    # asyncpg's connect() doesn't understand it.
    local plain="${DATABASE_URL/postgresql+asyncpg:/postgresql:}"
    if "$VENV/bin/python" -c "
import asyncio, asyncpg, sys
async def go():
    try:
        c = await asyncpg.connect('$plain', timeout=2)
        await c.close()
    except Exception as e:
        sys.exit(1)
asyncio.run(go())
" 2>/dev/null; then
      ok "Postgres on :54322 is accepting connections (${elapsed}s)"
      return 0
    fi
    sleep 1
    ((elapsed++))
    # Progress dot every 5s so a long wait isn't silent
    (( elapsed % 5 == 0 )) && info "  still waiting for Postgres… (${elapsed}/${timeout}s)"
  done
  return 1
}
info "Waiting for Postgres on :54322 to accept connections…"
wait_for_postgres || die "Postgres on :54322 didn't accept connections within 45s. \
Check 'docker ps | grep supabase' — the supabase_db container should be running and healthy. \
If it's missing, run 'npx supabase stop && npx supabase start' from this directory."

# ── Step 2d: Alembic schema migration ─────────────────────────────────────────
info "Applying Alembic migrations…"
(
  cd "$ROOT/forge-server"
  # shellcheck disable=SC1090
  source "$VENV/bin/activate"
  export DATABASE_URL="$DATABASE_URL"
  alembic upgrade head 2>&1 | pipe_prefix "alembic" '\033[0;34m'
) || die "alembic upgrade head failed — check DATABASE_URL and forge-server/alembic/versions/"

# ── Step 2.5: forge-llm-proxy (logging proxy between opencode ↔ vendors) ──────
# Single chokepoint for Anthropic, Moonshot (Kimi K2.6), and Google (Gemini Flash
# family). Captures tokens + cost on every call via forge-qa's rate card.
# Runs INSIDE forge-server/.venv — same reason as forge-be: known-good Python.
LLM_PROXY_PORT="${LLM_PROXY_PORT:-7799}"
info "Starting forge-llm-proxy on :${LLM_PROXY_PORT}…"
free_port "$LLM_PROXY_PORT"
(
  cd "$ROOT/forge-llm-proxy"

  # The venv must already exist — forge-server's setup creates it earlier in
  # this script. If it's somehow missing, surface a clear error instead of
  # falling back to a broken pyenv Python.
  if [[ ! -x "$VENV/bin/python" ]]; then
    warn "forge-server venv at $VENV/bin/python is missing — proxy can't start."
    warn "Run forge-server setup first (or remove forge-server/.venv and re-run dev.sh)."
    exit 1
  fi

  # Install proxy deps into the shared venv if missing (lightweight: fastapi +
  # httpx are already there because forge-server uses them; only uvicorn might
  # need installing).
  if ! "$VENV/bin/python" -c "import fastapi, httpx, uvicorn" 2>/dev/null; then
    info "Installing forge-llm-proxy deps into forge-server venv…"
    "$VENV/bin/pip" install -q -r requirements.txt 2>&1 \
      | pipe_prefix "llm-proxy-install" '\033[0;35m'
  fi

  export PORT="$LLM_PROXY_PORT"
  export FORGE_LLM_PROXY_LOG_DIR="$ROOT/forge-llm-proxy-logs"
  # Point the proxy at the forge-qa rate card so each call's `cost` field is
  # populated inline. Without this, log files contain usage but no dollars,
  # and BYOK/Platform-managed dashboards have nothing to display.
  export FORGE_QA_PATH="$ROOT/forge-qa"
  "$VENV/bin/python" proxy.py 2>&1 | pipe_prefix "llm-proxy" '\033[0;36m'
) &
PIDS+=($!)

# Wait briefly for /healthz, then surface what the proxy actually loaded.
# A silent proxy that says "0/3 upstreams ready" is what's actually killing
# debug time — surface it ON BOOT, not when the user notices missing cost.
(
  sleep 1.5
  if hz="$(curl -fsS "http://127.0.0.1:${LLM_PROXY_PORT}/healthz" 2>/dev/null)"; then
    rc="$(echo "$hz" | python3 -c "import sys,json;d=json.load(sys.stdin);print('rate_card_loaded=%s' % d.get('rate_card_loaded'))" 2>/dev/null)"
    ok "forge-llm-proxy healthy ($rc)"
  else
    warn "forge-llm-proxy didn't respond on :${LLM_PROXY_PORT}/healthz within 1.5s"
  fi
) &

# ── Step 3: OpenCode server ───────────────────────────────────────────────────
info "Starting OpenCode on :${OPENCODE_PORT}…"

# Render the platform-level opencode config into a Forge-controlled XDG_CONFIG_HOME
# so design-analyst + design-critic subagents are defined when running via ./dev.sh
# (not just docker-compose). We also need to rewrite two container-only paths to
# their local-dev equivalents:
#   /forge-skills              → $ROOT/forge-server/forge_server/skills
#   http://forge-llm-proxy:7799 → http://127.0.0.1:$LLM_PROXY_PORT
# We write to forge-opencode-config-local/ (gitignored) instead of the user's
# real ~/.config/opencode/ — non-destructive.
LOCAL_XDG="$ROOT/forge-opencode-config-local"
LOCAL_OC_DIR="$LOCAL_XDG/opencode"
LOCAL_SKILLS="$ROOT/forge-server/forge_server/skills"
LOCAL_PROXY="http://127.0.0.1:${LLM_PROXY_PORT}"
mkdir -p "$LOCAL_OC_DIR"

if [[ -f "$ROOT/forge-opencode-config/opencode.json" ]]; then
  # design-analyst / design-critic model is now resolved per-request by the
  # opencode-side resolver in src/forge/agent-model.ts using the user's
  # design_model from forge-server. dev.sh only substitutes the two
  # container→host path tokens; the agent.model sentinel passes through
  # verbatim so the resolver sees it on every dispatch.
  PRIMARY_MODEL="${PRIMARY_MODEL:-opencode/deepseek-v4-flash-free}"
  export PRIMARY_MODEL

  # Substitute container-only paths → host equivalents, and merge in any
  # user-added entries from the existing local file. The "merge" step is
  # what fixes "custom provider info is not saving":
  #   1. opencode's globalConfigFile() picks opencode.json as its write
  #      target when it exists (config.ts:340), so the FE's custom-provider
  #      save lands here.
  #   2. Before this change, this block clobbered that file every dev.sh
  #      run with the bare platform template — wiping user-added providers
  #      and disabled_providers entries.
  #   3. We now READ the existing file, extract only the user-added bits
  #      (providers NOT in the platform template + the disabled list), and
  #      fold them into the freshly-substituted platform template before
  #      writing. Platform-owned keys still win on conflict — a user can't
  #      redefine `anthropic` to point upstream and bypass the proxy.
  # python3 over sed because paths/JSON contain chars sed would need escaped.
  python3 -c "
import json, os, sys
platform = json.loads(open(sys.argv[1]).read())
# Token substitution (do it on the parsed dict's JSON form so escaping is correct)
text = json.dumps(platform)
text = text.replace('\"/forge-skills\"',         json.dumps(sys.argv[2]))
text = text.replace('http://forge-llm-proxy:7799', sys.argv[3])
platform = json.loads(text)

# Preserve user-added entries from any prior local file.
out_path = sys.argv[4]
preserved_providers = 0
preserved_disabled  = 0
if os.path.exists(out_path):
    try:
        existing = json.loads(open(out_path).read())
    except Exception:
        existing = {}
    # User providers = providers in existing that are NOT in platform template.
    plat_p = (platform.get('provider') or {})
    exi_p  = (existing.get('provider') or {})
    user_only = {k: v for k, v in exi_p.items() if k not in plat_p}
    if user_only:
        platform['provider'] = {**plat_p, **user_only}
        preserved_providers = len(user_only)
    # Preserve disabled_providers wholesale — a list of provider IDs the
    # user opted out of. Merge by union so platform defaults still apply
    # plus anything the user added.
    plat_d = list(platform.get('disabled_providers') or [])
    exi_d  = list(existing.get('disabled_providers') or [])
    merged_d = list(dict.fromkeys(plat_d + exi_d))  # dedupe, preserve order
    if merged_d != plat_d:
        platform['disabled_providers'] = merged_d
        preserved_disabled = len(set(exi_d) - set(plat_d))

open(out_path, 'w').write(json.dumps(platform, indent=2))
print(f'preserved {preserved_providers} user provider(s), {preserved_disabled} user disabled entry(s)')
" "$ROOT/forge-opencode-config/opencode.json" "$LOCAL_SKILLS" "$LOCAL_PROXY" "$LOCAL_OC_DIR/opencode.json"
  ok "Platform opencode config installed → $LOCAL_OC_DIR/opencode.json"
  ok "  - skills.paths        → $LOCAL_SKILLS"
  ok "  - anthropic base      → $LOCAL_PROXY/v1"
  ok "  - design subagent     → resolved per-request from user_settings"
  ok "  - primary model       → $PRIMARY_MODEL (exported for opencode fork)"
else
  warn "forge-opencode-config/opencode.json missing — subagents won't be defined."
fi

(
  cd "$ROOT/opencode/packages/opencode"

  # Load non-LLM env vars into the opencode subshell (DATABASE_URL,
  # JWT_SECRET, DESIGN_ANALYST_MODEL, etc.). LLM provider keys are NOT
  # in either .env anymore — they're per-user, Fernet-encrypted in
  # user_provider_keys, and injected per-request by the resolver. The
  # default model is opencode zen's free DeepSeek V4 Flash, which needs
  # no key at all. opencode.json's {env:MOONSHOT_API_KEY} placeholders
  # resolve to empty strings and the resolver overrides them on every
  # authenticated request.
  #
  # We still source both env files (in this order) for the non-LLM
  # config they carry. Later file wins on collision — forge-server/.env
  # can override root for backend-specific values.
  if [[ -f "$ROOT/.env" ]]; then
    set -o allexport
    # shellcheck disable=SC1091
    source "$ROOT/.env"
    set +o allexport
  fi
  if [[ -f "$ROOT/forge-server/.env" ]]; then
    set -o allexport
    # shellcheck disable=SC1091
    source "$ROOT/forge-server/.env"
    set +o allexport
  fi

  # Point opencode at our platform config (subagents + skills.paths) without
  # touching the user's ~/.config/opencode/. opencode reads xdgConfig/opencode/
  # which respects $XDG_CONFIG_HOME — see opencode/packages/core/src/global.ts.
  export XDG_CONFIG_HOME="$LOCAL_XDG"

  # Forge per-request internal auth. opencode's forge-user middleware verifies
  # X-Forge-Internal-Token with this; the agent-model resolver calls back into
  # forge-server at FORGE_API_URL. Both must match what forge-server sees.
  export FORGE_INTERNAL_SECRET="$FORGE_INTERNAL_SECRET"
  export FORGE_API_URL="$FORGE_API_URL"

  bun run --conditions=browser ./src/index.ts serve \
    --port "$OPENCODE_PORT" \
    --hostname 127.0.0.1 \
    2>&1 | pipe_prefix "opencode" '\033[0;35m'
) &
PIDS+=($!)

# ── Step 4: forge-server (FastAPI) ────────────────────────────────────────────
info "Starting forge-server on :${BE_PORT}…"
# Capture the intended values BEFORE the subshell so they survive .env sourcing.
# This matters because forge-server/.env can have a stale DATABASE_URL (e.g.
# the legacy SQLite path) which would otherwise clobber the Postgres URL we
# set at the top of this script.
INTENDED_DATABASE_URL="$DATABASE_URL"
INTENDED_FORGE_DATA_ROOT="$FORGE_DATA_ROOT"
(
  cd "$ROOT/forge-server"

  # Activate the venv for this subshell
  # shellcheck disable=SC1090
  source "$VENV/bin/activate"

  # Source forge-server/.env if present, then apply hard dev overrides
  if [[ -f ".env" ]]; then
    set -o allexport
    # shellcheck disable=SC1091
    source ".env"
    set +o allexport
  fi

  # Dev overrides — always applied when running via dev.sh. Re-export the
  # values we captured pre-subshell so a stale .env can't beat us.
  export DEV_MODE=true
  export DATABASE_URL="$INTENDED_DATABASE_URL"
  export FORGE_DATA_ROOT="$INTENDED_FORGE_DATA_ROOT"
  export OPENCODE_URL="http://127.0.0.1:${OPENCODE_PORT}"
  export PREVIEW_DOMAIN="${PREVIEW_DOMAIN:-preview.lvh.me}"
  export PREVIEW_SCHEME="${PREVIEW_SCHEME:-http}"
  # Same internal secret used by the opencode subshell to sign per-request
  # X-Forge-Internal-Token. Must match — verification will reject if they
  # drift.
  export FORGE_INTERNAL_SECRET="$FORGE_INTERNAL_SECRET"

  uvicorn forge_server.app:app \
    --reload \
    --host 0.0.0.0 \
    --port "$BE_PORT" \
    --timeout-graceful-shutdown 1 \
    2>&1 | pipe_prefix "forge-be" '\033[0;33m'
) &
PIDS+=($!)

# ── Step 5: forge-ui (Vite / SolidJS) ────────────────────────────────────
info "Starting forge-ui on :${FE_PORT}…"
(
  cd "$ROOT/forge-ui"

  # Tell the UI which OpenCode port and where forge-server lives
  export VITE_OPENCODE_SERVER_PORT="$OPENCODE_PORT"
  export VITE_API_URL="http://localhost:${BE_PORT}"

  bun run dev 2>&1 | pipe_prefix "forge-ui" '\033[0;36m'
) &
PIDS+=($!)

# ── Banner ────────────────────────────────────────────────────────────────────
sleep 1
echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${GREEN}  Forge dev environment ready!${RESET}"
echo -e ""
echo -e "  ${CYAN}Forge UI${RESET}        →  http://localhost:${FE_PORT}"
echo -e "  ${CYAN}forge-server${RESET}    →  http://localhost:${BE_PORT}"
echo -e "  ${CYAN}OpenCode${RESET}        →  http://localhost:${OPENCODE_PORT}"
echo -e "  ${CYAN}Postgres${RESET}        →  127.0.0.1:54322 (postgres/postgres)"
echo -e "  ${CYAN}Supabase Studio${RESET} →  http://127.0.0.1:54323"
echo -e "  ${CYAN}Projects${RESET}        →  ${FORGE_DATA_ROOT}/users/"
echo -e ""
echo -e "  Press ${BOLD}Ctrl+C${RESET} to stop all services"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""

# Open in browser on macOS
if [[ "$OSTYPE" == "darwin"* ]]; then
  (sleep 3 && open "http://localhost:${FE_PORT}") &
fi

wait
