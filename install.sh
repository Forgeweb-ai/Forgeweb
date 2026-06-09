#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# install.sh — One-time bootstrap for a fresh Forge clone.
#
# What it does (idempotent — safe to re-run):
#   1. Detects OS (macOS / Linux), refuses gracefully on Windows
#   2. Checks for required tools:
#        - docker        (must be installed AND running)
#        - python3 ≥3.11 (build instruction if missing)
#        - bun ≥1.3.14   (auto-installs via official one-liner if missing)
#        - lsof, curl, openssl  (port + env utilities)
#   3. Verifies ~10 GB free disk for Docker images + project workspaces
#   4. Creates .env from .env.example if missing and generates JWT_SECRET.
#      Does NOT prompt for any LLM key — Forge ships with a free default
#      model (opencode zen DeepSeek V4 Flash) and per-user paid keys are
#      entered in-app at Settings → API Keys, never .env.
#   5. Hands off to ./dev.sh to start every service
#
# Usage:
#   ./install.sh              # bootstrap + start (recommended for first run)
#   ./install.sh --no-start   # bootstrap only, don't run dev.sh
#   ./install.sh --check      # only run pre-flight checks, change nothing
#
# Daily development: just run ./dev.sh — it assumes install.sh ran once.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()  { echo -e "${CYAN}[install]${RESET} $*"; }
ok()    { echo -e "${GREEN}[install]${RESET} $*"; }
warn()  { echo -e "${YELLOW}[install]${RESET} $*"; }
die()   { echo -e "${RED}[install] ERROR:${RESET} $*" >&2; exit 1; }

# ── Args ──────────────────────────────────────────────────────────────────────
START_AFTER=true
CHECK_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --no-start) START_AFTER=false ;;
    --check)    CHECK_ONLY=true; START_AFTER=false ;;
    -h|--help)
      sed -n '2,22p' "$0"; exit 0 ;;
    *) die "Unknown flag: $arg (use --help)" ;;
  esac
done

# ── 1. OS detection ───────────────────────────────────────────────────────────
case "$(uname -s)" in
  Darwin)  OS=macos ;;
  Linux)   OS=linux ;;
  *)       die "Unsupported OS: $(uname -s). macOS and Linux only. (Windows users: run inside WSL2.)" ;;
esac
ok "OS detected: $OS"

# ── Network reachability (fail fast with a useful message) ───────────────────
# Some networks (corporate proxies, captive portals, air-gapped boxes) block
# the curl|bash bun installer. Detect this BEFORE we try to download anything
# so the user gets "your network can't reach bun.sh" instead of a long hang.
HAVE_NETWORK=true
if command -v curl >/dev/null 2>&1; then
  if ! curl -fsS --max-time 5 -o /dev/null https://bun.sh 2>/dev/null; then
    HAVE_NETWORK=false
    warn "Can't reach https://bun.sh within 5s."
    warn "  You may be offline, behind a corporate proxy, or on a captive portal."
    warn "  If you already have bun installed locally, this is fine."
    warn "  Otherwise install bun manually (https://bun.sh) and re-run."
  fi
fi

# ── \$HOME writable? (NAS-mounted homes, locked corp Macs, jailed shells) ────
# The bun installer writes to ~/.bun/bin. On a read-only / network-mounted
# \$HOME it will fail silently or leave nothing on PATH. Catch this up front.
HOME_WRITABLE=true
if ! ( touch "$HOME/.forge-install-write-test" 2>/dev/null && rm -f "$HOME/.forge-install-write-test" ); then
  HOME_WRITABLE=false
  warn "\$HOME ($HOME) is not writable."
  warn "  bun's installer drops files into ~/.bun and will fail here."
  warn "  Install bun system-wide (or to a writable location) and re-run."
fi

# ── Version-compare helper (semver-ish, returns 0 if $1 >= $2) ────────────────
ver_ge() {
  # Compare two dotted versions. Pads to 3 parts with zeros.
  local a b
  a=$(echo "$1" | awk -F. '{ printf("%d%03d%03d\n", $1,$2,$3) }')
  b=$(echo "$2" | awk -F. '{ printf("%d%03d%03d\n", $1,$2,$3) }')
  [[ "$a" -ge "$b" ]]
}

# ── 2. Required tools ────────────────────────────────────────────────────────
MISSING=()

# 2a. docker (installed + running)
if ! command -v docker >/dev/null 2>&1; then
  MISSING+=("docker")
  warn "docker not found."
  if [[ "$OS" == macos ]]; then
    warn "  Install Docker Desktop: https://docs.docker.com/desktop/install/mac-install/"
    warn "  Or via Homebrew:        brew install --cask docker"
  else
    warn "  Install Docker Engine:  https://docs.docker.com/engine/install/"
  fi
