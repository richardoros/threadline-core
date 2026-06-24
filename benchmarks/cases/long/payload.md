# Resume payload — web-dashboard

Composed from threadline core records. Handed to the next session instead of the transcript.

**Objective:** Build the customer-facing analytics dashboard (React) on top of the new API.

**Decisions in force:**
- Client state with Zustand, not Redux (the shared state surface is small).
- Routing with TanStack Router, for type-safe routes and route loaders.
- Styling with Tailwind v4; design tokens defined in the @theme block.

**Open loops:**
- Accessibility audit (keyboard nav, ARIA roles, contrast) has not been done.
- i18n / localization is deferred to a later milestone.

**Known traps (do not repeat):**
- Fetching in useEffect caused request waterfalls; load data in the route loader instead.

**Active caveats:**
- Direct window access breaks SSR hydration; guard any window/document use behind a check.
