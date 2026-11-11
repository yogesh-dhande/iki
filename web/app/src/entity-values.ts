/**
 * Entity forms' values: what each kind of field holds while people type
 * (a draft), the checks the browser makes before sending, and the JSON the
 * API takes. The API checks everything again; these checks give the same
 * answers sooner, in plain words.
 */

import type { Entity, EntityType, FieldDef } from "./entities";

/** A referenced entity, as a picker shows it. */
export interface Picked {
  id: string;
  hrid: string;
  title: string;
  /** A file still uploading: the form waits for it. */
  uploading?: boolean;
}

export interface QuantityDraft {
  number: string;
  /** A UCUM code from the field's units, or OTHER_UNIT to type one. */
  unit: string;
  other: string;
}

export type Draft = string | QuantityDraft | Picked | null | Draft[];

export const OTHER_UNIT = "__other";

/** The fields people fill in: actions set computed ones. */
/** What a form's fields are checked against: an entity type's values, or an action's params. */
export interface Schemaed {
  values_schema: EntityType["values_schema"];
}

export function formFields(type: EntityType): FieldDef[] {
  return type.fields.filter((f) => !f.computed);
}

export function itemField(field: FieldDef): FieldDef {
  return { ...field, type: field.items ?? "string", required: false };
}

// --- JSON → draft -----------------------------------------------------------------

/** "2026-03-01T09:30:00Z" → "2026-03-01T09:30" in the browser's time zone. */
export function localDateTime(iso: string): string {
  const date = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  const seconds = date.getSeconds() ? `:${pad(date.getSeconds())}` : "";
  return (
    `${String(date.getFullYear())}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}${seconds}`
  );
}

/** The unit a quantity is typed in at first: the one people read it in. */
export function defaultUnit(field: FieldDef): string {
  return field.input_units[0]?.code ?? field.unit ?? "";
}

export function emptyDraft(field: FieldDef): Draft {
  if (field.type === "list") return [];
  if (field.type === "quantity") return { number: "", unit: defaultUnit(field), other: "" };
  if (field.type === "ref" || field.type === "file") return null;
  return "";
}

/** A field's draft from its default, for a new entity. */
export function initialDraft(field: FieldDef, type: Schemaed): Draft {
  const schema = propertySchema(type, field.name);
  if (schema.default === undefined || schema.default === null) return emptyDraft(field);
  return toDraft(field, schema.default, { refs: {}, files: {} });
}

/** A value from the API as a draft. Quantities show in the unit people read them in. */
export function toDraft(
  field: FieldDef,
  value: unknown,
  entity: Pick<Entity, "refs" | "files">,
): Draft {
  if (value === null || value === undefined) return emptyDraft(field);
  if (field.type === "list") {
    const item = itemField(field);
    return (value as unknown[]).map((v) => toDraft(item, v, entity));
  }
  switch (field.type) {
    case "quantity": {
      const number = value as number;
      const unit = defaultUnit(field);
      if (unit !== field.unit && field.display_unit === unit && field.display_scale != null) {
        const shown = number * field.display_scale + (field.display_offset ?? 0);
        return { number: String(Number(shown.toPrecision(12))), unit, other: "" };
      }
      return { number: String(number), unit: field.unit ?? unit, other: "" };
    }
    case "ref": {
      const id = value as string;
      const found = entity.refs[id];
      return { id, hrid: found?.hrid ?? id, title: found?.title ?? "An entity you can’t read" };
    }
    case "file": {
      const id = value as string;
      return { id, hrid: "", title: entity.files[id]?.name ?? "A file you can’t read" };
    }
    case "bool":
      return value === true ? "true" : "false";
    case "datetime":
      return localDateTime(value as string);
    case "json":
      return JSON.stringify(value, null, 2);
    default:
      return typeof value === "string" ? value : JSON.stringify(value);
  }
}

// --- Checks -----------------------------------------------------------------------

