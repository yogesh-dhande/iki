// A record, and beside it the thing that explains it: the plugin that defines its type.
import { Layout, type Node } from "@motion-canvas/2d";
import { LinkArrow } from "../components/Connector.tsx";
import type { Parts } from "../components/parts.ts";
import { PluginInset } from "../components/PluginInset.tsx";
import { RecordCard } from "../components/RecordCard.tsx";
import type { SceneOf } from "../script/schema.ts";
import { layoutSize } from "../theme/theme.ts";
import { ContentFrame } from "./frame.tsx";

export function recordInsetLayout(scene: SceneOf<"record-inset">, parts: Parts): Node {
  const { slots } = scene;
  return (
    <ContentFrame parts={parts}>
      <Layout layout direction="row" alignItems="center">
        <RecordCard ref={parts.ref("record")} record={slots.record} parts={parts} />
        <LinkArrow ref={parts.ref("link")} width={layoutSize.link} label={slots.link} reverse />
        <PluginInset ref={parts.ref("inset")} plugin={slots.inset} />
      </Layout>
    </ContentFrame>
  );
}
