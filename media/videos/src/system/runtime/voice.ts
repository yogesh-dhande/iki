// A video's voice-over timing, when it has one: src/<video>/voice.json, written by the
// voice-over tool, gives each beat's audio length in seconds. Without it, captions are
// timed for reading (theme/pacing.ts).
import type { Voice } from "../script/plan.ts";

const files = import.meta.glob<Voice>("../../*/voice.json", { eager: true, import: "default" });

export function voiceFor(video: string): Voice | undefined {
  return files[`../../${video}/voice.json`];
}
