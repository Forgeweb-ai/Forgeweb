import { Schema } from "effect"

export const Info = Schema.Struct({
  paths: Schema.optional(Schema.Array(Schema.String)).annotate({
    description: "Additional paths to skill folders",
  }),
  urls: Schema.optional(Schema.Array(Schema.String)).annotate({
    description: "URLs to fetch skills from (e.g., https://example.com/.well-known/skills/)",
  }),
  // Platforms hosting opencode (e.g. Forge) ship a curated set of skills
  // and need to guarantee user-supplied skills cannot shadow them by
  // registering the same `name`. Any skill whose resolved location is under
  // one of these paths is marked protected at load time: a later-discovered
  // skill with the same name is logged and skipped instead of overwriting.
  // Default behavior (empty list) is unchanged — disk skills still override
  // each other last-write-wins, matching opencode's standard semantics.
  protected_paths: Schema.optional(Schema.Array(Schema.String)).annotate({
    description:
      "Paths whose skills are immutable: a duplicate skill name discovered elsewhere will be skipped, not overwrite. Use for platform-managed skill folders.",
  }),
})

export type Info = Schema.Schema.Type<typeof Info>

export * as ConfigSkills from "./skills"
