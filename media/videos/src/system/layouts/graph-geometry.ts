// Where a graph's columns go, worked out before anything is drawn, so a scene's rules and
// tests can check it too. Each gap between columns is wide enough for the longest label
// in it, with a stretch of line showing on each side of the label and the arrowhead clear
// of it. Columns spread across the content area as evenly as those gaps allow; when they
// don't fit, nodes narrow (their titles wrap) down to a minimum, and past that the script
// is refused.
import type { GraphEdgeData, GraphNodeData } from "../script/schema.ts";
import { textWidth } from "../theme/measure.ts";
import { content, size, space } from "../theme/theme.ts";

/** The fixed parts of a graph's edges, in pixels. */
export const graphEdge = {
  /** The gap between a node and the end of a line. */
  inset: space.xs,
  /** The arrowhead's length (the Connector's arrowSize). */
  arrow: space.s,
  /** The least line that shows on each side of a label. */
  stub: space.s,
  /** A label's plate: its padding on each side. */
  platePadding: space.xs,
  /** The narrowest gap between columns. */
  minGap: space.xxl,
  /** The narrowest a node gets before the graph is refused. */
  minNode: 320,
} as const;

/** Each node's depth: the longest path to it from a node nothing points to. */
export function depths(nodes: GraphNodeData[], edges: GraphEdgeData[]): Map<string, number> {
  const depth = new Map(nodes.map((node) => [node.id, 0]));
  // Longest paths in a DAG: relax every edge until nothing changes (at most once a node).
  let changed = true;
  while (changed) {
    changed = false;
    for (const edge of edges) {
      const next = (depth.get(edge.from) ?? 0) + 1;
      if (next > (depth.get(edge.to) ?? 0) && next < nodes.length) {
        depth.set(edge.to, next);
        changed = true;
      }
    }
  }
  return depth;
}

/** A label's plate width. */
export function plateWidth(label: string): number {
  return textWidth(label, "label") + graphEdge.platePadding * 2;
}

/** The gap a label needs: its plate, a stub of line each side, the arrowhead, the insets. */
export function gapFor(label: string | undefined): number {
  const plate = label === undefined ? 0 : plateWidth(label);
  const needed = plate + 2 * (graphEdge.stub + graphEdge.inset) + graphEdge.arrow;
  return Math.max(graphEdge.minGap, needed);
}

export interface GraphColumns {
  levels: number;
  depth: Map<string, number>;
  nodeWidth: number;
  /** Each column's centre x. */
  centers: number[];
  /** Each gap's width: gaps[i] lies between column i and column i + 1. */
  gaps: number[];
  /** Whether it fits the content area with nodes at least `graphEdge.minNode` wide. */
  fits: boolean;
}

/** The gap an edge's label sits in: the one just before its target's column. */
export function gapOf(edge: GraphEdgeData, depth: Map<string, number>): number {
  return (depth.get(edge.to) ?? 0) - 1;
}

/** A left-to-right graph's columns. */
export function graphColumns(nodes: GraphNodeData[], edges: GraphEdgeData[]): GraphColumns {
  const depth = depths(nodes, edges);
  const levels = Math.max(...depth.values()) + 1;
  const needs: number[] = Array.from({ length: levels - 1 }, () => graphEdge.minGap);
  for (const edge of edges) {
    const gap = gapOf(edge, depth);
    if (gap >= 0 && gap < needs.length) {
      needs[gap] = Math.max(needs[gap] ?? 0, gapFor(edge.label));
    }
  }
  const sum = (values: number[]): number => values.reduce((total, value) => total + value, 0);
  // With nodes at full width, every gap is at least `even` (and its need), with half an
  // `even` gap at each side: evenly spread columns, as wide as the labels allow.
  const width = (even: number, node: number): number =>
    levels * node + sum(needs.map((need) => Math.max(need, even))) + even;
  let nodeWidth: number = size.node.width;
  let even: number = graphEdge.minGap;
  if (width(even, nodeWidth) <= content.width) {
    let low = even;
    let high = content.width;
    for (let i = 0; i < 60; i++) {
      const middle = (low + high) / 2;
      if (width(middle, nodeWidth) <= content.width) low = middle;
      else high = middle;
    }
    even = low;
  } else {
    // Too wide: no margin at the sides (the content area has its own), then narrower nodes.
    even = 0;
    nodeWidth = Math.min(size.node.width, (content.width - sum(needs)) / levels);
  }
  const fits = nodeWidth >= graphEdge.minNode;
  nodeWidth = Math.max(nodeWidth, graphEdge.minNode);
  const gaps = needs.map((need) => Math.max(need, even));
  const total = levels * nodeWidth + sum(gaps);
  const centers: number[] = [];
  let x = (content.left + content.right) / 2 - total / 2 + nodeWidth / 2;
  for (let level = 0; level < levels; level++) {
    centers.push(x);
    x += nodeWidth + (gaps[level] ?? 0);
  }
  return { levels, depth, nodeWidth, centers, gaps, fits };
}

export interface EdgeGeometry {
  edge: GraphEdgeData;
  /** The line's ends, in x. */
  x0: number;
  x1: number;
  /** Where along the line its label's centre is, from 0 to 1. */
  labelAt: number;
  /** The label plate's left and right, in x, if it has a label. */
  plate?: { left: number; right: number };
}

/**
 * Each edge's line and label, in x. The label sits in the gap before its target, half an
 * arrowhead left of the gap's centre, so the same stub of line shows on each side of it.
 */
export function edgeGeometry(columns: GraphColumns, edges: GraphEdgeData[]): EdgeGeometry[] {
  const { depth, centers, nodeWidth, gaps } = columns;
  return edges.map((edge) => {
    const from = depth.get(edge.from) ?? 0;
    const to = depth.get(edge.to) ?? 0;
    const x0 = (centers[from] ?? 0) + nodeWidth / 2 + graphEdge.inset;
    const x1 = (centers[to] ?? 0) - nodeWidth / 2 - graphEdge.inset;
    const gap = gapOf(edge, depth);
    const center = (centers[to] ?? 0) - nodeWidth / 2 - (gaps[gap] ?? 0) / 2 - graphEdge.arrow / 2;
    const labelAt = x1 === x0 ? 0.5 : (center - x0) / (x1 - x0);
    if (edge.label === undefined) return { edge, x0, x1, labelAt };
    const half = plateWidth(edge.label) / 2;
    return { edge, x0, x1, labelAt, plate: { left: center - half, right: center + half } };
  });
}
