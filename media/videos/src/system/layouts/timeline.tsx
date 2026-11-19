// A record's history: who it's about at the top, its revisions along an axis under it.
import { Layout, type Node } from "@motion-canvas/2d";
import { HridBadge } from "../components/HridBadge.tsx";
import type { Parts } from "../components/parts.ts";
import { Text } from "../components/Text.tsx";
import { Timeline } from "../components/Timeline.tsx";
import type { SceneOf } from "../script/schema.ts";
import { content, space } from "../theme/theme.ts";

export function timelineLayout(scene: SceneOf<"timeline">, parts: Parts): Node {
  const { slots } = scene;
  return (
    <Layout layout direction="column" gap={space.xl} width={content.width} y={content.centerY}>
      {slots.heading === undefined ? null : (
        <Text ref={parts.ref("heading")} text={slots.heading} role="heading" ink="muted" />
      )}
      <Layout ref={parts.ref("subject")} layout direction="row" alignItems="center" gap={space.s}>
        <Text text={slots.subject.type} role="label" ink="muted" />
        <HridBadge hrid={slots.subject.hrid} />
        <Text text={slots.subject.name} role="heading" />
      </Layout>
      <Timeline revisions={slots.revisions} parts={parts} />
    </Layout>
  );
}
