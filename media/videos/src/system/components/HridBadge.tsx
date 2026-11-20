import { Rect } from "@motion-canvas/2d";
import { color, radius, space, stroke } from "../theme/theme.ts";
import { Text } from "./Text.tsx";

export interface HridBadgeProps {
  /** An HRID, such as ABY-000001, or a word for a record without one ("New"). */
  hrid: string;
  /** A draft: not registered, so no HRID yet. */
  draft?: boolean;
  ref?: (node: Rect) => void;
}

/** An HRID as the app shows it: the accent badge, round, on the sunken surface. */
export function HridBadge(props: HridBadgeProps): Rect {
  return (
    <Rect
      ref={props.ref}
      layout
      paddingLeft={space.s}
      paddingRight={space.s}
      paddingTop={space.xxs}
      paddingBottom={space.xxs}
      radius={radius.pill}
      fill={color.surfaceSunken}
      stroke={color.border}
      lineWidth={stroke.hairline}
    >
      <Text
        text={props.hrid}
        role="label"
        ink={props.draft ? "muted" : "accent"}
        mono={!props.draft}
      />
    </Rect>
  ) as Rect;
}
