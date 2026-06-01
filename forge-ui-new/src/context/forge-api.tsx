/**
 * forge-api.tsx
 * =============
 * Context for all forge-server API interactions.
 *
 * Handles:
 *  - Auto-registration / JWT auth (persisted in localStorage)
 *  - Container lifecycle: ensure, stop, ping, status, SSE stream
 *  - Token refresh on 401
 *
 * Usage:
 *   const forge = useForgeApi()
 *   await forge.ensure(projectId, workspacePath, name)
 *   forge.subscribeStatus(projectId, (event) => { ... })
 */

import {
  createContext,
  createSignal,
  onCleanup,
  useContext,
  type Accessor,
  type JSX,
} from "solid-js"

// ── Config ────────────────────────────────────────────────────────────────────

const FORGE_API_URL: string =
  (import.meta.env.VITE_API_URL as string | undefined) ?? "http://api.forge.localhost"

const TOKEN_KEY   = "forge_jwt"
const DEVICE_KEY  = "forge_device_id"

// ── Types ─────────────────────────────────────────────────────────────────────

export type ContainerStatus =
  | "not_found"
  | "starting"
  | "creating"
  | "installing"
  | "running"
  | "sleeping"
  | "stopped"
  | "crashed"

export type StatusEvent = {
  status: ContainerStatus
  error?:  string
}

export type DevStatus = {
  project_id:     string
  status:         ContainerStatus
  container_name: string | null
  preview_url:    string | null
  last_ping_at:   string | null
}

export type EnsureResult = {
  status:      ContainerStatus
  preview_url: string | null
  message?:    string
}

// ── Device ID ─────────────────────────────────────────────────────────────────

function getDeviceId(): string {
  let id = localStorage.getItem(DEVICE_KEY)
  if (!id) {
    id = crypto.randomUUID()
    localStorage.setItem(DEVICE_KEY, id)
  }
  return id
}

// ── HTTP helpers ──────────────────────────────────────────────────────────────

async function apiFetch(
  path:    string,
  options: RequestInit & { token?: string } = {},
): Promise<Response> {
  const { token, ...rest } = options
  const headers: HeadersInit = {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(rest.headers ?? {}),
  }
  return fetch(`${FORGE_API_URL}${path}`, { ...rest, headers })
}

// ── Auth helpers ──────────────────────────────────────────────────────────────

/**
 * Derive stable credentials from the device ID so we auto-register/login
 * without requiring a real sign-up form in the UI.
 */
function deviceCredentials() {
  const id  = getDeviceId()
  return {
    email:    `device-${id}@forge-app.com`,
    username: `device-${id.slice(0, 8)}`,
    password: `forge-${id}`,
  }
}

async function register(): Promise<string | null> {
  const creds = deviceCredentials()
  const res   = await apiFetch("/api/auth/register", {
    method: "POST",
    body:   JSON.stringify(creds),
  })
  if (!res.ok) return null
  const data = await res.json()
  return (data as { access_token: string }).access_token
}

async function login(): Promise<string | null> {
  const creds = deviceCredentials()
  const res   = await apiFetch("/api/auth/login", {
    method: "POST",
    body:   JSON.stringify({ email: creds.email, password: creds.password }),
  })
  if (!res.ok) return null
  const data = await res.json()
  return (data as { access_token: string }).access_token
}

async function getToken(): Promise<string> {
  // Return cached token if present
  const cached = localStorage.getItem(TOKEN_KEY)
  if (cached) return cached

  // Try login first (device may already be registered)
  let token = await login()
  if (!token) {
    // Register then login
    await register()
    token = await login()
  }
  if (!token) throw new Error("forge-server: unable to authenticate")
  localStorage.setItem(TOKEN_KEY, token)
  return token
}

