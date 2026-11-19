// The title and closing cards: the series' bookends. They have no burned-in captions,
// so they sit in the middle of the frame, not above the caption band.
import { Layout, type Node } from "@motion-canvas/2d";
import { BrandMark } from "../components/BrandMark.tsx";
import type { Parts } from "../components/parts.ts";
import { Text } from "../components/Text.tsx";
import type { SceneOf } from "../script/schema.ts";
import { layoutSize, space } from "../theme/theme.ts";

/** The title card: the mark, the series, the video's title and its promise. */
export function titleLayout(scene: SceneOf<"title">, parts: Parts): Node {
  const { slots } = scene;
  return (
    <Layout layout direction="column" alignItems="center" gap={space.l}>
      <BrandMark ref={parts.ref("brand")} />
      <Layout layout direction="column" alignItems="center" gap={space.s}>
        <Text ref={parts.ref("eyebrow")} text={slots.eyebrow} role="label" ink="accent" />
        <Text ref={parts.ref("title")} text={slots.title} role="display" align="center" />
        {slots.subtitle === undefined ? null : (
          <Text
            ref={parts.ref("subtitle")}
            text={slots.subtitle}
            role="heading"
            ink="muted"
            align="center"
          />
        )}
      </Layout>
    </Layout>
  );
}

/** The closing card: what to take away, and what's next in the series. */
export function closingLayout(scene: SceneOf<"closing">, parts: Parts): Node {
  const { slots } = scene;
  return (
    <Layout layout direction="column" alignItems="center" gap={space.xl}>
      <Text
        ref={parts.ref("statement")}
        text={slots.statement}
        role="title"
        align="center"
        width={layoutSize.statement}
      />
      {slots.next === undefined ? null : (
        <Text
          ref={parts.ref("next")}
          text={slots.next}
          role="heading"
          ink="accent"
          align="center"
        />
      )}
      <BrandMark ref={parts.ref("brand")} />
    </Layout>
  );
}
