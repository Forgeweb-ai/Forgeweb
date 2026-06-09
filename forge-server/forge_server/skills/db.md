# DB Skill — Drizzle ORM + Postgres (the ONLY way to do DB work in Forge)

## ⚠️ Common errors → the fix is ALWAYS `/db/provision`

If you see ANY of these in `/api/projects/.../runtime-errors`, in tool
output, in build logs, or anywhere else — the cause is the same and the
fix is the same:

| Error message | Cause | Fix |
|---|---|---|
| `role "user" does not exist` | DATABASE_URL is a placeholder `postgres://user:password@...` | Call `/db/provision`, write returned URL to `.env.local` |
| `role "postgres" does not exist` | Same — placeholder using the superuser name | Same |
| `database "mydb" does not exist` | Placeholder URL pointing at a database name that wasn't created | Same |
| `password authentication failed for user "X"` | Same — placeholder credentials | Same |
| `connect ECONNREFUSED 127.0.0.1:5432` | Code is talking to a non-Forge Postgres | Same — use Forge's URL from `/db/provision` |
| `DATABASE_URL is not defined` | `.env.local` wasn't written | Same |
| `[x] url: undefined` (from drizzle-kit) | DATABASE_URL not loaded — you ran raw `drizzle-kit` instead of `npm run db:*` | Use `npm run db:migrate` (NOT `npx drizzle-kit migrate`) |
| `forge-db-precheck` says "DATABASE_URL is missing from .env.local" | You haven't called `/db/provision` yet | The precheck stderr tells you the exact curl command — copy it |
| `forge-db-precheck` says "DATABASE_URL is a placeholder" | You wrote a fake URL | Same — call `/db/provision`, write the result |

**DO NOT** ask the user to provide a DATABASE_URL. They have no shell, no
terminal, no .env access. There is no path by which the user can supply
this — only the Forge platform can. If your draft final message contains
the phrase "provide a valid DATABASE_URL", "set DATABASE_URL in your env",
"configure your database connection", or anything like it, **delete that
sentence and call `/db/provision` instead**.

## ⚠️ EXACT COMMANDS — use these, do not improvise

**Rule: never invoke `drizzle-kit` directly. Always go through `npm run db:*`.**

The reason: `npm run db:generate` and `npm run db:migrate` are configured to
wrap drizzle-kit with `dotenv -e .env.local --` so `DATABASE_URL` is loaded
from `.env.local` automatically. If you run `npx drizzle-kit migrate` (or
`pnpm exec drizzle-kit ...`, or `./node_modules/.bin/drizzle-kit ...`,
etc.) the `.env.local` is NOT loaded and drizzle-kit immediately fails with:

    Error  Please provide required params for Postgres driver:
        [x] url: undefined

If you see that error, the fix is **not** to inline `DATABASE_URL=...`
in front of the command (the platform denies that and the URL contains a
role password that must never appear in chat). The fix is to use the right
script: `npm run db:migrate` (NOT `npx drizzle-kit migrate`).

All raw drizzle-kit invocations are denied. If a command below is rejected,
it means YOUR command is wrong, not that the platform is broken. Re-read
this section before trying a workaround.

| You want to… | RUN this exact command | DO NOT run |
|---|---|---|
| Generate a new migration after editing schema.ts | `npm run db:generate` | ❌ `pnpm exec drizzle-kit generate:sqlite` (banned — old subcommand syntax + SQLite) <br> ❌ `pnpm exec drizzle-kit generate:pg` (banned — old syntax) <br> ❌ `npx drizzle-kit generate` (works but bypasses .env.local — use `npm run db:generate` so DATABASE_URL is loaded) |
| Apply pending migrations | `npm run db:migrate` | ❌ `pnpm exec drizzle-kit migrate` (bypasses dotenv) <br> ❌ `drizzle-kit push:pg` (banned — old syntax + push is destructive) |
| Open Drizzle Studio | `npm run db:studio` | ❌ raw `drizzle-kit studio` (bypasses dotenv) |
| Provision the database (first time) | `curl -X POST "$FORGE_API_URL/api/projects/$PROJECT_ID/db/provision" -H "Authorization: Bearer $FORGE_API_TOKEN"` | ❌ `createdb`, `psql`, `pnpm exec drizzle-kit push` (all banned) |
| Install drizzle / pg | **Don't.** The bootstrap pins exact versions in package.json and `pnpm install` runs on container start. | ❌ `pnpm install drizzle-orm@0.30` (banned — old major) <br> ❌ `pnpm install drizzle-kit@0.20` (banned — old major) <br> ❌ `pnpm install better-sqlite3` (banned — SQLite) |

**The package versions are pinned for a reason.** drizzle-kit 0.20.x uses
the old `generate:<dialect>` subcommand syntax and still supports SQLite.
drizzle-kit 0.31+ uses the unified `generate` command with the dialect
read from `drizzle.config.ts` (which is Forge-owned and always says
`dialect: "postgresql"`). If you find yourself wanting to install an older
version, you're solving the wrong problem.

