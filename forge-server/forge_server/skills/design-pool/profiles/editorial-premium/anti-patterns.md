# editorial-premium — Anti-patterns

Things this profile refuses. design-critic should reject any output that contains these. If the main agent reaches for one of these, it's not following the profile.

## Hard NOs (reject on sight)

- **Purple / pink / blue-to-purple CTA gradients.** This is the single biggest tell of a generic SaaS template. The primary button is `#1A1818` (the text color) on the cream background. No gradient anywhere on the page.
- **Pure black (`#000`) on pure white (`#FFF`).** Always `#1A1818` on `#FAF8F4`. The warmth matters — without it the design feels clinical, not editorial.
- **AI-startup imagery.** Glowing orbs, abstract neural-network swooshes, gradient meshes, "AI for X" hero images. None of it. Real product screenshots or no imagery at all.
- **Glassmorphism, neumorphism, claymorphism, aurora gradients.** Any "morphism" trend style. This profile is FLAT.
- **More than one accent color.** Pick `accent` (terracotta) OR `accent_alt` (sage). Never both in the same project. Never both in the same component.
- **Lorem Ipsum.** Write real copy. If the agent doesn't know what to write, ask the user — don't pad with Lorem.
- **Trust-badge clutter.** "256-bit encryption", "SOC 2 Type II", "GDPR compliant" badges plastered on the login page. One subtle line at most. Editorial designs trust the design itself to signal premium.

## Subtle drift to watch (design-critic should flag these)

- **Body text in serif.** Display is serif. Body is sans. Mixing them up makes the page feel like a 2010 blog.
- **Pill-shaped buttons** (`border-radius: 9999px` on rectangular buttons). Pills feel modern-SaaS. Editorial uses 6px radius. Same for inputs.
- **Drop shadows on buttons or cards.** This profile is flat. If a thing needs to "pop", use border or spacing, not shadow.
- **Stacking marketing copy next to the form on a login page.** The form gets its own column. The other column is ONE editorial element (testimonial, brand statement, image). Not three value-prop cards.
- **Sans-serif display.** If someone is reading this and it says "use Inter for headlines because Playfair Display is dated," they're wrong for this profile. The serif is the point.
- **OAuth buttons above the email field.** Email/password is the primary path. OAuth ("Or continue with...") is the alternative, below. Order matters.
- **More than one CTA per section.** Editorial designs are confident — one primary action per major section.
- **"By signing up you agree to our..."** legal text in tiny gray sans below the button — fine. But not three lines of disclaimers, not a checkbox that says "I agree".
- **Browser default `:invalid` focus rings** (the orange/red one). Override with `:focus-visible { outline: 2px solid #1A1818; outline-offset: 2px }`.

## Things people will ask for that violate the profile (push back gently)

- **"Add dark mode."** This profile is light-mode only. The warm cream IS the design. Dark mode is a different profile (look at `linear-product`). If the user insists on dark for a marketing site, recommend `linear-product` instead.
- **"Make the CTA pop more."** No. The button is black on cream and that IS confident. "Pop more" usually means "add a gradient" or "make it bigger". Editorial confidence is restraint.
- **"Add some animations."** Subtle 200ms transitions on hover are fine. Scroll-triggered hero animations, parallax, anything moving for its own sake — no. The design's confidence comes from stillness.

## When to switch profiles entirely

If the user's request is fundamentally about a product UI (dashboard, app screens, dense data, keyboard-driven workflow), this profile is wrong. Switch to `linear-product`. The editorial palette and serif typography will fight a product surface.

If the user wants something playful, joyful, or consumer-friendly (think Loops, Notion's marketing, Linear's loops page), this profile is wrong. We don't have a `playful-modern` profile yet — fall back to ui-ux-pro-max's Claymorphism + bright pastels combination.
