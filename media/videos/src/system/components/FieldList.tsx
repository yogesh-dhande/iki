import { Layout, Rect } from "@motion-canvas/2d";
import type { FieldData } from "../script/schema.ts";
import { color, excerpt, layoutSize, space, stroke } from "../theme/theme.ts";
import type { Parts } from "./parts.ts";
import { Text, excerptOf } from "./Text.tsx";

export interface FieldListProps {
  fields: FieldData[];
  /** Files each row as a part: `<prefix>field:<label>`. */
  parts?: Parts | undefined;
  prefix?: string | undefined;
  /** Each label above its value, for narrow cards. */
  stacked?: boolean | undefined;
}

function valueText(field: FieldData): string {
  return field.kind === "sequence" ? excerptOf(field.value, excerpt.sequence) : field.value;
}

/** Fields as the app's details card lists them: the label, then the value. */
export function FieldList(props: FieldListProps): Layout {
  return (
    <Layout layout direction="column" gap={space.xs} width="100%">
      {props.fields.map((field) => (
        <Layout
          ref={props.parts?.ref(`${props.prefix ?? ""}field:${field.label}`)}
          layout
          direction="column"
          gap={space.xs}
          width="100%"
        >
          <Rect width="100%" height={stroke.hairline} fill={color.border} />
          <Layout
            layout
            direction={props.stacked ? "column" : "row"}
            gap={props.stacked ? space.xxs : space.s}
            width="100%"
          >
            <Layout width={props.stacked ? undefined : layoutSize.fieldLabel} shrink={0}>
              <Text text={field.label} role="label" ink="muted" />
            </Layout>
            <Layout layout direction="column" gap={space.xxs} grow={1}>
              <Text text={valueText(field)} role="code" mono={field.kind !== "text"} />
              {field.detail === undefined ? null : (
                <Text
                  text={
                    field.detailKind === "sequence"
                      ? excerptOf(field.detail, excerpt.sequence)
                      : field.detail
                  }
                  role="small"
                  ink="muted"
                  mono={field.detailKind === "sequence"}
                />
              )}
            </Layout>
          </Layout>
        </Layout>
      ))}
    </Layout>
  ) as Layout;
}
