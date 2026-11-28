/**
 * How Iki's page reads what a component's frame sends it (ADR 0084, ADR 0286). The frame
 * is the plugin's, not Iki's, so the page trusts nothing it says: each message is read
 * here against what the component declared, and anything else is dropped. Iki's server
 * checks a refinement again. These are the validators Iki's own page runs, published so
 * that a component's tests can check what it sends the same way.
 */

import {
  HRID,
  MAX_CONDITIONS,
  MAX_DEPTH,
  MAX_FRAME_HEIGHT,
  MAX_QUERY_LENGTH,
  MAX_REFINEMENT_SIZE,
  MAX_SELECTED,
  MIN_FRAME_HEIGHT,
  PROTOCOL,
  RECORD_UUID,
  REFINABLE_COLUMNS,
  type Condition,
  type FrameMessage,
  type Operator,
  type Refinement,
} from "./index.js";

/** What a component declared, as `GET /components` lists it: whether it lets people pick
 * entities (`events.select`), the fields it reads of the many entities it shows
 * (`entities`), and the actions it may offer (`events.run`), qualified. */
export interface Declaration {
  selects: string | null;
  entities: { fields: readonly string[] } | null;
  runs: readonly string[];
}

/** A refinement as the page read it: within bounds, and its sort says which way. */
export interface ReadRefinement extends Refinement {
  sort?: { field: string; descending: boolean };
}

/** A frame's message as the page read it. */
export type ReadMessage =
  Exclude<FrameMessage, { type: "refine" }> | { type: "refine"; refinement: ReadRefinement | null };

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A refinement's conditions, if each names a field the component reads and they're
 * within bounds; null otherwise. */
function readCondition(node: unknown, readable: ReadonlySet<string>): Condition | null {
  let count = 0;
  function walk(at: unknown, depth: number): Condition | null {
    if (!isObject(at) || depth > MAX_DEPTH) return null;
    const keys = Object.keys(at);
    for (const group of ["all", "any"] as const) {
      if (group in at) {
        const children = at[group];
        if (keys.length !== 1 || !Array.isArray(children) || !children.length) return null;
        const read = children.map((c: unknown) => walk(c, depth + 1));
        if (!read.every((c) => c !== null)) return null;
        return group === "all" ? { all: read } : { any: read };
      }
    }
    if ("not" in at) {
      if (keys.length !== 1) return null;
      const inner = walk(at["not"], depth + 1);
      return inner ? { not: inner } : null;
    }
    count += 1;
    const { field, op, value } = at;
    if (keys.some((k) => !["field", "op", "value"].includes(k))) return null;
    if (typeof field !== "string" || !readable.has(field) || typeof op !== "string") return null;
    // The server knows the operators; the page checks only what the refinement may name.
    return { field, op: op as Operator, value };
  }
  const read = walk(node, 1);
  return read && count <= MAX_CONDITIONS ? read : null;
}

/** A refinement, if it's one the component may ask for: only its fields, within bounds.
 * `null` clears it; `undefined` is a refinement refused. The server checks it again. */
export function readRefinement(
  declared: Declaration,
  value: unknown,
): ReadRefinement | null | undefined {
  if (value === null || value === undefined) return null;
  if (!isObject(value) || !declared.entities) return undefined;
  if (Object.keys(value).some((k) => !["where", "sort", "q"].includes(k))) return undefined;
  let size: number;
  try {
    size = JSON.stringify(value).length;
  } catch {
    return undefined;
  }
  if (size > MAX_REFINEMENT_SIZE) return undefined;
  const readable = new Set([...declared.entities.fields, ...REFINABLE_COLUMNS]);
  const out: ReadRefinement = {};
  if (value["where"] !== undefined && value["where"] !== null) {
    const where = readCondition(value["where"], readable);
    if (!where) return undefined;
    out.where = where;
  }
  const sort = value["sort"];
  if (sort !== undefined && sort !== null) {
    if (!isObject(sort) || typeof sort["field"] !== "string" || !readable.has(sort["field"])) {
      return undefined;
    }
    const descending = sort["descending"];
    if (descending !== undefined && typeof descending !== "boolean") return undefined;
    out.sort = { field: sort["field"], descending: descending ?? false };
  }
  const q = value["q"];
  if (q !== undefined && q !== null) {
    if (typeof q !== "string" || q.length > MAX_QUERY_LENGTH) return undefined;
    if (q.trim()) out.q = q.trim();
  }
  return Object.keys(out).length ? out : null;
}

function readHrid(value: unknown): string | null {
  return typeof value === "string" && value.length <= 100 && HRID.test(value)
    ? value.toUpperCase()
    : null;
}

/** A frame's message, if it's one the component may send; null otherwise. */
export function readMessage(declared: Declaration, data: unknown): ReadMessage | null {
  if (!isObject(data)) return null;
  const message = data;
  switch (message["type"]) {
    case "ready":
      // A bridge that speaks another version of the protocol isn't one this page can use.
      return message["protocol"] === PROTOCOL ? { type: "ready", protocol: PROTOCOL } : null;
    case "resize": {
      const height = message["height"];
      if (typeof height !== "number" || !Number.isFinite(height)) return null;
      const clamped = Math.round(Math.min(Math.max(height, MIN_FRAME_HEIGHT), MAX_FRAME_HEIGHT));
      return { type: "resize", height: clamped };
    }
    case "select": {
      const entities = message["entities"];
      if (!declared.selects || !Array.isArray(entities)) return null;
      if (entities.length > MAX_SELECTED) return null;
      const hrids = entities.map(readHrid);
      if (hrids.some((h) => h === null)) return null;
      return { type: "select", entities: [...new Set(hrids as string[])] };
    }
    case "more":
      return declared.entities ? { type: "more" } : null;
    case "refine": {
      const refinement = readRefinement(declared, message["refinement"]);
      return refinement === undefined ? null : { type: "refine", refinement };
    }
    case "open": {
      if ("revision" in message) {
        const record = message["entity"];
        const revision = message["revision"];
        if (
          typeof record !== "string" ||
          !RECORD_UUID.test(record) ||
          typeof revision !== "number" ||
          !Number.isSafeInteger(revision) ||
          revision < 1
        )
          return null;
        return { type: "open", entity: record, revision };
      }
      const hrid = readHrid(message["entity"]);
      return hrid ? { type: "open", entity: hrid } : null;
    }
    case "run": {
      // Only an action it declared it offers: the page then checks the person may run it.
      const action = message["action"];
      return typeof action === "string" && declared.runs.includes(action)
        ? { type: "run", action }
        : null;
    }
    default:
      return null;
  }
}
