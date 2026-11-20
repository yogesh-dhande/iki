import type { Node } from "@motion-canvas/2d";
import { Notice } from "../components/Notice.tsx";
import type { Parts } from "../components/parts.ts";
import { RecordCard } from "../components/RecordCard.tsx";
import { SpreadsheetFragment } from "../components/SpreadsheetFragment.tsx";
import type { Content } from "../script/schema.ts";
import type { size } from "../theme/theme.ts";

/** The component for a slot's content: a record card, a notice or a fragment. */
export function contentNode(
  value: Content,
  ref: (node: Node) => void,
  parts: Parts,
  prefix = "",
  width: keyof typeof size.card = "regular",
): Node {
  if ("record" in value) {
    return (
      <RecordCard ref={ref} record={value.record} parts={parts} prefix={prefix} width={width} />
    );
  }
  if ("notice" in value) return <Notice ref={ref} notice={value.notice} width={width} />;
  return <SpreadsheetFragment ref={ref} fragment={value.fragment} />;
}
