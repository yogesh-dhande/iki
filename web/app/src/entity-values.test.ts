import { describe, expect, it } from "vitest";

import type { EntityType } from "./entities";
import { encode, isBlank, localDateTime, OTHER_UNIT, toDraft } from "./entity-values";
import { field, PROTEIN } from "./entity-fixtures";

const TYPE: EntityType = {
  ...PROTEIN,
  values_schema: {
    properties: {
      count: { anyOf: [{ type: "integer", minimum: 0, maximum: 10 }, { type: "null" }] },
      name: { type: "string", minLength: 1, maxLength: 5, pattern: "^[^\\r\\n]*$" },
      bases: {
        type: "string",
        allOf: [{ pattern: "^[ACGT]*$" }, { pattern: "^[^\\r\\n]*$" }],
        "x-iki-format": "dna_sequence",
      },
      volume: { anyOf: [{ type: "number", minimum: 0 }, { type: "null" }] },
      tags: { type: "array", items: { type: "string", maxLength: 3 }, maxItems: 2 },
    },
  },
};

const count = field({ name: "count", label: "Count", type: "int" });
const volume = field({
  name: "volume",
  label: "Volume",
  type: "quantity",
  unit: "mL",
  unit_symbol: "mL",
  display_unit: "uL",
  display_unit_symbol: "μL",
  display_scale: 1000,
  display_offset: 0,
  input_units: [
    { code: "uL", symbol: "μL" },
    { code: "mL", symbol: "mL" },
    { code: "L", symbol: "L" },
  ],
});

describe("encoding drafts", () => {
  it("checks whole numbers and their range", () => {
    expect(encode(count, "4", TYPE)).toEqual({ ok: true, value: 4 });
    expect(encode(count, "4.5", TYPE)).toEqual({
      ok: false,
      error: "Enter a whole number, such as 42.",
    });
    expect(encode(count, "11", TYPE)).toEqual({ ok: false, error: "It must be at most 10." });
    expect(encode(count, " ", TYPE)).toEqual({ ok: true, value: null });
  });

  it("refuses numbers written with separators, which read differently by locale", () => {
    const f = field({ name: "x", label: "X", type: "float" });
    expect(encode(f, "1,5", TYPE).ok).toBe(false);
    expect(encode(f, "2e-3", TYPE)).toEqual({ ok: true, value: 0.002 });
  });

  it("sends quantities in other units with their unit, and checks ranges in its own", () => {
    expect(encode(volume, { number: "2.5", unit: "L", other: "" }, TYPE)).toEqual({
      ok: true,
      value: { value: 2.5, unit: "L" },
    });
    expect(encode(volume, { number: "2.5", unit: "mL", other: "" }, TYPE)).toEqual({
      ok: true,
      value: 2.5,
    });
    expect(encode(volume, { number: "-1", unit: "mL", other: "" }, TYPE)).toEqual({
      ok: false,
      error: "It must be at least 0 mL.",
    });
    expect(encode(volume, { number: "3", unit: OTHER_UNIT, other: " dL " }, TYPE)).toEqual({
      ok: true,
      value: { value: 3, unit: "dL" },
    });
    expect(encode(volume, { number: "3", unit: OTHER_UNIT, other: "" }, TYPE)).toEqual({
      ok: false,
      error: "Enter the unit, as a UCUM code such as mg/mL.",
    });
  });

  it("explains a format's pattern in words", () => {
    const bases = field({ name: "bases", label: "Bases", type: "string" });
    expect(encode(bases, "ACGX", TYPE)).toEqual({
      ok: false,
      error: "Use DNA bases only: A, C, G, T, or IUPAC codes such as N.",
    });
    const name = field({ name: "name", label: "Name", type: "string" });
    expect(encode(name, "  abcdef ", TYPE)).toEqual({
      ok: false,
      error: "Use at most 5 characters: this is 6.",
    });
  });

  it("leaves blank list items out, and says which item is wrong", () => {
    const tags = field({ name: "tags", label: "Tags", type: "list", items: "string" });
    expect(encode(tags, ["a", " ", "b"], TYPE)).toEqual({ ok: true, value: ["a", "b"] });
    expect(encode(tags, ["a", "long"], TYPE)).toEqual({
      ok: false,
      error: "Item 2: Use at most 3 characters: this is 4.",
    });
    expect(encode(tags, ["a", "b", "c"], TYPE)).toEqual({
      ok: false,
      error: "Add at most 2: there are 3.",
    });
    expect(isBlank(tags, ["", " "])).toBe(true);
  });

  it("reads times in the browser's time zone and sends them in UTC", () => {
    const at = field({ name: "at", label: "At", type: "datetime" });
    const local = localDateTime("2026-03-01T09:30:00Z");
    expect(encode(at, local, TYPE)).toEqual({ ok: true, value: "2026-03-01T09:30:00.000Z" });
  });
});

describe("drafts from values", () => {
  it("shows quantities in the unit people read them in", () => {
    expect(toDraft(volume, 0.25, { refs: {}, files: {} })).toEqual({
      number: "250",
      unit: "uL",
      other: "",
    });
  });

  it("names references by their title", () => {
    const source = field({ name: "source", label: "Source", type: "ref", to: "protein.Protein" });
    const refs = {
      e1: { hrid: "PRT-000001", title: "ABL1 kinase", type: "protein.Protein", archived: false },
    };
    expect(toDraft(source, "e1", { refs, files: {} })).toEqual({
      id: "e1",
      hrid: "PRT-000001",
      title: "ABL1 kinase",
    });
  });
});
