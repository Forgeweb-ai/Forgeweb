#!/bin/sh
# forge-pkg-guard — wraps npm / pnpm / npx to reject Forge-banned packages.
#
# Installed via the runner image Dockerfile: the real npm/pnpm/npx binaries
# are renamed to `_real_<cmd>` and this script takes their original path on
# PATH. When invoked, we look at $0 to know which wrapper got called, scan
# the install args, and either reject with a clear error or transparently
# exec the real binary.
#
# Why this exists: model freelancing.  Even with explicit bans in AGENTS.md
# and the db.md skill telling the agent to use /db/provision + Drizzle+pg,
# weaker models (and sometimes stronger ones under load) reach for
# `better-sqlite3` / `prisma` / `sequelize` because those are overwhelmingly
# common in their training data. Prompt-level enforcement is necessary but
# not sufficient at this scale — platform-level enforcement is the durable
# fix. This script is the npm-side half of that; the runner's bash deny
# rules block raw psql / createdb on the bash-tool side.
#
# Error UX: when a banned package is requested we print a multi-line message
# that names the right recipe (the db.md skill at /forge-skills/db.md and
# the /db/provision endpoint). The model sees the error in tool output and
# reliably course-corrects on the next turn.

set -e

CMD_NAME="$(basename "$0")"
REAL="/usr/local/bin/_real_${CMD_NAME}"

# If the real binary doesn't exist we've been mis-installed — fall through
# to the system PATH lookup rather than blocking the user entirely. This
# should never fire in a correctly built image; failing safe in dev keeps
# a misconfigured rebuild from bricking the container.
if [ ! -x "$REAL" ]; then
  exec "$CMD_NAME" "$@" 2>/dev/null || {
    echo "[forge-pkg-guard] FATAL: $REAL not found and no fallback. Rebuild forge-runner:latest." >&2
    exit 127
  }
fi

# Packages we never want in a generated Forge app. Anything matching is
# rejected. The list is conservative — only the failure modes we've actually
# seen models reach for. Add new entries here when a new failure mode shows
# up; don't soften the existing ones without a clear reason in LAUNCH_PLAN.
BANNED="
better-sqlite3
@types/better-sqlite3
sqlite3
sqlite
prisma
@prisma/client
@prisma/migrate
sequelize
sequelize-typescript
typeorm
mongoose
knex
kysely
"

print_db_redirect() {
  cat >&2 <<'MSG'
[forge-pkg-guard] This package is not allowed in Forge projects.

  Forge uses Drizzle + node-postgres for ALL data persistence (LAUNCH_PLAN D9).
  Do NOT install ORMs or DB drivers by hand. Read /forge-skills/db.md and
  follow the recipe — it's three calls:

    1. POST ${FORGE_API_URL}/api/projects/${PROJECT_ID}/db/provision
    2. Write the returned database_url to .env.local (do NOT echo to chat)
    3. bash /usr/local/bin/forge-enable-db.sh

  This scaffolds Drizzle + pg, applies the initial migration, and registers
  the schema with the Data tab. Any other path produces a project that
  appears to work but breaks the moment the user opens Data.
MSG
}

# Subcommands across npm / pnpm / npx that can install or add packages.
SUBCMD="${1:-}"
case "$SUBCMD" in
  install|i|add|update|upgrade|create|exec)
    # Scan all args for banned package names. We accept "pkg" and "pkg@version"
    # forms; reject either way. Skip anything that starts with a dash (flag).
    for arg in "$@"; do
      case "$arg" in
        -*) continue ;;
      esac
      # Strip @version if present, leaving just the package name.
      # Handles bare "name@2.1", scoped "@scope/name@2", and "@scope/name".
      pkg="$arg"
      case "$arg" in
        @*)
          # Scoped pkg: keep @scope/name, strip @version after the second @
          pkg="$(printf '%s' "$arg" | awk -F'@' '{ if (NF<=2) print $0; else print "@"$2 }')"
          ;;
        *@*)
          # Bare pkg with @version → strip from first @
          pkg="${arg%%@*}"
          ;;
      esac
      for banned in $BANNED; do
        if [ "$pkg" = "$banned" ]; then
          echo "[forge-pkg-guard] '$arg' is in the banned list." >&2
          print_db_redirect
          exit 1
        fi
      done
    done
    ;;
esac

# Also catch the no-args install case: `npm install` / `pnpm install` with
# no args reads package.json. Scan it for banned packages so a model that
# edits package.json directly and then runs a plain install also gets
# rejected. Only fire when no positional args were given (subcommand only).
if [ "$#" -le 1 ] && [ -f package.json ]; then
  case "$SUBCMD" in
    install|i)
      for banned in $BANNED; do
        # grep for "banned": (with optional whitespace) in dependencies blocks.
        # No JSON parser in alpine sh — keep it simple, false-positives are
        # extremely unlikely for these specific package names.
        if grep -qE "\"$banned\"[[:space:]]*:" package.json 2>/dev/null; then
          echo "[forge-pkg-guard] package.json declares '$banned' which is in the banned list." >&2
          print_db_redirect
          exit 1
        fi
      done
      ;;
  esac
fi

# Not blocked — exec the real binary, preserving args.
exec "$REAL" "$@"
