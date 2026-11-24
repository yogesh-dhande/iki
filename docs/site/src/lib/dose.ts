// The dose-response figure's geometry: the four-parameter curve the fit records, drawn on
// a log₂ concentration axis. The parameters are the plugin's (iki/plate_assays,
// scripts/four_pl.py); this only draws them, with the response function its worked example
// gives (plugins/plate_assays/data/curve-example.md):
//   bottom + (top − bottom) / (1 + (midpoint / concentration)^hill_slope)
import type { Fit } from "./examples.ts";

export interface Frame {
  left: number;
  right: number;
  top: number;
  bottom: number;
  /** The concentration axis, as powers of two. */
  fromLog2: number;
  toLog2: number;
  /** The response axis's top, in the readout's units (it starts at 0). */
  maxResponse: number;
}

export const FRAME: Frame = {
  left: 56,
  right: 536,
  top: 20,
  bottom: 280,
  fromLog2: -1,
  toLog2: 8,
  maxResponse: 1.1,
};

export function response(
  fit: Pick<Fit, "bottom" | "top" | "endpoint_value" | "hill_slope">,
  concentration: number,
): number {
  return (
    fit.bottom +
    (fit.top - fit.bottom) / (1 + (fit.endpoint_value / concentration) ** fit.hill_slope)
  );
}

export function x(concentration: number, frame: Frame = FRAME): number {
  const share = (Math.log2(concentration) - frame.fromLog2) / (frame.toLog2 - frame.fromLog2);
  return frame.left + share * (frame.right - frame.left);
}

export function y(value: number, frame: Frame = FRAME): number {
  return frame.bottom - (value / frame.maxResponse) * (frame.bottom - frame.top);
}

const fixed = (n: number) => n.toFixed(1);

/** The fitted curve across the whole axis, as an SVG path. */
export function curve(fit: Fit, frame: Frame = FRAME, steps = 90): string {
  const parts: string[] = [];
  for (let i = 0; i <= steps; i += 1) {
    const concentration = 2 ** (frame.fromLog2 + (i / steps) * (frame.toLog2 - frame.fromLog2));
    parts.push(
      `${i ? "L" : "M"} ${fixed(x(concentration, frame))} ${fixed(y(response(fit, concentration), frame))}`,
    );
  }
  return parts.join(" ");
}

/** The concentrations the axis labels: every other power of two that's a whole number. */
export function ticks(frame: Frame = FRAME): number[] {
  const found: number[] = [];
  for (let power = Math.ceil(frame.fromLog2); power <= frame.toLog2; power += 2) {
    if (power >= 0) found.push(2 ** power);
  }
  return found;
}

/** A concentration as the guide writes it: 12.07. */
export function nM(value: number): string {
  return value.toFixed(2);
}

/** How strongly a well is tinted for its reading: 1 (faint) to 8 (full). */
export function level(reading: number, max: number): number {
  return Math.min(8, Math.max(1, Math.round((reading / max) * 8)));
}
