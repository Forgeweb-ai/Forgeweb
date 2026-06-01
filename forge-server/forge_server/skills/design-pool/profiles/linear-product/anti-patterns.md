# linear-product — Anti-patterns

Things this profile refuses. design-critic should reject any output that contains these.

## Hard NOs (reject on sight)

- **Warm / cream / earthy palettes.** This profile is cool-toned. Cream backgrounds belong in `editorial-premium`. Linear-style is `#08090A` (near-black with a hint of cool) or `#FFFFFF` for light mode — both cool-neutral.
- **Serif typography anywhere.** No Playfair, no Tiempos, no Georgia. Inter (or Geist) for everything except code/mono. Mixing a serif in for "premium" feel makes this look mid-2010s.
- **Multiple primary buttons on one screen.** Linear-product is precise — there is ONE primary action per visible region. Two accent-colored buttons next to each other is a violation.
- **Drop shadows on cards.** This profile uses 1px borders for depth. Shadows are reserved for modals and popovers only, and they're sharp/short, not soft fluffy clouds.
- **Big colorful illustrations.** Anywhere. This profile's visuals are the *product itself* — a screenshot of the UI, an animated UI clip, a code block with syntax highlighting. Not abstract gradients, not vector illustrations of people, not isometric stylized graphics.
- **Hero copy that sounds like marketing.** "Scale your product ideas without limits" is editorial-premium copy at best, marketing-slop at worst. Linear-product copy is direct and short — *"Issue tracking for modern teams."* Period. One claim.
- **Spacing that feels airy.** Editorial-premium uses 96px section gutters. This profile uses 24-32px. If something feels "breathy and open", it's wrong for this aesthetic.
- **Stat cards with huge accent-colored numbers.** ($1.2M in 60px purple text). The numbers are in `text_primary` size 2xl or 3xl. The visual emphasis comes from being tabular and aligned, not from color.

## Subtle drift to watch (design-critic should flag these)

- **Pill-shaped buttons.** Same as editorial — radius is 6px, not 9999px. Pills feel consumer/SaaS, wrong for product.
- **Center-aligned text in product UI.** Center alignment is fine for the login card and modal headlines. Everywhere else (lists, tables, forms) is left-aligned.
- **Missing keyboard shortcut hints.** A signature of this aesthetic is `⌘K`, `Esc`, `Tab` hints next to interactive elements. If a user can do something with the keyboard, *show* the keyboard hint. This is a positive expectation — the *absence* of these hints is the drift.
- **Glow effects beyond focus rings.** The accent-color glow on focused inputs (`0 0 0 3px accent_subtle`) is fine. Glowing on hover, on buttons, on cards — no.
- **Tooltip explanations of UI elements.** Tooltips are for keyboard shortcuts and abbreviations. If a button needs a tooltip to explain *what it does*, the button is named wrong.
- **Light-mode shadows that work in dark-mode.** If you implemented both modes, shadows need different opacity (more aggressive in dark mode to be visible). Don't just port the same shadow values across.
- **Loose line-height in dense UI.** This profile is dense. `line-height: 1.5` is for prose. UI labels and table rows should be `1.3-1.4`. Lists of items use `1.2`.
- **Inter as Display variant not enabled.** Modern Inter has optical sizing via `font-variation-settings: "opsz" 32` for large sizes. Not using it makes large headlines look slightly off. If using Tailwind, make sure the font-family includes "Inter Display" as the first option.

## Things people will ask for that violate the profile (push back gently)

- **"Make it more colorful."** No. The whole point of this profile is restraint with one accent. "More colorful" usually means "less Linear, more Notion" — which means switching profile, not adding more colors.
- **"Add some warmth."** Don't. Warmth is editorial-premium's territory. If the user wants warmth, the profile is wrong; switch.
- **"Light mode should be the default."** Default is dark. Light is an option. Most product UIs in this aesthetic ship dark-first. If the user has a strong reason (accessibility, daytime workflows), light is fine — but offer it as an option, not the default.
- **"Add testimonials to the login page."** No testimonials on login. This profile assumes the user knows your product. Testimonials go on the marketing page (separately).
- **"Make the primary button bigger."** Same. The button is correctly sized. "Bigger" usually means "less confident" — confident UIs use small, precise buttons.

## When to switch profiles entirely

If the user's request is for a marketing site for a non-technical audience (consumer SaaS, services, brands), this profile will feel cold. Switch to `editorial-premium`.

If the user wants something joyful, friendly, illustrative (children's app, wellness, creative tool for non-developers), neither this nor editorial-premium fits. Fall back to ui-ux-pro-max with Claymorphism + bright pastels until we ship a `playful-modern` profile.

If the user explicitly wants brutalist, retro, or any deliberately rough aesthetic, neither profile fits. Use ui-ux-pro-max's Brutalism style entry.
