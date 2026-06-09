/**
 * Forge `request_images` tool
 * ===========================
 * Lets the main coding agent enqueue 1–6 image-gen jobs in the user's
 * project queue and continue coding **without blocking** on the worker.
 * Returned `served_url` strings are stable placeholders the agent embeds
 * in JSX (`<img src="…">`); the runner resolves them to the real asset
 * once the image-gen worker on forge-server completes.
 *
 * Why a tool (not a subagent):
 *   opencode's `task` tool is synchronous — the calling agent waits. That
 *   would make image-gen latency dominate the whole turn. The request
 *   endpoint returns immediately with slot ids; the actual provider call
 *   happens out-of-band on forge-server's image worker. See
 *   TODO_IMAGE_GEN.md "Architecture (agreed)".
 *
 * Auth: HMAC pattern matching agent-model.ts / custom-providers.ts. The
 * opencode_proxy in forge-server injects X-Forge-User-Id +
 * X-Forge-Internal-Token on every request to opencode; we forward both
 * to /api/internal/projects/{id}/images/request which re-verifies the HMAC.
 * This is the SAME contract every other opencode → forge-server callback
 * uses; do NOT mint a separate FORGE_API_TOKEN — the shared opencode
 * process has no per-user JWT and inventing one is a credential-rotation
 * footgun (see commit history for the original FORGE_API_TOKEN regression
 * surfaced 2026-06-04).
 *
 * Failure modes (mapped to user-readable strings, NOT raw HTTP):
 *   - 401  → no Forge user-id on the request (e.g. tool fired outside a
 *            Forge proxy hop). Fail closed with "unavailable" so the
 *            agent falls back to Unsplash.
 *   - 409  → image-gen disabled in Settings; the agent tells the user
 *            "enable Image AI in Settings to generate images here."
 *   - 422  → invalid request shape (e.g. prompt too long)
 *   - 5xx / network → "image service unavailable; placeholders shown."
 *
 * Never throws on backend failure: returns a structured error in the
 * tool output so the main agent can decide whether to fall back (e.g.
 * Unsplash placeholder, or skip the section).
 */
import { Effect, Schema } from "effect"

import * as Tool from "./tool"
import { UserId, InternalToken } from "../forge/user-id"
import { InstanceState } from "@/effect/instance-state"

// ── Description (inline; small + helps debug missing .txt issues) ────────────

const DESCRIPTION = `Enqueue 1–6 AI-generated images for this Forge project. Returns immediately with placeholder URLs the agent embeds in JSX — the real images stream in asynchronously while the agent continues coding.

WHEN TO USE
- Building or refining a page that needs hero art, illustrations, photos, icons larger than emoji, or any decorative imagery.
- Only call when Image AI is enabled in Settings (the call will 409 otherwise and you should tell the user how to enable it).

OUTPUT JSX PATTERN
For each returned slot, embed the served_url verbatim:
  <img src="<served_url>" alt="..." />
The served_url is a relative path like \`/images/{slot_id}.png\`. Paste it as-is — do NOT prepend any base URL. The worker writes the file into THIS project's public/images/ directory and the project's own dev server serves it. The image will briefly show as broken (~2-5s) while the worker generates it; once the worker finishes the file appears at the same URL on the next render. You do NOT need to poll status or rewrite the JSX later.

HARD RULES
- Max 6 images per call. The 7th+ are rejected; do not embed JSX for rejected items.
- Same prompt+model+size submitted twice returns the SAME slot id (dedup) — call it again deliberately if you want a re-roll.
- This tool does NOT block; do not wait for completion before continuing the turn.`

// ── Schemas ──────────────────────────────────────────────────────────────────