export async function authedFetch(
  path:    string,
  options: RequestInit = {},
  retried  = false,
): Promise<Response> {
  const token = await getToken()
  const res   = await apiFetch(path, { ...options, token })

  // 401 → clear cached token and retry once
  if (res.status === 401 && !retried) {
    localStorage.removeItem(TOKEN_KEY)
    return authedFetch(path, options, true)
  }
  return res
}

// ── Public auth helpers (used by the auth/onboarding pages, which sit
//    outside AppShellProviders and so can't use the context above) ───────────

/**
 * Auth state returned by register / login / verifyEmail / completeOnboarding.
 * The FE uses `email_verified` + `onboarding_completed` to decide which screen
 * to land on next. Sensitive fields (passwords, API keys) are never returned.
 */
export type AuthStatus = {
  token:                string
  user_id:              string
  username:             string
  email_verified:       boolean
  onboarding_completed: boolean
}

export type CurrentUser = {
  id:                   string
  email:                string
  username:             string
  created_at:           string
  email_verified:       boolean
  onboarding_completed: boolean
  full_name:            string | null
  role:                 string | null
  company_size:         string | null
  theme_pref:           string | null
}

type TokenOut = {
  access_token:         string
  token_type:           string
  user_id:              string
  username:             string
  email_verified:       boolean
  onboarding_completed: boolean
}

function persistAuth(data: TokenOut): AuthStatus {
  localStorage.setItem(TOKEN_KEY, data.access_token)
  return {
    token:                data.access_token,
    user_id:              data.user_id,
    username:             data.username,
    email_verified:       data.email_verified,
    onboarding_completed: data.onboarding_completed,
  }
}

export async function loginWithEmail(
  email: string,
  password: string,
): Promise<AuthStatus | { error: string }> {
  try {
    const res = await apiFetch("/api/auth/login", {
      method: "POST",
      body:   JSON.stringify({ email, password }),
    })
    if (!res.ok) {
      const text = await res.text().catch(() => "")
      const msg  = parseApiError(text) || "Invalid email or password"
      return { error: msg }
    }
    return persistAuth(await res.json() as TokenOut)
  } catch {
    return { error: "Could not reach the server" }
  }
}

export async function registerWithEmail(
  email: string,
  password: string,
  username?: string,
): Promise<AuthStatus | { error: string }> {
  try {
    const res = await apiFetch("/api/auth/register", {
      method: "POST",
      body:   JSON.stringify({
        email,
        password,
        username: username ?? email.split("@")[0],
      }),
    })
    if (!res.ok) {
      const text = await res.text().catch(() => "")
      const msg  = parseApiError(text) || "Registration failed"
      return { error: msg }
    }
    return persistAuth(await res.json() as TokenOut)
  } catch {
    return { error: "Could not reach the server" }
  }
}

/** Extract a human-readable message from a FastAPI JSON error body. */
function parseApiError(text: string): string {
  if (!text) return ""
  try {
    const json = JSON.parse(text) as { detail?: unknown }
    if (typeof json.detail === "string") return json.detail
    if (Array.isArray(json.detail)) {
      // Pydantic validation errors: [{msg: "..."}]
      const first = json.detail[0] as { msg?: string }
      if (first?.msg) return first.msg
    }
  } catch { /* not JSON — fall through */ }
  return text
}

/**
 * Fetch the currently-authenticated user's full profile.
 * Returns null on 401 / network failure — callers should treat that as
 * "log the user out and send to /auth".
 */
export async function fetchCurrentUser(): Promise<CurrentUser | null> {
  const token = localStorage.getItem(TOKEN_KEY)
  if (!token) return null
  try {
    const res = await apiFetch("/api/auth/me", { token })
    if (!res.ok) return null
    return await res.json() as CurrentUser
  } catch {
    return null
  }
}

/**
 * Update the current user's profile. Only `full_name` and `theme_pref` are
 * patchable today — the backend rejects other fields. Returns the fresh
 * profile on success, an error message otherwise.
 */
