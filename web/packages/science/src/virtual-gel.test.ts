import { assert, describe, expect, it } from "vitest";
import { gelLanes, migrationAt } from "./gel-layout.js";
import "./virtual-gel.js";

describe("virtual gel", () => {
  it("preserves size order and scales migration to the selected gel percentage", () => {
    const one = migrationAt(1);
    const two = migrationAt(2);
    // Effective separation anchors from MAN0012614, Table 1.
    // https://assets.thermofisher.com/TFS-Assets/LSG/manuals/MAN0012614_Gen_Recommend_DNA_Electrophoresis_UG.pdf
    expect(one(8000)).toBeCloseTo(0.2);
    expect(one(400)).toBeCloseTo(0.8);
    expect(two(2000)).toBeCloseTo(0.2);
    expect(two(100)).toBeCloseTo(0.8);
    // Southern 1979 treats the semilog relation as an approximation.
    // https://doi.org/10.1016/0003-2697(79)90235-5
    expect(one(50_000)).toBeLessThan(one(1000));
    expect(one(25)).toBeGreaterThan(one(1000));
    expect(two(1000)).toBeLessThan(one(1000));
    expect(migrationAt(1.1)(1000)).toBeCloseTo((one(1000) + migrationAt(1.2)(1000)) / 2);
  });

  it("combines equal sizes and preserves relative, reported and apportioned mass", () => {
    const fragments = [{ sizeBp: 500 }, { sizeBp: 500 }, { sizeBp: 1000 }];
    const [relative, apportioned, measured] = gelLanes([
      { label: "Synthetic", fragments },
      { label: "Synthetic with known mass", fragments, totalMassNg: 40 },
      {
        label: "Synthetic with measured fragments",
        fragments: [
          { sizeBp: 500, massNg: 8 },
          { sizeBp: 500, massNg: 1 },
        ],
      },
    ]);
    expect(relative?.bands).toMatchObject([
      { sizeBp: 1000, count: 1, massNg: undefined, weight: 1000 },
      { sizeBp: 500, count: 2, massNg: undefined, weight: 1000 },
    ]);
    expect(apportioned?.bands.map(({ massNg }) => massNg)).toEqual([20, 20]);
    expect(measured?.bands).toMatchObject([{ sizeBp: 500, count: 2, massNg: 9, weight: 9 }]);
  });

  it("renders the published 500 and 517 bp ladder fragments as a combined band", () => {
    const [oneKb, hundredBp] = gelLanes([{ ladder: "neb-1-kb" }, { ladder: "neb-100-bp" }]);
    // N3232 describes ten visible bands and associates fragments 10a/10b.
    // https://www.neb.com/products/n3232-1-kb-dna-ladder
    expect(oneKb?.bands).toHaveLength(10);
    expect(oneKb?.bands.at(-1)).toMatchObject({
      sizeBp: 500,
      companionSizeBp: 517,
      count: 2,
      massNg: 42,
    });
    // N3231 publishes the combined approximate mass in its table.
    // https://www.neb.com/products/n3231-100-bp-dna-ladder
    expect(hundredBp?.bands).toHaveLength(12);
    expect(hundredBp?.bands.find(({ sizeBp }) => sizeBp === 500)?.massNg).toBe(97);
  });

  it("aligns matching sizes across lanes and reports mass and duplicates in the details", () => {
    const gel = document.createElement("iki-virtual-gel");
    gel.data = {
      agarosePercent: 1,
      lanes: [
        { ladder: "neb-1-kb" },
        {
          label: "Synthetic sample",
          fragments: [{ sizeBp: 1000 }, { sizeBp: 500 }, { sizeBp: 500 }],
          totalMassNg: 120,
        },
      ],
    };
    document.body.append(gel);
    try {
      const ladder = gel.querySelector<HTMLElement>('[title="NEB 1 kb DNA Ladder · 1,000 bp"]');
      const sample = gel.querySelector<HTMLElement>('[title="Synthetic sample · 1,000 bp"]');
      assert(ladder && sample);
      const position = sample.style.getPropertyValue("--band-position");
      expect(position).toBe(ladder.style.getPropertyValue("--band-position"));
      expect(Number(sample.style.getPropertyValue("--band-intensity"))).toBeGreaterThan(
        Number(ladder.style.getPropertyValue("--band-intensity")),
      );
      const details = gel.querySelector("details");
      assert(details);
      details.open = true;
      expect(gel.querySelector("tbody:last-child tr:last-child")?.textContent).toBe("500260");
      gel.data = { ...gel.data, agarosePercent: 2 };
      expect(gel.querySelector("details")?.open).toBe(true);
      expect(
        gel
          .querySelector<HTMLElement>('[title="Synthetic sample · 1,000 bp"]')
          ?.style.getPropertyValue("--band-position"),
      ).not.toBe(position);
    } finally {
      gel.remove();
    }
  });
});
