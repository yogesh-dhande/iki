# 0029. The web app: React pages served by Iki, beside server-rendered ones

- Status: Accepted
- Date: 2026-11-10
- Refines: [0004](0004-backend-stack-django.md),
  [0027](0027-rest-api-conventions-and-service-accounts.md)

## Context

Projects are where people will spend their day: entities, runs and views,
with tables, filters and live updates. Server-rendered pages can't do that
well. Pages people visit rarely, such as sign-in, setup, consent and admin
settings, are simple forms, and they're where security matters most.

Pharma and life-science customers will review the web app as part of
vendor assessments and validation (GxP, 21 CFR Part 11). Those reviews look
for a strict Content Security Policy, no third-party code at run time, WCAG
accessibility, and the same authorization whichever way data is reached.

## Decision

**A hybrid, as GitHub does it.** Home and the project pages (`/`,
`/projects/…`) are a React app at the same addresses the server-rendered
pages had, so links and bookmarks keep working. Sign-in, setup, OAuth
consent and the device flow, teams, people, org settings and the audit log
stay server-rendered. Both kinds of page share one stylesheet
(`static/iki/iki.css`) and the same markup, so the app looks and behaves the
same throughout.

**The server serves the app, and it reads only the API.** Django answers
the app's addresses with one page (`web/app.html`) that loads the built
entry module, the stylesheet and the CSRF token, after the usual sign-in
check. That page carries no data and is the same for everyone. Everything
comes from `/api/v1`, so the API's authorization (ADR 0027) is the only
authorization: an outsider gets the same page as a member, then a 404 from
the API.

**The browser session is the credential.** This is the backend-for-frontend
pattern: no tokens in JavaScript. The app calls the API with the session
cookie (HttpOnly, SameSite) and sends the CSRF token from the page on every
change. When the session ends, the next call answers 401, and the app sends
the person to sign in and back to where they were.

**The policy stays strict.** The CSP is unchanged: `script-src 'self'`,
`style-src 'self'`, no inline code, no `eval`, and Trusted Types with no
policies (`trusted-types 'none'`). The app is ES modules from Iki's own
origin. It bundles no CSS and injects no styles, and it never renders raw
HTML: ESLint refuses `dangerouslySetInnerHTML`, and Trusted Types would
refuse it anyway. Even the development setup keeps the policy. Vite's React
Fast Refresh preamble is a file, not the inline script Vite's guide uses.

**Building and serving.**

- **Stack:** Vite builds the app (TypeScript, React 19). Routing uses
  TanStack Router, with code-based, type-checked routes. Data fetching and
  caching use TanStack Query.
- **API client:** generated from the OpenAPI document
  (`web/packages/sdk`, openapi-typescript and openapi-fetch). CI fails if the
  document or the client is stale.
- **Image:** the server image builds the app in a Node stage. Vite names
  files by content hash. Django's manifest storage keeps those names, so
  chunks can still import each other, and servestatic serves them with
  `immutable` caching.
- **Missing build:** an image without the build answers 503 with a page that
  says so, rather than a blank page.

**Accessibility is checked, not hoped for.**

- **Linting:** `eslint-plugin-jsx-a11y` (strict) and React's hooks rules.
- **Forms:** native form controls, wired as the server's forms are: labels,
  help and errors linked by `aria-describedby`, `aria-invalid`, fieldsets for
  radio groups, focus on the first invalid field. Required fields are checked
  in the browser before sending, in plain words; the API still checks
  everything. A refusal about a field (a duplicate name) names that field in
  the problem's `errors`, so the message shows next to it.
- **Dialogs:** React Aria, which traps focus, closes on Escape and returns
  focus.
- **Navigation:** after each move within the app, focus goes to the new
  page's heading, as a page load would. Confirmations are announced through a
  live region.
- **Browser tests:** Playwright runs Chromium, Firefox and WebKit against the
  real server (production settings, collected static files, uvicorn, the
  least-privileged database role). axe checks every page, app and
  server-rendered, against WCAG 2.2 AA in light and dark mode. The same tests
  fail on any CSP violation or console error, and check keyboard flows. The
  dev cluster's end-to-end checks sign in through Keycloak in Chromium and
  create a project in the app.

## Consequences

- One authorization path: a bug in the app can show the wrong thing, but it
  can't show data the API wouldn't give that person.
- Pages without JavaScript don't work, as with GitHub. The page says so
  (`<noscript>`), and the server-rendered sign-in and admin pages don't need
  it.
- Builds and CI now need Node. The image's Node stage is pinned by digest,
  and Corepack checks pnpm's hash.
- `axe-core` (MPL-2.0) and Playwright are development dependencies only;
  nothing third-party loads at run time.
- New server-rendered pages should stay rare: forms for setup, sign-in and
  administration. Anything people work in day to day goes in the app.