export async function updateCurrentUser(
  patch: { full_name?: string; theme_pref?: "light" | "dark" },
): Promise<CurrentUser | { error: string }> {
  try {
    const res = await authedFetch("/api/auth/me", {
      method: "PATCH",
      body:   JSON.stringify(patch),
    })
    if (!res.ok) {
      const text = await res.text().catch(() => "")
      return { error: parseApiError(text) || `update failed (${res.status})` }
    }
    return await res.json() as CurrentUser
  } catch {
    return { error: "Could not reach the server" }
  }
}

/**
 * Flip the calling user's email_verified flag to true.
 * Returns the refreshed profile so the caller can route based on the new state.
 * (No token / no email send yet — that gets wired in later without changing
 * this contract.)
 */
export async function verifyEmail(): Promise<CurrentUser | { error: string }> {
  const token = localStorage.getItem(TOKEN_KEY)
  if (!token) return { error: "Not signed in" }
  try {
    const res = await apiFetch("/api/auth/verify-email", { method: "POST", token })
    if (!res.ok) {
      const text = await res.text().catch(() => "")
      return { error: text || "Could not verify email" }
    }
    return await res.json() as CurrentUser
  } catch {
    return { error: "Could not reach the server" }
  }
}

/**
 * Submit the onboarding answers and flip onboarding_completed=true.
 * Returns the refreshed profile.
 */
export async function completeOnboarding(input: {
  full_name:    string
  role:         string
  company_size: string
  theme_pref?:  "light" | "dark"
}): Promise<CurrentUser | { error: string }> {
  const token = localStorage.getItem(TOKEN_KEY)
  if (!token) return { error: "Not signed in" }
  try {
    const res = await apiFetch("/api/auth/onboarding", {
      method: "POST",
      body:   JSON.stringify(input),
      token,
    })
    if (!res.ok) {
      const text = await res.text().catch(() => "")
      return { error: text || "Could not save onboarding" }
    }
    return await res.json() as CurrentUser
  } catch {
    return { error: "Could not reach the server" }
  }
}

/**
 * Resolve the right post-auth destination from a user profile.
 * Used by both the AuthGuard and the auth.tsx form's success handler.
 */
export function postAuthDestination(u: {
  email_verified:       boolean
  onboarding_completed: boolean
}): string {
  if (!u.email_verified)       return "/auth/verify-email"
  if (!u.onboarding_completed) return "/onboarding/style"
  return "/home"
}

/**
 * Avatar initials from the user's profile.
 *
 *   "Jane Doe"  → "JD"
 *   "Jane"      → "J"
 *   ""          → username's first letter, else "U"
 *
 * Used by the home page's profile block + anywhere else we surface an avatar.
 */
export function userInitials(fullName?: string | null, fallback?: string | null): string {
  const name = (fullName ?? "").trim()
  if (name) {
    const parts = name.split(/\s+/).filter(Boolean)
    if (parts.length >= 2) {
      return (parts[0]![0]! + parts[parts.length - 1]![0]!).toUpperCase()
    }
    return parts[0]![0]!.toUpperCase()
  }
  const fb = (fallback ?? "").trim()
  return (fb[0] ?? "U").toUpperCase()
}

export function isAuthenticated(): boolean {
  return !!localStorage.getItem(TOKEN_KEY)
}

export function logout(): void {
  localStorage.removeItem(TOKEN_KEY)
}

/**
 * Decode the JWT payload (no signature verification — display only).
 * Returns { email, username, sub } or null if no token / malformed.
 */
export function currentUserInfo(): { email: string; username: string; sub: string } | null {
  const token = localStorage.getItem(TOKEN_KEY)
  if (!token) return null
  try {
    const parts = token.split(".")
    if (parts.length < 2) return null
    const payload = JSON.parse(atob(parts[1].replace(/-/g, "+").replace(/_/g, "/")))
    return {
      sub:      String(payload.sub ?? ""),
      email:    String(payload.email ?? ""),
      username: String(payload.username ?? payload.email?.split("@")[0] ?? ""),
    }
  } catch {
    return null
  }
}

