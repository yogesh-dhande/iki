// The plugin spec's reference, generated from the iki/v1 JSON Schemas in
// spec/src/iki_spec/schemas: a page for each kind of manifest or document, and every key
// it takes, nested keys beneath their parents, with its type, allowed values, default,
// limits and description. Nothing here is written by hand but the grouping, and the build
// fails if a schema isn't in a group, if a key has no description, or if a definition
// can't be reached from any page.
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { withoutCitations } from "./markdown.ts";
import { ROOT } from "./repo.ts";

/** Where the schemas are, from the repository's root. */
export const SCHEMAS = "spec/src/iki_spec/schemas";
/** The URL each schema's `$id` starts with. */
export const BASE = "https://ikisci.org/schemas/";
export const SPEC = "/reference/plugin-spec/";

export interface Schema {
  $id?: string;
  $ref?: string;
  $defs?: Record<string, Schema>;
  title?: string;
  description?: string;
  type?: string | string[];
  format?: string;
  const?: unknown;
  enum?: unknown[];
  default?: unknown;
  examples?: unknown[];
  properties?: Record<string, Schema | boolean>;
  required?: string[];
  additionalProperties?: Schema | boolean;
  propertyNames?: Schema;
  items?: Schema;
  allOf?: Schema[];
  anyOf?: Schema[];
  oneOf?: Schema[];
  if?: Schema;
  then?: Schema;
  else?: Schema;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  maxItems?: number;
  minProperties?: number;
  maxProperties?: number;
  not?: Schema;
}

export interface Group {
  id: string;
  title: string;
  /** Markdown: what its schemas are. */
  text: string;
  schemas: readonly string[];
}

/** The reference's sections, in order: each schema is in one. */
export const GROUPS: readonly Group[] = [
  {
    id: "manifests",
    title: "Plugin manifests",
    text: "The YAML files a plugin is made of, each with its `kind`.",
    schemas: [
      "plugin",
      "entity-type",
      "entity-type-extension",
      "action",
      "environment",
      "workflow",
      "trigger",
      "component",
      "action-test",
    ],
  },
  {
    id: "parts",
    title: "Fields, rules and settings",
    text: "What record types and plugins declare inside their manifests.",
    schemas: ["field", "rule", "settings", "unit"],
  },
  {
    id: "runs",
    title: "What a run reads and writes",
    text: "The JSON a worker gives an action's command, and the JSON the command writes back.",
    schemas: ["run-input", "run-output", "setup-input"],
  },
  {
    id: "deployment",
    title: "A deployment's configuration",
    text: "The files in a deployment's config repository: its plugins, and the machines their actions run on.",
    schemas: [
      "plugin-lock",
      "compute-template",
      "environment-binding",
      "environment-image-binding",
      "api-price",
    ],
  },
  {
    id: "bundles",
    title: "Bundles",
    text: "What carries a project's records from one deployment to another.",
    schemas: ["bundle"],
  },
];

/** Schemas that pick another by `kind`, for an editor to check any file: described on the
 * reference's index, with the kinds they take. */
export const CHOOSERS = ["manifest", "compute"] as const;

/** Definitions of a schema with a section of their own, and where in it they are: a
 * rule's and a guard's keys, and each of a bundle's lines. `intro` is the definition whose
 * description heads the section, when it isn't the section's own. */
export type Sections = Record<string, { def: string; title: string; intro?: string }[]>;

export const SECTIONS: Sections = {
  rule: [
    { def: "rule", title: "Rules", intro: "rules" },
    { def: "guard", title: "Guards", intro: "guards" },
  ],
  bundle: [
    { def: "person", title: "people.ndjson" },
    { def: "agent", title: "agents.ndjson" },
    { def: "file", title: "files.ndjson" },
    { def: "entry", title: "entries.ndjson" },
    { def: "entity", title: "entities.ndjson" },
    { def: "outside", title: "outside.ndjson" },
    { def: "run", title: "runs.ndjson" },
    { def: "signature", title: "signatures.ndjson" },
  ],
};

