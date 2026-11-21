/**
 * The entity grid's filters: conditions on fields, as the search API takes
 * them (ADR 0037), with the words people read and the inputs they type in.
 *
 * Filters live in the page's address (`?filters=[…]&sort=…`), so a filtered
 * grid can be bookmarked and shared. References are written by HRID there,
 * and people by email address, which the API takes too, so addresses stay
 * readable. "Is me" is `{{ me }}`: a shared search finds each reader's own.
 */

import type { Schemas } from "@ikisci/sdk";

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
  | "motif"
  | "me"
  | "set"
  | "unset"
  | "derived_from"
  | "used_for";

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
    to: [],
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

/** An enum value's label: a state's (ADR 0125), a rule's (ADR 0127), or the value itself. */
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

/** What plugins' rules caught (ADR 0127): records a rule holds for review now, and records
 * that break a rule they were saved before (one added or changed since, or brought from
 * elsewhere). By one of the type's rules, or by any. */
export const HELD = builtIn("held", "Held for review", "rule");
export const BREAKS = builtIn("breaks", "Breaks", "rule");

type Rule = Schemas["RuleOut"];

/** A filter by the rules that catch records one way: theirs are its values, as an enum's,
 * each with its label (as a state's is, which is all `optionLabel` reads). */
function byRules(field: FieldDef, rules: Rule[]): FieldDef {
  return {
    ...field,
    values: rules.map((r) => r.name),
    states: rules.map((r) => ({
      name: r.name,
      label: r.label,
      description: r.description ?? null,
      locks: false,
      holds_amount: false,
      reason: null,
      set_by: [],
      signatures: [],
    })),
  };
}

/** The filters by what rules caught: without a type, by any rule; with one, those its
 * rules can catch, by each. */
function caughtBy(type: EntityType | undefined): FieldDef[] {
  if (!type) return [HELD, BREAKS];
  const checks = (type.rules ?? []).filter((r) => r.kind === "check");
  const holding = checks.filter((r) => r.hold);
  const refusing = checks.filter((r) => !r.hold);
  return [
    ...(holding.length ? [byRules(HELD, holding)] : []),
    ...(refusing.length ? [byRules(BREAKS, refusing)] : []),
  ];
}

/** Whether a search's conditions ask what rules caught. */
export function asksWhatRulesCaught(conditions: Filter[]): boolean {
  return conditions.some((c) => c.field === HELD.name || c.field === BREAKS.name);
}

/** What it was derived from, or what it was used to make, across the graph (ADR 0123):
 * one row of a filter, which the API has as `downstream` or `upstream` `of` an entity. */
export const LINEAGE: FieldDef = {
  ...builtIn("lineage", "Lineage", "lineage"),
  to: ["iki/core.Entity"],
};
const LINEAGE_FIELDS = new Map<string, "derived_from" | "used_for">([
  ["downstream", "derived_from"],
  ["upstream", "used_for"],
]);

/** A built-in field naming a project, chosen from `projects` by ID, as the API takes it;
 * each choice is labelled with its project's name, as a state field's are with their
 * states'. `optional`: it may be unset, and offers "is set" and "is empty" too. */
function projectField(
  name: string,
  label: string,
  projects: readonly { id: string; name: string }[],
  optional: boolean,
): FieldDef {
  return {
    ...builtIn(name, label, "enum"),
    required: !optional,
    values: projects.map((p) => p.id),
    states: projects.map((p) => ({
      name: p.id,
      label: p.name,
      description: null,
      set_by: [],
      locks: false,
      holds_amount: false,
      reason: null,
      signatures: [],
    })),
  };
}

/** The service a request was sent to (ADR 0132), from `projects`: a service's queue is the
 * requests sent to it. Unless `sent`, it may be unset: not sent yet. */
export function sentToField(
  projects: readonly { id: string; name: string }[],
  sent = false,
): FieldDef {
  return projectField("sent_to", "Sent to", projects, !sent);
}

