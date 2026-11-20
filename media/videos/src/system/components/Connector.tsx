import { Layout, Line, Rect } from "@motion-canvas/2d";
import type { PossibleVector2, SignalValue } from "@motion-canvas/core";
import { color, radius, space, stroke } from "../theme/theme.ts";
import { Text } from "./Text.tsx";

export interface ConnectorProps {
  /** Where it starts and ends, in its parent's space: signals follow nodes as they move. */
  from: SignalValue<PossibleVector2>;
  to: SignalValue<PossibleVector2>;
  label?: string | undefined;
  /** Where the label sits along the line, from 0 to 1. */
  labelAt?: number | undefined;
  /** On the line, on a plate (a graph's edges), or above it (a short link between panels). */
  labelPlacement?: "on" | "above" | undefined;
  /** The line itself, for the `draw` entrance. */
  lineRef?: ((node: Line) => void) | undefined;
  ref?: ((node: Layout) => void) | undefined;
}

/** An arrow between two things, with an optional label on a plate: a graph's edge, a link. */
export function Connector(props: ConnectorProps): Layout {
  const line = (
    <Line
      ref={props.lineRef}
      points={[props.from, props.to]}
      stroke={color.muted}
      lineWidth={stroke.connector}
      endArrow
      arrowSize={space.s}
      lineCap="round"
    />
  ) as Line;
  // Drawn where it's put, never laid out by a parent's flexbox.
  return (
    <Layout ref={props.ref} layout={false}>
      {line}
      {props.label === undefined ? null : (
        <Rect
          layout
          offset={props.labelPlacement === "above" ? [0, 1] : [0, 0]}
          position={() =>
            line
              .getPointAtPercentage(props.labelAt ?? 0.5)
              .position.addY(props.labelPlacement === "above" ? -space.xs : 0)
          }
          paddingLeft={space.xs}
          paddingRight={space.xs}
          paddingTop={space.xxs}
          paddingBottom={space.xxs}
          radius={radius.pill}
          fill={props.labelPlacement === "above" ? undefined : color.canvas}
        >
          <Text text={props.label} role="label" ink="muted" />
        </Rect>
      )}
    </Layout>
  ) as Layout;
}

/** A fixed-width gap in a row, with an arrow across it: a split layout's link. */
export function LinkArrow(props: {
  width: number;
  label?: string | undefined;
  /** Point right to left. */
  reverse?: boolean | undefined;
  ref?: ((node: Layout) => void) | undefined;
  lineRef?: ((node: Line) => void) | undefined;
}): Layout {
  const half = props.width / 2 - space.s;
  const [from, to] = props.reverse ? [half, -half] : [-half, half];
  return (
    <Layout ref={props.ref} width={props.width} height={space.xxl} shrink={0}>
      <Connector
        from={[from, 0]}
        to={[to, 0]}
        label={props.label}
        labelPlacement="above"
        lineRef={props.lineRef}
      />
    </Layout>
  ) as Layout;
}
