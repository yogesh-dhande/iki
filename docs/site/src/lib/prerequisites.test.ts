import { describe, expect, it } from "vitest";

import { prerequisites, unknownPlugins } from "./prerequisites.ts";

describe("a use case's prerequisites", () => {
  it("may give any part of them, or none", () => {
    expect(prerequisites.parse({})).toEqual({});
    expect(prerequisites.parse({ plugins: [{ name: "iki/lots" }] })).toEqual({
      plugins: [{ name: "iki/lots" }],
    });
  });

  it("name only plugins Iki has, so each links to its entry", () => {
    const known = [{ name: "iki/lots" }];
    expect(unknownPlugins({ plugins: [{ name: "iki/lots", version: ">=9" }] }, known)).toEqual([]);
    expect(unknownPlugins({ plugins: [{ name: "iki/nope" }] }, known)).toEqual([
      "iki/nope isn't one of Iki's plugins",
    ]);
    expect(unknownPlugins({}, known)).toEqual([]);
  });
});
