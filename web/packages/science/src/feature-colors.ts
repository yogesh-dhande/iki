import { kindOf, type Kind } from "./sequence.js";
const COLORS = {
  coding: ["#2f6fb5", "#7fb0ff"],
  regulatory: ["#2e8540", "#6fd49a"],
  terminator: ["#b5452f", "#ff9b85"],
  origin: ["#8a4fb5", "#c9a2f2"],
  binding: ["#a36b00", "#f2b75a"],
  other: ["#5f6b78", "#a3b0bd"],
} as const;

/** A host can override a kind's CSS token. The native color scheme supplies the default. */
export function featureColor(type: string): string {
  const kind = kindOf(type);
  const [light, dark] = COLORS[kind];
  return `var(--feature-${kind}, light-dark(${light}, ${dark}))`;
}

/** Palette for the sequence-viewer's CSS, scoped to its own host. */
export function defaultFeatureColors(host: HTMLElement): void {
  for (const kind of Object.keys(COLORS) as Kind[]) {
    const [light, dark] = COLORS[kind];
    host.style.setProperty(`--sequence-default-${kind}`, `light-dark(${light}, ${dark})`);
  }
}
