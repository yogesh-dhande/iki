// The merge transition: several things in the scene before become one thing in this one.
// Each merging part flies to the target's place, shrinking and fading as it goes, while
// the target appears there; the rest of the scene before fades out.
import type { Node } from "@motion-canvas/2d";
import { all, delay, type ThreadGenerator } from "@motion-canvas/core";
import type { Parts } from "../components/parts.ts";
import { easing } from "../theme/easing.ts";
import { merge as timing } from "../theme/motion.ts";

export interface MergeProps {
  /** The scene before's parts that merge: every gallery item (`item:<n>`) if left out. */
  from?: string[] | undefined;
  /** This scene's part they merge into. */
  into: string;
}

export function* mergeTransition(
  { from, into }: MergeProps,
  parts: Parts,
  previous: { root: Node; parts: Parts },
  length: number,
): ThreadGenerator {
  const target = parts.get(into);
  target.opacity(0);
  const names = from ?? previous.parts.names().filter((name) => /^item:\d+$/.test(name));
  // Lift the merging parts out of their layout, just where they are, so they can move
  // freely: every place first, since each lift reflows the rest.
  const movers = names.map((name) => {
    const node = previous.parts.get(name);
    return {
      node,
      position: node.absolutePosition(),
      rotation: node.absoluteRotation(),
      scale: node.absoluteScale(),
    };
  });
  for (const { node, position, rotation, scale } of movers) {
    node.remove();
    previous.root.add(node);
    node.absolutePosition(position);
    node.absoluteRotation(rotation);
    node.absoluteScale(scale);
  }
  const moving = new Set(movers.map(({ node }) => node));
  // Whatever else the scene before shows fades as they gather.
  const rest = previous.root.children().filter((child) => !moving.has(child));
  const destination = target.absolutePosition().transformAsPoint(previous.root.worldToLocal());
  const gather = length * timing.gather;
  yield* all(
    ...rest.map((node) => node.opacity(0, gather, easing.exit)),
    ...movers.map(({ node }) =>
      all(
        node.position(destination, gather, easing.move),
        node.scale(timing.shrinkTo, gather, easing.move),
        node.rotation(0, gather, easing.move),
        node.opacity(0, gather, easing.exit),
      ),
    ),
    delay(
      length * timing.arriveFrom,
      target.opacity(1, length * (1 - timing.arriveFrom), easing.enter),
    ),
  );
}
