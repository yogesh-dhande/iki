// The REST API reference, generated from the OpenAPI document the server publishes
// (web/packages/sdk/openapi.json, which `make client` writes): every operation's page,
// its parameters, body and response as tables of fields, and an example request and
// response shape, all from the document. Nothing here is written by hand.
import { pathValue, sampleNumber, sampleString } from "./samples.ts";

export interface Schema {
  $ref?: string;
  type?: string | string[];
  format?: string;
  title?: string;
  description?: string;
  enum?: (string | number | boolean | null)[];
  default?: unknown;
  anyOf?: Schema[];
  oneOf?: Schema[];
  allOf?: Schema[];
  items?: Schema;
  properties?: Record<string, Schema>;
  required?: string[];
  additionalProperties?: boolean | Schema;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  maxItems?: number;
  minItems?: number;
  pattern?: string;
}

interface Parameter {
  name: string;
  in: "path" | "query" | "header" | "cookie";
  required?: boolean;
  description?: string;
  schema?: Schema;
}

interface Content {
  content?: Record<string, { schema?: Schema }>;
  description?: string;
  required?: boolean;
}

interface Operation {
  operationId: string;
  summary?: string;
  description?: string;
  tags?: string[];
  parameters?: Parameter[];
  requestBody?: Content;
  responses: Record<string, Content>;
  security?: Record<string, string[]>[];
}

export interface Document {
  openapi: string;
  info: { title: string; version: string; description?: string };
  paths: Record<string, Partial<Record<Method, Operation>>>;
  components: { schemas: Record<string, Schema>; securitySchemes?: Record<string, unknown> };
}

export const METHODS = ["get", "post", "put", "patch", "delete"] as const;
export type Method = (typeof METHODS)[number];

export interface Field {
  /** The field's name; `items[]` for an array's elements, `{key}` for a map's values. */
  name: string;
  /** How deep it is: 0 for the top level's fields. */
  depth: number;
  type: TypeLabel;
  required: boolean;
  description: string;
  /** Allowed values, when the schema lists them. */
  values: string[];
  /** Its default and its limits, as words: "default false", "at most 200". */
  notes: string[];
}

export interface TypeLabel {
  /** As a reader reads it: `string`, `uuid`, `integer`, `SignatureOut[]`, `object | null`. */
  text: string;
  /** The named schemas it refers to, which have pages of their own. */
  schemas: string[];
}

export interface Endpoint {
  method: Method;
  path: string;
  slug: string;
  operationId: string;
  tag: string;
  summary: string;
  description: string;
  parameters: (Field & { in: Parameter["in"] })[];
  body: { schema: string | null; type: TypeLabel; required: boolean; fields: Field[] } | null;
  responses: {
    status: string;
    description: string;
    schema: string | null;
    type: TypeLabel | null;
    fields: Field[];
  }[];
  /** How a caller may authenticate: a bearer token, the web app's session. */
  security: string[];
}

export interface Tag {
  name: string;
  label: string;
  slug: string;
  endpoints: Endpoint[];
}

