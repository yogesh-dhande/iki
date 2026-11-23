/**
 * How a type's records move between the states of each of its state fields (ADR 0125):
 * the flow a diagram draws, shaped as `GET /api/v1/entity-types/{type}/flow` answers
 * (ADR 0198).
 *
 * This is the one place the app reads a type's flow. Until the server serves that
 * endpoint, it works the flow out from the type definition the app already has: each
 * state's guards (ADR 0128) say which states a record may come from. Switching to the
 * endpoint changes {@link typeFlowQuery} alone.
 *
 * The working-out follows the server's own (iki-wasm's `rules/flow.rs`): a guard is read
 * knowing only the field, the state a record comes from and the one it goes to, never
 * the record, the person or the history. A guard that settles to false rules an origin
 * out; one that can't be settled without the record keeps the origin, as conditional.
 * An expression this doesn't understand is never a reason to leave a transition out.
 */

import type { Schemas } from "@ikisci/sdk";
import { queryOptions } from "@tanstack/react-query";

import { typeNamed, typesQuery, type EntityType, type FieldDef } from "./entities";

type StateOut = Schemas["StateOut"];
type GuardOut = Schemas["GuardOut"];

/** A move from one state to another that a state field allows. */
export interface FlowTransition {
  from: string;
  to: string;
  /** Whether it also depends on the record, the person or its history: its guards can't
   * be settled from the two states alone. */
  conditional: boolean;
}

/** One state field's flow. */
export interface FlowField {
  name: string;
  label: string;
  initial: string | null;
  states: StateOut[];
  transitions: FlowTransition[];
}

/** A type's flows: one per state field, none for a type without one. */
export interface TypeFlow {
  type: string;
  label: string;
  fields: FlowField[];
}

/** A type's flows. It reads the type definition now; it reads the server's
 * `GET /api/v1/entity-types/{type}/flow` once that's there, with the same answer. */
export function typeFlowQuery(name: string) {
  return queryOptions({
    ...typesQuery,
    select: (types: EntityType[]) => typeFlow(typeNamed(types, name)),
  });
}

/** A type's flows, from its definition. */
export function typeFlow(
  type: Pick<EntityType, "name" | "label"> & { fields: readonly FlowSource[] },
): TypeFlow {
  return {
    type: type.name,
    label: type.label,
    fields: type.fields.filter((f) => f.type === undefined || f.type === "state").map(fieldFlow),
  };
}

/** What a state field's flow is worked out from: a type's `FieldOut`, or the same parts of
 * it. */
export type FlowSource = Pick<FieldDef, "name" | "label" | "initial" | "states"> & {
  type?: string;
};

/** One state field's flow: for each state someone sets, the states a record may come to it
 * from. A state no one sets (an initial state that's only a start) has no way in. */
export function fieldFlow(field: FlowSource): FlowField {
  const transitions: FlowTransition[] = [];
  for (const target of field.states) {
    // A state no one sets, an initial one only, has no way in.
    if (!target.set_by.length) continue;
    const guards = (target.guards ?? []).map((g) => parsed(g));
    for (const origin of field.states) {
      if (origin.name === target.name) continue;
      const change: Change = { field: field.name, from: origin.name, to: target.name };
      const allowed = guards.reduce<Truth>((all, g) => both(all, holds(g, change)), true);
      if (allowed === false) continue;
      transitions.push({ from: origin.name, to: target.name, conditional: allowed === null });
    }
  }
  return {
    name: field.name,
    label: field.label,
    initial: field.initial,
    states: field.states,
    transitions,
  };
}

/** True, false, or unknown without the record, the person or the history. */
type Truth = boolean | null;

interface Change {
  field: string;
  from: string;
  to: string;
}

interface Parsed {
  check: Expr | null;
  /** Undefined: it always applies. */
  when?: Expr | null;
}

function parsed(guard: GuardOut): Parsed {
  return {
    check: parse(guard.check),
    ...(guard.when ? { when: parse(guard.when) } : {}),
  };
}

