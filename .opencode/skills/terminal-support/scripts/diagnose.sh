#!/usr/bin/env bash
# diagnose.sh — inspect the current project and emit a JSON summary opencode
# can read before running build/install commands.
#
# Usage:
#   bash .opencode/skills/terminal-support/scripts/diagnose.sh [project_dir]
#
# Output: a single JSON object on stdout. Errors go to stderr.

set -u
PROJECT_DIR="${1:-$PWD}"
cd "$PROJECT_DIR" 2>/dev/null || { echo "{\"error\":\"cannot cd to $PROJECT_DIR\"}"; exit 1; }

# ---------- helpers ----------
json_escape() {
  # Escape backslashes, quotes, control chars for JSON string values.
  python3 - <<'PY' "$1"
import json, sys
print(json.dumps(sys.argv[1]))
PY
}

exists() { [ -e "$1" ]; }

# ---------- collect facts ----------
ROOT="$(pwd)"
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo "")"

STACK="unknown"
PACKAGE_MGR="unknown"
RUN_SCRIPT=""
BUILD_SCRIPT=""
INSTALL_CMD=""

# Node detection
if exists package.json; then
  STACK="node"
  if exists bun.lock || exists bun.lockb; then PACKAGE_MGR="bun"; INSTALL_CMD="bun install"
  elif exists pnpm-lock.yaml; then PACKAGE_MGR="pnpm"; INSTALL_CMD="pnpm install"
  elif exists yarn.lock; then PACKAGE_MGR="yarn"; INSTALL_CMD="yarn install"
  elif exists package-lock.json; then PACKAGE_MGR="npm"; INSTALL_CMD="npm install"
  else PACKAGE_MGR="npm"; INSTALL_CMD="npm install"
  fi
  RUN_SCRIPT="$(python3 -c "import json; d=json.load(open('package.json')); s=d.get('scripts',{}); print(s.get('start') or s.get('dev') or '')" 2>/dev/null || echo "")"
  BUILD_SCRIPT="$(python3 -c "import json; d=json.load(open('package.json')); s=d.get('scripts',{}); print(s.get('build') or '')" 2>/dev/null || echo "")"
fi

# Python detection (can coexist with node)
if exists pyproject.toml || exists requirements.txt || exists setup.py; then
  if [ "$STACK" = "unknown" ]; then STACK="python"; else STACK="${STACK}+python"; fi
  if exists poetry.lock; then PACKAGE_MGR="${PACKAGE_MGR}+poetry"
  elif exists uv.lock; then PACKAGE_MGR="${PACKAGE_MGR}+uv"
  elif exists requirements.txt; then PACKAGE_MGR="${PACKAGE_MGR}+pip"
  fi
fi

# Go / Rust
exists go.mod && { STACK="${STACK/unknown/go}"; PACKAGE_MGR="${PACKAGE_MGR/unknown/go}"; }
exists Cargo.toml && { STACK="${STACK/unknown/rust}"; PACKAGE_MGR="${PACKAGE_MGR/unknown/cargo}"; }

# Docker detection
HAS_DOCKERFILE=false; exists Dockerfile && HAS_DOCKERFILE=true
HAS_COMPOSE=false; COMPOSE_FILE=""
for f in docker-compose.yml docker-compose.yaml compose.yml compose.yaml; do
  if exists "$f"; then HAS_COMPOSE=true; COMPOSE_FILE="$f"; break; fi
done
HAS_DOCKERIGNORE=false; exists .dockerignore && HAS_DOCKERIGNORE=true

# Compose services (no yq dependency — parse top-level `services:` block)
COMPOSE_SERVICES="[]"
if [ "$HAS_COMPOSE" = "true" ]; then
  COMPOSE_SERVICES="$(python3 - "$COMPOSE_FILE" <<'PY'
import sys, json, re
path = sys.argv[1]
services = []
try:
    with open(path) as f:
        lines = f.readlines()
    in_services = False
    base_indent = None
    for line in lines:
        stripped = line.rstrip("\n")
        if not stripped.strip(): continue
        if re.match(r"^services:\s*$", stripped):
            in_services = True; continue
        if in_services:
            m = re.match(r"^(\s+)([A-Za-z0-9_.-]+):\s*$", stripped)
            if m:
                indent = len(m.group(1))
                if base_indent is None: base_indent = indent
                if indent == base_indent:
                    services.append(m.group(2))
            elif re.match(r"^\S", stripped):
                # top-level key — left the services block
                in_services = False
except Exception as e:
    print(json.dumps({"_error": str(e)})); sys.exit(0)
print(json.dumps(services))
PY
)"
fi

# Env files
ENV_FILES="[]"
ENV_FILES="$(python3 - <<'PY'
import os, json
out = []
for f in os.listdir("."):
    if f == ".env" or f.startswith(".env."):
        out.append(f)
print(json.dumps(sorted(out)))
PY
)"

# ---------- emit JSON ----------
python3 - "$ROOT" "$BRANCH" "$COMMIT" "$STACK" "$PACKAGE_MGR" "$INSTALL_CMD" "$RUN_SCRIPT" "$BUILD_SCRIPT" "$HAS_DOCKERFILE" "$HAS_COMPOSE" "$COMPOSE_FILE" "$HAS_DOCKERIGNORE" "$COMPOSE_SERVICES" "$ENV_FILES" <<'PY'
import sys, json
keys = ["root","branch","commit","stack","package_manager","install_command","run_script","build_script",
        "has_dockerfile","has_compose","compose_file","has_dockerignore","compose_services","env_files"]
vals = sys.argv[1:]
def coerce(k, v):
    if k in ("has_dockerfile","has_compose","has_dockerignore"):
        return v == "true"
    if k in ("compose_services","env_files"):
        try: return json.loads(v)
        except: return []
    return v
print(json.dumps({k: coerce(k, v) for k, v in zip(keys, vals)}, indent=2))
PY
