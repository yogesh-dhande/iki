// The iki/v1 JSON Schemas in spec/src/iki_spec/schemas, read at build time: one page per
// schema, and the schema node that describes each key of a plugin's YAML. Nothing here is
// hand-written: a key's documentation is the `description` its schema gives it.
//
// Finding a key's schema follows JSON Schema's own rules, far enough to annotate a valid
// document: `$ref` (across schemas by their $id), `properties`, `patternProperties`,
// `additionalProperties`, `items`, `allOf`, the `anyOf`/`oneOf` branches the value matches,
// and `if`/`then`/`else` chosen by whether the value matches `if`.
import { filesUnder, readRepo } from "./repo.ts";
import { adrs } from "./adrs.ts";

export const SCHEMA_DIR = "spec/src/iki_spec/schemas";
const BASE = "https://ikisci.org/schemas/";
export const SCHEMA_HREF = "/plugin-browser/schemas/";

/** A node of a schema: the schema's name and a JSON pointer in it ("" is its root). */
export interface Loc {
  schema: string;
  ptr: string;
}
export const locKey = (l: Loc): string => `${l.schema}#${l.ptr}`;

// The schemas are JSON: their nodes are objects, arrays, strings, numbers and booleans.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type J = any;

export interface SchemaFile {
  name: string;
  file: string;
  json: Record<string, J>;
  title: string;
  description: string;
  /** The manifest kind this schema checks, from its `kind` property's `const`. */
  kind?: string;
}

let loaded: Map<string, SchemaFile> | undefined;

/** Every schema, by name (its directory). */
export function schemas(): Map<string, SchemaFile> {
  if (loaded) return loaded;
  loaded = new Map();
  for (const file of filesUnder(SCHEMA_DIR).filter((f) => /\/v1\.json$/.test(f))) {
    const name = file.split("/").at(-2)!;
    const json = JSON.parse(readRepo(file)) as Record<string, J>;
    if (json.$id !== `${BASE}${name}/v1`) {
      throw new Error(`${file}'s $id is ${json.$id}, not ${BASE}${name}/v1.`);
    }
    const kind = json.properties?.kind?.const;
    loaded.set(name, {
      name,
      file,
      json,
      title: String(json.title ?? name),
      description: String(json.description ?? ""),
      ...(typeof kind === "string" ? { kind } : {}),
    });
  }
  if (!loaded.size) throw new Error(`There are no schemas in ${SCHEMA_DIR}.`);
  return loaded;
}

/** The schema that checks a manifest of `kind`, such as entity-type for EntityType. */
export function schemaForKind(kind: string): string | undefined {
  for (const s of schemas().values()) if (s.kind === kind) return s.name;
  return undefined;
}

const unescape = (s: string): string => s.replaceAll("~1", "/").replaceAll("~0", "~");
const escape = (s: string): string => s.replaceAll("~", "~0").replaceAll("/", "~1");

export function segments(ptr: string): string[] {
  return ptr ? ptr.split("/").slice(1).map(unescape) : [];
}

export function sub(l: Loc, ...segs: (string | number)[]): Loc {
  return { schema: l.schema, ptr: l.ptr + segs.map((s) => "/" + escape(String(s))).join("") };
}

/** The schema node at a location, or undefined. */
export function at(l: Loc): J {
  const s = schemas().get(l.schema);
  if (!s) return undefined;
  let node: J = s.json;
  for (const seg of segments(l.ptr)) {
    if (node === null || typeof node !== "object") return undefined;
    node = node[seg];
  }
  return node;
}

/** Where a `$ref` in schema `from` points. */
export function resolveRef(ref: string, from: string): Loc {
  const [base, frag = ""] = ref.split("#");
  let schema = from;
  if (base) {
    const m = new RegExp(`^${BASE.replaceAll(".", "\\.")}([a-z-]+)/v1$`).exec(base);
    if (!m) throw new Error(`A schema refers to ${ref}, which isn't an iki/v1 schema.`);
    schema = m[1]!;
  }
  const l = { schema, ptr: frag };
  if (at(l) === undefined) throw new Error(`${from} refers to ${ref}, which doesn't exist.`);
  return l;
}

const isObj = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);

