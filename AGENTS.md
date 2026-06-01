# Forge Agent Instructions

You are building web applications inside **Forge** — an AI-powered app builder. Follow these rules exactly.

---

## 0. Design-system-first (read this twice)

For ANY task that creates, modifies, or restyles user-facing UI you MUST invoke the
`ui-ux-pro-max` skill BEFORE writing UI code. The skill lives at
`.opencode/skills/ui-ux-pro-max/`.

Flow, in order, every time:

1. Read `.opencode/skills/ui-ux-pro-max/SKILL.md`.
2. Run the design-system generator:
   `python3 .opencode/skills/ui-ux-pro-max/scripts/search.py "<product> <industry> <style>" --design-system --persist -p "<Project>"`
3. Read `design-system/MASTER.md` — single source of truth for colors, typography, spacing, radius, shadows, pattern, and anti-patterns.
4. Expose every MASTER.md token via the `@theme` block in `app/globals.css` (Tailwind v4 — see §Tailwind below). Never hard-code a color/font/spacing value — only token references. **Do not create `tailwind.config.js` / `tailwind.config.ts`** — Tailwind v4 reads tokens from CSS.
5. Build sections as separate components using only those tokens.
6. Run MASTER.md's pre-delivery checklist before reporting done.

Hard NOs: no emoji as icons (use Lucide/Heroicons), no random Tailwind defaults
(`bg-blue-600`, `text-gray-500`), no Lorem Ipsum, no "AI purple-pink gradient"
unless MASTER.md recommends it, no skipping the responsive pass.

If `python3` is missing in the runtime, install it before retrying. If the skill
folder is missing, STOP and report — do not improvise a UI.

---

## 0b. Run-debug-fix loop (terminal-support skill)

For ANY task whose next step is "run something and see what happens" — build,
install, test, or anything inside Docker — you MUST invoke the
`terminal-support` skill instead of running raw shell commands. The skill lives
at `.opencode/skills/terminal-support/`.

Flow, in order:

1. Read `.opencode/skills/terminal-support/SKILL.md`.
2. Snapshot the project once: `bash .opencode/skills/terminal-support/scripts/diagnose.sh`.
3. Snapshot Docker once (only if Docker is in play): `bash .opencode/skills/terminal-support/scripts/docker_info.sh`.
4. Run the command through the loop:
   `bash .opencode/skills/terminal-support/scripts/fix_loop.sh "<command>"`
5. Read the `===VERDICT===` JSON block printed at the end of the output.
6. If `verdict == "OK"` → done. If `"FIXABLE"` → apply `suggested_fix` to
   `files_to_edit`, then re-run step 4. If `"FATAL"` → stop and surface to the
   user.
7. Cap retries at **5**. Don't fix more than one thing per iteration. Don't
   `docker system prune` or `rm -rf node_modules` reflexively.

This loop replaces ad-hoc `docker logs` / `docker ps` / "let me try again"
behaviour. Use it any time exit code matters.

---

## Tailwind (v4) — Forge-owned files, do not freelance

Forge ships Tailwind **v4**, set up at every container start by `forge-bootstrap.sh`. The setup is fixed; trying to "fix" it is the #1 cause of "my classes don't apply" — every model that rewrites these files breaks Tailwind for the user.

These four artefacts are **Forge-owned**. Do not edit, rename, or replace them:

- `postcss.config.mjs` — must load `@tailwindcss/postcss` (NOT `tailwindcss`).
- `app/globals.css` — must start with `@import "tailwindcss";` on line 1. Your `@theme { … }` block and custom CSS go BELOW it.
- `app/layout.tsx` import of `./globals.css` — must remain.
- `tailwindcss` + `@tailwindcss/postcss` in `devDependencies`.

**v4 rules (the v3 patterns models reach for do NOT work):**

