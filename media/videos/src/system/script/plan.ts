// When everything happens. The plan is worked out from the script alone, before anything
// renders, so the captions, the .vtt file and (later) the voice-over all share one timing.
// The runtime follows it to the frame and fails if a step overruns its beat.
import { duration, stagger, stepDuration } from "../theme/motion.ts";
import { pacing, readingSeconds } from "../theme/pacing.ts";
import { wrapCaption } from "./captions.ts";
import type { Beat, Scene, Step, StepGroup, Video } from "./schema.ts";

/** Seconds of recorded voice-over per beat id, when there is one (voice.json). */
export type Voice = Record<string, number>;

export interface PlannedStep {
  step: Step;
  /** Seconds from the beat's start. */
  at: number;
  length: number;
}

export interface PlannedBeat {
  beat: Beat;
  /** The caption, wrapped into its lines. */
  lines: string[];
  start: number;
  end: number;
  steps: PlannedStep[];
}

export interface PlannedScene {
  scene: Scene;
  start: number;
  /** When the transition in is over and the first beat starts. */
  ready: number;
  end: number;
  transitionLength: number;
  beats: PlannedBeat[];
}

export interface Plan {
  scenes: PlannedScene[];
  duration: number;
}

export function stepLength(step: Step): number {
  if ("custom" in step) return duration[step.length];
  if ("show" in step) return stepDuration.show;
  if ("hide" in step) return stepDuration.hide;
  if ("emphasize" in step) return stepDuration.emphasize;
  return stepDuration.dim;
}

function planSteps(groups: StepGroup[]): { steps: PlannedStep[]; length: number } {
  const steps: PlannedStep[] = [];
  let at = 0;
  for (const group of groups) {
    const members = "together" in group ? group.together : [group];
    let groupEnd = at;
    members.forEach((step, index) => {
      const length = stepLength(step);
      const start = at + index * stagger;
      steps.push({ step, at: start, length });
      groupEnd = Math.max(groupEnd, start + length);
    });
    at = groupEnd;
  }
  return { steps, length: at };
}

export function transitionLength(scene: Scene, first: boolean): number {
  if (first || scene.transition === "cut") return 0;
  if (typeof scene.transition === "object") return duration[scene.transition.length];
  return duration.transition;
}

/** How long a line needs: its voice-over if there is one, else the time to read it. */
export function lineSeconds(beat: Beat, voice: Voice | undefined): number {
  const spoken = voice?.[beat.id];
  if (spoken !== undefined) return Math.max(pacing.minLine, spoken + pacing.voicePad);
  const words = beat.say ?? beat.caption;
  return words === undefined ? 0 : readingSeconds(words);
}

export function planVideo(video: Video, voice?: Voice): Plan {
  const scenes: PlannedScene[] = [];
  let time = 0;
  video.scenes.forEach((scene, sceneIndex) => {
    const start = time;
    const transition = transitionLength(scene, sceneIndex === 0);
    time += transition;
    const ready = time;
    const beats = scene.beats.map((beat): PlannedBeat => {
      const { steps, length } = planSteps(beat.do);
      const beatStart = time;
      time += Math.max(lineSeconds(beat, voice), length) + pacing.hold[beat.hold];
      return {
        beat,
        lines: beat.caption ? wrapCaption(beat.caption) : [],
        start: beatStart,
        end: time,
        steps,
      };
    });
    time += sceneIndex === video.scenes.length - 1 ? pacing.endHold : pacing.sceneHold;
    scenes.push({ scene, start, ready, end: time, transitionLength: transition, beats });
  });
  return { scenes, duration: time };
}
