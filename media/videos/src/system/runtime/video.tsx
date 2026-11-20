// Plays a script: builds each scene from its layout, runs its beats' steps on time, shows
// its captions, and moves between scenes with the named transitions. Everything follows
// the plan (script/plan.ts), so the video and its .vtt agree to the frame.
import { Node, type View2D } from "@motion-canvas/2d";
import { all, useTime, type ThreadGenerator } from "@motion-canvas/core";
import { CaptionTrack } from "../components/CaptionTrack.tsx";
import { Parts } from "../components/parts.ts";
import { buildLayout } from "../layouts/index.ts";
import { partsOf } from "../script/parts.ts";
import { planVideo, type PlannedBeat, type PlannedScene, type Voice } from "../script/plan.ts";
import type { Scene, Step, Video } from "../script/schema.ts";
import { easing } from "../theme/easing.ts";
import { travel } from "../theme/motion.ts";
import { color, frame } from "../theme/theme.ts";
import { mergeTransition } from "./merge.ts";
import { dim, emphasize, enter, exit, prepareEntrance, wait } from "./steps.ts";

/** What a custom step or transition gets: the scene's parts, and the scene before's. */
export interface CustomContext {
  view: View2D;
  root: Node;
  parts: Parts;
  previous: { root: Node; parts: Parts } | null;
  /** Seconds it must finish in: the length the script gave it. */
  length: number;
  args: Record<string, unknown>;
}

export type CustomStep = (context: CustomContext) => ThreadGenerator;

/** A video's own steps and transitions, by the names its script uses (src/<video>/custom.ts). */
export type CustomSteps = Record<string, CustomStep>;

/** Waits until `time` seconds into the video; fails if it's already past, by more than a frame. */
function* until(time: number, what: string): ThreadGenerator {
  const late = useTime() - time;
  if (late > 1 / frame.fps + 1e-6) {
    throw new Error(
      `${what} ran ${late.toFixed(2)} s past its planned end: a step overruns its beat.`,
    );
  }
  yield* wait(time - useTime());
}

function partOf(step: Step): string | null {
  if ("show" in step) return step.show;
  if ("hide" in step) return step.hide;
  if ("emphasize" in step) return step.emphasize;
  if ("dim" in step) return step.dim;
  return null;
}

function buildScene(planned: PlannedScene): { root: Node; parts: Parts } {
  const parts = new Parts();
  const root = <Node>{buildLayout(planned.scene, parts)}</Node>;
  const expected = new Set(partsOf(planned.scene));
  const built = new Set(parts.names());
  const missing = [...expected].filter((name) => !built.has(name));
  const extra = [...built].filter((name) => !expected.has(name));
  if (missing.length > 0 || extra.length > 0) {
    throw new Error(
      `The ${planned.scene.layout} layout's parts don't match script/parts.ts: missing ${missing.join(", ") || "none"}, extra ${extra.join(", ") || "none"}.`,
    );
  }
  return { root, parts };
}

/** Hides every part a step will show, in the state its entrance starts from. */
function prepare(scene: Scene, parts: Parts): void {
  for (const beat of scene.beats) {
    for (const group of beat.do) {
      for (const step of "together" in group ? group.together : [group]) {
        if ("show" in step) prepareEntrance(parts.get(step.show), step.as);
      }
    }
  }
}

function* runStep(step: Step, context: CustomContext, custom: CustomSteps): ThreadGenerator {
  const name = partOf(step);
  const node = name === null ? null : context.parts.get(name);
  if ("show" in step && node) yield* enter(node, step.as);
  else if ("hide" in step && node) yield* exit(node, step.as);
  else if ("emphasize" in step && node) yield* emphasize(node);
  else if ("dim" in step && node) yield* dim(node);
  else if ("custom" in step) {
    const run = custom[step.custom];
    if (!run) throw new Error(`No custom step "${step.custom}" in this video's custom.ts.`);
    yield* run({ ...context, args: step.args });
  }
}

function* runBeat(
  planned: PlannedBeat,
  context: Omit<CustomContext, "length" | "args">,
  captions: CaptionTrack,
  custom: CustomSteps,
): ThreadGenerator {
  const threads: ThreadGenerator[] = [];
  if (planned.lines.length > 0) threads.push(captions.show(planned.lines));
  for (const step of planned.steps) {
    threads.push(
      (function* () {
        yield* wait(planned.start + step.at - useTime());
        yield* runStep(step.step, { ...context, length: step.length, args: {} }, custom);
      })(),
    );
  }
  yield* all(...threads);
  yield* until(planned.end, `Beat ${planned.beat.id}`);
}

function* transition(
  planned: PlannedScene,
  context: Omit<CustomContext, "length" | "args">,
  custom: CustomSteps,
): ThreadGenerator {
  const { root, previous } = context;
  const length = planned.transitionLength;
  const how = planned.scene.transition;
  if (!previous || length === 0) {
    previous?.root.remove();
    return;
  }
  if (typeof how === "object" && "merge" in how) {
    yield* mergeTransition(how.merge, context.parts, previous, length);
  } else if (typeof how === "object") {
    const run = custom[how.custom];
    if (!run) throw new Error(`No custom transition "${how.custom}" in this video's custom.ts.`);
    yield* run({ ...context, length, args: {} });
  } else if (how === "slide") {
    root.opacity(0);
    root.x(travel.long);
    yield* all(
      previous.root.opacity(0, length, easing.exit),
      previous.root.x(-travel.long, length, easing.move),
      root.opacity(1, length, easing.enter),
      root.x(0, length, easing.move),
    );
  } else {
    // A fade: the old scene goes, then the new one comes, so they never overlap.
    root.opacity(0);
    yield* previous.root.opacity(0, length / 2, easing.exit);
    yield* root.opacity(1, length / 2, easing.enter);
  }
  previous.root.remove();
}

/** Plays the whole video in `view`. */
export interface PlayOptions {
  /** The video's own steps and transitions, if it has any (src/<video>/custom.ts). */
  custom?: CustomSteps;
  /** The voice-over's line lengths, which retime the plan (src/<video>/voice.json). */
  voice?: Voice | undefined;
  /** Burn the captions in: false for the clean render, whose captions are the sidecars. */
  captions?: boolean;
}

export function* playVideo(
  view: View2D,
  video: Video,
  { custom = {}, voice, captions: burnIn = true }: PlayOptions = {},
): ThreadGenerator {
  view.fill(color.canvas);
  const plan = planVideo(video, voice);
  const stage = <Node />;
  const captions = new CaptionTrack();
  view.add(stage);
  // The clean render leaves the captions out of the picture: its captions are the sidecars.
  const track = captions.node();
  if (burnIn) view.add(track);
  let previous: { root: Node; parts: Parts } | null = null;
  for (const planned of plan.scenes) {
    const { root, parts } = buildScene(planned);
    stage.add(root);
    prepare(planned.scene, parts);
    const context = { view, root, parts, previous };
    yield* transition(planned, context, custom);
    yield* until(planned.ready, `Scene ${planned.scene.id}'s transition`);
    for (const beat of planned.beats) yield* runBeat(beat, context, captions, custom);
    // A caption belongs to its scene: it goes when the scene's last line ends, as its cue
    // in the .vtt file does, not with the transition.
    yield* captions.clear();
    yield* until(planned.end, `Scene ${planned.scene.id}`);
    previous = { root, parts };
  }
}
