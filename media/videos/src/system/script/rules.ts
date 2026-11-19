// What the schema can't say: the checks that keep scripts consistent with the layouts and
// the pacing rules. Each problem names where it is.
import { caption } from "../theme/theme.ts";
import { wrapCaption } from "./captions.ts";
import { partsOf } from "./parts.ts";
import type { Step, Video } from "./schema.ts";

function partOf(step: Step): string | undefined {
  if ("show" in step) return step.show;
  if ("hide" in step) return step.hide;
  if ("emphasize" in step) return step.emphasize;
  if ("dim" in step) return step.dim;
  return undefined;
}

export function checkRules(video: Video): string[] {
  const problems: string[] = [];
  const sceneIds = new Set<string>();
  const beatIds = new Set<string>();
  video.scenes.forEach((scene, index) => {
    const where = `scenes.${String(index)} (${scene.id})`;
    if (sceneIds.has(scene.id)) problems.push(`${where}: another scene has the id "${scene.id}".`);
    sceneIds.add(scene.id);
    if (index === 0 && scene.transition !== "fade") {
      problems.push(`${where}: the first scene has nothing to transition from; leave it out.`);
    }
    const parts = new Set(partsOf(scene));
    if (scene.layout === "graph") {
      const nodes = new Set(scene.slots.nodes.map((node) => node.id));
      for (const edge of scene.slots.edges) {
        for (const end of [edge.from, edge.to]) {
          if (!nodes.has(end))
            problems.push(`${where}: an edge names "${end}", which isn't a node.`);
        }
      }
    }
    if (scene.layout === "gallery") {
      const cards = scene.slots.items.filter((item) => !("fragment" in item)).length;
      if (cards > 0 && scene.slots.items.length > 3) {
        problems.push(
          `${where}: a gallery of records or notices holds at most 3: they're taller than files.`,
        );
      }
    }
    if (scene.layout === "timeline") {
      const numbers = scene.slots.revisions.map((revision) => revision.number);
      if (numbers.some((number, i) => i > 0 && number <= (numbers[i - 1] ?? 0))) {
        problems.push(`${where}: revisions go oldest first, numbered upwards.`);
      }
    }
    for (const beat of scene.beats) {
      const at = `${where}, beat ${beat.id}`;
      if (beatIds.has(beat.id)) problems.push(`${at}: another beat has this id.`);
      beatIds.add(beat.id);
      if (!beat.caption && scene.layout !== "title" && scene.layout !== "closing") {
        problems.push(`${at}: give it a caption; only title and closing cards may go without.`);
      }
      if (beat.caption) {
        const lines = wrapCaption(beat.caption);
        if (lines.length > caption.maxLines) {
          problems.push(
            `${at}: the caption needs ${String(lines.length)} lines; keep it to ${String(caption.maxLines)} (about ${String(caption.maxLines * caption.maxCharsPerLine)} characters), or split the beat.`,
          );
        }
      }
      const card = scene.layout === "title" || scene.layout === "closing";
      if (beat.say && !beat.caption && !card) {
        problems.push(`${at}: "say" needs a caption to go with it.`);
      }
      if (beat.say && wrapCaption(beat.say).length > caption.maxLines) {
        problems.push(
          `${at}: what it says needs more than ${String(caption.maxLines)} caption lines; split the beat.`,
        );
      }
      const steps = beat.do.flatMap((group) => ("together" in group ? group.together : [group]));
      for (const step of steps) {
        const part = partOf(step);
        if (part !== undefined && !parts.has(part)) {
          problems.push(
            `${at}: "${part}" isn't a part of a ${scene.layout} scene; it has ${[...parts].join(", ")}.`,
          );
        }
      }
    }
  });
  return problems;
}
