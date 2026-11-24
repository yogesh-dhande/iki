// The site's map: who reads it (the three doors), what each audience can do and
// understand, and where each item is. An item is a page of the site, or, until its page
// is written, the document in the repository that covers it today, or neither yet.
import { github } from "./repo.ts";

export type Audience = "bench" | "run" | "build";

export interface AudienceInfo {
  id: Audience;
  /** The door's name, which every page for this audience wears. */
  label: string;
  /** Who it's for, as a sentence. */
  who: string;
  /** Its pages are in this section: the bench's are use cases, the others' guides. */
  section: "Use cases" | "Guides";
  /** Where the home page's link to all of them goes. */
  all: string;
}

export const AUDIENCES: Record<Audience, AudienceInfo> = {
  bench: {
    id: "bench",
    label: "At the bench",
    who: "For scientists, QA and lab managers.",
    section: "Use cases",
    all: "/use-cases/",
  },
  run: {
    id: "run",
    label: "Running Iki",
    who: "For the admins who deploy and operate it.",
    section: "Guides",
    all: "/guides/#run",
  },
  build: {
    id: "build",
    label: "Building on Iki",
    who: "For plugin authors, integrators and coding agents.",
    section: "Guides",
    all: "/guides/#build",
  },
};

/** An item of a list: a site page (`href` from `/`), a repository document on GitHub
 * (`source`), or planned (neither). */
export interface Item {
  label: string;
  href?: string;
  /** A file in the repository that covers it until its page is written. */
  source?: string;
  /** A short qualifier after it: "plugin, bundle, deploy, starter". */
  note?: string;
  /** The other pages of a guide in several pages, under its first. */
  children?: Item[];
}

/** A link as a page's frontmatter writes it: a site path, or `repo:<path>` for a file in the
 * repository, which links to GitHub. */
export function itemOf(link: {
  label: string;
  href?: string | undefined;
  note?: string | undefined;
}): Item {
  const { label, href, note } = link;
  if (href === undefined) {
    return note ? { label, note } : { label };
  }
  const item: Item = href.startsWith("repo:")
    ? { label, source: href.slice("repo:".length) }
    : { label, href };
  return note ? { ...item, note } : item;
}

export function hrefOf(item: Item): string | null {
  if (item.href) return item.href;
  if (item.source) return github(item.source);
  return null;
}

export const TOP_NAV = [
  { label: "Guides", href: "/guides/" },
  { label: "Use cases", href: "/use-cases/" },
  { label: "Concepts", href: "/concepts/" },
  { label: "Reference", href: "/reference/" },
] as const;

export type Section = (typeof TOP_NAV)[number]["label"];

export const PAGES = {
  api: "/reference/api/",
  spec: "/reference/plugin-spec/",
} as const;

/** What the home page lists for admins and plugin authors that isn't a page here yet: the
 * repository's own documents. The bench's list is its use cases. */
export const REPOSITORY_GUIDES: Record<"run" | "build", Item[]> = {
  run: [
    { label: "Run Iki on a laptop with kind", source: "deploy/dev/README.md" },
    { label: "Deploy on Google Cloud", source: "deploy/gcp/README.md" },
    { label: "Deploy on AWS", source: "deploy/aws/README.md" },
    {
      label: "Add a plugin through the config repository",
      source: "deploy/config-template/README.md",
    },
    { label: "Restore from a backup", source: "docs/disaster-recovery.md" },
  ],
  build: [
    {
      label: "Validate and test a plugin in CI",
      source: "plugin-template/.agents/skills/iki-test-plugin/SKILL.md",
    },
    {
      label: "Upgrade a plugin without losing history",
      source: "plugin-template/.agents/skills/iki-upgrade-plugin/SKILL.md",
    },
  ],
};

/** The references. */
export const REFERENCES: Item[] = [
  { label: "REST API", href: PAGES.api, note: "every call and schema" },
  { label: "Plugin spec", href: PAGES.spec, note: "every manifest kind and key" },
  { label: "The iki CLI", source: "cli/README.md", note: "plugin, bundle, deploy, starter" },
  { label: "Python SDK and MCP server", source: "sdk/python/README.md" },
];

/** A page of one of the sections, as the navigation lists it. */
export interface Page {
  section: Section;
  audience: Audience;
  label: string;
  href: string;
  order: number;
  text?: string;
  /** The first page of the guide in several pages it's one of, by its path. */
  parent?: string;
}

function inOrder(a: Page, b: Page): number {
  return a.order - b.order || a.label.localeCompare(b.label);
}

/** Pages as a list: each guide in several pages once, its other pages under its first. */
export function nested(pages: readonly Page[]): (Item & { text?: string })[] {
  const sorted = [...pages].sort(inOrder);
  const item = (page: Page): Item & { text?: string } => {
    const children = sorted.filter((child) => child.parent === page.href).map(item);
    return {
      label: page.label,
      href: page.href,
      ...(page.text === undefined ? {} : { text: page.text }),
      ...(children.length > 0 ? { children } : {}),
    };
  };
  const hrefs = new Set(sorted.map((page) => page.href));
  return sorted.filter((page) => !page.parent || !hrefs.has(page.parent)).map(item);
}

/** A page's left-hand navigation: its audience's pages, and every concept, grouped under
 * the top navigation's own section names in its order, so a page is filed under one name
 * everywhere. Only this site's pages. */
export function sideNav(
  pages: readonly Page[],
  audience: Audience,
): { section: Section; items: Item[] }[] {
  const bare = (item: Item): Item => ({
    label: item.label,
    ...(item.href === undefined ? {} : { href: item.href }),
    ...(item.children ? { children: item.children.map(bare) } : {}),
  });
  return TOP_NAV.map((entry) => entry.label)
    .map((section) => ({
      section,
      items: nested(
        pages
          .filter((page) => page.section === section)
          .filter((page) => page.audience === audience || section === "Concepts"),
      ).map(bare),
    }))
    .filter((group) => group.items.length > 0);
}

/** The pages before and after a page of a guide in several pages, in the guide's order:
 * its first page, then the others. */
export function pager(
  pages: readonly Page[],
  href: string,
): { previous?: Item | undefined; next?: Item | undefined } {
  const page = pages.find((candidate) => candidate.href === href);
  const first = page?.parent ?? href;
  const guide = [
    ...pages.filter((candidate) => candidate.href === first),
    ...pages.filter((candidate) => candidate.parent === first).sort(inOrder),
  ];
  const at = guide.findIndex((candidate) => candidate.href === href);
  if (guide.length < 2 || at < 0) return {};
  const link = (found: Page | undefined) =>
    found ? { label: found.label, href: found.href } : undefined;
  return { previous: link(guide[at - 1]), next: link(guide[at + 1]) };
}

export const FOOTER: Item[] = [
  { label: "Contributing", source: "CONTRIBUTING.md" },
  { label: "Security", source: "SECURITY.md" },
  { label: "Code of Conduct", source: "CODE_OF_CONDUCT.md" },
  { label: "Roadmap to M1", source: "docs/roadmap.md" },
];