/** The page an operation has: its method and path, `get-entities-ref-signatures`. */
export function slugOf(method: Method, path: string): string {
  const words = path
    .replace(/^\/api\/v\d+\//, "")
    .split(/[^A-Za-z0-9]+/)
    .filter(Boolean)
    .map((word) => word.toLowerCase());
  return [method, ...words].join("-");
}

export function tagSlug(tag: string): string {
  return tag
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

function refName(ref: string): string {
  const name = /^#\/components\/schemas\/(.+)$/.exec(ref)?.[1];
  if (!name) {
    throw new Error(`Only component schemas can be referred to, not ${ref}`);
  }
  return decodeURIComponent(name);
}

/** Its union's members other than null, and whether null is one. */
function members(schema: Schema): { options: Schema[]; nullable: boolean } {
  const union = schema.anyOf ?? schema.oneOf;
  if (union) {
    const options = union.filter((option) => option.type !== "null");
    return { options, nullable: options.length < union.length };
  }
  if (Array.isArray(schema.type)) {
    const types = schema.type.filter((type) => type !== "null");
    return {
      options: types.map((type) => ({ ...schema, type })),
      nullable: types.length < schema.type.length,
    };
  }
  return { options: [schema], nullable: false };
}

export function typeOf(schema: Schema): TypeLabel {
  const { options, nullable } = members(schema);
  const labels = options.map((option): TypeLabel => {
    if (option.$ref) {
      const name = refName(option.$ref);
      return { text: name, schemas: [name] };
    }
    if (option.allOf?.length === 1 && option.allOf[0]) {
      return typeOf(option.allOf[0]);
    }
    if (option.type === "array") {
      const inner = option.items ? typeOf(option.items) : { text: "any", schemas: [] };
      const text = inner.text.includes(" ") ? `(${inner.text})[]` : `${inner.text}[]`;
      return { text, schemas: inner.schemas };
    }
    if (option.enum) {
      return { text: "enum", schemas: [] };
    }
    if (option.type === "string" && option.format) {
      return { text: option.format, schemas: [] };
    }
    if (
      option.type === "object" &&
      !option.properties &&
      typeof option.additionalProperties === "object"
    ) {
      const inner = typeOf(option.additionalProperties);
      return { text: `map of ${inner.text}`, schemas: inner.schemas };
    }
    return { text: typeof option.type === "string" ? option.type : "any", schemas: [] };
  });
  if (nullable) {
    labels.push({ text: "null", schemas: [] });
  }
  return {
    text: labels.map((label) => label.text).join(" | ") || "any",
    schemas: [...new Set(labels.flatMap((label) => label.schemas))],
  };
}

function notesOf(schema: Schema): string[] {
  const notes: string[] = [];
  const { options } = members(schema);
  const own = options.length === 1 && options[0] ? { ...options[0], ...schema } : schema;
  if (own.default !== undefined && own.default !== null) {
    notes.push(`default ${JSON.stringify(own.default)}`);
  }
  if (own.minimum !== undefined) notes.push(`at least ${String(own.minimum)}`);
  if (own.maximum !== undefined) notes.push(`at most ${String(own.maximum)}`);
  if (own.minLength !== undefined && own.minLength > 0)
    notes.push(`at least ${String(own.minLength)} characters`);
  if (own.maxLength !== undefined) notes.push(`at most ${String(own.maxLength)} characters`);
  if (own.minItems !== undefined && own.minItems > 0)
    notes.push(`at least ${String(own.minItems)} items`);
  if (own.maxItems !== undefined) notes.push(`at most ${String(own.maxItems)} items`);
  return notes;
}

function valuesOf(schema: Schema): string[] {
  const { options } = members(schema);
  const listed = options.flatMap((option) => option.enum ?? option.items?.enum ?? []);
  return listed.filter((value) => value !== null).map(String);
}

/** The schema a field's rows describe: an object's own, or its array's elements'. */
function resolve(schema: Schema, doc: Document): Schema | null {
  const { options } = members(schema);
  if (options.length !== 1 || !options[0]) {
    return null;
  }
  const only = options[0];
  if (only.$ref) {
    return doc.components.schemas[refName(only.$ref)] ?? null;
  }
  if (only.allOf?.length === 1 && only.allOf[0]) {
    return resolve(only.allOf[0], doc);
  }
  return only;
}

/** A schema's fields as table rows: each property, then its own fields beneath it, as
 * deep as `depth` and never round a schema it's already inside. */
export function fieldsOf(schema: Schema, doc: Document, depth = 2, seen: string[] = []): Field[] {
  const rows: Field[] = [];
  const visit = (current: Schema, level: number, inside: string[]): void => {
    const object = resolve(current, doc);
    if (!object?.properties) {
      return;
    }
    const required = new Set(object.required ?? []);
    for (const [name, property] of Object.entries(object.properties)) {
      const type = typeOf(property);
      rows.push({
        name,
        depth: level,
        type,
        required: required.has(name),
        description: property.description ?? resolve(property, doc)?.description ?? "",
        values: valuesOf(property),
        notes: notesOf(property),
      });
      const named = type.schemas[0];
      if (level + 1 < depth && type.schemas.length === 1 && named && !inside.includes(named)) {
        const element = resolve(property, doc);
        const next = element?.type === "array" && element.items ? element.items : property;
        visit(next, level + 1, [...inside, named]);
      }
    }
  };
  // The schema itself is one it's inside: a field of its own type isn't expanded again.
  visit(schema, 0, schema.$ref ? [...seen, refName(schema.$ref)] : seen);
  return rows;
}

const SECURITY: Record<string, string> = {
  BearerAuth: "Bearer token",
  SessionAuth: "Web app session",
};

function bodyOf(content: Content | undefined): { schema: Schema; media: string } | null {
  const entries = Object.entries(content?.content ?? {});
  const [media, value] = entries.find(([type]) => type === "application/json") ?? entries[0] ?? [];
  return media && value?.schema ? { schema: value.schema, media } : null;
}

function schemaName(schema: Schema): string | null {
  return schema.$ref ? refName(schema.$ref) : null;
}

export function endpoints(doc: Document): Endpoint[] {
  const found: Endpoint[] = [];
  const slugs = new Map<string, string>();
  for (const [path, item] of Object.entries(doc.paths)) {
    for (const method of METHODS) {
      const operation = item[method];
      if (!operation) {
        continue;
      }
      const slug = slugOf(method, path);
      const taken = slugs.get(slug);
      if (taken) {
        throw new Error(`${method.toUpperCase()} ${path} and ${taken} would share a page`);
      }
      slugs.set(slug, `${method.toUpperCase()} ${path}`);
      const body = bodyOf(operation.requestBody);
      found.push({
        method,
        path,
        slug,
        operationId: operation.operationId,
        tag: operation.tags?.[0] ?? "other",
        summary: operation.summary ?? `${method.toUpperCase()} ${path}`,
        description: operation.description ?? "",
        parameters: (operation.parameters ?? []).map((parameter) => ({
          name: parameter.name,
          in: parameter.in,
          depth: 0,
          type: typeOf(parameter.schema ?? {}),
          required: parameter.required ?? false,
          description: parameter.description ?? parameter.schema?.description ?? "",
          values: valuesOf(parameter.schema ?? {}),
          notes: notesOf(parameter.schema ?? {}),
        })),
        body: body
          ? {
              schema: schemaName(body.schema),
              type: typeOf(body.schema),
              required: operation.requestBody?.required ?? false,
              fields: fieldsOf(body.schema, doc),
            }
          : null,
        responses: Object.entries(operation.responses).map(([status, response]) => {
          const returned = bodyOf(response);
          return {
            status,
            description: response.description ?? "",
            schema: returned ? schemaName(returned.schema) : null,
            type: returned ? typeOf(returned.schema) : null,
            fields: returned ? fieldsOf(returned.schema, doc) : [],
          };
        }),
        security: (operation.security ?? []).flatMap((scheme) =>
          Object.keys(scheme).map((name) => SECURITY[name] ?? name),
        ),
      });
    }
  }
  return found;
}

/** The operations by tag, each tag's in the document's order, the tags by name. */
export function tags(doc: Document): Tag[] {
  const grouped = new Map<string, Endpoint[]>();
  for (const endpoint of endpoints(doc)) {
    grouped.set(endpoint.tag, [...(grouped.get(endpoint.tag) ?? []), endpoint]);
  }
  return [...grouped]
    .map(([name, list]) => ({
      name,
      label: name.charAt(0).toUpperCase() + name.slice(1),
      slug: tagSlug(name),
      endpoints: list,
    }))
    .sort((a, b) => a.label.localeCompare(b.label));
}

/** An example of a schema: its documented defaults and enums, and otherwise the example
 * lab's values (samples.ts) by each field's name and format. Arrays hold one element;
 * deep or circular schemas stop at the schema's name. */
export function exampleOf(
  schema: Schema,
  doc: Document,
  depth = 0,
  inside: string[] = [],
  field?: string,
  seed = "",
): unknown {
  const { options, nullable } = members(schema);
  const only = options[0];
  if (!only) {
    return nullable ? null : {};
  }
  if (only.$ref) {
    const name = refName(only.$ref);
    const target = doc.components.schemas[name];
    if (!target || inside.includes(name) || depth > 3) {
      return `<${name}>`;
    }
    return exampleOf(target, doc, depth, [...inside, name], field, seed || name);
  }
  if (only.allOf?.length === 1 && only.allOf[0]) {
    return exampleOf(only.allOf[0], doc, depth, inside, field, seed);
  }
  if (only.default !== undefined && only.default !== null && only.default !== "") {
    return only.default;
  }
  if (only.enum?.length) {
    return only.enum.find((value) => value !== null) ?? null;
  }
  const description = schema.description ?? only.description;
  switch (only.type) {
    case "object": {
      if (!only.properties) {
        return {};
      }
      return Object.fromEntries(
        Object.entries(only.properties).map(([name, property]) => [
          name,
          exampleOf(property, doc, depth + 1, inside, name, `${seed}.${name}`),
        ]),
      );
    }
    case "array":
      return only.items
        ? [exampleOf(only.items, doc, depth + 1, inside, singular(field), `${seed}[]`)]
        : [];
    case "integer":
      return Math.round(sampleNumber(field, only.minimum));
    case "number":
      return sampleNumber(field, only.minimum);
    case "boolean":
      return false;
    case "string":
      return sampleString(field, only.format, description, seed, inside.at(-1));
    default:
      return null;
  }
}

/** An array field's element, named as one: `methods` → `method`. */
function singular(field: string | undefined): string | undefined {
  return field?.endsWith("s") ? field.slice(0, -1) : field;
}

/** A curl call of the operation, with the example lab's values in its path (or `example`'s). */
export function curl(endpoint: Endpoint, example?: Record<string, string>): string {
  const path = endpoint.path.replace(
    /\{([^}]+)\}/g,
    (_, name: string) => example?.[name] ?? pathValue(endpoint.path, name),
  );
  const query = endpoint.parameters
    .filter((parameter) => parameter.in === "query" && example?.[parameter.name] !== undefined)
    .map((parameter) => `${parameter.name}=${encodeURIComponent(example?.[parameter.name] ?? "")}`)
    .join("&");
  const url = `$IKI_URL${path}${query ? `?${query}` : ""}`;
  const lines = [
    `curl${endpoint.method === "get" ? "" : ` -X ${endpoint.method.toUpperCase()}`} -H "Authorization: Bearer $IKI_TOKEN" \\`,
  ];
  if (endpoint.body) {
    lines.push(`  -H "Content-Type: application/json" -d @body.json \\`);
  }
  lines.push(`  "${url}"`);
  return lines.join("\n");
}

/** Each named schema, and the endpoints that take or return it at any depth. */
export function schemaUses(doc: Document, all: readonly Endpoint[]): Map<string, Endpoint[]> {
  const reach = new Map<string, Set<string>>();
  const reachable = (name: string): Set<string> => {
    const known = reach.get(name);
    if (known) {
      return known;
    }
    const found = new Set<string>([name]);
    reach.set(name, found);
    const target = doc.components.schemas[name];
    for (const child of target ? referenced(target) : []) {
      for (const inner of reachable(child)) {
        found.add(inner);
      }
    }
    return found;
  };
  const uses = new Map<string, Endpoint[]>();
  for (const endpoint of all) {
    const named = new Set<string>();
    for (const top of [
      endpoint.body?.type.schemas ?? [],
      ...endpoint.responses.map((r) => r.type?.schemas ?? []),
    ].flat()) {
      reachable(top).forEach((name) => named.add(name));
    }
    for (const name of named) {
      uses.set(name, [...(uses.get(name) ?? []), endpoint]);
    }
  }
  return uses;
}

/** The schemas a schema names, anywhere inside it. */
export function referenced(schema: Schema): string[] {
  const found = new Set<string>();
  const walk = (value: unknown): void => {
    if (Array.isArray(value)) {
      value.forEach(walk);
    } else if (value && typeof value === "object") {
      for (const [key, inner] of Object.entries(value)) {
        if (key === "$ref" && typeof inner === "string") {
          found.add(refName(inner));
        } else {
          walk(inner);
        }
      }
    }
  };
  walk(schema);
  return [...found];
}
