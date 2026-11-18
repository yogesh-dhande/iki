/**
 * Plugins' rules in forms (ADR 0127): a type's `check` rules, evaluated with iki-wasm as
 * the server will evaluate them at the save, so a form says what a save would break as
 * it's typed. The server checks every rule again, and those this can't (one reading what
 * a reference refers to, or the database's `unique` and `no_overlap`), so its answer is
 * the last word.
 */

import { useEffect, useState } from "react";

import { expressionRenderer } from "./apps";
import type { EntityType, FieldDef } from "./entities";
import type { Schemas } from "@ikisci/sdk";

export type Rule = Schemas["RuleOut"];

/** A rule the values break, and what to tell whoever's typing. */
export interface RuleBreak {
  rule: string;
  label: string;
  message: string;
  /** It holds the record for review, rather than refuse the save: a reason saves it. */
  hold: boolean;
  fields: string[];
}

type Outcome = { value: boolean } | { text: string } | { error: string; empty?: boolean };

/** How long typing pauses before the rules are checked, in milliseconds. */
export const SETTLE_MS = 250;

/** Whether a form can check a rule: a `check` reading only the record's own values. */
export function checkable(type: EntityType, rule: Rule): boolean {
  if (rule.kind !== "check" || !rule.check || !type.rule_variables) return false;
  const fields = new Map(type.fields.map((f) => [f.name, f]));
  return rule.reads.every((name) => {
    const f = fields.get(name);
    return f !== undefined && !refers(f);
  });
}

function refers(f: FieldDef): boolean {
  return f.type === "ref" || (f.type === "list" && f.items === "ref");
}

/** A value as a rule reads it: dates as midnight UTC, as the server gives them. Undefined
 * when the form can't say, such as a quantity typed in another unit: the server converts. */
function readable(f: FieldDef, value: unknown): unknown {
  if (value === null || value === undefined) return null;
  return item(f.type === "list" ? (f.items ?? "") : f.type, value);
}

function item(kind: string, value: unknown): unknown {
  if (kind === "date" && typeof value === "string") return `${value}T00:00:00Z`;
  if (kind === "quantity" && typeof value !== "number") return undefined;
  if (Array.isArray(value)) {
    const items = value.map((v) => item(kind, v));
    return items.includes(undefined) ? undefined : items;
  }
  return value;
}

function now(): { now: string; today: string } {
  const moment = new Date();
  const day = new Date(
    Date.UTC(moment.getUTCFullYear(), moment.getUTCMonth(), moment.getUTCDate()),
  );
  return {
    now: moment.toISOString().replace(/\.\d+Z$/, "Z"),
    today: day.toISOString().replace(/\.\d+Z$/, "Z"),
  };
}

/** The rules a record with these values breaks, of those the form can check. An edit
 * names what it ``changed``: only rules reading one of those are checked, as the server
 * checks them. */
export async function breaks(
  type: EntityType,
  values: Record<string, unknown>,
  changed: Set<string> | null = null,
): Promise<RuleBreak[]> {
  const fields = new Map(type.fields.map((f) => [f.name, f]));
  // Every field, as the server gives a record: null where it's empty, or not read here.
  const record: Record<string, unknown> = { id: "", hrid: "" };
  for (const f of type.fields) record[f.name] = null;
  const rules: Rule[] = [];
  for (const rule of type.rules ?? []) {
    if (!checkable(type, rule)) continue;
    if (changed && rule.reads.length && !rule.reads.some((n) => changed.has(n))) continue;
    let known = true;
    for (const name of rule.reads) {
      const f = fields.get(name);
      const value = f ? readable(f, values[name]) : undefined;
      if (value === undefined) known = false;
      else record[name] = value;
    }
    if (known) rules.push(rule);
  }
  if (!rules.length) return [];
  const expressions: { source: string; kind: "condition" | "message" }[] = [];
  const at: { rule: Rule; check: number; when: number | null; message: number }[] = [];
  for (const rule of rules) {
    const check = expressions.push({ source: rule.check ?? "", kind: "condition" }) - 1;
    const when = rule.when ? expressions.push({ source: rule.when, kind: "condition" }) - 1 : null;
    const message = expressions.push({ source: rule.message, kind: "message" }) - 1;
    at.push({ rule, check, when, message });
  }
  const request = JSON.stringify({
    variables: type.rule_variables,
    expressions,
    contexts: [{ record, ...now() }],
  });
  const results = (await expressionRenderer().rules(request)) as Outcome[][];
  const outcomes = results[0] ?? [];
  const found: RuleBreak[] = [];
  for (const { rule, check, when, message } of at) {
    if (when !== null) {
      const w = outcomes[when];
      // Doesn't apply, or can't be worked out here: the server says.
      if (!w || "error" in w || ("value" in w && !w.value)) continue;
    }
    const c = outcomes[check];
    if (!c || "error" in c || !("value" in c) || c.value) continue;
    const m = outcomes[message];
    found.push({
      rule: rule.name,
      label: rule.label,
      message: m && "text" in m ? m.text : `It breaks “${rule.label}”.`,
      hold: rule.hold !== null && rule.hold !== undefined,
      fields: rule.fields.length ? rule.fields : rule.reads,
    });
  }
  return found;
}

/** The rules the values break, checked once typing settles. Empty while there's nothing
 * to check, or if the module can't load: the server checks at the save. */
export function useRuleBreaks(
  type: EntityType,
  values: Record<string, unknown>,
  changed: Set<string> | null = null,
): RuleBreak[] {
  const [found, setFound] = useState<RuleBreak[]>([]);
  const key = JSON.stringify([values, changed ? [...changed].sort() : null]);
  const has = (type.rules ?? []).some((r) => checkable(type, r));
  useEffect(() => {
    if (!has) return;
    let current = true;
    const timer = setTimeout(() => {
      breaks(type, values, changed).then(
        (result) => {
          if (current) setFound(result);
        },
        () => {
          if (current) setFound([]);
        },
      );
    }, SETTLE_MS);
    return () => {
      current = false;
      clearTimeout(timer);
    };
    // `key` stands for values and changed, which are new objects each render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, has, type]);
  return has ? found : [];
}

/** Each field's first rule message, by `values/<name>` as the API points. */
export function breakErrors(found: RuleBreak[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const b of found) {
    for (const name of b.fields) {
      out[`values/${name}`] ??= b.message;
    }
  }
  return out;
}
