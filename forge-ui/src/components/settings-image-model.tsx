/**
 * settings-image-model.tsx
 * =========================
 * Settings tab for AI image generation.
 *
 * Surfaces:
 *   - Mode toggle (off | auto | ask) — the consent gate.
 *   - Model picker — the curated registry + user's custom entries, with a
 *     `unlocked` badge that disables rows whose required provider key is
 *     missing (and links to the Providers tab to add one).
 *   - Custom AI form — define an arbitrary provider (e.g. self-hosted SDXL
 *     or an internal OpenRouter-compatible gateway) by pasting
 *     base URL + model id + protocol.
 *
 * Talks to /api/image-models (curated + custom merged) and
 * /api/user/settings (mode + selected model + custom_image_providers
 * round-trip). Keys live in user_provider_keys via the existing Providers
 * tab — adding a key here would split the storage path and is intentionally
 * NOT done.
 */
import { ProviderIcon } from "@opencode-ai/ui/provider-icon"
import { type Component, createResource, createSignal, For, Show } from "solid-js"
import { useForgeApi, type CustomImageProviderConfig, type ImageModelInfo } from "@/context/forge-api"
import { SettingsList } from "./settings-list"

type ImageMode = "off" | "auto" | "ask"
const MODES: { value: ImageMode; label: string; hint: string }[] = [
  { value: "off",  label: "Off",                hint: "Agent never generates images." },
  { value: "auto", label: "Auto",               hint: "Agent decides; runs in the background." },
  { value: "ask",  label: "Ask once per session", hint: "Confirm the first time per session, then sticky." },
]

const PROTOCOLS = ["replicate", "openrouter_chat", "openai_images", "google_imagen"] as const

// Human-friendly labels for the protocol dropdown. Raw ids leak into screen
// shots otherwise (bug surfaced 2026-06-04) and confuse non-engineers picking
// a wire format. Source-of-truth IDs unchanged on the wire — these are
// presentation-only.
const PROTOCOL_LABELS: Record<typeof PROTOCOLS[number], string> = {
  replicate:       "Replicate · prediction API",
  openrouter_chat: "OpenRouter · chat completions (image modality)",
  openai_images:   "OpenAI · /images/generations",
  google_imagen:   "Google · Imagen :generateContent",
}