/** Whether a guard lets a change through: one that doesn't apply does. */
function holds(guard: Parsed, change: Change): Truth {
  const applies = guard.when === undefined ? true : guard.when ? truth(guard.when, change) : null;
  if (applies === false) return true;
  if (applies === null) return null;
  return guard.check ? truth(guard.check, change) : null;
}

function both(a: Truth, b: Truth): Truth {
  if (a === false || b === false) return false;
  if (a === true && b === true) return true;
  return null;
}

function not(a: Truth): Truth {
  return a === null ? null : !a;
}

function truth(expr: Expr, change: Change): Truth {
  switch (expr.kind) {
    case "bool":
      return expr.value;
    case "and":
      return both(truth(expr.a, change), truth(expr.b, change));
    case "or":
      return not(both(not(truth(expr.a, change)), not(truth(expr.b, change))));
    case "not":
      return not(truth(expr.a, change));
    case "eq":
    case "ne": {
      const a = text(expr.a, change);
      const b = text(expr.b, change);
      if (a === null || b === null) return null;
      return expr.kind === "eq" ? a === b : a !== b;
    }
    case "in": {
      const item = text(expr.a, change);
      if (item === null || expr.b.kind !== "list") return null;
      let found = false;
      for (const element of expr.b.items) {
        const value = text(element, change);
        if (value === null) return null;
        found ||= value === item;
      }
      return found;
    }
    default:
      return null;
  }
}

/** A string an expression stands for, given the change: a literal, or what `state.from`,
 * `state.to`, `state.field` and the record's own field (its state now) are. */
function text(expr: Expr, change: Change): string | null {
  if (expr.kind === "string") return expr.value;
  if (expr.kind !== "select" || expr.operand.kind !== "ident") return null;
  const [name, field] = [expr.operand.name, expr.field];
  if (name === "state") {
    if (field === "from") return change.from;
    if (field === "to") return change.to;
    if (field === "field") return change.field;
    return null;
  }
  if (name === "record" && field === change.field) return change.from;
  return null;
}

// CEL (https://github.com/google/cel-spec), as far as a flow needs to read it: logic,
// equality, `in` a list, strings and the fields of names. Anything else parses as
// something unknown, so its truth is unknown.

type Expr =
  | { kind: "or" | "and" | "eq" | "ne" | "in"; a: Expr; b: Expr }
  | { kind: "not"; a: Expr }
  | { kind: "string"; value: string }
  | { kind: "bool"; value: boolean }
  | { kind: "list"; items: Expr[] }
  | { kind: "ident"; name: string }
  | { kind: "select"; operand: Expr; field: string }
  | { kind: "other" };

const OTHER: Expr = { kind: "other" };

interface Token {
  kind: "op" | "string" | "number" | "ident";
  value: string;
}

const OPERATORS = ["||", "&&", "==", "!=", "<=", ">=", "<", ">", "!", "?", ":", ".", ","];
const BRACKETS = "()[]{}+-*/%";

