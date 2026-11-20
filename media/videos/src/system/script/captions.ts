// Captions: wrapped into short lines for the screen, and written out as WebVTT.
import { caption } from "../theme/theme.ts";
import type { Plan } from "./plan.ts";

/** Wraps a caption into lines of at most `caption.maxCharsPerLine`, breaking at spaces. */
export function wrapCaption(text: string, width: number = caption.maxCharsPerLine): string[] {
  const lines: string[] = [];
  let line = "";
  for (const word of text.trim().split(/\s+/)) {
    if (line && line.length + 1 + word.length > width) {
      lines.push(line);
      line = word;
    } else {
      line = line ? `${line} ${word}` : word;
    }
  }
  if (line) lines.push(line);
  return balance(lines, width);
}

/** Two lines read better when they're about the same length: move words down to even them. */
function balance(lines: string[], width: number): string[] {
  if (lines.length !== 2) return lines;
  let [top = "", bottom = ""] = lines;
  for (;;) {
    const cut = top.lastIndexOf(" ");
    if (cut < 0) break;
    const nextTop = top.slice(0, cut);
    const nextBottom = `${top.slice(cut + 1)} ${bottom}`;
    if (nextBottom.length > width || nextBottom.length > top.length) break;
    top = nextTop;
    bottom = nextBottom;
  }
  return [top, bottom];
}

function timestamp(seconds: number, separator = "."): string {
  const ms = Math.round(seconds * 1000);
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  const pad = (n: number, width = 2) => String(n).padStart(width, "0");
  return `${pad(h)}:${pad(m)}:${pad(s)}${separator}${pad(ms % 1000, 3)}`;
}

interface Cue {
  id: string;
  start: number;
  end: number;
  lines: string[];
}

/** One cue per line, timed as the video shows it. A card's line is said, not burned in. */
function cuesOf(plan: Plan): Cue[] {
  return plan.scenes.flatMap((scene) =>
    scene.beats.flatMap((beat) => {
      const lines =
        beat.lines.length > 0 ? beat.lines : beat.beat.say ? wrapCaption(beat.beat.say) : [];
      return lines.length > 0
        ? [{ id: beat.beat.id, start: beat.start, end: beat.end, lines }]
        : [];
    }),
  );
}

/** The plan's captions as WebVTT, for the website's <track>. Cues are named by beat. */
export function toVtt(plan: Plan): string {
  return [
    "WEBVTT",
    "",
    ...cuesOf(plan).flatMap((cue) => [
      cue.id,
      `${timestamp(cue.start)} --> ${timestamp(cue.end)}`,
      ...cue.lines,
      "",
    ]),
  ].join("\n");
}

/** The same captions as SubRip, which YouTube's caption upload takes. Cues are numbered. */
export function toSrt(plan: Plan): string {
  return cuesOf(plan)
    .flatMap((cue, index) => [
      String(index + 1),
      `${timestamp(cue.start, ",")} --> ${timestamp(cue.end, ",")}`,
      ...cue.lines,
      "",
    ])
    .join("\n");
}