const Item = Schema.Struct({
  prompt: Schema.String.annotate({
    description: "Text prompt for the image. 1–4000 chars. Be specific about subject, style, lighting, mood.",
  }),
  size: Schema.optional(Schema.String).annotate({
    description: "Target size, e.g. '1024x1024' or '1024x1536'. Defaults to the model's first supported size.",
  }),
  ref_blob_sha: Schema.optional(Schema.String).annotate({
    description: "SHA256 of a reference image already in the content-addressed blob store (image-to-image). Omit for pure text-to-image.",
  }),
})

export const Parameters = Schema.Struct({
  items: Schema.mutable(Schema.Array(Item)).annotate({
    description: "Batch of image requests. 1–6 entries; over-cap entries are rejected.",
  }),
})

type Metadata = {
  slot_ids: ReadonlyArray<string>
  dedup_count: number
  rejected_count: number
  // Non-zero exit category when the whole call failed (e.g. 409 disabled).
  // null on success or partial success.
  error: string | null
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function forgeApiBase(): string | undefined {
  // Mirrors agent-model.ts. Either env name works so opencode running in
  // dev vs prod containers needs no tool-side branching.
  return process.env.FORGE_API_URL || process.env.FORGE_INTERNAL_API_URL
}

/**
 * Recover the project id from a workspace path like
 *   /forge-data/users/<uid>/projects/<pid>/workspace
 * Mirrors data-panel.tsx's regex on the FE side. Returns undefined when
 * the path doesn't match — caller falls back to FORGE_PROJECT_ID env (set
 * by the per-project shell environment, when present) before failing.
 */
function projectIdFromDir(dir: string | undefined): string | undefined {
  if (!dir) return undefined
  const m = dir.match(/\/projects\/([a-f0-9-]{8,}[a-f0-9])\/workspace/)
  return m?.[1]
}

// ── Tool ─────────────────────────────────────────────────────────────────────

export const RequestImagesTool = Tool.define<typeof Parameters, Metadata>(
  "request_images",
  Effect.gen(function* () {
    return {
      description: DESCRIPTION,
      parameters: Parameters,
      execute: (params: Schema.Schema.Type<typeof Parameters>, ctx: Tool.Context<Metadata>) =>
        Effect.gen(function* () {
          // ── Auth + identity ─────────────────────────────────────────────
          // Per the file header: HMAC via Context, NOT FORGE_API_TOKEN.
          const userId    = yield* UserId
          const token     = yield* InternalToken
          const apiBase   = forgeApiBase()

          if (!userId || !token || !apiBase) {
            return {
              title: "request_images unavailable",
              output: `Image generation is not available: ${
                !apiBase ? "FORGE_API_URL not set" :
                !userId  ? "request did not pass through Forge proxy (no user id)" :
                           "internal token missing"
              }.`,
              metadata: { slot_ids: [], dedup_count: 0, rejected_count: 0, error: "unavailable" },
            }
          }

          // Project id resolution — matches the pattern read.ts uses for
          // workspace lookups. The InstanceState.context carries `directory`
          // and `worktree` set when the session was created from a Forge
          // workspace path. FORGE_PROJECT_ID env is a fallback for non-
          // session contexts (CLI, tests).
          const instance = yield* InstanceState.context
          const projectId =
            projectIdFromDir(instance?.directory) ||
            projectIdFromDir(instance?.worktree) ||
            process.env.FORGE_PROJECT_ID
          if (!projectId) {
            return {
              title: "request_images unavailable",
              output: `Image generation is not available: could not resolve the current project id (directory=${
                instance?.directory ?? "<none>"
              }).`,
              metadata: { slot_ids: [], dedup_count: 0, rejected_count: 0, error: "unavailable" },
            }
          }

          if (params.items.length === 0) {
            return {
              title: "request_images: empty batch",
              output: "No image requests provided.",
              metadata: { slot_ids: [], dedup_count: 0, rejected_count: 0, error: "empty" },
            }
          }

          const url = `${apiBase.replace(/\/$/, "")}/api/internal/projects/${projectId}/images/request`

          const response = yield* Effect.tryPromise({
            try: () => fetch(url, {
              method: "POST",
              headers: {
                "x-forge-user-id":        userId,
                "x-forge-internal-token": token,
                "Content-Type":           "application/json",
              },
              body: JSON.stringify({ items: params.items }),
            }),
            catch: (e) => e,
          }).pipe(
            Effect.catch((cause) => Effect.succeed({ ok: false, status: 0, _err: String(cause) } as const)),
          )

          // Bail out on network errors with a clear, agent-readable message.
          if (!("status" in response) || response.status === 0) {
            return {
              title: "request_images failed",
              output: `Image service unreachable. Tell the user the placeholders will not be filled this turn; suggest retry.`,
              metadata: { slot_ids: [], dedup_count: 0, rejected_count: 0, error: "network" },
            }
          }

          if (response.status === 409) {
            const body = yield* Effect.tryPromise({ try: () => response.text(), catch: () => "" }).pipe(
              Effect.catch(() => Effect.succeed("")),
            )
            return {
              title: "Image AI disabled",
              output: `Image generation is turned off for this user. Tell them to enable it at Settings → Image AI and pick a model. Server said: ${body.slice(0, 200)}`,
              metadata: { slot_ids: [], dedup_count: 0, rejected_count: 0, error: "disabled" },
            }
          }

          if (response.status >= 400) {
            const body = yield* Effect.tryPromise({ try: () => response.text(), catch: () => "" }).pipe(
              Effect.catch(() => Effect.succeed("")),
            )
            return {
              title: `request_images error (${response.status})`,
              output: `Image service rejected the request: ${response.status}. Body: ${body.slice(0, 240)}`,
              metadata: { slot_ids: [], dedup_count: 0, rejected_count: 0, error: `http_${response.status}` },
            }
          }

          // Happy path: parse the structured response.
          const json = (yield* Effect.tryPromise({
            try: () => response.json() as Promise<{
              jobs: Array<{ slot_id: string; served_url: string; status: string; deduplicated: boolean }>
              rejected: Array<{ index: number; reason: string; prompt: string }>
            }>,
            catch: (e) => e,
          }).pipe(Effect.catch(() => Effect.succeed(undefined)))) ?? undefined

          if (!json || !Array.isArray(json.jobs)) {
            return {
              title: "request_images: bad response",
              output: "Image service returned an unrecognized payload. Skip image embedding for this turn.",
              metadata: { slot_ids: [], dedup_count: 0, rejected_count: 0, error: "bad_response" },
            }
          }

          const slot_ids = json.jobs.map((j) => j.slot_id)
          const dedupCount = json.jobs.filter((j) => j.deduplicated).length
          const rejectedCount = json.rejected?.length ?? 0

          const lines: string[] = []
          lines.push(`Enqueued ${json.jobs.length} image${json.jobs.length === 1 ? "" : "s"}${dedupCount ? ` (${dedupCount} reused from earlier turn)` : ""}.`)
          lines.push("Embed each in JSX as <img src=\"<served_url>\" alt=\"...\" /> with descriptive alt text:")
          for (const j of json.jobs) {
            lines.push(`  - slot ${j.slot_id}: ${j.served_url}${j.deduplicated ? " (deduped)" : ""}`)
          }
          if (rejectedCount > 0) {
            lines.push(`${rejectedCount} item${rejectedCount === 1 ? "" : "s"} rejected (per-call cap of 6). Do NOT embed JSX for these.`)
          }
          lines.push("Continue coding; the runner will resolve the placeholders automatically when images finish.")

          return {
            title: `request_images: ${json.jobs.length} queued${dedupCount ? `, ${dedupCount} reused` : ""}`,
            output: lines.join("\n"),
            metadata: {
              slot_ids,
              dedup_count: dedupCount,
              rejected_count: rejectedCount,
              error: null,
            },
          }
        }).pipe(Effect.orDie),
    }
  }),
)
