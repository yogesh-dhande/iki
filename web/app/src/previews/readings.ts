/** What iki-previews' readers answer: the Rust structs in worker/crates/iki-previews. */

export interface Chromatogram {
  sample: string | null;
  instrument: string | null;
  bases: string;
  peaks: number[];
  quality: number[] | null;
  traces: { A: number[]; C: number[]; G: number[]; T: number[] };
  points: number;
  problems: string[];
}

export interface SpectrumBlock {
  title: string | null;
  data_type: string | null;
  x_units: string | null;
  y_units: string | null;
  kind: "xy" | "peaks";
  x: number[];
  y: number[];
  declared_points: number | null;
}

export interface Spectra {
  title: string | null;
  blocks: SpectrumBlock[];
  truncated: boolean;
  problems: string[];
}

export interface MzmlEntry {
  id: string;
  offset: number;
}

export interface MzmlIndex {
  spectra: MzmlEntry[];
  chromatograms: MzmlEntry[];
  truncated: boolean;
  declared_spectra: number | null;
}

export interface MzmlSpectrum {
  kind: "spectrum";
  id: string;
  index: number | null;
  ms_level: number | null;
  scan_start_minutes: number | null;
  polarity: string | null;
  centroided: boolean | null;
  base_peak_mz: number | null;
  base_peak_intensity: number | null;
  total_ion_current: number | null;
  precursor_mz: number[];
  mz: number[];
  intensity: number[];
  points: number;
  truncated: boolean;
}

export interface MzmlChromatogram {
  kind: "chromatogram";
  id: string;
  chromatogram_type: string | null;
  time_minutes: number[];
  intensity: number[];
  points: number;
  truncated: boolean;
}

export type MzmlElement = MzmlSpectrum | MzmlChromatogram;

export interface Span {
  offset: number;
  length: number;
}

export interface TiffPixels {
  compression: number;
  predictor: number;
  photometric: number;
  bits_per_sample: number;
  samples_per_pixel: number;
  sample_format: number;
  planar: number;
  colormap: number[] | null;
  little_endian: boolean;
}

export interface TiffStored extends TiffPixels {
  width: number;
  height: number;
  tiled: boolean;
  tile_width: number;
  tile_height: number;
  offsets: number[];
  byte_counts: number[];
  jpeg_tables: Span | null;
  unsupported: string | null;
}

export interface TiffImage extends TiffStored {
  index: number;
  levels: TiffStored[];
}

export interface Ome {
  size_x: number;
  size_y: number;
  size_c: number;
  size_z: number;
  size_t: number;
  dimension_order: string;
  channels: { name: string | null; color: number | null }[];
  planes: { c: number; z: number; t: number; ifd: number }[];
  physical_size_x: number | null;
  physical_size_y: number | null;
  physical_size_unit: string | null;
}

export interface TiffLayout {
  bigtiff: boolean;
  images: TiffImage[];
  description: string | null;
  ome: Ome | null;
  truncated: boolean;
}

export type TiffStep = { need: Span[] } | { layout: TiffLayout };

export interface TiffTile {
  width: number;
  height: number;
  min: number;
  max: number;
}

export interface Table {
  sheets: { name: string; hidden: boolean }[];
  sheet: number;
  rows: string[][];
  first_row: number;
  first_column: number;
  columns: number;
  more_rows: boolean;
  more_columns: boolean;
  cut_cells: number;
  encoding: string;
  problems: string[];
}