/** The project that asked for a request (ADR 0132), which owns it, from `projects`. */
export function askedByField(projects: readonly { id: string; name: string }[]): FieldDef {
  return projectField("owner_project", "Asked by", projects, false);
}

/** What a grid can filter by: every entity's ID and times, built-in fields it's given
 * (`extra`, such as the service a request was sent to), its state fields (ADR 0125), who
 * it's assigned to, the type's other fields, what its rules caught, how each was made,
 * and its lineage. */
export function filterable(type: EntityType | undefined, extra: FieldDef[] = []): FieldDef[] {
  const fields = type?.fields ?? [];
  const states = fields.filter((f) => f.type === "state");
  const rest = fields.filter((f) => f.type !== "state");
  const assigned = fields.some((f) => f.assignment) ? [ASSIGNED, NEEDS_REASSIGNING] : [];
  return [
    HRID,
    ...extra,
    ...states.map(asEnum),
    ...assigned,
    ...rest,
    ...caughtBy(type),
    CREATED,
    UPDATED,
    MADE,
    LINEAGE,
  ];
}

const ORDERED = new Set(["int", "float", "quantity", "date", "datetime"]);
/** The string formats a search finds by a motif, as the API's operators say. */
const SEQUENCE_FORMATS = new Set(["dna_sequence", "rna_sequence", "protein_sequence"]);
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
          // A sequence's bases or residues in IUPAC codes (ADR 0175): on a duplex's other
          // strand too, and across a circle's origin.
          ...(field.format && SEQUENCE_FORMATS.has(field.format)
            ? ([["motif", "has the motif"]] satisfies [Op, string][])
            : []),
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
      case "lineage":
        return [
          ["derived_from", "is derived from"],
          ["used_for", "was used to make"],
        ];
      case "making":
        return [
          ["has", "was"],
          ["set", "is recorded"],
          ["unset", "isn’t recorded"],
        ];
      case "rule": {
        // "Held for review by the rule …", "Breaks the rule …": by one, if the type has any.
        const held = field.name === HELD.name;
        const one: [Op, string][] = field.values.length
          ? [["eq", held ? "by the rule" : "the rule"]]
          : [];
        return [
          ["set", held ? "by any rule" : "any rule"],
          ...one,
          ["unset", held ? "by no rule" : "no rule"],
        ];
      }
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
    field.required || field.type === "making" || field.type === "lineage"
      ? []
      : field.plugin === "iki/core" && field.name === "sent_to"
        ? [
            ["set", "is any service"],
            ["unset", "is none: not sent yet"],
          ]
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
  if (field.type === "lineage") return { ...field, type: "ref", required: true };
  if (field.type === "rule") return { ...field, type: "enum", required: true };
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
    const lineage = f.op === "of" ? LINEAGE_FIELDS.get(f.field) : undefined;
    const field = fields.find((d) => d.name === (lineage ? LINEAGE.name : f.field));
    if (!field) continue;
    const op: Op | undefined =
      lineage ??
      (f.op === "is_set"
        ? f.value === false
          ? "unset"
          : "set"
        : (f.op === "eq" || f.op === "has") && f.value === ME
          ? "me"
          : (f.op as Op));
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
  if (field.type === "lineage") {
    const picked = row.draft as Picked | null;
    if (!picked?.id) return null;
    const name = row.op === "used_for" ? "upstream" : "downstream";
    return { filter: { field: name, op: "of", value: picked.hrid || picked.id } };
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
 * people, such as `reviewer:me` or `reviewer:@ana@example.com`; of lineage (ADR 0123):
 * `downstream:DNR-000001` (what was derived from it) and `upstream:RES-000004` (what it
 * was derived from); and of what rules caught (ADR 0135): `is:held` and `is:breaking`, by
 * any rule, and `held:purity_in_specification` and `breaks:expires_after_made`, by one.
 * The rest is text. */
export function searchWords(q: string): { text: string; conditions: Filter[] } {
  const conditions: Filter[] = [];
  const rest: string[] = [];
  // A list of rows' fields (ADR 0137), `components.part:PRT-000001`: one row with every
  // value the words give for that list.
  const rows = new Map<string, Record<string, string>>();
  for (const word of q.split(/\s+/).filter(Boolean)) {
    const inRow = /^([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*):(.+)$/i.exec(word);
    if (inRow?.[1] && inRow[2] && inRow[3]) {
      const list = inRow[1].toLowerCase();
      const row = rows.get(list) ?? {};
      if (!rows.has(list)) {
        rows.set(list, row);
        conditions.push({ field: list, op: "has", value: row });
      }
      row[inRow[2].toLowerCase()] = inRow[3];
      continue;
    }
    const match = /^([a-z_][a-z0-9_]*):(.+)$/i.exec(word);
    const name = match?.[1]?.toLowerCase();
    const said = match?.[2] ?? "";
    if ((name === "downstream" || name === "upstream") && said) {
      conditions.push({ field: name, op: "of", value: said });
    } else if (name === "is" && said.toLowerCase() === "held") {
      conditions.push({ field: HELD.name, op: "is_set", value: true });
    } else if (name === "is" && said.toLowerCase() === "breaking") {
      conditions.push({ field: BREAKS.name, op: "is_set", value: true });
    } else if ((name === HELD.name || name === BREAKS.name) && said) {
      // A rule by its name, as its type lists it: an extension's has its plugin before it.
      conditions.push({ field: name, op: "eq", value: said });
    } else if (name === "needs" && said.toLowerCase() === "reassigning") {
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

/** The words `searchWords` reads as these conditions, or null if some have none: each word
 * read again gives the same condition, so the words say just what the conditions do. */
export function wordsOf(conditions: unknown[]): string[] | null {
  const words: string[] = [];
  for (const c of conditions) {
    if (typeof c !== "object" || c === null || !("field" in c) || !("op" in c)) return null;
    const { field, op, value } = c as Filter;
    if (op === "of") words.push(`${field}:${String(value)}`);
    else if (op === "is_set" && value === true && field === HELD.name) words.push("is:held");
    else if (op === "is_set" && value === true && field === BREAKS.name) words.push("is:breaking");
    else if (op === "is_set" && value === false && field === "assigned")
      words.push("assigned:none");
    else if (op === "eq" && value === true && field === "needs_reassigning")
      words.push("needs:reassigning");
    else if (op === "eq" && value === ME) words.push(`${field}:me`);
    else if (op === "eq" && (field === HELD.name || field === BREAKS.name))
      words.push(`${field}:${String(value)}`);
    else if (op === "eq") words.push(`${field}:@${String(value)}`);
    else if (op === "has" && typeof value === "object" && value !== null && !Array.isArray(value))
      words.push(...Object.entries(value).map(([k, v]) => `${field}.${k}:${String(v)}`));
    else return null;
  }
  const again = searchWords(words.join(" "));
  return !again.text && sameJson(again.conditions, conditions) ? words : null;
}

/** Whether two JSON values say the same thing: arrays in order, objects whatever the order
 * of their keys, which a database needn't keep (PostgreSQL's jsonb sorts them). */
export function sameJson(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) || Array.isArray(b)) {
    return (
      Array.isArray(a) &&
      Array.isArray(b) &&
      a.length === b.length &&
      a.every((item, i) => sameJson(item, b[i]))
    );
  }
  if (typeof a === "object" && a !== null && typeof b === "object" && b !== null) {
    const keys = Object.keys(a);
    const theirs = b as Record<string, unknown>;
    return (
      keys.length === Object.keys(b).length &&
      keys.every((k) => k in theirs && sameJson((a as Record<string, unknown>)[k], theirs[k]))
    );
  }
  return a === b;
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
