import path from "path"
import { Effect, Layer, Record, Result, Schema, Context } from "effect"
import { NonNegativeInt } from "@opencode-ai/core/schema"
import { Global } from "@opencode-ai/core/global"
import { AppFileSystem } from "@opencode-ai/core/filesystem"

export const OAUTH_DUMMY_KEY = "opencode-oauth-dummy-key"

const file = path.join(Global.Path.data, "auth.json")

/**
 * Request-scoped key override. Populated by the forge-auth router middleware
 * from the `X-Forge-Auth` header so the shared opencode process can serve many
 * users without leaking keys through the on-disk auth.json. When set, entries
 * here win over both auth.json and OPENCODE_AUTH_CONTENT for the duration of
 * the current Effect (i.e. one HTTP request).
 *
 * Default is an empty record so behaviour outside HTTP request scope (CLI,
 * tests, non-Forge deployments) is unchanged.
 */
export const Override = Context.Reference<Record<string, unknown>>(
  "@opencode/Auth/Override",
  { defaultValue: () => ({}) },
)

/**
 * Signals that this request originated from Forge's per-user proxy. When true,
 * provider key resolution becomes per-user only: process env vars and the
 * `apiKey` field on config providers are ignored, so brand-new users never
 * inherit platform-level credentials. The proxy sets the `X-Forge-Auth`
 * header on every Forge call (even with an empty key map), which the
 * forge-auth middleware uses to flip this flag.
 */
export const ForgeMode = Context.Reference<boolean>(
  "@opencode/Auth/ForgeMode",
  { defaultValue: () => false },
)

const fail = (message: string) => (cause: unknown) => new AuthError({ message, cause })

export class Oauth extends Schema.Class<Oauth>("OAuth")({
  type: Schema.Literal("oauth"),
  refresh: Schema.String,
  access: Schema.String,
  expires: NonNegativeInt,
  accountId: Schema.optional(Schema.String),
  enterpriseUrl: Schema.optional(Schema.String),
}) {}

export class Api extends Schema.Class<Api>("ApiAuth")({
  type: Schema.Literal("api"),
  key: Schema.String,
  metadata: Schema.optional(Schema.Record(Schema.String, Schema.String)),
}) {}

export class WellKnown extends Schema.Class<WellKnown>("WellKnownAuth")({
  type: Schema.Literal("wellknown"),
  key: Schema.String,
  token: Schema.String,
}) {}

export const Info = Schema.Union([Oauth, Api, WellKnown]).annotate({ discriminator: "type", identifier: "Auth" })
export type Info = Schema.Schema.Type<typeof Info>

export class AuthError extends Schema.TaggedErrorClass<AuthError>()("AuthError", {
  message: Schema.String,
  cause: Schema.optional(Schema.Defect),
}) {}

export interface Interface {
  readonly get: (providerID: string) => Effect.Effect<Info | undefined, AuthError>
  readonly all: () => Effect.Effect<Record<string, Info>, AuthError>
  readonly set: (key: string, info: Info) => Effect.Effect<void, AuthError>
  readonly remove: (key: string) => Effect.Effect<void, AuthError>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/Auth") {}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const fsys = yield* AppFileSystem.Service
    const decode = Schema.decodeUnknownOption(Info)

    const all = Effect.fn("Auth.all")(function* () {
      // Request-scoped override + ForgeMode flag from the forge-auth middleware.
      // In ForgeMode, the override is the *only* source of truth — disk
      // auth.json and OPENCODE_AUTH_CONTENT are ignored even when override is
      // empty. Otherwise an old auth.json entry from a previous tenant would
      // still be reported as a connected provider for a brand-new Forge user.
      const override = yield* Override
      const forgeMode = yield* ForgeMode
      const overrideDecoded = Record.filterMap(override, (value) =>
        Result.fromOption(decode(value), () => undefined),
      )

      if (forgeMode) return overrideDecoded

      if (process.env.OPENCODE_AUTH_CONTENT) {
        try {
          const env = JSON.parse(process.env.OPENCODE_AUTH_CONTENT) as Record<string, Info>
          return { ...env, ...overrideDecoded }
        } catch (err) {}
      }

      const data = (yield* fsys.readJson(file).pipe(Effect.orElseSucceed(() => ({})))) as Record<string, unknown>
      const diskDecoded = Record.filterMap(data, (value) => Result.fromOption(decode(value), () => undefined))
      return { ...diskDecoded, ...overrideDecoded }
    })

    const get = Effect.fn("Auth.get")(function* (providerID: string) {
      return (yield* all())[providerID]
    })

    const set = Effect.fn("Auth.set")(function* (key: string, info: Info) {
      const norm = key.replace(/\/+$/, "")
      const data = yield* all()
      if (norm !== key) delete data[key]
      delete data[norm + "/"]
      yield* fsys
        .writeJson(file, { ...data, [norm]: info }, 0o600)
        .pipe(Effect.mapError(fail("Failed to write auth data")))
    })

    const remove = Effect.fn("Auth.remove")(function* (key: string) {
      const norm = key.replace(/\/+$/, "")
      const data = yield* all()
      delete data[key]
      delete data[norm]
      yield* fsys.writeJson(file, data, 0o600).pipe(Effect.mapError(fail("Failed to write auth data")))
    })

    return Service.of({ get, all, set, remove })
  }),
)

export const defaultLayer = layer.pipe(Layer.provide(AppFileSystem.defaultLayer))

export * as Auth from "."
