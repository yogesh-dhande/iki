/**
 * An entity's lineage (ADR 0123): what it was derived from (upstream) and what was derived
 * from it (downstream), through references, runs and files. The walks' queries, the page's
 * address, the tree their paths make, and how the graph is laid out: entities and the runs
 * between them in columns, earliest on the left.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { infiniteQueryOptions, keepPreviousData, queryOptions } from "@tanstack/react-query";

import { iki } from "./api";
import { arrange, type Edge } from "./dag";
import { ENTITIES } from "./entities";

export type Walk = Schemas["WalkOut"];
export type WalkItem = Schemas["WalkItemOut"];
export type WalkStep = Schemas["WalkStepOut"];
export type WalkEntity = Schemas["GraphEntityOut"];
export type LineageGraph = Schemas["GraphOut"];
export type LineageEntity = Schemas["GraphNodeOut"];
export type LineageRun = Schemas["GraphRunOut"];
export type LineageEdge = Schemas["GraphEdgeOut"];

export type Direction = "upstream" | "downstream";
export type EdgeKind = "reference" | "run" | "file" | "aliquot" | "transfer";
export const EDGE_KINDS: readonly { value: EdgeKind; label: string; description: string }[] = [
  { value: "reference", label: "References", description: "A field names it." },
  { value: "run", label: "Runs", description: "A run made or changed it from it." },
  { value: "file", label: "Files", description: "A run made it from a file it held." },
  { value: "aliquot", label: "Aliquots", description: "It was split from it." },
  { value: "transfer", label: "Transfers", description: "Some of it was moved into it." },
];

/** As the server allows: how far a walk goes, at most, and when it isn't said. */
export const MAX_DEPTH = 30;
export const DEFAULT_DEPTH = 10;
/** How many steps each way the graph shows at first, each "Show" adds, and at most. */
export const GRAPH_STEPS = 3;
export const MAX_GRAPH_STEPS = 10;
const PAGE = 50;

/** What a walk is narrowed to. */
export interface WalkFilter {
  depth: number;
  edges: EdgeKind[];
  type: string;
}

async function walk(ref: string, direction: Direction, filter: WalkFilter, cursor?: string) {
  const query = {
    depth: filter.depth,
    limit: PAGE,
    ...(filter.edges.length ? { edges: filter.edges } : {}),
    ...(filter.type ? { type: [filter.type] } : {}),
    ...(cursor ? { cursor } : {}),
  };
  const params = { params: { path: { ref }, query } };
  return unwrap(
    direction === "upstream"
      ? await iki.GET("/api/v1/entities/{ref}/upstream", params)
      : await iki.GET("/api/v1/entities/{ref}/downstream", params),
  );
}

export function lineageWalkQuery(ref: string, direction: Direction, filter: WalkFilter) {
  return infiniteQueryOptions({
    queryKey: [...ENTITIES, "lineage", ref.toUpperCase(), direction, filter],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => walk(ref, direction, filter, pageParam),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    placeholderData: keepPreviousData,
  });
}

/** The address of the CSV of everything the walk finds. */
export function lineageCsvUrl(ref: string, direction: Direction, filter: WalkFilter): string {
  const query = new URLSearchParams({ depth: String(filter.depth) });
  for (const e of filter.edges) query.append("edges", e);
  if (filter.type) query.append("type", filter.type);
  return `/api/v1/entities/${encodeURIComponent(ref)}/${direction}.csv?${query.toString()}`;
}

export function lineageGraphQuery(ref: string, up: number, down: number, edges: EdgeKind[]) {
  return queryOptions({
    queryKey: [...ENTITIES, "lineage", ref.toUpperCase(), "graph", up, down, edges],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/entities/{ref}/graph", {
          params: { path: { ref }, query: { up, down, ...(edges.length ? { edges } : {}) } },
        }),
      ),
    placeholderData: keepPreviousData,
  });
}

// --- The address -----------------------------------------------------------------------------

export type LineageView = "tree" | "list" | "graph";

/** The lineage page's address: ?direction=downstream&view=list&depth=4&edges=run&type=… */
export interface LineageSearch {
  direction?: "downstream" | undefined;
  view?: "list" | "graph" | undefined;
  depth?: number | undefined;
  edges?: EdgeKind[] | undefined;
  type?: string | undefined;
  up?: number | undefined;
  down?: number | undefined;
}

function count(value: unknown, min: number, max: number): number | undefined {
  const n = typeof value === "string" ? Number(value) : value;
  return typeof n === "number" && Number.isInteger(n) && n >= min && n <= max ? n : undefined;
}

