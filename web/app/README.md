# Iki web app

The React app the server serves at `/` and `/projects/…` (see
[ADR 0029](../../docs/adr/0029-react-web-app.md)). Sign-in, setup, consent and
admin pages are server-rendered; both use the server's stylesheet,
`server/src/iki_server/web/static/iki/iki.css`.

```sh
make web       # build into server/src/iki_server/web/static/app (the server serves it)
make web-dev   # instant reloads at http://localhost:5173, against the dev cluster
make client    # regenerate the API client after changing the API
```

## Working on it

1. Bring up the dev cluster (`make dev-up`) and sign in at
   <http://localhost:8000>. Cookies are shared across ports, so you stay
   signed in on the dev server.
2. `make web-dev`, then open <http://localhost:5173>. Vite serves the app's
   modules and passes everything else (pages, `/api`, sign-in) to the
   cluster. The dev cluster runs with `web.devServer: true`, so pages
   requested through Vite load its modules instead of the built ones.
3. Set `IKI_DEV_SERVER` to point Vite at another server.

The production CSP applies in development too. Vite's error overlay can't
show (Trusted Types blocks it), so read build errors in the terminal or the
console.

## Layout

- `src/api.ts`: the API client (the browser session plus the CSRF token) and
  a TanStack Query query for each resource.
- `src/ui.tsx`: the design system, as components over the stylesheet's classes.
- `src/forms.tsx`: form state, checks in the browser, and focus on errors.
- `src/entities.ts`: entity and type queries, and how each kind of value
  reads (quantities in their display unit, references by title, files by name).
- `src/entity-values.ts` and `src/entity-inputs.tsx`: entity forms, one input
  per kind of field, built from each type's fields and its JSON Schema. The
  browser checks what it can (required, numbers, ranges, lengths, patterns)
  and sends JSON as the API takes it; the API checks everything again.
- `src/uploads.ts`: uploading a file as the files API takes it (ADR 0036):
  its SHA-256 a slice at a time on the page (the CSP allows no worker
  scripts), then its parts straight to storage, retried and re-signed as
  needed. `src/media.ts` names lab formats browsers don't know (`.pdb`,
  `.cif`, `.fcs`) by extension.
- `src/filters.ts` and `src/pages/entity-filters.tsx`: the grid's filters,
  as the search API takes them (ADR 0037): each kind of field's conditions
  in words, typed into the same inputs as forms, and kept in the page's
  address so a filtered, sorted grid can be bookmarked and shared.
  `src/pages/saved-searches.tsx` keeps a grid's search under a name, for
  oneself or shared with the project. `src/pages/collections.tsx` saves it
  as a collection (ADR 0038), an entity of its own, and shows a collection's
  members in the grid.
- `src/router.tsx` and `src/pages/`: the pages, at the server pages' old
  addresses. Pages other than the frame, home and a project's overview load
  when they're first opened (`lazyRouteComponent`), so the first page stays
  small.

## Tests

- `pnpm --filter @ikisci/app test`: the pages against a fake API (Vitest,
  Testing Library), covering what people see and the calls each action makes.
- `server/tests/test_browser.py`: Chromium, Firefox and WebKit against the
  real server, with axe (WCAG 2.2 AA) and the production CSP. It needs
  `make web` first.
