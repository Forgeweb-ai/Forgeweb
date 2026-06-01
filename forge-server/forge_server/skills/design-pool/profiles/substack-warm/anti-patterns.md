# substack-warm — Anti-patterns

## Hard NOs

- **Sans-serif body text.** This is THE defining feature. Body in sans = editorial-premium. Body in serif = this profile. If you reach for Inter for body text, you've drifted.
- **Pure white background.** Background is warm parchment (`#FBF8F1`). Pure white loses the reading warmth.
- **Multi-column layouts.** Single column, max-width 640-680px. Newspaper-style columns belong in a different aesthetic.
- **Sidebars in the article.** Sidebars distract reading flow. If "related articles" matter, put them at the END of the article, not floating beside it.
- **CTAs mid-article.** "Subscribe now!" buttons interrupting the read. End-of-article subscribe block is fine. Mid-article is hostile.
- **Marketing chrome.** No giant navigation bars, no announcement banners. The article is the design.
- **Pull quotes with giant fancy quotation marks at 80px.** Quote design is: italic serif, indented with a 3px left border. Not flair.
- **Small base font size.** 19px is the minimum, 20-21px is better. Reading at 14-16px in serif feels cramped.

## Subtle drift to watch

- **Pure black text (`#000`).** Use `#2C2418` (warm dark brown). Pure black against parchment reads cold.
- **Underlines hidden on hover.** Links are underlined ALWAYS. This is a 30-year-old reading affordance. Don't be clever.
- **Justified text.** Justified text introduces irregular spacing. Use left-aligned ragged-right — much more readable.
- **Line-height too tight.** 1.7-1.8 for body prose. Default Tailwind line-heights (1.5) feel cramped here.
- **Missing dropcap on first paragraph.** Optional but signature — a single large dropcap on the opening paragraph immediately signals "editorial reading" to the visitor.
- **Author photo too large.** Author photo is 32-40px circular in byline. If it's bigger, it competes with the title.
- **Aggressive radius.** Almost everything is 0-4px. Cards (rare) at 4px. Buttons at 4-6px.

## Things people will ask for that violate the profile

- **"Make the body text easier to scan."** No. This profile is REGISTRATIONS-for-reading. Scannable = lists, headers, short paragraphs — which is what `notion-docs` is for. If they want scanning, wrong profile.
- **"Add a colorful hero to the publication home."** No. Publication home stacks article cards. The "hero" is the publication name in display serif, restrained.
- **"Use sans-serif for body — it's more modern."** No. Sans-serif body in this aesthetic feels like a blog post in 2008. The serif IS the modernity here.
- **"Add a sidebar with author info."** End of article, not sidebar. Sidebar breaks single-column reading flow.

## When to switch profiles

- Building docs (not essays) → `notion-docs` (different reading mode — reference vs prose)
- Building marketing site for a publication → `editorial-premium` (similar warmth, but sans body, more landing-feel)
- Building a writer's portfolio that's mostly visual → `brutalist-editorial` for creative writers, `editorial-premium` for traditional
- Building a Medium-competitor (designed product) → `editorial-premium` is closer; this profile is for SOLO PUBLICATIONS
