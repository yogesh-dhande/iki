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
  { label: "Use cases", href: "/use-cases/" },
  { label: "Concepts", href: "/concepts/" },
  { label: "Guides", href: "/guides/" },
  { label: "Reference", href: "/reference/" },
] as const;

export type Section = (typeof TOP_NAV)[number]["label"];

export const PAGES = {
  api: "/reference/api/",
  spec: "/reference/plugin-spec/",
  plugins: "/reference/plugins/",
} as const;

/** The references. */
export const REFERENCES: Item[] = [
  { label: "REST API", href: PAGES.api, note: "every call and schema" },
  { label: "Plugin spec", href: PAGES.spec, note: "every manifest kind and key" },
  { label: "Plugins", href: PAGES.plugins, note: "Iki's own plugins: their types and actions" },
  { label: "The iki CLI", href: "/reference/cli/", note: "plugin, bundle, deploy, starter" },
  { label: "Python SDK and MCP server", href: "/reference/python-sdk/" },
];

/** The four kinds of use case, in the order they're listed. */
export const USE_CASE_CATEGORIES = [
  { id: "registry-and-inventory", label: "Registry and inventory" },
  { id: "data-tracking", label: "Data tracking" },
  { id: "analysis", label: "Analysis" },
  { id: "collaboration", label: "Collaboration" },
] as const;

export type UseCaseCategory = (typeof USE_CASE_CATEGORIES)[number]["id"];

/** Use cases are listed under their kinds once there are more than this many. */
export const USE_CASES_BEFORE_GROUPS = 8;

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
  /** A use case's kind. */
  category?: UseCaseCategory;
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

/** A link in the site's navigation. */
export interface NavLink {
  label: string;
  href: string;
}

/** A named set of links within a section, such as the guides for running Iki. */
export interface NavGroup {
  label: string;
  links: NavLink[];
}

export type NavEntry = NavLink | NavGroup;

/** A section of the site's navigation, named and ordered as the top navigation is. */
export interface NavSection {
  section: Section;
  href: string;
  entries: NavEntry[];
}

/** What the navigation lists besides the written pages: the generated references. */
export interface Generated {
  api: readonly { label: string; slug: string }[];
  spec: readonly { title: string; href: string }[];
  plugins: readonly { title: string; href: string }[];
}

export function isGroup(entry: NavEntry): entry is NavGroup {
  return "links" in entry;
}

/** The use cases in the order they're listed: by kind, then their own order. */
export function useCasesInOrder(pages: readonly Page[]): Page[] {
  const kind = (page: Page) =>
    USE_CASE_CATEGORIES.findIndex((category) => category.id === page.category);
  return pages
    .filter((page) => page.section === "Use cases")
    .sort((a, b) => kind(a) - kind(b) || inOrder(a, b));
}

/** The site's one navigation, the same on every page: each section of the top navigation in
 * its order, each page under one of them, never more than a section and a group deep. A
 * guide in several pages lists its pages after its first. */
export function siteNav(pages: readonly Page[], generated: Generated): NavSection[] {
  const links = (section: Section, audience?: Audience): NavLink[] => {
    const flat = (items: Item[]): NavLink[] =>
      items.flatMap((item) => [
        { label: item.label, href: item.href ?? "" },
        ...flat(item.children ?? []),
      ]);
    return flat(
      nested(
        pages.filter(
          (page) =>
            page.section === section && (audience === undefined || page.audience === audience),
        ),
      ),
    );
  };
  const overview = (href: string): NavLink => ({ label: "Overview", href });
  const useCases = useCasesInOrder(pages);
  const link = (page: Page): NavLink => ({ label: page.label, href: page.href });
  const byKind: NavEntry[] =
    useCases.length > USE_CASES_BEFORE_GROUPS
      ? USE_CASE_CATEGORIES.map((category) => ({
          label: category.label,
          links: useCases.filter((page) => page.category === category.id).map(link),
        })).filter((group) => group.links.length > 0)
      : useCases.map(link);
  const reference: NavEntry[] = [
    {
      label: "REST API",
      links: [
        overview(PAGES.api),
        ...generated.api.map((tag) => ({ label: tag.label, href: `${PAGES.api}#${tag.slug}` })),
      ],
    },
    {
      label: "Plugin spec",
      links: [
        overview(PAGES.spec),
        ...generated.spec.map((page) => ({ label: page.title, href: page.href })),
      ],
    },
    ...(generated.plugins.length > 0
      ? [
          {
            label: "Plugins",
            links: [
              overview(PAGES.plugins),
              ...generated.plugins.map((page) => ({ label: page.title, href: page.href })),
            ],
          },
        ]
      : []),
    ...links("Reference"),
  ];
  const entries: Record<Section, NavEntry[]> = {
    "Use cases": byKind,
    Concepts: links("Concepts"),
    Guides: (["run", "build"] as const)
      .map((audience) => ({ label: AUDIENCES[audience].label, links: links("Guides", audience) }))
      .filter((group) => group.links.length > 0),
    Reference: reference,
  };
  return TOP_NAV.map((entry) => ({
    section: entry.label,
    href: entry.href,
    entries: entries[entry.label],
  }));
}

/** Where a page is in the navigation: the link to it, or for a page the navigation doesn't
 * list, such as an API call, the link it's listed under. */
export function currentIn(nav: readonly NavSection[], path: string, under?: string): string | null {
  const all = nav.flatMap((section) => [
    section.href,
    ...section.entries.flatMap((entry) =>
      isGroup(entry) ? entry.links.map((link) => link.href) : [entry.href],
    ),
  ]);
  if (all.includes(path)) return path;
  return under !== undefined && all.includes(under) ? under : null;
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
