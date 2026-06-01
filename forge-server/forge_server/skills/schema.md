# Schema Skill — Postgres migrations

## When to invoke

Invoke this skill whenever you are about to:

- Read or write a table that doesn't exist yet in the connected Supabase
- Add / drop a column on an existing table
- Add / modify an index, foreign key, check constraint, or trigger
- Enable RLS or change an RLS policy
- Create / modify any database function or extension

If the user asked for a feature that needs persisted data (todos, posts,
profiles, sessions — anything), the schema for it goes through this skill.

## What NOT to do — these are the failure modes you must avoid

- Do NOT call the Supabase Management API or dashboard SQL editor to create
  tables directly. That works once and then drifts from the codebase.
- Do NOT run raw `psql ... CREATE TABLE ...` to apply schema. Same problem.
- Do NOT skip writing the migration file because "it's just a quick column add".
  The file is the source of truth; the live DB just reflects it.
- Do NOT generate migrations that don't have a clear `up` AND `down` pair.

## How to identify the current project

The session is pinned to a workspace under
`/forge-data/users/<user>/projects/<project_id>/workspace`. The workspace IS
the project root — migrations live in `supabase/migrations/` relative to it.

```bash
PROJECT_ID=$(pwd | sed -n 's|.*/projects/\([^/]*\)/workspace.*|\1|p')
```

If `PROJECT_ID` is empty, stop — you're not inside a Forge workspace.

## The flow

### Step 1. Ensure the supabase/ workspace exists

```bash
test -d supabase || npx supabase init --workdir .
```

This creates `supabase/migrations/` and a `supabase/config.toml`. Idempotent —
skips if already present. Use `npx supabase` rather than a global install so
the version is pinned in package.json.

### Step 2. Write the migration file

File path: `supabase/migrations/YYYYMMDDHHMMSS_short_description.sql`.
The timestamp prefix makes file order deterministic and matches Supabase CLI
conventions exactly. Use the current UTC time when generating the prefix.

Each migration file is plain SQL. Always include both `up` and (when reversible)
a paired `down` migration file with `_down` suffix — or use Supabase's `BEGIN; ...
COMMIT;` form so a failed apply rolls back cleanly.

Example — adding a `todos` table:

```sql
-- supabase/migrations/20260527140000_create_todos.sql
BEGIN;

CREATE TABLE IF NOT EXISTS todos (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  title       text NOT NULL,
  completed   boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_todos_user_id ON todos (user_id);

-- RLS: each user sees only their own rows. ALWAYS include RLS for any
-- table that's exposed via PostgREST (i.e. queried from the browser).
ALTER TABLE todos ENABLE ROW LEVEL SECURITY;

CREATE POLICY "todos_select_own" ON todos
  FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "todos_insert_own" ON todos
  FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "todos_update_own" ON todos
  FOR UPDATE USING (auth.uid() = user_id);
CREATE POLICY "todos_delete_own" ON todos
  FOR DELETE USING (auth.uid() = user_id);

COMMIT;
```

### Step 3. Apply the migration

If the user has connected their own Supabase (the BYOK case), push to it:

```bash
npx supabase db push --linked
```

If the project is still on the Forge playground schema (no Supabase connected
yet), apply against the local Postgres directly:

```bash
npx supabase db reset --local   # or `supabase migration up --local`
```

The Forge runner container has access to both `supabase` CLI and `npx`.

### Step 4. Verify the migration applied cleanly

```bash
npx supabase db diff --schema public
```

This should print *nothing* — the live DB now matches the migration files. If
it prints anything, the migration is out of sync; fix the migration file
(don't poke the DB by hand).

### Step 5. Commit (or stage) the migration file

The migration file lives in the project workspace and travels with the code.
When the user eventually deploys to a hosted Supabase, the same `supabase db
push` against the linked remote project applies the same schema. No drift.

## Schema rules — non-negotiable

- Every table that's queryable from the browser MUST have RLS enabled and at
  least one policy. If you're not sure who should be able to see rows, ask the
  user; don't guess.
- Every user-facing table MUST have a `user_id uuid REFERENCES auth.users(id)
  ON DELETE CASCADE` column unless it's explicitly global.
- Every table MUST have `created_at timestamptz NOT NULL DEFAULT now()`.
- Tables that are mutated (not just appended) MUST also have `updated_at
  timestamptz NOT NULL DEFAULT now()` with a trigger to bump on UPDATE.
- Use `gen_random_uuid()` for primary keys, not `serial` / `bigserial`.
- Foreign keys MUST have `ON DELETE` behavior specified (`CASCADE` or `SET NULL`).
- Never use `DROP TABLE` in an up-migration without a paired `_down` that recreates it.

## Workspace layout

- Workspace path inside the dev container: `/app`
- Migrations live at: `/app/supabase/migrations/`
