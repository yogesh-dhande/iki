/** Laying out a graph in columns: steps that run side by side share a column. */

import { describe, expect, it } from "vitest";

import { arrange } from "./dag";

describe("arranging a graph", () => {
  it("puts each node one column right of the furthest before it", () => {
    // a → b → d, a → c → d, and e on its own.
    const { places, edges } = arrange(
      ["a", "b", "c", "d", "e"],
      [
        ["a", "b"],
        ["a", "c"],
        ["b", "d"],
        ["c", "d"],
        ["a", "b"], // repeated: drawn once
        ["a", "nowhere"], // to a node it wasn't given: left out
      ],
    );
    const column = Object.fromEntries(places.map((p) => [p.key, p.x]));
    expect(column["a"]).toBe(0);
    expect(column["e"]).toBe(0);
    expect(column["b"]).toBe(column["c"]);
    expect(column["d"]).toBeGreaterThan(column["b"] ?? 0);
    expect(edges.map((e) => `${e.from}>${e.to}`)).toEqual(["a>b", "a>c", "b>d", "c>d"]);
  });

  it("lays out nothing as nothing", () => {
    expect(arrange([], [])).toMatchObject({ places: [], edges: [], width: 0 });
  });
});
