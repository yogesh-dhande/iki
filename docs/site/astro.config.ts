// Iki's docs site, ikisci.org. `make docs` builds it into docs/site/dist, `make docs-dev`
// serves it, and `make check-docs` checks it (python -m sitegen check, and the browser
// tests in docs/sitegen/tests).
//
// Plain Astro with content collections, not Starlight: the site's pages are laid out by
// who reads them (the three doors, each page in its audience's colour, concept pages in
// fixed layers), which Starlight's single sidebar and page frame would have to be
// overridden almost entirely to give.

import { satteri } from "@astrojs/markdown-satteri";
import mdx from "@astrojs/mdx";
import { defineConfig, passthroughImageService } from "astro/config";
import { defaultClientConditions } from "vite";

import { repositoryLinks } from "./src/lib/links.ts";
import { focusableScrollers } from "./src/lib/scrollers.ts";
import { REF, REPOSITORY, ROOT, sources } from "./src/lib/repo.ts";

export default defineConfig({
  site: "https://ikisci.org",
  trailingSlash: "always",
  // Nothing to resize, so no sharp (whose libvips is LGPL; pnpm-workspace.yaml).
  image: { service: passthroughImageService() },
  // Stylesheets are files, never inlined into pages, so the policy allows them as 'self'.
  build: { inlineStylesheets: "never", format: "directory" },
  // Development only: Astro's toolbar adds scripts of its own.
  devToolbar: { enabled: false },
  markdown: {
    // Highlighting would colour code with style attributes, which the policy refuses.
    syntaxHighlight: false,
    processor: satteri({
      mdastPlugins: [
        repositoryLinks({ root: ROOT, sources: sources(), repository: REPOSITORY, ref: REF }),
      ],
      hastPlugins: [focusableScrollers()],
    }),
  },
  integrations: [mdx()],
  vite: {
    // The packages' TypeScript sources, as the web app builds them (web/app/vite.config.ts).
    resolve: { conditions: ["@ikisci/source", ...defaultClientConditions] },
    server: { fs: { allow: [ROOT] } },
    // Where the repository is, for pages that read its files as they're built (src/lib/repo.ts).
    define: { __IKI_ROOT__: JSON.stringify(ROOT) },
  },
});
