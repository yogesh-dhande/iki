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
    plugin: "iki/core",
    required: true,
    immutable: true,
    computed: false,
    unique: false,
    detached: false,
    link: null,
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
/** How it was made (ADR 0106): the software, the kind and who says so, of one making. */
export const MADE: FieldDef = { ...builtIn("made", "How it was made", "making"), required: false };
/** The software a making's filter names, typed as a string field is. */
export const SOFTWARE = builtIn("software", "Software", "string");

/** The parts of a making's draft, in order. */
export type MakingDraft = [software: string, kind: string, by: string];

/** The labels of a state field's values, by the values' list: a filter's copies keep it. */
const LABELS = new WeakMap<readonly string[], Record<string, string>>();

/** An enum value's label: a state's (ADR 0118), or the value itself. */
export function optionLabel(field: FieldDef, value: string): string {
  return LABELS.get(field.values)?.[value] ?? value;
}

/** A type's records' state, as a field to filter by: null for a type without states. */
export function stateField(type: EntityType | undefined): FieldDef | null {
  if (!type?.states.length) return null;
  const values = type.states.map((s) => s.name);
  LABELS.set(values, Object.fromEntries(type.states.map((s) => [s.name, s.label])));
  return { ...builtIn("state", "State", "enum"), values };
}

/** What a grid can filter by: every entity's ID and times, its state, the type's fields,
 * and how each was made. */
export function filterable(type: EntityType | undefined): FieldDef[] {
  const state = stateField(type);
  return [HRID, ...(state ? [state] : []), ...(type?.fields ?? []), CREATED, UPDATED, MADE];
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
      case "making":
        return [
          ["has", "was"],
          ["set", "is recorded"],
          ["unset", "isn’t recorded"],
        ];
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
  const presence: [Op, string][] =
    field.required || field.type === "making"
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

/** A filter's empty value: a making's has three parts. */
export function emptyValue(field: FieldDef): Draft {
  return field.type === "making" ? ["", "", ""] : emptyDraft(valueField(field));
}

export function newDraft(field: FieldDef): FilterDraft {
  const op = operators(field)[0]?.value ?? "set";
  return { key: ++keys, field: field.name, op, draft: emptyValue(field) };
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
    case "making": {
      const given = (value && typeof value === "object" ? value : {}) as Record<string, unknown>;
      const text = (key: string) => (typeof given[key] === "string" ? given[key] : "");
      return [text("software"), text("kind"), text("by")] satisfies MakingDraft;
    }
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
  if (field.type === "making") {
    const [software, kind, by] = (row.draft ?? ["", "", ""]) as MakingDraft;
    const value = Object.fromEntries(
      Object.entries({ software: software.trim(), kind, by }).filter(([, v]) => v !== ""),
    );
    return Object.keys(value).length ? { filter: { field: field.name, op: "has", value } } : null;
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
