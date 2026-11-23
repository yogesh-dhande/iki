import { stretchLength } from "./sequence.js";

export type Topology = "linear" | "circular";

/** First and last bases (1-based, inclusive) on the reference strand, and length in bp. */
export interface DigestFragment {
  start: number;
  end: number;
  length: number;
  topology: Topology;
}

function validLength(length: number): void {
  if (!Number.isSafeInteger(length) || length < 1)
    throw new RangeError("Expected a positive molecule length in bases.");
}

/** Partition a restriction map into top-strand fragments. A boundary k is immediately
 * after base k (0 is before base 1). Equal boundaries are one cut; boundary n is the
 * origin on a circle. Following the feature convention of INSDC, returned base
 * coordinates are inclusive, with a decreasing span crossing the circular origin.
 * https://www.insdc.org/submitting-standards/feature-table/ */
export function digestFragments(
  length: number,
  cutBoundaries: readonly number[],
  topology: Topology,
): DigestFragment[] {
  validLength(length);
  if (!["linear", "circular"].includes(topology))
    throw new RangeError("Expected linear or circular topology.");
  const circular = topology === "circular";
  const cuts = new Set<number>();
  for (const at of cutBoundaries) {
    if (!Number.isSafeInteger(at) || at < 0 || at > length)
      throw new RangeError("Cut lies outside the map.");
    cuts.add(circular && at === length ? 0 : at);
  }
  if (!cuts.size) return [{ start: 1, end: length, length, topology }];
  if (!circular) {
    cuts.add(0);
    cuts.add(length);
  }
  const edges = [...cuts].sort((a, b) => a - b);
  const first = edges[0];
  if (circular && first !== undefined) edges.push(first + length);
  const fragments: DigestFragment[] = [];
  for (let i = 1; i < edges.length; i++) {
    const left = edges[i - 1];
    const right = edges[i];
    if (left === undefined || right === undefined) continue;
    fragments.push({
      start: left + 1,
      end: ((right - 1) % length) + 1,
      length: right - left,
      topology: "linear",
    });
  }
  return fragments;
}

export interface BindingSite {
  start: number;
  end: number;
  strand: "forward" | "reverse";
}

/** Coordinate of an already annotated primer's paired base, counted from its 3′ end
 * (the terminal base is offset 1), following the reference-coordinate convention of
 * INSDC. No sequence or alignment is needed for this projection. */
export function bindingSiteCoordinate(
  site: BindingSite,
  offsetFromThreePrime: number,
  length: number,
  topology: Topology,
): number {
  validLength(length);
  if (!["linear", "circular"].includes(topology))
    throw new RangeError("Expected linear or circular topology.");
  if (
    ![site.start, site.end].every((n) => Number.isSafeInteger(n) && n >= 1 && n <= length) ||
    (topology === "linear" && site.start > site.end) ||
    !["forward", "reverse"].includes(site.strand)
  )
    throw new RangeError("Invalid binding-site interval.");
  const span = stretchLength(site.start, site.end, length);
  if (
    !Number.isSafeInteger(offsetFromThreePrime) ||
    offsetFromThreePrime < 1 ||
    offsetFromThreePrime > span
  ) {
    throw new RangeError("Base lies outside the paired interval.");
  }
  const at =
    site.strand === "forward"
      ? site.end - offsetFromThreePrime
      : site.start + offsetFromThreePrime - 2;
  return (((at % length) + length) % length) + 1;
}
