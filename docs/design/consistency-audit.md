# UI consistency and usability audit

Status: findings · Owner: design council · Date: 2026-10-27 · Build audited: `frontend/` production build at `098c0c7`
plus uncommitted work in progress (served by `vite preview`, with the real backend and the offline `mock` provider).

## Method

- I registered through the UI, created the council "Market research team" in the builder, and started three
  research runs from Home. The mock provider completed all three.
- I captured every route at **1440×900** and **390×844**, in light and dark (`colorScheme` emulation). That is
  124 screenshots, in `/tmp/claude-0/design/shots/` and named `<page>_<d|m>-<light|dark>.png`. The `empty-*.png`
  files show each list before any data existed, and the `setup-*` and `builder-validation` files show validation
  states.
- On each page I also did the following:
  - ran axe-core 4.14 (desktop in both themes, mobile in light);
  - counted `h1` elements;
  - checked for horizontal overflow;
  - listed interactive targets under 24px (desktop) and under 40px (mobile);
  - pressed Tab 30 times and recorded any focused element that had no visible focus indicator;
  - recorded the computed font-size and weight of every text node.
- Raw metrics are in `/tmp/claude-0/design/all.json`.
- Not covered: dialogs (Connect provider, Add server, Create API key, Add webhook), toasts, live and streaming
  states, and the pending-approval state. The mock runs finish in about 1s. Routes added after this build
  (`/knowledge`, `/templates`) are also out of scope.

Severity:
- **P1**: breaks a spec or WCAG requirement, or blocks a task on some viewport. Fix before release.
- **P2**: a visible inconsistency or usability cost across several pages.
- **P3**: polish.

## Prioritised findings

