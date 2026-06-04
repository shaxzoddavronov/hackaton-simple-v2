# QueryMind AI — Web App UI Kit

A high-fidelity, interactive recreation of the **QueryMind AI** web app: a
chat-first analytics workspace. This kit is *cosmetic* — it fakes data and
agent behavior — but the visuals, layout, and interaction feel are built to be
pixel-faithful to the documented product so you can lift components into mocks
or use them as a reference for production work.

> ⚠️ Built from `BACKEND_SURFACE.md`, not the frontend source (the repo was not
> accessible — see the root `README.md` caveat). The visual layer is defined
> fresh per the Neural Dark brand notes, not recreated from the real code.

## Run it

Open `index.html`. It's a single-page React app (Babel-in-browser) that boots
to the **login screen** → sign in (any values) → lands in the workspace.

## What's interactive

- **Login / register** — toggle between modes; "Sign in" enters the app.
- **Workspace rail** — switch between workspaces (Growth Analytics / Support Ops / Finance).
- **Chat** (default tab) — type a question or click a starter / similar-question
  chip. The agent streams its **node trace** (`route → plan → validate →
  execute → [federate] → render`) then renders a typed answer:
  - *"active users…"* → **KPI** card with delta + sparkline
  - *"revenue by region…"* → **bar** chart
  - *"compare … volume"* → **federation badge** + **table**
  Each answer shows read-only **SQL**, **citations**, and Star / Export / Copy tools.
  Type `/` to open the **slash-command** menu.
- **Connections** — connection cards with health dot, latency, status badge;
  **+ Add connection** opens the dialect-picker wizard with **Test connection**.
- **Documents** — RAG source list with status + "Crawl now".
- **Dashboards** — a 12-column dashboard grid (KPI + bar + table) with Run all / Schedule.
- **Usage** — stat cards + per-day LLM-calls bars.
- **Schema explorer** — open from a connection card's table icon: table tree +
  columns with dtypes, PK/FK, and sampled values.

## Files

| File | What it holds |
|---|---|
| `index.html` | Boots React + Babel + Lucide; mounts `<App/>` |
| `app.css` | All component styles (consumes `../../colors_and_type.css` tokens) |
| `data.jsx` | `Icon` (Lucide wrapper), status maps, viz palette, fake workspaces/connections/sessions, canned answers |
| `UISpec.jsx` | `StatusBadge`, `HealthDot`, `SqlBlock` + UISpec renderers: `KpiCard`, `BarChart`, `PieChart`, `DataTable`, `renderSpec` (incl. recursive `dashboard`) |
| `Chat.jsx` | `ChatView`, message components, agent-thinking stream, `Composer` with slash menu + similar chips |
| `Views.jsx` | `ConnectionsView` + `AddConnectionModal`, `DocumentsView`, `UsageView`, `DashboardsView`, `SchemaView` |
| `App.jsx` | `Login`, `WorkspaceRail`, `Sidebar`, `TopBar`, top-level `App` state/routing |

## Conventions & invariants honored

- **SQL is read-only** — shown with a `read-only` chip, never an editable field.
- **No raw credentials** — the password field is masked; nothing is echoed back.
- **Status colors are reserved** (emerald/blue/rose/amber/slate) and never used decoratively.
- **One accent** — Neural Cyan only for primary actions, active state, focus, links, the live pulse.
- **Icons** — [Lucide](https://lucide.dev) via CDN at a consistent 1.75 stroke. Swap the CDN link if the real app uses a different set.

## Reuse notes

Components export to `window` (Babel scripts don't share scope otherwise), so
files load in order in `index.html`. To reuse a renderer in a mock, copy
`colors_and_type.css`, `app.css`, `data.jsx`, `UISpec.jsx`, and the component
you need, then call e.g. `renderSpec(spec)`.
