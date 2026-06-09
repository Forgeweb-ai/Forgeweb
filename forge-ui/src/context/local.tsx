import { createSimpleContext } from "@opencode-ai/ui/context"
import { base64Encode } from "@opencode-ai/core/util/encode"
import { useParams } from "@solidjs/router"
import { batch, createEffect, createMemo, createSignal } from "solid-js"
import { createStore } from "solid-js/store"
import { useModels } from "@/context/models"
import { useForgeApiOptional } from "@/context/forge-api"
import { useProviders } from "@/hooks/use-providers"
import { Persist, persisted } from "@/utils/persist"
import { cycleModelVariant, getConfiguredAgentVariant, resolveModelVariant } from "./model-variant"
import { useSDK } from "./sdk"
import { useSync } from "./sync"

export type ModelKey = { providerID: string; modelID: string; variant?: string }

type State = {
  agent?: string
  model?: ModelKey
  variant?: string | null
}

type Saved = {
  session: Record<string, State | undefined>
}

const WORKSPACE_KEY = "__workspace__"
const handoff = new Map<string, State>()

const handoffKey = (dir: string, id: string) => `${dir}\n${id}`

const migrate = (value: unknown) => {
  if (!value || typeof value !== "object") return { session: {} }

  const item = value as {
    session?: Record<string, State | undefined>
    pick?: Record<string, State | undefined>
  }

  if (item.session && typeof item.session === "object") return { session: item.session }
  if (!item.pick || typeof item.pick !== "object") return { session: {} }

  return {
    session: Object.fromEntries(Object.entries(item.pick).filter(([key]) => key !== WORKSPACE_KEY)),
  }
}

const clone = (value: State | undefined) => {
  if (!value) return
  return {
    ...value,
    model: value.model ? { ...value.model } : undefined,
  } satisfies State
}

