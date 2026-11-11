/**
 * The entity grid's filters: conditions on fields, as the search API takes
 * them (ADR 0037), with the words people read and the inputs they type in.
 *
 * Filters live in the page's address (`?filters=[…]&sort=…`), so a filtered
 * grid can be bookmarked and shared. References are written by HRID there,
 * which the API takes too, so addresses stay readable.
 */

import type { EntityType, FieldDef } from "./entities";
import {
  defaultUnit,
  emptyDraft,
  encodeValue,
  isBlank,
  itemField,
  localDateTime,
  type Draft,
  type Encoded,
  type Picked,
} from "./entity-values";

/** A condition, as the API and the address hold it. */
export interface Filter {
  field: string;
  op: string;
  value: unknown;
}

/** The choices of operator: the API's, with `is_set` as two ("is set", "is empty"). */
export type Op =
  | "eq"
  | "ne"
  | "lt"
  | "lte"
  | "gt"
  | "gte"
  | "contains"
  | "starts_with"
  | "matches"
  | "has"
  | "set"
  | "unset";

export const MAX_FILTERS = 20;

function builtIn(name: string, label: string, type: string): FieldDef {
  return {
    name,
    label,
    type,
    description: null,
    plugin: "core",
    required: true,
    immutable: true,
    computed: false,
    unit: null,
    unit_symbol: null,
    display_unit: null,
    display_unit_symbol: null,
    display_scale: null,
    display_offset: null,
    input_units: [],
    items: null,
    to: null,
    values: [],
  };
}

export const HRID = builtIn("hrid", "ID", "string");
const CREATED = builtIn("created_at", "Created", "datetime");
export const UPDATED = builtIn("updated_at", "Last changed", "datetime");

/** What a grid can filter by: every entity's ID and times, and the type's fields. */
export function filterable(type: EntityType | undefined): FieldDef[] {
  return [HRID, ...(type?.fields ?? []), CREATED, UPDATED];
}

const ORDERED = new Set(["int", "float", "quantity", "date", "datetime"]);
const SORTABLE = new Set([
  "string",
  "enum",
  "int",
  "float",
  "quantity",
  "date",
  "datetime",
  "bool",
]);

export function sortable(field: FieldDef): boolean {
  return SORTABLE.has(field.type);
}

/** The operators a field takes, with their words. */
export function operators(field: FieldDef): { value: Op; label: string }[] {
  const when = field.type === "date" || field.type === "datetime";
  const own: [Op, string][] = (() => {
    switch (field.type) {
      case "string":
        return [
          ["contains", "contains"],
          ["eq", "is"],
          ["ne", "is not"],
          ["starts_with", "starts with"],
        ];
      case "text":
        return [
          ["matches", "has the words"],
          ["contains", "contains"],
        ];
      case "enum":
      case "bool":
      case "ref":
        return [
          ["eq", "is"],
          ["ne", "is not"],
        ];
      case "list":
        return [["has", "has"]];
      case "file":
      case "json":
        return [];
      default:
        return ORDERED.has(field.type)
          ? [
              ["eq", "is"],
              ["ne", "is not"],
              ["gt", when ? "is after" : "is more than"],
              ["gte", when ? "is on or after" : "is at least"],
              ["lt", when ? "is before" : "is less than"],
              ["lte", when ? "is on or before" : "is at most"],
            ]
          : [];
    }
  })();
  const presence: [Op, string][] = field.required
    ? []
    : [
        ["set", "is set"],
        ["unset", "is empty"],
      ];
  return [...own, ...presence].map(([value, label]) => ({ value, label }));
}

/** Whether the operator needs a value typed in. */
export function takesValue(op: Op): boolean {
  return op !== "set" && op !== "unset";
}

/** The field a filter's value is typed into: a list's item, for `has`. */
export function valueField(field: FieldDef): FieldDef {
  const typed = field.type === "list" ? itemField(field) : field;
  // The rules of what can be stored don't limit what can be searched for, and the
  // words looked for in long text fit on a line.
  return {
    ...typed,
    type: typed.type === "text" ? "string" : typed.type,
    required: true,
    description: null,
  };
}

/** A filter row while it's being edited. */
export interface FilterDraft {
  key: number;
  field: string;
  op: Op;
  draft: Draft;
}

let keys = 0;

export function newDraft(field: FieldDef): FilterDraft {
  const op = operators(field)[0]?.value ?? "set";
  return { key: ++keys, field: field.name, op, draft: emptyDraft(valueField(field)) };
}

/** The address's filters as rows to edit. Ones that no longer fit the type are dropped. */
export function toDrafts(filters: Filter[], fields: FieldDef[]): FilterDraft[] {
  const out: FilterDraft[] = [];
  for (const f of filters) {
    const field = fields.find((d) => d.name === f.field);
    if (!field) continue;
    const op: Op | undefined =
      f.op === "is_set" ? (f.value === false ? "unset" : "set") : (f.op as Op);
    if (!operators(field).some((o) => o.value === op)) continue;
    out.push({
      key: ++keys,
      field: field.name,
      op,
      draft: takesValue(op) ? draftOf(valueField(field), f.value) : null,
    });
  }
  return out;
}

function draftOf(field: FieldDef, value: unknown): Draft {
  switch (field.type) {
    case "quantity": {
      if (typeof value === "number")
        return { number: String(value), unit: field.unit ?? "", other: "" };
      if (value && typeof value === "object" && "value" in value && "unit" in value) {
        const { value: number, unit } = value as { value: number; unit: string };
        const known = field.input_units.some((u) => u.code === unit);
        return known
          ? { number: String(number), unit, other: "" }
          : { number: String(number), unit: "__other", other: unit };
      }
      return { number: "", unit: defaultUnit(field), other: "" };
    }
    case "ref":
      return typeof value === "string"
        ? ({ id: value, hrid: value, title: value } satisfies Picked)
        : null;
    case "bool":
      return value === true ? "true" : value === false ? "false" : "";
    case "datetime":
      return typeof value === "string" ? localDateTime(value) : "";
    default:
      return typeof value === "string" || typeof value === "number" ? String(value) : "";
  }
}

/** A row as a filter: null while it's incomplete, or what's wrong with what's typed. */
export function toFilter(
  row: FilterDraft,
  fields: FieldDef[],
): { filter: Filter } | { error: string } | null {
  const field = fields.find((d) => d.name === row.field);
  if (!field) return null;
  if (row.op === "set" || row.op === "unset") {
    return { filter: { field: field.name, op: "is_set", value: row.op === "set" } };
  }
  const typed = valueField(field);
  if (isBlank(typed, row.draft)) return null;
  if (typed.type === "ref") {
    // By HRID: the API takes it, and the address stays readable.
    const picked = row.draft as Picked;
    return { filter: { field: field.name, op: row.op, value: picked.hrid || picked.id } };
  }
  const encoded: Encoded = encodeValue(typed, row.draft);
  return encoded.ok
    ? { filter: { field: field.name, op: row.op, value: encoded.value } }
    : { error: encoded.error };
}

/** The address's `filters`, if it holds them. */
export function validateFilters(value: unknown): Filter[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const out = value
    .filter(
      (f): f is Filter =>
        typeof f === "object" &&
        f !== null &&
        typeof (f as Filter).field === "string" &&
        typeof (f as Filter).op === "string" &&
        "value" in f,
    )
    .slice(0, MAX_FILTERS)
    .map(({ field, op, value: v }) => ({ field, op, value: v }));
  return out.length ? out : undefined;
}

/** For inputs that ask their type for a field's rules: one with none. */
export const NO_RULES = { name: "", fields: [], values_schema: {} } as unknown as EntityType;