else
  if ! docker info >/dev/null 2>&1; then
    # Try to start Docker for the user instead of dying immediately. On macOS
    # this is `open -a Docker` (no admin prompt). On Linux it requires sudo,
    # which we don't want to silently prompt for, so we just print instructions.
    if [[ "$OS" == macos ]]; then
      info "Docker is installed but not running. Trying to start Docker Desktop…"
      if open -a Docker >/dev/null 2>&1; then
        info "  Docker Desktop launching — waiting up to 60s for daemon…"
        DAEMON_READY=false
        for i in $(seq 1 60); do
          if docker info >/dev/null 2>&1; then
            DAEMON_READY=true
            ok "Docker daemon ready (${i}s)"
            break
          fi
          sleep 1
          # progress dot every 10s
          (( i % 10 == 0 )) && info "  still waiting for Docker… (${i}/60s)"
        done
        $DAEMON_READY || die "Docker Desktop didn't become ready within 60s. Open it manually, wait for the whale icon to stop animating, then re-run."
      else
        die "Couldn't start Docker Desktop automatically. Open it manually, wait for the whale icon to stop animating, then re-run."
      fi
    else
      die "docker is installed but the daemon isn't running. Run 'sudo systemctl start docker' (Linux), then re-run."
    fi
  else
    ok "docker installed and running"
  fi
fi

# 2b. python3 ≥3.11
PY_MIN="3.11.0"
if ! command -v python3 >/dev/null 2>&1; then
  MISSING+=("python3")
  warn "python3 not found."
  [[ "$OS" == macos ]] && warn "  brew install python@3.11"
  [[ "$OS" == linux ]] && warn "  sudo apt install python3.11 python3.11-venv  (Debian/Ubuntu)"
else
  PY_VER=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
  if ver_ge "$PY_VER" "$PY_MIN"; then
    ok "python3 $PY_VER (≥ $PY_MIN)"
  else
    MISSING+=("python3>=$PY_MIN")
    warn "python3 $PY_VER is too old. Need ≥ $PY_MIN."
  fi
fi

# 2c. bun (auto-install if missing — official one-liner, no sudo)
BUN_MIN="1.3.14"   # pinned in opencode/package.json "packageManager"
install_bun() {
  info "Installing bun ($BUN_MIN+) via the official installer…"
  # Capture installer output so we can surface a useful error if it fails,
  # instead of just "bun install failed — see https://bun.sh".
  local log; log="$(mktemp -t forge-bun-install.XXXXXX)"
  if ! curl -fsSL https://bun.sh/install | bash >"$log" 2>&1; then
    echo "--- bun installer output (last 20 lines) ---" >&2
    tail -n 20 "$log" >&2 || true
    echo "--------------------------------------------" >&2
    rm -f "$log"
    die "bun install failed. Common causes: corporate proxy blocking bun.sh, \
no write access to ~/.bun, or unzip missing. See https://bun.sh for manual install."
  fi
  rm -f "$log"
  # The installer adds bun to ~/.bun/bin — load it into THIS shell.
  export BUN_INSTALL="${BUN_INSTALL:-$HOME/.bun}"
  export PATH="$BUN_INSTALL/bin:$PATH"
  command -v bun >/dev/null 2>&1 || die "bun installed but not on PATH. Add to your shell rc: export PATH=\"\$HOME/.bun/bin:\$PATH\""
  ok "bun installed: $(bun --version)"
}
if ! command -v bun >/dev/null 2>&1; then
  if ! $HAVE_NETWORK || ! $HOME_WRITABLE; then
    MISSING+=("bun")
    warn "bun not found and cannot be auto-installed here"
    warn "  (no network and/or \$HOME not writable). Install it manually"
    warn "  from https://bun.sh and re-run."
  elif $CHECK_ONLY; then
    MISSING+=("bun")
    warn "bun not found (would auto-install on real run)."
  else
    install_bun
  fi
else
  BUN_VER=$(bun --version)
  if ver_ge "$BUN_VER" "$BUN_MIN"; then
    ok "bun $BUN_VER (≥ $BUN_MIN)"
  else
    warn "bun $BUN_VER is older than pinned $BUN_MIN — upgrading…"
    $CHECK_ONLY || install_bun
  fi
fi

# 2d. Misc utilities used by dev.sh
for tool in lsof curl openssl; do
  command -v "$tool" >/dev/null 2>&1 || { MISSING+=("$tool"); warn "$tool not found (used by dev.sh)"; }
done