function deepEq(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

const regexes = new Map<string, RegExp | null>();
function regex(p: string): RegExp | null {
  if (!regexes.has(p)) {
    let r: RegExp | null = null;
    try {
      r = new RegExp(p, "u");
    } catch {
      try {
        r = new RegExp(p);
      } catch {
        r = null;
      }
    }
    regexes.set(p, r);
  }
  return regexes.get(p)!;
}

function typeOk(t: J, v: unknown): boolean {
  const one = (x: string): boolean => {
    switch (x) {
      case "string":
        return typeof v === "string";
      case "integer":
        return typeof v === "number" && Number.isInteger(v);
      case "number":
        return typeof v === "number";
      case "boolean":
        return typeof v === "boolean";
      case "null":
        return v === null;
      case "array":
        return Array.isArray(v);
      case "object":
        return isObj(v);
      default:
        return true;
    }
  };
  return Array.isArray(t) ? t.some((x) => one(String(x))) : one(String(t));
}

/** Whether `v` is valid against the schema at `l`, for choosing branches and `if`s. */
export function matches(l: Loc, v: unknown, depth = 0): boolean {
  if (depth > 60) return true;
  const s = at(l);
  if (s === true || s === undefined) return true;
  if (s === false) return false;
  if (!isObj(s)) return true;
  if (typeof s.$ref === "string" && !matches(resolveRef(s.$ref, l.schema), v, depth + 1)) {
    return false;
  }
  if ("const" in s && !deepEq(s.const, v)) return false;
  if (Array.isArray(s.enum) && !s.enum.some((e: unknown) => deepEq(e, v))) return false;
  if (s.type !== undefined && !typeOk(s.type, v)) return false;
  if (typeof v === "string") {
    if (typeof s.pattern === "string") {
      const r = regex(s.pattern);
      if (r && !r.test(v)) return false;
    }
    if (typeof s.minLength === "number" && [...v].length < s.minLength) return false;
    if (typeof s.maxLength === "number" && [...v].length > s.maxLength) return false;
  }
  if (typeof v === "number") {
    if (typeof s.minimum === "number" && v < s.minimum) return false;
    if (typeof s.maximum === "number" && v > s.maximum) return false;
    if (typeof s.exclusiveMinimum === "number" && v <= s.exclusiveMinimum) return false;
    if (typeof s.exclusiveMaximum === "number" && v >= s.exclusiveMaximum) return false;
  }
  if (Array.isArray(v)) {
    if (typeof s.minItems === "number" && v.length < s.minItems) return false;
    if (typeof s.maxItems === "number" && v.length > s.maxItems) return false;
    const prefix = Array.isArray(s.prefixItems) ? s.prefixItems.length : 0;
    for (let i = 0; i < v.length; i++) {
      const item =
        i < prefix
          ? sub(l, "prefixItems", i)
          : isObj(s.items) || s.items === false
            ? sub(l, "items")
            : undefined;
      if (item && !matches(item, v[i], depth + 1)) return false;
    }
  }
  if (isObj(v)) {
    const keys = Object.keys(v);
    if (Array.isArray(s.required) && !s.required.every((k: string) => k in v)) return false;
    if (typeof s.minProperties === "number" && keys.length < s.minProperties) return false;
    if (typeof s.maxProperties === "number" && keys.length > s.maxProperties) return false;
    for (const k of keys) {
      let named = false;
      if (isObj(s.properties) && k in s.properties) {
        named = true;
        if (!matches(sub(l, "properties", k), v[k], depth + 1)) return false;
      }
      if (isObj(s.patternProperties)) {
        for (const p of Object.keys(s.patternProperties)) {
          if (regex(p)?.test(k)) {
            named = true;
            if (!matches(sub(l, "patternProperties", p), v[k], depth + 1)) return false;
          }
        }
      }
      if (!named && s.additionalProperties !== undefined) {
        if (!matches(sub(l, "additionalProperties"), v[k], depth + 1)) return false;
      }
      if (s.propertyNames !== undefined && !matches(sub(l, "propertyNames"), k, depth + 1)) {
        return false;
      }
    }
    if (isObj(s.dependentRequired)) {
      for (const [k, need] of Object.entries(s.dependentRequired)) {
        if (k in v && Array.isArray(need) && !need.every((n) => n in v)) return false;
      }
    }
  }
  if (
    Array.isArray(s.allOf) &&
    !s.allOf.every((_: J, i: number) => matches(sub(l, "allOf", i), v, depth + 1))
  ) {
    return false;
  }
  if (
    Array.isArray(s.anyOf) &&
    !s.anyOf.some((_: J, i: number) => matches(sub(l, "anyOf", i), v, depth + 1))
  ) {
    return false;
  }
  if (
    Array.isArray(s.oneOf) &&
    !s.oneOf.some((_: J, i: number) => matches(sub(l, "oneOf", i), v, depth + 1))
  ) {
    return false;
  }
  if (s.not !== undefined && matches(sub(l, "not"), v, depth + 1)) return false;
  if (s.if !== undefined) {
    const branch = matches(sub(l, "if"), v, depth + 1) ? "then" : "else";
    if (s[branch] !== undefined && !matches(sub(l, branch), v, depth + 1)) return false;
  }
  return true;
}

/** Every schema node that applies to `v` at `l`: itself, what it refers to, and the
 * branches that hold for `v`. */
export function applicable(l: Loc, v: unknown): Loc[] {
  const out: Loc[] = [];
  const seen = new Set<string>();
  const go = (x: Loc): void => {
    const key = locKey(x);
    if (seen.has(key)) return;
    seen.add(key);
    out.push(x);
    const s = at(x);
    if (!isObj(s)) return;
    if (typeof s.$ref === "string") go(resolveRef(s.$ref, x.schema));
    if (Array.isArray(s.allOf)) s.allOf.forEach((_: J, i: number) => go(sub(x, "allOf", i)));
    for (const k of ["anyOf", "oneOf"] as const) {
      if (!Array.isArray(s[k])) continue;
      const all = s[k].map((_: J, i: number) => sub(x, k, i));
      const fit = all.filter((b: Loc) => matches(b, v));
      (fit.length ? fit : all).forEach(go);
    }
    if (s.if !== undefined) {
      const branch = matches(sub(x, "if"), v) ? "then" : "else";
      if (s[branch] !== undefined) go(sub(x, branch));
    }
    if (isObj(s.dependentSchemas) && isObj(v)) {
      for (const k of Object.keys(s.dependentSchemas))
        if (k in v) go(sub(x, "dependentSchemas", k));
    }
  };
  go(l);
  return out;
}

/** The nodes that declare key `key` of an object, among those that apply to the object. */
export function declarations(parents: Loc[], key: string): Loc[] {
  const out: Loc[] = [];
  for (const p of parents) {
    const s = at(p);
    if (!isObj(s)) continue;
    let named = false;
    if (isObj(s.properties) && key in s.properties) {
      out.push(sub(p, "properties", key));
      named = true;
    }
    if (isObj(s.patternProperties)) {
      for (const re of Object.keys(s.patternProperties)) {
        if (regex(re)?.test(key)) {
          out.push(sub(p, "patternProperties", re));
          named = true;
        }
      }
    }
    if (!named && isObj(s.additionalProperties)) out.push(sub(p, "additionalProperties"));
  }
  return out;
}

/** The nodes that declare item `i` of an array. */
export function itemDeclarations(parents: Loc[], i: number): Loc[] {
  const out: Loc[] = [];
  for (const p of parents) {
    const s = at(p);
    if (!isObj(s)) continue;
    if (Array.isArray(s.prefixItems) && i < s.prefixItems.length)
      out.push(sub(p, "prefixItems", i));
    else if (isObj(s.items)) out.push(sub(p, "items"));
  }
  return out;
}

/** The nodes that check a key's name, such as a plugin reference for `dependencies`. */
export function nameSchemas(parents: Loc[], key: string): Loc[] {
  return parents.flatMap((p) => {
    const s = at(p);
    return isObj(s) && s.propertyNames !== undefined
      ? applicable(sub(p, "propertyNames"), key)
      : [];
  });
}

/** A node's description: its own, or what it refers to says. */
export function describe(l: Loc, depth = 0): string | undefined {
  const s = at(l);
  if (!isObj(s) || depth > 10) return undefined;
  if (typeof s.description === "string" && s.description.trim()) return s.description.trim();
  if (typeof s.$ref === "string") return describe(resolveRef(s.$ref, l.schema), depth + 1);
  return undefined;
}

// --- Schema pages --------------------------------------------------------------------

/** A readable, stable anchor for a node: `fields.*.to`, `$defs.step.action`, `@allOf-2`. */
export function anchorOf(ptr: string): string {
  const segs = segments(ptr);
  if (!segs.length) return "top";
  const out: string[] = [];
  for (let i = 0; i < segs.length; i++) {
    const s = segs[i]!;
    const next = segs[i + 1];
    switch (s) {
      case "properties":
        out.push(next!);
        i++;
        break;
      case "$defs":
        out.push(`$defs.${next}`);
        i++;
        break;
      case "patternProperties":
        out.push(`~${next!.replace(/[^A-Za-z0-9_]+/g, "-").replace(/^-|-$/g, "")}`);
        i++;
        break;
      case "additionalProperties":
        out.push("*");
        break;
      case "allOf":
      case "anyOf":
      case "oneOf":
      case "prefixItems":
      case "dependentSchemas":
        out.push(`@${s}-${next}`);
        i++;
        break;
      case "items":
        out.push("@items");
        break;
      case "propertyNames":
        out.push("@names");
        break;
      default:
        out.push(`@${s}`);
    }
  }
  return out.join(".");
}

export interface Summary {
  types: string[];
  enum?: unknown[];
  const?: unknown;
  default?: unknown;
  ref?: Loc;
  facts: string[];
}

export interface SNode {
  loc: Loc;
  id: string;
  label: string;
  role: "root" | "prop" | "pattern" | "additional" | "items" | "names" | "def" | "branch";
  required: boolean;
  description?: string;
  summary: Summary;
  children: SNode[];
}

function summarize(l: Loc): Summary {
  const s = at(l);
  const out: Summary = { types: [], facts: [] };
  if (!isObj(s)) {
    if (s === false) out.facts.push("not allowed");
    return out;
  }
  if (s.type !== undefined)
    out.types = Array.isArray(s.type) ? s.type.map(String) : [String(s.type)];
  if (Array.isArray(s.enum)) out.enum = s.enum;
  if ("const" in s) out.const = s.const;
  if ("default" in s) out.default = s.default;
  if (typeof s.$ref === "string") out.ref = resolveRef(s.$ref, l.schema);
  const f = (k: string, label: string): void => {
    if (s[k] !== undefined)
      out.facts.push(`${label} ${typeof s[k] === "string" ? s[k] : JSON.stringify(s[k])}`);
  };
  f("minLength", "at least");
  f("maxLength", "at most");
  if (s.minLength !== undefined || s.maxLength !== undefined) {
    const last = out.facts.length - 1;
    out.facts[last] = `${out.facts[last]} characters`;
  }
  f("minimum", "≥");
  f("maximum", "≤");
  f("exclusiveMinimum", ">");
  f("exclusiveMaximum", "<");
  f("minItems", "min items");
  f("maxItems", "max items");
  f("maxProperties", "max keys");
  if (s.uniqueItems === true) out.facts.push("unique items");
  f("pattern", "pattern");
  if (s.additionalProperties === false && isObj(s.properties)) out.facts.push("no other keys");
  return out;
}

/** What an `if` asks, in words: "`type` is `ref`". */
export function condition(l: Loc): string {
  const s = at(l);
  if (!isObj(s)) return "it matches";
  const parts: string[] = [];
  if (isObj(s.properties)) {
    for (const [k, v] of Object.entries(s.properties)) {
      if (isObj(v) && "const" in v)
        parts.push(`\`${k}\` is \`${JSON.stringify(v.const).replace(/^"|"$/g, "")}\``);
      else if (isObj(v) && Array.isArray(v.enum)) {
        parts.push(`\`${k}\` is one of ${v.enum.map((e) => `\`${String(e)}\``).join(", ")}`);
      } else parts.push(`\`${k}\` matches ${JSON.stringify(v)}`);
    }
  }
  const named = new Set(isObj(s.properties) ? Object.keys(s.properties) : []);
  if (Array.isArray(s.required)) {
    const extra = s.required.filter((k: string) => !named.has(k));
    if (extra.length) parts.push(`it has ${extra.map((k: string) => `\`${k}\``).join(", ")}`);
  }
  for (const k of Object.keys(s)) {
    if (k !== "properties" && k !== "required") parts.push(`\`${k}\`: ${JSON.stringify(s[k])}`);
  }
  return parts.length ? parts.join(" and ") : "it matches";
}