export const { use: useLocal, provider: LocalProvider } = createSimpleContext({
  name: "Local",
  init: () => {
    const params = useParams()
    const sdk = useSDK()
    const sync = useSync()
    const providers = useProviders()
    const models = useModels()
    // Optional — non-Forge dev still uses this provider, so no throw on missing.
    const forge = useForgeApiOptional()

    // DB-backed primary model. On mount we GET /api/user/settings.primary_model
    // and use it as the highest-priority fallback for a session with no
    // explicit pick. On user-driven changes (model.set / cycle / agent.set)
    // we fire-and-forget PATCH so the choice follows the user across devices.
    //
    // Skipped entirely if useForgeApiOptional() returns undefined (stock
    // opencode mode). The dedup signal `lastPatchedKey` ensures we never
    // re-PATCH the same value (cheap when users click around).
    const [serverPrimaryModel, setServerPrimaryModel] = createSignal<ModelKey | undefined>(undefined)
    let lastPatchedKey: string | undefined
    if (forge) {
      void forge.getSettings().then((s) => {
        const raw = s.primary_model
        if (!raw || !raw.includes("/")) return
        const [providerID, modelID] = raw.split("/", 2)
        if (!providerID || !modelID) return
        setServerPrimaryModel({ providerID, modelID })
        lastPatchedKey = `${providerID}/${modelID}`
      }).catch(() => { /* offline / unauthed — fall back to local defaults */ })
    }

    const patchPrimaryModel = (item: ModelKey | undefined) => {
      if (!forge || !item) return
      const key = `${item.providerID}/${item.modelID}`
      if (key === lastPatchedKey) return
      lastPatchedKey = key
      void forge.updateSettings({ primary_model: key }).catch(() => {
        // Network blip — leave lastPatchedKey set so we don't retry every
        // click; the next genuine change will re-attempt.
      })
    }

    const id = createMemo(() => params.id || undefined)
    const list = createMemo(() => sync.data.agent.filter((item) => item.mode !== "subagent" && !item.hidden))
    const connected = createMemo(() => new Set(providers.connected().map((item) => item.id)))

    const [saved, setSaved] = persisted(
      {
        ...Persist.workspace(sdk.directory, "model-selection", ["model-selection.v1"]),
        migrate,
      },
      createStore<Saved>({
        session: {},
      }),
    )

    const [store, setStore] = createStore<{
      current?: string
      draft?: State
      last?: {
        type: "agent" | "model" | "variant"
        agent?: string
        model?: ModelKey | null
        variant?: string | null
      }
    }>({
      current: list()[0]?.name,
      draft: undefined,
      last: undefined,
    })

    const validModel = (model: ModelKey) => {
      const provider = providers.all().get(model.providerID)
      return !!provider?.models[model.modelID] && connected().has(model.providerID)
    }

    const firstModel = (...items: Array<() => ModelKey | undefined>) => {
      for (const item of items) {
        const model = item()
        if (!model) continue
        if (validModel(model)) return model
      }
    }

    const pickAgent = (name: string | undefined) => {
      const items = list()
      if (items.length === 0) return
      return items.find((item) => item.name === name) ?? items[0]
    }

    createEffect(() => {
      const items = list()
      if (items.length === 0) {
        if (store.current !== undefined) setStore("current", undefined)
        return
      }
      if (items.some((item) => item.name === store.current)) return
      setStore("current", items[0]?.name)
    })

    const scope = createMemo<State | undefined>(() => {
      const session = id()
      if (!session) return store.draft
      return saved.session[session] ?? handoff.get(handoffKey(sdk.directory, session))
    })

    createEffect(() => {
      const session = id()
      if (!session) return

      const key = handoffKey(sdk.directory, session)
      const next = handoff.get(key)
      if (!next) return
      if (saved.session[session] !== undefined) {
        handoff.delete(key)
        return
      }

      setSaved("session", session, clone(next))
      handoff.delete(key)
    })

    const configuredModel = () => {
      if (!sync.data.config.model) return
      const [providerID, modelID] = sync.data.config.model.split("/")
      const model = { providerID, modelID }
      if (validModel(model)) return model
    }

    const recentModel = () => {
      for (const item of models.recent.list()) {
        if (validModel(item)) return item
      }
    }

    const serverModel = () => {
      // DB-backed primary_model wins over any heuristic so the user's choice
      // follows them across devices. Resolved only if the provider is
      // connected and the model exists in the registry.
      const m = serverPrimaryModel()
      if (m && validModel(m)) return m
    }

    const defaultModel = () => {
      // Forge default policy: prefer opencode-zen's free DeepSeek so a fresh
      // user lands on a usable model without having to add a provider key.
      // Match by name (case-insensitive, contains "deepseek") because the
      // exact zen slug ("deepseek-v4-flash-free" or similar) is registry-side
      // and can change without notice.
      const opencode = providers.all().get("opencode")
      if (opencode) {
        const deepseek = Object.values(opencode.models).find((m) => {
          const name = (m as { name?: string }).name?.toLowerCase() ?? ""
          return name.includes("deepseek")
        })
        if (deepseek) {
          const model = { providerID: "opencode", modelID: (deepseek as { id: string }).id }
          if (validModel(model)) return model
        }
      }

      const defaults = providers.default()
      for (const provider of providers.connected()) {
        const configured = defaults[provider.id]
        if (configured) {
          const model = { providerID: provider.id, modelID: configured }
          if (validModel(model)) return model
        }

        const first = Object.values(provider.models)[0]
        if (!first) continue
        const model = { providerID: provider.id, modelID: first.id }
        if (validModel(model)) return model
      }
    }

    // Fallback order:
    //   1. serverModel    — DB-backed primary_model (cross-device user choice)
    //   2. recentModel    — last-used model on this device
    //   3. configuredModel — platform default in opencode.json
    //   4. defaultModel    — opencode-zen free DeepSeek, then any connected provider
    // serverModel jumps ahead of recent so a user signed-in on a new device
    // immediately lands on their saved model, not whatever localStorage
    // accidentally cached on first paint.
    const fallback = createMemo<ModelKey | undefined>(() => serverModel() ?? recentModel() ?? configuredModel() ?? defaultModel())

    const agent = {
      list,
      current() {
        return pickAgent(scope()?.agent ?? store.current)
      },
      set(name: string | undefined) {
        const item = pickAgent(name)
        if (!item) {
          setStore("current", undefined)
          return
        }

        batch(() => {
          setStore("current", item.name)
          setStore("last", {
            type: "agent",
            agent: item.name,
            model: item.model,
            variant: item.variant ?? null,
          })
          const prev = scope()
          const next = {
            agent: item.name,
            model: item.model ?? prev?.model,
            variant: item.variant ?? prev?.variant,
          } satisfies State
          const session = id()
          if (session) {
            setSaved("session", session, next)
            return
          }
          setStore("draft", next)
        })
      },
      move(direction: 1 | -1) {
        const items = list()
        if (items.length === 0) {
          setStore("current", undefined)
          return
        }

        let next = items.findIndex((item) => item.name === agent.current()?.name) + direction
        if (next < 0) next = items.length - 1
        if (next >= items.length) next = 0
        const item = items[next]
        if (!item) return
        agent.set(item.name)
      },
    }

    const current = () => {
      const item = firstModel(
        () => scope()?.model,
        () => agent.current()?.model,
        fallback,
      )
      if (!item) return
      return models.find(item)
    }

    const configured = () => {
      const item = agent.current()
      const model = current()
      if (!item || !model) return
      return getConfiguredAgentVariant({
        agent: { model: item.model, variant: item.variant },
        model: { providerID: model.provider.id, modelID: model.id, variants: model.variants },
      })
    }

    const selected = () => scope()?.variant

    const snapshot = () => {
      const model = current()
      return {
        agent: agent.current()?.name,
        model: model ? { providerID: model.provider.id, modelID: model.id } : undefined,
        variant: selected(),
      } satisfies State
    }

    const write = (next: Partial<State>) => {
      const state = {
        ...(scope() ?? { agent: agent.current()?.name }),
        ...next,
      } satisfies State

      const session = id()
      if (session) {
        setSaved("session", session, state)
        return
      }
      setStore("draft", state)
    }

    const recent = createMemo(() => models.recent.list().map(models.find).filter(Boolean))

    const model = {
      ready: models.ready,
      current,
      recent,
      list: models.list,
      cycle(direction: 1 | -1) {
        const items = recent()
        const item = current()
        if (!item) return

        const index = items.findIndex((entry) => entry?.provider.id === item.provider.id && entry?.id === item.id)
        if (index === -1) return

        let next = index + direction
        if (next < 0) next = items.length - 1
        if (next >= items.length) next = 0

        const entry = items[next]
        if (!entry) return
        model.set({ providerID: entry.provider.id, modelID: entry.id })
      },
      set(item: ModelKey | undefined, options?: { recent?: boolean }) {
        batch(() => {
          setStore("last", {
            type: "model",
            agent: agent.current()?.name,
            model: item ?? null,
            variant: selected(),
          })
          write({ model: item })
          if (!item) return
          models.setVisibility(item, true)
          if (!options?.recent) return
          models.recent.push(item)
        })
        // Persist user's choice to forge-server so it follows them across
        // devices. Fire-and-forget — no UI block on network. Dedup inside
        // patchPrimaryModel ensures repeat clicks don't spam the endpoint.
        patchPrimaryModel(item)
      },
      visible(item: ModelKey) {
        return models.visible(item)
      },
      setVisibility(item: ModelKey, visible: boolean) {
        models.setVisibility(item, visible)
      },
      variant: {
        configured,
        selected,
        current() {
          const resolved = resolveModelVariant({
            variants: this.list(),
            selected: this.selected(),
            configured: this.configured(),
          })
          if (resolved) return resolved
          const model = current()
          if (!model) return
          const saved = models.variant.get({ providerID: model.provider.id, modelID: model.id })
          if (saved && this.list().includes(saved)) return saved
        },
        list() {
          const item = current()
          if (!item?.variants) return []
          return Object.keys(item.variants)
        },
        set(value: string | undefined) {
          batch(() => {
            const model = current()
            setStore("last", {
              type: "variant",
              agent: agent.current()?.name,
              model: model ? { providerID: model.provider.id, modelID: model.id } : null,
              variant: value ?? null,
            })
            write({ variant: value ?? null })
            if (model) {
              models.variant.set({ providerID: model.provider.id, modelID: model.id }, value ?? undefined)
            }
          })
        },
        cycle() {
          const items = this.list()
          if (items.length === 0) return
          this.set(
            cycleModelVariant({
              variants: items,
              selected: this.selected(),
              configured: this.configured(),
            }),
          )
        },
      },
    }

    const result = {
      slug: createMemo(() => base64Encode(sdk.directory)),
      model,
      agent,
      session: {
        reset() {
          setStore("draft", undefined)
        },
        promote(dir: string, session: string) {
          const next = clone(snapshot())
          if (!next) return

          if (dir === sdk.directory) {
            setSaved("session", session, next)
            setStore("draft", undefined)
            return
          }

          handoff.set(handoffKey(dir, session), next)
          setStore("draft", undefined)
        },
        restore(msg: { sessionID: string; agent: string; model: ModelKey }) {
          const session = id()
          if (!session) return
          if (msg.sessionID !== session) return
          if (saved.session[session] !== undefined) return
          if (handoff.has(handoffKey(sdk.directory, session))) return

          setSaved("session", session, {
            agent: msg.agent,
            model: msg.model,
            variant: msg.model?.variant ?? null,
          })
        },
      },
    }
    return result
  },
})
