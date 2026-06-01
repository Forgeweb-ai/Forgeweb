---
name: terminal-support
description: Run-debug-fix loop for terminal commands and Docker. Detects success/failure from output, gathers project + docker context, and keeps iterating on code until commands succeed.
---

# terminal-support

Use this skill whenever you need to **run a command, judge whether it worked, and fix the code if it didn't** — especially Docker-based runs where the failure could be in the Dockerfile, compose file, app code, deps, env vars, ports, or runtime logs.

The skill is designed for an autonomous loop: opencode runs a command, the skill scripts decide pass/fail + classify the cause, opencode patches the code, then re-runs. Keep looping until the verdict is `OK` or the retry budget is exhausted.

---

## When to invoke this skill

Invoke `terminal-support` for any of:

- `docker compose up`, `docker build`, `docker run` failing or exiting non-zero
- Build / install / test commands failing (`npm`, `pnpm`, `bun`, `pip`, `cargo`, `go build`, `tsc`)
- Runtime crashes inside a container (port already in use, OOM, missing env, migration failure)
- "It ran but the app is broken" — health check failing, container restarting, 5xx in logs
- Any time the agent's next step is "run something and see what happens"

Do **not** invoke it for pure code-reading or design tasks — those don't need a run-loop.

---

## Prerequisites

The skill needs `bash`, `python3`, and `docker` available. Check once at the start of a session:

```bash
bash --version >/dev/null && python3 --version >/dev/null && docker --version >/dev/null && echo "terminal-support: prerequisites OK"
```

If `python3` is missing, install it the same way the `ui-ux-pro-max` skill does (`apk add python3` on alpine, `apt install python3` on debian, `brew install python3` on macOS). If `docker` is missing, stop and report — this skill can still partially work without it (project diagnosis + error parsing), but the docker loop will be skipped.

---

## The loop (read this first)

```
   ┌─────────────────────────┐
   │ 1. diagnose project     │  scripts/diagnose.sh
   │    (stack, package mgr) │
   └────────────┬────────────┘
                │
   ┌────────────▼────────────┐
   │ 2. snapshot docker      │  scripts/docker_info.sh
   │    (ps, logs, networks) │
   └────────────┬────────────┘
                │
   ┌────────────▼────────────┐
   │ 3. run the command      │  scripts/fix_loop.sh "<cmd>"
   │    capture stdout+stderr│
   └────────────┬────────────┘
                │
   ┌────────────▼────────────┐
   │ 4. classify result      │  scripts/parse_errors.py
   │    OK | FIXABLE | FATAL │
   └────────────┬────────────┘
                │
        OK ◄────┤───► FIXABLE ──► edit code per "suggested_fix" ──► back to step 3
                │
                └───► FATAL ──► stop, summarise for user
```

Keep a retry budget (default 5). If you exceed it, surface the last error to the user instead of grinding forever.

---

## Step 1 — Diagnose the current project

```bash
bash .opencode/skills/terminal-support/scripts/diagnose.sh
```

Emits JSON describing:
- workspace root and git branch
- detected stack (node / python / go / rust / mixed)
- package manager (`npm` / `pnpm` / `yarn` / `bun` / `pip` / `poetry` / `uv`)
- presence of `Dockerfile`, `docker-compose.yml`, `.dockerignore`
- declared services in compose
- declared scripts in `package.json`

Read this **before** running anything — it tells you which install/build/start command to use and which container names to inspect.

---

## Step 2 — Snapshot Docker state

```bash
bash .opencode/skills/terminal-support/scripts/docker_info.sh [project-name]
```

If `project-name` is omitted, the script infers it from the compose file directory. Output sections:

- `services` — declared in compose
- `containers` — `docker ps -a` filtered to this project, with state + health + uptime
- `images` — `docker images` for this project
- `networks` — `docker network ls` for this project, plus which containers are attached
- `logs/<service>` — last 80 lines of each running service's logs
- `inspect/<service>` — exit code, restart count, last error, mounts, env

This is the single command you run any time something behaves weirdly in Docker — it replaces a dozen ad-hoc `docker ps` / `docker logs` calls.

---

## Step 3 — Run the command through the loop

```bash
bash .opencode/skills/terminal-support/scripts/fix_loop.sh "<command>"
```

`fix_loop.sh` does **one** attempt (not the whole loop — opencode owns the loop, the script owns one iteration):

1. Runs `<command>` with a 10-minute timeout
2. Captures stdout + stderr to `.opencode/skills/terminal-support/.last_run.log`
3. Pipes the log through `parse_errors.py`
4. Prints a JSON verdict block at the end of stdout, e.g.:

```json
{
  "verdict": "FIXABLE",
  "exit_code": 1,
  "category": "node.missing_dependency",
  "evidence": "Cannot find module 'react-router-dom'",
  "suggested_fix": "Add 'react-router-dom' to dependencies and re-run install",
  "files_to_edit": ["package.json"],
  "retry_command": "npm install && npm run build"
}
```

opencode should parse that JSON, apply `suggested_fix` to `files_to_edit`, then call `fix_loop.sh` again with `retry_command` (or the same command).