if (( ${#MISSING[@]} > 0 )); then
  echo
  die "Missing prerequisites: ${MISSING[*]}. Install them and re-run."
fi

# ── 3. Disk space (Docker images: Supabase stack ~6 GB, runner ~2 GB) ─────────
NEED_GB=10
if [[ "$OS" == macos ]]; then
  FREE_GB=$(df -g / | awk 'NR==2 {print $4}')
else
  FREE_GB=$(df -BG --output=avail / | awk 'NR==2 {gsub("G",""); print $1}')
fi
if (( FREE_GB < NEED_GB )); then
  warn "Only ${FREE_GB} GB free on /. Forge needs ~${NEED_GB} GB for Docker images + project workspaces."
  warn "Free some space, then re-run. Continuing anyway (it may fail later)…"
else
  ok "Disk space OK: ${FREE_GB} GB free (need ~${NEED_GB} GB)"
fi

# ── 4. .env bootstrap ────────────────────────────────────────────────────────
ENV_FILE="$ROOT/.env"
ENV_EXAMPLE="$ROOT/.env.example"

if [[ -f "$ENV_FILE" ]]; then
  ok ".env already present — leaving it alone"
else
  if $CHECK_ONLY; then
    warn ".env is missing (would be created on real run)"
  else
    [[ -f "$ENV_EXAMPLE" ]] || die ".env.example missing — can't bootstrap .env"
    info "Creating .env from .env.example…"
    cp "$ENV_EXAMPLE" "$ENV_FILE"

    # 4a. Auto-generate JWT_SECRET (never ship the placeholder)
    JWT="$(openssl rand -hex 32)"
    # Portable sed -i (macOS BSD vs GNU)
    if [[ "$OS" == macos ]]; then
      sed -i '' "s|^JWT_SECRET=.*|JWT_SECRET=$JWT|" "$ENV_FILE"
    else
      sed -i "s|^JWT_SECRET=.*|JWT_SECRET=$JWT|" "$ENV_FILE"
    fi
    ok "JWT_SECRET generated"

    # 4b. NO LLM-key prompt.
    # Forge ships with a free default model (opencode zen DeepSeek V4 Flash)
    # so a fresh install can chat immediately. Users add paid-model keys
    # (Anthropic / OpenAI / Moonshot / Google / Replicate) from in-app
    # Settings → API Keys, where they're Fernet-encrypted in Postgres and
    # injected per-request by the resolver. Nothing to gather here.

    if chmod 600 "$ENV_FILE" 2>/dev/null; then
      ok ".env created (mode 600 — readable only by you)"
    else
      warn ".env created, but chmod 600 failed."
      warn "  Filesystem may not support POSIX permissions (NAS / exFAT / SMB share)."
      warn "  Move the repo to a local disk if you need real secrets protection."
    fi
  fi
fi

# ── 5. Pre-pull base container images ────────────────────────────────────────
# Surface network failures HERE (in install.sh, before any service tries to
# boot) instead of mid-boot in dev.sh where the error is harder to interpret.
# Only pulls images dev.sh + docker-compose.yml actually use directly; Supabase
# pulls its own image set when `npx supabase start` runs.
#
# Skipped in --check mode (would touch the network) and when Docker is missing.
if ! $CHECK_ONLY && command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  PREPULL_IMAGES=( "traefik:v3.0" )
  info "Pre-pulling base images so first boot is offline-tolerant…"
  PULL_FAILED=()
  for img in "${PREPULL_IMAGES[@]}"; do
    if docker image inspect "$img" >/dev/null 2>&1; then
      ok "  $img — already present"
    else
      info "  pulling $img …"
      if docker pull --quiet "$img" >/dev/null 2>&1; then
        ok "  $img — ready"
      else
        warn "  $img — pull failed (will retry on first boot)"
        PULL_FAILED+=("$img")
      fi
    fi
  done
  if (( ${#PULL_FAILED[@]} > 0 )); then
    warn "Some images failed to pull (${PULL_FAILED[*]}). dev.sh will retry on first boot;"
    warn "  if it fails again, check your network / Docker Hub access."
  fi
fi

# ── 6. Pre-flight done ───────────────────────────────────────────────────────
echo
ok "Pre-flight checks passed."

if $CHECK_ONLY; then
  ok "(--check mode) Skipping start."
  exit 0
fi

if ! $START_AFTER; then
  ok "Bootstrap complete. Run ./dev.sh when you're ready."
  exit 0
fi

# ── 6. Hand off to dev.sh ────────────────────────────────────────────────────
echo
info "Starting Forge via ./dev.sh …"
[[ -x "$ROOT/dev.sh" ]] || die "dev.sh not found or not executable at $ROOT/dev.sh"
exec "$ROOT/dev.sh"
