/**
 * Plugins' components (ADR 0084, ADR 0117): which show where, what fills each place, and
 * the messages their frames may send. A component's code runs only in a sandboxed frame;
 * the page checks everything it says, because the frame is the plugin's, not Iki's.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { queryOptions } from "@tanstack/react-query";

import { iki } from "./api";
import type { EntityType } from "./entities";

export type PluginComponent = Schemas["ComponentOut"];
export type ComponentSlot = PluginComponent["slots"][number];
export type SlotKind = ComponentSlot["slot"];
/** An entity as a component that shows many gets it: only the fields it reads. */
export type ComponentEntity = Schemas["ComponentEntityOut"];
export type Condition = Schemas["Condition"];
/** The grid's search, as a component's place gives it. */
export type PlaceSearch = Schemas["PlaceSearchIn"];

/** What fills a component's place: exactly one of these (ADR 0117). */
export type PlaceRequest =
  { entity: string } | { search: PlaceSearch } | { project: string } | { home: true };

/** Where a component is, as its element is told: its slot (or an app's panel), the
 * name it's shown under there, and, on an entity's tab, which of the actions it offers
 * the person viewing it may run there now (ADR 0170). */
export interface PlaceInfo {
  slot: SlotKind | "app.panel";
  label: string;
  runs?: string[] | undefined;
}

/** The installed components: they change only when the deployment does. */
export const componentsQuery = queryOptions({
  queryKey: ["components"],
  queryFn: async ({ signal }) => unwrap(await iki.GET("/api/v1/components", { signal: signal })),
  staleTime: 10 * 60_000,
});

/** A type and the types it extends, nearest first. */
export function lineageOf(name: string, types: EntityType[]): string[] {
  const out: string[] = [];
  let current: string | null | undefined = name;
  while (current && !out.includes(current)) {
    out.push(current);
    const at: string = current;
    current = types.find((t) => t.name === at)?.extends;
  }
  return out;
}

export interface EntityTab {
  component: PluginComponent;
  label: string;
  slot: ComponentSlot;
}

/** A tab for this type is on every record's page where its search finds something for
 * the record, unless the component has a tab for a nearer type (ADR 0237). */
export const EVERY_RECORD = "iki/core.Entity";

/** Whether the tab is on every record, shown only where its search finds something. */
export function onEveryRecord(tab: EntityTab): boolean {
  return tab.slot.type === EVERY_RECORD;
}

/** The component's tab on pages of entities of `type`: the one for the nearest type it
 * extends, or else its tab on every record, as the server picks it. */
export function tabFor(
  component: PluginComponent,
  type: string,
  types: EntityType[],
): ComponentSlot | undefined {
  const tabs = component.slots.filter((s) => s.slot === "entity.tab");
  for (const name of [...lineageOf(type, types), EVERY_RECORD]) {
    const found = tabs.find((s) => s.type === name);
    if (found) return found;
  }
  return undefined;
}

/** The tabs plugins add to the pages of entities of `type`, its parents' included. */
export function entityTabs(
  type: string,
  types: EntityType[],
  components: PluginComponent[],
): EntityTab[] {
  return components.flatMap((component) => {
    const slot = tabFor(component, type, types);
    return slot ? [{ component, label: slot.label, slot }] : [];
  });
}

export interface GridView {
  component: PluginComponent;
  label: string;
}

/** The components that can show a grid's results of `type`: those that show it, or a type
 * it extends. */
export function gridViews(
  type: string | undefined,
  types: EntityType[],
  components: PluginComponent[],
): GridView[] {
  if (!type) return [];
  const lineage = new Set(lineageOf(type, types));
  return components.flatMap((component) => {
    const slot = component.slots.find((s) => s.slot === "search.results");
    return slot && component.entities && lineage.has(component.entities.type)
      ? [{ component, label: slot.label }]
      : [];
  });
}

/** The most sections plugins' components add to a project's page or a person's home. */
export const MAX_SECTIONS = 8;

export interface Section {
  component: PluginComponent;
  label: string;
}

/** The sections plugins' components add to a project's page or a person's home. */
export function sectionsFor(
  kind: "project.page" | "person.home",
  components: PluginComponent[],
): Section[] {
  return components
    .flatMap((component) => {
      const slot = component.slots.find((s) => s.slot === kind);
      return slot && component.entities ? [{ component, label: slot.label }] : [];
    })
    .slice(0, MAX_SECTIONS);
}

/** The most a frame may grow to, and starts at. */
export const MAX_FRAME_HEIGHT = 20_000;
export const MIN_FRAME_HEIGHT = 160;
/** The most entities one selection may hold. */
export const MAX_SELECTED = 1000;
/** What a selection names entities by: their HRIDs, such as `PRT-000001`. */
export const HRID = /^[A-Za-z][A-Za-z0-9]{1,7}-[0-9]{1,20}$/;
/** The most a refinement may say, as the server allows it (ADR 0117). */
export const MAX_REFINE_CONDITIONS = 20;
export const MAX_REFINE_DEPTH = 4;
/** The most characters a refinement may have, as JSON. */
export const MAX_REFINE_SIZE = 10_000;
/** What a refinement can name besides the component's fields. */
export const REFINABLE_COLUMNS = ["hrid"];

