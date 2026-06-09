# Contributing to Forge

Thanks for your interest in making Forge better. This doc covers how to set up a dev environment, how the repo is laid out, and how PRs work.

If you're here to just *use* Forge, the [README](README.md) is the right starting point — you don't need this doc.

---

## Quick dev setup

Forge has two install paths. For contributing, use the from-source path so you can edit the code and see changes immediately.

```bash
git clone https://github.com/Forgeweb-ai/Forgeweb.git
cd forge
./install.sh
```

`install.sh` runs once to verify prerequisites (Docker, Python 3.11+, bun, ~10 GB free disk), generate a JWT secret, and hand off to `./dev.sh`. For daily restarts use `./dev.sh` directly.

Services after boot:

- **forge-ui** — the chat UI, at `http://app.forge.localhost`
- **forge-server** — the API at `http://api.forge.localhost`
- **opencode** — the AI agent runtime
- **forge-llm-proxy** — audit log between the agent and any paid model
- **Supabase / Postgres** — Forge's own storage (set up automatically)

---

## How the repo is laid out

```
forge/
├── forge-ui/                 # SolidJS chat UI
├── forge-server/             # FastAPI orchestrator + BYOK vault
├── forge-llm-proxy/          # Audit log for every AI call
├── forge-opencode-config/    # Platform skills, subagents, instructions
├── opencode/                 # Vendored fork of opencode (keep close to upstream)
├── traefik/                  # Local routing
├── supabase/                 # Local Supabase config
├── landing/                  # The landing page at forgeweb.ai
├── install.sh                # First-run bootstrap
├── dev.sh                    # Daily start script
├── docker-compose.yml        # Source-build compose (for contributors)
└── docker-compose.release.yml # Released-image compose (for users)
```

**Rule of thumb on `opencode/`.** It's a vendored fork of [sst/opencode](https://github.com/sst/opencode), kept as close to upstream as possible. If your change is something upstream would accept (a generic bug fix, a provider addition, an SDK update), **file it upstream first** — that benefits the wider opencode community and means we can drop our patch when upstream merges. If your change is Forge-specific (BYOK middleware, our skill catalog, the design-pool, the verify subagent), it lives in our fork.

---

## Where changes should go

| You're adding / fixing… | It goes in… |
|---|---|
| The chat UI, settings dialogs, file tree, preview tabs | `forge-ui/` |
| API endpoints, BYOK encryption, image-job worker, sleep manager | `forge-server/` |
| The skill the agent reads when picking colors / building auth / etc. | `forge-opencode-config/` (or `forge-server/forge_server/skills/` for skills mounted into the agent container) |
| A new LLM provider, an SDK update, a generic agent improvement | `opencode/` — but file upstream first |
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

## Local Supabase notes

`dev.sh` runs `npx supabase start`, which boots Supabase's local stack on Docker. If `supabase status` ever wedges, `npx supabase stop && npx supabase start` from the repo root is the standard reset. The local Studio is at `http://localhost:54323`, the Postgres is at `localhost:54322`, and the service-role key for storage operations is captured into the forge-server env automatically.

If you're hacking on the snapshot worker or anything storage-related, browse the local Studio to see the actual rows — much faster than reading code.

---

## Licensing

Forge platform code is [Business Source License 1.1](LICENSE) (converting to Apache 2.0 four years after each release). Contributions to platform code are accepted under BSL 1.1.

The vendored `opencode/` subtree stays MIT (upstream's license). Contributions to `opencode/` are accepted under MIT, and we strongly prefer they go upstream first.

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
