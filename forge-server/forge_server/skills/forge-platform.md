# Forge Platform Skill

## When to use

Trigger this skill when the user asks to:

- Add an API key or secret (OpenAI, Stripe, Resend, etc.)
- Set an environment variable
- View what env vars are configured
- Update project settings (stack, name, description)
- Check project configuration

## How to identify the current project

The current opencode session is pinned to a workspace under
`/forge-data/users/<user>/projects/<project_id>/workspace`. Derive the project
id from the current working directory:

```bash
PROJECT_ID=$(pwd | sed -n 's|.*/projects/\([^/]*\)/workspace.*|\1|p')
API_URL="${FORGE_API_URL:-http://forge-server:8000}"
TOKEN="${FORGE_API_TOKEN:-}"
```

If `PROJECT_ID` comes back empty, you're not running inside a Forge workspace
— stop and tell the user.

## Reading current project state

There is no `forge.json` in the workspace anymore. Project state lives in
the Forge database and is read via the API:

```bash
curl -sS "${API_URL}/api/projects/${PROJECT_ID}/config" \
  -H "Authorization: Bearer ${TOKEN}"
```

Returns the same shape `forge.json` used to have: `stack`, `services`,
`supabase`, `env_vars` (keys + labels, never values), `created_at`,
`updated_at`.

## Setting an environment variable / API key

### Step 1: Ask for the value

Ask the user to paste the key value. Remind them it will be stored encrypted
and never appear in code.

### Step 2: Store via forge-server API

```bash
curl -sS -X POST "${API_URL}/api/projects/${PROJECT_ID}/env" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TOKEN}" \
  -d '{
    "key": "OPENAI_API_KEY",
    "value": "sk-...",
    "label": "OpenAI production key",
    "inject_runtime": true
  }'
```

### Step 3: Use in code

Tell the user to access the variable via `process.env.KEY_NAME` (JS) or
`os.environ["KEY_NAME"]` (Python). Never import secrets directly — they are
injected into the dev container automatically at startup.

### Step 4: Confirm

```bash
# List current env vars (keys only, values are masked)
curl -sS "${API_URL}/api/projects/${PROJECT_ID}/env" \
  -H "Authorization: Bearer ${TOKEN}"
```

## Updating project config (stack, services)

When you detect or the user tells you their stack, push it to the project
config:

```bash
curl -sS -X PATCH "${API_URL}/api/projects/${PROJECT_ID}/config" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TOKEN}" \
  -d '{
    "stack": "next-fastapi",
    "services": {
      "frontend": {"framework": "next", "port": 3000},
      "backend": {"framework": "fastapi", "port": 8000}
    }
  }'
```

## Important rules

- NEVER store secret values in source files or `.env` committed to git.
- ALWAYS use the API to store secrets — they are encrypted in the database.
- The container receives secrets as injected environment variables at startup.
- Do NOT create a `forge.json` file in the workspace. Project state lives
  in the database; the API is the only source of truth.
