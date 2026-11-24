import { GEL_LADDERS, type GelLadderBand, type GelLadderId } from "./gel-ladders.js";

export interface GelFragment {
  readonly sizeBp: number;
  readonly massNg?: number;
}

export type GelLane =
  | { readonly ladder: GelLadderId; readonly label?: string }
  | {
      readonly label: string;
      readonly fragments: readonly GelFragment[];
      readonly totalMassNg?: number;
    };

export interface VirtualGelData {
  readonly label?: string;
  readonly agarosePercent: number;
  readonly lanes: readonly GelLane[];
}

export interface GelBand {
  sizeBp: number;
  companionSizeBp?: number;
  count: number;
  massNg: number | undefined;
  weight: number;
}

export interface GelLaneLayout {
  label: string;
  sourceUrl?: string;
  bands: GelBand[];
}

export const GEL_AGAROSE_RANGE = { min: 0.5, max: 3 } as const;

// Effective separation ranges, in bp, for TopVision agarose (Thermo Fisher,
// MAN0012614, Table 1). Use these ranges to set the span of the schematic.
// https://assets.thermofisher.com/TFS-Assets/LSG/manuals/MAN0012614_Gen_Recommend_DNA_Electrophoresis_UG.pdf
const separationRanges = [
  { percent: GEL_AGAROSE_RANGE.min, low: 2000, high: 50000 },
  { percent: 0.6, low: 1000, high: 20000 },
  { percent: 0.7, low: 800, high: 12000 },
  { percent: 0.8, low: 800, high: 10000 },
  { percent: 0.9, low: 600, high: 10000 },
  { percent: 1, low: 400, high: 8000 },
  { percent: 1.2, low: 300, high: 7000 },
  { percent: 1.5, low: 200, high: 3000 },
  { percent: 2, low: 100, high: 2000 },
  { percent: GEL_AGAROSE_RANGE.max, low: 25, high: 1000 },
] as const;

export function migrationAt(percent: number): (sizeBp: number) => number {
  const upper = separationRanges.findIndex((range) => range.percent >= percent);
  const a = separationRanges[upper - 1] ?? separationRanges[0];
  const b = separationRanges[upper] ?? separationRanges[separationRanges.length - 1] ?? a;
  const fraction = a.percent === b.percent ? 0 : (percent - a.percent) / (b.percent - a.percent);
  const low = Math.log(a.low) * (1 - fraction) + Math.log(b.low) * fraction;
  const high = Math.log(a.high) * (1 - fraction) + Math.log(b.high) * fraction;
  // Use the conventional semilog relation (Southern, 1979) as a near-linear
  // center. The display maps the range endpoints to 20% and 80%, with
  // smoothly compressed tails so out-of-range bands remain represented.
  // https://doi.org/10.1016/0003-2697(79)90235-5
  return (sizeBp) => {
    const relative = (high - Math.log(sizeBp)) / (high - low);
    return 1 / (1 + 4 ** (1 - 2 * relative));
  };
}

function nonNegative(value: number | undefined): value is number {
  return value !== undefined && Number.isFinite(value) && value >= 0;
}

function sampleBands(fragments: readonly GelFragment[], totalMassNg?: number): GelBand[] {
  const valid = fragments.filter(({ sizeBp }) => Number.isSafeInteger(sizeBp) && sizeBp > 0);
  const allMeasured = valid.every(({ massNg }) => nonNegative(massNg));
  const totalBp = valid.reduce((sum, { sizeBp }) => sum + sizeBp, 0);
  const bands = new Map<number, GelBand>();
  for (const { sizeBp, massNg } of valid) {
    // For equimolar linear dsDNA fragments, approximate their mass fraction
    // by their base-pair fraction. Without a total, keep relative weights.
    const mass = allMeasured
      ? massNg
      : nonNegative(totalMassNg)
        ? totalMassNg * (sizeBp / totalBp)
        : undefined;
    const group = bands.get(sizeBp);
    if (group) {
      group.count++;
      group.weight += mass ?? sizeBp;
      if (group.massNg !== undefined && mass !== undefined) group.massNg += mass;
    } else {
      bands.set(sizeBp, { sizeBp, count: 1, massNg: mass, weight: mass ?? sizeBp });
    }
  }
  return [...bands.values()].sort((a, b) => b.sizeBp - a.sizeBp);
}

function visibleLadderBands(id: GelLadderId): readonly GelLadderBand[] {
  const { bands } = GEL_LADDERS[id];
  if (id !== "neb-1-kb") return bands;
  // The N3232 property table specifies two fragments, 10a and 10b. The
  // manufacturer photographs them as one band. Keep both source masses.
  // https://www.neb.com/products/n3232-1-kb-dna-ladder
  const small = bands.find(({ sizeBp }) => sizeBp === 500);
  const large = bands.find(({ sizeBp }) => sizeBp === 517);
  if (!small || !large) return bands;
  return [
    { sizeBp: small.sizeBp, companionSizeBp: large.sizeBp, massNg: small.massNg + large.massNg },
    ...bands.filter((band) => band !== small && band !== large),
  ];
}

export function gelLanes(lanes: readonly GelLane[]): GelLaneLayout[] {
  return lanes.map((lane) => {
    if ("ladder" in lane) {
      const ladder = GEL_LADDERS[lane.ladder];
      return {
        label: lane.label ?? ladder.label,
        sourceUrl: ladder.sourceUrl,
        bands: visibleLadderBands(lane.ladder)
          .map((band) => ({
            ...band,
            count: band.companionSizeBp === undefined ? 1 : 2,
            weight: band.massNg,
          }))
          .sort((a, b) => b.sizeBp - a.sizeBp),
      };
    }
    return { label: lane.label, bands: sampleBands(lane.fragments, lane.totalMassNg) };
  });
}
