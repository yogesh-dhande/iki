// The plugin browser: every plugin in the repository, its YAML as the spec reads it, each
// key linked to its schema's description, and each value that names another part of a
// plugin linked to it, with the links back. Built once per build from the files.
//
// A plugin is a directory with plugin.yaml (spec/src/iki_spec/validate.py: `_read`). Its
// manifests are the YAML documents under each kind's directory, as `_files` finds them,
// and its tests' ActionTest documents under tests/. References resolve as the spec does
// (spec/src/iki_spec/model.py: `qualify`): a name with a `.` is `namespace/plugin.Part`;
// one without is the referring plugin's own. Core types are `iki/core.Type`.
import { execFileSync } from "node:child_process";
import { parseAllDocuments, isMap, isScalar, isSeq, type Document, type Node as YNode } from "yaml";
import { REPO, filesUnder, githubUrl, readRepo } from "./repo.ts";
import { adrs } from "./adrs.ts";
import {
  applicable,
  at,
  declarations,
  describe,
  esc,
  itemDeclarations,
  locKey,
  locLabel,
  nameSchemas,
  rich,
  schemaForKind,
  schemaHref,
  type Loc,
} from "./schemas.ts";

export const BROWSER = "/plugin-browser/";
export const LIST_HREF = `${BROWSER}all-plugins/`;
export const CORE_HREF = `${BROWSER}core-types/`;
const PROJECT_ROLES_HREF = "/permissions/projects-and-roles/";
const STARTERS_PY = "server/src/iki_server/setup/starters.py";

export type Group = "core" | "official" | "example" | "test";
export const GROUPS: { id: Group; label: string; one: string; where: string }[] = [
  { id: "core", label: "Core", one: "Core", where: "spec/src/iki_spec/core" },
  { id: "official", label: "Official", one: "Official", where: "plugins/" },
  { id: "example", label: "Examples", one: "Example", where: "examples/plugins/" },
  { id: "test", label: "Test and fixture", one: "Test or fixture", where: "everywhere else" },
];

export type PartKind =
  | "type"
  | "extension"
  | "action"
  | "environment"
  | "workflow"
  | "trigger"
  | "component"
  | "test"
  | "role"
  | "format"
  | "writer"
  | "api"
  | "setting"
  | "starter"
  | "field";

/** Each manifest kind, the part it declares, and its anchor prefix on a plugin's page. */
const KIND_PARTS: Record<string, { part: PartKind; prefix: string }> = {
  EntityType: { part: "type", prefix: "type" },
  EntityTypeExtension: { part: "extension", prefix: "ext" },
  Action: { part: "action", prefix: "action" },
  Environment: { part: "environment", prefix: "env" },
  Workflow: { part: "workflow", prefix: "workflow" },
  Trigger: { part: "trigger", prefix: "trigger" },
  Component: { part: "component", prefix: "component" },
  ActionTest: { part: "test", prefix: "test" },
};

export const PART_LABELS: Record<PartKind, [string, string]> = {
  type: ["Type", "Types"],
  extension: ["Extension", "Extensions"],
  action: ["Action", "Actions"],
  environment: ["Environment", "Environments"],
  workflow: ["Workflow", "Workflows"],
  trigger: ["Trigger", "Triggers"],
  component: ["Component", "Components"],
  test: ["Test", "Tests"],
  role: ["Lab role", "Lab roles"],
  format: ["File format", "File formats"],
  writer: ["Writer", "Writers"],
  api: ["Outside API", "Outside APIs"],
  setting: ["Setting", "Settings"],
  starter: ["Starter", "Starters"],
  field: ["Field", "Fields"],
};

export interface Part {
  plugin: PluginRec;
  kind: PartKind;
  /** Its local name: `Lot`, `release_lot`, `qa`. */
  name: string;
  /** As other plugins name it: `iki/lots.Lot`. */
  qualified: string;
  anchor: string;
  file: string;
  label?: string;
  description?: string;
  abstract?: boolean;
  /** For a type: its parent as written; for an extension: the type it extends. */
  extends?: string;
  fields?: string[];
  /** For a starter: what the server's recipe says. */
  starter?: StarterRecipe;
}

export interface Token {
  start: number;
  end: number;
  cls: string;
  href?: string;
  id?: string;
  /** A key's documentation, by its schema node. */
  doc?: string;
  title?: string;
}

export interface DocBlock {
  start: number;
  end: number;
  kind?: string;
  schema?: string;
  part?: Part;
}

export interface YamlFile {
  path: string;
  rel: string;
  text: string;
  tokens: Token[];
  docs: DocBlock[];
  /** YAML Iki doesn't read as a manifest, such as a conda environment. */
  plain: boolean;
}

export interface PluginRec {
  id: string;
  href: string;
  dir: string;
  group: Group;
  name: string;
  version: string;
  title: string;
  description: string;
  iki?: string;
  license?: string;
  dependencies: { name: string; types: Record<string, string> }[];
  files: string[];
  yaml: YamlFile[];
  others: string[];
  parts: Part[];
  /** For a test plugin: the files that name its directory or the plugin. */
  usedFrom: string[];
}

export interface Ref {
  from: PluginRec;
  /** The part whose manifest holds it, or the plugin itself for plugin.yaml. */
  fromPart?: Part;
  path: string;
  relation: "extends" | "depends" | "uses";
}

export interface Unresolved {
  plugin: PluginRec;
  file: string;
  path: string;
  value: string;
  reason: string;
  href: string;
}