const trees = new Map<string, { root: SNode; defs: SNode[]; ids: Set<string> }>();

/** A schema's tree for its page: its root's properties, then its $defs. */
export function schemaTree(name: string): { root: SNode; defs: SNode[]; ids: Set<string> } {
  const hit = trees.get(name);
  if (hit) return hit;
  const ids = new Set<string>();
  const build = (l: Loc, label: string, role: SNode["role"], required = false): SNode => {
    const id = anchorOf(l.ptr);
    ids.add(id);
    const s = at(l);
    const node: SNode = { loc: l, id, label, role, required, summary: summarize(l), children: [] };
    if (isObj(s) && typeof s.description === "string") node.description = s.description.trim();
    if (!isObj(s)) return node;
    const req = new Set<string>(Array.isArray(s.required) ? s.required : []);
    if (isObj(s.properties)) {
      for (const k of Object.keys(s.properties)) {
        node.children.push(build(sub(l, "properties", k), k, "prop", req.has(k)));
      }
    }
    if (isObj(s.patternProperties)) {
      for (const p of Object.keys(s.patternProperties)) {
        node.children.push(build(sub(l, "patternProperties", p), `/${p}/`, "pattern"));
      }
    }
    if (
      isObj(s.propertyNames) &&
      (s.propertyNames.$ref ||
        s.propertyNames.description ||
        s.propertyNames.enum ||
        s.propertyNames.pattern)
    ) {
      node.children.push(build(sub(l, "propertyNames"), "names", "names"));
    }
    if (isObj(s.additionalProperties)) {
      node.children.push(build(sub(l, "additionalProperties"), "*", "additional"));
    }
    if (isObj(s.items)) node.children.push(build(sub(l, "items"), "[ ]", "items"));
    if (Array.isArray(s.prefixItems)) {
      s.prefixItems.forEach((_: J, i: number) =>
        node.children.push(build(sub(l, "prefixItems", i), `[${i}]`, "items")),
      );
    }
    for (const k of ["allOf", "anyOf", "oneOf"] as const) {
      const branches: unknown = s[k];
      if (!Array.isArray(branches)) continue;
      const word = { allOf: "all of", anyOf: "any of", oneOf: "one of" }[k];
      branches.forEach((_, i) => {
        node.children.push(
          build(sub(l, k, i), `${word} (${i + 1} of ${branches.length})`, "branch"),
        );
      });
    }
    if (s.if !== undefined) {
      ids.add(anchorOf(sub(l, "if").ptr));
      const cond = condition(sub(l, "if"));
      if (s.then !== undefined) node.children.push(build(sub(l, "then"), `when ${cond}`, "branch"));
      if (s.else !== undefined)
        node.children.push(build(sub(l, "else"), `otherwise (unless ${cond})`, "branch"));
    }
    if (s.not !== undefined) node.children.push(build(sub(l, "not"), "not", "branch"));
    if (isObj(s.dependentSchemas)) {
      for (const k of Object.keys(s.dependentSchemas)) {
        node.children.push(build(sub(l, "dependentSchemas", k), `with \`${k}\``, "branch"));
      }
    }
    return node;
  };
  const rootLoc = { schema: name, ptr: "" };
  const root = build(rootLoc, name, "root");
  const defs: SNode[] = [];
  const json = schemas().get(name)?.json;
  if (json && isObj(json.$defs)) {
    for (const d of Object.keys(json.$defs)) defs.push(build(sub(rootLoc, "$defs", d), d, "def"));
  }
  const tree = { root, defs, ids };
  trees.set(name, tree);
  return tree;
}

