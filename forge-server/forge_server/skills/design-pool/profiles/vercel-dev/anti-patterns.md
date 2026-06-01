# vercel-dev — Anti-patterns

## Hard NOs

- **Warm / cream / beige palettes.** This profile is COOL TO MONOCHROME. Cream is editorial-premium territory.
- **Serifs.** Geist Sans + Geist Mono. No Playfair, no Georgia, no Tiempos.
- **Pastel accents.** The accent IS the inversion (white-on-black or black-on-white). Semantic colors (green/amber/red) appear only as 1-2px indicators, never as backgrounds.
- **Drop shadows on cards.** Borders carry depth. Shadows only on modals/popovers.
- **Stock illustrations of people-at-laptops.** Real product screenshots only. The product itself is the visual.
- **Loose letter-spacing.** Geist headlines use `-0.04em` to `-0.05em`. Anything looser feels wrong for this aesthetic.
- **Pill-shaped buttons.** 6px radius, period.
- **Gradient buttons or hero gradients.** The inversion is the design. If you reach for a gradient, you've drifted toward `arc-experimental`.

## Subtle drift to watch

- **Inter where Geist could be used.** Geist's proportions (especially the lowercase 'a' and 'g') are part of the signal. Fall back to Inter only if Geist isn't loaded.
- **Missing monospace where Vercel would use it.** Deploy IDs, commit hashes, URLs, durations, ms counts — all in mono. If you see these in proportional sans, it's drift.
- **Right-padding mismatch in inputs.** Input padding should match button padding exactly (8px 12px or 8px 14px) so they line up vertically when stacked.
- **Headline tracking too loose.** Defaults of `tracking-tight` aren't tight enough — explicitly use `-0.04em` for display sizes.
- **Bigger-is-better headlines.** Vercel uses restrained 4xl-5xl for hero, not 6xl+. Confident, not shouting.

## Things people will ask for that violate the profile

- **"Add some color."** No. The whole point is monochrome with the inversion. "Color" usually means "less Vercel, more Linear" — recommend switching profiles instead.
- **"Make it warmer."** Wrong profile — switch to editorial-premium.
- **"Add an illustration."** Use a product screenshot instead. If you don't have one, use a code snippet rendered as syntax-highlighted text. Stock illustrations are anti-this-profile.
- **"Light mode default."** Dark is the default. Light is the option. Vercel's marketing is light, the product is dark — both are valid, but dark is the more recognized signal.

## When to switch profiles

- Need warmth or editorial feel? → `editorial-premium`
- Need a colorful accent and personality? → `linear-product` (has the purple-blue), or `arc-experimental` (gradient identity)
- Building docs specifically? → `notion-docs` (more reading-optimized)
- Targeting consumers, not developers? → almost anything else; this profile reads "for engineers"
