/**
 * DataPanel
 * ---------
 * Visual database explorer for a Forge project. Reads the project's local
 * SQLite (data.db) via forge-server's /api/projects/:id/db/* endpoints.
 *
 * Three sections:
 *   1. Left rail   — table list with row counts
 *   2. Main grid   — rows of the selected table (inline edit + insert + delete)
 *   3. Bottom bar  — SQL console + "Migrate to Supabase" button
 *
 * Self-contained: derives projectId from the SDK directory, fetches its own
 * state. Drop into any tab/page that needs it.
 */
import { For, Show, createEffect, createMemo, createSignal, onCleanup } from "solid-js"
import {
  useForgeApi,
  type DbTable,
  type DbRowsResponse,
  type DbMigrationJob,
  type DbInfoResponse,
} from "@/context/forge-api"
import { useSDK } from "@/context/sdk"

const PAGE_SIZE = 50

export function DataPanel() {
  const forge = useForgeApi()
  const sdk   = useSDK()

  // ── Project id derivation (matches MobilePreviewPanel) ────────────────────
  const projectId = createMemo((): string | null => {
    const dir = sdk.directory
    if (!dir) return null
    const m = dir.match(/\/projects\/([a-f0-9-]{8,}[a-f0-9])\/workspace/)
    return m?.[1] ?? null
  })

  // ── State ────────────────────────────────────────────────────────────────
  // dbInfo drives header text and the Supabase-vs-SQLite CTA swap. We fetch
  // it once per project on mount (and after a successful migration) — one
  // indexed lookup server-side, no polling.
  const [info,         setInfo]         = createSignal<DbInfoResponse | null>(null)
  const [tables,       setTables]       = createSignal<DbTable[]>([])
  const [activeTable,  setActiveTable]  = createSignal<string | null>(null)
  const [rows,         setRows]         = createSignal<DbRowsResponse | null>(null)
  const [offset,       setOffset]       = createSignal(0)
  const [loading,      setLoading]      = createSignal(false)
  const [error,        setError]        = createSignal<string | null>(null)
  const [editing,      setEditing]      = createSignal<{ pk: string; col: string } | null>(null)
  const [editValue,    setEditValue]    = createSignal("")
  const [showInsert,   setShowInsert]   = createSignal(false)
  const [insertValues, setInsertValues] = createSignal<Record<string, string>>({})
  const [sqlOpen,      setSqlOpen]      = createSignal(false)
  const [sqlText,      setSqlText]      = createSignal("")
  const [sqlResult,    setSqlResult]    = createSignal<{ columns: string[]; rows: unknown[][] } | null>(null)
  const [migrate,      setMigrate]      = createSignal<DbMigrationJob | null>(null)
  const [migrateUrl,   setMigrateUrl]   = createSignal("")
  const [showMigrate,  setShowMigrate]  = createSignal(false)

  // ── Loaders ──────────────────────────────────────────────────────────────
  async function refreshInfo() {
    const pid = projectId()
    if (!pid) return
    try {
      setInfo(await forge.dbInfo(pid))
    } catch (e) {
      // Non-fatal: keep showing whatever we had. Header falls back to "—".
      // The tables fetch below has its own error path.
      console.warn("dbInfo failed:", e)
    }
  }

  async function refreshTables() {
    const pid = projectId()
    if (!pid) return
    setLoading(true); setError(null)
    try {
      const data = await forge.dbListTables(pid)
      setTables(data.tables)
      if (!activeTable() && data.tables.length) setActiveTable(data.tables[0].name)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally { setLoading(false) }
  }

  async function refreshRows() {
    const pid = projectId()
    const t   = activeTable()
    if (!pid || !t) return
    setLoading(true); setError(null)
    try {
      const data = await forge.dbGetRows(pid, t, { limit: PAGE_SIZE, offset: offset() })
      setRows(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally { setLoading(false) }
  }

  createEffect(() => { void projectId(); refreshInfo(); refreshTables() })

  // Auto-poll /db/info every 3s while we're in the "no DB yet" state. As
  // soon as the AI calls /db/provision (or the user connects BYO Supabase),
  // the next poll picks up the new state and the empty-state CTA gives way
  // to the table list. Stops polling as soon as a connection appears, so we
  // pay the cost only during the first-time setup window — flat per project
  // per session, not per-turn. Per CLAUDE.md §2: bounded resource cost.
  let pollTimer: ReturnType<typeof setInterval> | undefined
  createEffect(() => {
    const pid = projectId()
    const connected = info()?.supabase.connected
    if (pollTimer) { clearInterval(pollTimer); pollTimer = undefined }
    if (pid && !connected) {
      pollTimer = setInterval(() => {
        // Don't pile on requests when the tab isn't visible. Saves the
        // user's tokens (no, but saves their CPU + battery) when they're
        // working in chat and not looking at Data.
        if (document.hidden) return
        refreshInfo()
        // Also refresh tables in case the AI created tables without going
        // through /db/provision (legacy / hand-rolled path).
        refreshTables()
      }, 3000)
    }
  })
  onCleanup(() => { if (pollTimer) clearInterval(pollTimer) })
  createEffect(() => { void activeTable(); setOffset(0); refreshRows() })
  createEffect(() => { void offset(); refreshRows() })

  // ── Mutations ────────────────────────────────────────────────────────────
  const activeTableMeta = createMemo(() => tables().find(t => t.name === activeTable()) ?? null)
  const pkColumn = createMemo(() => activeTableMeta()?.columns.find(c => c.primary_key)?.name ?? null)

  async function commitEdit() {
    const e = editing()
    const pid = projectId()
    const t   = activeTable()
    if (!e || !pid || !t) return
    try {
      await forge.dbUpdateRow(pid, t, e.pk, { [e.col]: coerce(editValue()) })
      setEditing(null)
      await refreshRows()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  async function doInsert() {
    const pid = projectId()
    const t   = activeTable()
    if (!pid || !t) return
    try {
      const coerced = Object.fromEntries(
        Object.entries(insertValues()).filter(([, v]) => v !== "").map(([k, v]) => [k, coerce(v)]),
      )
      await forge.dbInsertRow(pid, t, coerced)
      setShowInsert(false); setInsertValues({})
      await refreshTables(); await refreshRows()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  async function doDelete(pk: string) {
    if (!confirm("Delete this row?")) return
    const pid = projectId()
    const t   = activeTable()
    if (!pid || !t) return
    try {
      await forge.dbDeleteRow(pid, t, pk)
      await refreshTables(); await refreshRows()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  async function runSql() {
    const pid = projectId()
    if (!pid) return
    const trimmed = sqlText().trim()
    if (!trimmed) return
    const isWrite = /^(insert|update|delete|create|drop|alter|replace)\b/i.test(trimmed)
    try {
      const r = await forge.dbRunSql(pid, trimmed, isWrite)
      setSqlResult({ columns: r.columns, rows: r.rows })
      if (isWrite) { await refreshTables(); await refreshRows() }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  // ── Migration to Supabase ────────────────────────────────────────────────
  let migratePoll: ReturnType<typeof setInterval> | undefined
  onCleanup(() => { if (migratePoll) clearInterval(migratePoll) })

  async function startMigration() {
    const pid = projectId()
    if (!pid) return
    try {
      const job = await forge.dbStartMigration(pid, migrateUrl() || undefined)
      setMigrate(job)
      migratePoll = setInterval(async () => {
        const m = migrate()
        if (!m) return
        try {
          const next = await forge.dbMigrationStatus(pid, m.job_id)
          setMigrate(next)
          if (next.status === "succeeded" || next.status === "failed") {
            if (migratePoll) { clearInterval(migratePoll); migratePoll = undefined }
            if (next.status === "succeeded") {
              // /db/info now sees a SupabaseConnection row — refresh so the
              // header badge and "Open in Supabase" CTA swap in immediately
              // instead of waiting for the user to remount the panel.
              await Promise.all([refreshInfo(), refreshTables()])
            }
          }
        } catch { /* keep polling */ }
      }, 1000)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  function supabaseHost(url: string): string {
    // "https://xxxxx.supabase.co" → "xxxxx.supabase.co". URL() can throw on
    // malformed input, so fall back to the raw value rather than crash the
    // header render path.
    try { return new URL(url).host } catch { return url }
  }

  function driverLabel(i: DbInfoResponse | null): string {
    // Human-readable header label. We avoid surfacing the connection URL
    // (it contains a role password in the postgres-local case); the schema
    // name is enough to identify which project's data this is.
    if (!i) return "—"
    if (i.driver === "postgres-local" && i.supabase.schema_name) {
      return `postgres · ${i.supabase.schema_name}`
    }
    if (i.driver === "supabase") return "supabase"
    return `sqlite · ${i.path ?? "data.db"}`
  }

  // Connect-Database flow. We don't have a programmatic "submit to composer"
  // API yet (planned for v1.1) — for now, copy the prompt to clipboard with
  // a clear toast. The user pastes + sends in one motion; the AI runs the
  // db.md skill and calls /db/provision. Auto-poll below picks up the result.
  const CONNECT_DB_PROMPT =
    "Set up a database for this project. Use Forge's db.md skill — provision " +
    "a Postgres schema (call /db/provision via FORGE_API_URL), write the " +
    "DATABASE_URL to .env.local, then run forge-enable-db.sh to scaffold " +
    "Drizzle. Ask me what tables I need."

  async function connectDatabase() {
    try {
      await navigator.clipboard.writeText(CONNECT_DB_PROMPT)
      // Toast via the same path other DataPanel errors use — keeps the
      // component self-contained. If the user has a toast lib hooked up
      // globally we could prefer that, but inline alert keeps it portable.
      setError(null)
      // Use the success-tinted info channel via a transient banner. We
      // reuse the error banner but with neutral copy.
      alert("Prompt copied. Paste in the chat composer and send — the agent will set up your database.")
    } catch {
      // Clipboard API can fail in non-secure contexts (some self-hosters
      // may run http://). Fall back to a prompt window so the user can
      // still grab the text.
      window.prompt("Copy this and paste in chat:", CONNECT_DB_PROMPT)
    }
  }

  function coerce(v: string): unknown {
    if (v === "") return null
    if (v === "true")  return 1
    if (v === "false") return 0
    if (/^-?\d+(\.\d+)?$/.test(v)) return Number(v)
    return v
  }

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div style={{
      display: "flex", "flex-direction": "column", height: "100%",
      "font-family": "ui-sans-serif, system-ui, sans-serif", color: "var(--color-fg, #e7e7e7)",
      background: "var(--color-bg, #0c0c0e)",
    }}>
      {/* Header — driver + connection state come from /db/info.
          When Supabase is connected we add a compact host badge so the user
          can see at a glance which project this workspace is wired to. */}
      <div style={{
        display: "flex", "align-items": "center", "justify-content": "space-between",
        padding: "10px 14px", "border-bottom": "1px solid var(--color-border, #222)",
      }}>
        <div style={{ "font-weight": 600, display: "flex", "align-items": "center", gap: "10px" }}>
          Data
          {/* Driver label — three modes per LAUNCH_PLAN D9:
              - postgres-local : Forge provisioned the schema (local-self-host default)
              - supabase       : user connected BYO Supabase (hosted, or layered for Auth)
              - sqlite         : legacy holdover from pre-D9 projects
              The label tells the user where their data actually lives, no lies. */}
          <span style={{ opacity: 0.6, "font-weight": 400, "font-size": "12px" }}>
            {driverLabel(info())}
          </span>
          <Show when={info()?.supabase.connected && info()?.supabase.url && !info()?.supabase.provisioned_locally}>
            <span title={info()!.supabase.url!} style={{
              "font-weight": 500, "font-size": "11px",
              padding: "2px 8px", "border-radius": "999px",
              background: "var(--color-accent-bg, #1a2533)",
              color: "var(--color-accent-fg, #6db4ff)",
              "letter-spacing": "0.02em",
            }}>
              ● Supabase: {supabaseHost(info()!.supabase.url!)}
            </span>
          </Show>
        </div>
        <div style={{ display: "flex", gap: "8px" }}>
          <button onClick={() => setSqlOpen(s => !s)} style={btnStyle()}>SQL</button>
          {/* CTA logic:
              - BYO Supabase connected (provisioned_locally=false) → deep-link to dashboard
              - Postgres-local connected → no CTA, the DB is internal (deploy via "Download project")
              - Nothing connected → "Connect Database" (sends chat prompt) */}
          <Show when={info()?.supabase.connected && !info()?.supabase.provisioned_locally}>
            <a
              href={info()!.supabase.url!}
              target="_blank"
              rel="noopener noreferrer"
              style={{ ...btnPrimaryStyle(), "text-decoration": "none" }}
            >
              Open in Supabase ↗
            </a>
          </Show>
        </div>
      </div>

      <Show when={error()}>
        <div style={{ padding: "8px 14px", background: "#3a1818", color: "#ffb4b4", "font-size": "12px" }}>
          {error()}
          <button onClick={() => setError(null)} style={{ "margin-left": "8px", background: "transparent", color: "inherit", border: "none", cursor: "pointer" }}>×</button>
        </div>
      </Show>

      <div style={{ display: "flex", flex: 1, "min-height": 0 }}>
        {/* Left rail */}
        <div style={{
          width: "220px", "border-right": "1px solid var(--color-border, #222)",
          overflow: "auto", padding: "8px 0",
        }}>
          {/* Empty-state branches:
              1. No connection at all → big "Connect Database" CTA. Click
                 copies a chat prompt and tells the user to paste/send. The
                 AI runs db.md skill, calls /db/provision, scaffolds Drizzle,
                 and creates tables. Auto-poll below picks up the result.
              2. Connected (postgres-local or BYO) but no tables yet → "Ask
                 the agent to add tables" nudge. */}
          <Show when={tables().length === 0 && !loading()}>
            <Show
              when={info() && !info()!.supabase.connected}
              fallback={
                <div style={{ padding: "12px", opacity: 0.6, "font-size": "12px", "line-height": 1.55 }}>
                  <Show
                    when={info()?.supabase.provisioned_locally}
                    fallback={
                      <>
                        Local prototype DB is empty. Production tables live in Supabase —{" "}
                        <a
                          href={info()?.supabase.url ?? "#"}
                          target="_blank"
                          rel="noopener noreferrer"
                          style={{ color: "var(--color-accent-fg, #6db4ff)" }}
                        >
                          open the dashboard ↗
                        </a>
                      </>
                    }
                  >
                    Database connected — ask the agent in chat to add your first table
                    (e.g. <em>"add a tasks table with name, done, due_date"</em>).
                  </Show>
                </div>
              }
            >
              <div style={{ padding: "16px 12px", display: "flex", "flex-direction": "column", gap: "10px" }}>
                <div style={{ "font-size": "12px", "line-height": 1.55, opacity: 0.8 }}>
                  No database yet. Click below to have the agent set one up.
                </div>
                <button
                  onClick={() => void connectDatabase()}
                  style={{
                    ...btnPrimaryStyle(),
                    padding: "10px 14px",
                    "font-size": "13px",
                    width: "100%",
                  }}
                >
                  Connect Database
                </button>
                <div style={{ "font-size": "11px", opacity: 0.55, "line-height": 1.5 }}>
                  Copies a prompt to your clipboard — paste in chat and the agent
                  will provision a Postgres schema and scaffold Drizzle.
                </div>
              </div>
            </Show>
          </Show>
          <For each={tables()}>{(t) => (
            <button onClick={() => setActiveTable(t.name)} style={{
              display: "flex", "justify-content": "space-between", "align-items": "center",
              width: "100%", padding: "8px 14px", "text-align": "left",
              background: activeTable() === t.name ? "var(--color-accent-bg, #1a2533)" : "transparent",
              color: "inherit", border: "none", cursor: "pointer", "font-size": "13px",
            }}>
              <span>{t.name}</span>
              <span style={{ opacity: 0.5, "font-size": "11px" }}>{t.row_count}</span>
            </button>
          )}</For>
        </div>

        {/* Main grid */}
        <div style={{ flex: 1, overflow: "auto", "min-width": 0 }}>
          <Show when={activeTable() && rows()}>
            <div style={{ padding: "8px 14px", display: "flex", "align-items": "center", gap: "8px" }}>
              <button onClick={() => setShowInsert(true)} style={btnStyle()}>+ Row</button>
              <button onClick={() => refreshRows()} style={btnStyle()}>↻</button>
              <div style={{ "margin-left": "auto", opacity: 0.6, "font-size": "12px" }}>
                {offset() + 1}–{Math.min(offset() + (rows()?.rows.length ?? 0), rows()?.total ?? 0)}
                {" of "}{rows()?.total}
              </div>
              <button
                disabled={offset() === 0}
                onClick={() => setOffset(o => Math.max(0, o - PAGE_SIZE))}
                style={btnStyle()}>‹</button>
              <button
                disabled={offset() + PAGE_SIZE >= (rows()?.total ?? 0)}
                onClick={() => setOffset(o => o + PAGE_SIZE)}
                style={btnStyle()}>›</button>
            </div>

            <table style={{ width: "100%", "border-collapse": "collapse", "font-size": "12px" }}>
              <thead>
                <tr>
                  <For each={rows()?.columns ?? []}>{(c) => (
                    <th style={thStyle()}>{c}</th>
                  )}</For>
                  <th style={thStyle()} />
                </tr>
              </thead>
              <tbody>
                <For each={rows()?.rows ?? []}>{(row) => {
                  const pk = pkColumn()
                  const pkVal = pk ? String(row[pk] ?? "") : ""
                  return (
                    <tr>
                      <For each={rows()?.columns ?? []}>{(col) => {
                        const e = editing()
                        const isEditing = e && e.pk === pkVal && e.col === col
                        return (
                          <td
                            style={tdStyle()}
                            onDblClick={() => {
                              if (!pk) return
                              setEditing({ pk: pkVal, col })
                              setEditValue(row[col] == null ? "" : String(row[col]))
                            }}>
                            <Show
                              when={isEditing}
                              fallback={<span>{row[col] == null ? <em style={{ opacity: 0.4 }}>null</em> : String(row[col])}</span>}>
                              <input
                                value={editValue()}
                                onInput={(e) => setEditValue(e.currentTarget.value)}
                                onBlur={commitEdit}
                                onKeyDown={(e) => {
                                  if (e.key === "Enter") commitEdit()
                                  if (e.key === "Escape") setEditing(null)
                                }}
                                autofocus
                                style={inputStyle()}
                              />
                            </Show>
                          </td>
                        )
                      }}</For>
                      <td style={tdStyle()}>
                        <Show when={pk}>
                          <button onClick={() => doDelete(pkVal)} style={btnDangerStyle()}>✕</button>
                        </Show>
                      </td>
                    </tr>
                  )
                }}</For>
              </tbody>
            </table>
            <Show when={(rows()?.rows.length ?? 0) === 0}>
              <div style={{ padding: "30px", "text-align": "center", opacity: 0.5, "font-size": "12px" }}>
                No rows yet.
              </div>
            </Show>
          </Show>
          <Show when={!activeTable() && !loading()}>
            <div style={{ padding: "30px", opacity: 0.6, "font-size": "12px" }}>
              Pick a table on the left.
            </div>
          </Show>
        </div>
      </div>

      {/* SQL drawer */}
      <Show when={sqlOpen()}>
        <div style={{ "border-top": "1px solid var(--color-border, #222)", padding: "10px 14px", background: "#0a0a0c" }}>
          <textarea
            value={sqlText()}
            onInput={(e) => setSqlText(e.currentTarget.value)}
            placeholder="SELECT * FROM ..."
            style={{
              width: "100%", "min-height": "70px", background: "#16161a",
              color: "inherit", border: "1px solid var(--color-border, #222)",
              "border-radius": "6px", padding: "8px", "font-family": "ui-monospace, monospace",
              "font-size": "12px",
            }}
          />
          <div style={{ display: "flex", gap: "8px", "margin-top": "6px" }}>
            <button onClick={runSql} style={btnPrimaryStyle()}>Run</button>
            <button onClick={() => { setSqlText(""); setSqlResult(null) }} style={btnStyle()}>Clear</button>
          </div>
          <Show when={sqlResult()}>
            <div style={{ "margin-top": "8px", "max-height": "200px", overflow: "auto" }}>
              <table style={{ width: "100%", "border-collapse": "collapse", "font-size": "11px" }}>
                <thead><tr>
                  <For each={sqlResult()?.columns}>{(c) => <th style={thStyle()}>{c}</th>}</For>
                </tr></thead>
                <tbody>
                  <For each={sqlResult()?.rows}>{(r) => (
                    <tr><For each={r}>{(v) => <td style={tdStyle()}>{v == null ? <em style={{ opacity: 0.4 }}>null</em> : String(v)}</td>}</For></tr>
                  )}</For>
                </tbody>
              </table>
            </div>
          </Show>
        </div>
      </Show>

      {/* Insert modal */}
      <Show when={showInsert()}>
        <ModalShell onClose={() => setShowInsert(false)} title={`Insert into ${activeTable()}`}>
          <For each={activeTableMeta()?.columns ?? []}>{(c) => (
            <Show when={!(c.primary_key && c.type.toLowerCase().includes("int"))}>
              <label style={{ display: "block", "margin-bottom": "8px", "font-size": "12px" }}>
                <span style={{ opacity: 0.7 }}>{c.name} <em style={{ opacity: 0.5 }}>({c.type})</em></span>
                <input
                  value={insertValues()[c.name] ?? ""}
                  onInput={(e) => setInsertValues({ ...insertValues(), [c.name]: e.currentTarget.value })}
                  style={inputStyle()}
                />
              </label>
            </Show>
          )}</For>
          <button onClick={doInsert} style={btnPrimaryStyle()}>Insert</button>
        </ModalShell>
      </Show>

      {/* Migrate modal */}
      <Show when={showMigrate()}>
        <ModalShell onClose={() => setShowMigrate(false)} title="Migrate to Supabase / Postgres">
          <p style={{ "font-size": "12px", opacity: 0.7, "margin-bottom": "10px" }}>
            Forge will read your <code>schema.ts</code>, create the same tables in Postgres,
            and stream rows over. Your app's <code>client.ts</code> driver gets swapped — the rest of your code is unchanged.
          </p>
          <Show
            when={!migrate()}
            fallback={
              <div style={{ "font-size": "12px" }}>
                <div>Status: <strong>{migrate()?.status}</strong></div>
                <div style={{
                  height: "6px", background: "#222", "border-radius": "3px",
                  "margin": "8px 0", overflow: "hidden",
                }}>
                  <div style={{
                    height: "100%", width: `${migrate()?.progress ?? 0}%`,
                    background: "#3b82f6", transition: "width 0.3s",
                  }} />
                </div>
                <div style={{ opacity: 0.7 }}>{migrate()?.message}</div>
              </div>
            }>
            <label style={{ display: "block", "font-size": "12px", "margin-bottom": "10px" }}>
              Postgres connection URL
              <input
                value={migrateUrl()}
                onInput={(e) => setMigrateUrl(e.currentTarget.value)}
                placeholder="postgresql://postgres:[pw]@db.xxx.supabase.co:5432/postgres"
                style={inputStyle()}
              />
              <span style={{ opacity: 0.5, "font-size": "11px" }}>
                Find this in Supabase → Project Settings → Database → Connection string.
              </span>
            </label>
            <button onClick={startMigration} style={btnPrimaryStyle()}>Start migration</button>
          </Show>
        </ModalShell>
      </Show>
    </div>
  )
}

// ── Style helpers (inline so the panel doesn't depend on a CSS framework) ──
const btnStyle = () => ({
  background: "var(--color-button-bg, #1a1a1f)", color: "inherit",
  border: "1px solid var(--color-border, #2a2a30)", padding: "5px 10px",
  "border-radius": "5px", "font-size": "12px", cursor: "pointer",
} as const)
const btnPrimaryStyle = () => ({
  ...btnStyle(), background: "#1f4ed8", "border-color": "#1f4ed8", color: "white",
} as const)
const btnDangerStyle = () => ({
  ...btnStyle(), background: "transparent", "border-color": "transparent",
  color: "#f87171", padding: "2px 6px",
} as const)
const thStyle = () => ({
  "text-align": "left", padding: "6px 10px",
  "border-bottom": "1px solid var(--color-border, #222)",
  background: "#101014", "font-weight": 500, opacity: 0.7,
  position: "sticky" as const, top: "0px",
} as const)
const tdStyle = () => ({
  padding: "5px 10px", "border-bottom": "1px solid var(--color-border, #181820)",
  "white-space": "nowrap" as const, "max-width": "300px",
  overflow: "hidden", "text-overflow": "ellipsis",
} as const)
const inputStyle = () => ({
  width: "100%", background: "#16161a", color: "inherit",
  border: "1px solid var(--color-border, #2a2a30)",
  "border-radius": "4px", padding: "5px 8px",
  "font-family": "inherit", "font-size": "12px",
} as const)

function ModalShell(props: { onClose: () => void; title: string; children: any }) {
  return (
    <div onClick={props.onClose} style={{
      position: "fixed", inset: "0", background: "rgba(0,0,0,0.6)",
      display: "flex", "align-items": "center", "justify-content": "center", "z-index": 100,
    }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        background: "#16161a", border: "1px solid #2a2a30",
        "border-radius": "8px", padding: "20px", width: "440px", "max-width": "90vw",
        "max-height": "80vh", overflow: "auto",
      }}>
        <div style={{ display: "flex", "justify-content": "space-between", "margin-bottom": "12px" }}>
          <strong style={{ "font-size": "14px" }}>{props.title}</strong>
          <button onClick={props.onClose} style={{ background: "transparent", color: "inherit", border: "none", cursor: "pointer", "font-size": "18px" }}>×</button>
        </div>
        {props.children}
      </div>
    </div>
  )
}
