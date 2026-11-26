import { describe, expect, it } from "vitest";

import {
  REFERENCES,
  TOP_NAV,
  USE_CASES_BEFORE_GROUPS,
  currentIn,
  hrefOf,
  isGroup,
  nested,
  pager,
  siteNav,
  type Generated,
  type NavGroup,
  type Page,
} from "./site.ts";

const sections: string[] = TOP_NAV.map((entry) => entry.label);

const pages: Page[] = [
  {
    section: "Concepts",
    audience: "bench",
    label: "Records and revisions",
    href: "/concepts/records/",
    order: 10,
  },
  {
    section: "Use cases",
    audience: "bench",
    label: "Verify a plasmid prep",
    href: "/use-cases/verify/",
    order: 20,
    category: "analysis",
  },
  {
    section: "Use cases",
    audience: "bench",
    label: "Run a dose-response plate assay",
    href: "/use-cases/dose/",
    order: 10,
    category: "analysis",
  },
  {
    section: "Use cases",
    audience: "bench",
    label: "Register reagents and track stock",
    href: "/use-cases/reagents/",
    order: 90,
    category: "registry-and-inventory",
  },
  {
    section: "Guides",
    audience: "run",
    label: "Run Iki on a laptop with kind",
    href: "/guides/laptop/",
    order: 10,
  },
  {
    section: "Reference",
    audience: "build",
    label: "The iki CLI",
    href: "/reference/cli/",
    order: 30,
  },
  {
    section: "Guides",
    audience: "build",
    label: "Write a plugin",
    href: "/guides/write-a-plugin/",
    order: 10,
  },
  {
    section: "Concepts",
    audience: "build",
    label: "Corrections",
    href: "/concepts/corrections/",
    order: 40,
  },
  {
    section: "Guides",
    audience: "build",
    label: "Track a record's state",
    href: "/guides/write-a-plugin/states/",
    order: 40,
    parent: "/guides/write-a-plugin/",
  },
  {
    section: "Guides",
    audience: "build",
    label: "Declare a type's fields",
    href: "/guides/write-a-plugin/fields/",
    order: 20,
    parent: "/guides/write-a-plugin/",
  },
];

const generated: Generated = {
  api: [{ label: "Entities", slug: "entities" }],
  spec: [{ title: "Field", href: "/reference/plugin-spec/field/" }],
  plugins: [{ title: "Lots", href: "/reference/plugins/lots/" }],
};
const nav = siteNav(pages, generated);
const group = (section: string, label: string): NavGroup | undefined =>
  nav
    .find((candidate) => candidate.section === section)
    ?.entries.filter(isGroup)
    .find((candidate) => candidate.label === label);

describe("the site's navigation", () => {
  it("has the top navigation's sections, in its order, each linking its overview", () => {
    expect(nav.map((section) => section.section)).toEqual(sections);
    expect(nav.map((section) => section.href)).toEqual(TOP_NAV.map((entry) => entry.href));
  });

  it("lists every page once, whoever it's for", () => {
    const listed = nav.flatMap((section) =>
      section.entries.flatMap((entry) =>
        isGroup(entry) ? entry.links.map((link) => link.href) : [entry.href],
      ),
    );
    for (const page of pages) {
      expect(
        listed.filter((href) => href === page.href),
        page.href,
      ).toHaveLength(1);
    }
  });

  it("lists use cases by kind, then their order, in one list until there are many", () => {
    expect(nav[0]?.entries.map((entry) => ("href" in entry ? entry.href : entry.label))).toEqual([
      "/use-cases/reagents/",
      "/use-cases/dose/",
      "/use-cases/verify/",
    ]);
    const many = Array.from({ length: USE_CASES_BEFORE_GROUPS + 1 }, (_, n) => ({
      section: "Use cases" as const,
      audience: "bench" as const,
      label: `Use case ${String(n)}`,
      href: `/use-cases/${String(n)}/`,
      order: n,
      category: n % 2 ? ("analysis" as const) : ("collaboration" as const),
    }));
    expect(siteNav(many, generated)[0]?.entries.map((entry) => entry.label)).toEqual([
      "Analysis",
      "Collaboration",
    ]);
  });

  it("groups guides by who they're for, a guide's pages after its first", () => {
    expect(group("Guides", "Running Iki")?.links.map((link) => link.href)).toEqual([
      "/guides/laptop/",
    ]);
    expect(group("Guides", "Building on Iki")?.links.map((link) => link.href)).toEqual([
      "/guides/write-a-plugin/",
      "/guides/write-a-plugin/fields/",
      "/guides/write-a-plugin/states/",
    ]);
  });

  it("lists the generated references, each with its overview, then the written ones", () => {
    const reference = nav.find((section) => section.section === "Reference")?.entries ?? [];
    expect(reference.map((entry) => entry.label)).toEqual([
      "REST API",
      "Plugin spec",
      "Plugins",
      "The iki CLI",
    ]);
    expect(group("Reference", "REST API")?.links).toEqual([
      { label: "Overview", href: "/reference/api/" },
      { label: "Entities", href: "/reference/api/#entities" },
    ]);
  });

  it("marks the page being read, or the link it's under", () => {
    expect(currentIn(nav, "/concepts/corrections/")).toBe("/concepts/corrections/");
    expect(currentIn(nav, "/reference/api/get-entities/", "/reference/api/#entities")).toBe(
      "/reference/api/#entities",
    );
    expect(currentIn(nav, "/nowhere/")).toBeNull();
  });
});

describe("a guide in several pages", () => {
  it("is listed once, its other pages under its first in their order", () => {
    const guides = nested(
      pages.filter((page) => page.section === "Guides" && page.audience === "build"),
    );
    expect(guides.map((item) => item.label)).toEqual(["Write a plugin"]);
    expect(guides[0]?.children?.map((item) => item.href)).toEqual([
      "/guides/write-a-plugin/fields/",
      "/guides/write-a-plugin/states/",
    ]);
  });

  it("links each page to the ones before and after it", () => {
    expect(pager(pages, "/guides/write-a-plugin/")).toEqual({
      previous: undefined,
      next: { label: "Declare a type's fields", href: "/guides/write-a-plugin/fields/" },
    });
    expect(pager(pages, "/guides/write-a-plugin/states/")).toEqual({
      previous: { label: "Declare a type's fields", href: "/guides/write-a-plugin/fields/" },
      next: undefined,
    });
    expect(pager(pages, "/concepts/corrections/")).toEqual({});
  });
});

describe("the site's lists", () => {
  it("list only the site's own pages", () => {
    for (const item of REFERENCES) {
      expect(hrefOf(item), item.label).not.toBeNull();
      expect(item.source, item.label).toBeUndefined();
    }
  });
});
