import { describe, expect, it } from "vitest";

import { readMessage, readRefinement, type Declaration } from "./host.js";
import { MAX_CONDITIONS, MAX_DEPTH, MAX_QUERY_LENGTH, PROTOCOL } from "./index.js";

const BOARD: Declaration = {
  selects: null,
  entities: { fields: ["name", "notes"] },
  runs: ["acme/protein.compute"],
};

describe("what a page reads from a frame", () => {
  it("is ready only in the protocol it speaks", () => {
    expect(readMessage(BOARD, { type: "ready", protocol: PROTOCOL })).toEqual({
      type: "ready",
      protocol: PROTOCOL,
    });
    expect(readMessage(BOARD, { type: "ready", protocol: PROTOCOL + 1 })).toBeNull();
  });

  it("is a refinement only of the fields the component reads, within bounds", () => {
    const leaf = { field: "name", op: "eq", value: "AB" };
    expect(readRefinement(BOARD, { where: leaf })).toEqual({ where: leaf });
    expect(readRefinement(BOARD, { where: { ...leaf, field: "secret" } })).toBeUndefined();
    let deep: unknown = leaf;
    for (let depth = 1; depth < MAX_DEPTH; depth += 1) deep = { not: deep };
    expect(readRefinement(BOARD, { where: deep })).toBeDefined();
    expect(readRefinement(BOARD, { where: { not: deep } })).toBeUndefined();
    const many = Array.from({ length: MAX_CONDITIONS + 1 }, () => leaf);
    expect(readRefinement(BOARD, { where: { any: many } })).toBeUndefined();
    expect(readRefinement(BOARD, { q: "x".repeat(MAX_QUERY_LENGTH + 1) })).toBeUndefined();
    // Its sort always says which way.
    expect(readRefinement(BOARD, { sort: { field: "hrid" } })).toEqual({
      sort: { field: "hrid", descending: false },
    });
  });

  it("runs only an action the component declared", () => {
    expect(readMessage(BOARD, { type: "run", action: "acme/protein.compute" })).toEqual({
      type: "run",
      action: "acme/protein.compute",
    });
    expect(readMessage(BOARD, { type: "run", action: "acme/protein.delete" })).toBeNull();
    expect(readMessage({ ...BOARD, runs: [] }, { type: "run", action: "x" })).toBeNull();
  });
});
