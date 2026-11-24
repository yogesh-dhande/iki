import { describe, expect, it } from "vitest";

import {
  REFERENCES,
  REPOSITORY_GUIDES,
  TOP_NAV,
  hrefOf,
  nested,
  pager,
  sideNav,
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
  },
  {
    section: "Use cases",
    audience: "bench",
    label: "Run a dose-response plate assay",
    href: "/use-cases/dose/",
    order: 10,
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

describe("the side navigation", () => {
  it("files pages under the top navigation's section names, in its order", () => {
    for (const audience of ["bench", "run", "build"] as const) {
      const names = sideNav(pages, audience).map((group) => group.section);
      for (const name of names) {
        expect(sections).toContain(name);
      }
      expect([...names].sort((a, b) => sections.indexOf(a) - sections.indexOf(b))).toEqual(names);
    }
  });

  it("lists an audience's own guides and use cases, every concept, each in its order", () => {
    expect(sideNav(pages, "bench")).toEqual([
      {
        section: "Use cases",
        items: [
          { label: "Run a dose-response plate assay", href: "/use-cases/dose/" },
          { label: "Verify a plasmid prep", href: "/use-cases/verify/" },
        ],
      },
      {
        section: "Concepts",
        items: [
          { label: "Records and revisions", href: "/concepts/records/" },
          { label: "Corrections", href: "/concepts/corrections/" },
        ],
      },
    ]);
    expect(sideNav(pages, "build").map((group) => group.section)).toEqual(["Guides", "Concepts"]);
    expect(sideNav(pages, "run").map((group) => group.section)).toEqual(["Concepts"]);
  });
});

describe("a guide in several pages", () => {
  it("is listed once, its other pages under its first in their order", () => {
    const guides = nested(pages.filter((page) => page.section === "Guides"));
    expect(guides.map((item) => item.label)).toEqual(["Write a plugin"]);
    expect(guides[0]?.children?.map((item) => item.href)).toEqual([
      "/guides/write-a-plugin/fields/",
      "/guides/write-a-plugin/states/",
    ]);
    expect(sideNav(pages, "build")[0]?.items[0]?.children).toHaveLength(2);
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
  it("list nothing that isn't written yet", () => {
    for (const item of [...Object.values(REPOSITORY_GUIDES).flat(), ...REFERENCES]) {
      expect(hrefOf(item), item.label).not.toBeNull();
    }
  });
});
