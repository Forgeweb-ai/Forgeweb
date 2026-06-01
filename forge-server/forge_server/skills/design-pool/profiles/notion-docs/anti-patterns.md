# notion-docs — Anti-patterns

## Hard NOs

- **Marketing CTAs in docs body.** Docs are for documenting. Marketing pages link OUT to docs, not the reverse. No "Sign up free" buttons mid-tutorial.
- **Hero illustrations on docs pages.** Pages start with the title, immediately. No marketing-feel intro graphics.
- **Colored accents on chrome.** The accent IS the text color (inverted button). Don't paint sidebars or buttons in brand colors — docs aesthetic is neutral.
- **Pill-shaped buttons.** 6px radius.
- **Wide content area.** Content max-width is 720-800px. Going wider hurts readability.
- **Serif typography.** Inter throughout. Serif in docs reads like 19th-century manuals.
- **Reinventing link styling.** Links are standard blue (`#2563EB` light / `#60A5FA` dark). 30 years of muscle memory — don't break it.
- **No code-block syntax highlighting.** If there's code, it's syntax-highlighted (Shiki / Prism). Plain text code reads like a notepad.
- **No table of contents on long pages.** Right-sidebar TOC is mandatory for pages over ~600 words.

## Subtle drift to watch

- **Sidebar items too tall.** Each sidebar item is 28-32px tall, not 48px. Dense nav is correct.
- **Missing "edit on GitHub" / "improve this page" link.** Docs feel alive when they invite contribution.
- **Code blocks without language labels.** Always label the language in the top-right of the block.
- **Inline code without 1px border.** The border is what makes inline code feel chip-like instead of just colored text.
- **No prev/next navigation at end of pages.** Hard to read sequentially without it.
- **Search not surfaced.** ⌘K search hint visible in top bar is a strong signal.
- **No breadcrumb.** Breadcrumb above page title shows where you are in the hierarchy.

## Things people will ask for that violate the profile

- **"Make it more branded."** Add brand color to: the brand mark only. Maybe the logo color in the favicon. Don't paint sidebars, buttons, or headings in brand colors. Docs aesthetic is shared across all docs sites — that's a feature.
- **"Add a colorful hero to the docs landing page."** Docs landing has a category grid, not a marketing hero. If they want a marketing site, that's `editorial-premium` or `apple-marketing` — separate from docs.
- **"Use our marketing font for docs."** No. Docs use Inter even if the marketing site uses a serif. Reading comfort > brand consistency in docs context.
- **"Add a chat widget to the docs."** Maybe, but tuck it into the corner and don't let it cover content. Most chat widgets are over-prominent.

## When to switch profiles

- Marketing site for the product → `editorial-premium`, `apple-marketing`, `vercel-dev` depending on audience
- Long-form essays / writer publication → `substack-warm`
- Internal-only handbook with personality → could stay here, or try `playful-pastel` if the company culture wants warmth
