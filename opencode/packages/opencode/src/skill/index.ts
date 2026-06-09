import path from "path"
import { pathToFileURL } from "url"
import { Effect, Layer, Context, Schema } from "effect"
import { NamedError } from "@opencode-ai/core/util/error"
import type { Agent } from "@/agent/agent"
import { Bus } from "@/bus"
import { InstanceState } from "@/effect/instance-state"
import { Global } from "@opencode-ai/core/global"
import { Permission } from "@/permission"
import { AppFileSystem } from "@opencode-ai/core/filesystem"
import { Config } from "@/config/config"
import { ConfigMarkdown } from "@/config/markdown"
import { RuntimeFlags } from "@/effect/runtime-flags"
import { Glob } from "@opencode-ai/core/util/glob"
import * as Log from "@opencode-ai/core/util/log"
import { Discovery } from "./discovery"
import CUSTOMIZE_OPENCODE_SKILL_BODY from "./prompt/customize-opencode.md" with { type: "text" }
import UI_UX_PRO_MAX_SKILL_BODY from "./prompt/ui-ux-pro-max.md" with { type: "text" }
import { isRecord } from "@/util/record"

const log = Log.create({ service: "skill" })
const CLAUDE_EXTERNAL_DIR = ".claude"
const AGENTS_EXTERNAL_DIR = ".agents"
const EXTERNAL_SKILL_PATTERN = "skills/**/SKILL.md"
const OPENCODE_SKILL_PATTERN = "{skill,skills}/**/SKILL.md"
const SKILL_PATTERN = "**/SKILL.md"

// Built-in skill (Forge fork: renamed from "customize-opencode" — the
// original name leaked the upstream runtime brand into any chat surface
// that rendered the skill name verbatim, violating the IDENTITY GUARDRAIL
// in forge-opencode-config/AGENTS.md). The skill's BODY still describes
// opencode-config schema because the runtime IS opencode under the hood —
// but the name and description shown to users (and to the model when it
// decides whether to invoke) carry no brand leak. The trigger description
// is also rewritten to be tighter — Forge users almost never touch runtime
// config directly, so this skill should rarely (ideally never) fire for
// app-building requests.
const CUSTOMIZE_OPENCODE_SKILL_NAME = "platform-config"
const CUSTOMIZE_OPENCODE_SKILL_DESCRIPTION =
  "Use ONLY when the user is directly editing the runtime's own configuration files (e.g. files named opencode.json, opencode.jsonc, or paths under .opencode/ or ~/.config/opencode/), or creating new platform agents/subagents/skills/plugins/MCP servers/permission rules. Do NOT use for the user's own application code, scaffolding a new app, creating a database, building UI, or any normal feature work — those are not platform config."

const UI_UX_PRO_MAX_SKILL_NAME = "ui-ux-pro-max"
const UI_UX_PRO_MAX_SKILL_DESCRIPTION =
  "Use when building, refactoring, or reviewing web applications, user interfaces, or frontend components. Provides design intelligence for layout, styles, color harmony, typography, micro-animations, accessibility, and modern interactive patterns."

export const Info = Schema.Struct({
  name: Schema.String,
  description: Schema.optional(Schema.String),
  location: Schema.String,
  content: Schema.String,
})
export type Info = Schema.Schema.Type<typeof Info>

const Issue = Schema.StructWithRest(
  Schema.Struct({
    message: Schema.String,
    path: Schema.Array(Schema.String),
  }),
  [Schema.Record(Schema.String, Schema.Unknown)],
)

function isSkillFrontmatter(data: unknown): data is { name: string; description?: string } {
  return (
    isRecord(data) &&
    typeof data.name === "string" &&
    (data.description === undefined || typeof data.description === "string")
  )
}

export class InvalidError extends Schema.TaggedErrorClass<InvalidError>()("SkillInvalidError", {
  path: Schema.String,
  message: Schema.optional(Schema.String),
  issues: Schema.optional(Schema.Array(Issue)),
}) {}

