# apple-marketing — Anti-patterns

## Hard NOs

- **Small hero headlines.** Apple hero is 80-128px. If your headline is under 48px, you've drifted toward a different profile.
- **Multiple competing CTAs in the hero.** ONE "Buy" button + ONE "Learn more →" text link. Apple's confidence comes from singular focus.
- **Stock illustrations.** Real product photography only. If you don't have it, use restrained typography on negative space instead.
- **Gradient CTAs.** Apple's primary CTA is solid blue. Period.
- **Feature grids in the hero.** Push features below the fold. The hero shows the product, full stop.
- **Marketing parallelism** ("Fast. Smart. Beautiful."). Apple writes singular sentences. "The most personal iPhone ever." Not "Faster. Smarter. Smaller."
- **Body weight 600.** Body is 400 — Apple's body is light. Save 600+ for headlines only.
- **Loose tracking on large headlines.** -0.04em or tighter. Default tracking on 80px+ type looks unfinished.
- **Cards with shadows + borders + gradients.** Apple's cards have NONE of these. They use color separation and large radii to define edges.

## Subtle drift to watch

- **Section gutters too tight.** 128-200px section vertical gaps. If sections feel adjacent, scale way up.
- **Not alternating light/dark sections.** Apple's marketing rhythm is white → black → white → black. Missing this rhythm makes the page feel monotone.
- **Product imagery without proper lighting.** Apple's product shots are rendered/photographed with realistic light, including a shadow under the product. Flat or floating product images break the polish.
- **Pricing not in mono.** Pricing tables use tabular monospace. Tabular-nums on `font-variant-numeric` is non-negotiable.
- **Pill buttons too small.** Apple's pill buttons are GENEROUS — 16-20px vertical padding, 28-32px horizontal. Tiny pills look like consumer-app buttons.
- **Subhead too long.** Hero subhead is ~10-15 words. If yours is two sentences, trim it.

## Things people will ask for that violate the profile

- **"Make it more colorful."** No. Apple marketing uses near-monochrome. Color comes from the product itself, not the page chrome.
- **"Add an animated hero."** Maybe — but only if it's the *actual product* moving (UI animation), not generic gradient mesh motion.
- **"Stack the value props with the hero so they're all visible."** Wrong profile — that's editorial-premium or a 'features-grid' page. Apple's hero has nothing competing for attention with the product.
- **"Use our brand color for the primary button."** Apple uses blue regardless of product. Brand color goes in the logo, maybe small accents. The primary button stays blue.

## When to switch profiles

- Building a marketing site for a B2B SaaS, not consumer hardware → `editorial-premium`
- Building docs → `notion-docs`
- Building a product UI (not marketing) → `linear-product` or `vercel-dev`
- Building consumer with warmth → `playful-pastel`
- Brand needs strong identity/gradient → `arc-experimental`
