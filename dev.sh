#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# dev.sh — Start all Forge services for local development
#
# Usage:
#   ./dev.sh
#
# This script is fully self-contained:
#   - Installs bun deps (opencode monorepo + forge-ui-new workspace)
#   - Creates a Python venv for forge-server and installs deps
#   - Starts all three services and opens the browser
#
# Services:
#   :7777  OpenCode server  (AI coding agent backend)
#   :8000  forge-server     (FastAPI — project CRUD, auth, config)
#   :3000  forge-ui-new     (Vite/SolidJS — the Forge UI)
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
# Note: the `supabase` CLI is only required if Postgres isn't already running
# on :54322. We check for it lazily inside the Supabase bootstrap step below.

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

# Supabase service role key for storage operations. dev.sh picks it up from
# Supabase CLI status output below; you can also set it manually via env or
# forge-server/.env. Empty value = snapshot worker no-ops cleanly.
SUPABASE_SERVICE_ROLE_KEY="${SUPABASE_SERVICE_ROLE_KEY:-}"

# ── Step 1: Bun deps (opencode monorepo — includes forge-ui-new workspace) ───
info "Checking bun dependencies…"
# forge-ui-new is a workspace in opencode/package.json, so install from there.
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
  # Priority order for DESIGN_ANALYST_MODEL:
  #  1. Already set in environment (highest precedence)
  #  2. user_settings table in the forge.db (set via UI → Settings → Manage Models)
  #  3. forge-server/.env fallback
  #  4. Hard-coded default: claude-sonnet-4-6

  # Single DB read returns both models so we don't open two psycopg2
  # connections at boot. Output format: "<primary>|<design>" — chosen because
  # neither model id contains a pipe. Empty halves are tolerated downstream.
  #
  # NOTE: this is a boot-time, last-row read and is multi-tenancy-incorrect by
  # construction (process-global, shared across all users). It's the explicit
  # stopgap noted in the BYOK rework — the proper fix is the opencode fork
  # passing per-request {user_id, model} so forge-server can resolve from the
  # right user_settings row on every call. Until that ships, this preserves
  # the dev-loop behaviour of "I picked a model in Settings, it sticks."
  if [[ -z "${DESIGN_ANALYST_MODEL:-}" || -z "${PRIMARY_MODEL:-}" ]]; then
    _MODEL_PAIR="$("$VENV/bin/python" -c "
import json, os, re
try:
    import psycopg2
except ImportError:
    raise SystemExit(0)
url = os.environ.get('DATABASE_URL') or '${DATABASE_URL}'
url = re.sub(r'^postgresql\+[a-z0-9]+://', 'postgresql://', url)
try:
    con = psycopg2.connect(url)
    cur = con.cursor()
    cur.execute('''
        SELECT us.settings_json FROM user_settings us
        JOIN users u ON u.id = us.user_id
        ORDER BY us.updated_at DESC LIMIT 1
    ''')
    row = cur.fetchone()
    if row:
        data = json.loads(row[0])
        print('%s|%s' % (data.get('primary_model', ''), data.get('design_model', '')))
except Exception:
    pass
" 2>/dev/null)"
    if [[ -z "${PRIMARY_MODEL:-}" ]]; then
      PRIMARY_MODEL="${_MODEL_PAIR%%|*}"
    fi
    if [[ -z "${DESIGN_ANALYST_MODEL:-}" ]]; then
      DESIGN_ANALYST_MODEL="${_MODEL_PAIR##*|}"
    fi
    unset _MODEL_PAIR
  fi

  if [[ -z "${DESIGN_ANALYST_MODEL:-}" && -f "$ROOT/forge-server/.env" ]]; then
    DESIGN_ANALYST_MODEL="$(grep -E '^DESIGN_ANALYST_MODEL=' "$ROOT/forge-server/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
  fi
  DESIGN_ANALYST_MODEL="${DESIGN_ANALYST_MODEL:-anthropic/claude-sonnet-4-6}"
  PRIMARY_MODEL="${PRIMARY_MODEL:-opencode/deepseek-v4-flash-free}"
  # Export so the opencode subshell sees it — picked up by the upcoming
  # per-request auth path in the opencode fork. Today's opencode ignores it,
  # which is fine: no behavioural regression.
  export PRIMARY_MODEL

  # Substitute container-only paths → host equivalents + the design model.
  # python3 over sed because paths/JSON contain chars sed would need escaped.
  python3 -c "
import json, sys
src = open(sys.argv[1]).read()
src = src.replace('\"/forge-skills\"',           json.dumps(sys.argv[2]))
src = src.replace('http://forge-llm-proxy:7799', sys.argv[3])
src = src.replace('__DESIGN_MODEL__',            sys.argv[4])
open(sys.argv[5], 'w').write(src)
" "$ROOT/forge-opencode-config/opencode.json" "$LOCAL_SKILLS" "$LOCAL_PROXY" "$DESIGN_ANALYST_MODEL" "$LOCAL_OC_DIR/opencode.json"
  ok "Platform opencode config installed → $LOCAL_OC_DIR/opencode.json"
  ok "  - skills.paths        → $LOCAL_SKILLS"
  ok "  - anthropic base      → $LOCAL_PROXY/v1"
  ok "  - design subagent     → $DESIGN_ANALYST_MODEL"
  ok "  - primary model       → $PRIMARY_MODEL (exported for opencode fork)"
else
  warn "forge-opencode-config/opencode.json missing — subagents won't be defined."
fi

(
  cd "$ROOT/opencode/packages/opencode"

  # Load API keys for opencode. Without this the opencode subshell launches
  # with no ANTHROPIC_API_KEY and every session goes "thinking → done" with
  # no response.
  #
  # We source TWO env files, in this order:
  #   1. $ROOT/.env             (root — MOONSHOT_API_KEY, GOOGLE_API_KEY,
  #                              the vendor keys opencode resolves via
  #                              {env:...} in opencode.json)
  #   2. $ROOT/forge-server/.env (legacy — kept for ANTHROPIC_API_KEY and
  #                              DESIGN_ANALYST_MODEL until consolidation)
  # Later file wins on collision — forge-server/.env can override the root
  # for backend-specific values.
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

  uvicorn forge_server.app:app \
    --reload \
    --host 0.0.0.0 \
    --port "$BE_PORT" \
    --timeout-graceful-shutdown 1 \
    2>&1 | pipe_prefix "forge-be" '\033[0;33m'
) &
PIDS+=($!)

# ── Step 5: forge-ui-new (Vite / SolidJS) ────────────────────────────────────
info "Starting forge-ui on :${FE_PORT}…"
(
  cd "$ROOT/forge-ui-new"

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
