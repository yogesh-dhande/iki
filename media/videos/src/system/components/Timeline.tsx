import { Circle, Layout, Line, Rect } from "@motion-canvas/2d";
import { createRef } from "@motion-canvas/core";
import type { RevisionData } from "../script/schema.ts";
import { color, content, size, space, stroke } from "../theme/theme.ts";
import type { Parts } from "./parts.ts";
import { RevisionChip } from "./RevisionChip.tsx";
import { SignatureMark } from "./SignatureMark.tsx";

export interface TimelineProps {
  revisions: RevisionData[];
  /** Files `axis`, `revision:<n>` and `signature:<n>`. */
  parts: Parts;
}

/**
 * Revisions oldest first along an axis: each a dot on the line, with its chip under it and
 * its signature, if it has one, under that. Nothing on it is ever replaced, only added to.
 */
export function Timeline(props: TimelineProps): Layout {
  const row = createRef<Layout>();
  return (
    <Layout
      ref={row}
      layout
      direction="row"
      alignItems="start"
      justifyContent="space-between"
      width={content.width}
    >
      <Line
        ref={props.parts.ref("axis")}
        layout={false}
        points={() => [
          [-row().width() / 2, -row().height() / 2 + size.dot / 2],
          [row().width() / 2, -row().height() / 2 + size.dot / 2],
        ]}
        stroke={color.border}
        lineWidth={stroke.rule}
        lineCap="round"
      />
      {props.revisions.map((revision) => (
        <Layout
          ref={props.parts.ref(`revision:${String(revision.number)}`)}
          layout
          direction="column"
          alignItems="center"
          gap={space.xs}
          width={size.chip.width}
        >
          <Circle
            size={size.dot}
            fill={color.surface}
            stroke={color.accent}
            lineWidth={stroke.rule}
          />
          <Rect width={stroke.rule} height={space.s} fill={color.border} />
          <RevisionChip revision={revision} />
          {revision.signature === undefined ? null : (
            <Layout
              ref={props.parts.ref(`signature:${String(revision.number)}`)}
              paddingTop={space.xs}
            >
              <SignatureMark signature={revision.signature} />
            </Layout>
          )}
        </Layout>
      ))}
    </Layout>
  ) as Layout;
}