export const SettingsImageModel: Component = () => {
  const forge = useForgeApi()

  const [settings, { refetch: refetchSettings }] = createResource(
    () => forge.getSettings().catch(() => null),
  )
  const [catalog, { refetch: refetchCatalog }] = createResource(
    () => forge.listImageModels().catch(() => ({ models: [], connected_key_providers: [] })),
  )

  const [saving,   setSaving]   = createSignal(false)
  const [saveErr,  setSaveErr]  = createSignal("")
  const [savedMsg, setSavedMsg] = createSignal(false)

  // ── Mode / model save helpers ────────────────────────────────────────────

  async function patch(patch: Record<string, unknown>) {
    setSaving(true); setSaveErr(""); setSavedMsg(false)
    try {
      await forge.updateSettings(patch as never)
      void refetchSettings()
      void refetchCatalog()
      setSavedMsg(true)
      setTimeout(() => setSavedMsg(false), 2000)
    } catch (e) {
      // Surface the server's reason verbatim — invalid mode / protocol
      // errors are user-actionable.
      setSaveErr(String((e as Error)?.message ?? "Failed to save"))
    } finally {
      setSaving(false)
    }
  }

  function selectModel(id: string) {
    if (id === settings()?.image_model) return
    void patch({ image_model: id })
  }

  function setMode(value: ImageMode) {
    if (value === settings()?.image_mode) return
    void patch({ image_mode: value })
  }

  // ── Custom-AI form state ─────────────────────────────────────────────────
  // Whole-map PUT semantics (matches BE): build the new map from existing +
  // user input, send. PATCH layer rejects malformed entries up front so a
  // bad add never partially persists.

  const [showCustom, setShowCustom] = createSignal(false)
  const [cProviderId,    setCProviderId]    = createSignal("")
  const [cModelId,       setCModelId]       = createSignal("")
  const [cDisplayName,   setCDisplayName]   = createSignal("")
  const [cKeyProvider,   setCKeyProvider]   = createSignal("")
  const [cProtocol,      setCProtocol]      = createSignal<typeof PROTOCOLS[number]>("openrouter_chat")
  const [cBaseUrl,       setCBaseUrl]       = createSignal("")
  const [cSupportsImg2,  setCSupportsImg2]  = createSignal(false)

  // ── Inline "Connect key" state per locked row ────────────────────────────
  // A locked row used to be inert (button disabled). The user has to jump to
  // Providers, find the right provider id, paste the key, come back, refetch.
  // Replace that with an inline form so unlocking is one click away.
  const [keyRowOpen, setKeyRowOpen] = createSignal<string | null>(null)
  const [keyValue,   setKeyValue]   = createSignal("")
  const [keySaving,  setKeySaving]  = createSignal(false)

  async function connectKey(providerId: string) {
    const v = keyValue().trim()
    if (!v) { setSaveErr("API key is required"); return }
    setKeySaving(true); setSaveErr("")
    try {
      await forge.setProviderKey(providerId, v)
      setKeyRowOpen(null)
      setKeyValue("")
      // Refetch the catalog so the row flips to unlocked AND the now-selectable
      // models appear in available_for() filter.
      void refetchCatalog()
      void refetchSettings()
      setSavedMsg(true); setTimeout(() => setSavedMsg(false), 2000)
    } catch (e) {
      setSaveErr(String((e as Error)?.message ?? "Failed to save key"))
    } finally {
      setKeySaving(false)
    }
  }

  function resetCustomForm() {
    setCProviderId(""); setCModelId(""); setCDisplayName("")
    setCKeyProvider(""); setCProtocol("openrouter_chat"); setCBaseUrl(""); setCSupportsImg2(false)
  }

  async function addCustom() {
    const provider_id = cProviderId().trim()
    const model_id    = cModelId().trim()
    if (!provider_id || !model_id) {
      setSaveErr("provider id and model id are required")
      return
    }
    const cfg: CustomImageProviderConfig = {
      provider_id,
      model_id,
      display_name:          cDisplayName().trim() || `${provider_id}/${model_id}`,
      required_key_provider: cKeyProvider().trim() || provider_id,
      protocol:              cProtocol(),
      base_url:              cBaseUrl().trim() || null,
      supports_img2img:      cSupportsImg2(),
    }
    const existing = settings()?.custom_image_providers ?? {}
    const next     = { ...existing, [`${provider_id}/${model_id}`]: cfg }
    await patch({ custom_image_providers: next })
    setShowCustom(false)
    resetCustomForm()
  }

  async function removeCustom(id: string) {
    const existing = settings()?.custom_image_providers ?? {}
    if (!(id in existing)) return
    const next = { ...existing }
    delete next[id]
    await patch({ custom_image_providers: next })
  }

  // ── Render helpers ───────────────────────────────────────────────────────

  const selectedId = () => settings()?.image_model ?? ""

  function modelRow(m: ImageModelInfo) {
    const isSelected = () => selectedId() === m.id
    const isKeyRowOpen = () => keyRowOpen() === m.id
    return (
      <div class="border-b border-border-weak-base last:border-none">
        <div class="w-full flex items-center justify-between gap-3 py-3 px-1">
          {/* Left: radio + provider icon + name + description */}
          <button
            type="button"
            disabled={saving() || !m.unlocked}
            onClick={() => m.unlocked && selectModel(m.id)}
            class="flex items-center gap-3 min-w-0 flex-1 text-left rounded-sm transition-colors hover:bg-surface-hover-base disabled:cursor-not-allowed"
            title={!m.unlocked ? `Add a ${m.required_key_provider} key to unlock` : ""}
          >
            <div
              class="flex-shrink-0 size-4 rounded-full border-2 flex items-center justify-center transition-colors"
              classList={{
                "border-text-accent bg-text-accent":               isSelected() && m.unlocked,
                "border-border-base bg-transparent":               !(isSelected() && m.unlocked),
                "opacity-50":                                       !m.unlocked,
              }}
            >
              <Show when={isSelected() && m.unlocked}>
                <div class="size-1.5 rounded-full bg-white" />
              </Show>
            </div>
            <ProviderIcon id={m.required_key_provider} class="size-5 shrink-0 icon-strong-base" />
            <div class="flex flex-col min-w-0">
              <span
                class="text-14-regular truncate"
                classList={{
                  "text-text-strong": isSelected() && m.unlocked,
                  "text-text-base":   !(isSelected() && m.unlocked),
                  "opacity-70":       !m.unlocked,
                }}
              >
                {m.display_name}
              </span>
              <span class="text-12-regular text-text-weak truncate">{m.description}</span>
            </div>
          </button>

          {/* Right: price + action */}
          <div class="flex items-center gap-2 flex-shrink-0">
            <span class="text-12-regular text-text-weak">${m.price_usd_per_image.toFixed(3)}/img</span>

            <Show when={!m.unlocked}>
              {/* Clickable inline-unlock action. Used to be a static badge;
                  one-click unlock removes the dead-end UX from the screenshots. */}
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  setKeyRowOpen(isKeyRowOpen() ? null : m.id)
                  setKeyValue("")
                }}
                class="text-11-regular text-text-accent border border-text-accent rounded px-1.5 py-0.5 hover:bg-surface-accent-base"
              >
                {isKeyRowOpen() ? "Cancel" : `+ Add ${m.required_key_provider} key`}
              </button>
            </Show>

            <Show when={m.source === "custom"}>
              <button
                type="button"
                class="text-11-regular text-text-weak hover:text-text-danger"
                onClick={(e) => { e.stopPropagation(); void removeCustom(m.id) }}
              >
                Remove
              </button>
            </Show>
          </div>
        </div>

        {/* Inline key-entry row. Renders only when the user clicks
            "+ Add <provider> key" on a locked row. Saves through the
            existing /api/user/providers POST — no new endpoint. */}
        <Show when={isKeyRowOpen()}>
          <div class="pb-3 px-1 flex items-center gap-2">
            <input
              type="password"
              autocomplete="off"
              placeholder={`paste your ${m.required_key_provider} API key`}
              value={keyValue()}
              onInput={(e) => setKeyValue(e.currentTarget.value)}
              class="flex-1 rounded border border-border-weak-base bg-surface-base px-2 py-1.5 text-13-regular"
            />
            <button
              type="button"
              disabled={keySaving() || !keyValue().trim()}
              onClick={() => void connectKey(m.required_key_provider)}
              class="rounded-md bg-surface-accent-base border border-text-accent text-text-strong px-3 py-1.5 text-13-medium disabled:opacity-60"
            >
              {keySaving() ? "Saving…" : "Save key"}
            </button>
          </div>
        </Show>
      </div>
    )
  }

  return (
    <div class="flex flex-col h-full overflow-y-auto no-scrollbar px-4 pb-10 sm:px-10 sm:pb-10">
      {/* Header */}
      <div class="flex flex-col gap-3 pt-6 pb-6 max-w-[720px]">
        <h2 class="text-16-medium text-text-strong">Image AI</h2>
        <div class="rounded-lg border border-border-weak-base bg-surface-base px-4 py-3 flex flex-col gap-1.5 max-w-[600px]">
          <span class="text-13-medium text-text-strong">When this runs</span>
          <p class="text-13-regular text-text-weak leading-relaxed">
            When enabled, the building agent generates illustrations and photos directly into your pages
            instead of leaving blank slots. Keys are billed to your own account — Forge passes them through;
            never stores plaintext.
          </p>
        </div>

        {/* Mode toggle */}
        <div class="flex flex-col gap-2">
          <span class="text-13-medium text-text-strong">Mode</span>
          <div class="flex gap-2 flex-wrap">
            <For each={MODES}>
              {(m) => {
                const active = () => (settings()?.image_mode ?? "off") === m.value
                return (
                  <button
                    type="button"
                    disabled={saving()}
                    onClick={() => setMode(m.value)}
                    class="rounded-md border px-3 py-1.5 text-13-regular transition-colors"
                    classList={{
                      "border-text-accent bg-surface-accent-base text-text-strong": active(),
                      "border-border-weak-base bg-surface-base text-text-base hover:bg-surface-hover-base": !active(),
                    }}
                    title={m.hint}
                  >
                    {m.label}
                  </button>
                )
              }}
            </For>
          </div>
        </div>

        <Show when={savedMsg()}>
          <span class="text-13-regular text-text-success">✓ Saved</span>
        </Show>
        <Show when={saveErr()}>
          <span class="text-13-regular text-text-danger">{saveErr()}</span>
        </Show>
      </div>

      {/* Models */}
      <div class="flex flex-col gap-4 max-w-[720px]">
        <div class="flex items-center justify-between">
          <span class="text-13-medium text-text-strong">Model</span>
          <button
            type="button"
            onClick={() => setShowCustom((v) => !v)}
            class="text-13-regular text-text-accent hover:underline"
          >
            {showCustom() ? "Cancel" : "+ Add custom AI"}
          </button>
        </div>

        <Show when={showCustom()}>
          <div class="flex flex-col gap-3 rounded-lg border border-border-weak-base bg-surface-base p-4">
            <p class="text-12-regular text-text-weak leading-relaxed">
              Point Forge at any image API. Reuses the API key you added under <span class="text-text-base font-medium">Providers</span> for the same provider id.
            </p>
            <div class="grid grid-cols-2 gap-3">
              <input
                class="rounded border border-border-weak-base bg-surface-base px-2 py-1.5 text-13-regular"
                placeholder="provider id (e.g. myco)"
                value={cProviderId()}
                onInput={(e) => setCProviderId(e.currentTarget.value)}
              />
              <input
                class="rounded border border-border-weak-base bg-surface-base px-2 py-1.5 text-13-regular"
                placeholder="model id (e.g. flux-pro)"
                value={cModelId()}
                onInput={(e) => setCModelId(e.currentTarget.value)}
              />
              <input
                class="rounded border border-border-weak-base bg-surface-base px-2 py-1.5 text-13-regular col-span-2"
                placeholder="display name (optional)"
                value={cDisplayName()}
                onInput={(e) => setCDisplayName(e.currentTarget.value)}
              />
              <input
                class="rounded border border-border-weak-base bg-surface-base px-2 py-1.5 text-13-regular"
                placeholder="required key provider id (defaults to provider id)"
                value={cKeyProvider()}
                onInput={(e) => setCKeyProvider(e.currentTarget.value)}
              />
              <select
                class="rounded border border-border-weak-base bg-surface-base px-2 py-1.5 text-13-regular"
                value={cProtocol()}
                onChange={(e) => setCProtocol(e.currentTarget.value as typeof PROTOCOLS[number])}
                title="Wire shape the adapter uses to call your endpoint"
              >
                <For each={PROTOCOLS}>{(p) => <option value={p}>{PROTOCOL_LABELS[p]}</option>}</For>
              </select>
              <input
                class="rounded border border-border-weak-base bg-surface-base px-2 py-1.5 text-13-regular col-span-2"
                placeholder="base url (e.g. https://api.example.com/v1)"
                value={cBaseUrl()}
                onInput={(e) => setCBaseUrl(e.currentTarget.value)}
              />
              <label class="flex items-center gap-2 text-13-regular text-text-base">
                <input
                  type="checkbox"
                  checked={cSupportsImg2()}
                  onChange={(e) => setCSupportsImg2(e.currentTarget.checked)}
                />
                supports image-to-image
              </label>
            </div>
            <div class="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => { setShowCustom(false); resetCustomForm() }}
                class="text-13-regular text-text-weak hover:text-text-base"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={saving()}
                onClick={() => void addCustom()}
                class="rounded-md bg-surface-accent-base border border-text-accent text-text-strong px-3 py-1.5 text-13-medium disabled:opacity-60"
              >
                Add
              </button>
            </div>
          </div>
        </Show>

        <SettingsList>
          <Show
            when={(catalog()?.models?.length ?? 0) > 0}
            fallback={
              <div class="py-8 text-center text-13-regular text-text-weak">No image models available.</div>
            }
          >
            <For each={catalog()?.models ?? []}>{(m) => modelRow(m)}</For>
          </Show>
        </SettingsList>

        <Show when={(catalog()?.connected_key_providers ?? []).length === 0}>
          <p class="text-12-regular text-text-weak max-w-[520px]">
            You don't have any image-provider keys connected yet. Add one under{" "}
            <span class="text-text-base font-medium">Settings → Providers</span> (Replicate, OpenRouter, OpenAI, Google).
          </p>
        </Show>
      </div>
    </div>
  )
}
