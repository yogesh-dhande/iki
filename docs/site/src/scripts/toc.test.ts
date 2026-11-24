import { describe, expect, it } from "vitest";

import { current } from "./toc.ts";

describe("the section being read", () => {
  it("is the last whose top has passed the line, or the first", () => {
    expect(current([100, 900, 1800], 160)).toBe(0);
    expect(current([-500, 120, 900], 160)).toBe(1);
    expect(current([-900, -400, -10], 160)).toBe(2);
    expect(current([400, 900], 160)).toBe(0);
  });
});