export interface KeyDoc {
  html: string;
  href: string;
  where: string;
}

export interface Browser {
  plugins: PluginRec[];
  byId: Map<string, PluginRec>;
  /** Links in, by target: a part's `${plugin.id}#${anchor}`, or a plugin's id. */
  refsTo: Map<string, Ref[]>;
  unresolved: Unresolved[];
  keyDocs: Map<string, KeyDoc>;
  /** Keys no schema declares, and declared keys with no description, by schema node. */
  undeclared: { plugin: PluginRec; file: string; path: string }[];
  undescribed: Map<string, { loc: Loc; count: number }>;
  /** Where each schema node is used: up to a few places, and how many. */
  uses: Map<
    string,
    {
      count: number;
      examples: { plugin: PluginRec; href: string; label: string; path: string }[];
    }
  >;
}

// --- Discovery ------------------------------------------------------------------------

export const pluginSlug = (name: string): string => name.replace("/", "-").replaceAll("_", "-");

function groupOf(dir: string): Group {
  if (dir === "spec/src/iki_spec/core") return "core";
  if (/^plugins\/[^/]+$/.test(dir)) return "official";
  if (/^examples\/plugins\/[^/]+$/.test(dir)) return "example";
  return "test";
}

/** Every directory in the repository with a plugin.yaml, as the validator recognises one. */
export function pluginDirs(): string[] {
  const out = execFileSync(
    "git",
    ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
    {
      cwd: REPO,
      encoding: "utf8",
      maxBuffer: 256 * 1024 * 1024,
    },
  )
    .split("\0")
    .filter(
      (f) => /(^|\/)plugin\.ya?ml$/.test(f) && !f.split("/").some((p) => p === "node_modules"),
    );
  return [...new Set(out.map((f) => f.replace(/\/?plugin\.ya?ml$/, "")))].sort();
}

const text = (v: unknown): string =>
  typeof v === "string" || typeof v === "number" ? String(v) : "";
const isObj = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);

/** The kinds' directories, from the validator (`DIRECTORIES`), plus tests/. */
function kindDirectories(): string[] {
  const source = readRepo("spec/src/iki_spec/validate.py");
  const m = /^DIRECTORIES = \{([\s\S]*?)^\}/m.exec(source);
  if (!m) throw new Error("spec/src/iki_spec/validate.py no longer defines DIRECTORIES.");
  const typesDir = /^TYPES_DIR = "([a-z]+)"/m.exec(source)?.[1] ?? "types";
  const dirs = [...m[1]!.matchAll(/:\s*("?)([A-Za-z_]+)\1,/g)].map((x) =>
    x[2] === "TYPES_DIR" ? typesDir : x[2]!,
  );
  return [...new Set([...dirs, "tests"])];
}

// --- Starters: the server's recipes, which plugins name in plugin.yaml ------------------

export interface StarterRecipe {
  id: string;
  version: string;
  label: string;
  description: string;
  plugin: string;
  resultBase: string;
  flows: Record<string, string>;
  pinnedBy?: string;
  roles: string[];
}

let recipes: StarterRecipe[] | undefined;

/** The recipes in the server's catalogue (`Starter(...)` in starters.py), read as written. */
export function starterRecipes(): StarterRecipe[] {
  if (recipes) return recipes;
  const src = readRepo(STARTERS_PY);
  const consts = new Map<string, string>();
  for (const m of src.matchAll(/^([A-Z_]+) = "([^"]*)"$/gm)) consts.set(m[1]!, m[2]!);
  const evalExpr = (e: string): string => {
    const parts = e.split("+").map((p) => p.trim());
    return parts
      .map((p) => {
        const lit = /^"([^"]*)"$/.exec(p);
        if (lit) return lit[1]!;
        const c = consts.get(p);
        if (c === undefined) throw new Error(`${STARTERS_PY}: can't read ${e} (no constant ${p}).`);
        return c;
      })
      .join("");
  };
  // NAME, NAME = PREFIX + "a", PREFIX + "b"
  for (const m of src.matchAll(
    /^([A-Z_]+(?:, [A-Z_]+)+) = ([A-Z_]+ \+ "[^"]+"(?:, [A-Z_]+ \+ "[^"]+")+)$/gm,
  )) {
    const names = m[1]!.split(", ");
    const values = m[2]!.split(", ").map(evalExpr);
    names.forEach((n, i) => consts.set(n, values[i]!));
  }
  // NAME, NAME = (PREFIX + r for r in ("a", "b"))
  for (const m of src.matchAll(
    /^([A-Z_]+(?:, [A-Z_]+)+) = \(\s*([A-Z_]+) \+ r for r in \(([^)]*)\)\s*\)/gm,
  )) {
    const names = m[1]!.split(", ");
    const prefix = consts.get(m[2]!) ?? "";
    const values = [...m[3]!.matchAll(/"([^"]+)"/g)].map((x) => prefix + x[1]);
    names.forEach((n, i) => consts.set(n, values[i]!));
  }
  recipes = [];
  for (const m of src.matchAll(/^[A-Z_]+ = Starter\(\n([\s\S]*?)^\)$/gm)) {
    const body = m[1]!;
    const str = (k: string): string | undefined =>
      new RegExp(`^    ${k}="([^"]*)",$`, "m").exec(body)?.[1];
    const expr = (k: string): string | undefined => {
      const x = new RegExp(`^    ${k}=([^\\n]+),$`, "m").exec(body)?.[1];
      return x === undefined ? undefined : evalExpr(x);
    };
    const block = (k: string): string =>
      new RegExp(`^    ${k}=([\\s\\S]*?)^    [a-z_]+=|^    ${k}=(.*)$`, "m")
        .exec(body)
        ?.slice(1)
        .find(Boolean) ?? "";
    const desc = /^ {4}description=\(?([\s\S]*?)\)?,\n {4}[a-z_]+=/m.exec(body)?.[1] ?? "";
    const flows: Record<string, string> = {};
    const flowsText = /^ {4}flows=\{([^}]*)\}/m.exec(body)?.[1] ?? "";
    for (const f of flowsText.matchAll(/"([a-z_]+)": ([A-Z_]+ \+ "[^"]+"|"[^"]+")/g))
      flows[f[1]!] = evalExpr(f[2]!);
    const rolesText = /^ {4}roles=\{([\s\S]*?)\n {4}\},/m.exec(body)?.[1] ?? block("roles");
    const roles = [...rolesText.matchAll(/^\s+([A-Z_]+): ProjectRole\.[A-Z]+,/gm)].map((r) => {
      const v = consts.get(r[1]!);
      if (!v) throw new Error(`${STARTERS_PY}: can't read the role ${r[1]}.`);
      return v;
    });
    const id = str("id");
    const version = str("version");
    const plugin = str("plugin");
    const resultBase = expr("result_base");
    if (!id || !version || !plugin || !resultBase) {
      throw new Error(`${STARTERS_PY}: a Starter(...) this site can't read (id ${id ?? "?"}).`);
    }
    const pinned = expr("pinned_by");
    recipes.push({
      id,
      version,
      label: str("label") ?? id,
      description: [...desc.matchAll(/"([^"]*)"/g)].map((x) => x[1]).join(""),
      plugin,
      resultBase,
      flows,
      roles,
      ...(pinned ? { pinnedBy: pinned } : {}),
    });
  }
  if (!recipes.length) throw new Error(`${STARTERS_PY} has no Starter(...) this site can read.`);
  return recipes;
}

