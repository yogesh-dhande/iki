import { Circle, Layout, Line, Rect } from "@motion-canvas/2d";
import type { SignatureData } from "../script/schema.ts";
import { color, radius, size, space, stroke, tones } from "../theme/theme.ts";
import { Text } from "./Text.tsx";

export interface SignatureMarkProps {
  signature: SignatureData;
  /** Wrap to this width, from the theme's sizes: under a graph node, the node's. */
  width?: number | undefined;
  ref?: ((node: Rect) => void) | undefined;
}

/** A tick in a circle: the mark of a signature that checks out. */
function SealGlyph(): Layout {
  const s = size.icon;
  return (
    <Layout width={s} height={s}>
      <Circle layout={false} size={s} fill={tones.success.line} />
      <Line
        layout={false}
        points={[
          [-s * 0.22, 0],
          [-s * 0.05, s * 0.17],
          [s * 0.24, -s * 0.15],
        ]}
        stroke={color.onAccent}
        lineWidth={stroke.focus}
        lineCap="round"
        lineJoin="round"
      />
    </Layout>
  ) as Layout;
}

/**
 * An electronic signature as the record's page shows it: what it means, who signed and
 * when ("Approved by … on … UTC. Meaning: QA release.").
 */
export function SignatureMark(props: SignatureMarkProps): Rect {
  const { signature } = props;
  const textWidth =
    props.width === undefined ? undefined : props.width - size.icon - space.s * 2 - space.m;
  return (
    <Rect
      ref={props.ref}
      width={props.width}
      layout
      direction="row"
      alignItems="center"
      gap={space.s}
      padding={space.s}
      paddingRight={space.m}
      radius={radius.card}
      fill={tones.success.wash}
      stroke={tones.success.line}
      lineWidth={stroke.hairline}
    >
      <SealGlyph />
      <Layout layout direction="column" gap={space.xxs} grow={1} shrink={1}>
        <Text
          text={`${signature.verb} by ${signature.by}`}
          role="label"
          ink="success"
          width={textWidth}
        />
        <Text text={signature.when} role="small" ink="muted" width={textWidth} />
        <Text text={`Meaning: ${signature.meaning}.`} role="small" ink="text" width={textWidth} />
      </Layout>
    </Rect>
  ) as Rect;
}