export class NameMismatchError extends Schema.TaggedErrorClass<NameMismatchError>()("SkillNameMismatchError", {
  path: Schema.String,
  expected: Schema.String,
  actual: Schema.String,
}) {}

export class NotFoundError extends Schema.TaggedErrorClass<NotFoundError>()("Skill.NotFoundError", {
  name: Schema.String,
  available: Schema.Array(Schema.String),
}) {
  override get message() {
    return `Skill "${this.name}" not found. Available skills: ${this.available.join(", ") || "none"}`
  }
}

type State = {
  skills: Record<string, Info>
  dirs: Set<string>
  // Names of skills loaded from a path declared in `skills.protected_paths`.
  // A later-discovered skill with the same name is skipped, not overwritten.
  // See ConfigSkills.protected_paths for rationale.
  protectedNames: Set<string>
  protectedPaths: string[]
}

type DiscoveryState = {
  matches: string[]
  dirs: string[]
}

type ScanState = {
  matches: Set<string>
  dirs: Set<string>
}

export interface Interface {
  readonly get: (name: string) => Effect.Effect<Info | undefined>
  readonly require: (name: string) => Effect.Effect<Info, NotFoundError>
  readonly all: () => Effect.Effect<Info[]>
  readonly dirs: () => Effect.Effect<string[]>
  readonly available: (agent?: Agent.Info) => Effect.Effect<Info[]>
}

// Path-prefix check: returns true iff `child` is exactly `parent` or sits under
// it. Compared with separators to avoid `/forge-skills` falsely matching
// `/forge-skills-backup/...`. Normalize once with path.resolve so callers can
// pass mixed absolute/relative or trailing-slash variants.
function isUnderPath(child: string, parent: string) {
  const c = path.resolve(child)
  const p = path.resolve(parent)
  if (c === p) return true
  return c.startsWith(p + path.sep)
}

function isProtectedLocation(location: string, protectedPaths: string[]) {
  if (protectedPaths.length === 0) return false
  for (const root of protectedPaths) {
    if (isUnderPath(location, root)) return true
  }
  return false
}

const add = Effect.fnUntraced(function* (state: State, match: string, bus: Bus.Interface) {
  const md = yield* Effect.tryPromise({
    try: () => ConfigMarkdown.parse(match),
    catch: (err) => err,
  }).pipe(
    Effect.catch(
      Effect.fnUntraced(function* (err) {
        const message = ConfigMarkdown.FrontmatterError.isInstance(err)
          ? err.data.message
          : `Failed to parse skill ${match}`
        const { Session } = yield* Effect.promise(() => import("@/session/session"))
        yield* bus.publish(Session.Event.Error, { error: new NamedError.Unknown({ message }).toObject() })
        log.error("failed to load skill", { skill: match, err })
        return undefined
      }),
    ),
  )

  if (!md) return

  if (!isSkillFrontmatter(md.data)) return

  const name = md.data.name
  const incomingIsProtected = isProtectedLocation(match, state.protectedPaths)

  if (state.skills[name]) {
    // Refuse to overwrite a skill that came from a protected path. The
    // hosting platform (e.g. Forge) needs its skill registry to be
    // tamper-proof from user-supplied skills sharing a name. Log loudly so
    // the user notices their skill was ignored.
    if (state.protectedNames.has(name)) {
      log.warn("ignored skill (name collides with protected skill)", {
        name,
        protected: state.skills[name].location,
        ignored: match,
      })
      return
    }
    log.warn("duplicate skill name", {
      name,
      existing: state.skills[name].location,
      duplicate: match,
    })
  }

  state.dirs.add(path.dirname(match))
  state.skills[name] = {
    name,
    description: md.data.description,
    location: match,
    content: md.content,
  }
  if (incomingIsProtected) state.protectedNames.add(name)
  else state.protectedNames.delete(name)
})

