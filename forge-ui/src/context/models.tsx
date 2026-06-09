import { createEffect, createMemo, createResource, createSignal, onCleanup } from "solid-js"
import { createStore } from "solid-js/store"
import { DateTime } from "luxon"
import { filter, firstBy, flat, groupBy, mapValues, pipe, uniqueBy, values } from "remeda"
import { createSimpleContext } from "@opencode-ai/ui/context"
import { useProviders } from "@/hooks/use-providers"
import { useForgeApiOptional } from "@/context/forge-api"
import { Persist, persisted } from "@/utils/persist"

export type ModelKey = { providerID: string; modelID: string }

// `recent` and `variant` stay device-local — they're per-browser ergonomics
// (most-recently-used short-list, variant tweaks) with no value in cross-
// device sync. The model VISIBILITY allowlist used to live here too (`user`
// field) but moved to user_settings.enabled_models on the BE so toggles
// follow the user across browsers, devices, and survive localStorage quota
// sweeps. The legacy `user[]` entries from older clients are migrated up
// one-time on mount (see `legacyMigrationDone` below).
type Store = {
  recent: ModelKey[]
  variant?: Record<string, string | undefined>
}

// Legacy shape kept so we can read pre-migration localStorage one last time
// and forward the user's existing toggles to the DB.
type LegacyVisibility = "show" | "hide"
type LegacyUserEntry = ModelKey & { visibility: LegacyVisibility; favorite?: boolean }
type LegacyStore = {
  user?: LegacyUserEntry[]
  recent?: ModelKey[]
  variant?: Record<string, string | undefined>
}

const RECENT_LIMIT = 5

// Coalesce bulk visibility toggles (e.g. the provider-level switch in
// dialog-manage-models.tsx flips N models in one tick) into a single PATCH.
// 250ms is the sweet spot from observation: long enough to batch a
// provider-toggle that fires N synchronous setVisibility calls, short
// enough that user perceives "save" as instant. Without this, toggling
// a 20-model provider on would fire 20 PATCHes back-to-back — wasted
// bandwidth at scale (CLAUDE.md §2).
const SAVE_DEBOUNCE_MS = 250

function modelKey(model: ModelKey) {
  return `${model.providerID}:${model.modelID}`
}

function allowlistKey(model: ModelKey) {
  // The BE stores entries as "<providerID>/<modelID>" (mirrors how every
  // other model id is spelled across user_settings — primary_model,
  // design_model, image_model). The internal `modelKey()` uses ":" because
  // that's what the legacy Store used as a Map key. Keep them distinct so
  // grep finds the right call site.
  return `${model.providerID}/${model.modelID}`
}

function parseAllowlistKey(key: string): ModelKey | undefined {
  const slash = key.indexOf("/")
  if (slash <= 0 || slash === key.length - 1) return undefined
  return { providerID: key.slice(0, slash), modelID: key.slice(slash + 1) }
}

