// What a script's steps do to parts: the named entrances, exits and emphasis.
import { Line, type Node } from "@motion-canvas/2d";
import { all, chain, waitFor, type ThreadGenerator } from "@motion-canvas/core";
import type { Entrance, Exit } from "../theme/motion.ts";
import { dimmedOpacity, duration, emphasisScale, stepDuration, travel } from "../theme/motion.ts";
import { easing } from "../theme/easing.ts";

function lines(node: Node): Line[] {
  return node instanceof Line ? [node] : node.findAll<Line>((child) => child instanceof Line);
}

/** Puts a part in the state its entrance starts from, before the scene shows. */
export function prepareEntrance(node: Node, entrance: Entrance): void {
  node.opacity(0);
  if (entrance === "rise") node.position.y(node.position.y() + travel.short);
  if (entrance === "pop") node.scale(1 - (emphasisScale - 1));
  if (entrance === "draw") for (const line of lines(node)) line.end(0);
}

export function* enter(node: Node, entrance: Entrance): ThreadGenerator {
  const length = stepDuration.show;
  switch (entrance) {
    case "fade":
      yield* node.opacity(1, length, easing.enter);
      return;
    case "rise":
      yield* all(
        node.opacity(1, length, easing.enter),
        node.position.y(node.position.y() - travel.short, length, easing.enter),
      );
      return;
    case "pop":
      yield* all(node.opacity(1, length, easing.enter), node.scale(1, length, easing.settle));
      return;
    case "draw": {
      // The line draws itself, then whatever it carries (a label) fades in.
      node.opacity(1);
      const carried = node.children().filter((child) => !(child instanceof Line));
      for (const child of carried) child.opacity(0);
      yield* chain(
        all(...lines(node).map((line) => line.end(1, length - duration.instant, easing.move))),
        all(...carried.map((child) => child.opacity(1, duration.instant, easing.enter))),
      );
      return;
    }
  }
}

export function* exit(node: Node, how: Exit): ThreadGenerator {
  const length = stepDuration.hide;
  if (how === "sink") {
    yield* all(
      node.opacity(0, length, easing.exit),
      node.position.y(node.position.y() + travel.short, length, easing.exit),
    );
    return;
  }
  yield* node.opacity(0, length, easing.exit);
}

/** Draws the eye: the part grows a little and settles back. */
export function* emphasize(node: Node): ThreadGenerator {
  const half = stepDuration.emphasize / 2;
  const scale = node.scale.x();
  yield* chain(
    node.scale(scale * emphasisScale, half, easing.move),
    node.scale(scale, half, easing.move),
  );
}

export function* dim(node: Node): ThreadGenerator {
  yield* node.opacity(dimmedOpacity, stepDuration.dim, easing.move);
}

export function* wait(seconds: number): ThreadGenerator {
  if (seconds > 0) yield* waitFor(seconds);
}
