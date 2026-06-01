# mercury-fintech — Anti-patterns

## Hard NOs

- **Bright "trust" blue** (`#0066CC`, `#2563EB`, `#0080FF`). This is the 1990s-bank palette Mercury rejected. It's the single most identifiable tell of a generic-fintech template.
- **"256-bit encryption" badges, lock icons in the hero, FDIC trust seals plastered everywhere.** Trust comes from the design's confidence, not from badging anxiety.
- **Floating credit-card mockups tilted at 30°.** Every fintech marketing template uses this. Don't.
- **Vivid green (`#00DC82`) for incoming money + vivid red (`#FF0000`) for outgoing.** The whole stock-broker thing. Real money UIs let the `+` and `-` carry the direction. Numbers stay text-primary.
- **Gradients on CTAs, especially blue-to-purple.** Sage solid button or navy solid button only.
- **Sans-serif everything for marketing.** This profile uses serif display in marketing surfaces. (Sans for product UI is correct.)
- **Multiple competing accents.** Sage is primary; gold is occasional. Never both on the same screen at the same density.

## Subtle drift to watch

- **Mixing the marketing typography rules into product UI.** Serif in a transaction table is wrong. Marketing = serif display. Product UI = sans throughout.
- **Money in proportional sans.** Money belongs in monospace, always. So do account numbers, routing numbers, transaction IDs.
- **Pure-white backgrounds.** Background is warm cream (`#F5F1EA`), surfaces are white. Pure-white background loses the signature.
- **Pill-shaped buttons.** 8px radius. Not 9999.
- **Glow effects on accent buttons.** No glow. The button is a flat sage block. Hover = brightness shift, not shadow.
- **Charts/graphs in vivid traffic-light colors.** Use sage + gold + neutral grays. Maybe muted terracotta for negative. Never the candy-shop palette.
- **Emoji as icons for transaction categories.** Use Lucide icons in `text-secondary` color. Emoji in a financial UI reads cheap.

## Things people will ask for that violate the profile

- **"Make the balance pop."** No. The balance is in mono, text-primary, large. "Pop" usually means green-glow or accent-color, both of which break the confident-banking vibe.
- **"Add gradients to the CTAs to make them more modern."** Modern = sage solid. The era of gradient CTAs is the era this profile rejected.
- **"Use blue, customers expect blue from a bank."** This is the trope Mercury / Brex / Ramp explicitly broke. Cream + navy + sage IS the modern-fintech palette now.
- **"More security badges in the footer."** One sentence is enough. The design's restraint signals more trust than 5 stock badges.

## When to switch profiles

- Building a consumer payments app (Venmo-tier) → `playful-pastel` (warmer, friendlier)
- Building a crypto product → `linear-product` (cooler, more technical)
- Building a developer-facing financial API (Stripe-tier) → `vercel-dev` or `editorial-premium`
- Building a personal finance app with strong opinions → maybe `arc-experimental` if you have an identity gradient