/** The link to a schema node on its schema's page: the node itself, or the nearest node
 * above it the page shows. */
export function schemaHref(l: Loc): string {
  const { ids } = schemaTree(l.schema);
  let segs = segments(l.ptr);
  while (segs.length) {
    const ptr = segs.map((s) => "/" + escape(s)).join("");
    const id = anchorOf(ptr);
    if (ids.has(id)) return `${SCHEMA_HREF}${l.schema}/#${encodeURIComponent(id)}`;
    segs = segs.slice(0, -1);
  }
  return `${SCHEMA_HREF}${l.schema}/`;
}

/** Where a loc is, in words: "entity-type › fields › * › to". */
export function locLabel(l: Loc): string {
  const a = anchorOf(l.ptr);
  return a === "top" ? l.schema : `${l.schema} › ${a.replaceAll(".", " › ")}`;
}

// --- Text ----------------------------------------------------------------------------

export const esc = (s: string): string =>
  s
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");

/** A schema's prose as HTML: `code` spans, and ADR numbers linked to their pages. */
export function rich(text: string): string {
  const known = adrs();
  return esc(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\bADRs? (\d{4}(?:(?:, | and |, and )\d{4})*)/g, (whole, nums: string) =>
      whole.replace(/\d{4}/g, (n) =>
        known.has(n) ? `<a class="adr" href="/adrs/${n}/">${n}</a>` : n,
      ),
    );
}
