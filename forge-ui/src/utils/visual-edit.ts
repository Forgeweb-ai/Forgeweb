/**
 * visual-edit.ts
 * ==============
 * Parent-side controller for "visual edits" — the user clicks a Select
 * button in the preview toolbar, then clicks an element inside the iframe,
 * types an instruction, and the agent receives a turn anchored to the
 * exact source location of that element.
 *
 * Pipeline:
 *   1. Toolbar Select toggles → postMessage `forge:select-enter` / -exit
 *      to the iframe (the bridge in instrumentation-client.ts handles the
 *      in-page overlay + click-to-pick).
 *   2. On pick, bridge posts back `forge:select-pick` with:
 *        { source: "relpath:line:col" | null, tag, text }
 *      `source` is the data-forge-source attribute walked up from the
 *      clicked element. `null` means the element wasn't stamped (e.g.
 *      production build, third-party widget, SVG internals).
 *   3. We dispatch the existing `forge:prefill-prompt` event with a
 *      payload extended to carry the source location. prompt-input.tsx
 *      attaches it to the next turn via the same opencodeComment channel
 *      already used by editor-side comments — so the agent gets a
 *      structured edit anchor, not a free-text "look at this thing".
 *
 * Why we don't ship a screenshot by default:
 *   Per CLAUDE.md §3 (token cost): the source path tells the agent
 *   exactly which file to open. A screenshot adds ~1–3k image tokens
 *   per edit on BYOK — that's the user's wallet. Add it as an opt-in
 *   "include screenshot" toggle later, not by default.
 *
 * Cascade safety (per CLAUDE.md §3.2):
 *   All overlay rendering happens INSIDE the iframe and uses inline
 *   styles with `all: initial` + a unique attribute selector. No class
 *   names, no shared CSS, no parent inheritance possible. See the
 *   bridge implementation in forge-bootstrap.sh / instrumentation-client.
 */

import type { FixPrefillPayload } from "./fix-this-screen"

export type VisualEditPick = {
  /** "relpath:line:col" from data-forge-source, or null if unstamped. */
  source: string | null
  /** Lowercase tag name of the clicked element, e.g. "button". */
  tag:    string
  /** Up to ~80 chars of textContent from the clicked element. */
  text:   string
}

export type VisualEditPrefillPayload = FixPrefillPayload & {
  /**
   * When present, prompt-input attaches this as an opencodeComment so
   * the agent sees a structured edit anchor in addition to the text.
   */
  visualEdit?: {
    path:      string      // file path relative to project root
    startLine: number
    startChar: number      // always 0 — JSX opening tag start col is fine
    endLine:   number
    endChar:   number      // always 0
    tag:       string
    text:      string
  }
}

/**
 * Enter select mode in the iframe. The bridge inside the iframe responds
 * by attaching its hover overlay + click-capture listeners.
 */
export function enterSelectMode(iframe: HTMLIFrameElement | undefined): void {
  const win = iframe?.contentWindow
  if (!iframe || !win) return
  try {
    win.postMessage({ type: "forge:select-enter" }, "*")
  } catch {
    /* iframe gone — silent */
  }
}

/**
 * Exit select mode in the iframe (user cancelled, toolbar toggled off,
 * or pick completed). Idempotent on the bridge side.
 */
export function exitSelectMode(iframe: HTMLIFrameElement | undefined): void {
  const win = iframe?.contentWindow
  if (!iframe || !win) return
  try {
    win.postMessage({ type: "forge:select-exit" }, "*")
  } catch {
    /* iframe gone — silent */
  }
}

/**
 * Subscribe to picks from the iframe. Returns an unsubscribe fn.
 *
 * Origin gating: we don't gate on origin here because the same component
 * also hosts the screenshot reply (`forge:screenshot-response`), and the
 * payload itself is trivial (no secrets, no DOM injection). The bridge
 * is gated by being inside the user's own preview origin; anything else
 * posting `forge:select-pick` from a random tab can't reach this listener
 * via the iframe contentWindow.
 */
export function onSelectPick(
  cb: (pick: VisualEditPick) => void,
): () => void {
  const handler = (ev: MessageEvent) => {
    const data = ev.data
    if (!data || typeof data !== "object") return
    if (data.type !== "forge:select-pick") return
    const source = typeof data.source === "string" ? data.source : null
    const tag    = typeof data.tag === "string" ? data.tag.toLowerCase() : ""
    const text   = typeof data.text === "string" ? data.text.slice(0, 80) : ""
    if (!tag) return
    cb({ source, tag, text })
  }
  window.addEventListener("message", handler)
  return () => window.removeEventListener("message", handler)
}

/**
 * Parse "relpath:line:col" → {path, line, col}, or null on bad shape.
 */
export function parseSource(source: string | null): {
  path: string
  line: number
  col:  number
} | null {
  if (!source) return null
  // Path may contain colons (rare on unix, possible on windows-style
  // paths), so split from the right: last two chunks are line and col.
  const parts = source.split(":")
  if (parts.length < 3) return null
  const col  = Number(parts[parts.length - 1])
  const line = Number(parts[parts.length - 2])
  const path = parts.slice(0, parts.length - 2).join(":")
  if (!path || !Number.isFinite(line) || !Number.isFinite(col)) return null
  return { path, line, col }
}

/**
 * Build the prompt anchor text for a visual edit. Kept short — the file:line
 * is the load-bearing signal; everything else is restated context that
 * re-bills on every later turn (CLAUDE.md §3). Trailing space invites the
 * user to type their instruction after.
 *
 * Stamped:    "Update <button> at `src/app/page.tsx:42`: "
 * Unstamped:  "Update <button> \"Sign in\" (source location unknown — please
 *              search): "
 */
export function buildVisualEditAnchor(pick: VisualEditPick): string {
  const parsed = parseSource(pick.source)
  if (parsed) {
    return `Update <${pick.tag}> at \`${parsed.path}:${parsed.line}\`: `
  }
  const snippet = pick.text ? ` "${pick.text}"` : ""
  return `Update <${pick.tag}>${snippet} (source location unknown — please search the repo): `
}

/**
 * Dispatch the prefill event so prompt-input pre-fills the editor with the
 * anchor text and focuses it at the end, ready for the user to type their
 * instruction.
 *
 * `visualEdit` metadata is included so a future revision of prompt-input
 * can attach a structured opencodeComment if it wants — current prompt-input
 * just uses `text`, which is already enough for the agent to locate the
 * file (file:line is in the leading sentence). Adding the structured route
 * later is non-breaking: this payload already carries the data.
 */
export function dispatchVisualEditPrefill(pick: VisualEditPick): void {
  const text   = buildVisualEditAnchor(pick)
  const parsed = parseSource(pick.source)
  const payload: VisualEditPrefillPayload = {
    text,
    visualEdit: parsed
      ? {
          path:      parsed.path,
          startLine: parsed.line,
          startChar: 0,
          endLine:   parsed.line,
          endChar:   0,
          tag:       pick.tag,
          text:      pick.text,
        }
      : undefined,
  }
  window.dispatchEvent(
    new CustomEvent<VisualEditPrefillPayload>("forge:prefill-prompt", { detail: payload }),
  )
}
