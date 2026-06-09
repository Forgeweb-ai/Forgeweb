/**
 * Tests for the Forge user-preferences ("skills.md") system-prompt injection.
 *
 * These cover the load-bearing invariants:
 *  1. No-op when the per-project file doesn't exist (so non-Forge usage of
 *     opencode is byte-identical).
 *  2. Empty / whitespace file = no injection (don't bloat the system prompt
 *     with nothing).
 *  3. Content from the right file is included and clearly tagged so the
 *     model treats it as user-authored preferences distinct from platform
 *     rules.
 *  4. Oversized blobs get truncated rather than blown straight into the
 *     model's context (defensive against forge-server's size cap being
 *     bypassed or a hand-edited file).
 */
import { describe, expect } from "bun:test"
import { Effect, Layer } from "effect"
import path from "path"
import fs from "fs/promises"
import { AppFileSystem } from "@opencode-ai/core/filesystem"
import { CrossSpawnSpawner } from "@opencode-ai/core/cross-spawn-spawner"
import { userPreferencesPrompt } from "../../src/forge/user-preferences"
import { provideTmpdirInstance } from "../fixture/fixture"
import { testEffect } from "../lib/effect"

const it = testEffect(Layer.mergeAll(AppFileSystem.defaultLayer, CrossSpawnSpawner.defaultLayer))

// All tests use a workspace at `<tmp>/workspace` so the prefs file location
// follows the Forge convention: one level above the workspace directory.
async function writeWorkspace(dir: string) {
  const ws = path.join(dir, "workspace")
  await fs.mkdir(ws, { recursive: true })
  return ws
}

describe("Forge.userPreferencesPrompt", () => {
  it.live("returns undefined when prefs file is absent (non-Forge usage)", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          // Provided fixture pins ctx.directory = dir. The injector looks
          // one level up at path.dirname(dir)/preferences.md, which doesn't
          // exist in this fresh tmpdir. Must return undefined.
          void dir
          const out = yield* userPreferencesPrompt()
          expect(out).toBeUndefined()
        }),
      { git: true },
    ),
  )

  it.live("returns undefined when prefs file is empty / whitespace only", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          // Write a whitespace-only file at the location the injector
          // checks (path.dirname(dir)/preferences.md). After .trim() it's
          // empty, so injection should be skipped.
          const prefsPath = path.join(path.dirname(dir), "preferences.md")
          yield* Effect.promise(() => fs.writeFile(prefsPath, "   \n  \n\t"))
          yield* Effect.addFinalizer(() =>
            Effect.promise(() => fs.unlink(prefsPath).catch(() => undefined)),
          )

          const out = yield* userPreferencesPrompt()
          expect(out).toBeUndefined()
        }),
      { git: true },
    ),
  )

  it.live("returns tagged block when prefs file has content", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          // The function derives prefs as path.join(path.dirname(ctx.directory), "preferences.md").
          // The fixture sets ctx.directory = dir, so we write to its parent.
          const prefsPath = path.join(path.dirname(dir), "preferences.md")
          yield* Effect.promise(() =>
            fs.writeFile(prefsPath, "Always use Tailwind. Prefer snake_case in DB columns."),
          )
          // Clean up after — the parent tmp dir is not auto-cleaned, only `dir` is.
          yield* Effect.addFinalizer(() =>
            Effect.promise(() => fs.unlink(prefsPath).catch(() => undefined)),
          )

          const out = yield* userPreferencesPrompt()
          expect(out).toBeDefined()
          expect(out).toContain("<user_preferences>")
          expect(out).toContain("</user_preferences>")
          expect(out).toContain("Always use Tailwind")
          expect(out).toContain("snake_case")
          // Must NOT claim to be a platform rule — frame as user-authored
          // so the model knows scope rules take precedence on conflict.
          expect(out).toContain("standing preferences")
        }),
      { git: true },
    ),
  )

  it.live("truncates oversized prefs files with a marker", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          const prefsPath = path.join(path.dirname(dir), "preferences.md")
          // 150KB of content — well past the 100KB cap.
          const huge = "x".repeat(150 * 1024)
          yield* Effect.promise(() => fs.writeFile(prefsPath, huge))
          yield* Effect.addFinalizer(() =>
            Effect.promise(() => fs.unlink(prefsPath).catch(() => undefined)),
          )

          const out = yield* userPreferencesPrompt()
          expect(out).toBeDefined()
          // Marker present.
          expect(out).toContain("[truncated")
          // Stayed under cap (cap + truncation marker + wrapper tags +
          // explanatory intro — ~1KB overhead is fine; we just don't want
          // the full 150KB ending up in the system prompt).
          expect(out!.length).toBeLessThan(100 * 1024 + 1024)
        }),
      { git: true },
    ),
  )
})
