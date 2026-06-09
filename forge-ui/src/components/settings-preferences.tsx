/**
 * settings-preferences.tsx
 * =========================
 * Settings tab for the user's free-form preferences blob ("skills.md").
 *
 * One textarea. Saved to forge-server's users.preferences_md column and
 * materialized to disk at /forge-data/users/<uid>/preferences.md. Opencode's
 * Forge user-preferences injector reads it per-turn into every project's
 * system prompt — so a save here affects ALL of the user's projects on
 * their next message (no session restart needed; verified — opencode's
 * instruction.system() is called per-turn, not cached).
 *
 * Token-cost-aware UX:
 *   - Live byte + approximate-token count so the user sees the per-turn
 *     cost they're opting into BEFORE saving.
 *   - Soft warning above ~4 KB (~1k tokens) — that's a noticeable per-turn
 *     cost on every message. Not a hard block; users with real preferences
 *     can exceed it.
 *   - Hard cap at 100 KB matching the server's MAX_PREFERENCES_BYTES.
 *
 * No-op short-circuit on save (content === current) handled server-side;
 * the FE just calls PUT.
 */
import { type Component, Show, createResource, createSignal, createMemo } from "solid-js"
import { showToast } from "@opencode-ai/ui/toast"
import { Button } from "@opencode-ai/ui/button"
import { useForgeApi } from "@/context/forge-api"

// Mirror forge-server's MAX_PREFERENCES_BYTES. Hard cap; server enforces too.
const MAX_BYTES = 100 * 1024
// Soft warning threshold — preferences over this size add notable per-turn
// token cost on EVERY message. Roughly aligns with ~1k tokens.
const SOFT_WARN_BYTES = 4 * 1024

// 1 token ≈ 4 chars is the standard back-of-envelope for English-ish text.
// Good enough for an informational FE-side counter (not a billing source).
const approxTokens = (bytes: number) => Math.ceil(bytes / 4)

export const SettingsPreferences: Component = () => {
  const forge = useForgeApi()

  // Resource holds the server's truth. Local `draft` holds the in-progress
  // edit; we compare to determine dirty state.
  const [server, { refetch }] = createResource(() =>
    forge.getPreferences().catch(() => ({ content: "", bytes: 0 })),
  )
  const [draft, setDraft] = createSignal<string | null>(null)
  const [saving, setSaving] = createSignal(false)

  // Current text being shown in the textarea. Falls back to server value
  // until the user types — once they edit (draft becomes a string), we use
  // draft. setDraft(null) on save success resets to server-truth.
  const value = createMemo(() => draft() ?? server()?.content ?? "")
  const bytes = createMemo(() => new TextEncoder().encode(value()).length)
  const tokens = createMemo(() => approxTokens(bytes()))
  const dirty = createMemo(() => draft() !== null && draft() !== (server()?.content ?? ""))
  const overCap = createMemo(() => bytes() > MAX_BYTES)
  const overSoft = createMemo(() => bytes() > SOFT_WARN_BYTES)

  async function save() {
    if (!dirty() || overCap()) return
    setSaving(true)
    try {
      await forge.updatePreferences(value())
      setDraft(null)
      void refetch()
      showToast({
        title: "Preferences saved",
        description: "Applies on your next message in any project.",
        variant: "success",
      })
    } catch (e: any) {
      showToast({
        title: "Save failed",
        description: e?.message ?? "Could not save preferences",
        variant: "error",
      })
    } finally {
      setSaving(false)
    }
  }

  function discard() {
    setDraft(null)
  }

  return (
    <div class="flex flex-col h-full overflow-y-auto no-scrollbar px-4 pb-10 sm:px-10 sm:pb-10">
      {/* ── Header ──────────────────────────────────────────────────────── */}
      <div class="flex flex-col gap-3 pt-6 pb-4 max-w-[760px]">
        <h2 class="text-16-medium text-text-strong">Preferences</h2>

        <div class="rounded-lg border border-border-weak-base bg-surface-base px-4 py-3 flex flex-col gap-1.5 max-w-[640px]">
          <span class="text-13-medium text-text-strong">How this works</span>
          <p class="text-13-regular text-text-weak leading-relaxed">
            Write standing instructions you want Forge to follow across{" "}
            <span class="text-text-base">all of your projects</span> — things like
            “always use Tailwind”, “prefer snake_case in DB columns”, or
            “use TanStack Query for data fetching”.
          </p>
          <p class="text-13-regular text-text-weak leading-relaxed">
            These are loaded into every session’s system prompt and apply on
            your next message — no restart needed. Platform rules and the
            in-project AGENTS.md still take precedence if anything conflicts.
          </p>
        </div>

        {/* Cost hint — primary lever, surface it before the user types. */}
        <p class="text-12-regular text-text-weak max-w-[600px]">
          Cost note: this content is added to every turn’s system prompt on
          your API key. ~500 tokens is sane; over ~1k tokens you’ll feel it
          on every message.
        </p>
      </div>

      {/* ── Editor ──────────────────────────────────────────────────────── */}
      <div class="flex flex-col gap-2 max-w-[760px]">
        <textarea
          value={value()}
          onInput={(e) => setDraft(e.currentTarget.value)}
          placeholder={`# My preferences\n\n- Always use Tailwind for styling\n- Prefer TanStack Query over SWR\n- snake_case for DB columns\n`}
          spellcheck={false}
          rows={18}
          class="w-full font-mono text-13-regular bg-surface-base border border-border-weak-base rounded-md p-3 resize-y leading-relaxed text-text-base placeholder:text-text-weak focus:outline-none focus:border-text-accent"
          classList={{
            "border-text-danger": overCap(),
          }}
          disabled={saving()}
        />

        {/* ── Counters + state row ────────────────────────────────────── */}
        <div class="flex items-center justify-between gap-3 text-12-regular text-text-weak">
          <div class="flex items-center gap-3 flex-wrap">
            <span>
              {bytes().toLocaleString()} bytes
              {" · "}
              ~{tokens().toLocaleString()} tokens per turn
            </span>
            <Show when={overCap()}>
              <span class="text-text-danger font-medium">
                Over {MAX_BYTES.toLocaleString()}-byte cap — trim before saving.
              </span>
            </Show>
            <Show when={!overCap() && overSoft()}>
              <span class="text-text-warning">
                Heads-up: this size will be felt on every message.
              </span>
            </Show>
          </div>

          <div class="flex items-center gap-2">
            <Show when={dirty()}>
              <Button
                type="button"
                variant="ghost"
                onClick={discard}
                disabled={saving()}
              >
                Discard
              </Button>
            </Show>
            <Button
              type="button"
              variant="primary"
              onClick={() => void save()}
              disabled={!dirty() || overCap() || saving()}
            >
              {saving() ? "Saving…" : "Save"}
            </Button>
          </div>
        </div>
      </div>
    </div>
  )
}