// ── Context ───────────────────────────────────────────────────────────────────

export type ShowcaseProject = {
  id:                   string
  name:                 string
  description:          string
  workspace_path:       string
  stack:                string | null
  container_status:     string
  preview_url:          string | null
  showcased_at:         string | null
  showcase_name:        string | null
  showcase_description: string | null
  thumbnail_url:        string | null
  starred_at?:          string | null
  forked_from_project_id?: string | null
}

// ── Settings & Provider Key types ────────────────────────────────────────────

export type UserSettings = {
  /** Model ID used by the main coding / chat agent (composer default). */
  primary_model: string
  /** Model ID used by design-analyst and design-critic subagents. */
  design_model:  string
}

export type ProviderKeyOut = {
  id:          string
  provider_id: string
  label:       string | null
  created_at:  string
  updated_at:  string
}

// ── Context type ──────────────────────────────────────────────────────────────

export type ForgeApiContext = {
  /** Base URL of the forge-server (VITE_API_URL) */
  baseUrl: string

  /** Start / wake the dev container for a project. */
  ensure(projectId: string, workspacePath?: string, name?: string): Promise<EnsureResult>

  /** Keep-alive heartbeat. Call every 2 minutes when preview is visible. */
  ping(projectId: string): Promise<void>

  /** Stop (sleep) a running container. */
  stop(projectId: string): Promise<void>

  /** Get current container status (single fetch). */
  getStatus(projectId: string): Promise<DevStatus>

  /**
   * Subscribe to SSE status events for a project.
   * Returns an unsubscribe function.
   */
  subscribeStatus(
    projectId: string,
    onEvent:   (e: StatusEvent) => void,
    onError?:  (err: Error) => void,
  ): () => void

  /**
   * Save project to the showcase gallery with a custom name + description.
   * Triggers a server-side screenshot in the background.
   */
  showcase(projectId: string, opts?: { name?: string; description?: string }): Promise<void>

  /** Remove project from the showcase gallery. */
  unshowcase(projectId: string): Promise<void>

  /** Fetch all showcased projects for the current user. */
  listShowcases(): Promise<ShowcaseProject[]>

  /**
   * Upload a base64 screenshot as the thumbnail for a project.
   * imageData format: "data:image/jpeg;base64,..."
   */
  uploadThumbnail(projectId: string, imageData: string): Promise<void>

  /**
   * Ask the server to take a headless-browser screenshot of the running container
   * and save it as the project thumbnail.  Requires Playwright on the server.
   * Returns { thumbnail_url } on success.
   */
  screenshot(projectId: string): Promise<{ thumbnail_url: string }>

  /**
   * Run the post-completion verifier on a project — ensures the container is
   * up, probes the dev server, and tails docker logs for known error
   * signatures. Used by the preview "Fix this" button to give the agent a
   * structured snapshot of what's broken in one round-trip.
   *
   * The endpoint is server-cached around docker logs, so calling repeatedly
   * during a debug loop is cheap. Returns the full structured report.
   */
  verify(projectId: string): Promise<VerifyReport>

  /**
   * Clone a showcase/template project into a fresh project for the current user.
   * Returns the new project record (with workspace_path for navigation).
   */
  cloneProject(projectId: string, opts?: { name?: string; description?: string }): Promise<ShowcaseProject>

  /**
   * Download the project workspace as a ZIP file (triggers browser download).
   * Excludes node_modules, .git, dist, and other build artefacts.
   */
  downloadProject(projectId: string, projectName?: string): Promise<void>

  /**
   * Permanently delete a project — stops the container, removes the workspace
   * directory on disk, and deletes the DB record.
   */
  deleteProject(projectId: string): Promise<void>

  /**
   * Star / unstar a project for the current user. Returns the updated record
   * so the caller can splice it back into the cached list without a full
   * refetch.
   */
  toggleProjectStar(projectId: string, starred: boolean): Promise<ShowcaseProject>

  /**
   * Public gallery: all users' showcased projects — used for the Resources page.
   * No auth required on the server side.
   */
  listAllShowcases(): Promise<ShowcaseProject[]>

  // ── User settings ──────────────────────────────────────────────────────────

  /** Fetch the current user's settings (design_model, etc.). */
  getSettings(): Promise<UserSettings>

  /** Update one or more settings fields. */
  updateSettings(patch: Partial<UserSettings>): Promise<UserSettings>

  // ── Provider API keys ──────────────────────────────────────────────────────

  /** List which providers the user has keys stored for (key values are never returned). */
  listProviderKeys(): Promise<ProviderKeyOut[]>

  /**
   * Add or update the API key for a provider.
   * The key is encrypted at rest; opencode's auth.json is updated immediately.
   */
  setProviderKey(providerId: string, apiKey: string, label?: string): Promise<ProviderKeyOut>

  /** Remove a stored provider key. */
  deleteProviderKey(providerId: string): Promise<void>

  // ── DB (Data tab) ─────────────────────────────────────────────────────────

  /** List tables + columns + row counts for a project's database. */
  dbListTables(projectId: string): Promise<DbTablesResponse>

  /** Get paginated rows from a table. */
  dbGetRows(
    projectId: string,
    table:     string,
    opts?: { limit?: number; offset?: number; orderBy?: string; orderDir?: "asc" | "desc" },
  ): Promise<DbRowsResponse>

  /** Insert a row into a table. */
  dbInsertRow(projectId: string, table: string, values: Record<string, unknown>): Promise<{ inserted_id: number }>

  /** Update a row by primary key. */
  dbUpdateRow(projectId: string, table: string, pk: string | number, values: Record<string, unknown>): Promise<{ rows_affected: number }>

  /** Delete a row by primary key. */
  dbDeleteRow(projectId: string, table: string, pk: string | number): Promise<{ rows_affected: number }>

  /** Execute raw SQL. write=true to allow mutations. */
  dbRunSql(projectId: string, sql: string, write?: boolean): Promise<DbSqlResponse>

  /** Kick off SQLite → Supabase migration. Returns a job to poll. */
  dbStartMigration(projectId: string, postgresUrl?: string): Promise<DbMigrationJob>

  /** Poll migration status. */
  dbMigrationStatus(projectId: string, jobId: string): Promise<DbMigrationJob>
}

