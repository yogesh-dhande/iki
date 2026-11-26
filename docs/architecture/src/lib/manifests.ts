// Plugins' manifests, read the way the spec's validator finds them
// (spec/src/iki_spec/validate.py: `KINDS`, `DIRECTORIES`, `_files`): plugin.yaml at the
// root, and any number of YAML documents under each kind's directory.
import { parseAllDocuments } from "yaml";
import { filesUnder, readRepo, resolvePath } from "./repo.ts";

export interface Manifest {
  kind: string;
  name: string;
  label?: string;
  description?: string;
  abstract?: boolean;
  extends?: string;
  schemaVersion?: number;
  file: string;
  fields: string[];
  body: Record<string, unknown>;
}

export interface Plugin {
  dir: string;
  name: string;
  version: string;
  title: string;
  description: string;
  iki?: string;
  dependencies: string[];
  manifests: Manifest[];
}

/** The kinds and their directories, read from the validator so this page can't disagree. */
export function manifestKinds(): { kind: string; schema: string; directory: string }[] {
  const source = readRepo("spec/src/iki_spec/validate.py");
  const block = (name: string): string => {
    const m = new RegExp(`^${name} = \\{([\\s\\S]*?)^\\}`, "m").exec(source);
    if (!m) throw new Error(`spec/src/iki_spec/validate.py no longer defines ${name}.`);
    return m[1];
  };
  const pairs = (text: string): [string, string][] =>
    [...text.matchAll(/"([A-Za-z]+)":\s*("?)([A-Za-z_-]+)\2,/g)].map((m) => [m[1], m[3]]);
  const dirConst = /^TYPES_DIR = "([a-z]+)"/m.exec(source)?.[1] ?? "types";
  const dirs = new Map(
    pairs(block("DIRECTORIES")).map(([k, v]) => [k, v === "TYPES_DIR" ? dirConst : v]),
  );
  return pairs(block("KINDS")).map(([kind, schema]) => ({
    kind,
    schema,
    directory: kind === "Plugin" ? "plugin.yaml" : `${dirs.get(kind) ?? "?"}/`,
  }));
}

function text(v: unknown): string {
  return typeof v === "string" || typeof v === "number" ? String(v) : "";
}

function str(v: unknown): string | undefined {
  return typeof v === "string" ? v.trim() : undefined;
}

function documents(file: string): Record<string, unknown>[] {
  return parseAllDocuments(readRepo(file))
    .map((d) => d.toJS() as unknown)
    .filter((d): d is Record<string, unknown> => !!d && typeof d === "object" && "kind" in d);
}

function manifest(file: string, body: Record<string, unknown>): Manifest {
  const fields = body.fields;
  const m: Manifest = {
    kind: text(body.kind),
    name: text(body.name),
    file,
    fields: fields && typeof fields === "object" ? Object.keys(fields) : [],
    body,
  };
  const label = str(body.label);
  if (label) m.label = label;
  const description = str(body.description);
  if (description) m.description = description;
  if (body.abstract === true) m.abstract = true;
  const ext = str(body.extends);
  if (ext) m.extends = ext;
  if (typeof body.schema_version === "number") m.schemaVersion = body.schema_version;
  return m;
}

export function readPlugin(dir: string): Plugin {
  resolvePath(dir);
  const top = documents(`${dir}/plugin.yaml`).at(0);
  if (!top) throw new Error(`${dir}/plugin.yaml holds no Plugin manifest.`);
  const deps = top.dependencies;
  const plugin: Plugin = {
    dir,
    name: text(top.name),
    version: text(top.version),
    title: str(top.title) ?? "",
    description: str(top.description) ?? "",
    dependencies: deps && typeof deps === "object" ? Object.keys(deps) : [],
    manifests: [],
  };
  const iki = str(top.iki);
  if (iki) plugin.iki = iki;
  const dirs = new Set(
    manifestKinds()
      .map((k) => k.directory)
      .filter((d) => d.endsWith("/")),
  );
  for (const sub of dirs) {
    for (const file of filesUnder(`${dir}/${sub}`)) {
      if (!/\.ya?ml$/.test(file) || file.split("/").some((p) => p.startsWith("."))) continue;
      for (const body of documents(file)) plugin.manifests.push(manifest(file, body));
    }
  }
  return plugin;
}

/** Every first-party plugin under plugins/, by name. */
export function firstPartyPlugins(): Plugin[] {
  const dirs = new Set(
    filesUnder("plugins")
      .filter((f) => /^plugins\/[^/]+\/plugin\.ya?ml$/.test(f))
      .map((f) => f.split("/").slice(0, 2).join("/")),
  );
  return [...dirs].map(readPlugin).sort((a, b) => a.name.localeCompare(b.name));
}
