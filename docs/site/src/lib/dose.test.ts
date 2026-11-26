import { describe, expect, it } from "vitest";

import { curve, FRAME, level, nM, response, ticks, x, y } from "./dose.ts";

const fit = { bottom: 0.1, top: 1, endpoint_value: 12, hill_slope: -1 };

describe("the dose-response figure", () => {
  it("draws the four-parameter curve the plugin fits: halfway at the midpoint", () => {
    expect(response(fit, 12)).toBeCloseTo(0.55, 10);
    // The curve-example's generator: 0.1 + 0.9 / (1 + c / 12).
    for (const c of [1, 8, 128]) {
      expect(response(fit, c)).toBeCloseTo(0.1 + 0.9 / (1 + c / 12), 10);
    }
  });

  it("places concentrations on a log₂ axis from 0.5 to 256 nM, as the mockup does", () => {
    expect(x(0.5)).toBe(FRAME.left);
    expect(x(256)).toBe(FRAME.right);
    expect(x(8)).toBeCloseTo(56 + ((3 + 1) / 9) * 480, 10);
    expect(y(0)).toBe(FRAME.bottom);
    expect(y(1.1)).toBe(FRAME.top);
    expect(ticks()).toEqual([2, 8, 32, 128]);
  });

  it("draws the curve across the whole axis", () => {
    const path = curve(fit as never);
    expect(path.startsWith(`M ${FRAME.left.toFixed(1)} `)).toBe(true);
    expect(path.split(" L ")).toHaveLength(91);
  });

  it("writes concentrations as the guide does, and tints wells in eight steps", () => {
    expect(nM(12.33962)).toBe("12.34");
    expect([level(0, 1), level(0.5, 1), level(1, 1), level(2, 1)]).toEqual([1, 4, 8, 8]);
  });
});