**The `drizzle.config.ts` file is Forge-owned and READ-ONLY.** Same for
`lib/db/client.ts` and `postcss.config.mjs`. Your Edit/Write tool will
return EACCES on these paths; bash `chmod`/`rm`/`mv`/`cat >`/`tee` against
them are denied by the platform. Even if you somehow get past all of
that, the `db:generate` / `db:migrate` scripts run a precheck FIRST that
auto-rewrites `drizzle.config.ts` back to `dialect: "postgresql"` before
drizzle-kit ever sees it — and the precheck exits 1 with a clear stderr
message naming exactly what was wrong, so you can fix `schema.ts` on the
next turn.

Don't waste a turn trying to edit these files. The schema lives in
`schema.ts` (writable, that's where you add tables). Everything else
is Forge's.

## When to invoke

Invoke this skill whenever the user asks for ANY of:

- "save / store / persist" anything (todos, posts, users, orders, settings)
- "add a table" / "add a column" / "schema change"
- "I need a database" / "track records" / "CRUD"
- Anything that needs a table to exist before you can write a route

If you find yourself about to write `CREATE TABLE`, `better-sqlite3`, `sqlite`,
`prisma`, `mongoose`, `sequelize`, `kysely`, or raw SQL DDL — STOP and read
this skill instead. Forge has exactly one DB stack and the agent must not
freelance.

## Hard rules — do not break these

1. **The ORM is Drizzle. Period.** No raw `pg` `Pool`/`Client` outside
   `client.ts`. No Prisma. No raw `CREATE TABLE` strings. No inline
   `db.execute(sql\`CREATE TABLE...\`)` for DDL.
2. **The dialect is Postgres.** Forge dropped SQLite entirely (LAUNCH_PLAN
   D9). If you see `sqliteTable` / `better-sqlite3` / `data.db` anywhere in
   this workspace, delete them — they're stale from an old project. Use
   `pgTable` and `drizzle-orm/pg-core` only.
3. **All schema lives in `src/lib/db/schema.ts`.** One source of truth.
   Forge's Data tab and migration tool both depend on this file existing
   at this exact path.
4. **The connection comes from `DATABASE_URL` in `.env.local`.** Forge wrote
   it there after provisioning a Postgres schema for this project. Never
   hardcode a URL, never echo `cat .env.local`, never paste `DATABASE_URL`
   into a chat message — the URL contains a role password.
5. **You do NOT swap drivers.** Never rewrite `client.ts` to use a different
   adapter. The same Drizzle/pg client works in local-self-host mode
   (Forge's Postgres via per-project schema) and in hosted/deploy mode
   (user's BYO Supabase). Mode is invisible to your code — it's just a
   different `DATABASE_URL`.
6. **Migrations are generated via `drizzle-kit`, not handwritten.** After
   you change `schema.ts`, run `npm run db:generate` to produce a migration
   file in `drizzle/`. Then `npm run db:migrate` applies it.
7. **Never put `.env`, `.env.local`, `drizzle/`, or `node_modules/` into
   git-tracked diffs** — they're runtime artifacts. `.env.local` carries
   the role password.

## How to identify the current project

```bash
PROJECT_ID=$(pwd | sed -n 's|.*/projects/\([^/]*\)/workspace.*|\1|p')
API_URL="${FORGE_API_URL:-http://forge-server:8000}"
TOKEN="${FORGE_API_TOKEN:-}"
```

If empty, stop — you're not in a Forge workspace.

## The flow

### Step 0. Ensure the DB is connected (first time only)

Check `/db/info` — it tells you whether a schema (provisioned-local) or
external Supabase is already connected to this project:

```bash
curl -sS "$API_URL/api/projects/$PROJECT_ID/db/info" \
     -H "Authorization: Bearer $TOKEN"
```

The response includes a `forge_mode` field and a `supabase` object.

- If `supabase.connected = true` → DB already wired. Skip to Step 1.
- If `supabase.connected = false` AND `forge_mode = "local-self-host"` →
  provision Forge's Postgres for this project (single tool call, no user
  prompts needed):
  ```bash
  RESP=$(curl -sS -X POST "$API_URL/api/projects/$PROJECT_ID/db/provision" \
              -H "Authorization: Bearer $TOKEN")
  # Extract database_url and write to .env.local. DO NOT echo $RESP in chat.
  URL=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['database_url'])")
  printf 'DATABASE_URL=%s\n' "$URL" >> .env.local
  ```
  **Never echo `$URL` or `$RESP` to the user.** The URL has a role password.
- If `supabase.connected = false` AND `forge_mode = "hosted"` → invoke the
  `supabase.md` skill instead. Hosted mode requires BYO Supabase via OAuth.

Then:
```bash
bash /usr/local/bin/forge-enable-db.sh
```
This runs the Drizzle scaffold + installs deps + applies the initial
migration. The script gates on `DATABASE_URL` being set, so order matters.

### Step 1. Add or modify a table

Open `src/lib/db/schema.ts` and add a Drizzle `pgTable` definition. Use only
`drizzle-orm/pg-core` builders — `serial`, `text`, `integer`, `boolean`,
`timestamp`, `jsonb`, `numeric`, etc.

```ts
// src/lib/db/schema.ts
import { pgTable, serial, text, boolean, timestamp, numeric } from "drizzle-orm/pg-core"
import { sql } from "drizzle-orm"

export const employees = pgTable("employees", {
  id:         serial("id").primaryKey(),
  firstName:  text("first_name").notNull(),
  lastName:   text("last_name").notNull(),
  email:      text("email").notNull().unique(),
  department: text("department").notNull(),
  role:       text("role").notNull(),
  salary:     numeric("salary", { precision: 12, scale: 2 }).notNull(),
  status:     text("status", { enum: ["Active", "On Leave", "Terminated"] })
                .notNull().default("Active"),
  startDate:  timestamp("start_date", { withTimezone: true }).notNull(),
  createdAt:  timestamp("created_at", { withTimezone: true }).notNull().default(sql`CURRENT_TIMESTAMP`),
  updatedAt:  timestamp("updated_at", { withTimezone: true }).notNull().default(sql`CURRENT_TIMESTAMP`),
})

export type Employee    = typeof employees.$inferSelect
export type EmployeeNew = typeof employees.$inferInsert
```

### Step 2. Generate + apply the migration

```bash
npm run db:generate    # writes drizzle/<n>_<hash>.sql
npm run db:migrate     # applies pending SQL to the connected schema
```

Both are idempotent. They wrap drizzle-kit with `dotenv -e .env.local --`
so the role password isn't required in the shell env.

### Step 3. Use the DB in routes

Import the shared client. Never instantiate your own.

```ts
// src/app/api/employees/route.ts
import { NextRequest, NextResponse } from "next/server"
import { db } from "@/lib/db/client"
import { employees } from "@/lib/db/schema"
import { desc } from "drizzle-orm"

export async function GET() {
  const rows = await db.select().from(employees).orderBy(desc(employees.createdAt))
  return NextResponse.json(rows)
}

export async function POST(req: NextRequest) {
  const body = await req.json()
  const [row] = await db.insert(employees).values(body).returning()
  return NextResponse.json(row, { status: 201 })
}
```

### Step 4. Seed data (optional)

If the user wants demo rows, write `src/lib/db/seed.ts` and run it once via
`npx tsx src/lib/db/seed.ts`. The seed file uses the same Drizzle client —
no raw SQL.

## Deploying / moving to a different Postgres

The user clicks **"Download project"** in the Forge UI — they get a `.zip`
that excludes `.env` / `.env.local` / `node_modules` / `.opencode`. To run
the app elsewhere, they:

1. Unzip, `npm install`.
2. Set `DATABASE_URL` in their target environment to their own Postgres
   (Supabase, Neon, RDS, etc.).
3. Run `npm run db:migrate` once to create the schema there.
4. Start the app.

The schema definitions in `schema.ts` are portable across any Postgres.
You don't need to touch driver code, connection logic, or migrations to
support deployment — that's the whole point of going Postgres-from-day-one.

If the user asks "how do I move this to my own Supabase?":

> "Click 'Download project' in the top bar, unzip it, set `DATABASE_URL`
> in your environment to your Supabase pooler URL, then `npm install &&
> npm run db:migrate && npm run dev`. Your schema is portable Postgres."

## What to do if the user explicitly asks for raw SQL

Use Drizzle's `db.execute(sql\`...\`)` for escape-hatch cases (recursive
CTE, vendor-specific Postgres function, window functions Drizzle's query
builder doesn't cover yet). Never `import { Pool } from "pg"` outside
`client.ts`.

## Failure modes to avoid

- ❌ `import Database from "better-sqlite3"` — Forge dropped SQLite.
- ❌ `sqliteTable` or `drizzle-orm/sqlite-core` imports.
- ❌ Looking for `data.db` — there isn't one.
- ❌ `pragma journal_mode = WAL` — that's SQLite. Postgres handles WAL.
- ❌ `cat .env.local` or echoing `DATABASE_URL` in any tool output that
  reaches the chat. The URL has a role password.
- ❌ Calling `db.execute(sql\`CREATE TABLE ...\`)` from a route. Always go
  through `schema.ts` + drizzle-kit.
- ❌ Adding a new ORM (Prisma, TypeORM, Sequelize) "for this one feature".
- ❌ Modifying `drizzle.config.ts` to point at a different DB. The URL is
  configured via env — never hardcoded.
- ❌ Telling the user to "run a manual migration" — `npm run db:migrate`
  is the only path.
