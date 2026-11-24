import { describe, expect, it } from "vitest";

import {
  GROUPS,
  loadSchemas,
  specPages,
  type Group,
  type Key,
  type Schema,
  type SpecPage,
} from "./spec.ts";

const pages = specPages(loadSchemas());
const page = (name: string): SpecPage => {
  const found = pages.find((candidate) => candidate.name === name);
  if (!found) throw new Error(`No page ${name}`);
  return found;
};
const key = (name: string, id: string): Key => {
  const found = page(name)
    .sections.flatMap((section) => section.keys)
    .find((candidate) => candidate.id === id);
  if (!found) throw new Error(`No key ${id} on ${name}`);
  return found;
};
const text = (k: Key) =>
  k.type.map((part) => (typeof part === "string" ? part : part.text)).join("");

describe("the plugin spec reference, from the repository's schemas", () => {
  it("has a page for every schema in a group, in the groups' order", () => {
    expect(pages.map((p) => p.name)).toEqual(GROUPS.flatMap((group) => [...group.schemas]));
    expect(page("entity-type").title).toBe("EntityType");
    expect(page("entity-type").href).toBe("/reference/plugin-spec/entity-type/");
    expect(page("field").title).toBe("Field");
  });

  it("describes every field key, the six that had none among them", () => {
    for (const id of [
      "computed",
      "registry",
      "immutable",
      "unique",
      "indexed",
      "link",
      "label",
      "description",
      "min_length",
      "max_length",
      "min_items",
      "max_items",
    ]) {
      expect(key("field", id).description, id).not.toBe("");
    }
    expect(key("field", "registry").values).toEqual(["derived", "fact"]);
  });

  it("nests keys beneath the ones they're inside, each with its path as its anchor", () => {
    const meaning = key("field", "states.signatures.meaning");
    expect(meaning.depth).toBe(2);
    expect(meaning.required).toBe(true);
    expect(meaning.values).toContain("approval");
    expect(key("entity-type", "hrid.prefix").depth).toBe(1);
    expect(key("action", "effects.creates.match_on").depth).toBe(2);
  });

  it("links a key to the page of what it holds rather than repeating its keys", () => {
    const fields = key("entity-type", "fields");
    expect(fields.type).toContainEqual({ text: "Field", href: "/reference/plugin-spec/field/" });
    expect(page("entity-type").sections[0]?.keys.some((k) => k.id.startsWith("fields."))).toBe(
      false,
    );
    expect(key("field", "states.guards").type).toContainEqual({
      text: "Guards",
      href: "/reference/plugin-spec/rule/#guard",
    });
    expect(key("action", "params").also).toContainEqual({
      text: "a field type's options",
      href: "/reference/plugin-spec/field/#by-type",
    });
  });

  it("gives a rule's and a guard's keys, and a bundle's lines, sections of their own", () => {
    expect(page("rule").sections.map((s) => s.id)).toEqual(["rule", "guard"]);
    expect(key("rule", "rule.hold.reviewed_by").required).toBe(true);
    expect(key("rule", "guard.check").required).toBe(true);
    expect(page("bundle").sections.map((s) => s.title)).toContain("signatures.ndjson");
  });

  it("lists the keys each field type takes", () => {
    const string = page("field").byType.find((row) => row.type === "string");
    expect(string?.keys).toEqual(expect.arrayContaining(["unique", "link", "pattern_help"]));
    expect(page("field").byType.find((row) => row.type === "enum")?.required).toContain("values");
    expect(page("settings").byType).toEqual([]);
  });

  it("says when a key is allowed only with another's value", () => {
    expect(key("entity-type", "display.viewers.sequence").when).toBe("With `viewer: sequence`");
    expect(key("entity-type", "display.viewers.viewer").when).toBeUndefined();
  });

  it("cites no design records", () => {
    const all = pages.flatMap((p) => [
      p.description,
      ...p.sections.flatMap((s) => [s.description, ...s.keys.map((k) => k.description)]),
    ]);
    expect(all.filter((t) => /\bADRs? \d{4}\b/.test(t))).toEqual([]);
  });
});

const group = (schemas: string[]): Group[] => [{ id: "g", title: "G", text: "", schemas }];
const one = (schema: Schema) => new Map<string, Schema>([["thing", schema]]);

describe("the generator", () => {
  it("refuses a key without a description, naming it", () => {
    const schemas = one({
      properties: {
        kind: { const: "Thing", description: "What it is." },
        size: { type: "object", description: "How big.", properties: { unit: { type: "string" } } },
      },
    });
    expect(() => specPages(schemas, group(["thing"]), {})).toThrow(
      "/reference/plugin-spec/thing/#size.unit has no description",
    );
  });

  it("refuses a schema in no group, and a definition no page shows", () => {
    const schemas = new Map<string, Schema>([
      ["thing", { $defs: { loose: { properties: { a: { description: "A." } } } } }],
      ["other", {}],
    ]);
    expect(() => specPages(schemas, group(["thing"]), {})).toThrow(
      "other isn't in any of the reference's groups",
    );
    expect(() => specPages(schemas, group(["thing", "other"]), {})).toThrow(
      "thing#/$defs/loose isn't shown on any page",
    );
  });

  it("takes a description from what a key refers to, without its citations", () => {
    const schemas = one({
      properties: { name: { $ref: "#/$defs/name" } },
      $defs: { name: { type: "string", description: "Its name (ADR 0113, ADR 0114)." } },
    });
    const [thing] = specPages(schemas, group(["thing"]), {});
    expect(thing?.sections[0]?.keys[0]?.description).toBe("Its name.");
  });

  it("doesn't require a key only a union's member requires, and links a key that holds its parent", () => {
    const schemas = one({
      properties: { where: { $ref: "#/$defs/condition" } },
      $defs: {
        condition: {
          description: "A condition.",
          oneOf: [
            { required: ["field"], properties: { field: { type: "string", description: "F." } } },
            {
              required: ["all"],
              properties: {
                all: { type: "array", description: "All.", items: { $ref: "#/$defs/condition" } },
              },
            },
          ],
        },
      },
    });
    const keys = specPages(schemas, group(["thing"]), {})[0]?.sections[0]?.keys ?? [];
    expect(keys.map((k) => [k.id, k.required])).toEqual([
      ["where", false],
      ["where.field", false],
      ["where.all", false],
    ]);
    expect(keys[2]?.type).toEqual([
      "list of ",
      { text: "where", href: "/reference/plugin-spec/thing/#where" },
    ]);
    expect(keys[0] && text(keys[0])).toBe("object");
  });
});
