import { Layout } from "@motion-canvas/2d";
import type { RecordData } from "../script/schema.ts";
import { size, space } from "../theme/theme.ts";
import { Card } from "./Card.tsx";
import { FieldList } from "./FieldList.tsx";
import { HridBadge } from "./HridBadge.tsx";
import type { Parts } from "./parts.ts";
import { Text } from "./Text.tsx";

export interface RecordCardProps {
  record: RecordData;
  /** Narrow cards stack each field's label above its value. */
  width?: keyof typeof size.card | undefined;
  /** Files the card's fields as parts. */
  parts?: Parts | undefined;
  prefix?: string | undefined;
  ref?: ((node: Layout) => void) | undefined;
}

/**
 * An entity as its page shows it: the type's label, the HRID badge, the title, and its
 * fields. A record without an HRID is a draft, not registered yet: dashed, marked "New".
 */
export function RecordCard(props: RecordCardProps): Layout {
  const { record } = props;
  const draft = record.hrid === undefined;
  return (
    <Card ref={props.ref} width={size.card[props.width ?? "regular"]} draft={draft} gap="m">
      <Layout layout direction="column" gap={space.xxs}>
        <Layout layout direction="row" justifyContent="space-between" alignItems="center">
          <Text text={record.type} role="label" ink="muted" />
          <HridBadge hrid={record.hrid ?? "New"} draft={draft} />
        </Layout>
        <Text text={record.name} role="heading" />
      </Layout>
      {record.fields.length > 0 ? (
        <FieldList
          fields={record.fields}
          parts={props.parts}
          prefix={props.prefix}
          stacked={props.width === "narrow"}
        />
      ) : undefined}
      {record.footer === undefined ? null : <Text text={record.footer} role="label" ink="muted" />}
    </Card>
  ) as Layout;
}
