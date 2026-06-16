# Contributing to Forge

Thanks for your interest in making Forge better. This doc covers how to set up a dev environment, how the repo is laid out, and how PRs work.

If you're here to just *use* Forge, the [README](README.md) is the right starting point — you don't need this doc.

---

## Quick dev setup

There are two ways to run Forge locally. For most contributor work — editing TS/Python source and seeing changes immediately — use the source path. Use the Docker path when you want to test the production-shape stack, reproduce a user-reported bug, or hack on the compose/entrypoint themselves.

**Source path (hot reload, recommended for code edits):**

```bash
git clone https://github.com/Forgeweb-ai/Forgeweb.git
cd Forgeweb
./install.sh
```

`install.sh` runs once: verifies prerequisites (Docker, Python 3.11+, Node 20+, bun ≥1.3.14 — auto-installed), generates a JWT secret, pre-pulls base images, and hands off to `./dev.sh`. Daily restarts are `./dev.sh` directly. Vite watches `forge-ui/`, uvicorn auto-reloads `forge-server/`, opencode runs from source under bun.

**Docker path (production-shape, no host deps beyond Docker):**

```bash
git clone https://github.com/Forgeweb-ai/Forgeweb.git
cd Forgeweb
docker compose up -d
```

Self-bootstrapping: compose creates the `forge-net` network, `forge-server`'s entrypoint waits for Postgres, runs `alembic upgrade head`, and builds the per-project `forge-runner` image. First build is ~10 min; rebuilds are seconds. Use this to verify a change works end-to-end against the same image set users will run.

Services after boot (either path):

- **forge-ui** — the chat UI, at <http://app.forge.localhost>
- **forge-server** — the API at <http://api.forge.localhost>
- **opencode** — the AI agent runtime (internal, on `forge-net`)
- **forge-llm-proxy** — audit log between the agent and any paid model
- **Postgres** — Forge's own storage. Source path runs Supabase (`:54322` + Studio at `:54323`); Docker path uses a plain `postgres:16-alpine`.

---

## How the repo is laid out

```
Forgeweb/
├── forge-ui/                 # SolidJS chat UI
├── forge-server/             # FastAPI orchestrator + BYOK vault
├── forge-llm-proxy/          # Audit log for every AI call
├── forge-opencode-config/    # Platform skills, subagents, instructions
├── opencode/                 # Vendored fork of opencode (keep close to upstream)
├── traefik/                  # Local routing
├── supabase/                 # Local Supabase config
├── install.sh                # First-run bootstrap
├── dev.sh                    # Daily start script
└── docker-compose.yml        # Source-build compose
```

**`opencode/` is frozen — don't modify it.** It's a vendored fork of [sst/opencode](https://github.com/sst/opencode), and Forge is tightly bound to this exact revision: the BYOK middleware, the skill catalog, the design-pool, and the verify subagent all depend on its current internals. **We are not accepting changes to `opencode/` for now — PRs that touch it will be closed.** If you've hit a bug that lives inside the agent runtime, open an issue describing it and we'll handle the fork bump on our side. Anything Forge-specific you'd want to change lives *outside* `opencode/` anyway — see the table below.

---

## Where changes should go

| You're adding / fixing… | It goes in… |
|---|---|
| The chat UI, settings dialogs, file tree, preview tabs | `forge-ui/` |
| API endpoints, BYOK encryption, image-job worker, sleep manager | `forge-server/` |
| The skill the agent reads when picking colors / building auth / etc. | `forge-opencode-config/` (or `forge-server/forge_server/skills/` for skills mounted into the agent container) |
| A change inside the agent runtime (new LLM provider, SDK bump, generic agent fix) | Nothing — `opencode/` is **frozen** right now. Open an issue, don't PR it. |
| A new image model | Provider registry in `forge-server/forge_server/imagegen/providers.py` + (optional) custom catalog entry |
| Landing-page copy / screenshots | `landing/` |

---

## PR conventions

- **One logical change per PR.** A typo fix plus an unrelated refactor in the same diff makes review hard and rolls back hard.
- **Tests where they make sense.** If you're touching a worker, an API endpoint, or a non-trivial helper, add a test. We won't block on tests for landing-page edits or doc fixes.
- **Run the checks.** Before opening a PR:
  ```bash
  # forge-server
  cd forge-server && .venv/bin/pytest && .venv/bin/ruff check .

  # forge-ui
  cd forge-ui && bun run typecheck && bun run lint
  ```
- **Migrations are reversible.** Any new Alembic migration in `forge-server/alembic/versions/` must include a working `downgrade()`. We will not merge "one-way" schema changes.
- **No secrets in the diff.** `.env` is gitignored. Test fixtures should use the well-known public example keys (`AKIAIOSFODNN7EXAMPLE` for AWS, etc.), never a real key.

---

## Local Supabase notes (source path only)

`dev.sh` runs `npx supabase start`, which boots Supabase's local stack on Docker. If `supabase status` ever wedges, `npx supabase stop && npx supabase start` from the repo root is the standard reset. The local Studio is at <http://localhost:54323>, the Postgres is at `localhost:54322`, and the service-role key for storage operations is captured into the forge-server env automatically.

If you're hacking on the snapshot worker or anything storage-related, browse the local Studio to see the actual rows — much faster than reading code.

The Docker path uses plain Postgres (no Supabase Storage), so snapshot worker no-ops cleanly there. If you're testing snapshot/storage features, use the source path.

## Docker path notes

`docker-compose.yml` mounts `./forge-opencode-config` read-write into the opencode container because opencode's `Config.loadInstanceState` writes a managed `.gitignore` and instance state into its config dir at startup. Repo-level `.gitignore` excludes opencode-generated files in that folder so they don't pollute `git status` — but if you see uncommitted `.gitignore` / `*.db` / instance-state files appear there, that's expected and gitignored.

`forge-server`'s entrypoint (`forge-server/docker-entrypoint.py`) runs alembic on every container start. Migrations must be idempotent — adding a one-way DDL change breaks restart for everyone, not just new installs.

---

## Licensing

Forge platform code is [Business Source License 1.1](LICENSE) (converting to Apache 2.0 four years after each release). Contributions to platform code are accepted under BSL 1.1.

The vendored `opencode/` subtree stays MIT (upstream's license), but it is **frozen** — we are not accepting contributions to it for now, since it's tightly bound to Forge's internals (see [How the repo is laid out](#how-the-repo-is-laid-out)).

By submitting a PR you confirm you have the right to license the contribution under the relevant license.

---

## Code style

We don't have a long style guide. The code base follows reasonable defaults:

- **Python:** `ruff` for lint, type hints where they help, docstrings on anything non-obvious. We're on Python 3.11+.
- **TypeScript / SolidJS:** TypeScript strict mode. Prefer `createSignal` for local state, Solid context for app-level state. Don't import React.
- **CSS:** Tailwind in `forge-ui`. For the landing page, plain CSS variables. Mind the cascade — check parent rules before introducing a new class.

---

## Questions, decisions, big features

Open a [GitHub Discussion](https://github.com/Forgeweb-ai/Forgeweb/discussions) before sinking a weekend into a big feature. We'd rather steer early than reject late.

For day-to-day "is this the right approach" — open the PR as a draft and tag it. Saves the back-and-forth.

---

Thanks again. The bar is "make Forge a little better than you found it." That's all.