Verdicts:
- `OK` — command succeeded. Stop the loop.
- `FIXABLE` — a recognised, code-level failure. Apply fix and retry.
- `FATAL` — host-level / out of scope (e.g. docker daemon not running, no disk). Stop and tell the user.

---

## Step 4 — Patch code, then retry

When verdict is `FIXABLE`:

1. Open every file in `files_to_edit`.
2. Apply the minimum change that addresses `suggested_fix`. **Do not refactor** unrelated code mid-loop — every extra edit makes the next failure harder to attribute.
3. If the fix is a missing dependency, prefer adding to the manifest (`package.json`, `requirements.txt`, `pyproject.toml`) over running an ad-hoc install — the install needs to be reproducible on the next container rebuild.
4. Call `fix_loop.sh` again with `retry_command`.

If the same `category` fails twice in a row, change strategy — re-read the full log, re-run `docker_info.sh`, and consider whether the diagnosis is wrong (e.g. the "missing module" is actually a path resolution issue).

---

## Error categories `parse_errors.py` recognises

| Category | Trigger phrases | Typical fix |
|---|---|---|
| `node.missing_dependency` | `Cannot find module`, `Module not found: Can't resolve` | Add to `package.json`, reinstall |
| `node.version_mismatch` | `engine "node" is incompatible`, `Unsupported engine` | Bump `engines.node` or Dockerfile base image |
| `node.typescript_error` | `error TS\d+`, `Type '.+' is not assignable` | Open the cited file:line and fix the type |
| `python.missing_module` | `ModuleNotFoundError`, `No module named` | Add to `requirements.txt` / `pyproject.toml` |
| `python.syntax` | `SyntaxError`, `IndentationError` | Fix cited file:line |
| `docker.port_in_use` | `port is already allocated`, `bind: address already in use` | Change host port mapping in compose |
| `docker.image_pull` | `pull access denied`, `manifest unknown`, `not found: manifest` | Fix image name/tag in compose / Dockerfile |
| `docker.build_failed` | `failed to solve`, `executor failed running` | Open Dockerfile at cited step, fix command |
| `docker.network_missing` | `network .* not found` | Recreate the network (`docker network create`) or fix compose `networks:` block |
| `docker.oom` | `Killed`, exit code `137` | Raise memory limit or shrink build context |
| `db.connection_refused` | `ECONNREFUSED`, `could not connect to server` | Check depends_on / healthcheck / DB env vars |
| `env.missing_var` | `is not set`, `KeyError: '.*'` from env lookups | Add to `.env`, document in `.env.example` |
| `permission.denied` | `EACCES`, `Permission denied` | Fix file mode / Dockerfile `USER` / volume mount |
| `unknown` | (nothing matched) | Don't guess — re-read the log manually |

`unknown` returns verdict `FIXABLE` only if exit code is non-zero AND there's a stack trace — otherwise it returns `FATAL` so opencode escalates to the user instead of looping blindly.

---

## Worked example

User: "the app won't start in docker"

```bash
# 1. Understand the project
bash .opencode/skills/terminal-support/scripts/diagnose.sh
# → stack=node, package_mgr=bun, services=[web, postgres, redis]

# 2. See what docker thinks
bash .opencode/skills/terminal-support/scripts/docker_info.sh
# → web container exited(1), logs show "Cannot find module '@auth/core'"

# 3. Run the command, get a verdict
bash .opencode/skills/terminal-support/scripts/fix_loop.sh "docker compose up --build web"
# → verdict: FIXABLE, category: node.missing_dependency, files_to_edit: ["package.json"]

# 4. Edit package.json to add @auth/core, then:
bash .opencode/skills/terminal-support/scripts/fix_loop.sh "docker compose up --build web"
# → verdict: OK
```

Report to the user: what was wrong, what was changed, current state. Don't paste the full log unless they ask.

---

## Anti-patterns

- **Don't loop without a budget.** Cap retries at 5. If still failing, surface to user.
- **Don't fix more than one thing per iteration.** Multi-fix iterations make root cause invisible.
- **Don't `docker system prune` to "fix" things.** It nukes state the user may need.
- **Don't run `rm -rf node_modules && npm install` reflexively.** Most failures are reproducible without it.
- **Don't suppress errors** (`|| true`, `2>/dev/null`) just to get a green exit code. The loop is the user's signal.
- **Don't run dev servers in this loop.** Long-running processes never "succeed" — use `--build` / `up -d` + healthcheck instead. (Forge's `AGENTS.md` already forbids `npm run dev` from the agent.)

---

## Files in this skill

```
.opencode/skills/terminal-support/
├── SKILL.md             ← this file
└── scripts/
    ├── diagnose.sh      ← project introspection (stack, manager, compose services)
    ├── docker_info.sh   ← docker state snapshot (ps, logs, inspect, networks)
    ├── parse_errors.py  ← classify a log into category + suggested_fix
    └── fix_loop.sh      ← single iteration: run command → classify → emit verdict JSON
```
