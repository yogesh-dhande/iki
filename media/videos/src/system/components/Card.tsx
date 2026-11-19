import { Rect, type RectProps } from "@motion-canvas/2d";
import { color, radius, shadow, space, stroke, tones, type Tone } from "../theme/theme.ts";

export interface CardProps {
  /** Width, from the theme's sizes. */
  width?: number;
  /** A sunken panel rather than a raised card. */
  sunken?: boolean;
  /** A draft: drawn with a dashed edge. */
  draft?: boolean;
  /** An edge in a tone, for the thing a scene is about. */
  tone?: Tone;
  padding?: keyof typeof space;
  gap?: keyof typeof space;
  ref?: (node: Rect) => void;
  children?: RectProps["children"];
}

/** The app's card: the surface, a hairline border, its radius and shadow. */
export function Card(props: CardProps): Rect {
  const edge = props.tone && props.tone !== "neutral" ? tones[props.tone].line : color.border;
  return (
    <Rect
      ref={props.ref}
      layout
      direction="column"
      width={props.width}
      padding={space[props.padding ?? "l"]}
      gap={space[props.gap ?? "s"]}
      radius={radius.card}
      fill={props.sunken ? color.surfaceSunken : color.surface}
      stroke={edge}
      lineWidth={props.tone && props.tone !== "neutral" ? stroke.rule : stroke.hairline}
      lineDash={props.draft ? [stroke.rule * 4, stroke.rule * 3] : []}
      shadowColor={props.sunken ? undefined : shadow.card.color}
      shadowBlur={props.sunken ? 0 : shadow.card.blur}
      shadowOffsetY={props.sunken ? 0 : shadow.card.offsetY}
    >
      {props.children}
    </Rect>
  ) as Rect;
}
