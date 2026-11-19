import { Rect, Txt } from "@motion-canvas/2d";
import type { NoticeData } from "../script/schema.ts";
import {
  font,
  ink,
  radius,
  size,
  space,
  stroke,
  tones,
  type as typeScale,
} from "../theme/theme.ts";
import { Text } from "./Text.tsx";

export interface NoticeProps {
  notice: NoticeData;
  width?: keyof typeof size.card | undefined;
  ref?: ((node: Rect) => void) | undefined;
}

/** A message as the app shows it: an alert in its tone, with the HRID it names as a link. */
export function Notice(props: NoticeProps): Rect {
  const { notice } = props;
  const tone = tones[notice.tone];
  const role = typeScale.body;
  const [before, after] =
    notice.link !== undefined && notice.text.includes(notice.link)
      ? [
          notice.text.slice(0, notice.text.indexOf(notice.link)),
          notice.text.slice(notice.text.indexOf(notice.link) + notice.link.length),
        ]
      : [notice.text, undefined];
  return (
    <Rect
      ref={props.ref}
      layout
      direction="column"
      gap={space.xs}
      width={size.card[props.width ?? "wide"]}
      padding={space.m}
      radius={radius.card}
      fill={tone.wash}
      stroke={tone.line}
      lineWidth={stroke.hairline}
    >
      {notice.title === undefined ? null : <Text text={notice.title} role="label" ink="text" />}
      <Txt
        fill={ink.text}
        fontFamily={font.text}
        fontSize={role.size}
        lineHeight={role.size * role.lineHeight}
        fontWeight={role.weight}
        textWrap
        width="100%"
      >
        {[
          <Txt text={before} />,
          ...(after === undefined
            ? []
            : [
                <Txt
                  text={notice.link ?? ""}
                  fill={ink.accent}
                  fontFamily={font.mono}
                  fontWeight={typeScale.label.weight}
                />,
                <Txt text={after} />,
              ]),
        ]}
      </Txt>
    </Rect>
  ) as Rect;
}
