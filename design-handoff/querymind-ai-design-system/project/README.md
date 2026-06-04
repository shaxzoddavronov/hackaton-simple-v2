# QueryMind AI — Design System

> A calm, technical, data-forward design system for **QueryMind AI**, a
> self-hosted natural-language-to-SQL agent that connects to your databases,
> documents, and APIs and answers questions in chat — in your own language.

This repository is a **design system**: brand foundations, color + type tokens,
preview cards, and high-fidelity UI kits that let a design agent (or a human)
build well-branded QueryMind interfaces and assets — production or throwaway.

---

## What QueryMind AI is

QueryMind is a **chat-first analytics workspace**. The one-line product brief,
straight from the engineering surface map:

> Build a chat-first analytics workspace where users connect any of **13
> databases / APIs**, upload documents, and ask questions in their own
> language; every answer is a typed **UISpec** ready to render as KPI / bar /
> line / pie / table / dashboard, with citations and one-click export.

### Mental model (three nested concepts)

1. **User** owns N **Workspaces**.
2. A **Workspace** is a folder holding **Connections** (one per database/API,
   across 13 dialects), **Document Sources** (folders, URLs, IMAP, Slack/Telegram
   exports… crawled into a RAG index), and **Dashboards** (grids of saved
   questions).
3. A **Chat Session** belongs to a workspace, remembers its last connection, and
   each turn produces one assistant **Message** carrying a structured **UISpec**
   the frontend renders.

### The core interaction loop

The user types a question (Uzbek, Russian, or English) → the backend streams
**Server-Sent Events** as a 9-node LangGraph agent works (`session` →
`similar` → `node…` → `final`) → the `final` event carries a **UISpec** the
UI renders as one of seven typed cards:

| UISpec `type` | Renders as |
|---|---|
| `text_only` | Markdown answer (chitchat, clarify, slash-command output) |
| `kpi` | Big-number callout with optional delta + sparkline |
| `bar` | Categorical bar chart (grouped or stacked) |
| `line` | Time-series line chart |
| `pie` | Share / distribution chart (2–8 slices) |
| `table` | Typed columns + rows — the universal fallback |
| `dashboard` | A 12-column grid of nested UISpec children |

Answers can carry **citations** (RAG transparency chips) and a
**federation badge** (when one question fanned out across multiple connections).

### Surfaces the product implies (information architecture)

- `/login`, `/register`
- `/workspaces` — grid of workspace cards
- `/workspaces/[id]` — workspace detail with tabs: **Chat** (default),
  **Connections**, **Documents**, **Dashboards**, **Usage**, **Settings**
- `/workspaces/[id]/connections/[cid]/schema` — schema explorer
- `/workspaces/[id]/dashboards/[did]` — dashboard detail (saved-question grid)
- `/settings` — profile, preferences, MCP/Claude Desktop, API tokens
- `/admin/users`, `/admin/audit` — admin only

---

## Sources used to build this system

| Source | Reference | Access status |
|---|---|---|
| **GitHub repo** | `shaxzoddavronov/hackaton-simple-v2` → https://github.com/shaxzoddavronov/hackaton-simple-v2 | ⚠️ **Not accessible** to this build — see caveat below |
| **Backend capability map** | `BACKEND_SURFACE.md` (provided in full) | ✅ Primary source of truth |

> **⚠️ Important caveat — the frontend code was not accessible.**
> The GitHub repository above could not be reached during this build (it is
> private / not publicly fetchable, and a GitHub connection was not authorized).
> Everything here is therefore derived from **`BACKEND_SURFACE.md`**, which
> explicitly frames itself as *"the single source of truth a designer needs to
> start from scratch."* The visual layer (exact colors, type scale, component
> styling) is **defined fresh** here, anchored to the documented brand voice
> ("technical, calm, Neural Dark") and type stack — it is **not a pixel-for-pixel
> recreation** of the existing hand-built frontend.
>
> **To make this perfect, re-share the repo:** connect GitHub via the Import
> menu, or paste the relevant `app/`, `components/`, and `globals.css` files.
> I'll then reconcile the tokens and UI kit against the real source.

