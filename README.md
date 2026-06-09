# Forge — open-source, self-hosted alternative to Lovable & v0.dev

**Build webapps by chatting with an AI agent. Self-host it. Bring your own keys (or don't).**

Forge is the open-source, self-hosted alternative to [Lovable](https://lovable.dev) and [v0.dev](https://v0.dev). Describe the app you want, an AI agent ships the code, and you keep your API keys and your code on your own machine. There is no Forge SaaS, no token resale, no telemetry. The default model is free (DeepSeek V4 Flash via opencode zen), so a fresh install works with zero API keys. Bring an Anthropic / OpenAI / Moonshot / Google / Replicate key from in-app Settings only if you want a paid model.

[Live page → forgeweb.ai](https://forgeweb.ai) · [License: BSL 1.1](LICENSE) · Built on [opencode](https://github.com/sst/opencode) (MIT)

---

## Quick start

Two install paths. Both stay on your machine. Pick one.

### Path A — Docker image (recommended)

Pulls prebuilt images from the container registry. Two commands, no source clone.

```bash
curl -fsSL https://raw.githubusercontent.com/Forgeweb-ai/Forgeweb/main/docker-compose.release.yml \
  -o docker-compose.yml
docker compose up -d
```

Then open <http://app.forge.localhost> and sign up.

### Path B — From source (for contributors)

Use this if you want to modify Forge itself.

```bash
git clone https://github.com/Forgeweb-ai/Forgeweb.git
cd forge
./install.sh
```

`install.sh` verifies prerequisites (Docker, Python 3.11+, bun, ~10 GB free disk), generates a JWT secret, and hands off to `dev.sh` to bring everything up.

### Prerequisites

**You install these yourself (they need admin / OS-level setup):**

- **Docker Desktop** (macOS / Windows) or **Docker Engine + Compose v2** (Linux) — must be installed *and* running before `./install.sh`. Docker needs admin rights on first install; if your machine is corp-locked and you can't get admin, Forge won't run.
- **Python 3.11+** — `brew install python@3.11` (macOS) or your distro's `python3.11` + `python3.11-venv` package.
- **Node.js + npx** — `dev.sh` boots the local Supabase via `npx supabase`. Install Node 20+ from [nodejs.org](https://nodejs.org) or via `brew install node` / your package manager.

**`install.sh` handles these automatically:**

- **bun** ≥1.3.14 — downloaded via the official one-liner into `~/.bun/bin` (no sudo). Needs network access to bun.sh and a writable `$HOME`.
- **JWT_SECRET** — generated via `openssl rand -hex 32` and written to `.env` (mode 600).
- **`.env` file** — copied from `.env.example` if missing.
- **Local Supabase** — Postgres + Auth + Storage + Studio booted by `dev.sh` via `npx supabase start`.

**Machine requirements:**

- ~4 GB free RAM
- ~10 GB free disk for images + project workspaces
- macOS, Linux, or Windows with WSL2

### If `install.sh` fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `Can't reach https://bun.sh within 5s` | Corporate proxy, captive portal, or offline | Install bun manually from [bun.sh](https://bun.sh), then re-run `./install.sh`. |
| `$HOME is not writable` | Home directory on a NAS / SMB mount / locked-down corp Mac | Install bun system-wide, or clone Forge under a writable location with a writable `$HOME`. |
| `docker is installed but the daemon isn't running` | Docker Desktop not started | Start Docker Desktop (macOS/Windows) or `sudo systemctl start docker` (Linux). |
| `npx supabase start` fails | Node not installed or wrong version | Install Node 20+ and re-run `./dev.sh`. |
| `chmod 600 failed` (warning, not fatal) | Filesystem doesn't honor POSIX modes (NAS/exFAT) | Move the repo to a local disk before adding real provider keys. |

Run `./install.sh --check` to dry-run all the checks without changing anything — useful before you commit to a full install.

The installer also brings up a **local Supabase** on your machine — that's where your account, your project list, and your encrypted provider keys live. You don't configure it, you don't connect to a hosted one; it just runs. Paid-model keys go in via in-app Settings after sign-up.

---

## What you get

**Chat with an AI agent that actually edits your code.** Type what you want; the agent reads and writes files in your project folder, runs commands, and shows the result in a live preview.

**Every project has a real folder.** Open it in your editor, run commands in it, commit it to git. Forge snapshots the folder every time the AI takes a turn, so you can roll back without losing anything you typed.

**An audit log for every paid AI call.** Every request your agent makes to a paid model is recorded to `forge-llm-proxy-logs/` on disk before it leaves your machine. Open the folder, read the JSON, see exactly what was sent and what came back.

**AI image generation that doesn't break the page.** The agent queues image jobs and shows placeholders until they finish. If your image provider is rate-limited, Forge retries quietly instead of leaving broken pictures in your page. If you haven't added an image-model key, it falls back to a stock-photo search.

**Self-host only.** There is no Forge SaaS, no Forge-controlled account server, no telemetry phoning home. The same Docker stack you start with `docker compose up` is the whole product.

---

## Bring your own keys

Forge doesn't resell tokens. You connect your own provider, you pay your own bill, you can cancel any time by removing the key.

| Provider | What it powers | Required? |
|---|---|---|
| **opencode zen** (default) | DeepSeek V4 Flash Free — chat + design out of the box | Default |
| Anthropic | Claude Sonnet 4.6, Opus 4.6, Haiku 4.5 chat models | Optional |
| OpenAI | GPT chat; gpt-image-1 for image gen | Optional |
| Moonshot | Kimi K2 chat | Optional |
| Google | Gemini chat + Imagen image gen | Optional |
| Replicate | Flux, SDXL, other open-weight image models | Optional |

All paid keys go in via **Settings → API Keys** inside the app. Never in `.env`. Encrypted on disk; decrypted only on the provider request that needs them.

### About Supabase

There are **two Supabases** in the story — worth keeping straight:

1. **Forge's own Supabase** — runs locally on your machine, set up automatically by the installer. This is where your Forge account, your project list, and your encrypted provider keys live. It is **not optional** — Forge needs a database to run.
2. **The Supabase your built apps connect to** *(optional)* — for the apps *you build* that need their own auth / database / file storage. Connect this from in-app Settings via OAuth. Forge holds a scoped delegation token only; it never sees your `service_role` JWT.

---

## How it works

```
Browser → forge-ui → forge-server → ┌─ opencode (the AI agent)
                                     ├─ forge-llm-proxy (audit log)
                                     ├─ Postgres / Supabase (Forge's storage)
                                     └─ runner containers (one per project preview)
```

When you send a chat message: forge-server forwards it to the opencode agent, the agent reads/writes files in your project folder, calls your chosen model via the audit-logging proxy, and streams the response back to the UI. Each project gets a live preview URL of its own; idle projects sleep automatically and wake when you visit them.

For deeper detail — what each service does, how snapshots work, how the BYOK key path is wired — see the [landing page docs](https://forgeweb.ai#docs) (same content, prettier).

---

## Documentation

- [`landing/index.html`](landing/index.html) — the full user-facing docs, including the architecture diagram and FAQ-style deep-dives.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — repo layout, dev environment, PR conventions.
- [`LICENSE`](LICENSE) — Business Source License 1.1.
- [`NOTICE`](NOTICE) — third-party attribution (opencode MIT, dependencies).

---

## Licensing

Forge platform code is [Business Source License 1.1](LICENSE). You can self-host, modify, contribute, and run your own apps freely. You can **not** resell Forge itself as a hosted AI-app-generation service. Four years after each release, that release converts to Apache 2.0.

The vendored `opencode/` subtree stays under its upstream [MIT license](opencode/LICENSE). See [`NOTICE`](NOTICE) for the full attribution.

---

## Built on opencode

Forge is built on [opencode](https://github.com/sst/opencode), the open-source AI coding agent. We extend it with multi-tenant workspaces, BYOK infrastructure, and a webapp-focused agent loop. opencode is MIT-licensed; we ship our additions under BSL 1.1. We say so openly because credit matters — and because you should always know what's running on your machine.

---

## Contributing

PRs welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the setup, the repo layout, and the conventions we follow.

Bug? Feature idea? Question? [Open an issue or discussion](https://github.com/Forgeweb-ai/Forgeweb/issues).
