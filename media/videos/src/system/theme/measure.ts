// How wide a line of text is drawn, without a browser: the sum of its characters' advances
// in the bundled fonts (metrics.generated.ts). Kerning only tightens text, so it's never
// short. Layouts use it to make room for text before they place things.
import { advances } from "./metrics.generated.ts";
import { type, weight, type TypeRole } from "./theme.ts";

type Face = keyof typeof advances;

const faces: Partial<Record<number, Face>> = {
  [weight.regular]: "regular",
  [weight.semibold]: "semibold",
};

/** The widest character a face has: what a character missing from the table counts as. */
function widest(face: Face): number {
  return Math.max(...Object.values(advances[face]));
}

/** A line of text's width in pixels, in a role whose face is Inter Regular or SemiBold. */
export function textWidth(text: string, role: TypeRole): number {
  const style = type[role];
  const face = faces[style.weight];
  if (face === undefined) {
    throw new Error(`No metrics for the ${role} role's weight, ${String(style.weight)}.`);
  }
  const table: Record<string, number | undefined> = advances[face];
  let units = 0;
  for (const character of text) units += table[character] ?? widest(face);
  return (units / 1000) * style.size;
}