// --- Reading plugins ------------------------------------------------------------------

function read(dir: string, kindDirs: string[]): PluginRec {
  const files = filesUnder(dir);
  const top = parseAllDocuments(readRepo(`${dir}/plugin.yaml`));
  const topDoc = (Array.isArray(top) ? top : [])[0];
  const data = topDoc?.toJS() as Record<string, unknown> | undefined;
  if (!data || data.kind !== "Plugin")
    throw new Error(`${dir}/plugin.yaml holds no Plugin manifest.`);
  const name = text(data.name);
  const group = groupOf(dir);
  const id = pluginSlug(name);
  const deps = isObj(data.dependencies) ? data.dependencies : {};
  const rec: PluginRec = {
    id,
    href: group === "core" ? CORE_HREF : `${LIST_HREF}${id}/`,
    dir,
    group,
    name,
    version: text(data.version),
    title: text(data.title) || name,
    description: text(data.description).trim(),
    dependencies: Object.entries(deps).map(([n, spec]) => ({
      name: n,
      types:
        isObj(spec) && isObj(spec.types)
          ? (Object.fromEntries(Object.entries(spec.types).map(([k, v]) => [k, text(v)])) as Record<
              string,
              string
            >)
          : {},
    })),
    files,
    yaml: [],
    others: [],
    parts: [],
    usedFrom: [],
  };
  if (typeof data.iki === "string") rec.iki = data.iki;
  if (typeof data.license === "string") rec.license = data.license;
  const inKindDir = (rel: string): boolean => kindDirs.some((d) => rel.startsWith(`${d}/`));
  for (const path of files) {
    const rel = path.slice(dir.length + 1);
    const hidden = rel.split("/").some((p) => p.startsWith("."));
    if (/\.ya?ml$/.test(rel) && !hidden) {
      rec.yaml.push({
        path,
        rel,
        text: readRepo(path),
        tokens: [],
        docs: [],
        plain: !(rel === "plugin.yaml" || inKindDir(rel)),
      });
    } else {
      rec.others.push(path);
    }
  }
  const order = (rel: string): number => {
    if (/^plugin\.ya?ml$/.test(rel)) return 0;
    const i = kindDirs.findIndex((d) => rel.startsWith(`${d}/`));
    return i < 0 ? 100 : i + 1;
  };
  rec.yaml.sort((a, b) => order(a.rel) - order(b.rel) || a.rel.localeCompare(b.rel));
  return rec;
}

/** The parts plugin.yaml declares under a map key: roles, formats, writers, apis, settings. */
const TOP_PARTS: [string, PartKind, string][] = [
  ["roles", "role", "role"],
  ["formats", "format", "format"],
  ["writers", "writer", "writer"],
  ["apis", "api", "api"],
  ["settings", "setting", "setting"],
  ["starters", "starter", "starter"],
];

