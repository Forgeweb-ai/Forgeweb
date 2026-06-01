# arc-experimental — Anti-patterns

## Hard NOs (this profile is unusual — read carefully)

- **Generic AI-pink-purple template gradient.** The ENTIRE POINT of this profile is that the gradient is INTENTIONAL — picked deliberately by the user, used purposefully. If the gradient is the generic `from-purple-500 to-pink-500` Tailwind default, you've drifted into "generic AI startup template" — the failure mode this profile exists to avoid being mistaken for.
- **Multiple gradient directions in one project.** ONE signature gradient. Pick it. Use it consistently.
- **Gradient on every card / every button.** The gradient is reserved for: hero panel, primary CTA, brand mark. Maybe 1-2 places on a page. Not 20.
- **Pastel gradients.** This profile is BOLD. Saturated colors. Pastel gradients belong in `playful-pastel`.
- **Light mode as default.** Dark is the default. The gradient pops against dark surfaces. Light mode is allowed but secondary.

## Subtle drift to watch

- **Gradient with no rationale.** Every gradient choice should connect to the product's identity. "Why orange-pink-purple?" should have an answer ("warm energy of creative thinking", "the spectrum of dawn", whatever). If there's no answer, the gradient becomes template clutter.
- **Brand mark in solid color when it could use the gradient.** The brand mark / logo is one of the best places to use the signature gradient.
- **Hero panel without sharp typography contrast.** Gradient bg + thin/light type = looks weak. Use weight 700-800 on hero copy against the gradient.
- **Cards with their own little gradients.** Cards in this profile are monochrome dark surfaces. Gradient stays at the brand-identity level.
- **Mono font everywhere.** Mono is for SMALL flourishes (version chips, beta labels). Body text is sans.
- **Glow effects on regular UI elements.** Glow is allowed as part of gradient identity (around the hero, around the primary CTA) — but not on every hover state of every card.

## Things people will ask for that violate the profile

- **"Use my brand colors for the gradient."** Yes — but EXPLICITLY chosen. If their brand is generic blue, the gradient should be a thoughtful interpretation (deep-navy to electric-blue), not just the brand color twice.
- **"More gradients!"** No. One signature gradient, used in 1-3 specific places. More dilutes the identity.
- **"Make it brighter and more eye-catching."** This profile is already eye-catching. "More" usually means cheaper. Restraint is what separates this from generic AI-startup template.
- **"Add an illustration."** Maybe — but only if it's a unique brand illustration that the user has, not stock. The gradient IS the illustration in most cases.

## When to switch profiles

- User wants restraint and editorial polish → `editorial-premium`
- User wants developer-product feel → `vercel-dev` or `linear-product`
- User wants warmth → `playful-pastel`
- User wants pure no-gradient minimalism → `editorial-premium` or `notion-docs`
- User wants intentional roughness/anti-design → `brutalist-editorial`

This profile is for products where IDENTITY matters more than convention. Arc Browser broke browser convention with identity. Granola is a meeting tool with strong gradient identity. If the product is conventional and needs to feel familiar, switch profiles.
