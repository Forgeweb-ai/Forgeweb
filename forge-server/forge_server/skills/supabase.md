# Supabase Skill

## When to invoke

Invoke this skill whenever the user asks for ANY of:

- "add login" / "registration" / "sign up" / "auth" / "users"
- "magic link" / "OAuth" / "Google sign-in" / "session"
- "connect Supabase" or mentions Supabase by name
- "set up a database" / "add a db" — but only in `hosted` mode (in
  `local-self-host` use `db.md` Step 0 instead — single call, no user prompts)

## Step 0 — Mode detection (decides EVERYTHING below)

Forge runs in one of two modes. The flow is completely different per mode.
Detect once, then branch:

```bash
PROJECT_ID=$(pwd | sed -n 's|.*/projects/\([^/]*\)/workspace.*|\1|p')
API_URL="${FORGE_API_URL:-http://forge-server:8000}"
TOKEN="${FORGE_API_TOKEN:-}"

MODE=$(curl -sS "$API_URL/api/projects/$PROJECT_ID/db/info" \
       -H "Authorization: Bearer $TOKEN" \
       | python3 -c "import json,sys; print(json.load(sys.stdin)['forge_mode'])")
echo "mode=$MODE"
```

### `MODE = local-self-host` (OSS self-hosted Forge)

**Skip every "ask the user for URL / anon key / service_role key" step
below.** Forge can provision a Postgres schema for the project with a single
tool call — no user prompts.

If the trigger was a plain DB ask ("add a db", "I need to store X") →
**stop reading this skill** and use `db.md` Step 0 instead. It's faster
and cleaner.

If the trigger was an AUTH ask ("add login", "Google sign-in") in
local-self-host mode → tell the user:

> "I've set up a Postgres database for you. If you want Supabase Auth
> (Google sign-in, magic links, sessions), you'll need to connect your
> own Supabase project — Forge's local Postgres doesn't bundle Supabase
> Auth. Want to do that now?"

If they say yes → proceed with the BYO Supabase flow below, starting at
Step 3a (OAuth). The provisioned local Postgres stays for app data; the
BYO Supabase layers on top just for Auth/Storage.

### `MODE = hosted` (Forge SaaS)

Use the BYO Supabase flow below. **Always try Step 3a (OAuth) first.**
The manual-paste flow (Step 3b) is a fallback only — pasting
`service_role_key` into chat puts a JWT into the persisted transcript,
which is a real secret-in-transcript risk. Only fall back to paste if
OAuth is not configured (`/api/supabase/oauth/start` returns 503) or the
user explicitly insists on pasting.

---

## BYO Supabase flow

The platform endpoint at `/api/supabase/connect` injects env vars into the
workspace for you, and this skill knows how to wire those env vars into each
supported stack.

## How to identify the current project

The opencode session is pinned to a workspace under
`/forge-data/users/<user>/projects/<project_id>/workspace`. Derive the project
id and API helpers at the top of every shell block:

```bash
PROJECT_ID=$(pwd | sed -n 's|.*/projects/\([^/]*\)/workspace.*|\1|p')
API_URL="${FORGE_API_URL:-http://forge-server:8000}"
TOKEN="${FORGE_API_TOKEN:-}"
```

If `PROJECT_ID` is empty, stop — you're not inside a Forge workspace.

---

## The flow — follow this order

### Step 1. Detect the stack

Read `package.json` (or `requirements.txt` / `pyproject.toml`). You need to
know this before scaffolding because env-var prefixes and import paths differ:

| Stack          | Env prefix            | Client file                  |
|----------------|-----------------------|------------------------------|
| Next.js        | `NEXT_PUBLIC_*`       | `lib/supabase.ts`            |
| Vite + React   | `VITE_*`              | `src/lib/supabase.ts`        |
| FastAPI        | bare `SUPABASE_*`     | `backend/db/supabase.py`     |

Forge's `/api/supabase/connect` writes ALL of these prefixes to `.env` and
`.env.local`, so once connection lands, any of these stacks reads the right
vars. You only need stack detection to pick the import path.

### Step 2. Check current connection state

```bash
curl -sS "$API_URL/api/supabase/status?project_id=$PROJECT_ID" \
  -H "Authorization: Bearer $TOKEN"
```

- `{"connected": false}` → go to Step 3a (OAuth, default).
- `{"connected": true, "supabase_url": "..."}` → tell the user:
  > "Supabase is already connected to `<url>`. Reuse it, or connect a different project?"
  Skip to Step 6 (scaffold) if reusing.

### Step 3a — OAuth connect (DEFAULT, do this first)

OAuth is the default path. It's strictly better than the manual-paste flow
because no secret ever passes through the chat transcript: the user clicks
Connect in their browser, Supabase delegates a scoped token to Forge, and
Forge stores the token encrypted server-side. We never see the
service_role JWT.

