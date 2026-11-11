/**
 * An entity's lineage: the query, and how its graph is laid out. Entities and
 * the runs that made them alternate in columns, earliest on the left: each
 * entity sits one column right of the run that made it, each run one column
 * right of the entities it was given.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { keepPreviousData, queryOptions } from "@tanstack/react-query";

import { iki } from "./api";
import { ENTITIES } from "./entities";

export type LineageGraph = Schemas["LineageGraphOut"];
export type LineageEntity = Schemas["LineageNodeOut"];
export type LineageRun = Schemas["LineageRunOut"];
export type LineageLink = Schemas["LineageGraphLinkOut"];

/** As the server allows: how many generations back or on, at most. */
export const MAX_GENERATIONS = 10;
/** How many generations each way a lineage shows at first, and each "Show" adds. */
export const GENERATIONS = 3;

export function lineageQuery(ref: string, up: number, down: number) {
  return queryOptions({
    queryKey: [...ENTITIES, "lineage", ref.toUpperCase(), up, down],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/entities/{ref}/lineage/graph", {
          params: { path: { ref }, query: { up, down } },
        }),
      ),
    placeholderData: keepPreviousData,
  });
}

/** The lineage page's address: ?up=6&down=3&view=list. */
export interface LineageSearch {
  up?: number | undefined;
  down?: number | undefined;
  view?: "list" | undefined;
}

function generations(value: unknown): number | undefined {
  const n = typeof value === "string" ? Number(value) : value;
  return typeof n === "number" && Number.isInteger(n) && n >= 0 && n <= MAX_GENERATIONS
    ? n
    : undefined;
}

export function validateLineageSearch(search: Record<string, unknown>): LineageSearch {
  const out: LineageSearch = {};
  const up = generations(search["up"]);
  const down = generations(search["down"]);
  if (up !== undefined && up !== GENERATIONS) out.up = up;
  if (down !== undefined && down !== GENERATIONS) out.down = down;
  if (search["view"] === "list") out.view = "list";
  return out;
}

// --- Layout ----------------------------------------------------------------------------------

/** Sizes, in rem, so the graph grows with the text. */
export const NODE_WIDTH = 13;
export const NODE_HEIGHT = 3.75;
const COLUMN_GAP = 3;
const ROW_GAP = 1;

export type Node =
  | { key: string; kind: "entity"; entity: LineageEntity; x: number; y: number }
  | { key: string; kind: "run"; run: LineageRun; x: number; y: number };

export interface Edge {
  from: string;
  to: string;
  /** An SVG path from the right of one node to the left of the other. */
  path: string;
}

export interface Layout {
  /** Column by column, top to bottom: the order they're read in. */
  nodes: Node[];
  edges: Edge[];
  width: number;
  height: number;
}

export const entityKey = (id: string) => `entity:${id}`;
export const runKey = (key: string) => `run:${key}`;

/**
 * Columns by the longest path to each node, so every link points right; then
 * within each column, nodes ordered by where their neighbours are, a few times
 * over, to keep lines from crossing.
 */
