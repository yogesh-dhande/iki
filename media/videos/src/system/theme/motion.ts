// How things move. Motion explains: it shows where something came from, what changed and
// what belongs together. Every entrance, exit, transition and emphasis is named here, with
// its duration and easing, and scripts refer to them by name.
// Pure data, so Node tools (the planner, the captions) can read it; easing.ts pairs the
// easing names with Motion Canvas's functions.

/** Durations, in seconds. */
export const duration = {
  /** A caption crossfade, a highlight flick. */
  instant: 0.2,
  /** Exits, small changes. */
  quick: 0.35,
  /** Entrances and emphasis. */
  base: 0.6,
  /** Moves across the frame, merges. */
  slow: 1.0,
  /** Something that should be watched happening, such as copies merging into one. */
  deliberate: 1.6,
  /** From one scene to the next. */
  transition: 0.7,
} as const;

export type DurationName = keyof typeof duration;

/** How far things travel when they enter or leave, in pixels. */
export const travel = {
  short: 24,
  long: 64,
} as const;

/** When several things appear in one step, each starts this long after the previous. */
export const stagger = 0.15;

/** A dimmed part keeps this opacity: there, but not the point. */
export const dimmedOpacity = 0.35;

/** A part grows to this scale while it's emphasized, then settles back. */
export const emphasisScale = 1.06;

/** Named entrances. `fade` is the default. */
export const entrances = ["fade", "rise", "pop", "draw"] as const;
export type Entrance = (typeof entrances)[number];

/** Named exits. */
export const exits = ["fade", "sink"] as const;
export type Exit = (typeof exits)[number];

/** Named transitions between scenes. */
export const transitions = ["fade", "slide", "cut", "merge", "custom"] as const;
export type Transition = (typeof transitions)[number];

/** How long each named step takes, so a script's timing is known before it renders. */
export const stepDuration = {
  show: duration.base,
  hide: duration.quick,
  emphasize: duration.base * 2,
  dim: duration.quick,
} as const;

/**
 * A merge: copies gather into the one thing they become. They travel for the first part
 * of its length; the one thing arrives over the last part. Fractions of the length.
 */
export const merge = {
  gather: 0.65,
  arriveFrom: 0.45,
  /** How small the copies get as they arrive. */
  shrinkTo: 0.3,
} as const;