- `@tailwind base; @tailwind components; @tailwind utilities;` — **v3 syntax, silently ignored in v4**. Use the single `@import "tailwindcss";` line at the top of `globals.css`.
- `import "tailwindcss/tailwind.css"` from a TS/JS file — **wrong**. The import lives in `globals.css`.
- `tailwind.config.js` / `tailwind.config.ts` — **v4 does not need a config file**. Tokens (colors, fonts, spacing) go in the `@theme { ... }` block inside `globals.css`. Example:

  ```css
  @import "tailwindcss";

  @theme {
    --color-bg: #FAF8F4;
    --color-ink: #1A1818;
    --font-display: "Playfair Display", Georgia, serif;
  }
  ```
  Tailwind v4 picks these up at build time and generates utilities like `bg-bg`, `text-ink`, `font-display` automatically. You do NOT need to declare them in a JS config.

- PostCSS plugin name is `@tailwindcss/postcss` (v4), not `tailwindcss` (v3). Do not change `postcss.config.mjs`.

If the user reports "styles aren't applying", **do not edit Tailwind setup files**. Re-read this section first, then look at the consumer (component) — the bug is almost always there.

---

## Database — OPT-IN, not default

Forge no longer scaffolds a database by default. A new project ships with no Drizzle, no `data.db`, no `app/api/items/*`, no `FORGE_DB.md`. The dev container starts faster and you don't fight a schema you didn't need.

**Enable a DB ONLY when the user explicitly asks for persistence** ("save my X", "tracker for Y", "list that survives refresh", "let users sign in", etc.). When that happens:

1. From your shell tool, inside `/app`:
   ```bash
   bash /usr/local/bin/forge-enable-db.sh
   ```
   This sets the `.forge/db-enabled` marker, lays down the Drizzle scaffold (`drizzle.config.ts`, `lib/db/schema.ts`, `lib/db/client.ts`, `app/api/items/*`, `FORGE_DB.md`), installs `drizzle-orm` + `better-sqlite3` + `drizzle-kit`, and applies the initial migration.

2. From there, follow `FORGE_DB.md` — copy the `items` table + routes, rename to the user's resource, generate + apply the migration.

**Do NOT** enable DB just because you *might* need persistence later. Don't `npm install drizzle-orm` by hand. Don't write `data.db` files. Don't import `better-sqlite3` in a project where the marker doesn't exist — your code won't have the deps and the container will crash on next restart.

If the user has not asked for persistence, use React state, props, and route params. That's the whole app.

---

## Communication style

- **Never include full file paths** in your responses. Use only the filename or a short relative path (e.g. `App.tsx`, `routes/index.ts`). Do NOT write `/Users/...` or `/home/...` or absolute paths.
- **Never write "To run:" messages** like `cd salon-booking && npm run dev`. Forge handles running automatically — just tell the user what you built.
- When you finish creating or modifying a project, say what was built and what it does. Do NOT give shell commands to run it.
- Keep responses concise. No need to list every file you created — summarise the outcome.

## Dev server

- Do NOT run `npm run dev`, `bun dev`, `python manage.py runserver`, or any other dev server command. Forge starts the dev server automatically in a managed container.
- Do NOT start any background processes or servers. Forge manages all lifecycle.
- If you need to install dependencies, run `npm install` (or `pip install -r requirements.txt`) **once** as part of setup, then stop.

## File writing — emit events

When you write or modify files, do it one file at a time and **flush after each write**. This allows Forge to stream file changes to the user interface in real time.

- Prefer smaller focused edits over large batched rewrites.
- After writing each file, pause briefly before the next one so the UI can render the change.

## Project structure

- Place all generated app code in the project workspace directory.
- Use a standard structure for the framework chosen (e.g. `src/`, `app/`, `components/`).
- Include a `package.json` (or equivalent) with a `dev` script so Forge knows how to start the server.

## What Forge shows

- Forge shows a **live preview** of the running app in an iframe — there's no need to tell the user to open a browser.
- Forge shows a **file tree** of all files you write in real time — no need to describe file locations.
- Forge shows a **todo list** of your current task plan — keep your tasks focused and under 10 items.

---

*These instructions are automatically read by opencode. Do not remove or modify this file.*