export const { use: useModels, provider: ModelsProvider } = createSimpleContext({
  name: "Models",
  init: () => {
    const providers = useProviders()
    const forge     = useForgeApiOptional()

    const [store, setStore, _, localReady] = persisted(
      Persist.global("model", ["model.v1"]),
      createStore<Store>({
        recent: [],
        variant: {},
      }),
    )

    // ── Visibility allowlist (DB-backed) ─────────────────────────────────────
    // Source of truth lives in user_settings.enabled_models. We mirror it
    // into a local signal so visibility checks are O(1) synchronous (every
    // model in the picker calls visible() — async on the hot path would
    // jank the list).
    //
    // The local signal is the optimistic copy: setVisibility writes it
    // immediately so the UI updates without waiting for the network, then a
    // debounced PATCH ships the new full list to the BE. If the PATCH fails
    // we don't revert (the local copy stays; next page-load reconciles from
    // BE) — toggling models is low-stakes and the alternative is flicker.
    const [allowlist, setAllowlist] = createSignal<Set<string>>(new Set())
    const [allowlistReady, setAllowlistReady] = createSignal(false)

    // Read the saved allowlist once on mount. We don't refetch on every
    // render — `getSettings()` is also called by other consumers (local.tsx,
    // dialog-manage-models.tsx) and we don't want to thrash the BE. If the
    // user changes models in another tab and wants this tab to see it,
    // they'll get the update on next reload.
    const [serverSettings] = createResource(
      () => (forge ? "load" : null),
      async () => {
        if (!forge) return null
        try {
          return await forge.getSettings()
        } catch (e) {
          // Don't block the picker on a settings load failure — fall back
          // to whatever's in the local signal (empty = default policy).
          console.warn("[models] failed to load settings; using local fallback:", e)
          return null
        }
      },
    )

    // One-time legacy migration: forward localStorage `user[]` toggles to
    // the DB so existing users don't lose their selections during the
    // storage cut-over. Runs once when both stores are ready, only if the
    // BE allowlist is empty (otherwise the DB already wins).
    let legacyMigrationDone = false
    createEffect(() => {
      if (legacyMigrationDone) return
      if (!localReady()) return
      if (serverSettings.loading) return
      const server = serverSettings()
      if (!server) {
        // No forge-api in this tree (tests, or pre-auth shell) — nothing to
        // migrate to. Mark ready so the picker doesn't hang.
        setAllowlistReady(true)
        legacyMigrationDone = true
        return
      }
      const fromServer = Array.isArray(server.enabled_models) ? server.enabled_models : []
      if (fromServer.length > 0) {
        // BE already has the user's choices — trust it, ignore legacy local.
        setAllowlist(new Set(fromServer))
        setAllowlistReady(true)
        legacyMigrationDone = true
        return
      }
      // BE is empty. Look for legacy localStorage entries to migrate.
      const legacyUser = (store as unknown as LegacyStore).user
      if (!legacyUser || legacyUser.length === 0) {
        setAllowlistReady(true)
        legacyMigrationDone = true
        return
      }
      const migrated = legacyUser
        .filter((entry) => entry.visibility === "show")
        .map((entry) => allowlistKey(entry))
      const unique = Array.from(new Set(migrated))
      setAllowlist(new Set(unique))
      setAllowlistReady(true)
      legacyMigrationDone = true
      if (unique.length > 0 && forge) {
        // Fire-and-forget — the local signal is already correct; pushing to
        // BE just makes it durable. If it fails the user keeps their state
        // for this session and we'll retry next visibility toggle.
        forge
          .updateSettings({ enabled_models: unique })
          .catch((e) => console.warn("[models] legacy migration PATCH failed:", e))
      }
      // Strip the legacy field from local storage so we don't migrate again
      // (and don't keep paying for stale bytes in every persisted snapshot).
      try {
        ;(setStore as unknown as (key: "user", value: undefined) => void)("user", undefined)
      } catch {
        // Old shape may not be writable through the new typed store — fine,
        // setting the migration flag above is enough to prevent re-runs.
      }
    })

    // Debounced PATCH. We send the entire current allowlist (whole-list
    // replace mirrors the BE schema and is simpler than a diff API).
    let saveTimer: ReturnType<typeof setTimeout> | undefined
    let pendingSnapshot: string[] | undefined
    function scheduleSave(next: string[]) {
      if (!forge) return
      pendingSnapshot = next
      if (saveTimer) clearTimeout(saveTimer)
      saveTimer = setTimeout(() => {
        const payload = pendingSnapshot ?? []
        pendingSnapshot = undefined
        saveTimer = undefined
        forge
          .updateSettings({ enabled_models: payload })
          .catch((e) => console.warn("[models] save enabled_models failed:", e))
      }, SAVE_DEBOUNCE_MS)
    }
    onCleanup(() => {
      if (saveTimer) {
        // Flush pending PATCH on teardown so a fast close doesn't drop the
        // user's most recent toggle.
        clearTimeout(saveTimer)
        if (forge && pendingSnapshot) {
          void forge.updateSettings({ enabled_models: pendingSnapshot }).catch(() => undefined)
        }
        saveTimer = undefined
        pendingSnapshot = undefined
      }
    })

    // ── Model catalog (unchanged from previous implementation) ───────────────
    const available = createMemo(() =>
      providers.connected().flatMap((p) =>
        Object.values(p.models).map((m) => ({
          ...m,
          provider: p,
        })),
      ),
    )

    const release = createMemo(
      () =>
        new Map(
          available().map((model) => {
            const parsed = DateTime.fromISO(model.release_date)
            return [modelKey({ providerID: model.provider.id, modelID: model.id }), parsed] as const
          }),
        ),
    )

    const latest = createMemo(() =>
      pipe(
        available(),
        filter(
          (x) =>
            Math.abs(
              (release().get(modelKey({ providerID: x.provider.id, modelID: x.id })) ?? DateTime.invalid("invalid"))
                .diffNow()
                .as("months"),
            ) < 6,
        ),
        groupBy((x) => x.provider.id),
        mapValues((models) =>
          pipe(
            models,
            groupBy((x) => x.family),
            values(),
            (groups) =>
              groups.flatMap((g) => {
                const first = firstBy(g, [(x) => x.release_date, "desc"])
                return first ? [{ modelID: first.id, providerID: first.provider.id }] : []
              }),
          ),
        ),
        values(),
        flat(),
      ),
    )

    const latestSet = createMemo(() => new Set(latest().map((x) => modelKey(x))))

    const list = createMemo(() =>
      available().map((m) => ({
        ...m,
        name: m.name.replace("(latest)", "").trim(),
        latest: m.name.includes("(latest)"),
      })),
    )

    const find = (key: ModelKey) => list().find((m) => m.id === key.modelID && m.provider.id === key.providerID)

    // ── Visibility resolver ──────────────────────────────────────────────────
    // Strict-allowlist semantics when the user has made explicit choices,
    // sensible defaults otherwise.
    const visible = (model: ModelKey) => {
      const set = allowlist()
      if (set.size > 0) {
        // User has an active allowlist — only listed models are visible.
        // Unknown entries (referenced provider currently disconnected,
        // catalog moved) are tolerated: they sit in the set but contribute
        // nothing until the catalog re-includes them.
        return set.has(allowlistKey(model))
      }
      // No allowlist set → default policy: opencode-zen free models are
      // visible (zero-cost out-of-box for fresh users), all paid models
      // hidden until the user explicitly enables them in Manage Models.
      // Matches the pre-DB behaviour for any user who hasn't toggled yet.
      const found = available().find(
        (m) => m.id === model.modelID && m.provider.id === model.providerID,
      )
      if (model.providerID === "opencode") {
        const cost = (found as { cost?: { input?: number } } | undefined)?.cost
        if (!cost || !cost.input) return true
        return false
      }
      return false
    }

    const setVisibility = (model: ModelKey, state: boolean) => {
      const key = allowlistKey(model)
      const current = allowlist()
      // Seed the allowlist on first explicit interaction. Without seeding,
      // a brand-new user who flips ONE paid model on would end up with a
      // 1-entry allowlist that hides every previously-default-visible
      // opencode-zen free model — a surprising regression. By seeding with
      // all currently-visible models the first time the user touches a
      // toggle, the default policy "freezes" into an explicit allowlist
      // that the user then mutates from.
      const next = current.size === 0
        ? new Set<string>(
            available()
              .filter((m) => visible({ modelID: m.id, providerID: m.provider.id }))
              .map((m) => allowlistKey({ modelID: m.id, providerID: m.provider.id })),
          )
        : new Set(current)
      if (state) next.add(key)
      else next.delete(key)
      if (next.size === current.size && (state ? current.has(key) : !current.has(key))) {
        // No-op toggle — don't trigger a PATCH.
        return
      }
      setAllowlist(next)
      scheduleSave(Array.from(next))
    }

    // Reference `latestSet` so the memo isn't tree-shaken — consumers may
    // call `.latest` on returned models for the "latest" tag rendering.
    void latestSet

    const push = (model: ModelKey) => {
      const uniq = uniqueBy([model, ...store.recent], (x) => `${x.providerID}:${x.modelID}`)
      if (uniq.length > RECENT_LIMIT) uniq.pop()
      setStore("recent", uniq)
    }

    const variantKey = (model: ModelKey) => `${model.providerID}/${model.modelID}`
    const getVariant = (model: ModelKey) => store.variant?.[variantKey(model)]

    const setVariant = (model: ModelKey, value: string | undefined) => {
      const key = variantKey(model)
      if (!store.variant) {
        setStore("variant", { [key]: value })
        return
      }
      setStore("variant", key, value)
    }

    const ready = () => localReady() && allowlistReady()

    return {
      ready,
      list,
      find,
      visible,
      setVisibility,
      recent: {
        list: createMemo(() => store.recent),
        push,
      },
      variant: {
        get: getVariant,
        set: setVariant,
      },
    }
  },
})
