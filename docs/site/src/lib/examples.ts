// The worked examples' values, as the example plugins computed them for this build
// (python -m sitegen generate writes src/generated/examples/). Their shapes are what the
// plugins' own outputs are; only the fields the pages read are named here.

const generated = import.meta.glob<{ default: unknown }>("../generated/examples/*.json", {
  eager: true,
});

function example(name: string): unknown {
  const found = generated[`../generated/examples/${name}.json`];
  if (!found) {
    throw new Error(`No ${name} example: run \`make docs\` (python -m sitegen generate) first`);
  }
  return found.default;
}

export interface Fit {
  fit_status: string;
  endpoint_kind: string;
  endpoint_value: number;
  ci_low: number;
  ci_high: number;
  bottom: number;
  top: number;
  hill_slope: number;
  point_count: number;
  qualifier: string;
  ci_method: string;
  fit_version: string;
  excluded: string[];
}

export interface Reading {
  well: string;
  role: string;
  concentration: number | null;
  concentration_unit: string | null;
  value: number;
  source_row: number;
}

export interface DoseResponse {
  data: { file: string; channel: string; unit: string };
  readings: Reading[];
  exclusion: { well: string; reason: string };
  fits: { all: Fit; excluded: Fit };
}

export interface Difference {
  hgvs: string;
  kind: "substitution" | "deletion" | "insertion" | "deletion_insertion";
  start: number;
  end: number;
  reference: string;
  observed: string;
  length_change: number;
  homopolymer: number;
  features: string[];
  depth: number;
  support: number;
  forward: number;
  reverse: number;
  fraction: number;
}

export interface WholePlasmid {
  type: string;
  values: {
    agreement: "matches" | "differs" | "incomplete";
    reads_total: number;
    reads_aligned: number;
    reference_length: number;
    topology: "circular" | "linear";
    mean_depth: number;
    least_depth: number;
    called_percent: number;
    min_depth: number;
    polishing_rounds: number;
    method: string;
    differences: Difference[];
    features: { type: string; name: string; start: number; end: number; strand: string }[];
    coverage: { bin_size: number; mean: number[]; min: number[] };
    low_coverage: { start: number; end: number }[];
  };
  uncalled: { position: number; base: string; run: number }[];
  data: { reads: string; reference: string; run: string; biosample: string; accession: string };
}

export const doseResponse = example("dose-response") as DoseResponse;
export const puc19 = example("puc19-reads") as WholePlasmid;
