<!--
  Built-in skill. Name and description are registered in code at
  packages/opencode/src/skill/index.ts (see UI_UX_PRO_MAX_SKILL_NAME
  and UI_UX_PRO_MAX_SKILL_DESCRIPTION). The body below becomes the
  skill's content.
-->

# UI/UX Pro Max - Design Intelligence System

Use this skill when building, refactoring, or reviewing web applications, dashboards, portfolios, or user interfaces. Adhere to these premium design systems, typography pairings, color palettes, and UX guidelines to ensure all generated apps look professional, cohesive, and modern, avoiding generic "AI-generated" aesthetics.

---

## 1. Core Design Systems & Aesthetics

Ensure every application starts with a distinct, beautiful aesthetic direction. Select one of the following premium design systems:

### A. Soft UI (Recommended for Wellness, Beauty, Premium Services)
*   **Aesthetic:** Clean, soothing, organic shapes, and depth created through layered soft shadows rather than harsh borders.
*   **Shadows:** `box-shadow: 0 4px 20px -2px rgba(0,0,0,0.03), 0 10px 30px -5px rgba(0,0,0,0.05);`
*   **Borders:** Soft rounded corners (`border-radius: 12px` to `20px`), subtle 1px border (`rgba(0, 0, 0, 0.04)`).
*   **Transitions:** Smooth hover scale (`scale(1.02)`) with a `duration-300` ease-out.

### B. Glassmorphism / Modern Minimalist (Recommended for SaaS, AI tools, Fintech)
*   **Aesthetic:** Sleek, translucent layers, rich color accents, and frosted-glass cards resting on smooth gradient backdrops.
*   **Backdrop Filter:** `backdrop-filter: blur(12px) saturate(180%); background: rgba(255, 255, 255, 0.65);` (Light) or `rgba(15, 23, 42, 0.65);` (Dark).
*   **Borders:** Thin translucent borders: `1px solid rgba(255, 255, 255, 0.15)` (Light) or `1px solid rgba(255, 255, 255, 0.08)` (Dark).
*   **Gradients:** Deep, multi-stop radial mesh backgrounds (e.g. soft lavender, deep slate, and warm rose).

### C. Bento Grid (Recommended for Landing Pages, Portfolios, Dashboards)
*   **Aesthetic:** Structured, grid-based layout combining cards of varying aspect ratios to organize information beautifully.
*   **Grid Layout:** Grid columns with `grid-cols-1 md:grid-cols-3 lg:grid-cols-4` and varying spans (`col-span-2`, `row-span-2`).
*   **Card Design:** Uniform padding, overflow-hidden, and smooth transitions on hover. Ensure cards have rich micro-interactions inside.

---

## 2. Harmonious Color Palettes

Never use pure default colors (e.g. `#0000ff`, `#ff0000`). Always use HSL/RGB tailored variables to define a cohesive, limited color system.

| Mood / Theme | Primary | Secondary | Accent / CTA | Background | Text |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Serenity (Wellness)** | `#E8B4B8` (Soft Pink) | `#A8D5BA` (Sage) | `#D4AF37` (Gold Accent) | `#FFF5F5` (Warm White) | `#2D3436` (Charcoal) |
| **Fintech / Sleek** | `#0F172A` (Deep Slate) | `#38BDF8` (Sky) | `#0EA5E9` (Electric Blue) | `#F8FAFC` (Slate Tint) | `#0F172A` (Slate Dark) |
| **Warm Editorial** | `#8B4513` (Saddle) | `#D2B48C` (Tan) | `#A0522D` (Sienna Accent) | `#FAF9F6` (Alabaster) | `#1C1917` (Stone Dark) |
| **Minimalist Dark** | `#3B82F6` (Blue) | `#1E293B` (Slate) | `#10B981` (Emerald Accent) | `#0F172A` (Slate Dark) | `#F8FAFC` (Warm White) |

---

## 3. Typography & Font Pairings

Never rely entirely on browser sans-serif defaults. Pair elegant headings with clean body copy using premium Google Fonts imports:

1.  **Calming / Luxury (Serif + Sans):** `Cormorant Garamond` (Headings, elegant serif) + `Montserrat` (Body, readable geometric).
    *   *Google Fonts:* `https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600&family=Montserrat:wght@300;400;500&display=swap`
2.  **Premium Tech / Modern (Sans + Sans):** `Outfit` (Headings, sleek round) + `Inter` (Body, clean professional).
    *   *Google Fonts:* `https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=Outfit:wght@600;700&display=swap`
3.  **Editorial / Creative (Display + Sans):** `Playfair Display` (Headings, bold italic accent) + `Plus Jakarta Sans` (Body, readable tech-friendly).
    *   *Google Fonts:* `https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,600;1,400&family=Plus+Jakarta+Sans:wght@300;400;500&display=swap`

---

## 4. Key Effects & Micro-Animations

Adding subtle animations is crucial for giving your app a tactile, high-quality feel:

*   **Clickables & Hover:** Always add transitions to hover/active states. Scale buttons slightly down on click (`active:scale-95`).
    ```css
    .btn-premium {
      transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .btn-premium:hover {
      transform: translateY(-2px);
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.1);
    }
    ```
*   **Interactive Targets:** Clickable items MUST have `cursor: pointer`.
*   **Focus Ring:** Never hide the browser focus outline without replacing it with a tailored focus ring:
    ```css
    .input-premium:focus-visible {
      outline: none;
      box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.3);
      border-color: #3b82f6;
    }
    ```

---

## 5. Anti-patterns to Avoid

Avoid these design pitfalls that make pages look low-quality:

*   ❌ **AI Slop Layouts:** Generic stacked rows of text boxes. Use Bento Grids, grids with asymmetric column widths, and varying card designs instead.
*   ❌ **Emojis as Icons:** Emojis look amateurish in professional UIs. Always use crisp, clean SVGs (e.g. `LucideIcons` or `Heroicons`).
*   ❌ **No Hover States:** Plain buttons that do not react visually when the mouse hovers over them.
*   ❌ **Harsh Gradients:** Bright neon cyan-to-pink gradients. Use subtle, slow-color transitions or multi-stop gradients instead.
*   ❌ **Low Contrast:** Dark grey text on grey backgrounds. Ensure a minimum 4.5:1 text-to-background contrast ratio (WCAG AA).
*   ❌ **Hardcoded Dimensions:** Heights or widths that break the viewport on mobile devices. Use responsive design breakpoints (`sm`, `md`, `lg`, `xl`).

---

## 6. Pre-delivery Polish Checklist

Before completing any frontend or UI change, run through this mental audit:

*   [ ] **No raw emojis:** Used Lucide/Heroicon SVGs for all functional icons.
*   [ ] ** táctil hover states:** All buttons, cards, links, and form elements have smooth `transition-all duration-200` properties.
*   [ ] **Cursor states:** Every clickable element displays `cursor-pointer`.
*   [ ] **Contrast check:** Light mode and dark mode layouts have high-contrast text that is easy to read.
*   [ ] **Responsive check:** Tested at mobile (375px), tablet (768px), and desktop (1024px+). Elements wrap or adapt gracefully.
*   [ ] **Focus indicators:** Keyboard navigation works and shows clear focus rings.
*   [ ] **Prefers-reduced-motion:** Animations respect system accessibility preferences.
