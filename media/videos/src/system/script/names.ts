// The names a script may use for timing, motion and tone, taken from the theme so the
// schema and the theme can't disagree.
import { duration, entrances, exits } from "../theme/motion.ts";
import { pacing } from "../theme/pacing.ts";
import { tones } from "../theme/theme.ts";

function keys<T extends object>(value: T): [keyof T & string, ...(keyof T & string)[]] {
  return Object.keys(value) as [keyof T & string, ...(keyof T & string)[]];
}

export const durationNames = keys(duration);
export const holdNames = keys(pacing.hold);
export const toneNames = keys(tones);
export const entranceNames = entrances;
export const exitNames = exits;