/** Definitions shown in a place of their own, which elsewhere link there rather than
 * repeat their keys: a field type's options, on the field's page. */
export const SHARED: Record<string, { href: string; label: string }> = {
  "field#/$defs/typed": {
    href: "/reference/plugin-spec/field/#by-type",
    label: "a field type's options",
  },
};

/** A key's type as a reader reads it: text, and links to the pages or keys it names. */
export type TypePart = string | { text: string; href: string };

export interface Key {
  /** Its anchor: its path of keys from its section's root, `states.set_by`. */
  id: string;
  name: string;
  /** How deep it is: 0 for a section's own keys. */
  depth: number;
  type: TypePart[];
  required: boolean;
  /** Its allowed values, when the schema lists them. */
  values: string[];
  /** Its default, as JSON. */
  default?: string;
  /** Its limits, in words: "at most 50 characters". */
  notes: string[];
  /** Markdown, without design-record citations. */
  description: string;
  /** The condition it's allowed under, in words: "With `viewer: sequence`". */
  when?: string;
  /** What else its value takes, shown elsewhere: a field type's options. */
  also: { text: string; href: string }[];
}

export interface Section {
  id: string;
  title: string;
  /** Markdown. */
  description: string;
  keys: Key[];
  also: { text: string; href: string }[];
}

export interface SpecPage {
  name: string;
  href: string;
  /** Its kind (`EntityType`), or what it is ("Field"). */
  title: string;
  kind?: string;
  group: string;
  /** Markdown. */
  description: string;
  /** The URL editors check its files against. */
  id: string;
  sections: Section[];
  /** For a schema whose keys depend on a `type`: the keys each type takes. */
  byType: { type: string; keys: string[]; required: string[] }[];
  /** A schema of a single value (a unit): its examples. */
  examples: string[];
}

/** Every schema, by name, from the repository. */
export function loadSchemas(root: string = ROOT): Map<string, Schema> {
  const directory = join(root, SCHEMAS);
  const schemas = new Map<string, Schema>();
  for (const name of readdirSync(directory, { withFileTypes: true })) {
    if (!name.isDirectory() || name.name.startsWith("_")) continue;
    const file = join(directory, name.name, "v1.json");
    schemas.set(name.name, JSON.parse(readFileSync(file, "utf-8")) as Schema);
  }
  return schemas;
}

/** A place in the schemas: a schema by name, and a JSON pointer in it ("" for its root). */
interface Place {
  schema: string;
  pointer: string;
}

const placeKey = (place: Place) => `${place.schema}#${place.pointer}`;

function lookup(schemas: Map<string, Schema>, place: Place): Schema {
  let node: unknown = schemas.get(place.schema);
  for (const part of place.pointer.split("/").filter(Boolean)) {
    node = (node as Record<string, unknown> | undefined)?.[part.replace(/~1/g, "/")];
  }
  if (node === undefined || typeof node !== "object" || node === null) {
    throw new Error(`${placeKey(place)} isn't in the schemas`);
  }
  return node;
}

function target(ref: string, from: string): Place {
  if (ref.startsWith("#")) return { schema: from, pointer: ref.slice(1) };
  const [url = "", pointer = ""] = ref.split("#");
  const match = new RegExp(`^${BASE.replace(/[.]/g, "\\.")}([a-z-]+)/v1$`).exec(url);
  if (!match?.[1]) throw new Error(`${ref} isn't one of the iki/v1 schemas`);
  return { schema: match[1], pointer };
}

/** A schema in place: where its `$ref`s are resolved from. */
interface At {
  node: Schema;
  base: string;
}

/** A page's title: its kind, or its schema's title without "Iki" and "(iki/v1)". */
function titleOf(schema: Schema, name: string): { title: string; kind?: string } {
  const kind = (schema.properties?.["kind"] as Schema | undefined)?.const;
  if (typeof kind === "string") return { title: kind, kind };
  const plain = (schema.title ?? name).replace(/^Iki /, "").replace(/ \(iki\/v1\)$/, "");
  return { title: plain.charAt(0).toUpperCase() + plain.slice(1) };
}

