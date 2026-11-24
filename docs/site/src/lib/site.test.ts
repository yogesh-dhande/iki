import { describe, expect, it } from "vitest";

import { CONCEPTS, DOORS, REFERENCES, SIDE_NAV, TOP_NAV, hrefOf } from "./site.ts";

const sections: string[] = TOP_NAV.map((entry) => entry.label);

describe("the site's navigation", () => {
  it("files the side navigation under the top navigation's section names, in its order", () => {
    for (const groups of Object.values(SIDE_NAV)) {
      const names = groups.map((group) => group.section);
      for (const name of names) {
        expect(sections).toContain(name);
      }
      expect([...names].sort((a, b) => sections.indexOf(a) - sections.indexOf(b))).toEqual(names);
    }
  });

  it("lists only this site's pages in the side navigation", () => {
    for (const groups of Object.values(SIDE_NAV)) {
      for (const item of groups.flatMap((group) => group.items)) {
        expect(item.href, item.label).toMatch(/^\//);
      }
    }
  });

  it("lists nothing that isn't written yet", () => {
    const items = [...Object.values(DOORS).flat(), ...CONCEPTS, ...REFERENCES];
    for (const item of items) {
      expect(hrefOf(item), item.label).not.toBeNull();
    }
  });
});
