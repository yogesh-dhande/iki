// The site's collections: the pages written for it, concepts and use cases, as MDX.
import { defineCollection } from "astro:content";
import { glob } from "astro/loaders";
import { z } from "astro/zod";

const audience = z.enum(["bench", "run", "build"]);
/** A link, or a planned page (no `href`). */
const link = z.object({
  label: z.string(),
  href: z.string().optional(),
  note: z.string().optional(),
});

export const collections = {
  /** Tasks for admins and plugin authors. A guide in several pages is a page and a
   * directory of the same name: `write-a-plugin.mdx`, then `write-a-plugin/fields.mdx`. */
  guides: defineCollection({
    loader: glob({ pattern: "**/*.mdx", base: "./src/content/guides" }),
    schema: z.object({
      title: z.string(),
      description: z.string(),
      audience: z.enum(["run", "build"]),
      /** Its order in its section's lists, or among its guide's pages: lower first. */
      order: z.number().default(100),
      reference: z.array(link).default([]),
    }),
  }),
  /** One lab workflow each, named as a task, its steps by role. */
  useCases: defineCollection({
    loader: glob({ pattern: "*.mdx", base: "./src/content/use-cases" }),
    schema: z.object({
      title: z.string(),
      description: z.string(),
      audience,
      topic: z.string(),
      order: z.number().default(100),
      facts: z.array(
        z.object({ label: z.string(), value: z.string(), code: z.boolean().default(false) }),
      ),
      /** The concept pages that explain it. */
      concepts: z.array(link).default([]),
      reference: z.array(link),
      aside: z.object({ title: z.string(), text: z.string(), href: z.string() }).optional(),
    }),
  }),
  /** How Iki behaves, one idea each, named as a noun. */
  concepts: defineCollection({
    loader: glob({ pattern: "*.mdx", base: "./src/content/concepts" }),
    schema: z.object({
      title: z.string(),
      description: z.string(),
      audience,
      topic: z.string(),
      order: z.number().default(100),
      /** The use cases that do what it explains. */
      useCases: z.array(link).default([]),
    }),
  }),
};
