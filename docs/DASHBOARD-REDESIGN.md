# Dashboard redesign plan

Status: **built (R0 to R6 done).** Written 2026-10-24, built the same day. Clickable mock-ups of Home, Clips and Calendar (amber accent, dark mode): https://claude.ai/artifact/XffCRE4X6RM2oppfP2GZSG (private to your account). Reference studied: Opus Clip's web app (clip.opus.pro), read in your
Chrome while signed in (read only: nothing was changed or clicked that saves anything).

## 1. What Opus Clip's UI is made of (measured from the page, not guessed)

**Stack.** shadcn/ui on Tailwind, its standard **"zinc" dark theme** (the CSS variables match shadcn's defaults
exactly), font **Geist Sans** (Vercel's open-source font, SIL Open Font License). That matters: the look comes from
open building blocks, so we can build the same *feel* without copying anything that is Opus Clip's own.

| Token | Value | Where it is used |
|---|---|---|
| Page background | `#0E1015` | Main content area |
| Sidebar | `#1A1A1A`, 224 px wide, collapsible | Left navigation |
| Raised surface / input | `#18181B` (zinc-900), border `#27272A` | Search box, popovers |
| Border / divider | `#27272A` (zinc-800) | Everything |
| Text | `#FAFAFA` | Titles, nav items, active text |
| Muted text | `#9B9EA3` body, `#A1A1AA` labels | Body copy, section labels ("Create", "Post") |
| Primary button | `#FAFAFA` fill, `#18181B` text, 600 weight, radius 16 px (hero) / 6 px (normal) | "Get clips in 1 click", "Add account" |
| Tonal button | white at 10% (hover 15%), radius 6 px, 36 px tall | "Add credits", "Upload local video" |
| Chip | transparent, 1 px white-18% border, pill radius, white-70% text, 38 px | Tool shortcuts |
| Active nav item | zinc-800 at 50% + 1 px `#27272A` border, radius 10 px, 38 px tall | "Home" when selected |
| Accent | lime `#A8E21F`, 24 px bold | Only the virality score on each clip |
| Danger | `hsl(0 62.8% 30.6%)`; small red dots for unread | Notifications |

**Type.** Geist Sans throughout. Nav 14 px/500. Section labels 14 px muted. Card titles 16 px/500. Page titles about 24 px.
Radii: 6 px buttons, 8 px inputs, 10 px nav items, 16-24 px hero elements, full pills for chips.

**Layout patterns.**
- Left sidebar: account switcher on top, groups with small muted headings (**Create**, **Post**), utility links pinned
  to the bottom, a collapse button. Top bar on the right: notifications, usage, one primary action.
- **Clip grid** (a project page): six 9:16 thumbnails per row, under each a big lime score, a two-line title and small
  icon actions (schedule, download). Breadcrumb at top left, "Find keywords or moments ⌘K" search in the top bar,
  grid/list toggle and Filter on the right.
- **Calendar**: a seven-column month grid, 128 px day cells in zinc-800 at 50% with 1 px gaps; today is marked by a
  dot and a bold white label; title with "Schedule post" and "Upload local video" buttons; timezone chip on the right.
- Projects: collections row, then a project grid with an "Upload new video" dashed tile first.
- Dialogs: centred shadcn dialog with a close button (Social accounts).

**What we will not copy:** the Opus Clip name, logo, illustrations, marketing tiles or their lime as our brand. We take
the open system (shadcn zinc + Geist + a single accent) and make it ours: **CavemanCam amber `#FFB000`** as the one
accent, the same amber the clips already use, so the dashboard and the videos look like one brand.

## 2. What other dashboards and research add

Sources are mostly practitioner blogs; treat as guidance.
- Collapsible left sidebars are back for tools with many modules; one most-important number goes top left; show
  summaries first and details on demand ([SaaSFrame](https://www.saasframe.io/blog/the-anatomy-of-high-performance-saas-dashboard-design-2026-trends-patterns), [AdminLTE](https://adminlte.io/blog/25-best-dashboard-design-examples-for-2026-real-products-principles/)).
- Every empty state should say how to get data flowing, with a button; stale or failed data should be flagged on the
  component itself, not only in a log.
- Content calendars: month view for planning with colour-coded status and "+n more" per day, week view for timing,
  a sortable list view for bulk review, click an empty day to schedule, drag only posts that have not gone out
  ([Statusbrew](https://statusbrew.com/help/articles/content-editorial-calendar), [Ordinal](https://docs.tryordinal.com/calendar/overview)).
- Dark mode: keep text contrast high. Checked for our palette: `#9B9EA3` on `#0E1015` is about 7:1, amber about 10:1,
  both above the WCAG AA 4.5:1 minimum.

## 3. Our pages today, and what happens to each

Today: 8 top-level pages in a text nav, all styled inline in one Python file (`villomillo/web/templates.py`).

| Today | Becomes | Why |
|---|---|---|
| Overview | **Home** | Rebuilt around "what needs you" and the next posts |
| Research | **Research** | Same job, new table with thumbnails, grade bars and filters |
| Learning | **Insights** | Self-check, calibration, weekly review; Phase 6 analytics will live here |
| Clip studio | **Clips → In progress** tab | The live engine timeline stays, as one tab |
| Clips to review | **Clips → Waiting for you** tab | Removed as a page; review happens in a side drawer |
| Manual uploads | **Calendar** drawer + "To post by hand" list | Removed as a page |
| Posts | **Calendar** (month, week, list) | Removed as a page |
| Accounts | **Accounts** | Adds connection status (YouTube connected, TikTok not), audits, safety stops |
| (none) | **Settings** | Channels, the format recipe with a live preview, budgets, key status (never key values), engine status |

**Removed outright:** the unused rating endpoint `/api/rate` (you don't rate videos; the buttons are already gone), the
3-second polling page (In progress refreshes only while a job runs), and the single giant template file (split into
real template files plus one stylesheet).

## 4. The new app

**Sidebar** (224 px, collapsible to icons): channel switcher on top (CavemanCam / Solved clips), then
**Find**: Home, Research. **Make**: Clips. **Post**: Calendar, Accounts. **Learn**: Insights. Bottom: Settings, engine
status dot.
**Top bar:** breadcrumb, ⌘K command palette (jump to any page, clip, post or video), budget pill (Jev and Claude spend
against the caps), notifications (paused accounts, failed posts, failed looks), one primary action per page.

| Page | What it shows |
|---|---|
| **Home** | Top left: posts ready today and the next post time. A "Needs you" list (clips waiting, manual uploads due, paused accounts, failed posts). The pipeline as a funnel (found → graded → passed → clipped → approved → posted). The next 7 days as a strip. Recent clips. Engine and budget health. |
| **Research** | Table: thumbnail, title, channel, age, grade with a bar, gate badge, reasons on hover; filters (pass / borderline / fail, channel, date); self-check verdict card on top. |
| **Clips** | Tabs: In progress (live timeline, step progress), Waiting for you, Approved, Rejected. An Opus-style 9:16 card grid with the grade in amber, title, look badge, campaign-rule badge. Click a card for a right-hand **review drawer**: player, checks, campaign rules, the title options, Approve / Reject. |
| **Calendar** | Month / week / list. Post chips coloured by status (planned, to post by hand, dry run, uploaded, failed). Click a post for a drawer: copy options, copy buttons, clip file, Mark as posted, and Send now behind a confirmation dialog. Today marked like Opus. |
| **Accounts** | One card per channel and platform: connected or not, audit passed or not, route (automatic or manual), safety state with the reason, Pause / Resume, Connect (opens the existing sign-in flow). |
| **Insights** | Self-check trend, calibration chart, weekly Claude review, AI spend by month; later real views and the prediction-versus-reality view (Phase 6). |
| **Settings** | Channels and their rules, the format recipe with a preview frame, budgets, keys present or missing, engine connection. |

## 5. Design system (ours)

- **Tokens** as CSS variables in one stylesheet: background `#0E1015`, sidebar `#151518`, surface `#18181B`, border
  `#27272A`, text `#FAFAFA`, muted `#9B9EA3`, label `#A1A1AA`, accent `#FFB000` (only for grades, the primary action and
  progress), success `#4CC38A`, warning `#E3A54A`, danger `#E5484D`. Dark only: light mode is not planned.
- **Type:** Geist Sans, self-hosted (OFL). Scale 12 / 14 / 16 / 20 / 24 / 32. Numbers use tabular figures.
- **Spacing:** 4 px grid. Radii 6 / 8 / 10 / 16 / full.
- **Icons:** Lucide line icons, vendored as one SVG sprite (license to be confirmed before vendoring).
- **Components:** sidebar, top bar, buttons (primary, tonal, outline, ghost, danger), badge and chip, card, stat tile,
  table, tabs, segmented control, drawer, dialog (every irreversible action), toast, tooltip, skeleton loader, empty
  state with a button, calendar grid, clip card, video player, step progress, command palette.

## 6. How it is built

**Recommendation: keep FastAPI with server-rendered templates**, move the templates into real files
(`villomillo/web/templates/`), add `static/app.css` (the design system) and a small `static/app.js` (drawers,
dialogs, palette, partial refresh). No Node build step, nothing new to install, and the existing tests keep working.

Alternative considered: a React + shadcn/ui single-page app. It would match shadcn exactly, but it needs a Node
toolchain, a second codebase and a JSON API for every page. Not worth it for a single-user local tool.

## 7. Leftovers in the repository: done

The fork carried Clips Kitty's product pieces that VilloMillo does not use. With your OK they were removed in one
commit, so `git revert` of that commit brings all of it back:

- **Removed:** the Electron app (`ui/`), the Next.js app (`web/`), the website (`site/`), `whop-app/`, `packaging/`,
  `feedback-relay/`, `twitch-proxy/`; the Microsoft Store, Whop, release, mirror and launch documents and images; the
  installer, MSIX and sitemap scripts and the PyInstaller spec; the release, Pages, mirror, stats, label and image
  workflows; the Electron and website jobs in CI; and the tests that only checked those pieces against the engine.
- **Kept on purpose:** every Python engine folder. A static import trace from `main.py`, `server/` and `villomillo/` reaches
  `analysis`, `core`, `creator`, `gaming`, `llm`, `longform`, `multilingual`, `plugins`, `publish`, `remote_render`,
  `sdk`, `server`, `sources`, `sports`, `transcription`, `video` and `video_editor`, some only through lazy imports
  inside functions. Nothing there is deleted on a guess; the real-engine run you will do later is the last check.
- **Moved:** the engine's version, which lived in `ui/package.json`, is now `version.json` at the repository root.
- **Fixed on the way:** the repository's own checks flagged VilloMillo code that called FFmpeg by bare name and opened
  OpenCV captures directly (both now go through `core.binaries` and `video.capture`), `villomillo` was missing from the
  plugin guard's package list, and some user-facing messages still said "Clips Kitty".

## 8. Build order

| Step | Work | Done when | Status |
|---|---|---|---|
| R0 | Repository clean-up (only with your OK) | Engine clips a test video; all tests pass | Done with your OK. Tests pass here; the real-engine clip is yours to run. |
| R1 | Design system and app shell: tokens, fonts, icons, sidebar, top bar, base components, templates split out | Every existing page renders inside the new shell | Done |
| R2 | Home | Shows real counts, needs-you list and the next 7 days | Done |
| R3 | Clips: grid, tabs, review drawer, in-progress timeline | Approve and reject work from the drawer; old studio and review pages removed | Done (old addresses redirect) |
| R4 | Calendar: month, week, list, post drawer, manual-upload flow | Old posts and manual pages removed; posting flows unchanged | Done (old addresses redirect) |
| R5 | Research, Accounts, Insights, Settings | All pages on the new system | Done |
| R6 | Polish: empty, loading and error states, Ctrl K palette, keyboard shortcuts, laptop widths | Every page checked in the browser at 1366 and 1920 px; tests pass | Done: empty states on every page; drawer and search show a loading state; errors show a page inside the dashboard (JSON stays for scripts); `g` + letter, `/`, `[` and `?` shortcuts; Geist font vendored; content widens to 1560 px; reduced-motion respected. Checked at 1366 and 1920 px with demo data: no horizontal overflow, no script errors. |

Not built, by decision: light mode. The dashboard is dark only.

## 9. Decisions needed

1. Accent colour: amber `#FFB000` (matches the clips; recommended) or something else.
2. Dark only. Decided: no light mode, ever.
3. A logo or wordmark for CavemanCam, or a simple text wordmark for now.
4. R0 (repository clean-up): approved and done.
5. Whether to see clickable mock-ups of Home, Clips and Calendar before R1.