function partsOf(rec: PluginRec): void {
  const used = new Set<string>();
  const unique = (a: string): string => {
    let x = a;
    for (let i = 2; used.has(x); i++) x = `${a}-${i}`;
    used.add(x);
    return x;
  };
  for (const f of rec.yaml) {
    if (f.plain) {
      f.docs.push({ start: 0, end: f.text.length });
      continue;
    }
    const docs = parseAllDocuments(f.text);
    const list = Array.isArray(docs) ? docs : [];
    list.forEach((d, i) => {
      const start = i === 0 ? 0 : (d.range?.[0] ?? 0);
      const end = i + 1 < list.length ? (list[i + 1]!.range?.[0] ?? f.text.length) : f.text.length;
      const body = d.toJS() as unknown;
      const block: DocBlock = { start, end };
      if (isObj(body) && typeof body.kind === "string") {
        block.kind = body.kind;
        const schema = schemaForKind(body.kind);
        if (schema) block.schema = schema;
        const kp = KIND_PARTS[body.kind];
        if (kp) {
          const local =
            body.kind === "EntityTypeExtension" ? text(body.extends_type) : text(body.name);
          const part: Part = {
            plugin: rec,
            kind: kp.part,
            name: local,
            qualified: `${rec.name}.${local}`,
            anchor: unique(`${kp.prefix}-${local.replace(/[^A-Za-z0-9_-]+/g, "-")}`),
            file: f.path,
          };
          if (typeof body.label === "string") part.label = body.label;
          if (typeof body.description === "string") part.description = body.description.trim();
          if (body.abstract === true) part.abstract = true;
          if (body.kind === "EntityType" && typeof body.extends === "string")
            part.extends = body.extends;
          if (body.kind === "EntityTypeExtension") part.extends = local;
          if (isObj(body.fields)) part.fields = Object.keys(body.fields);
          rec.parts.push(part);
          block.part = part;
        }
        if (body.kind === "Plugin") {
          for (const [key, kind, prefix] of TOP_PARTS) {
            const m = body[key];
            if (!isObj(m)) continue;
            for (const [n, spec] of Object.entries(m)) {
              const part: Part = {
                plugin: rec,
                kind,
                name: n,
                qualified: `${rec.name}.${n}`,
                anchor: unique(`${prefix}-${n}`),
                file: f.path,
              };
              if (isObj(spec) && typeof spec.label === "string") part.label = spec.label;
              if (isObj(spec) && typeof spec.description === "string")
                part.description = spec.description.trim();
              if (kind === "starter") {
                const recipe = starterRecipes().find((r) => r.id === n && r.plugin === rec.name);
                if (recipe) {
                  part.starter = recipe;
                  part.label = recipe.label;
                  part.description = recipe.description;
                }
              }
              rec.parts.push(part);
            }
          }
        }
      }
      f.docs.push(block);
    });
  }
}

// --- Resolving and highlighting -------------------------------------------------------

/** Schema nodes whose values name another part, besides the shared reference $defs. */
const VALUE_REFS: [
  string,
  "action" | "environment" | "file" | "file?" | "writer" | "format" | "format?" | "setting",
][] = [
  ["workflow#/$defs/step/properties/action", "action"],
  ["trigger#/properties/run", "action"],
  ["action-test#/properties/action", "action"],
  ["component#/properties/events/properties/run/properties/actions/items", "action"],
  ["action#/properties/runs_on/properties/environment", "environment"],
  ["environment#/$defs/file", "file"],
  ["action#/properties/command/items", "file?"],
  ["component#/properties/module", "file"],
  ["entity-type#/$defs/pluginExport/properties/export", "writer"],
  ["plugin#/$defs/writer/properties/format", "format"],
  ["field#/$defs/typed/properties/media_types/items", "format?"],
  ["action#/properties/requires_settings/items", "setting"],
];
const TYPE_REF = "field#/$defs/typeRef";
const ROLE_REF = "field#/$defs/roleRef";
const PLUGIN_REFS = ["field#/$defs/pluginRef", "field#/$defs/pluginName"];
const FIELD_NAME = "field#/$defs/fieldName";
const PROJECT_ROLES = new Set(["viewer", "editor", "runner", "admin"]);

function checkRefNodes(): void {
  for (const k of [TYPE_REF, ROLE_REF, FIELD_NAME, ...PLUGIN_REFS, ...VALUE_REFS.map(([k]) => k)]) {
    const [schema, ptr] = k.split("#") as [string, string];
    if (at({ schema, ptr }) === undefined) {
      throw new Error(
        `The plugin browser reads references at ${k}, which the schemas no longer have.`,
      );
    }
  }
}

interface Target {
  key: string;
  href: string;
  title: string;
}
type Resolution = Target | { unresolved: string } | undefined;

let built: Browser | undefined;

