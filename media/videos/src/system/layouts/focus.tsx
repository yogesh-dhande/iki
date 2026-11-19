// One thing, centred, with its caption: the plainest layout.
import type { Node } from "@motion-canvas/2d";
import type { Parts } from "../components/parts.ts";
import type { SceneOf } from "../script/schema.ts";
import { contentNode } from "./content.tsx";
import { ContentFrame } from "./frame.tsx";

export function focusLayout(scene: SceneOf<"focus">, parts: Parts): Node {
  return (
    <ContentFrame heading={scene.slots.heading} parts={parts}>
      {contentNode(scene.slots.object, parts.ref("object"), parts)}
    </ContentFrame>
  );
}
