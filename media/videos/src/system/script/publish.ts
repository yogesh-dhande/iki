// What publishing a video needs, from its script and its plan: the title, the description
// with its chapters in YouTube's format, the tags, and the files to upload. The render
// writes it to out/<video>.publish.json, and out/<video>.publish.md for pasting.
import { YOUTUBE } from "./rules.ts";
import type { Plan } from "./plan.ts";
import type { Video } from "./schema.ts";

export interface Chapter {
  /** Seconds from the start, whole, as YouTube reads them. */
  start: number;
  /** As YouTube's description wants it: 0:00, 1:05. */
  time: string;
  title: string;
}

export interface Publishing {
  video: string;
  title: string;
  /** The description to paste into YouTube or the website, chapters included. */
  description: string;
  tags: string[];
  chapters: Chapter[];
  duration: number;
  language: "en";
  files: {
    /** Without captions: the website's <video>, with the sidecars as <track>s, and YouTube. */
    video: string;
    /** With captions burned in, for social clips. */
    captioned: string;
    vtt: string;
    srt: string;
    thumbnail: string;
  };
}

/** The files a published video's render writes, under out/. */
export function outputFiles(video: string): Publishing["files"] {
  return {
    video: `${video}.mp4`,
    captioned: `${video}.captioned.mp4`,
    vtt: `${video}.vtt`,
    srt: `${video}.srt`,
    thumbnail: `${video}.thumbnail.png`,
  };
}

function timestamp(seconds: number): string {
  const whole = Math.floor(seconds);
  return `${String(Math.floor(whole / 60))}:${String(whole % 60).padStart(2, "0")}`;
}

export function chaptersOf(plan: Plan): Chapter[] {
  return plan.scenes.flatMap((planned, index) => {
    const title = planned.scene.chapter;
    if (!title) return [];
    // A chapter starts as its first scene starts to arrive; the first one at 0:00.
    const start = index === 0 ? 0 : Math.floor(planned.start);
    return [{ start, time: timestamp(start), title }];
  });
}

/** YouTube shows chapters only if there are 3 or more, each at least 10 seconds long. */
export function chapterProblems(plan: Plan): string[] {
  const chapters = chaptersOf(plan);
  const problems: string[] = [];
  if (chapters.length < YOUTUBE.chapters) {
    problems.push(
      `${String(chapters.length)} chapters: YouTube shows them only from ${String(YOUTUBE.chapters)}.`,
    );
  }
  chapters.forEach((chapter, index) => {
    const end = chapters[index + 1]?.start ?? plan.duration;
    if (end - chapter.start < YOUTUBE.chapterSeconds) {
      problems.push(
        `Chapter "${chapter.title}" runs ${(end - chapter.start).toFixed(1)} s: YouTube needs ${String(YOUTUBE.chapterSeconds)} s or more. Join it to the chapter before or after.`,
      );
    }
  });
  return problems;
}

export function publishing(video: Video, plan: Plan): Publishing {
  if (!video.publish) throw new Error(`${video.id}: no publish section in its script.`);
  const chapters = chaptersOf(plan);
  const description = [
    video.publish.description.trim(),
    ["Chapters", ...chapters.map((chapter) => `${chapter.time} ${chapter.title}`)].join("\n"),
  ].join("\n\n");
  return {
    video: video.id,
    title: video.title,
    description,
    tags: video.publish.tags,
    chapters,
    duration: Number(plan.duration.toFixed(3)),
    language: "en",
    files: outputFiles(video.id),
  };
}

export function publishingJson(publish: Publishing): string {
  return `${JSON.stringify(publish, null, 2)}\n`;
}

/** The same, to read and paste: generated, so edit the script instead. */
export function publishingMarkdown(publish: Publishing): string {
  const { files } = publish;
  return [
    `# ${publish.title}`,
    "",
    "<!-- Generated from the script by make video: edit the script, not this file. -->",
    "",
    `Duration: ${timestamp(publish.duration)}. Language: English.`,
    "",
    "## Files",
    "",
    `- Video, without captions (website and YouTube): \`${files.video}\``,
    `- Video, captions burned in (social clips): \`${files.captioned}\``,
    `- Captions: \`${files.vtt}\` (website \`<track>\`) and \`${files.srt}\` (YouTube)`,
    `- Thumbnail, 1280×720: \`${files.thumbnail}\``,
    "",
    "## Title",
    "",
    publish.title,
    "",
    "## Description",
    "",
    "```text",
    publish.description,
    "```",
    "",
    "## Tags",
    "",
    publish.tags.join(", "),
    "",
  ].join("\n");
}