export function browser(): Browser {
  if (built) return built;
  checkRefNodes();
  const kindDirs = kindDirectories();
  const plugins = pluginDirs().map((d) => read(d, kindDirs));
  const byId = new Map<string, PluginRec>();
  const byName = new Map<string, PluginRec[]>();
  for (const p of plugins) {
    if (byId.has(p.id))
      throw new Error(`Two plugins have the slug ${p.id}: ${byId.get(p.id)!.dir} and ${p.dir}.`);
    byId.set(p.id, p);
    byName.set(p.name, [...(byName.get(p.name) ?? []), p]);
    partsOf(p);
  }
  const partsByKey = new Map<string, Part>();
  for (const p of plugins) for (const x of p.parts) partsByKey.set(`${p.id}#${x.anchor}`, x);
  const order: Group[] = ["core", "official", "example", "test"];
  plugins.sort(
    (a, b) => order.indexOf(a.group) - order.indexOf(b.group) || a.name.localeCompare(b.name),
  );

  const b: Browser = {
    plugins,
    byId,
    refsTo: new Map(),
    unresolved: [],
    keyDocs: new Map(),
    undeclared: [],
    undescribed: new Map(),
    uses: new Map(),
  };

  /** A plugin by name, from `from`'s point of view: the one beside it when two share a name. */
  const plugin = (name: string, from: PluginRec): PluginRec | undefined => {
    const all = byName.get(name) ?? [];
    if (all.length < 2) return all[0];
    const parent = (d: string): string => d.split("/").slice(0, -1).join("/");
    return (
      all.find((p) => parent(p.dir) === parent(from.dir)) ??
      all.find((p) => p.group === from.group) ??
      all[0]
    );
  };
  const partIn = (p: PluginRec, kind: PartKind, name: string): Part | undefined =>
    p.parts.find((x) => x.kind === kind && x.name === name);
  const partTarget = (x: Part): Target => ({
    key: `${x.plugin.id}#${x.anchor}`,
    href: `${x.plugin.href}#${x.anchor}`,
    title: `${PART_LABELS[x.kind][0]} ${x.qualified}${x.description ? `: ${firstSentence(x.description)}` : ""}`,
  });
  const qualifiedPart = (
    raw: string,
    from: PluginRec,
    kind: PartKind,
    what: string,
  ): Resolution => {
    const dot = raw.lastIndexOf(".");
    const pname = dot >= 0 ? raw.slice(0, dot) : from.name;
    const local = dot >= 0 ? raw.slice(dot + 1) : raw;
    const p = plugin(pname, from);
    if (!p) return { unresolved: `No plugin named ${pname} is in this repository.` };
    const x = partIn(p, kind, local);
    if (!x) return { unresolved: `${pname} (${p.dir}) declares no ${what} ${local}.` };
    return partTarget(x);
  };

  /** Every type's fields with its ancestors', for field names in its own manifest. */
  const lineageFields = (t: Part): Map<string, Part> => {
    const out = new Map<string, Part>();
    let cur: Part | undefined = t;
    const seen = new Set<string>();
    while (cur && !seen.has(cur.qualified)) {
      seen.add(cur.qualified);
      for (const f of cur.fields ?? []) if (!out.has(f)) out.set(f, cur);
      const parent = cur.extends;
      if (!parent || cur.kind !== "type") break;
      const r = qualifiedPart(parent, cur.plugin, "type", "type");
      cur = r && "key" in r ? partsByKey.get(r.key) : undefined;
    }
    return out;
  };

  const addRef = (target: Target, ref: Ref): void => {
    const list = b.refsTo.get(target.key) ?? [];
    list.push(ref);
    b.refsTo.set(target.key, list);
  };

  for (const p of plugins) {
    for (const f of p.yaml) {
      if (f.plain) {
        plainTokens(f);
        continue;
      }
      const docs = parseAllDocuments(f.text);
      const list: Document.Parsed[] = Array.isArray(docs) ? docs : [];
      list.forEach((doc, di) => {
        const block = f.docs[di];
        if (!block) return;
        const body = doc.toJS() as unknown;
        const part = block.part;
        const docHref = part ? `${p.href}#${part.anchor}` : `${p.href}#${fileAnchor(f.rel)}`;
        const docLabel = part ? `${PART_LABELS[part.kind][0].toLowerCase()} ${part.name}` : f.rel;
        const fieldsHere =
          part && (part.kind === "type" || part.kind === "extension")
            ? fieldMap(part)
            : new Map<string, Part>();
        function fieldMap(t: Part): Map<string, Part> {
          if (t.kind === "type") return lineageFields(t);
          const r = qualifiedPart(t.extends ?? "", p, "type", "type");
          const target = r && "key" in r ? partsByKey.get(r.key) : undefined;
          const m = target ? lineageFields(target) : new Map<string, Part>();
          for (const fl of t.fields ?? []) m.set(fl, t);
          return m;
        }
        const fieldAnchor = (owner: Part, field: string): string =>
          `${owner.anchor}-field-${field}`;
        const rootLocs = block.schema ? applicable({ schema: block.schema, ptr: "" }, body) : [];
        const noteUse = (locs: Loc[], path: string): void => {
          for (const l of locs) {
            const k = locKey(l);
            const u = b.uses.get(k) ?? { count: 0, examples: [] };
            u.count += 1;
            // A few examples, each from a different plugin.
            if (u.examples.length < 4 && !u.examples.some((e) => e.plugin === p)) {
              u.examples.push({ plugin: p, href: docHref, label: docLabel, path });
            }
            b.uses.set(k, u);
          }
        };
        noteUse(rootLocs.slice(0, 1), "");
        const unresolved = (path: string, value: string, reason: string): void => {
          b.unresolved.push({ plugin: p, file: f.path, path, value, reason, href: docHref });
        };
        const relationOf = (path: (string | number)[]): Ref["relation"] => {
          const last = path.at(-1);
          return last === "extends" || last === "extends_type" ? "extends" : "uses";
        };
        const ref = (
          target: Target,
          path: (string | number)[],
          relation = relationOf(path),
        ): void => {
          addRef(target, {
            from: p,
            ...(part ? { fromPart: part } : {}),
            path: pathText(path),
            relation,
          });
        };

        const valueRef = (raw: string, locs: Loc[], path: (string | number)[]): Resolution => {
          const keys = new Set(locs.map(locKey));
          if (keys.has(TYPE_REF)) return qualifiedPart(raw, p, "type", "type");
          if (keys.has(ROLE_REF)) {
            if (!raw.includes(".") && PROJECT_ROLES.has(raw) && !partIn(p, "role", raw)) {
              return { key: "", href: PROJECT_ROLES_HREF, title: `The project role ${raw}` };
            }
            return qualifiedPart(raw, p, "role", "lab role");
          }
          if (PLUGIN_REFS.some((k) => keys.has(k))) {
            if (block.kind === "Plugin" && path.length === 1 && path[0] === "name")
              return undefined;
            const t = plugin(raw, p);
            return t
              ? { key: t.id, href: t.href, title: `${t.name}: ${t.title}` }
              : { unresolved: `No plugin named ${raw} is in this repository.` };
          }
          for (const [k, what] of VALUE_REFS) {
            if (!keys.has(k)) continue;
            switch (what) {
              case "action":
                return qualifiedPart(`${p.name}.${raw}`, p, "action", "action");
              case "environment":
                return qualifiedPart(
                  raw.includes(".") ? raw : `${p.name}.${raw}`,
                  p,
                  "environment",
                  "environment",
                );
              case "setting":
                return qualifiedPart(`${p.name}.${raw}`, p, "setting", "setting");
              case "writer":
                return qualifiedPart(raw, p, "writer", "writer");
              case "format":
                return qualifiedPart(raw, p, "format", "file format");
              case "format?": {
                const m = /^([a-z][a-z0-9_]*\/[a-z][a-z0-9_]*)\.([a-z0-9-]+)$/.exec(raw);
                if (!m || !plugin(m[1]!, p)) return undefined;
                return qualifiedPart(raw, p, "format", "file format");
              }
              case "file":
              case "file?": {
                const rel = raw.replace(/^\.\//, "");
                const full = `${p.dir}/${rel}`;
                if (p.files.includes(full)) {
                  return { key: "", href: `${p.href}#${fileAnchor(rel)}`, title: full };
                }
                return what === "file"
                  ? { unresolved: `There's no ${rel} in ${p.dir}.` }
                  : undefined;
              }
            }
          }
          if (keys.has(FIELD_NAME) && fieldsHere.has(raw)) {
            const owner = fieldsHere.get(raw)!;
            return {
              key: "",
              href: `${owner.plugin.href}#${fieldAnchor(owner, raw)}`,
              title: `Field ${raw} of ${owner.qualified}`,
            };
          }
          return undefined;
        };

        const keyRef = (key: string, path: (string | number)[]): Resolution => {
          if (block.kind !== "Plugin") return undefined;
          if (path.length === 1 && path[0] === "dependencies") {
            const t = plugin(key, p);
            return t
              ? { key: t.id, href: t.href, title: `${t.name}: ${t.title}` }
              : { unresolved: `No plugin named ${key} is in this repository.` };
          }
          if (path.length === 3 && path[0] === "dependencies" && path[2] === "types") {
            return qualifiedPart(`${path[1]}.${key}`, p, "type", "type");
          }
          return undefined;
        };

        const keyAnchor = (key: string, path: (string | number)[]): string | undefined => {
          if (block.kind === "Plugin" && path.length === 1) {
            const tp = TOP_PARTS.find(([k]) => k === path[0]);
            if (tp) return partIn(p, tp[1], key)?.anchor;
            if (path[0] === "dependencies") return `dep-${pluginSlug(key)}`;
          }
          if (part && (part.kind === "type" || part.kind === "extension")) {
            if (path.length === 1 && path[0] === "fields") return fieldAnchor(part, key);
            if (path.length === 3 && path[0] === "fields" && path[2] === "states")
              return `${fieldAnchor(part, String(path[1]))}-state-${key}`;
          }
          return undefined;
        };

        const keyDoc = (decls: Loc[]): string | undefined => {
          for (const d of decls) {
            const desc = describe(d);
            if (desc) {
              const k = locKey(d);
              if (!b.keyDocs.has(k))
                b.keyDocs.set(k, { html: rich(desc), href: schemaHref(d), where: locLabel(d) });
              return k;
            }
          }
          const d = decls[0];
          if (d) {
            const k = locKey(d);
            const u = b.undescribed.get(k) ?? { loc: d, count: 0 };
            u.count += 1;
            b.undescribed.set(k, u);
            if (!b.keyDocs.has(k))
              b.keyDocs.set(k, { html: "", href: schemaHref(d), where: locLabel(d) });
            return k;
          }
          return undefined;
        };

        const scalarClass = (n: YNode): string => {
          if (!isScalar(n)) return "ys";
          const v = n.value;
          if (typeof v === "number" || typeof v === "bigint") return "yn";
          if (typeof v === "boolean" || v === null) return "yb";
          return "ys";
        };

        const walk = (
          node: YNode | null | undefined,
          value: unknown,
          locs: Loc[],
          path: (string | number)[],
        ): void => {
          if (!node) return;
          if (isMap(node)) {
            for (const pair of node.items) {
              const kn = pair.key as YNode | null;
              const key = isScalar(kn) ? String(kn.value) : "";
              const child = isObj(value) ? value[key] : undefined;
              const decls = declarations(locs, key);
              const childLocs = dedupe(decls.flatMap((d) => applicable(d, child)));
              if (isScalar(kn) && kn.range) {
                const t: Token = { start: kn.range[0], end: kn.range[1], cls: "yk" };
                if (block.schema) {
                  const doc = keyDoc(decls);
                  if (doc) {
                    t.doc = doc;
                    t.href = b.keyDocs.get(doc)!.href;
                  } else if (forbids(locs)) {
                    t.cls += " nokey";
                    t.title = "No schema declares this key.";
                    b.undeclared.push({ plugin: p, file: f.path, path: pathText([...path, key]) });
                  } else {
                    // Free-form data, such as an action test's record values: the schema
                    // takes any key here and describes none.
                    t.cls += " free";
                  }
                }
                const id = keyAnchor(key, path);
                if (id) t.id = id;
                const kr = keyRef(key, path) ?? resolveNames(nameSchemas(locs, key), key);
                if (kr && "unresolved" in kr) {
                  t.cls += " unres";
                  t.title = `Unresolved: ${kr.unresolved}`;
                  unresolved(pathText([...path, key]), key, kr.unresolved);
                } else if (kr) {
                  t.cls += " ref";
                  t.href = kr.href;
                  t.title = kr.title;
                  if (kr.key)
                    ref(
                      kr,
                      [...path, key],
                      path[0] === "dependencies" && path.length === 1 ? "depends" : "uses",
                    );
                }
                f.tokens.push(t);
              }
              noteUse(
                dedupe([...decls.slice(0, 1), ...childLocs.filter(isDefOrRoot)]),
                pathText([...path, key]),
              );
              walk(pair.value as YNode | null, child, childLocs, [...path, key]);
            }
          } else if (isSeq(node)) {
            node.items.forEach((item, i) => {
              const child = Array.isArray(value) ? value[i] : undefined;
              const decls = itemDeclarations(locs, i);
              const itemLocs = dedupe(decls.flatMap((d) => applicable(d, child)));
              noteUse(itemLocs.filter(isDefOrRoot), pathText([...path, i]));
              walk(item as YNode, child, itemLocs, [...path, i]);
            });
          } else if (isScalar(node) && node.range && node.range[1] > node.range[0]) {
            const t: Token = { start: node.range[0], end: node.range[1], cls: scalarClass(node) };
            if (typeof node.value === "string" && block.schema) {
              const r = valueRef(node.value, locs, path);
              if (r && "unresolved" in r) {
                t.cls += " unres";
                t.title = `Unresolved: ${r.unresolved}`;
                unresolved(pathText(path), node.value, r.unresolved);
              } else if (r) {
                t.cls += " ref";
                t.href = r.href;
                t.title = r.title;
                if (r.key) ref(r, path);
              }
            }
            f.tokens.push(t);
          }
        };
        /** A key that names something by its name's schema, such as a plugin. */
        function resolveNames(locs: Loc[], key: string): Resolution {
          const keys = new Set(locs.map(locKey));
          if (PLUGIN_REFS.some((k) => keys.has(k))) {
            const t = plugin(key, p);
            return t
              ? { key: t.id, href: t.href, title: t.name }
              : { unresolved: `No plugin named ${key} is in this repository.` };
          }
          return undefined;
        }
        walk(doc.contents as YNode | null, body, rootLocs, []);
      });
      f.tokens.sort((x, y) => x.start - y.start);
    }
  }
  // The test plugins' origins: files that name their directory or the plugin.
  for (const p of plugins.filter((x) => x.group === "test")) p.usedFrom = mentions(p, plugins);
  built = b;
  return b;
}

/** Whether a schema that applies to an object refuses keys it doesn't name. */
function forbids(locs: Loc[]): boolean {
  return locs.some((l) => {
    const s = at(l);
    return isObj(s) && (s.additionalProperties === false || s.unevaluatedProperties === false);
  });
}

/** A description's first sentence, for a link's title. */
export function firstSentence(s: string): string {
  const one = s.replace(/\s+/g, " ").trim();
  const m = /^.*?[.!?](?=\s|$)/.exec(one);
  const first = m ? m[0] : one;
  return first.length > 180 ? `${first.slice(0, 177)}…` : first;
}

const isDefOrRoot = (l: Loc): boolean => l.ptr === "" || /^\/\$defs\/[^/]+$/.test(l.ptr);

function dedupe(locs: Loc[]): Loc[] {
  const seen = new Set<string>();
  return locs.filter((l) => {
    const k = locKey(l);
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

const pathText = (path: (string | number)[]): string =>
  path
    .map((s) => (typeof s === "number" ? `[${s}]` : s))
    .join(".")
    .replaceAll(".[", "[");

export const fileAnchor = (rel: string): string => `file-${rel.replace(/[^A-Za-z0-9_.-]+/g, "-")}`;

/** YAML that no schema checks, highlighted by its own structure. */
function plainTokens(f: YamlFile): void {
  const docs = parseAllDocuments(f.text);
  const walk = (node: YNode | null | undefined): void => {
    if (!node) return;
    if (isMap(node)) {
      for (const pair of node.items) {
        const kn = pair.key as YNode | null;
        if (isScalar(kn) && kn.range)
          f.tokens.push({ start: kn.range[0], end: kn.range[1], cls: "yk" });
        walk(pair.value as YNode | null);
      }
    } else if (isSeq(node)) node.items.forEach((i) => walk(i as YNode));
    else if (isScalar(node) && node.range && node.range[1] > node.range[0]) {
      const v = node.value;
      const cls = typeof v === "number" ? "yn" : typeof v === "boolean" || v === null ? "yb" : "ys";
      f.tokens.push({ start: node.range[0], end: node.range[1], cls });
    }
  };
  for (const d of Array.isArray(docs) ? docs : []) walk(d.contents as YNode | null);
  f.tokens.sort((x, y) => x.start - y.start);
}

function mentions(p: PluginRec, all: PluginRec[]): string[] {
  const official = new Set(all.filter((x) => x.group === "official").map((x) => x.dir));
  const segs = p.dir.split("/");
  const needles = new Set<string>([p.dir]);
  for (let i = 1; i <= segs.length - 2; i++) {
    const s = segs.slice(i).join("/");
    if (!official.has(s)) needles.add(s);
  }
  const grep = (args: string[]): string[] => {
    try {
      return execFileSync(
        "git",
        ["grep", "-l", ...args, "--", ".", ":!docs/architecture", ":!pnpm-lock.yaml", `:!${p.dir}`],
        {
          cwd: REPO,
          encoding: "utf8",
        },
      )
        .split("\n")
        .filter(Boolean);
    } catch {
      return [];
    }
  };
  const found = new Set<string>();
  for (const n of needles) for (const f of grep(["-F", "-e", n])) found.add(f);
  for (const f of grep(["-w", "-F", "-e", p.name])) found.add(f);
  return [...found].sort();
}

// --- Rendering ------------------------------------------------------------------------

/** One page's table of key documentation: each key on it names an entry by number. */
export class KeyDocTable {
  private index = new Map<string, number>();
  entries: KeyDoc[] = [];
  constructor(private b: Browser) {}
  number(doc: string): number {
    let n = this.index.get(doc);
    if (n === undefined) {
      n = this.entries.length;
      this.index.set(doc, n);
      this.entries.push(this.b.keyDocs.get(doc)!);
    }
    return n;
  }
  json(): string {
    return JSON.stringify(this.entries.map((e) => [e.html, e.where, e.href])).replaceAll(
      "</",
      "<\\/",
    );
  }
}

/** A slice of a YAML file as highlighted HTML: keys linked to their docs, references to
 * what they name, comments dimmed. */
export function renderYaml(f: YamlFile, from: number, to: number, docs: KeyDocTable): string {
  const src = f.text;
  let out = "";
  let pos = from;
  const gap = (a: number, z: number): string => {
    if (z <= a) return "";
    let html = "";
    const chunk = src.slice(a, z);
    // Between keys and values there's only punctuation, comments and document markers. A
    // comment starts with # at a line's start or after whitespace and runs to its end.
    const re = /#[^\n]*|^(?:---|\.\.\.)(?=[ \t]*$)/gm;
    let last = 0;
    for (const m of chunk.matchAll(re)) {
      const abs = a + m.index!;
      const prev = abs === 0 ? "\n" : src[abs - 1]!;
      if (m[0].startsWith("#") ? !/\s/.test(prev) : prev !== "\n") continue;
      const cls = m[0].startsWith("#") ? "yc" : "yd";
      const body = cls === "yc" ? adrLinks(esc(m[0])) : esc(m[0]);
      html += esc(chunk.slice(last, m.index!)) + `<span class="${cls}">${body}</span>`;
      last = m.index! + m[0].length;
    }
    return html + esc(chunk.slice(last));
  };
  for (const t of f.tokens) {
    if (t.end <= from || t.start >= to) continue;
    const s = Math.max(t.start, from);
    out += gap(pos, s);
    const body = esc(src.slice(s, Math.min(t.end, to)));
    const attrs: string[] = [`class="${t.cls}"`];
    if (t.id) attrs.push(`id="${esc(t.id)}"`);
    if (t.doc !== undefined) attrs.push(`data-k="${docs.number(t.doc)}"`);
    // A key's own popup shows what it refers to; a value's title does.
    if (t.title) attrs.push(`${t.doc !== undefined ? "data-t" : "title"}="${esc(t.title)}"`);
    out += t.href
      ? `<a href="${esc(t.href)}" ${attrs.join(" ")}>${body}</a>`
      : `<span ${attrs.join(" ")}>${body}</span>`;
    pos = Math.min(t.end, to);
  }
  out += gap(pos, to);
  return out.replace(/\n+$/, "");
}

export const github = (p: string): string => githubUrl(p);

/** ADR numbers in a comment, linked to their pages when they exist. */
function adrLinks(html: string): string {
  const known = adrs();
  return html.replace(/\bADRs? (\d{4}(?:(?:, | and |, and )\d{4})*)/g, (whole) =>
    whole.replace(/\d{4}/g, (n) => (known.has(n) ? `<a href="/adrs/${n}/">${n}</a>` : n)),
  );
}

/** The links into a part, grouped: extended by, depended on by, used by. */
export function linksIn(b: Browser, key: string): { relation: Ref["relation"]; refs: Ref[] }[] {
  const all = b.refsTo.get(key) ?? [];
  return (["extends", "depends", "uses"] as const)
    .map((relation) => ({ relation, refs: all.filter((r) => r.relation === relation) }))
    .filter((g) => g.refs.length);
}

export const RELATION_LABEL: Record<Ref["relation"], string> = {
  extends: "Extended by",
  depends: "Depended on by",
  uses: "Used by",
};

/** Each referring part once, with the keys it refers from. */
export function bySource(refs: Ref[]): { href: string; label: string; paths: string[] }[] {
  const out = new Map<string, { href: string; label: string; paths: string[] }>();
  for (const r of refs) {
    const href = r.fromPart ? `${r.from.href}#${r.fromPart.anchor}` : r.from.href;
    const label = r.fromPart ? r.fromPart.qualified : r.from.name;
    const e = out.get(href) ?? { href, label, paths: [] };
    if (!e.paths.includes(r.path)) e.paths.push(r.path);
    out.set(href, e);
  }
  return [...out.values()];
}
