/**
 * An entity's lineage: the query, and how its graph is laid out. Entities and
 * the runs that made them alternate in columns, earliest on the left: each
 * entity sits one column right of the run that made it, each run one column
 * right of the entities it was given.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { keepPreviousData, queryOptions } from "@tanstack/react-query";

import { iki } from "./api";
import { arrange, type Edge } from "./dag";
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

/** Entities and runs in columns (:func:`arrange`): each entity right of the run that made it,
 * each run right of what it was given. */
export function layout(graph: LineageGraph): Layout {
  const entities = new Map(graph.entities.map((e) => [entityKey(e.id), e]));
  const runs = new Map(graph.runs.map((r) => [runKey(r.key), r]));
  const order = [...entities.keys(), ...runs.keys()];
  const links = graph.links.flatMap(
    (l) =>
      [
        [entityKey(l.parent), runKey(l.run)],
        [runKey(l.run), entityKey(l.entity)],
      ] as const,
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
