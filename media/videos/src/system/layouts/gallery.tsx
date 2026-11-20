// Several of the same kind of thing at once: copies, versions, examples. Up to three a row,
// two rows, each slightly turned, like papers on a desk.
import { Layout, type Node } from "@motion-canvas/2d";
import type { Parts } from "../components/parts.ts";
import type { SceneOf } from "../script/schema.ts";
import { layoutSize, space } from "../theme/theme.ts";
import { contentNode } from "./content.tsx";
import { ContentFrame } from "./frame.tsx";

const PER_ROW = 3;

export function galleryLayout(scene: SceneOf<"gallery">, parts: Parts): Node {
  const { items } = scene.slots;
  const rows: (typeof items)[] = [];
  const perRow = items.length === 4 ? 2 : PER_ROW;
  for (let start = 0; start < items.length; start += perRow)
    rows.push(items.slice(start, start + perRow));
  let index = 0;
  return (
    <ContentFrame heading={scene.slots.heading} parts={parts}>
      <Layout layout direction="column" gap={space.l} alignItems="center">
        {rows.map((row) => (
          <Layout layout direction="row" gap={space.m} alignItems="start">
            {row.map((item) => {
              const position = index++;
              return (
                <Layout
                  rotation={layoutSize.galleryTilt[position % layoutSize.galleryTilt.length] ?? 0}
                >
                  {contentNode(
                    item,
                    parts.ref(`item:${String(position + 1)}`),
                    parts,
                    `item:${String(position + 1)}.`,
                    "narrow",
                  )}
                </Layout>
              );
            })}
          </Layout>
        ))}
      </Layout>
    </ContentFrame>
  );
}
