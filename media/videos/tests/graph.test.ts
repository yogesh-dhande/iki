// Graphs going right: every edge label fits its gap, clear of the nodes and of its own
// arrowhead, with line showing on each side; and the columns stay inside the frame.
import { describe, expect, it } from "vitest";
import { stringify } from "yaml";
import {
  edgeGeometry,
  graphColumns,
  graphEdge,
  type GraphColumns,
} from "../src/system/layouts/graph-geometry.ts";
import { loadScript } from "../src/system/script/load.ts";
import type { GraphEdgeData, GraphNodeData } from "../src/system/script/schema.ts";
import { textWidth } from "../src/system/theme/measure.ts";
import { content } from "../src/system/theme/theme.ts";
import { readVideo, videos } from "../tools/lib.ts";

/** What's wrong with a graph's geometry: nothing, when it's drawn right. */
function problems(nodes: GraphNodeData[], edges: GraphEdgeData[]): string[] {
  const columns: GraphColumns = graphColumns(nodes, edges);
  const { centers, nodeWidth } = columns;
  const found: string[] = [];
  const first = centers[0] ?? 0;
  const last = centers.at(-1) ?? 0;
  if (first - nodeWidth / 2 < content.left - 0.5 || last + nodeWidth / 2 > content.right + 0.5) {
    found.push("the columns leave the content area");
  }
  if (nodeWidth < graphEdge.minNode) found.push(`nodes are ${String(nodeWidth)} px wide`);
  for (const { edge, x0, x1, plate } of edgeGeometry(columns, edges)) {
    const name = `${edge.from}>${edge.to}`;
    const arrowhead = x1 - graphEdge.arrow;
    if (!plate) {
      if (arrowhead - x0 < graphEdge.stub) found.push(`${name}: its arrow is too short to see`);
      continue;
    }
    for (const center of centers) {
      const left = center - nodeWidth / 2;
      const right = center + nodeWidth / 2;
      if (plate.left < right && plate.right > left) found.push(`${name}: its label covers a node`);
    }
    if (plate.right > arrowhead) found.push(`${name}: its label covers its arrowhead`);
    if (plate.left - x0 < graphEdge.stub - 0.5 || arrowhead - plate.right < graphEdge.stub - 0.5) {
      found.push(`${name}: its label hides its line`);
    }
  }
  return found;
}

const node = (id: string, title = "Something"): GraphNodeData => ({
  id,
  kind: "Run",
  title,
  focus: false,
});

describe("a graph going right", () => {
  for (const video of videos()) {
    for (const scene of readVideo(video).scenes) {
      if (scene.layout !== "graph" || scene.slots.direction === "down") continue;
      it(`${video}/${scene.id}: its labels fit, clear of nodes and arrowheads`, () => {
        expect(problems(scene.slots.nodes, scene.slots.edges)).toEqual([]);
      });
    }
  }

  it("widens a gap for a long label, narrowing nodes to keep the graph in the frame", () => {
    const nodes = ["donor", "hold", "aliquot"].map((id) => node(id));
    const edges = [
      { from: "donor", to: "hold", label: "consent withdrawn" },
      { from: "hold", to: "aliquot", label: "put on hold" },
    ];
    const columns = graphColumns(nodes, edges);
    expect(columns.gaps[0]).toBeGreaterThan(textWidth("consent withdrawn", "label"));
    expect(columns.gaps[0]).toBeGreaterThan(columns.gaps[1] ?? 0);
    expect(problems(nodes, edges)).toEqual([]);
  });

  it("keeps arrows without labels long enough to see", () => {
    const nodes = ["a", "b", "c", "d"].map((id) => node(id));
    const edges = [
      { from: "a", to: "b" },
      { from: "b", to: "c" },
      { from: "c", to: "d" },
    ];
    expect(problems(nodes, edges)).toEqual([]);
  });

  it("puts a label that skips a column in the gap before its target", () => {
    const nodes = ["a", "b", "c"].map((id) => node(id));
    const edges = [
      { from: "a", to: "b", label: "input" },
      { from: "b", to: "c", label: "made" },
      { from: "a", to: "c", label: "lot of it" },
    ];
    expect(problems(nodes, edges)).toEqual([]);
  });

  it("refuses a graph whose labels can't fit across the frame", () => {
    const nodes = ["a", "b", "c", "d", "e"].map((id) => node(id));
    const label = "a label far too long to sit between two columns";
    const edges = ["a>b", "b>c", "c>d", "d>e"].map((pair) => {
      const [from = "", to = ""] = pair.split(">");
      return { from, to, label };
    });
    expect(graphColumns(nodes, edges).fits).toBe(false);
    const script = {
      id: "sample",
      title: "Sample",
      series: "Iki concepts",
      target: 5,
      publish: { description: "A graph.", tags: ["Iki"] },
      scenes: [
        {
          id: "graph",
          layout: "graph",
          chapter: "Graph",
          slots: { nodes, edges },
          beats: [{ id: "graph", caption: "Things link." }],
        },
      ],
    };
    expect(() => loadScript(stringify(script), "sample.yaml")).toThrow(/don't fit across/);
  });
});

describe("measuring text", () => {
  it("has every printable ASCII character, in both text faces", () => {
    for (const role of ["label", "body"] as const) {
      for (let code = 0x20; code < 0x7f; code++) {
        const character = String.fromCharCode(code);
        expect(textWidth(character, role), `${role} ${character}`).toBeGreaterThan(0);
        expect(textWidth(character, role)).toBeLessThan(textWidth("W", role) + 0.01);
      }
    }
  });

  it("adds characters up, and counts one it doesn't know as the widest", () => {
    expect(textWidth("ab", "label")).toBeCloseTo(textWidth("a", "label") + textWidth("b", "label"));
    expect(textWidth("\u{1F9EC}", "label")).toBeGreaterThanOrEqual(textWidth("W", "label"));
  });
});
