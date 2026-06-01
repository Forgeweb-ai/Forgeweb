import { Match, Show, Switch, createMemo } from "solid-js"
import { Tooltip, type TooltipProps } from "@opencode-ai/ui/tooltip"
import { ProgressCircle } from "@opencode-ai/ui/progress-circle"
import { Button } from "@opencode-ai/ui/button"

import { useFile } from "@/context/file"
import { useLayout } from "@/context/layout"
import { useSync } from "@/context/sync"
import { useLanguage } from "@/context/language"
import { useProviders } from "@/hooks/use-providers"
import { getSessionContextMetrics } from "@/components/session/session-context-metrics"
import { useSessionLayout } from "@/pages/session/session-layout"
import { createSessionTabs } from "@/pages/session/helpers"

interface SessionContextUsageProps {
  /**
   * - `button`    — clickable progress circle that opens the context panel (default)
   * - `indicator` — read-only progress circle (used inside tab labels)
   * - `row`       — horizontal strip showing model · in/out tokens · cost · ctx %,
   *                 sits above the prompt input so the user always sees per-turn
   *                 token + dollar feedback without hovering a tooltip
   */
  variant?: "button" | "indicator" | "row"
  placement?: TooltipProps["placement"]
}

function openSessionContext(args: {
  view: ReturnType<ReturnType<typeof useLayout>["view"]>
  layout: ReturnType<typeof useLayout>
  tabs: ReturnType<ReturnType<typeof useLayout>["tabs"]>
}) {
  if (!args.view.reviewPanel.opened()) args.view.reviewPanel.open()
  if (args.layout.fileTree.opened() && args.layout.fileTree.tab() !== "all") args.layout.fileTree.setTab("all")
  void args.tabs.open("context")
  args.tabs.setActive("context")
}

export function SessionContextUsage(props: SessionContextUsageProps) {
  const sync = useSync()
  const file = useFile()
  const layout = useLayout()
  const language = useLanguage()
  const providers = useProviders()
  const { params, tabs, view } = useSessionLayout()

  const variant = createMemo(() => props.variant ?? "button")
  const tabState = createSessionTabs({
    tabs,
    pathFromTab: file.pathFromTab,
    normalizeTab: (tab) => (tab.startsWith("file://") ? file.tab(tab) : tab),
  })
  const messages = createMemo(() => (params.id ? (sync.data.message[params.id] ?? []) : []))

  const usd = createMemo(
    () =>
      new Intl.NumberFormat(language.intl(), {
        style: "currency",
        currency: "USD",
      }),
  )

  const metrics = createMemo(() => getSessionContextMetrics(messages(), [...providers.all().values()]))
  const context = createMemo(() => metrics().context)
  const cost = createMemo(() => {
    return usd().format(metrics().totalCost)
  })

  const openContext = () => {
    if (!params.id) return

    if (tabState.activeTab() === "context") {
      tabs().close("context")
      return
    }
    openSessionContext({
      view: view(),
      layout,
      tabs: tabs(),
    })
  }

  const circle = () => (
    <div class="flex items-center justify-center">
      <ProgressCircle size={16} strokeWidth={2} percentage={context()?.usage ?? 0} />
    </div>
  )

  const tooltipValue = () => (
    <div>
      <Show when={context()}>
        {(ctx) => (
          <>
            <div class="flex items-center gap-2">
              <span class="text-text-invert-strong">{ctx().total.toLocaleString(language.intl())}</span>
              <span class="text-text-invert-base">{language.t("context.usage.tokens")}</span>
            </div>
            <div class="flex items-center gap-2">
              <span class="text-text-invert-strong">{ctx().usage ?? 0}%</span>
              <span class="text-text-invert-base">{language.t("context.usage.usage")}</span>
            </div>
          </>
        )}
      </Show>
      <div class="flex items-center gap-2">
        <span class="text-text-invert-strong">{cost()}</span>
        <span class="text-text-invert-base">{language.t("context.usage.cost")}</span>
      </div>
    </div>
  )

  // ── `row` variant ──────────────────────────────────────────────────────────
  // Horizontal strip. Always-visible per-turn telemetry: model · in / out /
  // cache tokens · session cost · context%. Data source is the same SDK
  // metadata the indicator already uses (msg.tokens, msg.cost) — no new
  // network calls, no FE-side cost computation. If a later turn has cost
  // newer than opencode's number (we change the rate card before opencode
  // does), we'll wire this row to fetch from forge-llm-proxy directly.
  const formatTokens = (n: number) => n.toLocaleString(language.intl())
  const row = () => (
    <div
      data-component="session-context-usage-row"
      class="flex items-center flex-wrap gap-x-3 gap-y-1 text-xs text-text-weak px-3 py-1.5 border-t border-border-weak-base bg-background-base/40"
    >
      <Show
        when={context()}
        fallback={
          <span class="text-text-weak">
            {language.t("context.usage.view")}
          </span>
        }
      >
        {(ctx) => (
          <>
            <span class="font-medium text-text-base">
              {ctx().providerLabel} {ctx().modelLabel}
            </span>
            <span aria-hidden="true">·</span>
            <span>
              {language.t("context.usage.tokens")}: <span class="text-text-base">{formatTokens(ctx().input)}</span> in
              {" / "}
              <span class="text-text-base">{formatTokens(ctx().output)}</span> out
            </span>
            <Show when={ctx().cacheRead > 0 || ctx().cacheWrite > 0}>
              <span>
                cache: <span class="text-text-base">{formatTokens(ctx().cacheRead)}</span> r
                {" / "}
                <span class="text-text-base">{formatTokens(ctx().cacheWrite)}</span> w
              </span>
            </Show>
            <span aria-hidden="true">·</span>
            <span class="font-medium text-text-base">{cost()}</span>
            <Show when={ctx().usage !== null}>
              <span aria-hidden="true">·</span>
              <span>{ctx().usage}% {language.t("context.usage.usage")}</span>
            </Show>
          </>
        )}
      </Show>
    </div>
  )

  return (
    <Show when={params.id}>
      <Switch>
        <Match when={variant() === "row"}>{row()}</Match>
        <Match when={variant() === "indicator"}>
          <Tooltip value={tooltipValue()} placement={props.placement ?? "top"}>
            {circle()}
          </Tooltip>
        </Match>
        <Match when={true}>
          <Tooltip value={tooltipValue()} placement={props.placement ?? "top"}>
            <Button
              type="button"
              variant="ghost"
              class="size-6"
              onClick={openContext}
              aria-label={language.t("context.usage.view")}
            >
              {circle()}
            </Button>
          </Tooltip>
        </Match>
      </Switch>
    </Show>
  )
}