export type DbColumn = {
  name:        string
  type:        string
  nullable:    boolean
  primary_key: boolean
  default:     string | null
}
export type DbTable = {
  name:      string
  columns:   DbColumn[]
  row_count: number
}
export type DbTablesResponse = { driver: string; tables: DbTable[] }
export type DbRowsResponse = {
  columns: string[]
  rows:    Array<Record<string, unknown>>
  total:   number
  limit:   number
  offset:  number
}
export type DbSqlResponse = {
  columns:        string[]
  rows:           unknown[][]
  rows_affected:  number
}
export type DbMigrationJob = {
  job_id:      string
  status:      "queued" | "running" | "succeeded" | "failed"
  progress:    number
  message:     string
  started_at:  number | null
  finished_at: number | null
}

// ── Verify report ────────────────────────────────────────────────────────────
// Shape mirrors VerifyReport in forge-server/forge_server/api/verify_routes.py.
// If the server type changes, this MUST be updated in lockstep.

export type VerifyLogError = {
  signature: string
  detail:    string
  line:      string
}

export type VerifyEndpointProbe = {
  path:         string
  status:       number
  body_snippet: string
  error?:       string | null
}

export type VerifyReport = {
  container_status: string
  preview_url:      string
  health_ok:        boolean
  endpoint_probes:  VerifyEndpointProbe[]
  log_errors:       VerifyLogError[]
  fatal:            boolean
  summary:          string
}

const ForgeApiCtx = createContext<ForgeApiContext>()