interface PropertySchema {
  default?: unknown;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  allOf?: { pattern?: string }[];
  minItems?: number;
  maxItems?: number;
  items?: PropertySchema;
  "x-iki-format"?: string;
  "x-iki-media-types"?: string[];
  "x-iki-max-size"?: number;
  anyOf?: PropertySchema[];
}

/** The field's JSON Schema, without the `null` its optional fields allow. */
export function propertySchema(type: Schemaed, name: string): PropertySchema {
  const properties = (type.values_schema["properties"] ?? {}) as Record<string, PropertySchema>;
  const found = properties[name] ?? {};
  const value = found.anyOf?.find((s) => (s as { type?: string }).type !== "null");
  return value ? { ...value, default: found.default } : found;
}

const FORMAT_HELP: Record<string, string> = {
  dna_sequence: "Use DNA bases only: A, C, G, T, or IUPAC codes such as N.",
  rna_sequence: "Use RNA bases only: A, C, G, U, or IUPAC codes such as N.",
  protein_sequence: "Use one-letter amino acid codes only, such as MKV.",
  email: "Enter an email address, such as name@example.com.",
  uri: "Enter an address with its scheme, such as https://example.com.",
};

function patterns(schema: PropertySchema): string[] {
  return [
    ...(schema.pattern ? [schema.pattern] : []),
    ...(schema.allOf ?? []).flatMap((s) => (s.pattern ? [s.pattern] : [])),
  ];
}

function matches(pattern: string, value: string): boolean {
  try {
    return new RegExp(pattern, "u").test(value);
  } catch {
    // A pattern JavaScript can't read: the server still checks it.
    return true;
  }
}

const number = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 20 });

function textProblem(schema: PropertySchema, value: string): string | undefined {
  // Code points, as the server counts them (Python's len).
  // eslint-disable-next-line @typescript-eslint/no-misused-spread
  const length = [...value].length;
  if (schema.minLength != null && schema.minLength > 1 && length < schema.minLength) {
    return `Enter at least ${String(schema.minLength)} characters.`;
  }
  if (schema.maxLength != null && length > schema.maxLength) {
    return `Use at most ${number.format(schema.maxLength)} characters: this is ${number.format(length)}.`;
  }
  for (const pattern of patterns(schema)) {
    if (!matches(pattern, value)) {
      const format = schema["x-iki-format"];
      if (pattern === "^[^\\r\\n]*$") return "Keep it to one line.";
      return (format && FORMAT_HELP[format]) ?? `It must match the pattern ${pattern}.`;
    }
  }
  return undefined;
}

function rangeProblem(schema: PropertySchema, value: number, unit = ""): string | undefined {
  const suffix = unit ? ` ${unit}` : "";
  if (schema.minimum != null && value < schema.minimum) {
    return `It must be at least ${number.format(schema.minimum)}${suffix}.`;
  }
  if (schema.maximum != null && value > schema.maximum) {
    return `It must be at most ${number.format(schema.maximum)}${suffix}.`;
  }
  return undefined;
}

/** "1,5" and "1 000" are ambiguous between locales: numbers are written 1.5 and 1000. */
const DECIMAL = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;
const WHOLE = /^[+-]?\d+$/;

export type Encoded = { ok: true; value: unknown } | { ok: false; error: string };

const ok = (value: unknown): Encoded => ({ ok: true, value });
const fail = (error: string): Encoded => ({ ok: false, error });

export function isBlank(field: FieldDef, draft: Draft): boolean {
  if (draft === null) return true;
  if (Array.isArray(draft)) return draft.every((d) => isBlank(itemField(field), d));
  if (typeof draft === "string") return draft.trim() === "";
  if ("number" in draft) return draft.number.trim() === "";
  return false;
}

/**
 * The JSON the API takes for a draft, or what's wrong with it. Blank is null:
 * whether a field can be blank is the caller's check.
 */
export function encode(field: FieldDef, draft: Draft, type: Schemaed): Encoded {
  return encodeWith(field, draft, propertySchema(type, field.name));
}