export function pageHref(name: string): string {
  return `${SPEC}${name}/`;
}

/** The description as the site shows it: without citations of design records. */
export function plain(text: string): string {
  return withoutCitations(text).trim();
}

function json(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

class Generator {
  /** Where each page and section is, by the place it's generated from. */
  readonly linked = new Map<string, { href: string; label: string }>();
  /** Where each definition was first shown as keys: what a recursive key links to. */
  readonly shownAt = new Map<string, { href: string; label: string }>();
  readonly missing: string[] = [];
  readonly reached = new Set<string>();
  readonly schemas: Map<string, Schema>;
  readonly sections: Sections;

  constructor(schemas: Map<string, Schema>, groups: readonly Group[], sections: Sections) {
    this.schemas = schemas;
    this.sections = sections;
    for (const [key, link] of Object.entries(SHARED)) {
      if (schemas.has(key.split("#")[0] ?? "")) this.linked.set(key, link);
    }
    for (const group of groups) {
      for (const name of group.schemas) {
        const schema = this.schema(name);
        this.linked.set(placeKey({ schema: name, pointer: "" }), {
          href: pageHref(name),
          label: titleOf(schema, name).title,
        });
        for (const section of sections[name] ?? []) {
          this.linked.set(placeKey({ schema: name, pointer: `/$defs/${section.def}` }), {
            href: `${pageHref(name)}#${section.def}`,
            label: section.title,
          });
          if (section.intro) {
            // A map of the section's definitions links to the section too.
            this.linked.set(placeKey({ schema: name, pointer: `/$defs/${section.intro}` }), {
              href: `${pageHref(name)}#${section.def}`,
              label: section.title,
            });
          }
        }
      }
    }
  }

  schema(name: string): Schema {
    const schema = this.schemas.get(name);
    if (!schema) throw new Error(`No schema ${name} in ${SCHEMAS}`);
    return schema;
  }

  /** Follows a schema's own `$ref` when it has nothing else: `{ $ref }` is its target. */
  deref(at: At): { at: At; place?: Place } {
    const { node, base } = at;
    if (node.$ref && !node.properties && !node.items && !node.additionalProperties) {
      const place = target(node.$ref, base);
      return { at: { node: lookup(this.schemas, place), base: place.schema }, place };
    }
    return { at };
  }

  /** A key's description: its own, or what it refers to has. */
  describe(at: At): string {
    if (at.node.description) return at.node.description;
    if (at.node.$ref) {
      const place = target(at.node.$ref, at.base);
      const found = lookup(this.schemas, place);
      return this.describe({ node: found, base: place.schema });
    }
    return "";
  }

  /** The schema's alternatives: its union's members, or its if/then/else branches. */
  alternatives(at: At): At[] {
    const { node, base } = at;
    const union = node.oneOf ?? node.anyOf;
    if (union) return union.map((member) => ({ node: member, base }));
    return [];
  }

  typeOf(at: At, stack: string[]): TypePart[] {
    const { node, base } = at;
    // An object with keys of its own and a `$ref` merged in is an object.
    if (node.$ref && (node.properties || node.type === "object")) {
      return ["object"];
    }
    if (node.$ref) {
      const place = target(node.$ref, base);
      const link = this.linked.get(placeKey(place));
      if (link) return [{ text: link.label, href: link.href }];
      const shown = this.shownAt.get(placeKey(place));
      if (stack.includes(placeKey(place)) && shown)
        return [{ text: shown.label, href: shown.href }];
      return this.typeOf({ node: lookup(this.schemas, place), base: place.schema }, stack);
    }
    if (node.const !== undefined) return [json(node.const)];
    const union = this.alternatives(at);
    if (union.length) {
      const parts: TypePart[] = [];
      const seen = new Set<string>();
      for (const member of union) {
        const text = this.typeOf(member, stack);
        const key = JSON.stringify(text);
        if (seen.has(key)) continue;
        seen.add(key);
        if (parts.length) parts.push(" or ");
        parts.push(...text);
      }
      return parts;
    }
    const types = Array.isArray(node.type) ? node.type : node.type ? [node.type] : [];
    if (types.length > 1) {
      return types.flatMap((type, index) =>
        index
          ? [" or ", ...this.typeOf({ node: { ...node, type }, base }, stack)]
          : this.typeOf({ node: { ...node, type }, base }, stack),
      );
    }
    const type = types[0];
    if (node.enum) {
      const kinds = new Set(node.enum.map((value) => (value === null ? "null" : typeof value)));
      return [kinds.size === 1 && kinds.has("string") ? "string" : "enum"];
    }
    if (type === "array") {
      const inner = node.items ? this.typeOf({ node: node.items, base }, stack) : ["any"];
      return ["list of ", ...inner];
    }
    if (type === "object" || (!type && (node.properties || node.additionalProperties))) {
      if (!node.properties && typeof node.additionalProperties === "object") {
        return ["map of ", ...this.typeOf({ node: node.additionalProperties, base }, stack)];
      }
      return ["object"];
    }
    if (type === "string" && node.format) return [`string (${node.format})`];
    if (type) return [type];
    if (node.not?.enum) return ["string"];
    if (node.if && (node.then || node.else)) {
      const branches = [node.then, node.else].filter((b): b is Schema => Boolean(b));
      return this.typeOf({ node: { oneOf: branches }, base }, stack);
    }
    return ["any"];
  }

  valuesOf(at: At): string[] {
    const { at: resolved } = this.deref(at);
    const node = resolved.node;
    if (node.const !== undefined) return [json(node.const)];
    const listed =
      node.enum ??
      (node.items ? this.deref({ node: node.items, base: resolved.base }).at.node.enum : undefined);
    if (listed) return listed.filter((value) => value !== null).map(json);
    const union = this.alternatives(resolved);
    return [
      ...new Set(
        union.flatMap((member) =>
          member.node.const !== undefined
            ? [json(member.node.const)]
            : (member.node.enum ?? []).filter((v) => v !== null).map(json),
        ),
      ),
    ];
  }

  notesOf(at: At): string[] {
    // Its own limits: a definition it refers to says what it is in its description.
    const own = at.node;
    const pick = <K extends keyof Schema>(key: K) => own[key];
    const notes: string[] = [];
    const minimum = pick("minimum");
    const maximum = pick("maximum");
    if (typeof minimum === "number" && minimum > -9e18) notes.push(`at least ${String(minimum)}`);
    if (typeof maximum === "number" && maximum < 9e18) notes.push(`at most ${String(maximum)}`);
    const minLength = pick("minLength");
    const maxLength = pick("maxLength");
    if (typeof minLength === "number" && minLength > 1)
      notes.push(`at least ${String(minLength)} characters`);
    if (typeof maxLength === "number") notes.push(`at most ${String(maxLength)} characters`);
    const minItems = pick("minItems");
    const maxItems = pick("maxItems");
    if (typeof minItems === "number" && minItems > 1)
      notes.push(`at least ${String(minItems)} items`);
    if (typeof maxItems === "number") notes.push(`at most ${String(maxItems)} items`);
    const minProperties = pick("minProperties");
    const maxProperties = pick("maxProperties");
    if (typeof minProperties === "number" && minProperties > 1)
      notes.push(`at least ${String(minProperties)} entries`);
    if (typeof maxProperties === "number") notes.push(`at most ${String(maxProperties)} entries`);
    return notes;
  }

  /** An object's keys, with what's merged into it: its `$ref`'s, its allOf's, its union's
   * members' and its branches'. A branch's key that isn't the object's own is allowed only
   * under the branch's condition. */
  keysOf(
    at: At,
    stack: string[],
    page: string,
  ): {
    keys: { name: string; at: At; required: boolean; when?: string }[];
    merged: string[];
    also: { text: string; href: string }[];
  } {
    const found = new Map<string, { name: string; at: At; required: boolean; when?: string }>();
    const merged: string[] = [];
    const also: { text: string; href: string }[] = [];
    // A union's members require their own keys, which the object doesn't: `optional`.
    const visit = (
      current: At,
      when: string | undefined,
      inside: string[],
      optional = false,
    ): void => {
      const { node, base } = current;
      const required = new Set(when || optional ? [] : (node.required ?? []));
      for (const [name, property] of Object.entries(node.properties ?? {})) {
        if (typeof property === "boolean") continue;
        const existing = found.get(name);
        if (existing) {
          if (required.has(name)) existing.required = true;
          continue;
        }
        // The key a condition tests is the condition's, not under it.
        const conditional = when && !when.includes(`\`${name}: `);
        found.set(name, {
          name,
          at: { node: property, base },
          required: required.has(name),
          ...(conditional ? { when } : {}),
        });
      }
      for (const name of required) {
        const existing = found.get(name);
        if (existing) existing.required = true;
      }
      if (node.$ref) {
        const place = target(node.$ref, base);
        const key = placeKey(place);
        const link = this.linked.get(key);
        if (link && (!link.href.startsWith(page) || inside.includes(key))) {
          // Shown in a place of its own: linked to, not repeated.
          if (!also.some((other) => other.href === link.href)) {
            also.push({ text: link.label, href: link.href });
          }
        } else if (!inside.includes(key)) {
          this.reached.add(key);
          merged.push(key);
          const resolved = { node: lookup(this.schemas, place), base: place.schema };
          visit(resolved, when, [...inside, key], optional);
        }
      }
      for (const part of node.allOf ?? []) visit({ node: part, base }, when, inside, optional);
      for (const member of node.oneOf ?? node.anyOf ?? []) {
        visit({ node: member, base }, when, inside, true);
      }
      if (node.if) {
        const condition = conditionOf(node.if) ?? when;
        if (node.then) visit({ node: node.then, base }, condition, inside, optional);
        if (node.else) visit({ node: node.else, base }, when, inside, optional);
      }
    };
    visit(at, undefined, stack);
    return { keys: [...found.values()], merged, also };
  }

  /** What a key's value holds: its own keys, if it's an object, or its items' or its
   * map's values'. Nothing for a page or section of its own, which its type links to. */
  inner(at: At, stack: string[]): { at: At; stack: string[] } | null {
    const { node, base } = at;
    if (node.$ref) {
      const place = target(node.$ref, base);
      const key = placeKey(place);
      if (this.linked.has(key) || stack.includes(key)) return null;
      this.reached.add(key);
      const resolved = { node: lookup(this.schemas, place), base: place.schema };
      if (node.properties || node.allOf) {
        return { at, stack };
      }
      return this.inner(resolved, [...stack, key]) ?? { at: resolved, stack: [...stack, key] };
    }
    if (node.type === "array" && node.items) return this.inner({ node: node.items, base }, stack);
    if (!node.properties && typeof node.additionalProperties === "object") {
      return this.inner({ node: node.additionalProperties, base }, stack);
    }
    return { at, stack };
  }

  rows(at: At, prefix: string, depth: number, stack: string[], href: string): Key[] {
    const rows: Key[] = [];
    const { keys, merged } = this.keysOf(at, stack, href.split("#")[0] ?? href);
    // What's merged into this object is being shown here: its keys don't repeat beneath.
    stack = [...stack, ...merged];
    for (const entry of keys) {
      const id = prefix ? `${prefix}.${entry.name}` : entry.name;
      const description = this.describe(entry.at);
      if (!description) this.missing.push(`${href}#${id}`);
      const def = this.deref(entry.at).at.node.default ?? entry.at.node.default;
      rows.push({
        id,
        name: entry.name,
        depth,
        type: this.typeOf(entry.at, stack),
        required: entry.required,
        values: this.valuesOf(entry.at),
        ...(def === undefined ? {} : { default: JSON.stringify(def) }),
        notes: this.notesOf(entry.at),
        description: plain(description),
        ...(entry.when ? { when: entry.when } : {}),
        also: [],
      });
      const inner = this.inner(entry.at, stack);
      if (inner) {
        const row = rows[rows.length - 1];
        if (row) row.also = this.keysOf(inner.at, inner.stack, href).also;
        const here = { href: `${href}#${id}`, label: entry.name };
        for (const key of inner.stack) if (!this.shownAt.has(key)) this.shownAt.set(key, here);
        rows.push(...this.rows(inner.at, id, depth + 1, inner.stack, href));
      }
    }
    return rows;
  }

  page(name: string, group: SpecPage["group"]): SpecPage {
    // A key that holds what it's inside links to it on this page.
    this.shownAt.clear();
    const schema = this.schema(name);
    const href = pageHref(name);
    const root: At = { node: schema, base: name };
    const rootKey = placeKey({ schema: name, pointer: "" });
    const sections: Section[] = [];
    // A map's keys are its values': a plugin's settings, each by its name.
    const top =
      !schema.properties && !schema.$ref && typeof schema.additionalProperties === "object"
        ? this.inner({ node: schema.additionalProperties, base: name }, [rootKey])
        : { at: root, stack: [rootKey] };
    const keys = top ? this.rows(top.at, "", 0, top.stack, href) : [];
    if (keys.length) {
      const also = top ? this.keysOf(top.at, top.stack, href).also : [];
      sections.push({ id: "keys", title: "Keys", description: "", keys, also });
    }
    for (const section of this.sections[name] ?? []) {
      const place = { schema: name, pointer: `/$defs/${section.def}` };
      this.reached.add(placeKey(place));
      const def = lookup(this.schemas, place);
      const intro = section.intro
        ? lookup(this.schemas, { schema: name, pointer: `/$defs/${section.intro}` })
        : def;
      if (section.intro)
        this.reached.add(placeKey({ schema: name, pointer: `/$defs/${section.intro}` }));
      if (!intro.description) this.missing.push(`${href}#${section.def}`);
      sections.push({
        id: section.def,
        title: section.title,
        description: plain(intro.description ?? ""),
        keys: this.rows({ node: def, base: name }, section.def, 0, [placeKey(place)], href),
        also: this.keysOf({ node: def, base: name }, [placeKey(place)], href).also,
      });
    }
    const { title, kind } = titleOf(schema, name);
    return {
      name,
      href,
      title,
      ...(kind ? { kind } : {}),
      group,
      description: plain(schema.description ?? ""),
      id: schema.$id ?? `${BASE}${name}/v1`,
      sections,
      byType: byTypeOf(this, top?.at ?? root, href),
      examples: (schema.examples ?? []).map(json),
    };
  }
}

/** "With `viewer: sequence`": what an if/then's `if` requires, in words. */
function conditionOf(test: Schema): string | undefined {
  const parts = Object.entries(test.properties ?? {}).flatMap(([name, property]) => {
    if (typeof property === "boolean") return [];
    if (property.const !== undefined) return [`\`${name}: ${json(property.const)}\``];
    if (property.enum)
      return [property.enum.map((value) => `\`${name}: ${json(value)}\``).join(" or ")];
    return [];
  });
  return parts.length ? `With ${parts.join(" and ")}` : undefined;
}

/** For a schema whose keys depend on its `type` (fields, settings): each type's keys, from
 * its `if type is X, then only these keys` branches. */
function byTypeOf(generator: Generator, at: At, page: string): SpecPage["byType"] {
  const found: SpecPage["byType"] = [];
  const visit = (current: At, inside: string[]): void => {
    const { node, base } = current;
    for (const part of node.allOf ?? []) {
      const type = (part.if?.properties?.["type"] as Schema | undefined)?.const;
      const keys = part.then?.propertyNames?.enum;
      if (typeof type === "string" && keys) {
        found.push({ type, keys: keys.map(String), required: part.then?.required ?? [] });
      }
    }
    if (node.$ref) {
      const place = target(node.$ref, base);
      const key = placeKey(place);
      const link = generator.linked.get(key);
      if (!inside.includes(key) && (!link || link.href.startsWith(page))) {
        visit({ node: lookup(generator.schemas, place), base: place.schema }, [...inside, key]);
      }
    }
  };
  visit(at, []);
  return found;
}

/** Every object definition with keys, in every schema: what the pages must reach. */
function definitions(schemas: Map<string, Schema>): string[] {
  const found: string[] = [];
  for (const [name, schema] of schemas) {
    for (const [def, node] of Object.entries(schema.$defs ?? {})) {
      if (node.properties && Object.keys(node.properties).length > 0) {
        found.push(placeKey({ schema: name, pointer: `/$defs/${def}` }));
      }
    }
  }
  return found;
}

/** The reference's pages, in the groups' order. Throws, listing each, if a schema isn't in
 * a group, a key has no description, a description still cites a design record, or a
 * definition with keys isn't shown on any page. */
export function specPages(
  schemas: Map<string, Schema> = loadSchemas(),
  groups: readonly Group[] = GROUPS,
  sections: Sections = SECTIONS,
): SpecPage[] {
  const grouped: string[] = groups.flatMap((group) => [...group.schemas]);
  const known = new Set<string>([
    ...grouped,
    ...CHOOSERS.filter((name) => schemas.has(name) || groups === GROUPS),
  ]);
  const problems: string[] = [];
  for (const name of schemas.keys()) {
    if (!known.has(name)) problems.push(`${name} isn't in any of the reference's groups`);
  }
  for (const name of known) {
    if (!schemas.has(name))
      problems.push(`${name} is in the reference's groups but isn't a schema`);
  }
  if (problems.length) throw new Error(problems.join("\n"));
  const generator = new Generator(schemas, groups, sections);
  const pages = groups.flatMap((group) =>
    group.schemas.map((name) => generator.page(name, group.id)),
  );
  for (const missing of generator.missing) problems.push(`${missing} has no description`);
  for (const key of definitions(schemas)) {
    const name = key.split("#")[0] ?? "";
    if (CHOOSERS.includes(name as (typeof CHOOSERS)[number])) continue;
    if (!generator.reached.has(key)) problems.push(`${key} isn't shown on any page`);
  }
  for (const page of pages) {
    const texts = [
      page.description,
      ...page.sections.flatMap((s) => [s.description, ...s.keys.map((k) => k.description)]),
    ];
    if (texts.some((text) => /\bADRs? \d{4}\b/.test(text))) {
      problems.push(`${page.href} still cites a design record`);
    }
    const ids = new Set<string>();
    const fixed = ["by-type", "by-type-h", "examples-h", "toc-h", "content"];
    const sections = page.sections.flatMap((s) => [s.id, `${s.id}-h`]);
    for (const id of [
      ...fixed,
      ...sections,
      ...page.sections.flatMap((s) => s.keys.map((k) => k.id)),
    ]) {
      if (ids.has(id)) problems.push(`${page.href}#${id} is two things`);
      ids.add(id);
    }
  }
  if (problems.length) {
    throw new Error(`The plugin spec reference can't be built:\n${problems.join("\n")}`);
  }
  return pages;
}

/** The kinds a chooser picks between: manifest/v1's, compute/v1's. */
export function chooserKinds(
  schemas: Map<string, Schema>,
  name: (typeof CHOOSERS)[number],
): string[] {
  const kind = schemas.get(name)?.properties?.["kind"];
  return typeof kind === "object" && kind.enum ? kind.enum.map(String) : [];
}

let cached: SpecPage[] | undefined;

/** The pages, built once per build. */
export function spec(): SpecPage[] {
  cached ??= specPages();
  return cached;
}
