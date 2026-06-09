/**
 * Forge user preferences ("skills.md") injection.
 *
 * Per-user free-form markdown blob, written by forge-server to a fixed
 * disk location, materialized per-project via a symlink. Injected into
 * every project session's system prompt per turn so the AI remembers the
 * user's standing preferences across all of their apps (e.g. "always use
 * Tailwind", "snake_case DB columns", etc.).
 *
 * Path convention (set by forge-server's api/preferences_routes.py):
 *   workspace:  /forge-data/users/<uid>/projects/<pid>/workspace/
 *   prefs:      /forge-data/users/<uid>/projects/<pid>/preferences.md
 *               (symlink → /forge-data/users/<uid>/preferences.md)
 *
 * We derive the prefs path purely positionally: one level above the
 * workspace directory. No path parsing, no env vars, no coupling to a
 * specific user_id format. This means:
 *   - In Forge: the file exists, gets injected.
 *   - In non-Forge opencode usage: the file doesn't exist (nothing wrote
 *     it), the read returns undefined, no injection. Zero behaviour change
 *     for users running opencode directly.
 *
 * Per-turn cost shape (§3): one stat() + one readFileString() per turn.
 * The OS file cache makes both effectively free after the first read.
 * Cost in tokens = the blob's size, added to the system prompt — stable
 * across the session, prompt-cacheable. NULL/empty file = zero tokens.
 *
 * Why a separate prompt block, not the standard `instructions[]` config:
 * the workspace is the worktree (git init'd at scaffold time, see
 * forge-server/api/projects.py::_scaffold_workspace), so opencode's
 * `findUp` instruction discovery doesn't walk above the workspace and
 * therefore can't find anything outside it. Per-project opencode.json
 * would solve that — but the workspace is sacred (Forge writes nothing
 * into it; user-visible diffs only), so we can't drop a config file there
 * either. A small injection here avoids both constraints.
 */
import path from "path"
import { Effect } from "effect"
import { AppFileSystem } from "@opencode-ai/core/filesystem"
import { InstanceState } from "@/effect/instance-state"
import * as Log from "@opencode-ai/core/util/log"

const log = Log.create({ service: "forge.user-preferences" })

// Cap so a runaway file can't blow up the per-turn system prompt at the
// model. Mirrors forge-server's MAX_PREFERENCES_BYTES (100 KB). Anything
// over is truncated with a clear marker so the user sees something is off.
const MAX_PREFERENCES_BYTES = 100 * 1024
const TRUNCATION_MARKER = "\n\n[truncated — preferences exceed 100KB]"

const PREFERENCES_FILENAME = "preferences.md"

/**
 * Returns the system-prompt block carrying the user's preferences, or
 * undefined when there are no preferences to inject. Safe to call in any
 * session — if the file doesn't exist (non-Forge usage, or user hasn't
 * set preferences yet), returns undefined.
 */
export const userPreferencesPrompt = Effect.fn("Forge.userPreferencesPrompt")(function* () {
  const ctx = yield* InstanceState.context
  // Project root is one level above the workspace (set by forge-server's
  // _workspace_path()). The symlink lives there.
  const prefsPath = path.join(path.dirname(ctx.directory), PREFERENCES_FILENAME)

  const fs = yield* AppFileSystem.Service
  const exists = yield* fs.existsSafe(prefsPath).pipe(Effect.catch(() => Effect.succeed(false)))
  if (!exists) return undefined

  const raw = yield* fs.readFileString(prefsPath).pipe(Effect.catch(() => Effect.succeed("")))
  const content = raw.trim()
  if (!content) return undefined

  // Defensive cap. forge-server enforces 100KB on PUT, but a hand-edited
  // file or a future code path could exceed it; cheaper to truncate here
  // than to ship oversize content × N turns × N users.
  const bytes = Buffer.byteLength(content, "utf-8")
  const body =
    bytes > MAX_PREFERENCES_BYTES
      ? content.slice(0, MAX_PREFERENCES_BYTES) + TRUNCATION_MARKER
      : content

  if (bytes > MAX_PREFERENCES_BYTES) {
    log.warn("preferences exceeded cap, truncating", { path: prefsPath, bytes })
  }

  // Frame the block clearly so the model treats it as user-authored
  // standing instructions, distinct from platform rules (scope prompt)
  // and environment info.
  return [
    "<user_preferences>",
    "The following are the user's standing preferences. Apply them when relevant; they reflect how this user wants you to work across all of their apps. They are NOT platform rules — defer to the platform scope rules above if anything conflicts.",
    "",
    body,
    "</user_preferences>",
  ].join("\n")
})
