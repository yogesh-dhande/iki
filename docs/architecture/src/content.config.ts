// The ADRs, rendered here from docs/adr so the site can show them beside the pages that
// cite them. lib/adrs.ts reads the same files' headers for the index and the graph.
import { defineCollection } from "astro:content";
import { glob } from "astro/loaders";

export const collections = {
  adr: defineCollection({
    loader: glob({ pattern: "[0-9][0-9][0-9][0-9]-*.md", base: "../adr" }),
  }),
};
