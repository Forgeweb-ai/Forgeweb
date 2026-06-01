import { Dialog } from "@opencode-ai/ui/dialog"
import { List } from "@opencode-ai/ui/list"
import { Switch } from "@opencode-ai/ui/switch"
import { Tooltip } from "@opencode-ai/ui/tooltip"
import { Button } from "@opencode-ai/ui/button"
import type { Component } from "solid-js"
import { createResource, createSignal, For, Show } from "solid-js"
import { useModels } from "@/context/models"
import { popularProviders } from "@/hooks/use-providers"
import { useLanguage } from "@/context/language"
import { useDialog } from "@opencode-ai/ui/context/dialog"
import { useForgeApi } from "@/context/forge-api"
import { DialogSelectProvider } from "./dialog-select-provider"

// NOTE: Uses useModels() (mounted in AppShellProviders) rather than useLocal()
// because this dialog is opened from /home, which lives outside the
// LocalProvider tree (LocalProvider is only mounted inside DirectoryLayout).
// Same constraint that settings-design-model.tsx documents.
export const DialogManageModels: Component = () => {
  const models   = useModels()
  const language = useLanguage()
  const dialog   = useDialog()
  const forge    = useForgeApi()

  // ── Design model setting ───────────────────────────────────────────────────
  const [settings, { refetch: refetchSettings }] = createResource(
    () => forge.getSettings().catch(() => ({ design_model: "anthropic/claude-sonnet-4-6" }))
  )
  const [savingDesign, setSavingDesign] = createSignal(false)
  const [designSaved,  setDesignSaved]  = createSignal(false)

  // Build a flat list of all visible models: value = "providerID/modelID"
  const designModelOptions = () =>
    models
      .list()
      .filter((m) => models.visible({ modelID: m.id, providerID: m.provider.id }))
      .sort((a, b) => {
        const provCmp = a.provider.name.localeCompare(b.provider.name)
        return provCmp !== 0 ? provCmp : a.name.localeCompare(b.name)
      })
      .map((m) => ({
        value: `${m.provider.id}/${m.id}`,
        label: `${m.provider.name} / ${m.name}`,
      }))

  async function onDesignModelChange(value: string) {
    setSavingDesign(true)
    setDesignSaved(false)
    try {
      await forge.updateSettings({ design_model: value })
      void refetchSettings()
      setDesignSaved(true)
      setTimeout(() => setDesignSaved(false), 2000)
    } catch (e) {
      console.error("Failed to save design model:", e)
    } finally {
      setSavingDesign(false)
    }
  }

  // ── Model list (existing behaviour) ───────────────────────────────────────
  const handleConnectProvider = () => {
    dialog.show(() => <DialogSelectProvider />)
  }
  const providerRank = (id: string) => popularProviders.indexOf(id)
  const providerList = (providerID: string) => models.list().filter((x) => x.provider.id === providerID)
  const providerVisible = (providerID: string) =>
    providerList(providerID).every((x) => models.visible({ modelID: x.id, providerID: x.provider.id }))
  const setProviderVisibility = (providerID: string, checked: boolean) => {
    providerList(providerID).forEach((x) => {
      models.setVisibility({ modelID: x.id, providerID: x.provider.id }, checked)
    })
  }

  return (
    <Dialog
      title={language.t("dialog.model.manage")}
      description={language.t("dialog.model.manage.description")}
      action={
        <Button class="h-7 -my-1 text-14-medium" icon="plus-small" tabIndex={-1} onClick={handleConnectProvider}>
          {language.t("command.provider.connect")}
        </Button>
      }
    >
      {/* ── Design Agent model picker ─────────────────────────────────────── */}
      <div
        style={{
          padding: "14px 16px",
          "border-bottom": "1px solid var(--hair)",
          "margin-bottom": "4px",
        }}
      >
        <div
          style={{
            display: "flex",
            "align-items": "flex-start",
            "justify-content": "space-between",
            gap: "16px",
          }}
        >
          <div style={{ "flex-shrink": "0" }}>
            <div
              style={{
                "font-size": "13.5px",
                "font-weight": "500",
                color: "var(--text-strong)",
                "margin-bottom": "2px",
              }}
            >
              Design Agent
            </div>
            <div style={{ "font-size": "12px", color: "var(--muted)", "line-height": "1.4", "max-width": "260px" }}>
              Model used by design‑analyst &amp; design‑critic subagents. Higher quality = better UI decisions.
            </div>
          </div>

          <div style={{ "flex-shrink": "0", "min-width": "220px" }}>
            <Show
              when={!settings.loading}
              fallback={
                <div style={{ "font-size": "12px", color: "var(--muted)", padding: "6px 0" }}>Loading…</div>
              }
            >
              <Show
                when={designModelOptions().length > 0}
                fallback={
                  <div style={{ "font-size": "12px", color: "var(--muted)", "font-style": "italic" }}>
                    No models connected — add a provider to select a design model.
                  </div>
                }
              >
                <select
                  style={{
                    width: "100%",
                    padding: "6px 10px",
                    "border-radius": "6px",
                    border: "1px solid var(--hair)",
                    background: "var(--surface)",
                    color: "var(--text-strong)",
                    "font-size": "13px",
                    "font-family": "var(--font-ui)",
                    cursor: "pointer",
                    appearance: "auto",
                    opacity: savingDesign() ? "0.6" : "1",
                  }}
                  disabled={savingDesign()}
                  value={settings()?.design_model ?? ""}
                  onChange={(e) => void onDesignModelChange(e.currentTarget.value)}
                >
                  <For each={designModelOptions()}>
                    {(opt) => <option value={opt.value}>{opt.label}</option>}
                  </For>
                </select>
              </Show>

              <Show when={designSaved()}>
                <div style={{ "font-size": "11px", color: "var(--syntax-success)", "margin-top": "4px" }}>
                  ✓ Saved — takes effect on next session
                </div>
              </Show>
            </Show>
          </div>
        </div>
      </div>

      {/* ── Model visibility list ─────────────────────────────────────────── */}
      <List
        search={{ placeholder: language.t("dialog.model.search.placeholder"), autofocus: true }}
        emptyMessage={language.t("dialog.model.empty")}
        key={(x) => `${x?.provider?.id}:${x?.id}`}
        items={models.list()}
        filterKeys={["provider.name", "name", "id"]}
        sortBy={(a, b) => a.name.localeCompare(b.name)}
        groupBy={(x) => x.provider.id}
        groupHeader={(group) => {
          const provider = group.items[0].provider
          return (
            <>
              <span>{provider.name}</span>
              <Tooltip
                placement="top"
                value={language.t("dialog.model.manage.provider.toggle", { provider: provider.name })}
              >
                <Switch
                  class="-mr-1"
                  checked={providerVisible(provider.id)}
                  onChange={(checked) => setProviderVisibility(provider.id, checked)}
                  hideLabel
                >
                  {provider.name}
                </Switch>
              </Tooltip>
            </>
          )
        }}
        sortGroupsBy={(a, b) => {
          const aRank = providerRank(a.items[0].provider.id)
          const bRank = providerRank(b.items[0].provider.id)
          const aPopular = aRank >= 0
          const bPopular = bRank >= 0
          if (aPopular && !bPopular) return -1
          if (!aPopular && bPopular) return 1
          return aRank - bRank
        }}
        onSelect={(x) => {
          if (!x) return
          const key = { modelID: x.id, providerID: x.provider.id }
          models.setVisibility(key, !models.visible(key))
        }}
      >
        {(i) => (
          <div class="w-full flex items-center justify-between gap-x-3">
            <span>{i.name}</span>
            <div onClick={(e) => e.stopPropagation()}>
              <Switch
                checked={!!models.visible({ modelID: i.id, providerID: i.provider.id })}
                onChange={(checked) => {
                  models.setVisibility({ modelID: i.id, providerID: i.provider.id }, checked)
                }}
              />
            </div>
          </div>
        )}
      </List>
    </Dialog>
  )
}