/** A draft as a value to search for: as the API takes it, without the field's rules. */
export function encodeValue(field: FieldDef, draft: Draft): Encoded {
  return encodeWith(field, draft, {});
}

function encodeWith(field: FieldDef, draft: Draft, schema: PropertySchema): Encoded {
  if (isBlank(field, draft)) return ok(field.type === "list" ? [] : null);
  if (field.type === "list") {
    const item = itemField(field);
    const values: unknown[] = [];
    const items = (draft as Draft[]).filter((d) => !isBlank(item, d));
    for (const [index, d] of items.entries()) {
      const one = encodeWith(item, d, schema.items ?? {});
      if (!one.ok) return fail(`Item ${String(index + 1)}: ${one.error}`);
      values.push(one.value);
    }
    if (schema.minItems != null && values.length < schema.minItems) {
      return fail(`Add at least ${String(schema.minItems)}.`);
    }
    if (schema.maxItems != null && values.length > schema.maxItems) {
      return fail(`Add at most ${String(schema.maxItems)}: there are ${String(values.length)}.`);
    }
    return ok(values);
  }
  switch (field.type) {
    case "string": {
      const value = (draft as string).trim();
      const problem = textProblem(schema, value);
      return problem ? fail(problem) : ok(value);
    }
    case "text": {
      const value = draft as string;
      const problem = textProblem(schema, value);
      return problem ? fail(problem) : ok(value);
    }
    case "int": {
      const value = (draft as string).trim();
      if (!WHOLE.test(value)) return fail("Enter a whole number, such as 42.");
      const parsed = Number(value);
      if (!Number.isSafeInteger(parsed)) return fail("That number is too large.");
      const problem = rangeProblem(schema, parsed);
      return problem ? fail(problem) : ok(parsed);
    }
    case "float": {
      const value = (draft as string).trim();
      if (!DECIMAL.test(value)) return fail("Enter a number, such as 1.5 or 2e-3.");
      const parsed = Number(value);
      if (!Number.isFinite(parsed)) return fail("That number is too large.");
      const problem = rangeProblem(schema, parsed);
      return problem ? fail(problem) : ok(parsed);
    }
    case "quantity": {
      const q = draft as QuantityDraft;
      const value = q.number.trim();
      if (!DECIMAL.test(value)) return fail("Enter a number, such as 1.5 or 2e-3.");
      const parsed = Number(value);
      if (!Number.isFinite(parsed)) return fail("That number is too large.");
      const unit = q.unit === OTHER_UNIT ? q.other.trim() : q.unit;
      if (!unit) return fail("Enter the unit, as a UCUM code such as mg/mL.");
      if (unit !== field.unit) return ok({ value: parsed, unit });
      const problem = rangeProblem(schema, parsed, field.unit_symbol ?? unit);
      return problem ? fail(problem) : ok(parsed);
    }
    case "bool":
      return ok(draft === "true");
    case "enum":
      return ok(draft);
    case "date":
      return /^\d{4}-\d{2}-\d{2}$/.test(draft as string)
        ? ok(draft)
        : fail("Enter a date, such as 2026-03-01.");
    case "datetime": {
      const date = new Date(draft as string);
      if (Number.isNaN(date.getTime())) return fail("Enter a date and time.");
      return ok(date.toISOString());
    }
    case "ref":
      return ok((draft as Picked).id);
    case "file": {
      const file = draft as Picked;
      return file.uploading ? fail(`Wait for ${file.title} to finish uploading.`) : ok(file.id);
    }
    case "json":
      try {
        return ok(JSON.parse(draft as string));
      } catch {
        return fail('Enter valid JSON, such as {"key": 1}.');
      }
    default:
      return ok(draft);
  }
}

/** Whether two JSON values are the same (an edit sends only what changed). */
export function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([k, v]) => [k, canonical(v)]),
    );
  }
  return value;
}
