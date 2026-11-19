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

function timestamp(seconds: number): string {
  const ms = Math.round(seconds * 1000);
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  const pad = (n: number, width = 2) => String(n).padStart(width, "0");
  return `${pad(h)}:${pad(m)}:${pad(s)}.${pad(ms % 1000, 3)}`;
}

/** The plan's captions as a WebVTT file: one cue per line, timed as the video shows it. */
export function toVtt(plan: Plan): string {
  const cues: string[] = ["WEBVTT", ""];
  for (const scene of plan.scenes) {
    for (const beat of scene.beats) {
      // A card's line is on screen in the card itself, not burned in, but it's still said.
      const lines =
        beat.lines.length > 0 ? beat.lines : beat.beat.say ? wrapCaption(beat.beat.say) : [];
      if (lines.length === 0) continue;
      cues.push(beat.beat.id, `${timestamp(beat.start)} --> ${timestamp(beat.end)}`, ...lines, "");
    }
  }
  return cues.join("\n");
}
