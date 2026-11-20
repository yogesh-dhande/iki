// How things connect: entities and runs as nodes, links as labelled arrows. Nodes go in
// columns (or rows, going down) by how far they are from where the graph starts, so an
// arrow always points onward: what something was made from, then what was made from it.
// Going right, each gap between columns fits its labels (graph-geometry.ts).
import { Node, type Layout, type Rect } from "@motion-canvas/2d";
import { Vector2 } from "@motion-canvas/core";
import { Connector } from "../components/Connector.tsx";
import { GraphNode } from "../components/GraphNode.tsx";
import type { Parts } from "../components/parts.ts";
import { SignatureMark } from "../components/SignatureMark.tsx";
import { Text } from "../components/Text.tsx";
import type { SceneOf } from "../script/schema.ts";
import { content, layoutSize, size, space } from "../theme/theme.ts";
import { depths, edgeGeometry, graphColumns, graphEdge } from "./graph-geometry.ts";

export function graphLayout(scene: SceneOf<"graph">, parts: Parts): Node {
  const { slots } = scene;
  const down = slots.direction === "down";
  const columns = down ? undefined : graphColumns(slots.nodes, slots.edges);
  const depth = columns?.depth ?? depths(slots.nodes, slots.edges);
  const levels = Math.max(...depth.values()) + 1;
  const nodeWidth = columns?.nodeWidth ?? size.node.width;
  const labelAt = new Map(
    columns ? edgeGeometry(columns, slots.edges).map((g) => [g.edge, g.labelAt]) : [],
  );
  const root = <Node />;
  const cards = new Map<string, Rect>();
  const signatures = new Map<string, Rect>();
  const top = slots.heading === undefined ? content.top : content.top + space.xl;
  const height = content.bottom - top;

  if (slots.heading !== undefined) {
    root.add(<Text ref={parts.ref("heading")} text={slots.heading} role="heading" ink="muted" />);
    const heading = parts.get("heading") as Layout;
    heading.position(() => new Vector2(content.left, content.top).add(heading.size().scale(0.5)));
  }

  for (const node of slots.nodes) {
    const card = (
      <GraphNode ref={parts.ref(`node:${node.id}`)} node={node} width={nodeWidth} />
    ) as Rect;
    cards.set(node.id, card);
    root.add(card);
    if (node.signature) {
      const mark = (
        <SignatureMark
          ref={parts.ref(`signature:${node.id}`)}
          signature={node.signature}
          width={nodeWidth}
        />
      ) as Rect;
      mark.offset([0, -1]);
      mark.position(() => card.bottom().addY(space.s));
      signatures.set(node.id, mark);
      root.add(mark);
    }
  }

  // Lay each level out: centred across (or down) the content area, in script order.
  for (let level = 0; level < levels; level++) {
    const members = slots.nodes.filter((node) => depth.get(node.id) === level);
    const extent = (id: string): number => {
      const card = cards.get(id);
      const mark = signatures.get(id);
      if (!card) return 0;
      if (down) return Math.max(card.width(), mark?.width() ?? 0);
      return card.height() + (mark ? space.s + mark.height() : 0);
    };
    members.forEach((node, index) => {
      const card = cards.get(node.id);
      if (!card) return;
      const along = (): number => {
        const sizes = members.map((member) => extent(member.id));
        const total =
          sizes.reduce((sum, value) => sum + value, 0) +
          layoutSize.graphRowGap * (members.length - 1);
        let offset = -total / 2;
        for (let i = 0; i < index; i++) offset += (sizes[i] ?? 0) + layoutSize.graphRowGap;
        return offset + (down ? card.width() : card.height()) / 2;
      };
      if (down) {
        card.y(top + ((level + 0.5) * height) / levels);
        card.x(along);
      } else {
        card.x(columns?.centers[level] ?? 0);
        card.y(() => (top + content.bottom) / 2 + along());
      }
    });
  }

  for (const edge of slots.edges) {
    const from = cards.get(edge.from);
    const to = cards.get(edge.to);
    if (!from || !to) continue;
    root.add(
      <Connector
        ref={parts.ref(`edge:${edge.from}>${edge.to}`)}
        from={() =>
          down ? from.bottom().addY(graphEdge.inset) : from.right().addX(graphEdge.inset)
        }
        to={() => (down ? to.top().addY(-graphEdge.inset) : to.left().addX(-graphEdge.inset))}
        label={edge.label}
        labelAt={labelAt.get(edge)}
      />,
    );
  }
  return root;
}
