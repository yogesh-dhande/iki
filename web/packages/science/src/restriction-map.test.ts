import { describe, expect, it } from "vitest";
import { bindingSiteCoordinate, digestFragments } from "./restriction-map.js";

describe("annotated reference coordinates", () => {
  // INSDC Feature Table, location descriptors, https://www.insdc.org/submitting-standards/feature-table/
  it("partitions a linear map once per distinct internal boundary", () => {
    expect(digestFragments(100, [75, 25, 0, 100, 75], "linear")).toEqual([
      { start: 1, end: 25, length: 25, topology: "linear" },
      { start: 26, end: 75, length: 50, topology: "linear" },
      { start: 76, end: 100, length: 25, topology: "linear" },
    ]);
  });
  it("retains circular topology until cut and joins the origin-spanning fragment", () => {
    expect(digestFragments(100, [], "circular")).toEqual([
      { start: 1, end: 100, length: 100, topology: "circular" },
    ]);
    expect(digestFragments(100, [100, 0], "circular")).toEqual([
      { start: 1, end: 100, length: 100, topology: "linear" },
    ]);
    expect(digestFragments(100, [25, 90], "circular")).toEqual([
      { start: 26, end: 90, length: 65, topology: "linear" },
      { start: 91, end: 25, length: 35, topology: "linear" },
    ]);
    expect(() => digestFragments(100, [101], "circular")).toThrow(RangeError);
  });
  it("projects offsets onto a recorded paired interval on either strand", () => {
    const forward = { start: 9, end: 2, strand: "forward" } as const;
    const reverse = { ...forward, strand: "reverse" } as const;
    expect([1, 2, 3, 4].map((i) => bindingSiteCoordinate(forward, i, 10, "circular"))).toEqual([
      2, 1, 10, 9,
    ]);
    expect([1, 2, 3, 4].map((i) => bindingSiteCoordinate(reverse, i, 10, "circular"))).toEqual([
      9, 10, 1, 2,
    ]);
    expect(() => bindingSiteCoordinate(reverse, 5, 10, "circular")).toThrow(RangeError);
    expect(() => bindingSiteCoordinate(forward, 1, 10, "linear")).toThrow(RangeError);
  });
});
