/**
 * The entity grid's filters: conditions on fields, as the search API takes
 * them (ADR 0037), with the words people read and the inputs they type in.
 *
 * Filters live in the page's address (`?filters=[…]&sort=…`), so a filtered
 * grid can be bookmarked and shared. References are written by HRID there,
 * and people by email address, which the API takes too, so addresses stay
 * readable. "Is me" is `{{ me }}`: a shared search finds each reader's own.
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
  | "me"
  | "set"
  | "unset";

/** Whoever searches (ADR 0117, ADR 0122). */
export const ME = "{{ me }}";

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
    role: null,
    role_label: null,
    assignment: false,
    assignment_field: null,
    assignment_states: [],
    initial: null,
    states: [],
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

/** An enum value's label: a state's (ADR 0125), or the value itself. */
export function optionLabel(field: FieldDef, value: string): string {
  return field.states.find((s) => s.name === value)?.label ?? value;
}

/** A state field (ADR 0125) as a filter takes it: an enum of its states, by name. */
function asEnum(field: FieldDef): FieldDef {
  if (field.type !== "state") return field;
  return { ...field, type: "enum", values: field.states.map((s) => s.name) };
}

/** Who's assigned a record now (ADR 0122), in any assignment field counting in its state. */
export const ASSIGNED: FieldDef = {
  ...builtIn("assigned", "Assigned to", "person"),
  required: false,
};
/** Records whose assignee can't be assigned them any more. */
export const NEEDS_REASSIGNING = builtIn("needs_reassigning", "Needs reassigning", "bool");

/** What a grid can filter by: every entity's ID and times, its state fields (ADR 0125),
 * who it's assigned to, the type's other fields, and how each was made. */
export function filterable(type: EntityType | undefined): FieldDef[] {
  const fields = type?.fields ?? [];
  const states = fields.filter((f) => f.type === "state");
  const rest = fields.filter((f) => f.type !== "state");
  const assigned = fields.some((f) => f.assignment) ? [ASSIGNED, NEEDS_REASSIGNING] : [];
  return [HRID, ...states.map(asEnum), ...assigned, ...rest, CREATED, UPDATED, MADE];
}

const ORDERED = new Set(["int", "float", "quantity", "date", "datetime"]);
const SORTABLE = new Set([
  "string",
  "enum",
  "state",
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
      case "state":
      case "bool":
      case "ref":
        return [
          ["eq", "is"],
          ["ne", "is not"],
        ];
      case "person":
        return [
          ["me", "is me"],
          ["eq", "is"],
          ["ne", "is not"],
        ];
      case "list":
        return field.items === "person"
          ? [
              ["me", "has me"],
              ["has", "has"],
            ]
          : [["has", "has"]];
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
  return op !== "set" && op !== "unset" && op !== "me";
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
      f.op === "is_set"
        ? f.value === false
          ? "unset"
          : "set"
        : (f.op === "eq" || f.op === "has") && f.value === ME
          ? "me"
          : (f.op as Op);
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
    case "person":
      // By email address, as the address holds it; the picker shows it as typed.
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
  if (row.op === "me") {
    return { filter: { field: field.name, op: field.type === "list" ? "has" : "eq", value: ME } };
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
  if (typed.type === "ref" || typed.type === "person") {
    // By HRID or email address: the API takes them, and the address stays readable.
    const picked = row.draft as Picked;
    return { filter: { field: field.name, op: row.op, value: picked.hrid || picked.id } };
  }
  const encoded: Encoded = encodeValue(typed, row.draft);
  return encoded.ok
    ? { filter: { field: field.name, op: row.op, value: encoded.value } }
    : { error: encoded.error };
}

/** What the search box's words say of people (ADR 0122), as conditions: `assigned:me`,
 * `assigned:none`, `assigned:@ana@example.com`, `needs:reassigning`, and a field naming
 * people, such as `reviewer:me` or `reviewer:@ana@example.com`. The rest is text. */
export function searchWords(q: string): { text: string; conditions: Filter[] } {
  const conditions: Filter[] = [];
  const rest: string[] = [];
  for (const word of q.split(/\s+/).filter(Boolean)) {
    const match = /^([a-z_][a-z0-9_]*):(.+)$/i.exec(word);
    const name = match?.[1]?.toLowerCase();
    const said = match?.[2] ?? "";
    if (name === "needs" && said.toLowerCase() === "reassigning") {
      conditions.push({ field: "needs_reassigning", op: "eq", value: true });
    } else if (name === "assigned" && said.toLowerCase() === "none") {
      conditions.push({ field: "assigned", op: "is_set", value: false });
    } else if (name && (said.toLowerCase() === "me" || said.toLowerCase() === "@me")) {
      conditions.push({ field: name, op: "eq", value: ME });
    } else if (name && said.startsWith("@") && said.length > 1) {
      conditions.push({ field: name, op: "eq", value: said.slice(1) });
    } else {
      rest.push(word);
    }
  }
  return { text: rest.join(" "), conditions };
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