Readers with access should explore
`https://github.com/shaxzoddavronov/hackaton-simple-v2` to ground any
production work in the actual component implementations.

---

## CONTENT FUNDAMENTALS — how QueryMind talks

QueryMind's voice is **technical, calm, and precise** — a competent tool that
respects an analyst's time. It never hypes, never emojis, never exclaims.

**Tone & vibe**
- Engineer-to-engineer. Direct, unembellished, quietly confident.
- Calm under load: status and errors are stated plainly, never alarming.
- Trust through transparency — the product *shows its work* (the SQL it ran,
  the rows it read, the documents it cited).

**Person & address**
- Address the user as **"you"**; the product refers to itself in the third
  person or by feature name ("QueryMind", "the agent"), not "I".
- Imperatives for actions: *"Add connection"*, *"Test connection"*,
  *"Crawl now"*, *"Re-run all"*.

**Casing**
- **Sentence case** everywhere — buttons, headers, menus, tab labels.
  *"New workspace"*, not *"New Workspace"*.
- **UPPERCASE + letterspacing** reserved for tiny overlines / section eyebrows
  and table-column meta (e.g. `READY`, `PROFILING`).
- Code, identifiers, dialect names, and SQL keep their literal casing
  (`postgres`, `bigquery`, `SELECT`).

**Multilingual (uz / ru / en)**
- The agent mirrors the user's language in answers. UI chrome ships text
  bundles for Uzbek, Russian, English. Keep microcopy short and translatable —
  avoid idioms and puns. Example real user prompt:
  *"oxirgi 30 kunda eng faol foydalanuvchi"* ("most active user in the last 30 days").

**Numbers & data**
- Tabular figures, monospace, right-aligned in tables. KPIs lead with the
  number, label below.
- Deltas are signed and colored (`+12.4%` emerald, `−3.1%` rose).

**Errors & empty states**
- Every error string is **backend-sanitized** — treat it as opaque, user-safe
  text; never parse it, never leak that a resource exists (403 → "Not found").
- Empty states are matter-of-fact and point to the next action:
  *"No connections yet. Add a database or API to start asking questions."*

**Microcopy examples (verbatim from the product surface)**
- *"+ Add connection"* → wizard: *"Test connection"* → *"Save"*
- *"⭐ Star question"* → modal: *"Save as…"*
- *"↻ Rerun"*, *"↻ Run all"*, *"↓ Export"*
- Federation badge: *"Queried: pg-quiz · 12 rows, es-search · 30 rows"*
- Status verbs: `pending` → `profiling` → `ready`; `idle` → `harvesting` → `ready`

**Things the copy must never do** (hard product invariants)
- Never offer to *show raw credentials*.
- Never present SQL as editable — it's shown **read-only**; no "edit & re-run".
- Never advertise a "send raw SQL" power-user mode (defense-in-depth invariant).

---

## VISUAL FOUNDATIONS — the Neural Dark system

The canonical theme is **Neural Dark**: a cool near-black workspace where data
and the cyan signal accent do the talking. Everything is in
`colors_and_type.css` as raw tokens (`--qm-*`) plus semantic roles
(`--bg-1`, `--fg-1`, `--accent`, `--h1-*`…).

**Color**
- **Neutrals** are a cool, slightly blue near-black ramp (`#07090D` canvas →
  `#212C3B` pressed surface). Surfaces layer by lightening, not by shadow alone.
- **Brand accent** is **Neural Cyan** `#34DCCB` — query/phosphor/signal. Used
  sparingly: primary actions, active nav, focus rings, the live "thinking"
  pulse, links. One accent, high intent.
- **Semantic hues** map 1:1 to the product's status vocabulary: emerald =
  `ready`/ok, blue = `profiling`/`harvesting`/activity, rose = `error`, amber =
  `auth_error`/warning, slate = `pending`/`idle`. These hues are deliberately
  distinct from the cyan accent so a status never reads as a brand element.
