import { Auth } from "@/auth"
import { ProviderAuth } from "@/provider/auth"
import { Config } from "@/config/config"
import { ModelsDev } from "@opencode-ai/core/models-dev"
import { Provider } from "@/provider/provider"
import { ProviderID } from "@/provider/schema"
import { resolveForgeCustomProviders } from "@/forge/custom-providers"
import { mapValues } from "remeda"
import { Effect, Schema } from "effect"
import { HttpServerRequest, HttpServerResponse } from "effect/unstable/http"
import { HttpApiBuilder } from "effect/unstable/httpapi"
import { InstanceHttpApi } from "../api"
import { ProviderAuthApiError } from "../groups/provider"

function mapProviderAuthError<A, R>(self: Effect.Effect<A, ProviderAuth.Error, R>) {
  return self.pipe(
    Effect.mapError((error) => {
      if (error instanceof ProviderAuth.OauthMissing) {
        return new ProviderAuthApiError({ name: error._tag, data: { providerID: error.providerID } })
      }
      if (error instanceof ProviderAuth.OauthCodeMissing) {
        return new ProviderAuthApiError({ name: error._tag, data: { providerID: error.providerID } })
      }
      if (error instanceof ProviderAuth.OauthCallbackFailed) {
        return new ProviderAuthApiError({ name: error._tag, data: {} })
      }
      if (error instanceof ProviderAuth.ValidationFailed) {
        return new ProviderAuthApiError({ name: error._tag, data: { field: error.field, message: error.message } })
      }
      return new ProviderAuthApiError({ name: "BadRequest", data: {} })
    }),
  )
}

export const providerHandlers = HttpApiBuilder.group(InstanceHttpApi, "provider", (handlers) =>
  Effect.gen(function* () {
    const cfg = yield* Config.Service
    const provider = yield* Provider.Service
    const svc = yield* ProviderAuth.Service

    const list = Effect.fn("ProviderHttpApi.list")(function* () {
      const config = yield* cfg.get()
      const all = yield* ModelsDev.Service.use((s) => s.get())
      const disabled = new Set(config.disabled_providers ?? [])
      const enabled = config.enabled_providers ? new Set(config.enabled_providers) : undefined
      const filtered: Record<string, (typeof all)[string]> = {}
      for (const [key, value] of Object.entries(all)) {
        if ((enabled ? enabled.has(key) : true) && !disabled.has(key)) filtered[key] = value
      }
      const registered = yield* provider.list()

      // Forge per-user custom providers, resolved from forge-server's
      // user_settings.custom_providers + user_provider_keys. Returns the
      // empty set for non-Forge requests, so the unconditional merge below
      // is a no-op outside Forge mode and adds zero overhead.
      //
      // Why we merge into `registered` (config layer) and NOT into the
      // ModelsDev catalog: custom providers are user-supplied configurations
      // with full options/headers — they belong in the same merge bucket as
      // the on-disk opencode.json providers. The mapValues→Object.assign
      // chain below already gives `registered` precedence over ModelsDev
      // entries, which is correct for our user-customs too.
      const forgeCustom = yield* resolveForgeCustomProviders()
      const forgeRegistered: Record<string, Provider.Info> = {}
      for (const [pid, cfgEntry] of Object.entries(forgeCustom.providers)) {
        // Don't overwrite a real opencode-registered provider with a user
        // custom of the same id. Backend already rejects platform IDs, so
        // this is purely belt-and-braces.
        if (pid in registered) continue
        forgeRegistered[pid] = Provider.fromForgeCustomProvider(
          pid,
          cfgEntry,
          forgeCustom.keyed.has(pid),
        )
      }

      const providers = Object.assign(
        mapValues(filtered, (item) => Provider.fromModelsDevProvider(item)),
        registered,
        forgeRegistered,
      )

      // In Forge BYOK mode, "connected" must mean "this user has a key
      // resolved for this provider", not "this provider is defined in the
      // platform config". Without this filter, config/custom providers from
      // the shared forge-opencode-config/opencode.json appear connected for
      // every user even though their apiKey has been stripped by the
      // ForgeMode-aware provider build. User-customs carry a sentinel
      // `key` set by fromForgeCustomProvider when forgeCustom.keyed has
      // their id, so they classify correctly here.
      const forgeMode = yield* Auth.ForgeMode
      const allRegistered: Record<string, unknown> = { ...registered, ...forgeRegistered }
      const connectedIds = forgeMode
        ? Object.keys(allRegistered).filter((id) => {
            const p = allRegistered[id] as { key?: unknown; options?: { apiKey?: unknown } } | undefined
            return p?.key !== undefined || p?.options?.apiKey !== undefined
          })
        : Object.keys(allRegistered)

      return {
        all: Object.values(providers).map(Provider.toPublicInfo),
        default: Provider.defaultModelIDs(providers),
        connected: connectedIds,
      }
    })

    const auth = Effect.fn("ProviderHttpApi.auth")(function* () {
      return yield* svc.methods()
    })

    const authorize = Effect.fn("ProviderHttpApi.authorize")(function* (ctx: {
      params: { providerID: ProviderID }
      payload: ProviderAuth.AuthorizeInput
    }) {
      return yield* mapProviderAuthError(
        svc.authorize({
          providerID: ctx.params.providerID,
          method: ctx.payload.method,
          inputs: ctx.payload.inputs,
        }),
      )
    })

    const authorizeRaw = Effect.fn("ProviderHttpApi.authorizeRaw")(function* (ctx: {
      params: { providerID: ProviderID }
      request: HttpServerRequest.HttpServerRequest
    }) {
      const body = yield* Effect.orDie(ctx.request.text)
      const payload = yield* Schema.decodeUnknownEffect(Schema.fromJsonString(ProviderAuth.AuthorizeInput))(body).pipe(
        Effect.mapError(() => new ProviderAuthApiError({ name: "BadRequest", data: {} })),
      )
      // Match legacy route behavior: when authorize() resolves without a
      // result (e.g. no further redirect), serialize as JSON `null` instead
      // of an empty body so clients can `.json()` parse the response.
      const result = yield* authorize({ params: ctx.params, payload })
      return HttpServerResponse.jsonUnsafe(result ?? null)
    })

    const callback = Effect.fn("ProviderHttpApi.callback")(function* (ctx: {
      params: { providerID: ProviderID }
      payload: ProviderAuth.CallbackInput
    }) {
      yield* mapProviderAuthError(
        svc.callback({
          providerID: ctx.params.providerID,
          method: ctx.payload.method,
          code: ctx.payload.code,
        }),
      )
      return true
    })

    return handlers
      .handle("list", list)
      .handle("auth", auth)
      .handleRaw("authorize", authorizeRaw)
      .handle("callback", callback)
  }),
)
