---
name: design-pool
description: Curated library of complete design profiles (palette + typography + layout + anti-patterns) that the design-analyst subagent picks from. Each profile is one "design language" — Apple/Stripe-editorial, Linear-product, etc. Use this BEFORE generating any UI so output adopts a coherent design instead of synthesizing an average.
---

# design-pool

A library of code-ready design profiles. One profile = one internally coherent design language with real palette tokens, real typography pairings, real layout patterns, and an anti-pattern list. The `design-analyst` subagent (Claude Sonnet 4.6) reads from here to pick the right profile for the user's request and emit a spec the main agent translates to code.

## When to use this skill

The design-analyst subagent invokes this skill for **any** UI work: landing pages, dashboards, forms, components, redesigns. The main coding agent does not invoke this directly — it gets a spec from design-analyst and codes against that spec.

If you are the main coding agent and you find yourself reading this file directly, **stop**. Invoke the design-analyst subagent via the task tool instead. Generating UI without going through design-analyst leads to "average of training data" output — exactly the purple-gradient-SaaS-template failure mode this skill exists to prevent.

## The two-step flow

```
USER request: "make me a login page, minimal and clean"
       │
       ▼
  Main agent recognises UI work
       │
       ▼
  Invokes design-analyst subagent  ← THIS skill
       │
       ▼
  design-analyst reads:
    1. /forge-skills/design-pool/INDEX.json     (lightweight catalog)
    2. The 1-3 candidate profile.json files     (full specs)
    3. Their anti-patterns.md files             (what to refuse)
       │
       ▼
  design-analyst emits structured spec:
    {
      "profile": "editorial-premium",
      "rationale": "...",
      "tokens":   { palette, typography, layout, ... },
      "anti_patterns_to_watch": [...]
    }
       │
       ▼
  Main agent receives spec, generates code against THOSE tokens.
```

## Directory layout

```
/forge-skills/design-pool/
├── SKILL.md                    ← this file
├── INDEX.json                  ← catalog (id, archetype, keywords, 1-line desc)
└── profiles/
    ├── editorial-premium/
    │   ├── profile.json        ← full design spec (palette, type, layout, components)
    │   └── anti-patterns.md    ← what this style refuses
    └── linear-product/
        ├── profile.json
        └── anti-patterns.md
```

## Profile selection rules (for design-analyst)

When picking a profile for a user request:

1. **Read INDEX.json first.** It's small. Don't read every profile.json upfront.
2. **Match on keywords + mood, not product category.** "Make me a SaaS dashboard" doesn't mean `editorial-premium` is wrong — Linear is a SaaS dashboard and uses `linear-product`. Match on the *feel* the user wants.
3. **If the user's prompt is style-ambiguous** ("make me a login page" with no style signal), ask ONE clarifying question before picking. Suggested options: "editorial / product / playful / brutalist". Never default to trend styles just because they're popular.
4. **Composing two profiles is allowed** for hybrid intents (e.g. "editorial but with developer-tool density" → editorial-premium palette + linear-product spacing/density). Cite both in `profile` as `"editorial-premium+linear-product"`.
5. **The output spec must reference tokens, not values.** Don't return `{"primary": "#FAF8F4"}` — return `{"primary": "palette.background"}` so the main agent uses CSS variables / Tailwind tokens, not hard-coded colors.

## Why this layer exists (don't skip it)

The `ui-ux-pro-max` skill has 67 styles, 96 palettes, 57 typography pairings. Picking from a matrix of disconnected pieces produces average designs — coherent-on-paper, generic-in-practice. v0's edge over generic-SaaS-template output is that it adopts one *complete* design language end-to-end. This pool gives the agent that same advantage.

For niche cases not covered by any profile (e.g. "make me a 1990s GeoCities tribute"), `design-analyst` may fall back to `ui-ux-pro-max` for raw style+palette+type lookups. That fallback is intentional and acceptable — but for any mainstream UI request, prefer a profile.

## Growing the pool

New profiles are added by dropping a folder under `profiles/` with `profile.json` + `anti-patterns.md`, then appending an entry to `INDEX.json`. No code change needed. The design-analyst picks up new profiles on next session.

Profile drift is a real concern — "looks like Linear" in 2024 may not match Linear in 2026. Treat profiles like software dependencies: versioned (`profile.json` carries a `version` field), with changelog.
