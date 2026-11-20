import { Layout, Rect } from "@motion-canvas/2d";
import type { GraphNodeData } from "../script/schema.ts";
import { size, space } from "../theme/theme.ts";
import { Card } from "./Card.tsx";
import { HridBadge } from "./HridBadge.tsx";
import { Text } from "./Text.tsx";

export interface GraphNodeProps {
  node: GraphNodeData;
  /** Its width: the theme's, unless a graph narrows its nodes to fit. */
  width?: number | undefined;
  ref?: ((node: Rect) => void) | undefined;
}

/** An entity or run in a graph, compact: its kind, its title and its HRID. */
export function GraphNode(props: GraphNodeProps): Rect {
  const { node } = props;
  const width = props.width ?? size.node.width;
  return (
    <Card
      ref={props.ref}
      width={width}
      padding="m"
      gap="xs"
      tone={node.focus ? "accent" : "neutral"}
    >
      <Text text={node.kind} role="label" ink="muted" />
      <Text text={node.title} role="body" width={width - space.m * 2} />
      {node.hrid === undefined ? null : (
        <Layout layout direction="row" paddingTop={space.xxs}>
          <HridBadge hrid={node.hrid} />
        </Layout>
      )}
    </Card>
  ) as Rect;
}
