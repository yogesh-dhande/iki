// The site's written pages, from their collections, as the navigation lists them.
import { getCollection } from "astro:content";

import type { Page } from "./site.ts";

export async function sitePages(): Promise<Page[]> {
  const [guides, useCases, concepts, reference] = await Promise.all([
    getCollection("guides"),
    getCollection("useCases"),
    getCollection("concepts"),
    getCollection("reference"),
  ]);
  return [
    ...guides.map((entry) => ({
      section: "Guides" as const,
      audience: entry.data.audience,
      label: entry.data.title,
      href: `/guides/${entry.id}/`,
      order: entry.data.order,
      text: entry.data.description,
      ...guideParent(entry.id),
    })),
    ...useCases.map((entry) => ({
      section: "Use cases" as const,
      audience: entry.data.audience,
      label: entry.data.title,
      href: `/use-cases/${entry.id}/`,
      order: entry.data.order,
      text: entry.data.description,
      category: entry.data.category,
    })),
    ...concepts.map((entry) => ({
      section: "Concepts" as const,
      audience: entry.data.audience,
      label: entry.data.title,
      href: `/concepts/${entry.id}/`,
      order: entry.data.order,
      text: entry.data.description,
    })),
    ...reference.map((entry) => ({
      section: "Reference" as const,
      audience: "build" as const,
      label: entry.data.title,
      href: `/reference/${entry.id}/`,
      order: entry.data.order,
      text: entry.data.description,
    })),
  ];
}

/** A page of a guide in several pages is under its first: `write-a-plugin/fields` is
 * under `write-a-plugin`. */
export function guideParent(id: string): { parent?: string } {
  const slash = id.lastIndexOf("/");
  return slash > 0 ? { parent: `/guides/${id.slice(0, slash)}/` } : {};
}

/** A section's pages in their order. */
export function inSection(pages: readonly Page[], section: Page["section"]): Page[] {
  return pages
    .filter((page) => page.section === section)
    .sort((a, b) => a.order - b.order || a.label.localeCompare(b.label));
}
