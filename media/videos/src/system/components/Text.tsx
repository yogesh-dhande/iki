import { Txt, type TxtProps } from "@motion-canvas/2d";
import { font, ink, type Ink, type TypeRole, type as typeScale } from "../theme/theme.ts";

export interface TextProps {
  text: string;
  role?: TypeRole;
  ink?: Ink;
  mono?: boolean;
  /** Wrap at this width, from the theme's sizes. */
  width?: number;
  align?: "left" | "center";
  ref?: (node: Txt) => void;
  children?: TxtProps["children"];
}

/** Text in one of the theme's type roles and inks. */
export function Text(props: TextProps): Txt {
  const role = typeScale[props.role ?? "body"];
  return (
    <Txt
      ref={props.ref}
      text={props.text}
      fill={ink[props.ink ?? "text"]}
      fontFamily={props.mono ? font.mono : font.text}
      fontSize={role.size}
      fontWeight={role.weight}
      lineHeight={role.size * role.lineHeight}
      textWrap={props.width === undefined ? false : true}
      width={props.width}
      textAlign={props.align ?? "left"}
    />
  ) as Txt;
}

/** A value cut to its first `length` characters, with an ellipsis if it was longer. */
export function excerptOf(value: string, length: number): string {
  return value.length > length ? `${value.slice(0, length)}…` : value;
}
