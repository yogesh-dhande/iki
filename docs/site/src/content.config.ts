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
      /** The guide that does what the concept explains. */
      guide: z.object({ text: z.string(), label: z.string(), href: z.string() }),
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
      /** The concept pages that explain it. */
      concepts: z.array(link).default([]),
      reference: z.array(link),
      aside: z.object({ title: z.string(), text: z.string(), href: z.string() }).optional(),
    }),
  }),
};
