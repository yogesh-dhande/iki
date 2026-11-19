// Two things side by side, and how they relate: before and after, new and existing. An
// arrow between them can carry a label, and a notice under both can say what it means.
import { Layout, type Node } from "@motion-canvas/2d";
import { LinkArrow } from "../components/Connector.tsx";
import { Notice } from "../components/Notice.tsx";
import type { Parts } from "../components/parts.ts";
import { Text } from "../components/Text.tsx";
import type { Content, SceneOf } from "../script/schema.ts";
import { layoutSize, space } from "../theme/theme.ts";
import { contentNode } from "./content.tsx";
import { ContentFrame } from "./frame.tsx";

function panel(
  side: "left" | "right",
  label: string | undefined,
  value: Content,
  parts: Parts,
): Node {
  return (
    <Layout ref={parts.ref(side)} layout direction="column" gap={layoutSize.panelLabelGap}>
      {label === undefined ? null : <Text text={label} role="label" ink="muted" />}
      {contentNode(value, () => undefined, parts, `${side}.`, "narrow")}
    </Layout>
  );
}

export function splitLayout(scene: SceneOf<"split">, parts: Parts): Node {
  const { slots } = scene;
  return (
    <ContentFrame heading={slots.heading} parts={parts}>
      <Layout layout direction="column" alignItems="center" gap={space.l}>
        <Layout layout direction="row" alignItems="center">
          {panel("left", slots.left.label, slots.left.content, parts)}
          <LinkArrow ref={parts.ref("link")} width={layoutSize.link} label={slots.link} />
          {panel("right", slots.right.label, slots.right.content, parts)}
        </Layout>
        {slots.note === undefined ? null : <Notice ref={parts.ref("note")} notice={slots.note} />}
      </Layout>
    </ContentFrame>
  );
}