const scan = Effect.fnUntraced(function* (
  state: ScanState,
  root: string,
  pattern: string,
  opts?: { dot?: boolean; scope?: string },
) {
  const matches = yield* Effect.tryPromise({
    try: () =>
      Glob.scan(pattern, {
        cwd: root,
        absolute: true,
        include: "file",
        symlink: true,
        dot: opts?.dot,
      }),
    catch: (error) => error,
  }).pipe(
    Effect.catch((error) => {
      if (!opts?.scope) return Effect.die(error)
      log.error(`failed to scan ${opts.scope} skills`, { dir: root, error })
      return Effect.succeed([] as string[])
    }),
  )

  for (const match of matches) {
    state.matches.add(match)
    state.dirs.add(path.dirname(match))
  }
})

const discoverSkills = Effect.fnUntraced(function* (
  config: Config.Interface,
  discovery: Discovery.Interface,
  fsys: AppFileSystem.Interface,
  global: Global.Interface,
  disableExternalSkills: boolean,
  disableClaudeCodeSkills: boolean,
  directory: string,
  worktree: string,
) {
  const state: ScanState = { matches: new Set(), dirs: new Set() }

  const externalDirs: string[] = []
  if (!disableExternalSkills) {
    if (!disableClaudeCodeSkills) externalDirs.push(CLAUDE_EXTERNAL_DIR)
    externalDirs.push(AGENTS_EXTERNAL_DIR)

    for (const dir of externalDirs) {
      const root = path.join(global.home, dir)
      if (!(yield* fsys.isDir(root))) continue
      yield* scan(state, root, EXTERNAL_SKILL_PATTERN, { dot: true, scope: "global" })
    }

    const upDirs = yield* fsys
      .up({ targets: externalDirs, start: directory, stop: worktree })
      .pipe(Effect.catch(() => Effect.succeed([] as string[])))

    for (const root of upDirs) {
      yield* scan(state, root, EXTERNAL_SKILL_PATTERN, { dot: true, scope: "project" })
    }
  }

  const configDirs = yield* config.directories()
  for (const dir of configDirs) {
    yield* scan(state, dir, OPENCODE_SKILL_PATTERN)
  }

  const cfg = yield* config.get()
  for (const item of cfg.skills?.paths ?? []) {
    const expanded = item.startsWith("~/") ? path.join(global.home, item.slice(2)) : item
    const dir = path.isAbsolute(expanded) ? expanded : path.join(directory, expanded)
    if (!(yield* fsys.isDir(dir))) {
      log.warn("skill path not found", { path: dir })
      continue
    }

    yield* scan(state, dir, SKILL_PATTERN)
  }

  for (const url of cfg.skills?.urls ?? []) {
    const pulledDirs = yield* discovery.pull(url)
    for (const dir of pulledDirs) {
      yield* scan(state, dir, SKILL_PATTERN)
    }
  }

  return {
    matches: Array.from(state.matches),
    dirs: Array.from(state.dirs),
  }
})

const loadSkills = Effect.fnUntraced(function* (state: State, discovered: DiscoveryState, bus: Bus.Interface) {
  yield* Effect.forEach(discovered.matches, (match) => add(state, match, bus), {
    concurrency: "unbounded",
    discard: true,
  })

  log.info("init", { count: Object.keys(state.skills).length })
})