export function useForgeApi(): ForgeApiContext {
  const ctx = useContext(ForgeApiCtx)
  if (!ctx) throw new Error("useForgeApi: must be inside ForgeApiProvider")
  return ctx
}

/**
 * Non-throwing variant for code paths that may run outside Forge (e.g. the
 * shared LocalProvider used by both Forge and stock opencode). Returns
 * undefined when no ForgeApiProvider is mounted.
 */
export function useForgeApiOptional(): ForgeApiContext | undefined {
  return useContext(ForgeApiCtx)
}

export function ForgeApiProvider(props: { children: JSX.Element }) {
  const api: ForgeApiContext = {
    baseUrl: FORGE_API_URL,

    async ensure(projectId, workspacePath, name) {
      const res = await authedFetch("/api/dev/ensure", {
        method: "POST",
        body:   JSON.stringify({
          project_id:     projectId,
          workspace_path: workspacePath ?? null,
          name:           name ?? null,
        }),
      })
      if (!res.ok) {
        const text = await res.text()
        throw new Error(`ensure failed (${res.status}): ${text}`)
      }
      return res.json() as Promise<EnsureResult>
    },

    async ping(projectId) {
      await authedFetch("/api/dev/ping", {
        method: "POST",
        body:   JSON.stringify({ project_id: projectId }),
      }).catch((e) => console.warn("forge ping failed", e))
    },

    async stop(projectId) {
      await authedFetch("/api/dev/stop", {
        method: "POST",
        body:   JSON.stringify({ project_id: projectId }),
      })
    },

    async getStatus(projectId) {
      const res = await authedFetch(`/api/dev/status?project_id=${projectId}`)
      if (!res.ok) throw new Error(`status fetch failed (${res.status})`)
      return res.json() as Promise<DevStatus>
    },

    async showcase(projectId, opts) {
      await authedFetch(`/api/projects/${projectId}/showcase`, {
        method: "POST",
        body:   JSON.stringify({
          showcase_name:        opts?.name        ?? null,
          showcase_description: opts?.description ?? null,
        }),
      }).catch((e) => console.warn("forge showcase failed", e))
    },

    async unshowcase(projectId) {
      await authedFetch(`/api/projects/${projectId}/showcase`, { method: "DELETE" })
        .catch((e) => console.warn("forge unshowcase failed", e))
    },

    async listShowcases() {
      const res = await authedFetch("/api/projects/showcase")
      if (!res.ok) return []
      return res.json() as Promise<ShowcaseProject[]>
    },

    async uploadThumbnail(projectId, imageData) {
      await authedFetch(`/api/projects/${projectId}/thumbnail`, {
        method: "POST",
        body:   JSON.stringify({ image_data: imageData }),
      }).catch((e) => console.warn("forge uploadThumbnail failed", e))
    },

    async screenshot(projectId) {
      const res = await authedFetch(`/api/projects/${projectId}/screenshot`)
      if (!res.ok) throw new Error(`Screenshot failed: ${res.status}`)
      return res.json() as Promise<{ thumbnail_url: string }>
    },

    async verify(projectId) {
      const res = await authedFetch(`/api/projects/${projectId}/verify`, {
        method: "POST",
      })
      if (!res.ok) {
        const text = await res.text().catch(() => "")
        throw new Error(`verify failed (${res.status}): ${text}`)
      }
      return res.json() as Promise<VerifyReport>
    },

    async downloadProject(projectId, projectName) {
      const res = await authedFetch(`/api/projects/${projectId}/download`)
      if (!res.ok) throw new Error(`Download failed: ${res.status}`)
      const blob = await res.blob()
      const url  = URL.createObjectURL(blob)
      const a    = document.createElement("a")
      a.href     = url
      a.download = `${projectName || "project"}.zip`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    },

    async deleteProject(projectId) {
      const res = await authedFetch(`/api/projects/${projectId}`, { method: "DELETE" })
      if (!res.ok && res.status !== 204) {
        const text = await res.text().catch(() => "")
        throw new Error(`deleteProject failed (${res.status}): ${text}`)
      }
    },

    async toggleProjectStar(projectId, starred) {
      const res = await authedFetch(`/api/projects/${projectId}/star`, {
        method: "PATCH",
        body:   JSON.stringify({ starred }),
      })
      if (!res.ok) {
        const text = await res.text().catch(() => "")
        throw new Error(`toggleProjectStar failed (${res.status}): ${text}`)
      }
      return res.json() as Promise<ShowcaseProject>
    },

    async listAllShowcases() {
      // Public endpoint — no auth needed, but we send token if available (best-effort)
      try {
        const token = localStorage.getItem(TOKEN_KEY)
        const res = await apiFetch("/api/projects/gallery", token ? { token } : {})
        if (!res.ok) return []
        return res.json() as Promise<ShowcaseProject[]>
      } catch {
        return []
      }
    },

    async cloneProject(projectId, opts) {
      const res = await authedFetch(`/api/projects/${projectId}/clone`, {
        method: "POST",
        body:   JSON.stringify({
          name:        opts?.name        ?? null,
          description: opts?.description ?? null,
        }),
      })
      if (!res.ok) {
        const text = await res.text()
        throw new Error(`cloneProject failed (${res.status}): ${text}`)
      }
      return res.json() as Promise<ShowcaseProject>
    },

    // ── User settings ──────────────────────────────────────────────────────

    async getSettings() {
      const res = await authedFetch("/api/user/settings")
      if (!res.ok) {
        const text = await res.text().catch(() => "")
        throw new Error(`getSettings failed (${res.status}): ${text}`)
      }
      return res.json() as Promise<UserSettings>
    },

    async updateSettings(patch) {
      const res = await authedFetch("/api/user/settings", {
        method: "PATCH",
        body:   JSON.stringify(patch),
      })
      if (!res.ok) {
        const text = await res.text().catch(() => "")
        throw new Error(`updateSettings failed (${res.status}): ${text}`)
      }
      return res.json() as Promise<UserSettings>
    },

    // ── Provider API keys ──────────────────────────────────────────────────

    async listProviderKeys() {
      const res = await authedFetch("/api/user/providers")
      if (!res.ok) {
        const text = await res.text().catch(() => "")
        throw new Error(`listProviderKeys failed (${res.status}): ${text}`)
      }
      return res.json() as Promise<ProviderKeyOut[]>
    },

    async setProviderKey(providerId, apiKey, label) {
      const res = await authedFetch("/api/user/providers", {
        method: "POST",
        body:   JSON.stringify({ provider_id: providerId, api_key: apiKey, label: label ?? null }),
      })
      if (!res.ok) {
        const text = await res.text().catch(() => "")
        throw new Error(`setProviderKey failed (${res.status}): ${text}`)
      }
      return res.json() as Promise<ProviderKeyOut>
    },

    async deleteProviderKey(providerId) {
      const res = await authedFetch(`/api/user/providers/${encodeURIComponent(providerId)}`, {
        method: "DELETE",
      })
      if (!res.ok && res.status !== 204) {
        const text = await res.text().catch(() => "")
        throw new Error(`deleteProviderKey failed (${res.status}): ${text}`)
      }
    },

    // ── DB (Data tab) ────────────────────────────────────────────────────────

    async dbListTables(projectId) {
      const res = await authedFetch(`/api/projects/${projectId}/db/tables`)
      if (!res.ok) throw new Error(`dbListTables failed (${res.status})`)
      return res.json() as Promise<DbTablesResponse>
    },

    async dbGetRows(projectId, table, opts) {
      const qs = new URLSearchParams()
      qs.set("limit",  String(opts?.limit  ?? 50))
      qs.set("offset", String(opts?.offset ?? 0))
      if (opts?.orderBy)  qs.set("order_by",  opts.orderBy)
      if (opts?.orderDir) qs.set("order_dir", opts.orderDir)
      const res = await authedFetch(
        `/api/projects/${projectId}/db/tables/${encodeURIComponent(table)}?${qs}`,
      )
      if (!res.ok) throw new Error(`dbGetRows failed (${res.status})`)
      return res.json() as Promise<DbRowsResponse>
    },

    async dbInsertRow(projectId, table, values) {
      const res = await authedFetch(
        `/api/projects/${projectId}/db/tables/${encodeURIComponent(table)}/rows`,
        { method: "POST", body: JSON.stringify({ values }) },
      )
      if (!res.ok) {
        const text = await res.text().catch(() => "")
        throw new Error(`insert failed (${res.status}): ${text}`)
      }
      return res.json()
    },

    async dbUpdateRow(projectId, table, pk, values) {
      const res = await authedFetch(
        `/api/projects/${projectId}/db/tables/${encodeURIComponent(table)}/rows/${encodeURIComponent(String(pk))}`,
        { method: "PATCH", body: JSON.stringify({ values }) },
      )
      if (!res.ok) {
        const text = await res.text().catch(() => "")
        throw new Error(`update failed (${res.status}): ${text}`)
      }
      return res.json()
    },

    async dbDeleteRow(projectId, table, pk) {
      const res = await authedFetch(
        `/api/projects/${projectId}/db/tables/${encodeURIComponent(table)}/rows/${encodeURIComponent(String(pk))}`,
        { method: "DELETE" },
      )
      if (!res.ok) {
        const text = await res.text().catch(() => "")
        throw new Error(`delete failed (${res.status}): ${text}`)
      }
      return res.json()
    },

    async dbRunSql(projectId, sql, write = false) {
      const res = await authedFetch(
        `/api/projects/${projectId}/db/sql${write ? "?write=1" : ""}`,
        { method: "POST", body: JSON.stringify({ sql }) },
      )
      if (!res.ok) {
        const text = await res.text().catch(() => "")
        throw new Error(text || `sql failed (${res.status})`)
      }
      return res.json() as Promise<DbSqlResponse>
    },

    async dbStartMigration(projectId, postgresUrl) {
      const res = await authedFetch(
        `/api/projects/${projectId}/db/migrate-to-supabase`,
        { method: "POST", body: JSON.stringify({ postgres_url: postgresUrl ?? null }) },
      )
      if (!res.ok) {
        const text = await res.text().catch(() => "")
        throw new Error(text || `migration failed (${res.status})`)
      }
      return res.json() as Promise<DbMigrationJob>
    },

    async dbMigrationStatus(projectId, jobId) {
      const res = await authedFetch(
        `/api/projects/${projectId}/db/migrate-to-supabase/${jobId}`,
      )
      if (!res.ok) throw new Error(`migration status failed (${res.status})`)
      return res.json() as Promise<DbMigrationJob>
    },

    subscribeStatus(projectId, onEvent, onError) {
      let es: EventSource | null = null
      let closed = false

      const connect = async () => {
        if (closed) return
        try {
          const token = await getToken()
          const url   = `${FORGE_API_URL}/api/dev/stream?project_id=${encodeURIComponent(projectId)}&token=${encodeURIComponent(token)}`
          es = new EventSource(url)

          es.onmessage = (e) => {
            try {
              onEvent(JSON.parse(e.data) as StatusEvent)
            } catch {
              // ignore malformed frames
            }
          }
          es.onerror = () => {
            es?.close()
            if (!closed) {
              // Reconnect after 2s
              setTimeout(connect, 2000)
            }
          }
        } catch (err) {
          onError?.(err instanceof Error ? err : new Error(String(err)))
        }
      }

      void connect()

      return () => {
        closed = true
        es?.close()
      }
    },
  }

  return (
    <ForgeApiCtx.Provider value={api}>
      {props.children}
    </ForgeApiCtx.Provider>
  )
}
