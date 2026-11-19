/** The sequence viewer's layout: rows, lanes, and features that wrap the origin. */

import { describe, expect, it } from "vitest";

import {
  columnOf,
  extremes,
  featureLength,
  kindOf,
  lanesPerRow,
  lengthText,
  locationText,
  piecesIn,
  readFeatures,
  readTrack,
  trackScale,
  type Feature,
} from "./sequence";

const PUC19 = [
  { type: "CDS", name: "bla", start: 1626, end: 2486, strand: "reverse" },
  { type: "promoter", name: "AmpR promoter", start: 2487, end: 2591, strand: "reverse" },
  { type: "rep_origin", name: "ori", start: 867, end: 1455, strand: "reverse" },
  { type: "CDS", name: "lacZα", start: 146, end: 469, strand: "reverse" },
  { type: "misc_feature", name: "MCS", start: 397, end: 453 },
];

describe("features", () => {
  it("are read as far as they can be drawn, and laned where they overlap", () => {
    const read = readFeatures([...PUC19, { type: "CDS", start: 0, end: 5 }, "junk"], 2686);
    expect(read.map((f) => [f.name, f.lane])).toEqual([
      ["bla", 0],
      ["AmpR promoter", 0],
      ["ori", 0],
      ["lacZα", 0],
      ["MCS", 1],
    ]);
    expect(read[4]?.strand).toBe("none");
    expect(readFeatures({ not: "a list" }, 10)).toEqual([]);
  });

  it("wrap the origin of a circular sequence when they end before they start", () => {
    const [wrapped] = readFeatures([{ type: "CDS", start: 2600, end: 120 }], 2686) as [Feature];
    expect(featureLength(wrapped, 2686)).toBe(87 + 120);
    expect(locationText(wrapped)).toBe("2600..120 (across the origin)");
    expect(piecesIn([wrapped], 1, 2686)).toMatchObject([{ column: 0, width: 65, arrow: null }]);
    expect(piecesIn([wrapped], 2641, 2686)).toMatchObject([{ shape: "part", arrow: null }]);
  });

  it("are drawn in their row's columns, with a gap after every ten residues", () => {
    expect(columnOf(0)).toBe(0);
    expect(columnOf(9)).toBe(9);
    expect(columnOf(10)).toBe(11);
    const [mcs] = readFeatures([{ type: "misc_feature", start: 397, end: 453 }], 2686) as [Feature];
    // Row 361..420 holds 397..420: offsets 36..59.
    expect(piecesIn([mcs], 361, 2686)).toEqual([
      { feature: mcs, column: 39, width: 26, shape: "part", arrow: null },
    ]);
  });

  it("cover only their parts: a join's, not what's between them, and a site none", () => {
    // INSDC: join(2..4,10..12) is six bases; 5^6 is the site between 5 and 6.
    const [joined, site] = readFeatures(
      [
        {
          type: "CDS",
          start: 2,
          end: 12,
          strand: "forward",
          parts: [
            { start: 2, end: 4, strand: "forward" },
            { start: 10, end: 12, strand: "forward" },
          ],
        },
        {
          type: "misc_feature",
          start: 5,
          end: 6,
          parts: [{ start: 5, end: 6, between: true }],
        },
      ],
      20,
    ) as [Feature, Feature];
    expect(featureLength(joined, 20)).toBe(6);
    expect(locationText(joined)).toBe("join(2..4,10..12)");
    expect(piecesIn([joined], 1, 20)).toEqual([
      { feature: joined, column: 4, width: 5, shape: "joining", arrow: null },
      { feature: joined, column: 1, width: 3, shape: "part", arrow: null },
      { feature: joined, column: 9, width: 4, shape: "part", arrow: "forward" },
    ]);
    expect(featureLength(site, 20)).toBe(0);
    expect(locationText(site)).toBe("5^6");
    expect(piecesIn([site], 1, 20)).toMatchObject([{ column: 4, shape: "site", arrow: null }]);
  });

  it("say when their parts are an order, not a join, and when their ends are partial", () => {
    // INSDC: order(2..4,10..12) says only that they're found in that order; <2..>9 goes
    // on past both ends, so 8 bases is only what's annotated of it.
    const number = new Intl.NumberFormat("en");
    const [ordered, partial, reverse, single] = readFeatures(
      [
        {
          type: "misc_feature",
          start: 2,
          end: 12,
          strand: "forward",
          operator: "order",
          parts: [
            { start: 2, end: 4, strand: "forward" },
            { start: 10, end: 12, strand: "forward" },
          ],
        },
        {
          type: "CDS",
          start: 2,
          end: 9,
          strand: "forward",
          parts: [{ start: 2, end: 9, strand: "forward", partial_start: true, partial_end: true }],
        },
        // SV40's VP1 from its other strand: complement(2651..>3745).
        {
          type: "CDS",
          start: 2651,
          end: 3745,
          strand: "reverse",
          parts: [{ start: 2651, end: 3745, strand: "reverse", partial_end: true }],
        },
        {
          type: "misc_feature",
          start: 7,
          end: 7,
          strand: "forward",
          parts: [{ start: 7, end: 7, strand: "forward", partial_start: true }],
        },
      ],
      5243,
    ) as [Feature, Feature, Feature, Feature];
    expect(locationText(ordered)).toBe("order(2..4,10..12)");
    expect(lengthText(ordered, 5243, number)).toBe("6");
    expect(locationText(partial)).toBe("<2..>9");
    expect(lengthText(partial, 5243, number)).toBe("at least 8");
    expect(locationText(reverse)).toBe("complement(2651..>3745)");
    expect(lengthText(reverse, 5243, number)).toBe("at least 1,095");
    expect(locationText(single)).toBe("<7");
  });

  it("cross the origin only where their parts do, the arrow at the last one's end", () => {
    // complement(join(90..100,1..5)) on a 100-base circle: 16 bases, read 5 to 1, then
    // 100 to 90, so it ends at 90.
    const [reverse] = readFeatures(
      [
        {
          type: "CDS",
          start: 90,
          end: 5,
          strand: "reverse",
          parts: [
            { start: 1, end: 5, strand: "reverse" },
            { start: 90, end: 100, strand: "reverse" },
          ],
        },
      ],
      100,
    ) as [Feature];
    expect(featureLength(reverse, 100)).toBe(16);
    expect(locationText(reverse)).toBe("complement(join(90..100,1..5)) (across the origin)");
    const pieces = piecesIn([reverse], 61, 100);
    expect(pieces.filter((p) => p.shape === "joining")).toEqual([]);
    expect(pieces.find((p) => p.arrow)).toMatchObject({ arrow: "reverse", column: 31 });
    expect(piecesIn([reverse], 1, 100).every((p) => p.arrow === null)).toBe(true);
  });

  it("say when their parts lie on both strands", () => {
    const [mixed] = readFeatures(
      [
        {
          type: "mRNA",
          start: 1,
          end: 20,
          strand: "mixed",
          parts: [
            { start: 1, end: 5, strand: "forward" },
            { start: 10, end: 20, strand: "reverse" },
          ],
        },
      ],
      30,
    ) as [Feature];
    expect(mixed.strand).toBe("mixed");
    expect(locationText(mixed)).toBe("join(1..5,complement(10..20))");
    // Parts that don't lie on the sequence leave the feature out.
    expect(
      readFeatures([{ type: "CDS", start: 1, end: 5, parts: [{ start: 1, end: 50 }] }], 30),
    ).toEqual([]);
  });

  it("say how many lanes each row needs", () => {
    const read = readFeatures(PUC19, 2686);
    const lanes = lanesPerRow(read, 2686);
    expect(lanes).toHaveLength(45);
    expect(lanes[0]).toBe(0);
    expect(lanes[6]).toBe(2); // 361..420: lacZα and the MCS
  });

  it("look like their kind", () => {
    expect(kindOf("CDS")).toBe("coding");
    expect(kindOf("rep_origin")).toBe("origin");
    expect(kindOf("misc_feature")).toBe("other");
  });
});

describe("tracks", () => {
  it("are read only when they hold one number, or null, per residue", () => {
    expect(readTrack([null, 1.5, -2], 3)).toEqual([null, 1.5, -2]);
    expect(readTrack([1, 2], 3)).toBeNull();
    expect(readTrack([1, "2", 3], 3)).toBeNull();
    expect(readTrack([1, Number.NaN, 3], 3)).toBeNull();
    expect(readTrack("1,2,3", 3)).toBeNull();
  });

  it("scale from their field's bounds, or else their values, with zero in them", () => {
    expect(trackScale([null, 1.5, -2], -4.5, 4.5)).toEqual([-4.5, 4.5]);
    expect(trackScale([null, 1.5, -2], null, null)).toEqual([-2, 1.5]);
    expect(trackScale([3, 5], null, null)).toEqual([0, 5]);
    expect(trackScale([null, null], null, null)).toEqual([0, 1]);
  });

  it("say where their highest and lowest values are", () => {
    expect(extremes([null, 1.5, -2, 1.5])).toEqual({ highest: [1.5, 2], lowest: [-2, 3] });
    expect(extremes([null, null])).toBeNull();
  });
});
