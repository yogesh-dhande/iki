import { Circle, Layout } from "@motion-canvas/2d";
import { color, size, space } from "../theme/theme.ts";
import { Text } from "./Text.tsx";

/**
 * Iki's mark, as the app's header draws it (a 32-unit ring, stroke 2.5, round a dot of
 * radius 5), in the accent, with the wordmark beside it.
 */
export function BrandMark(props: { ref?: ((node: Layout) => void) | undefined }): Layout {
  const unit = size.brand / 32;
  return (
    <Layout ref={props.ref} layout direction="row" alignItems="center" gap={space.s}>
      <Layout width={size.brand} height={size.brand}>
        <Circle layout={false} size={unit * 28} stroke={color.accent} lineWidth={unit * 2.5} />
        <Circle layout={false} size={unit * 10} fill={color.accent} />
      </Layout>
      <Text text="iki" role="title" />
    </Layout>
  ) as Layout;
}
