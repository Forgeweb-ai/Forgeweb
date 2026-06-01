/**
 * settings-api-keys.tsx
 * =====================
 * Settings panel for managing per-user encrypted API keys.
 *
 * Keys are stored encrypted in forge-server's DB and written to opencode's
 * auth.json on save so opencode picks them up without a restart.
 *
 * Key values are never returned by the API — the UI only shows provider name,
 * optional label, and last-updated timestamp.
 */
import type { Component } from "solid-js"
import { createResource, createSignal, For, Show } from "solid-js"
import { useForgeApi, type ProviderKeyOut } from "@/context/forge-api"
import { SettingsList } from "./settings-list"

// Providers we support (displayed in the "Add key" section)
const KNOWN_PROVIDERS = [
  { id: "anthropic",  label: "Anthropic",        placeholder: "sk-ant-api03-…" },
  { id: "openai",     label: "OpenAI",            placeholder: "sk-…" },
  { id: "moonshot",   label: "Moonshot / Kimi",   placeholder: "sk-…" },
  { id: "google",     label: "Google Gemini",     placeholder: "AIza…" },
  { id: "openrouter", label: "OpenRouter",        placeholder: "sk-or-v1-…" },
]

export const SettingsApiKeys: Component = () => {
  const forge = useForgeApi()

  const [keys, { refetch }] = createResource<ProviderKeyOut[]>(
    () => forge.listProviderKeys().catch(() => [] as ProviderKeyOut[])
  )

  // Per-provider input state (for the "Add / Update" form)
  const [selectedProvider, setSelectedProvider] = createSignal(KNOWN_PROVIDERS[0].id)
  const [keyValue,  setKeyValue]  = createSignal("")
  const [keyLabel,  setKeyLabel]  = createSignal("")
  const [saving,    setSaving]    = createSignal(false)
  const [saveError, setSaveError] = createSignal("")
  const [savedMsg,  setSavedMsg]  = createSignal("")

  async function handleSave() {
    const key = keyValue().trim()
    if (!key) { setSaveError("API key is required"); return }
    setSaving(true); setSaveError(""); setSavedMsg("")
    try {
      await forge.setProviderKey(
        selectedProvider(),
        key,
        keyLabel().trim() || undefined,
      )
      setKeyValue(""); setKeyLabel("")
      setSavedMsg(`Key saved for ${KNOWN_PROVIDERS.find(p => p.id === selectedProvider())?.label ?? selectedProvider()}`)
      setTimeout(() => setSavedMsg(""), 3000)
      void refetch()
    } catch (e: any) {
      setSaveError(e?.message ?? "Failed to save key")
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete(providerId: string) {
    try {
      await forge.deleteProviderKey(providerId)
      void refetch()
    } catch (e: any) {
      console.error("Failed to delete key:", e)
    }
  }

  const providerLabel = (id: string) =>
    KNOWN_PROVIDERS.find(p => p.id === id)?.label ?? id

  const selectedPlaceholder = () =>
    KNOWN_PROVIDERS.find(p => p.id === selectedProvider())?.placeholder ?? "API key…"

  // ── styles (inline so no new CSS file needed) ─────────────────────────────

  const sectionTitle: string = [
    "font-size: 11px",
    "letter-spacing: 0.07em",
    "text-transform: uppercase",
    "color: var(--muted)",
    "font-family: var(--font-mono)",
    "margin-bottom: 8px",
  ].join(";")

  const inputStyle = [
    "width: 100%",
    "padding: 7px 10px",
    "border-radius: 6px",
    "border: 1px solid var(--hair)",
    "background: var(--surface)",
    "color: var(--text-strong)",
    "font-size: 13px",
    "font-family: var(--font-ui)",
    "box-sizing: border-box",
  ].join(";")

  const labelStyle = [
    "display: block",
    "font-size: 12px",
    "color: var(--muted)",
    "margin-bottom: 4px",
    "font-family: var(--font-ui)",
  ].join(";")

  return (
    <SettingsList>
      {/* ── Header ──────────────────────────────────────────────────── */}
      <div style="padding: 20px 16px 14px; border-bottom: 1px solid var(--hair);">
        <div style="font-size: 15px; font-weight: 600; color: var(--text-strong); font-family: var(--font-ui); margin-bottom: 4px;">API Keys</div>
        <div style="font-size: 12.5px; color: var(--muted); font-family: var(--font-ui); line-height: 1.4;">
          Store provider API keys encrypted. Keys are written to opencode immediately on save — no restart needed.
        </div>
      </div>

      {/* ── Stored keys ─────────────────────────────────────────────── */}
      <div style="padding: 16px 16px 0;">
        <div style={sectionTitle}>Stored Keys</div>
        <Show
          when={!keys.loading}
          fallback={<div style="font-size: 13px; color: var(--muted); padding: 8px 0;">Loading…</div>}
        >
          <Show
            when={(keys() ?? []).length > 0}
            fallback={
              <div style="font-size: 13px; color: var(--muted); padding: 8px 0; font-style: italic;">
                No keys stored yet.
              </div>
            }
          >
            <div style="display: flex; flex-direction: column; gap: 8px; margin-bottom: 20px;">
              <For each={keys() ?? []}>
                {(k) => (
                  <div style={[
                    "display: flex",
                    "align-items: center",
                    "justify-content: space-between",
                    "gap: 12px",
                    "padding: 9px 12px",
                    "border-radius: 7px",
                    "border: 1px solid var(--hair)",
                    "background: var(--surface)",
                  ].join(";")}>
                    <div>
                      <div style="font-size: 13.5px; font-weight: 500; color: var(--text-strong); font-family: var(--font-ui);">
                        {providerLabel(k.provider_id)}
                      </div>
                      <Show when={k.label}>
                        <div style="font-size: 11.5px; color: var(--muted); font-family: var(--font-ui);">{k.label}</div>
                      </Show>
                      <div style="font-size: 11px; color: var(--muted); font-family: var(--font-mono); margin-top: 2px;">
                        Updated {new Date(k.updated_at).toLocaleDateString()}
                      </div>
                    </div>
                    <div style="display: flex; align-items: center; gap: 8px;">
                      {/* Key value masked — show only that one is stored */}
                      <span style="font-family: var(--font-mono); font-size: 12px; color: var(--muted); letter-spacing: 3px;">••••••••</span>
                      <button
                        type="button"
                        onClick={() => void handleDelete(k.provider_id)}
                        style={[
                          "padding: 3px 9px",
                          "border-radius: 5px",
                          "border: 1px solid color-mix(in srgb, var(--syntax-error) 40%, transparent)",
                          "background: transparent",
                          "color: var(--syntax-error)",
                          "font-size: 12px",
                          "cursor: pointer",
                          "font-family: var(--font-ui)",
                        ].join(";")}
                      >
                        Remove
                      </button>
                    </div>
                  </div>
                )}
              </For>
            </div>
          </Show>
        </Show>
      </div>

      {/* ── Add / Update key ────────────────────────────────────────── */}
      <div style="padding: 0 16px 20px; border-top: 1px solid var(--hair); padding-top: 16px;">
        <div style={sectionTitle}>Add / Update Key</div>

        <div style="display: flex; flex-direction: column; gap: 10px;">
          {/* Provider picker */}
          <div>
            <label style={labelStyle}>Provider</label>
            <select
              style={inputStyle}
              value={selectedProvider()}
              onChange={(e) => setSelectedProvider(e.currentTarget.value)}
            >
              <For each={KNOWN_PROVIDERS}>
                {(p) => <option value={p.id}>{p.label}</option>}
              </For>
            </select>
          </div>

          {/* API key input */}
          <div>
            <label style={labelStyle}>API Key</label>
            <input
              type="password"
              autocomplete="off"
              style={inputStyle}
              placeholder={selectedPlaceholder()}
              value={keyValue()}
              onInput={(e) => setKeyValue(e.currentTarget.value)}
            />
          </div>

          {/* Optional label */}
          <div>
            <label style={labelStyle}>Label (optional)</label>
            <input
              type="text"
              style={inputStyle}
              placeholder="e.g. Personal key, Work key…"
              value={keyLabel()}
              onInput={(e) => setKeyLabel(e.currentTarget.value)}
            />
          </div>

          <Show when={saveError()}>
            <div style="font-size: 12px; color: var(--syntax-error); font-family: var(--font-ui);">{saveError()}</div>
          </Show>
          <Show when={savedMsg()}>
            <div style="font-size: 12px; color: var(--syntax-success); font-family: var(--font-ui);">✓ {savedMsg()}</div>
          </Show>

          <button
            type="button"
            disabled={saving()}
            onClick={() => void handleSave()}
            style={[
              "padding: 7px 16px",
              "border-radius: 6px",
              "border: 1px solid var(--hair)",
              "background: var(--text-strong)",
              "color: var(--bg)",
              "font-size: 13px",
              "font-family: var(--font-ui)",
              "cursor: pointer",
              "font-weight: 500",
              saving() ? "opacity: 0.6" : "",
            ].join(";")}
          >
            {saving() ? "Saving…" : "Save Key"}
          </button>

          <p style="font-size: 11.5px; color: var(--muted); font-family: var(--font-ui); line-height: 1.5; margin: 0;">
            Keys are encrypted with Fernet AES-128 before being stored in the database.
            The plaintext key is never logged. opencode's auth.json is updated on save
            so the running AI agent picks up the new key immediately.
          </p>
        </div>
      </div>
    </SettingsList>
  )
}
