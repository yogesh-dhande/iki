import { Layout, Rect } from "@motion-canvas/2d";
import type { RevisionData } from "../script/schema.ts";
import { size, space } from "../theme/theme.ts";
import { Card } from "./Card.tsx";
import { Text } from "./Text.tsx";

export interface RevisionChipProps {
  revision: RevisionData;
  ref?: ((node: Rect) => void) | undefined;
}

/**
 * One revision, as the History tab lists it: "Revision 2: Edited by …", when, and the
 * reason, in quotes, when one was given.
 */
export function RevisionChip(props: RevisionChipProps): Rect {
  const { revision } = props;
  return (
    <Card ref={props.ref} width={size.chip.width} padding="s" gap="xxs">
      <Text text={`Revision ${String(revision.number)}`} role="label" ink="accent" />
      <Text
        text={`${revision.what} by ${revision.who}`}
        role="label"
        width={size.chip.width - space.s * 2}
      />
      <Text text={revision.when} role="small" ink="muted" width={size.chip.width - space.s * 2} />
      {revision.why === undefined ? null : (
        <Layout paddingTop={space.xxs}>
          <Text text={`“${revision.why}”`} role="small" width={size.chip.width - space.s * 2} />
        </Layout>
      )}
    </Card>
  ) as Rect;
}