| ID | Sev | Page(s) | Issue | Evidence | Fix |
| --- | --- | --- | --- | --- | --- |
| C-01 | P1 | All | **The root font-size is 14px, so every rem token renders 12.5% under spec.** `html { @apply text-base }` sets `font-size: 0.875rem` on `<html>`, and that changes the rem unit. Measured: body 14px but `text-xs` 10.5px (spec 12), `text-2xs` badges 9.6px (spec 11), `text-sm` labels 11.4px (spec 13), `text-xl` titles 17.5px (spec 20). Button heights are 28, 31.5 and 35px (spec 32, 36, 40), and inputs are 31.5px. This single bug causes most of the small-target and tiny-metadata findings. | `login_d-light.png`, `home_d-light.png` (computed: html 14px, submit button 31.5px) | `frontend/src/styles/index.css`: remove `text-base` from the `html` rule (leave the root at 100% = 16px) and put `@apply text-base` on `body`. Then re-check that the dense views (run rail, Gantt) still fit. |
| C-02 | P1 | Mobile, all app pages | **The bottom tab bar has Home, Runs, Councils and Skills, with no "More".** Connections, Insights and Settings have no mobile navigation. They are reachable only through the avatar menu (Profile, API keys, Ledger) or ⌘K. Spec §2: "Home, Runs, Councils, More". | `home_m-light.png`, `runs_m-dark.png` | `frontend/src/components/layout/AppShell.tsx`: make the 4th tab "More", opening a sheet that lists Skills, Connections, Insights and Settings. Mark it active when the path is in that set. |
| C-03 | P1 | Run workspace (<768px and <1280px) | **The council rail and inspector have no small-screen fallback.** The rail is `hidden md:block` and the inspector is `hidden xl:flex`. On phones you cannot focus an agent. On anything narrower than 1280px, "Inspect" on a timeline card selects an item that nothing displays. | `run-timeline_m-light.png` (no rail, no inspector) | `frontend/src/routes/RunWorkspace.tsx`: below `xl`, render `<Inspector>` in a right drawer (`z-drawer`, 480px, full-width on mobile) opened by selection. Below `md`, add a "Council" button that opens `<CouncilRail>` in a sheet. |
| C-04 | P1 | Run workspace, mobile | **The intervention composer is hidden behind the fixed bottom tab bar.** The workspace is `h-[calc(100vh-3rem)]`, ignoring the 56px tab bar, so the bar's second row (the message input and Send) sits under it. In the run header, the mode badge overlaps the budget meter and the topic truncates to "Compare ope…". | `run-timeline_m-light.png` | `RunWorkspace.tsx`: use `h-[calc(100dvh-3rem-3.5rem)] md:h-[calc(100dvh-3rem)]`. In `components/run/parts.tsx` (`InterventionBar`), use one row on mobile: the target select goes into a popover, then the input and Send. In the run `Header`, stack the meters below the title under `sm` instead of beside it. |
| C-05 | P1 | All (session) | **Any failure of `/auth/me` while the app boots signs the user out.** A 429 (I hit it at the default 120 requests/min while navigating quickly), a 5xx or going offline all clear the tokens and redirect to `/login`. | `set-profile_d-dark` in the first pass of `metrics.json` (console: 429, page = "Sign in") | `frontend/src/stores/session.ts` `bootstrap()`: call `auth.clear()` only for `ApiError.status === 401`. For anything else, keep the session and render `ErrorState` with Retry, plus a countdown for `rate_limited` as spec §3.1 says. |
| C-06 | P1 | Login, Register | **Validation errors are raw backend strings in a banner, not on the fields.** "email: value is not a valid email address: An email address must have an @-sign.; password: String should have at least 12 characters". The fields get no `aria-invalid`, and there is no show/hide toggle on the password. The council builder already does this correctly with a summary and inline errors. The auth pages also have no `<main>` landmark (axe `landmark-one-main` and `region`). | `setup-register-validation-empty-submit.png`, `setup-register-validation-short-pw.png` | `frontend/src/routes/Auth.tsx`: map `error.details` (or field-prefixed messages) to `TextField error=`, and keep the Callout as a short summary. Validate on the client first (email format, 12 characters). Add a password visibility toggle to `TextField` (or a `PasswordField` in `components/ui`). Wrap `AuthCard` in `<main>`. |
| C-07 | P1 | All app pages | axe **label-content-name-mismatch** (serious): the avatar button shows "DA" but its name is "Account menu". Also axe **button-name** (critical) on mobile: the ⌘K trigger is icon-only there, because its text is `hidden md:inline`, and it has no accessible name. | axe on all 25 app routes (`all.json`) | `AppShell.tsx`: in `UserMenu`, set `aria-label={\`${initials}, account menu\`}` (or drop the aria-label and add sr-only text after the initials). On the palette trigger, add `aria-label="Search or jump to (⌘K)"`. |
| C-08 | P1 | Run workspace (Timeline, Output) | **Two `h1` elements per page.** Deliverable Markdown renders its own `# Title` as an `h1` next to the run-topic `h1`. | `all.json`: `run-timeline_*`, `run-output_*` have h1×2 | `frontend/src/components/ui/Markdown.tsx`: add a `headingOffset` prop (default 1) that maps h1→h2 … h5→h6 in `components`, and use it everywhere agent or skill Markdown is embedded. |
| C-09 | P2 | All list and detail pages | **Six different page container widths**, so content jumps left and right as you navigate: Home and the builder 880px; council detail `max-w-5xl`; Runs, Councils, Skills and Connections `max-w-6xl`; Settings `max-w-3xl`; Insights and Audit `max-w-[96rem]` (left-hugging at 1440). The Users table is squeezed into 768px. | `home_d-light`, `runs_d-light`, `council-detail_d-light`, `set-users_d-light`, `insights_d-light` | Add `<Page width="form" \| "default" \| "wide">` to `components/ui/index.tsx` (880 / `max-w-6xl` / full), with gutters `p-4 md:p-6`. Use `form` only for single-column forms (Home, builder, Profile). Settings tables (Users, API keys, Webhooks) and council detail use `default`. |
| C-10 | P2 | Connections vs Settings | **Section sub-navigation sits in two different places.** Connections puts the tabs above the h1. Settings puts an eyebrow ("SETTINGS"), then the h1 and description, then the tabs. Insights and Audit have no tabs and use a chevron breadcrumb instead. | `conn-providers_d-light`, `set-profile_d-light`, `audit_d-dark` | Pick the Settings order: eyebrow or breadcrumb, then h1, then tabs, then content. Apply it in `components/connections/ConnectionsNav.tsx`. Turn Insights and Audit into one "Insights" section with tabs (Overview, Audit log) using the same `SectionNav`. |
| C-11 | P2 | Detail pages | **Three "where am I" patterns**, and the spec's top-bar breadcrumb is missing. The patterns are an `← Councils` / `← All skills` back link, an `Argus › Insights` breadcrumb, and nothing at all on the run workspace. | `council-detail_d-light`, `skill-detail_d-light`, `insights_d-light`, `run-timeline_d-light` | Add a `<Breadcrumbs>` slot to the `AppShell` header (spec §2) fed by route `handle.crumb`, for example `Runs › {topic}`, `Councils › {name}`, `Skills › {name}`, `Insights › Audit log`. Remove the per-page back links and the `<nav aria-label="Breadcrumb">` in `routes/insights/Insights.tsx` and `Audit.tsx`. |
| C-12 | P2 | Several pages | **The page-header contract is broken in five places.** <br>• Home: a hand-built `h1 text-2xl`. <br>• Insights and Audit: a hand-built header, with Range as a segmented control in the action slot. <br>• Council detail: `CouncilHeader`, plus an extra `sr-only` "Council" h1 in the loading and error branches. <br>• Run workspace: a custom header. <br>• Skill detail: a long description makes `flex-wrap` push the actions under the title on the left; every other page has them top-right. <br>Only some headers show metadata badges (Insights shows WINDOW · 7D, council detail shows a mode badge). | `skill-detail_d-light`, `insights_d-light`, `home_d-light` | `components/ui/index.tsx` `PageHeader`: add `eyebrow`, `meta` (badges row) and `back` props, and give the description `max-w-prose` so the actions keep their right column (`sm:flex-nowrap`). Port Home, Insights, Audit, `CouncilHeader` and `SkillDetail` to it. Keep the run header custom, but use the same title size and type token. |
| C-13 | P2 | Home, Runs, run header, Councils | **Four renderings of run or council mode.** <br>• Home `RunRow`: lowercase `research`, with an icon whose `aria-label` duplicates the text. <br>• Runs table: "Research" with an icon. <br>• Run header: a neutral `Badge` with no icon. <br>• Councils: an accent `ModeBadge` with an icon. <br>Three separate mode→icon maps exist (`RunRow` `MODE_ICON`, `features/runs/RunsViews.tsx` `MODE_META`, `councils` `MODES`). | `home_d-light`, `runs_d-light`, `run-timeline_d-light`, `councils_d-light` | Move `MODE_META` into `components/ui` with one `<ModeLabel mode variant="inline\|badge">`. Use it in `components/run/RunRow.tsx`, `RunWorkspace.tsx` `Header`, `RunsViews.tsx` and `components/councils/parts.tsx`. Always show the title-case label plus the icon. |
| C-14 | P2 | Home vs Runs | **Run rows differ between Home and /runs** (spec §3.10: "same row anatomy as Home"). Home is a stacked row with the pill on the right; Runs is a table with different type sizes. Neither shows council, pending approvals or a budget microbar (spec §3.2). | `home_d-light`, `runs_d-light` | `components/run/RunRow.tsx` and `features/runs/RunsViews.tsx` should share one `RunCells` set (status, topic, mode, council, elapsed, cost, approvals). Home uses the stacked layout and Runs the table, both built from the same cells. |
| C-15 | P2 | Viz views, Home, builder, Ledger, Insights | **Four segmented-control looks.** <br>• `Segmented` primitive: surface pill on surface-2. <br>• `RunWorkspace` `ViewSwitcher`: its own tablist, h-7. <br>• Gantt All/Steps/Agents: its own look. <br>• Heatmap metric: accent-filled active segment. <br>In dark mode the primitive's selected segment is *darker* than its track (surface on surface-2, about 1.1:1). Selection then relies on font weight alone, which fails 1.4.11. | `run-heatmap_d-light`, `run-gantt_d-light`, `home_d-dark` (Mode control) | `Segmented` in `components/ui/index.tsx`: selected = `bg-surface-3` plus `ring-1 ring-border-strong` (or an accent underline) in dark, and add `size="sm"`. Replace the custom toggles in `components/viz/heatmap/CommsHeatmap.tsx` and `components/viz/gantt/RunGantt.tsx` with it. Export a `SegmentedTabs` variant for `ViewSwitcher` so the roving-tabindex code lives in one place. |
| C-16 | P2 | Run workspace | **Two sibling tab sets both start with "Timeline":** Timeline/Output at the top right, and Timeline/Map/Gantt/Heatmap/Route in the pane. Also, on a finished run the intervention bar still shows two disabled selects, a disabled input, a disabled Send and an unlabeled red hand icon. | `run-timeline_d-light`, `run-map_d-dark` | `RunWorkspace.tsx`: rename the top-level pair to "Workspace / Deliverable", or turn "Output" into a primary "Open deliverable" button (it already exists in the banner, so drop one). In `components/run/parts.tsx`, when `disabled` (terminal), replace the bar with a one-line "Run finished · Start a follow-up run" row. |
| C-17 | P2 | Map, Gantt, Heatmap, Route | **The viz panes each have their own chrome.** <br>• Titles: Heatmap and Route have a card title; Map and Gantt have none. <br>• Zoom order: Map + − ↺ 100%; Gantt − 100% + ⤢. <br>• Status pills: custom outline pills ("Ended · completed", "4 of 4 steps"). <br>• Map: an accent "Live" toggle stays on for a completed run, and "100%" is clipped at the pane edge. <br>• Text: Route labels about 8px and clipped ("Scope the ques"); Heatmap time axis clipped (":38:30"). | `run-map_d-dark`, `run-gantt_d-light`, `run-heatmap_d-light`, `run-route_d-dark` | Create a shared `VizToolbar` (title, meta `Badge`s, filters, `ZoomControls` with a fixed order of − / % / + / reset / fit) in `components/viz/`. Hide or disable "Live" when the run is terminal (`components/viz/map/CouncilMap.tsx`). Set a floor of `text-2xs` (11px) for SVG labels in `components/viz/route/RunRoute.tsx`, and pad the heatmap axis. |
| C-18 | P2 | Runs, Gantt, Route, Audit, Profile, council seats (desktop) | **Targets under 24px (2.5.8).** <br>• Runs row checkboxes: 14px inputs inside a 24px `span` that does not toggle them. <br>• Gantt bars: as narrow as 4px for short spans. <br>• Route vote pin: 16px. <br>• Audit and Profile switches: 18px tall. <br>• Timeline actor-name buttons: 18px tall. | `all.json` `small` lists; `runs_d-light`, `run-gantt_d-light` | In `features/runs/RunsViews.tsx`, make the wrapper a `<label>`. In `components/viz/gantt/RunGantt.tsx`, enforce `min-width: 24px` hit rects (transparent) on bars. In `components/viz/route/RunRoute.tsx`, add `r ≥ 12` hit circles. In `SwitchTrack` and `Toggle`, use `min-h-6` on the button. In `components/run/parts.tsx`, give actor buttons `min-h-6`. |
| C-19 | P2 | Mobile, all pages | **Touch targets under 40px.** Every page has 9 to 39 of them: the header search, theme and avatar controls are 28px; segmented radios 28px; tabs 25 to 32px; selects and inputs 32px; seat move, edit and delete icon buttons 28px. About two thirds of this goes away with C-01. | `all.json` (`*_m-light` `small`), `council-detail_m-light` | After C-01: in `components/ui/index.tsx`, add `pointer-coarse:` variants (`[@media(pointer:coarse)]:min-h-11`) to `Button`, `IconButton`, `Segmented` and `control`, and to the `AppShell` header buttons, so touch gets 44px (spec §7.6). |
| C-20 | P2 | Builder, Home, council detail | **Three stepper implementations:** Home's inline `−`/`+` text buttons (28px), `Stepper` in `components/councils/fields.tsx` (icons, 32px), and the council-detail resize stepper (`components/councils/parts.tsx`). Home also has no visible label association beyond `aria-labelledby`, and no min/max hint. | `home_d-light`, `builder-validation.png`, `council-detail_d-light` | Promote `Stepper` from `components/councils/fields.tsx` to `components/ui`, and use it in `routes/Home.tsx` (with the "Between 1 and 12 seats" hint) and in council detail. |
| C-21 | P2 | Builder, Skill editor, Profile, council edit | **Form action bars differ.** <br>• Builder: inline, with Cancel as an un-styled `Link` at 16px. <br>• Skill editor: sticky footer, Cancel as an underlined text link on the right. <br>• Profile: a per-card footer band. <br>Cancel looks different in each. | `builder-validation.png`, `skill-new_d-light`, `set-profile_d-light` | Add `<FormActions sticky?>` in `components/ui` (primary on the left, secondary next to it, Cancel as `Button variant="ghost"` via `asChild`/Link, meta on the right). Use it in `routes/councils/CouncilBuilder.tsx`, `routes/skills/SkillEditor.tsx` and `components/councils/CouncilHeader.tsx`. |
| C-22 | P2 | Skills list | Card descriptions are clamped inconsistently: some show 3 lines with an ellipsis, some 4, and `source-evaluation` shows half a 5th line cut horizontally. The card footers then misalign. | `skills_d-light` | `components/skills/SkillCard.tsx`: use `line-clamp-3` with a fixed `min-h-[3lh]` and no `max-h` and `overflow-hidden` pair, and push the footer down with `mt-auto` in a `flex-col` card. |
| C-23 | P2 | Lists | **Empty-state CTA rules differ.** <br>• Councils removes the header's "New council" when empty. <br>• Runs, Providers, MCP, Agents and Webhooks show the same primary button twice (header plus empty state). <br>• Runs shows live filters over zero runs. <br>• 404 shows a bare EmptyState with no card and a duplicate `sr-only` h1. <br>• The EmptyState title is a `<p>`, so empty sections have no heading. | `empty-councils.png`, `empty-runs.png`, `conn-mcp_d-light`, `notfound_d-light` | Rule: the header action always stays, and the empty-state CTA is `variant="secondary"`, or omitted when it is the same action. Hide filters while the unfiltered total is 0 (`routes/RunsList.tsx`). In `EmptyState`, add `as="h2"` for the title. `routes/NotFound.tsx` should pass `titleAs="h1"` instead of the sr-only h1, and add a "Back" secondary button. |
| C-24 | P3 | Home | "Advanced guardrails" is a ghost button floating between the select and its hint (`items-end` against a field that has a hint). `⌘↵` is plain text and also shows on touch. Format chips, tool chips (skill editor) and tool tags (skills list) are three chip styles. | `home_d-light`, `skill-new_d-light`, `skills_d-light` | `routes/Home.tsx`: put the disclosure on its own row (as in the builder's "Customize guardrails"), and use `<Kbd>` hidden on `pointer:coarse`. Add `Chip` (toggle, rounded-full) and `Tag` (static, rounded-sm mono) to `components/ui` and use them in Home, `SkillEditor` and `SkillCard`. |
| C-25 | P3 | Run header | The budget meter labels are `$`, `tokens`, `steps` (lowercase, and "$" alone is cryptic). Spec §7.6 calls for the label `$1.24 / $5.00` with an `aria-valuetext`. | `run-timeline_d-light` | `components/run/parts.tsx` meter: use labels "Budget", "Tokens", "Steps" in `text-xs text-fg-subtle`, and verify `role="meter"` and `aria-valuetext`. |
| C-26 | P3 | Timeline | Vote messages render raw JSON (`{"option": "approve", "rationale": …}`) under each voter, next to a proper vote card. | `run-timeline_d-light` | `components/run/parts.tsx`: when a message parses as `{option, rationale}`, render "Voted **approve** · Meets the brief." or fold it into the vote card's Rationales. |
| C-27 | P3 | Insights | The "Quick actions" tile row (round icon tiles) is a pattern used nowhere else, and it duplicates the nav. "Not Probed" is Title Case where everything else is sentence case. The Range control sits where page actions normally go. | `insights_d-light`, `insights_m-dark` | `routes/insights/Insights.tsx`: drop the tiles and put "Export JSON" and "Refresh" in `PageHeader actions`, with Range as a `Segmented` above the KPI row. In `components/insights/charts.tsx`, change the copy to "Not probed". |
| C-28 | P3 | Filters | Each list filters differently. <br>• Runs: label-less selects that apply live, plus a ghost "Clear". <br>• Skills: a search plus a `Segmented` "Show". <br>• Ledger: a `Segmented` plus a select, with labels on different baselines. <br>• Audit: a boxed card with an explicit primary "Apply". | `runs_d-light`, `skills_d-light`, `conn-ledger_d-light`, `audit_d-dark` | Add a `FilterBar` in `components/ui` (unboxed, labels on one baseline, live apply with debounce, "Clear" ghost on the right). Remove the Apply button in `routes/insights/Audit.tsx`. |
| C-29 | P3 | Typography | Card titles vary: council card `text-base 600`, provider card `text-md 400`, skill card `text-base 600` mono-ish, seat row `text-md 600`, viz pane titles `text-lg`. Section titles mix `text-lg` (Home "Recent", builder sections) and `text-lg` plus a muted count, which is fine, but card titles need one token. | `councils_d-light`, `conn-providers_d-light`, `council-detail_d-light` | Add `CardTitle` (`text-base font-semibold`) and `SectionTitle` (`text-lg`) to `components/ui` and use them in `routes/connections/Providers.tsx`, `components/councils/seats.tsx` and `components/skills/SkillCard.tsx`. |
| C-30 | P3 | Shell | The mobile header shows the plain word "Argus" without the logo, while desktop shows logo plus "Argus Council". The theme toggle's icon reads as a small monitor glyph at 28px with no label. The spec's notifications bell is absent. | `home_m-light`, `home_d-light` | `AppShell.tsx`: show the logo mark on mobile too. Fold the theme toggle into the avatar menu on `<md`. Track the bell in the spec backlog. |
| C-31 | P3 | Iconography | "New run" uses `Sparkles` (Home button, palette), `Plus` (Runs header, Insights tile) and `Sparkles` again for Skills in the nav, so one glyph means two things. | `runs_d-light`, `home_d-light` | Keep `Sparkles` for Skills only. Use `Plus` for every "New …" create action and `Play` for "Start run" (council detail already uses `Play`). The files are `routes/Home.tsx` and `AppShell.tsx`. |
| C-32 | P3 | Providers | The mock provider has two names on one page: "Offline demo (mock)" (from the environment) and "Mock (offline, deterministic)" (in the available list). It is offered as "Connect" even though it is always on. | `conn-providers_d-light` | Backend catalog label or `routes/connections/Providers.tsx`: use one name ("Offline demo (mock)") and hide it from Available when it is already configured. |

## Per-area notes

### Shell and navigation
- The desktop rail is solid: the order matches the spec, the active state uses a subtle accent background plus the
  accent text, and the collapse control works.
- Mobile navigation is the weak point (C-02), and the mobile header controls are all 28px (C-19).
- There is no top-bar breadcrumb, so detail pages invent their own (C-11).
- The skip link exists and is the first tab stop.

### Home
- The composer follows spec §3.2 closely: Topic is autofocused, the description counter is there, the mode hint is
  there, Auto council shows a roster preview with overseer eye glyphs, and the offline-mode Callout links to
  Providers.
- Deviations:
  - the stepper is a third implementation (C-20);
  - the Advanced disclosure is misplaced (C-24);
  - Recent rows lack council, approvals and the budget microbar (C-14);
  - the Start button is disabled while Topic is empty, and nothing explains why (consider a hint on focus or hover).

### Runs list
- The table is clean. On mobile it turns into a stacked list instead of scrolling sideways (good).
- The checkbox hit area is wrong (C-18).
- The filters are always visible even with no runs (C-23).
- Status pills use icon plus label, as spec §7.2 requires.

### Run workspace (Timeline, Map, Gantt, Heatmap, Route, Output)
- This is the most complex screen and the least consistent one:
  - two "Timeline" tab sets (C-16);
  - four toolbar idioms (C-15, C-17);
  - two h1 elements (C-08);
  - no inspector below 1280px (C-03);
  - a broken mobile composer (C-04).
- Good:
  - every viz has a keyboard model with custom focus rings (dashed accent rhombus in the Map, focus-ring circle in
    the Route);
  - the Heatmap legend adds a hatch pattern for errors, so colour is not the only cue;
  - role colours with glyphs carry through the rail, the Gantt and the Map;
  - the Gantt has a text hint ("Arrow keys move between bars…").
- The Output view's Workspace file list is plain mono text with no button affordance, even though each entry is a
  button. Give the files the `FileViewer` list style used on Skill detail.

### Councils (list, builder, detail)
- The builder is the reference form in the app: an error summary that takes focus, inline field errors, hints,
  section cards, and roster mode versus custom seats. Copy its pattern elsewhere (C-06).
- The list is a card grid (the spec asked for a table or tree). That is acceptable at this scale, but the cards
  offer only Delete (the spec lists Run, Duplicate, Delete).
- Detail:
  - Seat rows have Move up/down buttons (2.5.7).
  - Delete is a `danger-ghost` button in the header next to the primary action, so it is one stray click from
    "Start run". Consider moving it into a ⋯ menu.
  - The trash icon on every seat row is visually loud. Use neutral `ghost` icons and turn red only on hover.

### Skills (list, detail, editor)
- The list and detail pages use `PageHeader` and cards consistently. The BUILT-IN badge and the read-only Callout
  on detail are clear.
- Issues: clamping (C-22), header actions dropping to the left (C-12), and the chip styles (C-24).
- The editor's two-column body and preview works well on desktop. On mobile it stacks (fine), but 29 targets are
  under 40px.

### Connections (Providers, MCP, Agents, Ledger)
- This is the most internally consistent area: the same sub-nav, the same `PageHeader`, the same
  "section title then card" rhythm, the same EmptyState inside a Card, and the same copy-block style for endpoints.
  Use it as the template for Settings and Insights.
- Problems:
  - the tabs sit above the title, unlike Settings (C-10);
  - the primary CTA appears twice (C-23);
  - the mock provider has two names (C-32).

### Insights and Audit
- They look polished, but they ignore the shared shell contract: a 96rem width (C-09), their own header and
  breadcrumb (C-11, C-12), quick-action tiles (C-27), and a boxed filter with Apply (C-28).
- The charts offer "View as table" (spec §8.11), which is good.
- The Audit result badges are text-only (SUCCESS). That is acceptable because the text carries the meaning, but
  `StatusPill` adds an icon elsewhere, so add `CheckCircle2`/`XCircle` here for parity.

### Settings (Profile with Appearance, API keys, Webhooks, Users)
- Appearance exists inside Profile (theme, density, reduced motion), and Keyboard has a single-key shortcuts
  toggle, as spec §3.10 requires.
- The per-card footer bands with right-aligned actions are a nice pattern, but appear only here (C-21).
- The `max-w-3xl` width is too narrow for the Users table (C-09).
- Users uses a self-explanatory switch plus role select, and the footnote explains why you can't demote yourself.

### Auth and 404
- The auth card matches spec §3.1 (400px, centred).
- Its error handling does not (C-06), there is no `<main>`, and there is no password reveal.
- 404: see C-23.

## What is consistent and good
- **Tokens are respected.** No raw palette colours or hex values appear in TSX outside the documented role palette
  (`components/ui/role.tsx`). Dark mode is token-driven everywhere, and axe found **zero colour-contrast
  violations** across 25 routes in both themes.
- **Focus is visible.** The global `:focus-visible` 2px ring with 2px offset holds on every tab stop sampled (30 per
  page, 27 pages). The custom SVG focus treatments in Map and Route, and the stretched-link focus on council cards,
  are done correctly.
- **No horizontal page overflow** at 390px on any route. Tables reflow to stacked lists (Runs) or scroll in their
  own region.
- **Status is never colour-only:** `StatusPill` always has an icon and a label, role avatars carry glyphs, overseers
  get a dashed ring plus an eye badge, and heatmap errors are hatched.
- **Button variants are disciplined:** primary, secondary, ghost, danger and danger-ghost come from one `Button`,
  and each view has one primary action (except the duplicated empty-state CTAs in C-23).
- **One switch visual:** every switch uses `SwitchTrack`, so state contrast is the same everywhere.
- **Each page has exactly one `h1`**, except the run workspace (C-08). Headings are otherwise in order.
- **The council builder's validation** (focused error summary plus inline `aria-describedby` errors) is a complete
  implementation of spec §8.9. Port it to auth and the other forms.
- **The Connections area** is the most consistent page family. Use it as the reference when fixing C-09 to C-12.
- **The wording is consistently plain and sentence-case:** "No councils yet", "Connect a provider", "Keep keys out
  of shared shells", "Built-in skills are read-only". The exceptions are a few Title Case strings ("Not Probed")
  and the raw backend messages in C-06.
