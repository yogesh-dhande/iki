// The one source of truth for how Iki's videos look and move. Colours, fonts, the corner
// radius and the shadow come from the web app's design tokens (tokens.generated.ts); the
// rest is the video's own scale, set once here. Components, layouts and scenes take every
// size, colour and duration from this module: ESLint refuses literals elsewhere.
import { cssTokens } from "./tokens.generated.ts";

/** The frame: 1920 × 1080 at 30 fps. Motion Canvas puts the origin at the centre. */
export const frame = {
  width: 1920,
  height: 1080,
  fps: 30,
} as const;

/**
 * How many video pixels one CSS pixel of the web app becomes. The app's 10 px radius and
 * 1 px borders scale by this, so cards keep the app's proportions at 1080p.
 */
export const cssScale = 2;

function cssPixels(value: string): number {
  const match = /^(-?[\d.]+)px$/.exec(value.trim());
  if (!match?.[1]) throw new Error(`Expected a length in px, got "${value}".`);
  return Number(match[1]) * cssScale;
}

/** The app's colours, by role. Only these, and the mixes below, appear in a video. */
export const color = {
  canvas: cssTokens.bg,
  surface: cssTokens.surface,
  surfaceSunken: cssTokens["surface-2"],
  text: cssTokens.text,
  muted: cssTokens.muted,
  border: cssTokens.border,
  accent: cssTokens.accent,
  accentStrong: cssTokens["accent-hover"],
  onAccent: cssTokens["accent-text"],
  focus: cssTokens.focus,
  error: cssTokens.error,
  errorWash: cssTokens["error-bg"],
  warning: cssTokens.warning,
  warningWash: cssTokens["warning-bg"],
  success: cssTokens.success,
  successWash: cssTokens["success-bg"],
} as const;

export type ColorRole = keyof typeof color;

/**
 * Tones say what something means, never how it looks: components take a tone and map it
 * to colours here, so "warning" is the same amber everywhere. Each pair passes WCAG AA.
 */
export const tones = {
  neutral: { text: color.text, wash: color.surfaceSunken, line: color.border },
  accent: { text: color.accent, wash: color.surfaceSunken, line: color.accent },
  warning: { text: color.warning, wash: color.warningWash, line: color.warning },
  error: { text: color.error, wash: color.errorWash, line: color.error },
  success: { text: color.success, wash: color.successWash, line: color.success },
} as const;

export type Tone = keyof typeof tones;

/**
 * The bundled fonts (fonts.ts): Inter for text, JetBrains Mono for IDs and sequences, in
 * place of the app's system font stacks, which differ from machine to machine.
 */
export const fontFamily = { text: "Iki Video Text", mono: "Iki Video Mono" } as const;

export const font = {
  text: `"${fontFamily.text}"`,
  mono: `"${fontFamily.mono}"`,
} as const;

/** Weights the app uses: 400 body, 600 emphasis and badges, 700 headings. */
export const weight = {
  regular: 400,
  medium: 500,
  semibold: 600,
  bold: 700,
} as const;

/**
 * Type sizes at 1080p, in pixels. Nothing smaller than `label`: on a phone the frame
 * shrinks to about a fifth, and 26 px is the smallest that stays readable there.
 */
export const type = {
  display: { size: 96, lineHeight: 1.1, weight: weight.bold },
  title: { size: 64, lineHeight: 1.15, weight: weight.bold },
  heading: { size: 44, lineHeight: 1.2, weight: weight.semibold },
  body: { size: 34, lineHeight: 1.35, weight: weight.regular },
  label: { size: 26, lineHeight: 1.3, weight: weight.semibold },
  small: { size: 26, lineHeight: 1.3, weight: weight.regular },
  code: { size: 30, lineHeight: 1.35, weight: weight.regular },
  caption: { size: 52, lineHeight: 1.3, weight: weight.semibold },
} as const;

export type TypeRole = keyof typeof type;

/** Spacing steps: every gap and padding is one of these. */
export const space = {
  xxs: 6,
  xs: 12,
  s: 20,
  m: 32,
  l: 48,
  xl: 72,
  xxl: 112,
} as const;

/** Corner radii: the app's radius, scaled; pills are fully round. */
export const radius = {
  card: cssPixels(cssTokens.radius),
  small: cssPixels(cssTokens.radius) / 2,
  pill: 999,
} as const;