export function validateLineageSearch(search: Record<string, unknown>): LineageSearch {
  const out: LineageSearch = {};
  if (search["direction"] === "downstream") out.direction = "downstream";
  if (search["view"] === "list" || search["view"] === "graph") out.view = search["view"];
  const depth = count(search["depth"], 1, MAX_DEPTH);
  if (depth !== undefined && depth !== DEFAULT_DEPTH) out.depth = depth;
  const given = Array.isArray(search["edges"]) ? (search["edges"] as unknown[]) : [];
  const edges = EDGE_KINDS.map((k) => k.value).filter((k) => given.includes(k));
  if (edges.length && edges.length < EDGE_KINDS.length) out.edges = edges;
  const type = search["type"];
  if (typeof type === "string" && /^[\w/.:-]{1,200}$/.test(type)) out.type = type;
  const up = count(search["up"], 0, MAX_GRAPH_STEPS);
  const down = count(search["down"], 0, MAX_GRAPH_STEPS);
  if (up !== undefined && up !== GRAPH_STEPS) out.up = up;
  if (down !== undefined && down !== GRAPH_STEPS) out.down = down;
  return out;
}

// --- Why each is there -------------------------------------------------------------------------

/**
 * A step, in words: what depends on what, and how. ``prev`` is where the step starts,
 * nearer the root: downstream, the step's entity depends on it; upstream, it depends on
 * the step's entity. "ALQ-000002’s sample is SAM-000001"; "RUN-000004 made PRT-000003
 * from PRT-000001 (its protein, at revision 2)".
 */
export function stepWords(step: WalkStep, prev: string, direction: Direction): string {
  const [dependent, dependency] =
    direction === "downstream" ? [step.entity.hrid, prev] : [prev, step.entity.hrid];
  const field = step.field.replaceAll("_", " ").replace(".", "’s ");
  const run = step.run ? step.run.hrid : "A run you can’t see";
  const at = step.revision ? `, at revision ${String(step.revision)}` : "";
  switch (step.kind) {
    case "reference":
      return `${dependent}’s ${field} is ${dependency}`;
    case "run":
      return `${run} made ${dependent} from ${dependency} (its ${field}${at})`;
    case "file": {
      const file = step.file ? step.file.name : "a file you can’t see";
      return `${run} made ${dependent} from ${file} of ${dependency} (its ${field})`;
    }
    case "aliquot":
      return `${dependent} was split from ${dependency}`;
    case "transfer":
      return `Some of ${dependency} was moved into ${dependent}`;
    case "record":
      return `${step.entity.hrid} is the same entity, in ${step.entity.project?.name ?? "another project"}`;
  }
}

// --- The tree ------------------------------------------------------------------------------------

export interface TreeNode {
  item: WalkItem;
  /** The step that reached it from its parent. */
  step: WalkStep | undefined;
  children: TreeNode[];
}

/**
 * The walk's results as a tree: each under the one its path reached it from, so the tree
 * reads as the walk went. One whose path runs through something not shown (a step to
 * another project's record of the root, say) goes under the nearest shown, or at the top.
 */
export function walkTree(items: readonly WalkItem[]): TreeNode[] {
  const nodes = new Map<string, TreeNode>();
  for (const item of items) {
    nodes.set(item.entity.id, { item, step: item.path.at(-1), children: [] });
  }
  const top: TreeNode[] = [];
  for (const item of items) {
    const node = nodes.get(item.entity.id);
    if (!node) continue;
    const parent = item.path
      .slice(0, -1)
      .reverse()
      .map((s) => nodes.get(s.entity.id))
      .find((n) => n !== undefined && n !== node);
    (parent ? parent.children : top).push(node);
  }
  return top;
}

// --- The graph's layout ----------------------------------------------------------------------

export { connected, NODE_HEIGHT, NODE_WIDTH, type Edge } from "./dag";

export type Node =
  | { key: string; kind: "entity"; entity: LineageEntity; x: number; y: number }
  | { key: string; kind: "run"; run: LineageRun; x: number; y: number };

export interface Layout {
  /** Column by column, top to bottom: the order they're read in. */
  nodes: Node[];
  edges: Edge[];
  width: number;
  height: number;
}

export const entityKey = (id: string) => `entity:${id}`;
export const runKey = (key: string) => `run:${key}`;

/** Entities and runs in columns (:func:`arrange`): what something depends on left of it,
 * and a run between what it read and what it made. */
export function layout(graph: LineageGraph): Layout {
  const entities = new Map(graph.entities.map((e) => [entityKey(e.id), e]));
  const runs = new Map(graph.runs.map((r) => [runKey(r.key), r]));
  const order = [...entities.keys(), ...runs.keys()];
  const links = graph.edges.flatMap((l): (readonly [string, string])[] =>
    l.run
      ? [
          [entityKey(l.on), runKey(l.run)],
          [runKey(l.run), entityKey(l.node)],
        ]
      : [[entityKey(l.on), entityKey(l.node)]],
  );
  const { places, edges, width, height } = arrange(order, links);
  const nodes: Node[] = [];
  for (const { key, x, y } of places) {
    const entity = entities.get(key);
    const run = runs.get(key);
    if (entity) nodes.push({ key, kind: "entity", entity, x, y });
    else if (run) nodes.push({ key, kind: "run", run, x, y });
  }
  return { nodes, edges, width, height };
}
