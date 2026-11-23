import { describe, expect, it } from "vitest";
import { GEL_LADDERS } from "./gel-ladders.js";

describe("commercial gel ladders", () => {
  // Sizes and reference masses from https://www.neb.com/products/n3232-1-kb-dna-ladder
  it("preserves the N3232 fragment sizes and its 3001 bp reference band", () => {
    const { bands } = GEL_LADDERS["neb-1-kb"];
    expect(bands.map((band) => band.sizeBp)).toEqual([
      500, 517, 1000, 1500, 2000, 3001, 4001, 5001, 6001, 8001, 10002,
    ]);
    expect(bands.slice(0, 2).map((band) => band.massNg)).toEqual([21, 21]);
    expect(bands.find((band) => band.sizeBp === 3001)?.massNg).toBe(125);
  });

  // Sizes and reference masses from https://www.neb.com/products/n3231-100-bp-dna-ladder
  it("preserves the N3231 band sizes and its combined 500/517 bp reference band", () => {
    const { bands } = GEL_LADDERS["neb-100-bp"];
    expect(bands.map((band) => band.sizeBp)).toEqual([
      100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1200, 1517,
    ]);
    expect(bands.find((band) => band.sizeBp === 500)).toEqual({
      sizeBp: 500,
      companionSizeBp: 517,
      massNg: 97,
    });
    expect(bands.find((band) => band.sizeBp === 1000)?.massNg).toBe(95);
  });
});
