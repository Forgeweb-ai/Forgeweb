/**
 * settings-design-model.tsx
 * ==========================
 * Settings tab for selecting the Design Agent model.
 *
 * The Design Agent consists of two subagents:
 *   - design-analyst  — evaluates UI quality and produces a structured critique
 *   - design-critic   — reviews iterations and scores visual/UX decisions
 *
 * Both subagents share a single model setting stored in forge-server's
 * user_settings table (design_model field). dev.sh reads this value at startup
 * to configure opencode's platform config.
 *
 * Uses useModels() (same context as settings-models.tsx) instead of useLocal()
 * because the Settings dialog lives outside the LocalProvider tree.
 */
import { ProviderIcon } from "@opencode-ai/ui/provider-icon"
import { type Component, createResource, createSignal, For, Show } from "solid-js"
import { useModels } from "@/context/models"
import { useForgeApi } from "@/context/forge-api"
import { popularProviders } from "@/hooks/use-providers"
import { SettingsList } from "./settings-list"

export const SettingsDesignModel: Component = () => {
  const models = useModels()
  const forge  = useForgeApi()

  const [settings, { refetch }] = createResource(
    () => forge.getSettings().catch(() => ({ design_model: "" }))
  )
  const [saving,   setSaving]   = createSignal(false)
  const [savedMsg, setSavedMsg] = createSignal(false)
  const [saveErr,  setSaveErr]  = createSignal("")

  // All models sorted popular-first then alphabetically, grouped by provider
  const allModels = () => {
    const list = models.list()
    const groups = new Map<string, typeof list>()
    for (const m of list) {
      const g = groups.get(m.provider.id) ?? []
      g.push(m)
      groups.set(m.provider.id, g)
    }
    return [...groups.entries()]
      .sort(([aId, aItems], [bId, bItems]) => {
        const aRank = popularProviders.indexOf(aId)
        const bRank = popularProviders.indexOf(bId)
        const aPopular = aRank >= 0
        const bPopular = bRank >= 0
        if (aPopular && !bPopular) return -1
        if (!aPopular && bPopular) return 1
        if (aPopular && bPopular) return aRank - bRank
        return aItems[0].provider.name.localeCompare(bItems[0].provider.name)
      })
      .map(([, items]) => ({
        provider: items[0].provider,
        items: items.sort((a, b) => a.name.localeCompare(b.name)),
      }))
  }

  const selectedValue = () => settings()?.design_model ?? ""

  async function selectModel(value: string) {
    if (value === selectedValue()) return
    setSaving(true); setSavedMsg(false); setSaveErr("")
    try {
      await forge.updateSettings({ design_model: value })
      void refetch()
      setSavedMsg(true)
      setTimeout(() => setSavedMsg(false), 2500)
    } catch (e: any) {
      setSaveErr(e?.message ?? "Failed to save")
    } finally {
      setSaving(false)
    }
  }

  const isEmpty = () => models.list().length === 0

  return (
    <div class="flex flex-col h-full overflow-y-auto no-scrollbar px-4 pb-10 sm:px-10 sm:pb-10">
      {/* ── Header ──────────────────────────────────────────────────────── */}
      <div class="flex flex-col gap-3 pt-6 pb-6 max-w-[720px]">
        <h2 class="text-16-medium text-text-strong">Design Agent</h2>

        {/* What is it */}
        <div class="rounded-lg border border-border-weak-base bg-surface-base px-4 py-3 flex flex-col gap-1.5 max-w-[600px]">
          <span class="text-13-medium text-text-strong">What is the Design Agent?</span>
          <p class="text-13-regular text-text-weak leading-relaxed">
            When you ask Forge to build or refine a UI, two subagents run in the background before the
            main coding agent touches any code:
          </p>
          <ul class="text-13-regular text-text-weak leading-relaxed list-disc pl-4 flex flex-col gap-0.5">
            <li><span class="text-text-base">design‑analyst</span> — reads your prompt and the current screenshot, then writes a structured design brief.</li>
            <li><span class="text-text-base">design‑critic</span> — scores each iteration and decides whether the result is good enough to ship.</li>
          </ul>
          <p class="text-13-regular text-text-weak leading-relaxed">
            Both subagents share the model you select here. A more capable model produces sharper design decisions;
            a faster model saves cost and time.
          </p>
        </div>

        {/* Providers hint */}
        <p class="text-12-regular text-text-weak max-w-[520px]">
          Only models you have connected appear below. To add more, go to{" "}
          <span class="text-text-base font-medium">Settings → Providers</span>.
        </p>

        {/* Save feedback */}
        <Show when={savedMsg()}>
          <span class="text-13-regular text-text-success">✓ Saved — takes effect on the next session</span>
        </Show>
        <Show when={saveErr()}>
          <span class="text-13-regular text-text-danger">{saveErr()}</span>
        </Show>
      </div>

      {/* ── Model list ──────────────────────────────────────────────────── */}
      <div class="flex flex-col gap-8 max-w-[720px]">
        <Show
          when={!isEmpty()}
          fallback={
            <div class="flex flex-col items-center justify-center py-12 text-center gap-2">
              <span class="text-14-regular text-text-weak">No providers connected yet.</span>
              <span class="text-13-regular text-text-weak">
                Go to <span class="text-text-base font-medium">Settings → Providers</span> to add one.
              </span>
            </div>
          }
        >
          <For each={allModels()}>
            {(group) => (
              <div class="flex flex-col gap-1">
                <div class="flex items-center gap-2 pb-2">
                  <ProviderIcon id={group.provider.id} class="size-5 shrink-0 icon-strong-base" />
                  <span class="text-14-medium text-text-strong">{group.provider.name}</span>
                </div>
                <SettingsList>
                  <For each={group.items}>
                    {(item) => {
                      const value = `${item.provider.id}/${item.id}`
                      const isSelected = () => selectedValue() === value
                      return (
                        <button
                          type="button"
                          disabled={saving()}
                          onClick={() => void selectModel(value)}
                          class="w-full flex items-center justify-between gap-4 py-3 border-b border-border-weak-base last:border-none text-left transition-colors hover:bg-surface-hover-base disabled:opacity-60 px-1 rounded-sm"
                        >
                          <div class="flex items-center gap-3 min-w-0">
                            {/* Radio indicator */}
                            <div
                              class="flex-shrink-0 size-4 rounded-full border-2 flex items-center justify-center transition-colors"
                              classList={{
                                "border-text-accent bg-text-accent": isSelected(),
                                "border-border-base bg-transparent": !isSelected(),
                              }}
                            >
                              <Show when={isSelected()}>
                                <div class="size-1.5 rounded-full bg-white" />
                              </Show>
                            </div>
                            <span
                              class="text-14-regular truncate block"
                              classList={{
                                "text-text-strong": isSelected(),
                                "text-text-base": !isSelected(),
                              }}
                            >
                              {item.name}
                            </span>
                          </div>
                          <Show when={isSelected()}>
                            <span class="text-12-regular text-text-accent flex-shrink-0">Selected</span>
                          </Show>
                        </button>
                      )
                    }}
                  </For>
                </SettingsList>
              </div>
            )}
          </For>
        </Show>
      </div>
    </div>
  )
}