export class Service extends Context.Service<Service, Interface>()("@opencode/Skill") {}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const discovery = yield* Discovery.Service
    const config = yield* Config.Service
    const bus = yield* Bus.Service
    const fsys = yield* AppFileSystem.Service
    const global = yield* Global.Service
    const flags = yield* RuntimeFlags.Service
    const discovered = yield* InstanceState.make(
      Effect.fn("Skill.discovery")(function* (ctx) {
        return yield* discoverSkills(
          config,
          discovery,
          fsys,
          global,
          flags.disableExternalSkills,
          flags.disableClaudeCodeSkills,
          ctx.directory,
          ctx.worktree,
        )
      }),
    )
    const state = yield* InstanceState.make(
      Effect.fn("Skill.state")(function* () {
        const cfg = yield* config.get()
        // Resolve once at state init. `protected_paths` is small (a handful
        // of platform-managed dirs) and stable for the life of the instance,
        // so caching here avoids re-resolving on every `add()`.
        const protectedPaths = (cfg.skills?.protected_paths ?? []).map((p) => path.resolve(p))
        const s: State = {
          skills: {},
          dirs: new Set(),
          protectedNames: new Set(),
          protectedPaths,
        }
        // Register the built-in skill BEFORE disk discovery so a user-disk
        // skill with the same name can override it. Built-ins are NOT marked
        // protected — that's a platform-host concern (set via
        // skills.protected_paths in their config), not an opencode-core one.
        s.skills[CUSTOMIZE_OPENCODE_SKILL_NAME] = {
          name: CUSTOMIZE_OPENCODE_SKILL_NAME,
          description: CUSTOMIZE_OPENCODE_SKILL_DESCRIPTION,
          location: "<built-in>",
          content: CUSTOMIZE_OPENCODE_SKILL_BODY,
        }
        s.skills[UI_UX_PRO_MAX_SKILL_NAME] = {
          name: UI_UX_PRO_MAX_SKILL_NAME,
          description: UI_UX_PRO_MAX_SKILL_DESCRIPTION,
          location: "<built-in>",
          content: UI_UX_PRO_MAX_SKILL_BODY,
        }
        yield* loadSkills(s, yield* InstanceState.get(discovered), bus)
        return s
      }),
    )

    const get = Effect.fn("Skill.get")(function* (name: string) {
      const s = yield* InstanceState.get(state)
      return s.skills[name]
    })

    const require = Effect.fn("Skill.require")(function* (name: string) {
      const s = yield* InstanceState.get(state)
      const info = s.skills[name]
      if (info) return info
      return yield* new NotFoundError({ name, available: Object.keys(s.skills).toSorted() })
    })

    const all = Effect.fn("Skill.all")(function* () {
      const s = yield* InstanceState.get(state)
      return Object.values(s.skills)
    })

    const dirs = Effect.fn("Skill.dirs")(function* () {
      return (yield* InstanceState.get(discovered)).dirs
    })

    const available = Effect.fn("Skill.available")(function* (agent?: Agent.Info) {
      const s = yield* InstanceState.get(state)
      const list = Object.values(s.skills).toSorted((a, b) => a.name.localeCompare(b.name))
      if (!agent) return list
      return list.filter((skill) => Permission.evaluate("skill", skill.name, agent.permission).action !== "deny")
    })

    return Service.of({ get, require, all, dirs, available })
  }),
)

export const defaultLayer = layer.pipe(
  Layer.provide(Discovery.defaultLayer),
  Layer.provide(Config.defaultLayer),
  Layer.provide(Bus.layer),
  Layer.provide(AppFileSystem.defaultLayer),
  Layer.provide(Global.layer),
  Layer.provide(RuntimeFlags.defaultLayer),
)

export function fmt(list: Info[], opts: { verbose: boolean }) {
  const described = list.filter((skill) => skill.description !== undefined)
  if (described.length === 0) return "No skills are currently available."
  if (opts.verbose) {
    return [
      "<available_skills>",
      ...described
        .toSorted((a, b) => a.name.localeCompare(b.name))
        .flatMap((skill) => [
          "  <skill>",
          `    <name>${skill.name}</name>`,
          `    <description>${skill.description}</description>`,
          `    <location>${pathToFileURL(skill.location).href}</location>`,
          "  </skill>",
        ]),
      "</available_skills>",
    ].join("\n")
  }

  return [
    "## Available Skills",
    ...described
      .toSorted((a, b) => a.name.localeCompare(b.name))
      .map((skill) => `- **${skill.name}**: ${skill.description}`),
  ].join("\n")
}

export * as Skill from "."
