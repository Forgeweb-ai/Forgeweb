<p align="center">
  <img src="docs/images/forge-logo.png" alt="Forge logo" width="160">
</p>

# Forge — open-source, self-hosted alternative to Lovable & v0.dev

**Build webapps by chatting with an AI agent. Self-host it. Bring your own keys (or don't).**

Forge is the open-source, self-hosted alternative to [Lovable](https://lovable.dev) and [v0.dev](https://v0.dev). Describe the app you want, an AI agent ships the code, and you keep your API keys and your code on your own machine. There is no Forge SaaS, no token resale, no telemetry. The default model is free (DeepSeek V4 Flash via opencode zen) from opencode, so a fresh install works with zero API keys. Bring an Anthropic / OpenAI / Moonshot / Google / Replicate key from in-app Settings only if you want a paid model.

[Live page → forgeweb.ai](https://forgeweb.ai) · [License: BSL 1.1](LICENSE) · Built on [opencode](https://github.com/sst/opencode) (MIT)

---

## Quick start

You need **Docker Desktop** (macOS / Windows) or **Docker Engine + Compose v2** (Linux) installed and running. Nothing else on the host — no Python, no Node, no bun.

```bash
git clone https://github.com/Forgeweb-ai/Forgeweb.git
cd Forgeweb
docker compose up -d
```

Then open <http://app.forge.localhost> and sign up. First boot builds 4 images (~10 minutes on a clean machine); subsequent boots are seconds.

The stack self-bootstraps: compose creates the `forge-net` network, Postgres comes up healthy, `forge-server`'s entrypoint waits for it, runs `alembic upgrade head`, builds the per-project `forge-runner` image if missing, then starts uvicorn. The default model is opencode zen's free DeepSeek V4 Flash, so a fresh sign-up can chat immediately without any API keys.

To stop: `docker compose down`. To rebuild after pulling changes: `docker compose up -d --build`.

### For development (hot reload)

If you want to edit the source and see changes live, the source-mode install runs services natively with watch mode:

```bash
./install.sh
```

`install.sh` checks prerequisites (Docker, Python 3.11+, Node 20+ for the local Supabase, bun ≥1.3.14 — auto-installed), generates a JWT secret, pre-pulls base images, and hands off to `dev.sh`. After first install, daily restarts are just `./dev.sh`.

### Prerequisites

| | Docker path | `install.sh` path |
|---|---|---|
| Docker Desktop / Engine | required | required |
| Python 3.11+ | not needed | required |
| Node.js 20+ | not needed | required (for `npx supabase`) |
| bun ≥1.3.14 | not needed | auto-installed by `install.sh` |
| ~4 GB RAM, ~10 GB disk | required | required |
| Supported OSes | macOS, Linux, Windows + WSL2 | macOS, Linux, Windows + WSL2 |

### If `install.sh` fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `Can't reach https://bun.sh within 5s` | Corporate proxy, captive portal, or offline | Install bun manually from [bun.sh](https://bun.sh), then re-run `./install.sh`. |
| `$HOME is not writable` | Home directory on a NAS / SMB mount / locked-down corp Mac | Install bun system-wide, or clone Forge under a writable location with a writable `$HOME`. |
| `docker is installed but the daemon isn't running` | Docker Desktop not started | Start Docker Desktop (macOS/Windows) or `sudo systemctl start docker` (Linux). The script tries to start it for you on macOS first. |
| `npx supabase start` fails | Node not installed or wrong version | Install Node 20+ and re-run `./dev.sh`. The Docker path doesn't need this. |
| `chmod 600 failed` (warning, not fatal) | Filesystem doesn't honor POSIX modes (NAS/exFAT) | Move the repo to a local disk before adding real provider keys. |

Run `./install.sh --check` to dry-run prerequisite checks without changing anything.

The Docker path uses plain Postgres for Forge's own metadata; the `install.sh` path runs a local Supabase stack (more services, more disk). Both store your account, project list, and encrypted provider keys locally on your machine. Paid-model keys are added via in-app Settings after sign-up.

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

---

## Documentation

- [forgeweb.ai](https://forgeweb.ai) — full user-facing docs, architecture diagram, FAQ-style deep-dives.
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
