#!/usr/bin/env bash
# docker_info.sh — snapshot Docker state for the current project.
#
# Usage:
#   bash .opencode/skills/terminal-support/scripts/docker_info.sh [project_name]
#
# If project_name is omitted, the compose project name is inferred from the
# directory name (docker compose's default). Output is plain text grouped by
# section so opencode can read it and reason about each piece.

set -u
PROJECT="${1:-$(basename "$PWD" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')}"

print_section() {
  echo
  echo "=============================="
  echo "== $1"
  echo "=============================="
}

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not installed" >&2
  exit 2
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: docker daemon not reachable" >&2
  exit 2
fi

# ---------- compose file & services ----------
COMPOSE_FILE=""
for f in docker-compose.yml docker-compose.yaml compose.yml compose.yaml; do
  [ -e "$f" ] && COMPOSE_FILE="$f" && break
done

print_section "project"
echo "name:    $PROJECT"
echo "compose: ${COMPOSE_FILE:-<none>}"

if [ -n "$COMPOSE_FILE" ]; then
  print_section "services (from compose)"
  docker compose -f "$COMPOSE_FILE" config --services 2>/dev/null || echo "(could not read compose)"
fi

# ---------- containers ----------
print_section "containers (docker ps -a, filtered)"
docker ps -a \
  --filter "label=com.docker.compose.project=$PROJECT" \
  --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null

# Fallback: also show containers whose names contain the project — useful for
# manually-run containers that don't carry compose labels.
echo
echo "-- name-matched (fallback) --"
docker ps -a --filter "name=$PROJECT" --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null

# ---------- images ----------
print_section "images"
docker images --filter "reference=*${PROJECT}*" --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}' 2>/dev/null

# ---------- networks ----------
print_section "networks"
docker network ls --filter "label=com.docker.compose.project=$PROJECT" --format 'table {{.Name}}\t{{.Driver}}\t{{.Scope}}' 2>/dev/null
echo
echo "-- name-matched (fallback) --"
docker network ls --filter "name=$PROJECT" --format 'table {{.Name}}\t{{.Driver}}\t{{.Scope}}' 2>/dev/null

# ---------- per-container detail ----------
CONTAINERS="$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}')"
if [ -z "$CONTAINERS" ]; then
  CONTAINERS="$(docker ps -a --filter "name=$PROJECT" --format '{{.Names}}')"
fi

for c in $CONTAINERS; do
  print_section "logs/$c (tail 80)"
  docker logs --tail 80 "$c" 2>&1 || echo "(no logs)"

  print_section "inspect/$c"
  docker inspect "$c" --format '{{json .State}}' 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "(no state)"
  echo
  echo "-- mounts --"
  docker inspect "$c" --format '{{range .Mounts}}{{.Type}}: {{.Source}} -> {{.Destination}}{{println}}{{end}}' 2>/dev/null
  echo "-- env (non-secret keys only) --"
  docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | grep -vE '(PASSWORD|SECRET|TOKEN|KEY)=' \
    | head -40
done

print_section "done"