Use the manual-paste flow (Step 3b) ONLY if OAuth is not configured
on this instance (the /start endpoint returns 503) or the user explicitly
insists on pasting keys.

**Check whether the user already has Supabase OAuth connected:**

```bash
RESP=$(curl -sS "$API_URL/api/supabase/oauth/status" \
  -H "Authorization: Bearer $TOKEN")
echo "$RESP"
```

- `{"connected": true, ...}` → skip to Step 3a.2 (pick or provision a project).
- `{"connected": false}` → Step 3a.1 (kick off OAuth).
- `503` → OAuth not configured on this instance. Fall back to Step 3b.

#### Step 3a.1 — Kick off OAuth (only if not connected)

Tell the user:

> I'll connect your Supabase account so I can set this up without you
> having to copy any keys. Click **Connect Supabase** in the Forge UI
> (top-right account menu) — Supabase will ask you to allow Forge, then
> bring you back here. Tell me "done" when you're back.

If the Forge UI doesn't surface a Connect button (older builds), get the
authorize URL directly:

```bash
curl -sS "$API_URL/api/supabase/oauth/start" \
  -H "Authorization: Bearer $TOKEN"
# → {"authorize_url": "...", "state": "..."}
```

And surface that URL to the user. Then wait. Re-check
`/api/supabase/oauth/status` after they say they're back. Do not proceed
to scaffolding until status returns `connected: true`.

#### Step 3a.2 — Pick or provision a project

Once OAuth is connected, the user either already has a Supabase project
they want this app to use, or wants a new one. Ask:

> You're connected. Want me to spin up a new Supabase project for this
> app, or use an existing one of yours?

If new → POST `/api/supabase/oauth/provision` with `{name, region, org_id,
db_pass}`. The response gives back `project_ref`, `anon_key`, `url` —
hand off to Step 4 with `connect` using those.

If existing → ask which one (by URL). Then go to Step 4. Either way: no
service_role key passes through chat — provisioning and management calls
go through the Forge-held OAuth token server-side.

### Step 3b — Manual paste (fallback only)

Use this ONLY when Step 3a is unavailable (OAuth not configured on this
instance) OR the user explicitly insists. Warn the user once before
starting: pasting `service_role_key` into chat puts a JWT into the
persisted transcript and they should rotate it after the session if
they're concerned about long-term transcript storage.

Do NOT dump a bulleted list of three things and wait. Ask sequentially. This
matters because users paste keys in the wrong field constantly when given
three blanks at once.

**Ask exactly this, first:**

> OAuth isn't available here, so I'll need a few values from your Supabase
> project. First — do you have a Supabase project already, or should I
> walk you through creating one?
> If you have one: open it, go to **Project Settings → API**, and paste your
> **Project URL** here (looks like `https://xxxxx.supabase.co`).

Wait for the URL. Validate it matches `^https://[a-z0-9-]+\.supabase\.co/?$` — if
not, ask them to recheck (the user may have pasted the dashboard URL by
mistake).

**Then ask:**

> Got it. Now paste your **anon / public key** from the same page. It's a
> long string that starts with `eyJ` (a JWT).

Wait for the anon key. Validate it starts with `eyJ` and is > 100 chars.

**Then ask (this one is optional but recommended):**

> Last one — paste your **service_role key**, also from the API page.
> This is the secret one (keep it private). It's needed if your app does any
> server-side database operations. You can skip it if you only need
> client-side reads — just say "skip". You can also rotate this key from
> your Supabase dashboard after we're done if you'd rather not leave it in
> the chat transcript.

Accept "skip" / "no" / blank as null.

### Step 4. POST /api/supabase/connect

```bash
SUPA_URL="<the URL the user provided>"
SUPA_ANON="<the anon key the user provided>"
SUPA_SRK="<the service_role key, or empty>"

PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({
  'project_id': sys.argv[1],
  'supabase_url': sys.argv[2],
  'anon_key': sys.argv[3],
  'service_role_key': sys.argv[4] or None,
}))" "$PROJECT_ID" "$SUPA_URL" "$SUPA_ANON" "$SUPA_SRK")

RESP=$(curl -sS -X POST "$API_URL/api/supabase/connect" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "$PAYLOAD")

echo "$RESP"
```

Expected success: `{"project_id":"...","supabase_url":"...","connected_at":"..."}`.

On `401` → forge-server auth expired; tell the user to refresh the Forge UI tab.
On `404 Project not found` → the workspace isn't registered with forge-server; stop and tell the user.
On `422` → JSON shape is wrong. Re-check that all three fields are strings.
On other 5xx → relay the error message; don't retry blindly.