/** Line widths: the app's 1 px border, scaled; connectors and the timeline are heavier. */
export const stroke = {
  hairline: cssScale,
  rule: cssScale * 2,
  connector: cssScale * 2,
  focus: cssScale * 3,
} as const;

/** A CSS box-shadow's last layer, scaled: `0 4px 16px rgb(16 24 40 / 6%)`. */
function cssShadow(value: string): { color: string; offsetY: number; blur: number } {
  const layers = value.split(/,(?![^(]*\))/);
  const last = layers[layers.length - 1]?.trim() ?? "";
  const match =
    /^(-?[\d.]+)(?:px)?\s+(-?[\d.]+)(?:px)?\s+([\d.]+)(?:px)?\s+rgb\((\d+) (\d+) (\d+) \/ ([\d.]+)%\)$/.exec(
      last,
    );
  if (!match)
    throw new Error(`Expected a shadow like "0 4px 16px rgb(r g b / a%)", got "${last}".`);
  const [, , y, blur, r, g, b, alpha] = match.map(Number);
  return {
    color: `rgba(${String(r)}, ${String(g)}, ${String(b)}, ${String((alpha ?? 0) / 100)})`,
    offsetY: (y ?? 0) * cssScale,
    blur: (blur ?? 0) * cssScale,
  };
}

/** The app's card shadow (its wider layer), scaled. */
export const shadow = {
  card: cssShadow(cssTokens.shadow),
} as const;

/** Widths things are drawn at, so the same object is the same size in every video. */
export const size = {
  card: { narrow: 520, regular: 760, wide: 980 },
  node: { width: 420 },
  fragment: { width: 520 },
  inset: { width: 600 },
  chip: { width: 380 },
  dot: 28,
  icon: 44,
  brand: 72,
} as const;

/**
 * Where things may go. Content stays inside the title-safe area, above the caption band;
 * captions own the band.
 */
export const safe = {
  /** Horizontal margin from each edge. */
  x: 120,
  /** Top margin. */
  top: 80,
  /** The caption band's height, at the bottom of the frame. */
  captionBand: 230,
  /** Bottom margin under the captions. */
  bottom: 48,
} as const;

/** The content area, in Motion Canvas coordinates (origin at the centre). */
export const content = {
  left: -frame.width / 2 + safe.x,
  right: frame.width / 2 - safe.x,
  top: -frame.height / 2 + safe.top,
  bottom: frame.height / 2 - safe.captionBand,
  get width(): number {
    return this.right - this.left;
  },
  get height(): number {
    return this.bottom - this.top;
  },
  get centerY(): number {
    return (this.top + this.bottom) / 2;
  },
} as const;

/** Captions: large, high-contrast, on a plate, at most two lines. */
export const caption = {
  type: type.caption,
  maxCharsPerLine: 42,
  maxLines: 2,
  plate: { fill: color.surface, line: color.border, padX: space.l, padY: space.s },
  maxWidth: 1500,
  /** The plate's centre, measured up from the bottom edge. */
  centerFromBottom: safe.bottom + 80,
} as const;

/** Ink: the colour a piece of text takes, by role. */
export const ink = {
  text: color.text,
  muted: color.muted,
  accent: color.accent,
  onAccent: color.onAccent,
  warning: color.warning,
  error: color.error,
  success: color.success,
} as const;

export type Ink = keyof typeof ink;

/** Content rules: how much of a long value shows. */
export const excerpt = {
  /** Residues of a sequence shown before the ellipsis. */
  sequence: 18,
  /** In a spreadsheet cell, which is narrower. */
  cell: 13,
} as const;

/** Fixed parts of the layouts. */
export const layoutSize = {
  /** The label column of a field list. */
  fieldLabel: 230,
  /** The gap a link arrow spans between two panels. */
  link: 240,
  /** A label above a panel, and the gap under it. */
  panelLabelGap: space.s,
  /** The gallery's slight tilts, in degrees, by position: papers dropped on a desk. */
  galleryTilt: [-2, 1.5, -1, 2, -1.5, 1],
  /** The closing statement's width. */
  statement: 1400,
  /** The graph's gap between nodes in a column. */
  graphRowGap: space.l,
} as const;
