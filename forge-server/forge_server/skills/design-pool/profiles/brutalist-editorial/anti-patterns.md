# brutalist-editorial — Anti-patterns

## Hard NOs

- **Any border-radius > 0 on rectangular elements.** Brutalism is SHARP. Buttons, inputs, cards — all 0 radius.
- **Soft shadows, glow effects, gradients.** None. The aesthetic is pure flat color + thick borders.
- **Stock photography of any kind.** Real work, real screenshots, or no imagery.
- **Multiple accent colors.** ONE shocking primary per project. Mixing two violates brutalist purity.
- **Mid-saturation accents.** The accent is SHOCKING — fully saturated (#FF4D00, #FF0000, #FFE600). No pastels, no muted versions.
- **Smooth transitions / fades.** Snap transitions or none. Brutalism rejects polish.
- **Centered-everything layouts.** Brutalism is asymmetric. Centered hero = wrong profile.
- **Justified body text.** Left-aligned, ragged right. Always.

## Subtle drift to watch

- **Thin borders.** Borders here are 2-4px THICK. Default 1px borders look like every other website.
- **Hidden grid.** A visible grid (1-2px subtle lines) is a brutalist signature. Use `background: linear-gradient` or a grid overlay.
- **Polished hover states.** Brutalist hovers are INVERSIONS (text and bg swap) or POSITION shifts (translate 4px to simulate a press). Not opacity fades.
- **Standard tracking on huge headlines.** Display headlines are -0.04em or tighter. Default tracking on 100px+ type ruins it.
- **Sans-serif label CAPS without tracking.** Tiny CAPS labels need wide letter-spacing (+0.05em to +0.1em), not normal spacing.
- **Card padding too tight.** Brutalist cards have generous interior padding (32px+). Thick border + tight interior = looks crammed.
- **Round avatars in the middle of brutalist layouts.** Avatars CAN be round (it's the exception) but consider square avatars for full purity.

## Things people will ask for that violate the profile

- **"Make it more friendly."** No. The whole point is intentional roughness. If they want friendly, this is the wrong profile — switch to `playful-pastel` or `editorial-premium`.
- **"Round the corners a bit."** No. Even 2px radius breaks the aesthetic. The sharpness IS the design.
- **"Add gradients to the buttons."** No. Flat color, thick border, sharp edges.
- **"Multiple bright colors."** No. ONE shocking primary. Mixing two starts to look like consumer-startup branding, not brutalist.
- **"Smooth animation on hover."** No. Snap transitions (transition-duration: 0s or 100ms with no easing). Smooth fades belong elsewhere.

## When to switch profiles

- Building a B2B product or corporate site → almost any other profile fits better; this is for creative agencies and artistic statements
- Need polish and refinement → `editorial-premium` (premium polish) or `apple-marketing` (commercial polish)
- Need warmth → `playful-pastel` or `substack-warm`
- Building a product UI users need to navigate efficiently → `linear-product`, `vercel-dev`, or `notion-docs`

This profile is for sites that *should* slow you down. Use it for portfolios, manifestos, agency sites, art projects. Not for daily-use products.
