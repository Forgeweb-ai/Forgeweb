# DB Skill — Drizzle ORM (the ONLY way to do DB work in Forge)

## When to invoke

Invoke this skill whenever the user asks for ANY of:

- "save / store / persist" anything (todos, posts, users, orders, settings)
- "add a table" / "add a column" / "schema change"
- "I need a database" / "track records" / "CRUD"
- Anything that needs a table to exist before you can write a route

If you find yourself about to write `CREATE TABLE`, `better-sqlite3`,
`prisma`, `mongoose`, `sequelize`, `kysely`, or raw SQL DDL — STOP and read
this skill instead. Forge has exactly one DB stack and the agent must not
freelance.

## Hard rules — do not break these

1. **The ORM is Drizzle. Period.** No `better-sqlite3` direct usage. No
   Prisma. No raw `CREATE TABLE` strings. No inline `db.exec("...")` with DDL.
2. **All schema lives in `src/lib/db/schema.ts`.** One source of truth.
   Forge's Data tab and migration tool both depend on this file existing
   at this exact path.
3. **The default driver is SQLite via better-sqlite3 (through Drizzle).**
   The DB file lives at `data.db` in the workspace root. Don't move it.
4. **Switching to Supabase/Postgres is a Forge job, not yours.** Never
   rewrite `src/lib/db/client.ts` to swap drivers. The user clicks
   "Migrate to Supabase" in the Data tab and Forge handles it. If they ask
   you to migrate, tell them about the button and stop.
5. **Migrations are generated via `drizzle-kit`, not handwritten.** After
   you change `schema.ts`, run `npx drizzle-kit generate` to produce a
   migration file in `drizzle/`. Then `npx drizzle-kit migrate` applies it.
6. **Never write `data.db`, `drizzle/`, or `node_modules/` into git-related
   files** — they're runtime artifacts.

## How to identify the current project

```bash
PROJECT_ID=$(pwd | sed -n 's|.*/projects/\([^/]*\)/workspace.*|\1|p')
```

If empty, stop — you're not in a Forge workspace.

## The flow

### Step 1. Check the scaffold is present

Every Forge project is auto-scaffolded with the Drizzle skeleton at create
time. Verify it exists:

```bash
test -f src/lib/db/schema.ts && \
test -f src/lib/db/client.ts && \
test -f drizzle.config.ts || echo "MISSING_SCAFFOLD"
```

If `MISSING_SCAFFOLD` appears, the project pre-dates auto-scaffold or was
cloned from elsewhere. Run:

```bash
bash /forge-skills/db/scripts/init.sh
```

This idempotently writes the skeleton.

### Step 2. Add or modify a table

Open `src/lib/db/schema.ts` and add a Drizzle table definition. Use ONLY
Drizzle's `sqliteTable` / `integer` / `text` / `real` / `blob` builders —
they are dialect-agnostic and Forge's migration tool knows how to translate
them to Postgres column types automatically.

```ts
// src/lib/db/schema.ts
import { sqliteTable, integer, text, real } from "drizzle-orm/sqlite-core"
import { sql } from "drizzle-orm"

export const employees = sqliteTable("employees", {
  id:         integer("id").primaryKey({ autoIncrement: true }),
  firstName:  text("first_name").notNull(),
  lastName:   text("last_name").notNull(),
  email:      text("email").notNull().unique(),
  department: text("department").notNull(),
  role:       text("role").notNull(),
  salary:     real("salary").notNull(),
  status:     text("status", { enum: ["Active", "On Leave", "Terminated"] })
                .notNull().default("Active"),
  startDate:  text("start_date").notNull(),
  createdAt:  text("created_at").notNull().default(sql`(datetime('now'))`),
  updatedAt:  text("updated_at").notNull().default(sql`(datetime('now'))`),
})

export type Employee     = typeof employees.$inferSelect
export type EmployeeNew  = typeof employees.$inferInsert
```

### Step 3. Generate + apply the migration

```bash
npx drizzle-kit generate   # writes drizzle/0001_<hash>.sql
npx drizzle-kit migrate    # applies it to data.db
```

Both commands are idempotent. They use `drizzle.config.ts` at the workspace
root which Forge has already configured.

### Step 4. Use the DB in routes

Import the shared client. Never instantiate your own.

```ts
// src/app/api/employees/route.ts
import { NextRequest, NextResponse } from "next/server"
import { db } from "@/lib/db/client"
import { employees } from "@/lib/db/schema"
import { eq } from "drizzle-orm"

export async function GET() {
  const rows = await db.select().from(employees).orderBy(employees.createdAt)
  return NextResponse.json(rows)
}

export async function POST(req: NextRequest) {
  const body = await req.json()
  const [row] = await db.insert(employees).values(body).returning()
  return NextResponse.json(row, { status: 201 })
}
```

### Step 5. Seed data (optional)

If the user wants demo rows, write `src/lib/db/seed.ts` and run it once
via `npx tsx src/lib/db/seed.ts`. The seed file uses the same Drizzle
client — no raw SQL.

## Migration to Supabase / Postgres

You do NOT do this. The user clicks **"Migrate to Supabase"** in the Forge
Data tab. Forge's migration job:

1. Reads `schema.ts`.
2. Translates SQLite column types → Postgres (`integer` PK → `serial`,
   `text` → `text`, `real` → `double precision`, `blob` → `bytea`,
   sqlite `text` with enum constraint → `text CHECK (col IN (...))`).
3. `drizzle-kit push` against the Supabase URL to create the schema.
4. Copies rows from `data.db` to Supabase via streaming `INSERT`.
5. Updates `.env.local` so the app now points at Supabase.
6. Updates `src/lib/db/client.ts` to use the Postgres driver. Drizzle's
   query API stays identical, so the rest of the app is unchanged.

When a user asks "how do I move this to Supabase?", tell them:

> "Open the Data tab and click 'Migrate to Supabase'. Forge handles the
> schema, the row copy, and the driver swap. I don't need to touch your
> code."

If you start rewriting `client.ts` or copying rows by hand, you will break
the migration tool's invariants and the user's data will end up in two
half-synced places. Don't.

## What to do if the user explicitly asks for raw SQL

Tell them Drizzle's `db.run(sql\`...\`)` exists for escape-hatch cases (e.g.
recursive CTE, vendor-specific function) and use that — never raw
`better-sqlite3`.

## Failure modes to avoid

- ❌ Writing `import Database from "better-sqlite3"` anywhere outside
  `src/lib/db/client.ts`.
- ❌ Calling `db.exec("CREATE TABLE ...")` from a route or seed file.
- ❌ Adding a new ORM (Prisma, TypeORM, Sequelize) "for this one feature".
- ❌ Putting schema declarations inside a route handler.
- ❌ Writing a SQL string into `migrate.ts` instead of using drizzle-kit.
- ❌ Modifying `drizzle.config.ts` to point at a different DB file.
- ❌ Telling the user to "run a manual migration" — there is no such thing.
