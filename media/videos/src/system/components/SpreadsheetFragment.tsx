import { Layout, Rect } from "@motion-canvas/2d";
import type { FragmentData } from "../script/schema.ts";
import { color, excerpt, radius, shadow, size, space, stroke, tones } from "../theme/theme.ts";
import { Text, excerptOf } from "./Text.tsx";

export interface SpreadsheetFragmentProps {
  fragment: FragmentData;
  ref?: ((node: Rect) => void) | undefined;
}

function isHighlighted(fragment: FragmentData, row: number, column: number): boolean {
  return fragment.highlight.some(([r, c]) => r === row && c === column);
}

function cellText(value: string): string {
  // Long values are sequences: cut them as a narrow cell would.
  return /^[A-Za-z*\s]{20,}$/.test(value)
    ? excerptOf(value.replace(/\s+/g, ""), excerpt.cell)
    : value;
}

/**
 * A file the data lives in outside Iki, cut down to a few rows: a spreadsheet's grid with
 * its header, or a notebook page's lines. Highlighted cells disagree with the other copies.
 */
export function SpreadsheetFragment(props: SpreadsheetFragmentProps): Rect {
  const { fragment } = props;
  const columns = Math.max(1, fragment.columns.length, ...fragment.rows.map((row) => row.length));
  const columnWidth = (size.fragment.width - space.s * 2) / columns;
  const notebook = fragment.kind === "notebook";
  return (
    <Rect
      ref={props.ref}
      layout
      direction="column"
      width={size.fragment.width}
      radius={radius.small}
      fill={color.surface}
      stroke={color.border}
      lineWidth={stroke.hairline}
      shadowColor={shadow.card.color}
      shadowBlur={shadow.card.blur}
      shadowOffsetY={shadow.card.offsetY}
      clip
    >
      <Rect
        layout
        direction="row"
        alignItems="center"
        gap={space.xs}
        padding={space.xs}
        paddingLeft={space.s}
        fill={color.surfaceSunken}
      >
        <Rect
          width={space.s}
          height={space.s}
          radius={radius.small / 2}
          fill={notebook ? tones.warning.line : tones.success.line}
        />
        <Text text={fragment.file} role="label" ink="muted" mono />
      </Rect>
      <Layout
        layout
        direction="column"
        padding={space.s}
        paddingTop={space.xs}
        gap={notebook ? space.xs : 0}
      >
        {fragment.columns.length > 0 && !notebook ? (
          <Layout layout direction="row">
            {fragment.columns.map((column) => (
              <Layout width={columnWidth} paddingTop={space.xxs} paddingBottom={space.xxs}>
                <Text text={column} role="label" ink="muted" />
              </Layout>
            ))}
          </Layout>
        ) : undefined}
        {fragment.rows.map((row, r) => (
          <Layout layout direction="row">
            {row.map((value, c) => (
              <Rect
                layout
                width={notebook ? "100%" : columnWidth}
                paddingTop={space.xxs}
                paddingBottom={space.xxs}
                paddingLeft={space.xxs}
                radius={radius.small / 2}
                fill={isHighlighted(fragment, r, c) ? tones.warning.wash : undefined}
                stroke={isHighlighted(fragment, r, c) ? tones.warning.line : undefined}
                lineWidth={stroke.hairline}
              >
                <Text
                  text={notebook ? value : cellText(value)}
                  role="small"
                  ink={isHighlighted(fragment, r, c) ? "warning" : "text"}
                  mono={!notebook && c > 0}
                />
              </Rect>
            ))}
          </Layout>
        ))}
      </Layout>
    </Rect>
  ) as Rect;
}