- **Data-viz** uses an 8-step categorical sequence (cyan → blue → violet →
  amber → emerald → rose → teal → pink). Violet appears *only* as a data series,
  never as a UI background.

**Type**
- **Space Grotesk** — display & headings. Geometric, technical, a little
  characterful. Negative tracking on large sizes.
- **Inter** — body & UI. Neutral, legible, dense-friendly.
- **JetBrains Mono** — code, SQL, identifiers, and **all tabular data**
  (table cells, KPI values) for honest column alignment.
- Type scale is tuned for a dense app, not a marketing page: body 15px,
  captions 12px, h1 30px. (For slides/print, scale up accordingly.)

**Backgrounds**
- Flat layered surfaces — **no decorative gradients, no imagery, no textures**
  behind content. The one permitted "atmosphere" is a faint radial accent glow
  behind the chat composer / hero (cyan at ≤6% alpha) and an optional subtle
  dotted-grid on empty canvases. Never the purple-gradient AI cliché.

**Borders & hairlines**
- Borders carry structure (shadows are quiet in dark mode). Three weights:
  `--border-subtle` (1px hairline inside cards), `--border` (default), and
  `--border-strong` (focus track, active separators). 1px, slightly cool.

**Corner radii**
- `sm 6px` (chips, inputs, badges), `md 10px` (buttons, menu items),
  `lg 14px` (cards, panels, message bubbles), `xl 20px` (modals, the composer),
  `pill 999px` (status dots, tags). Consistent and moderate — never fully square,
  never marshmallow-round.

**Cards**
- Surface `--bg-2` over the `--bg-0`/`--bg-1` page, a `--border-subtle`
  hairline, `lg` radius, and `--qm-shadow-2` only when elevated/floating
  (menus, modals, hover-lift). Resting cards rely on the surface step + border,
  not shadow.

**Elevation / shadows**
- Three deep, low-spread shadows (`shadow-1/2/3`) for menus, popovers, modals.
- The accent gets a **glow** (`--qm-glow-accent`) for the focused composer and
  the live agent state — a soft cyan bloom, never neon.

**Hover / press / focus**
- **Hover:** surface steps up one level (`--bg-2` → `--bg-hover`); accent
  buttons go to `--accent-hover` (lighter cyan). No scale on hover.
- **Press:** surface → `--bg-active`; accent → `--accent-press` (darker); a
  subtle 1px translate-down or `scale(.985)` on buttons only.
- **Focus:** always a visible `--qm-glow-focus` ring (3px cyan @ 30%) — never
  removed for "cleanliness".

**Motion**
- Calm and quick: `--qm-dur` 200ms with `--qm-ease`. Entrances use
  `--qm-ease-out` (decelerate) — fades + 4–8px rises, no bounce, no spring.
- The agent's "thinking" state is the one persistent motion: a gentle 1.4s
  opacity pulse on the active node label + a 3-dot cyan typing indicator.
- Streamed answer text fades/rises in as it arrives. Respect
  `prefers-reduced-motion` — drop to instant.

**Transparency & blur**
- Used sparingly and purposefully: sticky headers and the command palette use a
  `backdrop-blur` over `--bg-0` at ~80% opacity. Tooltips/popovers are solid.
  No glassmorphism on resting content cards.

**Imagery**
- The product is essentially imagery-free — it renders *data*, not photos.
  Charts, schema trees, and monospace tables are the visual texture. Any future
  marketing imagery should read cool, dark, and technical (terminal/grid/graph
  motifs), never stock-photo people.

**Layout rules**
- App shell: fixed left **workspace rail** (icon-width, collapsible) + a
  contextual **sidebar** (sessions / connections / docs list) + main content.
  Sticky top bar with workspace switcher + tabs. The chat composer is **docked
  to the bottom**, full-width of the content column, with the similar-question
  chip rail above it.
