// The first-party plugins' record types, extensions and actions, as `python -m sitegen
// generate` read them from their manifests (src/generated/plugins.json), and where each is
// on the Plugins reference: `/reference/plugins/samples/#Sample`,
// `#Sample.status` for one of its fields, `#import_from_lims` for an action.
import { withoutCitations } from "./markdown.ts";

export const PLUGINS = "/reference/plugins/";

export interface State {
  name: string;
  label: string;
  description: string;
  locks: boolean;
}

export interface Field {
  key: string;
  type: string;
  label: string;
  description: string;
  flags: string[];
  registry?: string;
  unit?: string;
  values?: string[];
  items?: string;
  to?: string[];
  fields?: Field[];
  states?: State[];
  initial?: string;
}

export interface RecordType {
  name: string;
  label: string;
  description: string;
  abstract: boolean;
  extends: string[];
  path: string;
  fields: Field[];
  hrid?: string;
  identity?: string[];
  amount?: string;
}

export interface Extension {
  extends: string;
  path: string;
  fields: Field[];
}

export interface Named {
  name: string;
  type: string;
  label: string;
  description: string;
  required: boolean;
  many?: boolean;
  optional?: boolean;
  default?: string;
  values?: string[];
}

export interface Action {
  name: string;
  label: string;
  description: string;
  path: string;
  inputs: Named[];
  params: Named[];
  creates: string[];
  updates: { input: string; fields: string[] }[];
}

export interface Role {
  name: string;
  label: string;
  description: string;
}

export interface Plugin {
  name: string;
  short: string;
  slug: string;
  title: string;
  description: string;
  version: string;
  path: string;
  roles: Role[];
  dependencies: string[];
  /** The starters it ships, by name: `dose-response`. */
  starters?: string[];
  types: RecordType[];
  extensions: Extension[];
  actions: Action[];
}

/** A plugin's page. */
export function pluginHref(plugin: Pick<Plugin, "slug">): string {
  return `${PLUGINS}${plugin.slug}/`;
}

/** An extension's anchor, by the type it extends: `extension-Protein`. */
export function extensionId(extension: Pick<Extension, "extends">): string {
  return `extension-${extension.extends.split(".").pop() ?? extension.extends}`;
}

/** A field's rows, each nested field beneath its parent, with its anchor. */
export interface FieldRow {
  id: string;
  depth: number;
  field: Field;
}

export function fieldRows(owner: string, fields: Field[], depth = 0): FieldRow[] {
  return fields.flatMap((field) => {
    const id = `${owner}.${field.key}`;
    return [{ id, depth, field }, ...fieldRows(id, field.fields ?? [], depth + 1)];
  });
}

/** Every anchor a plugin's page has, in order. */
export function anchors(plugin: Plugin): string[] {
  return [
    ...(plugin.roles.length ? ["roles", ...plugin.roles.map((role) => `role.${role.name}`)] : []),
    ...plugin.types.flatMap((type) => [
      type.name,
      ...fieldRows(type.name, type.fields).map((row) => row.id),
    ]),
    ...plugin.extensions.flatMap((extension) => {
      const id = extensionId(extension);
      return [id, ...fieldRows(id, extension.fields).map((row) => row.id)];
    }),
    ...plugin.actions.map((action) => action.name),
  ];
}

/** Where a type named in full is on the reference, or null if no first-party plugin has it. */
export function typeHref(plugins: Plugin[], qualified: string): string | null {
  const dot = qualified.lastIndexOf(".");
  const [plugin, name] = [qualified.slice(0, dot), qualified.slice(dot + 1)];
  const found = plugins.find((candidate) => candidate.name === plugin);
  return found?.types.some((type) => type.name === name) ? `${pluginHref(found)}#${name}` : null;
}

function strip<T extends { description: string }>(entry: T): T {
  return { ...entry, description: withoutCitations(entry.description) };
}

function stripFields(fields: Field[]): Field[] {
  return fields.map((field) => ({
    ...strip(field),
    ...(field.states ? { states: field.states.map(strip) } : {}),
    ...(field.fields ? { fields: stripFields(field.fields) } : {}),
  }));
}

/** The plugins as the pages show them: no design-record citations in their text, and every
 * anchor once on its page. */
export function prepared(plugins: Plugin[]): Plugin[] {
  return plugins.map((plugin) => {
    const shown: Plugin = {
      ...strip(plugin),
      roles: plugin.roles.map(strip),
      types: plugin.types.map((type) => ({ ...strip(type), fields: stripFields(type.fields) })),
      extensions: plugin.extensions.map((extension) => ({
        ...extension,
        fields: stripFields(extension.fields),
      })),
      actions: plugin.actions.map((action) => ({
        ...strip(action),
        inputs: action.inputs.map(strip),
        params: action.params.map(strip),
      })),
    };
    const seen = new Set<string>();
    for (const id of anchors(shown)) {
      if (seen.has(id)) throw new Error(`${plugin.name}'s page has #${id} twice`);
      seen.add(id);
    }
    const cited = /\bADRs? \d{4}\b/.exec(JSON.stringify(shown));
    if (cited) throw new Error(`${plugin.name}'s reference still cites ${cited[0]}`);
    return shown;
  });
}

const generated = import.meta.glob<{ default: { plugins: Plugin[] } }>(
  "../generated/plugins.json",
  { eager: true },
);

let cache: Plugin[] | undefined;

/** The first-party plugins, the core plugin's first. */
export function plugins(): Plugin[] {
  if (cache) return cache;
  const found = generated["../generated/plugins.json"];
  if (!found) {
    throw new Error("No plugins.json: run `make docs` (python -m sitegen generate) first");
  }
  cache = prepared(found.default.plugins);
  return cache;
}
