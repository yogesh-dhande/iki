// How long things stay on screen. Captions are timed for reading aloud, so the same timing
// works when a voice-over replaces the reading estimate (see script/plan.ts).

export const pacing = {
  /** Narration speed: about 160 words a minute, a calm explainer pace (and 15 characters a second to read, under the 17 that subtitle guides allow adults). */
  wordsPerSecond: 2.7,
  /** Added to every line, for the eye to find the caption and the viewer to take it in. */
  linePad: 0.6,
  /** No caption is on screen for less than this. */
  minLine: 2.0,
  /** With a voice-over, the gap after each line's audio before the next. */
  voicePad: 0.35,
  /** A scene holds this long after its last line, before the next scene's transition. */
  sceneHold: 0.5,
  /** The last scene holds this long before the video ends. */
  endHold: 1.5,
  /** Extra holds a script may ask for after a line, by name. */
  hold: { none: 0, short: 0.5, medium: 1.0, long: 2.0 },
} as const;

export type HoldName = keyof typeof pacing.hold;

/** Words in a line, as narration counts them: HRIDs and numbers count as one word each. */
export function wordCount(text: string): number {
  return text.split(/\s+/).filter((word) => /[\p{L}\p{N}]/u.test(word)).length;
}

/** How long a caption stays on screen when it's read, not voiced. */
export function readingSeconds(text: string): number {
  return Math.max(pacing.minLine, wordCount(text) / pacing.wordsPerSecond + pacing.linePad);
}