function tokens(source: string): Token[] | null {
  const out: Token[] = [];
  let i = 0;
  while (i < source.length) {
    const c = source.charAt(i);
    if (/\s/.test(c)) {
      i += 1;
      continue;
    }
    const raw = (c === "r" || c === "R") && /["']/.test(source.charAt(i + 1));
    if (c === '"' || c === "'" || raw) {
      const start = raw ? i + 1 : i;
      const quote = source.charAt(start);
      const triple = source.startsWith(quote.repeat(3), start);
      const close = triple ? quote.repeat(3) : quote;
      let j = start + close.length;
      let value = "";
      while (j < source.length && !source.startsWith(close, j)) {
        if (source.charAt(j) === "\\" && !raw) {
          value += source.charAt(j + 1);
          j += 2;
        } else {
          value += source.charAt(j);
          j += 1;
        }
      }
      if (j >= source.length) return null;
      out.push({ kind: "string", value });
      i = j + close.length;
      continue;
    }
    const number = /^(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)[uU]?/.exec(source.slice(i));
    if (number) {
      out.push({ kind: "number", value: number[0] });
      i += number[0].length;
      continue;
    }
    const ident = /^[A-Za-z_][A-Za-z0-9_]*/.exec(source.slice(i));
    if (ident) {
      out.push({ kind: "ident", value: ident[0] });
      i += ident[0].length;
      continue;
    }
    const op = OPERATORS.find((o) => source.startsWith(o, i));
    if (op) {
      out.push({ kind: "op", value: op });
      i += op.length;
      continue;
    }
    if (BRACKETS.includes(c)) {
      out.push({ kind: "op", value: c });
      i += 1;
      continue;
    }
    return null;
  }
  return out;
}

/** An expression's syntax, or null when it isn't one. */
export function parse(source: string): Expr | null {
  const found = tokens(source);
  if (!found) return null;
  const list: Token[] = found;
  let at = 0;
  const peek = (value: string) => list[at]?.kind === "op" && list[at]?.value === value;
  const take = (value: string) => {
    if (!peek(value)) return false;
    at += 1;
    return true;
  };
  const expect = (value: string) => {
    if (!take(value)) throw new SyntaxError(value);
  };

  function expression(): Expr {
    const condition = or();
    if (!take("?")) return condition;
    expression();
    expect(":");
    expression();
    return OTHER;
  }
  function or(): Expr {
    let a = and();
    while (take("||")) a = { kind: "or", a, b: and() };
    return a;
  }
  function and(): Expr {
    let a = relation();
    while (take("&&")) a = { kind: "and", a, b: relation() };
    return a;
  }
  function relation(): Expr {
    let a = additive();
    for (;;) {
      if (take("==")) a = { kind: "eq", a, b: additive() };
      else if (take("!=")) a = { kind: "ne", a, b: additive() };
      else if (list[at]?.kind === "ident" && list[at]?.value === "in") {
        at += 1;
        a = { kind: "in", a, b: additive() };
      } else if (take("<") || take("<=") || take(">") || take(">=")) {
        additive();
        a = OTHER;
      } else return a;
    }
  }
  function additive(): Expr {
    let a = unary();
    while (take("+") || take("-") || take("*") || take("/") || take("%")) {
      unary();
      a = OTHER;
    }
    return a;
  }
  function unary(): Expr {
    if (take("!")) return { kind: "not", a: unary() };
    if (take("-")) {
      unary();
      return OTHER;
    }
    return member();
  }
  function member(): Expr {
    let a = primary();
    for (;;) {
      if (take(".")) {
        const name = list[at];
        if (name?.kind !== "ident") throw new SyntaxError(".");
        at += 1;
        if (take("(")) {
          args(")");
          a = OTHER;
        } else a = { kind: "select", operand: a, field: name.value };
      } else if (take("[")) {
        expression();
        expect("]");
        a = OTHER;
      } else return a;
    }
  }
  function args(close: string): Expr[] {
    const items: Expr[] = [];
    if (take(close)) return items;
    do {
      if (peek(close)) break;
      items.push(expression());
      if (close === "}" && take(":")) expression();
    } while (take(","));
    expect(close);
    return items;
  }
  function primary(): Expr {
    const token = list[at];
    if (!token) throw new SyntaxError("end");
    at += 1;
    if (token.kind === "string") return { kind: "string", value: token.value };
    if (token.kind === "number") return OTHER;
    if (token.kind === "ident") {
      if (token.value === "true" || token.value === "false") {
        return { kind: "bool", value: token.value === "true" };
      }
      if (token.value === "null") return OTHER;
      if (take("(")) {
        args(")");
        return OTHER;
      }
      // A message's fields: `Type{field: value}`.
      if (take("{")) {
        args("}");
        return OTHER;
      }
      return { kind: "ident", name: token.value };
    }
    if (token.value === "(") {
      const inner = expression();
      expect(")");
      return inner;
    }
    if (token.value === "[") return { kind: "list", items: args("]") };
    if (token.value === "{") {
      args("}");
      return OTHER;
    }
    throw new SyntaxError(token.value);
  }

  try {
    const expr = expression();
    return at === list.length ? expr : null;
  } catch (error) {
    if (error instanceof SyntaxError) return null;
    throw error;
  }
}
