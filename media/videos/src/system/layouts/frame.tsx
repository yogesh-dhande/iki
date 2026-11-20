import { Layout, type ComponentChildren } from "@motion-canvas/2d";
import type { Parts } from "../components/parts.ts";
import { Text } from "../components/Text.tsx";
import { content, space } from "../theme/theme.ts";

export interface ContentFrameProps {
  heading?: string | undefined;
  parts: Parts;
  children?: ComponentChildren;
}

/**
 * The content area every layout but the title and closing cards uses: the title-safe
 * area above the caption band, with an optional heading at its top left and the body
 * centred under it.
 */
export function ContentFrame(props: ContentFrameProps): Layout {
  return (
    <Layout
      layout
      direction="column"
      width={content.width}
      height={content.height}
      y={content.centerY}
      gap={space.l}
    >
      {props.heading === undefined ? null : (
        <Text ref={props.parts.ref("heading")} text={props.heading} role="heading" ink="muted" />
      )}
      <Layout layout grow={1} direction="row" justifyContent="center" alignItems="center">
        {props.children}
      </Layout>
    </Layout>
  ) as Layout;
}
