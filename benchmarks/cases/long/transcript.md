# Session transcript — web-dashboard (raw, long session)

## Standup and setup

Morning. Long one today, we're standing up the dashboard front end from scratch. Pulled
the design files; the handoff from design is mostly complete though a few of the empty
and error states are still TODO on their side. I flagged it, they'll get to it this week,
not blocking the skeleton.

Build tooling first. Vite for the dev server and bundler — fast HMR, sane defaults,
nothing exotic. Node version pinned in the toolchain file so we don't get the "works on
my machine" drift the API team hit. Set up the path aliases so imports aren't a forest of
`../../../`. The lint and format config is shared from the org preset; I wired the
pre-commit hook so nobody pushes unformatted code. CI runs typecheck, lint, and the unit
suite on every PR; the preview deploy is a follow-up once we have something to look at.

Spent a little while on the component-library question. We're not pulling in a heavyweight
kit — most of those fight you the moment the design diverges from their defaults, and our
design system is opinionated. We'll build a thin set of primitives (button, input, dialog,
table) on accessible headless behaviour and style them ourselves. The table is the scary
one because the analytics views need virtualised rows for the big result sets, so I
prototyped that in isolation first to de-risk it. It scrolls smoothly at fifty thousand
rows, good enough.

Also talked through the data contract with the API folks. The endpoints return cursors,
not offsets, which is right for our infinite-scroll tables, and the error envelope is
consistent so we can centralise error handling. They'll version the response shape, so we
won't get surprised by a silent field rename. Good meeting overall, no decisions blocked.

## Architecture decisions (the substance)

Okay, the calls that matter for whoever picks this up next.

State management: we're using Zustand, not Redux. The app's shared state surface is
genuinely small — auth, the current workspace, a few UI toggles — and a store per concern
with hooks is far less ceremony than reducers and actions for this size. If it grows
unwieldy we revisit, but I doubt it will.

Routing: TanStack Router. The draw is type-safe routes and, more importantly, route
loaders — we declare each route's data dependency and the router fetches it before the
component renders, which sidesteps a whole class of spinner-on-spinner problems.

Styling: Tailwind v4. The design tokens — colour ramps, spacing scale, typography — live
in the @theme block as the single source of truth, and the design system maps onto them
cleanly. No separate CSS-in-JS layer.

Two things we are explicitly deferring, write them down. The accessibility audit — proper
keyboard navigation, ARIA roles on the custom widgets, contrast checking — has not been
done and must happen before launch. And i18n / localization is deferred to a later
milestone; we're shipping English-only for now and not threading a translation layer
through yet.

One trap to record from the prototype: I first fetched the table data in a useEffect, and
it produced request waterfalls — parent fetches, renders, child mounts, child fetches,
and the page assembles in slow stages. The fix is to load data in the route loader
instead, so it's in flight before render. Do not reintroduce useEffect fetching for route
data.

A caveat for the SSR pass we'll add later: direct window access during render breaks SSR
hydration. Any window or document use has to be guarded behind a client-side check or
moved into an effect, or the server render throws and you get a hydration mismatch.

## Implementation grind

With the calls made, spent the back half of the day actually building. The app shell is
up: top bar, collapsible nav, the content region with the router outlet. The auth flow
redirects unauthenticated users to login and bounces them back to where they were headed,
which is fiddlier than it sounds because of the deep links. Wired the workspace switcher;
changing workspace invalidates the relevant queries and the views refetch cleanly.

Built the first real view, the overview page, end to end: the metric cards, the time
range picker, and the main chart. The chart library choice took a minute — most of them
are either gorgeous and enormous or tiny and ugly — but landed on one that tree-shakes
well and looks fine. Hooked up the loaders so a hard refresh on a deep link lands you on a
fully populated page with no flash of empty state, which is the whole point of the routing
choice paying off.

Wrote tests as I went: the store logic has unit tests, the critical components have
interaction tests, and there's one end-to-end happy-path test that logs in, switches
workspace, and asserts the overview renders. The flaky bits are all around timing in the
e2e, the usual, nothing alarming.

## Wrap-up and logistics

Good progress for one day. Landed: build setup, the primitives, the app shell, auth, the
workspace switcher, and the overview page. Not landed but decided: the deferred items
above and the SSR pass. Next session I'd build the detail view and then start in on the
items we parked.

Logistics: I split the work into reviewable PRs rather than one giant branch — tooling,
then primitives, then shell, then the overview — so review is sane. The design team will
deliver the missing states by Thursday. Standup is at the usual time tomorrow. The API
team is deploying their cursor change tonight behind a flag, so if something looks off in
the morning, check that first. Calling it here.
