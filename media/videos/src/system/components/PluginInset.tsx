import { Circle, Layout, Rect } from "@motion-canvas/2d";
import type { PluginData } from "../script/schema.ts";
import { color, radius, size, space, stroke } from "../theme/theme.ts";
import { Card } from "./Card.tsx";
import { Text } from "./Text.tsx";

export interface PluginInsetProps {
  plugin: PluginData;
  ref?: ((node: Rect) => void) | undefined;
}

/** A plug: the glyph for a plugin, drawn from the theme's strokes. */
function PlugGlyph(): Layout {
  const s = size.icon;
  return (
    <Layout width={s} height={s}>
      <Rect
        layout={false}
        width={s * 0.6}
        height={s * 0.42}
        y={s * 0.08}
        radius={radius.small / 2}
        fill={color.accent}
      />
      <Rect
        layout={false}
        width={stroke.focus}
        height={s * 0.26}
        x={-s * 0.14}
        y={-s * 0.3}
        fill={color.accent}
      />
      <Rect
        layout={false}
        width={stroke.focus}
        height={s * 0.26}
        x={s * 0.14}
        y={-s * 0.3}
        fill={color.accent}
      />
      <Circle layout={false} size={stroke.focus * 2} y={s * 0.08} fill={color.onAccent} />
    </Layout>
  ) as Layout;
}

/** A plugin, as the thing that defines a type: its name and the fields it gives the type. */
export function PluginInset(props: PluginInsetProps): Rect {
  const { plugin } = props;
  return (
    <Card ref={props.ref} width={size.inset.width} sunken gap="s" padding="m">
      <Layout layout direction="row" alignItems="center" gap={space.s}>
        <PlugGlyph />
        <Layout layout direction="column">
          <Text text="Plugin" role="label" ink="muted" />
          <Text text={plugin.name} role="code" mono ink="accent" />
        </Layout>
      </Layout>
      <Text text={plugin.provides} role="body" />
      <Layout layout direction="column" gap={space.xxs}>
        {plugin.fields.map((field) => (
          <Layout layout direction="row" justifyContent="space-between" gap={space.s}>
            <Text text={field.name} role="small" mono />
            <Text text={field.type} role="small" ink="muted" />
          </Layout>
        ))}
      </Layout>
    </Card>
  ) as Rect;
}
