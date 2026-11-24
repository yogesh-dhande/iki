# ikisci.org

Iki's public documentation site: Astro with content collections and MDX, searched with
Pagefind, each page with its own Content Security Policy. `make docs` builds it into `dist/`,
`make docs-dev` serves it with reloads, and `make check-docs` checks the build's links and
policies.

## Where a page goes

- **Use case**: one lab workflow from its start to its result, its steps by the role that does
  them, on public or simulated data. Named as the task ("Request work from another team"). Each
  has a `category`: registry and inventory, data tracking, analysis or collaboration.
- **Concept**: one idea that at least two workflows or record types share, such as revisions,
  states or corrections. Named as a noun. A single plugin's record type isn't a concept: link
  to its entry under Reference, Plugins.
- **Guide**: a task for an admin who runs Iki or a plugin author who builds on it, in order.
  Named as the task. A long guide is a page and a directory of the same name, one topic each.
- **Reference**: what something takes and does, in full, to look up: the REST API, the plugin
  spec and the plugins (generated from the code), and the CLI and the Python SDK (written).

Use cases, concepts and guides link only to the site's own pages; a reference page may link to
a source file. No page cites or links the design records in `docs/adr/`.

## The sources

- `src/content/`: the written pages, one collection per section.
- `src/lib/site.ts`: the navigation, the same on every page, and the sections' order.
- `../sitegen`: `python -m sitegen generate` writes what pages read from the code (the
  examples' results, the plugins, the API's callers); `python -m sitegen check` checks a build.