/** What a component asks for: it can only narrow its place (ADR 0117). */
export interface Refinement {
  where?: Condition;
  sort?: { field: string; descending: boolean };
  q?: string;
}

export type FrameMessage =
  | { type: "ready" }
  | { type: "resize"; height: number }
  | { type: "select"; entities: string[] }
  | { type: "more" }
  | { type: "refine"; refinement: Refinement | null }
  | { type: "open"; entity: string; revision?: number }
  | { type: "run"; action: string };

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const RECORD_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export interface PinnedContext {
  values?: Record<string, unknown>;
  refs?: Record<string, { purged: boolean }>;
}

/** A stored revision a component asked to open (ADR 0189): a project record's, or its
 * registry entry's, by UUID. */
export interface PinnedRevision {
  of: "record" | "entry";
  id: string;
  revision: number;
}

/** The revision a component asked to open, if it's one a pin in the host's copy of the
 * entity names, of a reference the person can read: a record's `record_id` and
 * `record_revision`, or a registry pin's `entry_id` and `entry_revision`. Iki opens its page,
 * whose revision API checks the person's access to that exact record again. */
export function pinnedRevision(
  given: PinnedContext | undefined,
  id: string,
  revision: number,
): PinnedRevision | null {
  if (!RECORD_UUID.test(id) || !Number.isSafeInteger(revision) || revision < 1) return null;
  if (!given?.refs || !given.values) return null;
  for (const rows of Object.values(given.values)) {
    if (!Array.isArray(rows)) continue;
    for (const row of rows) {
      if (!isObject(row)) continue;
      const ref = row["entry_id"] ?? row["record_id"];
      if (typeof ref !== "string" || !RECORD_UUID.test(ref)) continue;
      const readable = given.refs[ref];
      if (!readable || readable.purged) continue;
      if (row["record_id"] === id && row["record_revision"] === revision) {
        return { of: "record", id, revision };
      }
      if (row["entry_id"] === id && row["entry_revision"] === revision) {
        return { of: "entry", id, revision };
      }
    }
  }
  return null;
}

/** A refinement's conditions, if each names a field the component reads and they're
 * within bounds; null otherwise. */
function readCondition(node: unknown, readable: ReadonlySet<string>): Condition | null {
  let count = 0;
  function walk(at: unknown, depth: number): Condition | null {
    if (!isObject(at) || depth > MAX_REFINE_DEPTH) return null;
    const keys = Object.keys(at);
    for (const group of ["all", "any"] as const) {
      if (group in at) {
        const children = at[group];
        if (keys.length !== 1 || !Array.isArray(children) || !children.length) return null;
        const read = children.map((c: unknown) => walk(c, depth + 1));
        return read.every((c) => c !== null) ? { [group]: read } : null;
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
    return { field, op: op as NonNullable<Condition["op"]>, value };
  }
  const read = walk(node, 1);
  return read && count <= MAX_REFINE_CONDITIONS ? read : null;
}

/** A refinement, if it's one the component may ask for: only its fields, within bounds.
 * `null` clears it; `undefined` is a refinement refused. The server checks it again. */
export function readRefinement(
  component: PluginComponent,
  value: unknown,
): Refinement | null | undefined {
  if (value === null || value === undefined) return null;
  if (!isObject(value) || !component.entities) return undefined;
  if (Object.keys(value).some((k) => !["where", "sort", "q"].includes(k))) return undefined;
  let size: number;
  try {
    size = JSON.stringify(value).length;
  } catch {
    return undefined;
  }
  if (size > MAX_REFINE_SIZE) return undefined;
  const readable = new Set([...component.entities.fields, ...REFINABLE_COLUMNS]);
  const out: Refinement = {};
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
    if (typeof q !== "string" || q.length > 200) return undefined;
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
export function readMessage(component: PluginComponent, data: unknown): FrameMessage | null {
  if (!isObject(data)) return null;
  const message = data;
  switch (message["type"]) {
    case "ready":
      return { type: "ready" };
    case "resize": {
      const height = message["height"];
      if (typeof height !== "number" || !Number.isFinite(height)) return null;
      const clamped = Math.round(Math.min(Math.max(height, MIN_FRAME_HEIGHT), MAX_FRAME_HEIGHT));
      return { type: "resize", height: clamped };
    }
    case "select": {
      const entities = message["entities"];
      if (!component.selects || !Array.isArray(entities)) return null;
      if (entities.length > MAX_SELECTED) return null;
      const hrids = entities.map(readHrid);
      if (hrids.some((h) => h === null)) return null;
      return { type: "select", entities: [...new Set(hrids as string[])] };
    }
    case "more":
      return component.entities ? { type: "more" } : null;
    case "refine": {
      const refinement = readRefinement(component, message["refinement"]);
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
      return typeof action === "string" && component.runs.includes(action)
        ? { type: "run", action }
        : null;
    }
    default:
      return null;
  }
}