- Content max-width ~860px for chat readability; dashboards and tables go
  full-width on a 12-column grid.
- 4px spacing base; generous vertical rhythm in chat, dense rhythm in tables
  and the schema explorer.

---

## ICONOGRAPHY

QueryMind uses a single **line-icon set** at a consistent stroke weight to keep
the technical-calm tone — no filled icons, no multicolor, no emoji in product
chrome.

- **Set:** [**Lucide**](https://lucide.dev) (1.5–2px stroke, rounded joins,
  24×24 grid). This is the documented choice for this system — see the caveat
  below.
- **Sizes:** 16px (inline, buttons, list rows), 20px (nav, toolbar), 24px
  (empty-state / feature). Stroke scales with size; never below 1.5px.
- **Color:** inherit `currentColor` — `--fg-2` at rest, `--fg-0` or `--accent`
  when active/hovered. Status icons take their semantic hue.
- **Emoji:** the product surface uses two emoji-as-glyph affordances in
  microcopy — **⭐** ("Star question") and the arrow glyphs **↻** (rerun) /
  **↓** (export) / **↑**. Treat these as *typographic glyphs*, not illustrative
  emoji; prefer the matching Lucide icon (`star`, `rotate-cw`, `download`) in
  rendered UI and reserve the glyphs for terse labels.
- **Status dots:** solid filled circles (pill radius) in the semantic hue, not
  icons — green/blue/rose/amber/grey per the status palette.
- **Dialect & source marks:** the 13 connection dialects and 9 doc-source kinds
  each want a recognizable mark. Use a neutral Lucide mapping (`database`,
  `file-text`, `globe`, `mail`, `folder`, `hard-drive`, `cloud`…) tinted in the
  brand neutral; real product brand logos (Postgres, Snowflake, Slack, etc.) may
  be substituted in production where licensing allows.

> **⚠️ Iconography caveat:** the actual icon set used by the frontend could not
> be confirmed (repo inaccessible). **Lucide** is selected as the closest fit
> for a Next.js + Tailwind app with a calm-technical voice and is the de-facto
> standard in that stack. If the real app uses a different set (Heroicons,
> Phosphor, Tabler…), swap the CDN link in the UI kits and update this section.
> UI kits load Lucide from CDN: `https://unpkg.com/lucide@latest`.

---

## INDEX — what's in this system

**Root**
- `README.md` — this file (product context, content + visual foundations, iconography, index)
- `colors_and_type.css` — design tokens: raw `--qm-*` palette/type + semantic roles (`--bg-1`, `--h1-*`…) + light-theme override
- `SKILL.md` — Agent-Skill manifest so this system can be used in Claude Code
- `BACKEND_SURFACE.md` — *(the provided source brief)* the backend capability map this system is built from. Drop the provided file into this folder to ship the skill self-contained.

**`preview/`** — Design System tab cards (colors, type, spacing, components, brand). Open any in the Design System tab.

**`assets/`** — logo lockups + brand marks used across the kits.

**`ui_kits/app/`** — the QueryMind **web app** UI kit (the chat-first analytics workspace): `index.html` (interactive click-thru), `README.md`, and JSX components (app shell, chat composer, message cards, UISpec renderers, connection cards, schema explorer, dashboards…).

> **Why only one UI kit?** `BACKEND_SURFACE.md` documents a single product
> surface — the Next.js web app. No marketing site, docs site, or mobile app was
> described, so this system ships one kit. Add more under `ui_kits/<product>/`
> if those surfaces exist in the repo.

---

## How to use this system

- **Throwaway artifacts (mocks, slides, prototypes):** copy assets out and build
  static HTML, referencing `colors_and_type.css` and the UI-kit components.
- **Production code:** read the tokens + foundations to design on-brand, and use
  the UI kit as a high-fidelity visual reference (it's cosmetic, not production
  code — reconcile against the real repo).
- Honor the hard invariants: SQL is read-only, credentials are never shown,
  status colors are reserved, the accent is used sparingly.
