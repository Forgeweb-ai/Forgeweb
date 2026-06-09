/**
 * Forge brand SVGs — Mark, Splash, Logo.
 *
 * Replaces the opencode-branded equivalents from `@opencode-ai/ui/logo`
 * (`Mark`, `Splash`, `Logo`) which render an outlined-square mark that
 * leaked into Forge surfaces: app boot splash, network-error fallback,
 * the "new session" empty state, etc.
 *
 * All three share the same pixel-art "F" silhouette so the brand reads
 * consistently from the 16px mark all the way up to the home-page
 * wordmark. The shapes use `currentColor` so any consumer can theme
 * them via `class` (e.g. `text-text-strong`, `opacity-50`).
 *
 * Why a single file: keeps the brand assets in one place — if we ever
 * rebrand (or want to swap in a designed SVG), there's one edit site.
 */
import type { ComponentProps } from "solid-js"

/**
 * Compact square mark — the equivalent of opencode's `Mark`. Used for
 * empty states / inline brand affordances at ~16-40px. 6px grid pixel-art
 * "F" inscribed in a 24×24 viewBox so the strokes stay crisp at any
 * power-of-two scale.
 */
export const ForgeMark = (props: { class?: string; style?: ComponentProps<"svg">["style"] }) => (
  <svg
    viewBox="0 0 24 24"
    fill="currentColor"
    xmlns="http://www.w3.org/2000/svg"
    classList={{ [props.class ?? ""]: !!props.class }}
    style={props.style}
    aria-label="Forge"
  >
    {/* F glyph on a 24×24 grid, 3px stroke width.
        Left vertical: x=3..6, y=3..21 (full height).
        Top bar:       x=3..21, y=3..6.
        Middle bar:    x=3..15, y=10..13. */}
    <rect x="3"  y="3"  width="18" height="3" />
    <rect x="3"  y="3"  width="3"  height="18" />
    <rect x="3"  y="10" width="12" height="3" />
  </svg>
)

/**
 * Boot / loading splash — equivalent of opencode's `Splash`. Same "F"
 * mark but at the slimmer 80×100 aspect opencode used, so existing
 * call sites that pass class="w-16 h-20" or "w-12 h-15" still look
 * proportional.
 */
export const ForgeSplash = (props: Pick<ComponentProps<"svg">, "ref" | "class" | "style">) => (
  <svg
    ref={props.ref}
    viewBox="0 0 80 100"
    fill="currentColor"
    xmlns="http://www.w3.org/2000/svg"
    classList={{ [props.class ?? ""]: !!props.class }}
    style={props.style}
    aria-label="Forge"
  >
    {/* 80×100 frame, 10px grid. F glyph:
        Left vertical: x=10..30, y=10..90.
        Top bar:       x=10..70, y=10..30.
        Middle bar:    x=10..55, y=45..60. */}
    <rect x="10" y="10" width="60" height="20" />
    <rect x="10" y="10" width="20" height="80" />
    <rect x="10" y="45" width="45" height="15" />
  </svg>
)

/**
 * Full "FORGE" pixel-art wordmark — equivalent of opencode's `Logo`.
 * Same 6px grid we already use in pages/error.tsx; this file becomes
 * the single source of truth.
 */
export const ForgeLogo = (props: { class?: string }) => (
  <svg
    viewBox="0 0 144 42"
    fill="currentColor"
    xmlns="http://www.w3.org/2000/svg"
    classList={{ [props.class ?? ""]: !!props.class }}
    aria-label="Forge"
  >
    {/* F */}
    <rect x="0"   y="6"  width="24" height="6" />
    <rect x="0"   y="6"  width="6"  height="30" />
    <rect x="0"   y="18" width="18" height="6" />
    {/* O */}
    <rect x="30"  y="6"  width="24" height="6" />
    <rect x="30"  y="30" width="24" height="6" />
    <rect x="30"  y="6"  width="6"  height="30" />
    <rect x="48"  y="6"  width="6"  height="30" />
    {/* R */}
    <rect x="60"  y="6"  width="18" height="6" />
    <rect x="60"  y="6"  width="6"  height="30" />
    <rect x="60"  y="18" width="18" height="6" />
    <rect x="72"  y="6"  width="6"  height="12" />
    <rect x="72"  y="24" width="12" height="12" />
    {/* G */}
    <rect x="90"  y="6"  width="24" height="6" />
    <rect x="90"  y="30" width="24" height="6" />
    <rect x="90"  y="6"  width="6"  height="30" />
    <rect x="108" y="18" width="6"  height="18" />
    <rect x="102" y="18" width="12" height="6" />
    {/* E */}
    <rect x="120" y="6"  width="24" height="6" />
    <rect x="120" y="30" width="24" height="6" />
    <rect x="120" y="6"  width="6"  height="30" />
    <rect x="120" y="18" width="18" height="6" />
  </svg>
)
