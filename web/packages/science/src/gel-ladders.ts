export type GelLadderId = "neb-1-kb" | "neb-100-bp";

export type GelLadderBand = Readonly<{
  sizeBp: number;
  /** A second size reported as part of the same band by the manufacturer. */
  companionSizeBp?: number;
  /** Approximate band mass in ng for the manufacturer's 500 ng ladder load. */
  massNg: number;
}>;

export type GelLadder = Readonly<{
  label: string;
  catalogNumber: string;
  sourceUrl: string;
  bands: readonly GelLadderBand[];
}>;

export const GEL_LADDERS: Readonly<Record<GelLadderId, GelLadder>> = {
  "neb-1-kb": {
    label: "NEB 1 kb DNA Ladder",
    catalogNumber: "N3232",
    sourceUrl: "https://www.neb.com/products/n3232-1-kb-dna-ladder",
    bands: [
      { sizeBp: 500, massNg: 21 },
      { sizeBp: 517, massNg: 21 },
      { sizeBp: 1000, massNg: 42 },
      { sizeBp: 1500, massNg: 36 },
      { sizeBp: 2000, massNg: 48 },
      { sizeBp: 3001, massNg: 125 },
      { sizeBp: 4001, massNg: 33 },
      { sizeBp: 5001, massNg: 42 },
      { sizeBp: 6001, massNg: 50 },
      { sizeBp: 8001, massNg: 42 },
      { sizeBp: 10002, massNg: 42 },
    ],
  },
  "neb-100-bp": {
    label: "NEB 100 bp DNA Ladder",
    catalogNumber: "N3231",
    sourceUrl: "https://www.neb.com/products/n3231-100-bp-dna-ladder",
    bands: [
      { sizeBp: 100, massNg: 48 },
      { sizeBp: 200, massNg: 25 },
      { sizeBp: 300, massNg: 29 },
      { sizeBp: 400, massNg: 38 },
      // NEB publishes one mass for the 500/517 bp band.
      { sizeBp: 500, companionSizeBp: 517, massNg: 97 },
      { sizeBp: 600, massNg: 18 },
      { sizeBp: 700, massNg: 21 },
      { sizeBp: 800, massNg: 24 },
      { sizeBp: 900, massNg: 27 },
      { sizeBp: 1000, massNg: 95 },
      { sizeBp: 1200, massNg: 35 },
      { sizeBp: 1517, massNg: 45 },
    ],
  },
};