export function layout(graph: LineageGraph): Layout {
  const entities = new Map(graph.entities.map((e) => [entityKey(e.id), e]));
  const runs = new Map(graph.runs.map((r) => [runKey(r.key), r]));
  const order = [...entities.keys(), ...runs.keys()];
  const before = new Map<string, Set<string>>(order.map((k) => [k, new Set()]));
  const after = new Map<string, Set<string>>(order.map((k) => [k, new Set()]));
  const pairs: [string, string][] = [];
  const link = (from: string, to: string) => {
    const out = after.get(from);
    const into = before.get(to);
    if (!out || !into || out.has(to)) return;
    out.add(to);
    into.add(from);
    pairs.push([from, to]);
  };
  for (const l of graph.links) {
    link(entityKey(l.parent), runKey(l.run));
    link(runKey(l.run), entityKey(l.entity));
  }

  // Columns: each node one right of the furthest before it. Lineage can loop back on itself
  // (a run can reuse an entity), so when nothing's left without a node before it, the one
  // with the fewest goes next.
  const column = new Map<string, number>();
  const waiting = new Map(order.map((k) => [k, before.get(k)?.size ?? 0]));
  while (waiting.size) {
    let next: string | undefined;
    for (const [k, n] of waiting) {
      if (next === undefined || n < (waiting.get(next) ?? 0)) next = k;
      if (n === 0) break;
    }
    if (next === undefined) break;
    waiting.delete(next);
    let c = 0;
    for (const p of before.get(next) ?? []) {
      const placed = column.get(p);
      if (placed !== undefined) c = Math.max(c, placed + 1);
    }
    column.set(next, c);
    for (const n of after.get(next) ?? []) {
      const left = waiting.get(n);
      if (left !== undefined) waiting.set(n, left - 1);
    }
  }

  const columns: string[][] = Array.from(
    { length: Math.max(0, ...column.values()) + (order.length ? 1 : 0) },
    () => [],
  );
  for (const k of order) columns[column.get(k) ?? 0]?.push(k);

  // Order within columns by the average place of their neighbours: right to left, then back.
  const place = new Map<string, number>();
  const remember = () => {
    for (const c of columns) for (const [i, k] of c.entries()) place.set(k, i);
  };
  remember();
  const centre = (k: string, side: Map<string, Set<string>>) => {
    const near = [...(side.get(k) ?? [])].map((n) => place.get(n) ?? 0);
    return near.length ? near.reduce((a, b) => a + b, 0) / near.length : (place.get(k) ?? 0);
  };
  for (let sweep = 0; sweep < 4; sweep++) {
    const side = sweep % 2 ? after : before;
    const range = sweep % 2 ? [...columns.keys()].reverse() : [...columns.keys()];
    for (const i of range) {
      const c = columns[i] ?? [];
      const scored = c.map((k) => [k, centre(k, side)] as const);
      // Stable: ties keep their place.
      scored.sort((a, b) => a[1] - b[1]);
      columns[i] = scored.map(([k]) => k);
      remember();
    }
  }

  const tallest = Math.max(1, ...columns.map((c) => c.length));
  const height = tallest * NODE_HEIGHT + (tallest - 1) * ROW_GAP;
  const nodes: Node[] = [];
  const at = new Map<string, { x: number; y: number }>();
  for (const [i, c] of columns.entries()) {
    const x = i * (NODE_WIDTH + COLUMN_GAP);
    const top = (height - (c.length * NODE_HEIGHT + (c.length - 1) * ROW_GAP)) / 2;
    for (const [j, k] of c.entries()) {
      const y = top + j * (NODE_HEIGHT + ROW_GAP);
      at.set(k, { x, y });
      const entity = entities.get(k);
      const run = runs.get(k);
      if (entity) nodes.push({ key: k, kind: "entity", entity, x, y });
      else if (run) nodes.push({ key: k, kind: "run", run, x, y });
    }
  }
  const edges = pairs.map(([from, to]) => {
    const a = at.get(from) ?? { x: 0, y: 0 };
    const b = at.get(to) ?? { x: 0, y: 0 };
    const [x1, y1] = [a.x + NODE_WIDTH, a.y + NODE_HEIGHT / 2];
    const [x2, y2] = [b.x, b.y + NODE_HEIGHT / 2];
    // A loop back runs right to left: it bows out further so it doesn't hide behind nodes.
    const bend = Math.max(COLUMN_GAP / 2, Math.abs(x2 - x1) / 2);
    const path = `M ${fmt(x1)} ${fmt(y1)} C ${fmt(x1 + bend)} ${fmt(y1)}, ${fmt(x2 - bend)} ${fmt(y2)}, ${fmt(x2)} ${fmt(y2)}`;
    return { from, to, path };
  });
  const width = columns.length * NODE_WIDTH + Math.max(0, columns.length - 1) * COLUMN_GAP;
  return { nodes, edges, width, height };
}

function fmt(n: number): string {
  return String(Math.round(n * 100) / 100);
}

/** Everything a node leads from and to, itself included: what lights up when it's pointed at. */
export function connected(edges: Edge[], key: string): Set<string> {
  const found = new Set([key]);
  for (const forward of [true, false]) {
    const queue = [key];
    const seen = new Set([key]);
    while (queue.length) {
      const k = queue.pop();
      for (const e of edges) {
        const [from, to] = forward ? [e.from, e.to] : [e.to, e.from];
        if (from === k && !seen.has(to)) {
          seen.add(to);
          found.add(to);
          queue.push(to);
        }
      }
    }
  }
  return found;
}
