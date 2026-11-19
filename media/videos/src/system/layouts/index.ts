// Every layout, by the name scripts use. A scene picks one and fills its slots; the layout
// decides where things go, inside the safe area and above the caption band.
import type { Node } from "@motion-canvas/2d";
import type { Parts } from "../components/parts.ts";
import type { LayoutName, SceneOf } from "../script/schema.ts";
import { closingLayout, titleLayout } from "./cards.tsx";
import { focusLayout } from "./focus.tsx";
import { galleryLayout } from "./gallery.tsx";
import { graphLayout } from "./graph.tsx";
import { recordInsetLayout } from "./record-inset.tsx";
import { splitLayout } from "./split.tsx";
import { timelineLayout } from "./timeline.tsx";

export type LayoutBuilder<L extends LayoutName> = (scene: SceneOf<L>, parts: Parts) => Node;

export const layouts: { [L in LayoutName]: LayoutBuilder<L> } = {
  title: titleLayout,
  closing: closingLayout,
  focus: focusLayout,
  gallery: galleryLayout,
  split: splitLayout,
  "record-inset": recordInsetLayout,
  timeline: timelineLayout,
  graph: graphLayout,
};

export function buildLayout<L extends LayoutName>(scene: SceneOf<L>, parts: Parts): Node {
  return (layouts[scene.layout] as LayoutBuilder<L>)(scene, parts);
}
