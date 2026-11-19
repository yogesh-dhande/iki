// The Entities video's own motion, beyond what the layouts and named steps do.
import { all, delay } from "@motion-canvas/core";
import type { CustomSteps } from "../system/runtime/video.tsx";
import { easing } from "../system/theme/easing.ts";
import { merge } from "../system/theme/motion.ts";

export const custom: CustomSteps = {
  /**
   * The copies collapse into one record: the gallery's files fly into the record card's
   * place and shrink away as the card arrives there.
   */
  *collapse({ parts, previous, length }) {
    const record = parts.get("record");
    record.opacity(0);
    if (!previous) return;
    const target = record.absolutePosition();
    const heading = previous.parts.names().includes("heading")
      ? previous.parts.get("heading")
      : null;
    // Lift the files out of the gallery's layout, where they are, so they can move freely:
    // every place first, since each lift reflows the rest.
    const files = previous.parts
      .names()
      .filter((name) => /^item:\d+$/.test(name))
      .map((name) => {
        const file = previous.parts.get(name);
        return {
          file,
          position: file.absolutePosition(),
          rotation: file.absoluteRotation(),
          scale: file.absoluteScale(),
        };
      });
    for (const { file, position, rotation, scale } of files) {
      file.remove();
      previous.root.add(file);
      file.absolutePosition(position);
      file.absoluteRotation(rotation);
      file.absoluteScale(scale);
    }
    const destination = target.transformAsPoint(previous.root.worldToLocal());
    yield* all(
      ...(heading ? [heading.opacity(0, length * merge.gather, easing.exit)] : []),
      ...files.map(({ file }) =>
        all(
          file.position(destination, length * merge.gather, easing.move),
          file.scale(merge.shrinkTo, length * merge.gather, easing.move),
          file.rotation(0, length * merge.gather, easing.move),
          file.opacity(0, length * merge.gather, easing.exit),
        ),
      ),
      delay(
        length * merge.arriveFrom,
        record.opacity(1, length * (1 - merge.arriveFrom), easing.enter),
      ),
    );
  },
};
