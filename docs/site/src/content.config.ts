// The site's collections: pages written for it (concepts and use cases, as MDX), and the
// decision records, read from docs/adr where they're kept.
import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

import { defineCollection } from "astro:content";
import { glob, type Loader } from "astro/loaders";
import { z } from "astro/zod";

import { guideOfIndex, parseDecision } from "./lib/adr.ts";
import { linkDecisions, type Decisions } from "./lib/markdown.ts";
import { ADRS, isDecision } from "./lib/repo.ts";

const audience = z.enum(["bench", "run", "build"]);
const link = z.object({ label: z.string(), href: z.string(), note: z.string().optional() });

/** Each decision record, its header parsed and its text rendered as the site's Markdown
 * renders the repository's: links to other files go to their pages, or to GitHub. */
function decisions(): Loader {
  return {
    name: "iki-decisions",
    async load(context) {
      const { store, watcher } = context;
      store.clear();
      const names = (await readdir(ADRS)).filter(isDecision).sort();
      // "ADR 0184" in a record's text links to that record's page, as it does elsewhere.
      const slugs: Decisions = new Map(
        names.map((name) => [name.slice(0, 4), name.replace(/\.md$/, "")]),
      );
      for (const name of names) {
        const path = join(ADRS, name);
        const text = await readFile(path, "utf-8");
        const { body, ...header } = parseDecision(name.replace(/\.md$/, ""), text);
        const data = await context.parseData({
          id: header.slug,
          data: { ...header, kind: "decision" },
        });
        store.set({
          id: header.slug,
          data,
          body,
          digest: context.generateDigest(text),
          rendered: await context.renderMarkdown(linkDecisions(body, slugs), {
            fileURL: pathToFileURL(path),
          }),
          filePath: `../../docs/adr/${name}`,
        });
      }
      const readme = join(ADRS, "README.md");
      const guide = guideOfIndex(await readFile(readme, "utf-8"));
      store.set({
        id: "README",
        data: await context.parseData({ id: "README", data: { kind: "index" } }),
        body: guide,
        digest: context.generateDigest(guide),
        rendered: await context.renderMarkdown(linkDecisions(guide, slugs), {
          fileURL: pathToFileURL(readme),
        }),
      });
      watcher?.add(ADRS);
    },
  };
}

const field = z.object({ label: z.string(), markdown: z.string(), decisions: z.array(z.string()) });

export const collections = {
  concepts: defineCollection({
    loader: glob({ pattern: "*.mdx", base: "./src/content/concepts" }),
    schema: z.object({
      title: z.string(),
      description: z.string(),
      audience,
      /** Who else it serves, in the audience band. */
      band: z.string(),
      /** Where it sits: "Lots and preps". */
      topic: z.string(),
      /** The task page, or document, that does what the concept explains. */
      doIt: z.object({ text: z.string(), label: z.string(), href: z.string() }),
    }),
  }),
  useCases: defineCollection({
    loader: glob({ pattern: "*.mdx", base: "./src/content/use-cases" }),
    schema: z.object({
      title: z.string(),
      description: z.string(),
      audience,
      band: z.string(),
      topic: z.string(),
      facts: z.array(
        z.object({ label: z.string(), value: z.string(), code: z.boolean().default(false) }),
      ),
      steps: z.array(z.object({ id: z.string(), role: z.string(), label: z.string() })),
      understand: z.array(link),
      reference: z.array(link),
      aside: z.object({ title: z.string(), text: z.string(), href: z.string() }).optional(),
    }),
  }),
  decisions: defineCollection({
    loader: decisions(),
    schema: z.discriminatedUnion("kind", [
      z.object({
        kind: z.literal("decision"),
        slug: z.string(),
        number: z.string(),
        title: z.string(),
        standing: z.enum(["accepted", "proposed", "superseded"]),
        status: z.string(),
        date: z.string(),
        fields: z.array(field),
      }),
      z.object({ kind: z.literal("index") }),
    ]),
  }),
};
