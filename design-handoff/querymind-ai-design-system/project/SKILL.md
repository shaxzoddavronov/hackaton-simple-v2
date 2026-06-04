---
name: querymind-design
description: Use this skill to generate well-branded interfaces and assets for QueryMind AI, either for production or throwaway prototypes/mocks/etc. Contains essential design guidelines, colors, type, fonts, assets, and UI kit components for prototyping.
user-invocable: true
---

Read the `README.md` file within this skill, and explore the other available files.

If creating visual artifacts (slides, mocks, throwaway prototypes, etc), copy assets out and create static HTML files for the user to view. If working on production code, you can copy assets and read the rules here to become an expert in designing with this brand.

If the user invokes this skill without any other guidance, ask them what they want to build or design, ask some questions, and act as an expert designer who outputs HTML artifacts _or_ production code, depending on the need.

## Quick map

- `README.md` — product context, content & visual foundations, iconography, full index. **Start here.**
- `colors_and_type.css` — design tokens. Link this file and reference its semantic vars (`--bg-1`, `--fg-1`, `--accent`, `--h1-*`, etc.). Loads the three brand fonts from Google Fonts.
- `assets/qm-mark.svg` — the brand mark (font-free). Render the wordmark "QueryMind AI" in Space Grotesk alongside it.
- `preview/` — small specimen cards for every token group (type, color, spacing, components, brand).
- `ui_kits/app/` — interactive web-app UI kit + reusable JSX components (chat, UISpec renderers, connection cards, schema explorer…). Read its `README.md` to reuse pieces.
- `BACKEND_SURFACE.md` — the source capability map (drop the provided file in here if missing).

## Non-negotiables (product invariants)

- **Neural Dark** is the canonical theme; cool near-black surfaces, **one** cyan accent (`#34DCCB`) used sparingly.
- **SQL is read-only** — never an editable/re-runnable field.
- **Never surface raw credentials.**
- **Status colors are reserved** (emerald=ready, blue=activity, rose=error, amber=auth, slate=pending) — don't use them decoratively.
- Voice is **technical, calm, sentence-case**; address the user as "you"; no emoji in product chrome; multilingual (uz/ru/en).
- Icons: **Lucide**, 1.75 stroke. Data is **JetBrains Mono**, tabular.

## Caveat

This system was built from `BACKEND_SURFACE.md`, not the frontend source repo
(`shaxzoddavronov/hackaton-simple-v2`, which was not accessible). The visual
layer is a fresh interpretation of the documented brand, not a pixel recreation.
If you have repo access, reconcile tokens and the UI kit against the real code.