### Step 5. Verify with /status

```bash
curl -sS "$API_URL/api/supabase/status?project_id=$PROJECT_ID" \
  -H "Authorization: Bearer $TOKEN"
```

Must return `{"connected": true, ...}`. If not, something went wrong in
Step 4 — surface to the user, don't proceed to scaffolding.

### Step 6. Install the client

Pick the install command based on detected stack:

```bash
# Node-based stacks
npm install @supabase/supabase-js

# Python (FastAPI)
pip install supabase
```

### Step 7. Scaffold the Supabase client

Pick ONE of the templates below based on the stack from Step 1.

#### Next.js (App Router) — `lib/supabase.ts`

```ts
import { createClient } from '@supabase/supabase-js'

export const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
)
```

For server-side / route handlers — `lib/supabase-server.ts`:

```ts
import { createClient } from '@supabase/supabase-js'

export const supabaseServer = () => createClient(
  process.env.SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!,  // server-only — never import in a client component
  { auth: { persistSession: false } },
)
```

#### Vite + React — `src/lib/supabase.ts`

```ts
import { createClient } from '@supabase/supabase-js'

export const supabase = createClient(
  import.meta.env.VITE_SUPABASE_URL,
  import.meta.env.VITE_SUPABASE_ANON_KEY,
)
```

#### FastAPI — `backend/db/supabase.py`

```python
import os
from supabase import create_client, Client

supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_ROLE_KEY"],
)
```

### Step 8. If the user's request involved login/register — wire it up

If the trigger was "make a login page", "add registration", "add auth", or
similar, you ALSO need an auth helper module.

#### Node stacks — `src/lib/auth.ts` (or `lib/auth.ts` for Next.js)

```ts
import { supabase } from './supabase'

export async function signUp(email: string, password: string) {
  const { data, error } = await supabase.auth.signUp({ email, password })
  if (error) throw error
  return data
}

export async function signIn(email: string, password: string) {
  const { data, error } = await supabase.auth.signInWithPassword({ email, password })
  if (error) throw error
  return data
}

export async function signInWithGoogle() {
  const { data, error } = await supabase.auth.signInWithOAuth({ provider: 'google' })
  if (error) throw error
  return data
}

export async function signOut() {
  const { error } = await supabase.auth.signOut()
  if (error) throw error
}

export async function getUser() {
  const { data: { user } } = await supabase.auth.getUser()
  return user
}
```

Then wire the existing login form's submit handler to `signIn()` and the
signup form to `signUp()`. The login UI you already built should keep its
styling — only the `onSubmit` body changes.

#### Don't build your own users table

Supabase has `auth.users` built-in. Email + password works out of the box.
If the user wants extra fields (display name, avatar, etc.), create a
`public.profiles` table keyed by `id uuid references auth.users(id)`, not a
parallel users table. Use a trigger to populate it on signup. Ask the user
what fields they want before creating it.

### Step 9. Sanity-check the wiring

After scaffolding, suggest the user test with:

- Open the login page → click "Create account" → enter an email + password
- Then go to their **Supabase dashboard → Authentication → Users** — the
  new user should appear.

Don't claim "done" before this test path is mentioned.

### Step 10. Confirm — be specific

Don't say "Supabase is connected". Say what's actually wired:

> Connected to `<url>`.
> Installed `@supabase/supabase-js`.
> Created `src/lib/supabase.ts` (client).
> Created `src/lib/auth.ts` (signUp / signIn / signOut helpers).
> Wired the login form's submit handler to `signIn()`.
> Reminder: before going live, enable Row Level Security on any tables
> you create. Auth tables already have it.
> Next: try creating an account on `/login`. New users will appear in your
> Supabase dashboard under Authentication → Users.

---

## Anti-patterns — don't do these

- **Don't skip Step 3a (OAuth) in favor of the paste flow because it
  "feels faster".** OAuth IS faster — one click vs. three sequential
  prompts vs. the user opening their Supabase dashboard and finding three
  keys — and it doesn't put a service_role JWT into the persisted chat
  transcript. The paste flow is the fallback, not the default.
- **Don't paste all three credentials prompts at once.** Ask sequentially.
- **Don't write a custom users table.** Use `auth.users` + `public.profiles`.
- **Don't hard-code keys in source code.** They live in `.env`, injected by
  the platform.
- **Don't `import` the service_role key in a client component / file.** It
  bypasses RLS and would be shipped to the browser. Server-only.
- **Don't skip the `/status` verify.** A 200 from `/connect` isn't proof the
  env injection landed if the workspace path is wrong.
- **Don't call `/api/supabase/query` to "test the connection".** That
  endpoint requires the `exec_sql` RPC which isn't installed by default —
  use `/status` and `/tables` instead.
