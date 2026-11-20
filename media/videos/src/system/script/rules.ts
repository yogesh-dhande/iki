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

/** YouTube's limits: title and tags in characters (its description's limit is far off). */
export const YOUTUBE = { title: 100, tags: 500, chapters: 3, chapterSeconds: 10 } as const;

export function checkRules(video: Video): string[] {
  const problems: string[] = [];
  const sceneIds = new Set<string>();
  const beatIds = new Set<string>();
  if (!video.specimen) {
    if (!video.publish) problems.push("publish: a published video needs its description and tags.");
    if (!video.scenes[0]?.chapter)
      problems.push("scenes.0: the first scene starts the first chapter: give it a chapter.");
  }
  if (video.publish) {
    const tags = video.publish.tags.join(",").length;
    if (tags > YOUTUBE.tags)
      problems.push(
        `publish.tags: ${String(tags)} characters, more than YouTube's ${String(YOUTUBE.tags)}.`,
      );
    if (video.title.length > YOUTUBE.title)
      problems.push(`title: longer than YouTube's ${String(YOUTUBE.title)} characters.`);
    const thumbnail = video.publish.thumbnail;
    if (
      thumbnail &&
      !video.scenes.some((scene) => scene.beats.some((beat) => beat.id === thumbnail))
    ) {
      problems.push(`publish.thumbnail: no beat "${thumbnail}".`);
    }
  }
  const text = JSON.stringify(video.scenes);
  video.quotes.forEach((quote, index) => {
    if (!text.includes(JSON.stringify(quote.text).slice(1, -1))) {
      problems.push(
        `quotes.${String(index)}: the script doesn't say "${quote.text}"; quote only what it shows.`,
      );
    }
  });
  video.scenes.forEach((scene, index) => {
    const where = `scenes.${String(index)} (${scene.id})`;
    if (sceneIds.has(scene.id)) problems.push(`${where}: another scene has the id "${scene.id}".`);
    sceneIds.add(scene.id);
    if (index === 0 && scene.transition !== "fade") {
      problems.push(`${where}: the first scene has nothing to transition from; leave it out.`);
    }
    const parts = new Set(partsOf(scene));
    const before = video.scenes[index - 1];
    if (typeof scene.transition === "object" && "merge" in scene.transition && before) {
      const { from, into } = scene.transition.merge;
      if (!parts.has(into))
        problems.push(`${where}: merges into "${into}", which this scene doesn't have.`);
      const earlier = partsOf(before);
      const merging = from ?? earlier.filter((name) => /^item:\d+$/.test(name));
      if (merging.length === 0) {
        problems.push(
          `${where}: merges the scene before's gallery items, but it has none: name them in "from".`,
        );
      }
      for (const name of merging) {
        if (!earlier.includes(name))
          problems.push(`${where}: merges "${name}", which the scene before doesn't have.`);
      }
    }
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
