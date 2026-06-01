# playful-pastel — Anti-patterns

## Hard NOs

- **Dark mode.** This profile is light-mode only. The warmth IS the design. If a user insists on dark, they want a different profile (linear-product probably).
- **Pure white backgrounds.** Background is warm off-white (`#FCF8F3`). Pure white loses the signature warmth.
- **Saturated neon accents** (electric blue, hot pink, neon green). The accent is warm terracotta or sage — saturated but earthy.
- **All-pastel button.** Buttons are saturated colors (accent terracotta or sage), not pastels. Pastels are PANEL backgrounds, not CTA fills.
- **Serif headlines.** Sans throughout. Inter or Söhne. Pull-quotes can use serif italics as a flourish, but the main type is sans.
- **Multiple pastels stacked.** Pick ONE pastel for the page (peach OR sky OR sage). Cycling between them in adjacent sections turns the page into a candy shop.
- **AI-startup gradients.** Especially purple-pink. Warmth here comes from cream + earthy accents, not gradients.
- **Stock illustrations of diverse-people-celebrating.** Use real product imagery, or abstract pastel shapes, or no imagery.

## Subtle drift to watch

- **Pure black text (`#000`).** Use `#2A2520` (warm dark) — pure black against cream reads cold.
- **Tight line-height.** This profile uses 1.6 for body, 1.1 for hero. Default tailwind line-heights are sometimes too tight.
- **Small radius.** Default radius is 12px here, not 6-8. Cards go to 20px, hero panels to 32px. Anything tighter starts feeling like a different profile.
- **Cold gray for borders.** Borders are warm (`#E8DECD`), not cool gray. If you see `border-gray-200`, replace it.
- **Pill-shaped buttons.** Still no. 12px radius, not 9999.
- **Compact spacing.** This profile uses GENEROUS spacing (96-128px section gutters). If sections feel cramped, scale up.
- **Corporate parallel headlines** ("Fast. Simple. Powerful."). Conversational copy beats parallelism here.

## Things people will ask for that violate the profile

- **"Add dark mode."** This profile doesn't have one. The warmth is the point. If they need dark, switch profile.
- **"Make it pop more."** No. "Pop" usually means add saturation or shadows, both of which break the gentleness. Confidence here is restraint.
- **"Use my brand color as the accent."** Fine, but it should sit in the warm-earthy register. If their brand color is electric blue or neon pink, this profile is wrong — recommend `arc-experimental` (which embraces vibrant identity).
- **"Add more pastels for color."** No. One pastel per section, maximum. More pastels = more candy-shop.

## When to switch profiles

- Need dark mode → `linear-product` or `vercel-dev`
- Need editorial premium feel (serif) → `editorial-premium`
- Building a fintech product → `mercury-fintech`
- Building a developer tool → `vercel-dev` or `linear-product`
- User wants vibrant identity that's NOT pastel → `arc-experimental` (gradient identity) or `brutalist-editorial` (one shocking primary)
