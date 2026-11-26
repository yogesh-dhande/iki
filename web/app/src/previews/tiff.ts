/**
 * A TIFF's preview without reading the file (ADR 0227): its layout is read from ranges the
 * reader asks for, then only the tiles (or strips) in view, at the coarsest pyramid level
 * that still shows every screen pixel. Everything here is the page's arithmetic and
 * bookkeeping, apart from the canvas, so it can be tested without one.
 */

import type { ByteSource } from "./bytes";
import type {
  Ome,
  Span,
  TiffImage,
  TiffLayout,
  TiffPixels,
  TiffStep,
  TiffStored,
  TiffTile,
} from "./readings";
import { ReadError } from "./source";

const MiB = 1024 * 1024;

/** What the layout is first given: a header and, in most files, the first IFD. */
export const FIRST_READ = 64 * 1024;
/** The most rounds of "read these, then ask again" a layout may take. */
export const MAX_LAYOUT_ROUNDS = 64;
/** The most bytes of structure (IFDs, tile lists, OME-XML) a layout may read. */
export const MAX_LAYOUT_BYTES = 64 * MiB;
/** The most pixels one tile or strip may be, as the reader also refuses (`MAX_TILE_PIXELS`). */
export const MAX_TILE_PIXELS = 16 * 1024 * 1024;
/** The most compressed bytes one tile or strip may be. */
export const MAX_TILE_BYTES = 64 * MiB;
/** Decoded tiles kept, as RGBA bytes. */
export const TILE_CACHE_BYTES = 256 * MiB;
/** Tiles' bytes kept as read, so a change of contrast decodes them again without reading. */
export const RAW_CACHE_BYTES = 64 * MiB;
/** Tiles read at once. */
export const MAX_FETCHES = 4;
/** The most of the file one view may need: past it, the view asks to be zoomed in instead. */
export const MAX_VIEW_BYTES = 128 * MiB;
/** The most tiles one view may need. */
export const MAX_VIEW_TILES = 1024;

// --- The layout --------------------------------------------------------------------------

/** The bytes read of a file so far, as non-overlapping ranges in order. */
export class Ranges {
  private parts: { offset: number; bytes: Uint8Array }[] = [];

  /** How many bytes are held. */
  get total(): number {
    return this.parts.reduce((sum, part) => sum + part.bytes.byteLength, 0);
  }

  get spans(): Span[] {
    return this.parts.map((part) => ({ offset: part.offset, length: part.bytes.byteLength }));
  }

  /** Adds bytes read from `offset`, merging them with any they overlap or touch. */
  add(offset: number, bytes: Uint8Array): void {
    if (bytes.byteLength === 0) return;
    let start = offset;
    let end = offset + bytes.byteLength;
    const touching = this.parts.filter(
      (part) => part.offset <= end && part.offset + part.bytes.byteLength >= start,
    );
    if (!touching.length) {
      // Kept as read, not copied: a file read whole is handed over in one range.
      this.parts = [...this.parts, { offset, bytes }].sort((a, b) => a.offset - b.offset);
      return;
    }
    for (const part of touching) {
      start = Math.min(start, part.offset);
      end = Math.max(end, part.offset + part.bytes.byteLength);
    }
    const merged = new Uint8Array(end - start);
    for (const part of touching) merged.set(part.bytes, part.offset - start);
    // The new bytes last: where they overlap old ones, they're the same bytes of the file.
    merged.set(bytes, offset - start);
    this.parts = [
      ...this.parts.filter((part) => !touching.includes(part)),
      { offset: start, bytes: merged },
    ].sort((a, b) => a.offset - b.offset);
  }

  /** The parts of `span` not held yet. */
  missing(span: Span): Span[] {
    const gaps: Span[] = [];
    let at = span.offset;
    const end = span.offset + span.length;
    for (const part of this.parts) {
      const partEnd = part.offset + part.bytes.byteLength;
      if (partEnd <= at) continue;
      if (part.offset >= end) break;
      if (part.offset > at) gaps.push({ offset: at, length: part.offset - at });
      at = Math.max(at, partEnd);
      if (at >= end) break;
    }
    if (at < end) gaps.push({ offset: at, length: end - at });
    return gaps;
  }

  /** The ranges as the layout reads them (`tiff::Ranges`): the file's size, then each range. */
  pack(size: number): Uint8Array {
    const out = new Uint8Array(
      12 + this.parts.reduce((n, part) => n + 12 + part.bytes.byteLength, 0),
    );
    const view = new DataView(out.buffer);
    view.setBigUint64(0, BigInt(size), true);
    view.setUint32(8, 0, true);
    let at = 12;
    for (const part of this.parts) {
      view.setBigUint64(at, BigInt(part.offset), true);
      view.setUint32(at + 8, part.bytes.byteLength, true);
      out.set(part.bytes, at + 12);
      at += 12 + part.bytes.byteLength;
    }
    return out;
  }
}

/** Runs the layout reader on packed ranges. */
export type LayoutReader = (input: Uint8Array) => Promise<TiffStep>;

const LAYOUT_TOO_BIG =
  "Its structure (its image directories and tile lists) is bigger than a preview reads. Download it to open it in Fiji or QuPath.";

/**
 * Reads a TIFF's layout: the header first, then whatever the reader asks for, until it has
 * all of it. A file read whole is handed over whole, so it takes one round.
 */
export async function readLayout(
  source: ByteSource,
  read: LayoutReader,
  signal: AbortSignal,
  limits: { rounds: number; bytes: number } = {
    rounds: MAX_LAYOUT_ROUNDS,
    bytes: MAX_LAYOUT_BYTES,
  },
): Promise<TiffLayout> {
  const ranges = new Ranges();
  const first = source.verified ? source.size : Math.min(source.size, FIRST_READ);
  ranges.add(0, await source.read(0, first, signal));
  for (let round = 0; round < limits.rounds; round++) {
    const step = await read(ranges.pack(source.size));
    if ("layout" in step) return step.layout;
    const asked = step.need
      .map((span) => ({
        offset: span.offset,
        length: Math.max(0, Math.min(span.offset + span.length, source.size) - span.offset),
      }))
      .filter((span) => span.length > 0);
    if (asked.length === 0) {
      throw new ReadError(
        "Its structure points past the end of the file: it may have been cut short.",
      );
    }
    const spans = asked.flatMap((span) => ranges.missing(span));
    if (spans.length === 0) {
      // The reader asked for bytes it was given: asking again would never end.
      throw new ReadError(
        "Iki’s TIFF reader asked again for bytes it already had, so Iki stopped.",
      );
    }
    const adding = spans.reduce((sum, span) => sum + span.length, 0);
    if (ranges.total + adding > limits.bytes) throw new ReadError(LAYOUT_TOO_BIG);
    const parts = await Promise.all(
      spans.map((span) => source.read(span.offset, span.length, signal)),
    );
    parts.forEach((bytes, i) => {
      const span = spans[i];
      if (span) ranges.add(span.offset, bytes);
    });
  }
  throw new ReadError(LAYOUT_TOO_BIG);
}

// --- Pyramid levels ----------------------------------------------------------------------

/** One level of an image's pyramid. */
export interface Level {
  stored: TiffStored;
  /** Its place in the pyramid, from 0 (full resolution). */
  index: number;
  /** How many full-resolution pixels each of its pixels covers, across and down. */
  downsampleX: number;
  downsampleY: number;
  /** Why it can't be drawn, if it can't. */
  unusable: string | null;
}

/** A level's tiles across and down. */
export function grid(stored: TiffStored): { across: number; down: number } {
  const across = stored.tiled ? Math.ceil(stored.width / Math.max(1, stored.tile_width)) : 1;
  const down = Math.ceil(stored.height / Math.max(1, stored.tile_height));
  return { across, down };
}

/** How many tiles one plane holds, and how many planes the offsets cover. */
function planes(stored: TiffStored): number {
  return stored.planar === 2 ? Math.max(1, stored.samples_per_pixel) : 1;
}

/** Why a stored image can't be drawn tile by tile, or null. */
export function whyUnusable(stored: TiffStored): string | null {
  if (stored.unsupported) return stored.unsupported;
  if (stored.width <= 0 || stored.height <= 0) return "It has no pixels.";
  if (stored.tile_width <= 0 || stored.tile_height <= 0) return "Its tiles have no size.";
  const { across, down } = grid(stored);
  if (
    stored.offsets.length < across * down * planes(stored) ||
    stored.byte_counts.length < stored.offsets.length
  ) {
    return "Its list of tiles is longer than a preview reads.";
  }
  const kind = stored.tiled ? "tiles" : "strips";
  if (stored.tile_width * stored.tile_height > MAX_TILE_PIXELS) {
    return `It’s stored in ${kind} of more than 16 megapixels each, more than a preview decodes. Download it to open it in Fiji or QuPath.`;
  }
  const perPlane = across * down;
  let biggest = 0;
  for (let i = 0; i < perPlane; i++) {
    let bytes = 0;
    for (let p = 0; p < planes(stored); p++) bytes += stored.byte_counts[p * perPlane + i] ?? 0;
    biggest = Math.max(biggest, bytes);
  }
  if (biggest > MAX_TILE_BYTES) {
    return `It’s stored in ${kind} of more than 64 MiB each, more than a preview reads. Download it to open it in Fiji or QuPath.`;
  }
  return null;
}

/** An image's levels, largest first: itself, then its reduced levels. */
export function levelsOf(image: TiffImage): Level[] {
  return [image, ...image.levels].map((stored, index) => ({
    stored,
    index,
    downsampleX: image.width / Math.max(1, stored.width),
    downsampleY: image.height / Math.max(1, stored.height),
    unusable: whyUnusable(stored),
  }));
}

/**
 * The level to draw at `scale` device pixels per full-resolution pixel: the coarsest one
 * whose pixels are still no bigger than the screen's, or the finest there is when zoomed in
 * past it. Only levels that can be drawn are chosen; undefined if none can.
 */
export function chooseLevel(levels: readonly Level[], scale: number): Level | undefined {
  const usable = levels.filter((level) => level.unusable === null);
  const enough = usable.filter((level) => level.downsampleX <= (1 / scale) * 1.0001);
  if (enough.length) {
    return enough.reduce((best, level) => (level.downsampleX > best.downsampleX ? level : best));
  }
  return usable.reduce<Level | undefined>(
    (best, level) => (!best || level.downsampleX < best.downsampleX ? level : best),
    undefined,
  );
}

// --- The view ----------------------------------------------------------------------------

/** Where the view is: its centre, in full-resolution pixels, and CSS pixels per image pixel. */
export interface View {
  cx: number;
  cy: number;
  scale: number;
}

export interface Size {
  width: number;
  height: number;
}

/** The scale at which the whole image fits the frame. */
export function fitScale(image: Size, frame: Size): number {
  if (!frame.width || !frame.height) return 1;
  return Math.min(frame.width / image.width, frame.height / image.height);
}

/** The scales a person can zoom between. */
export function scaleLimits(image: Size, frame: Size): { min: number; max: number } {
  const fit = fitScale(image, frame);
  return { min: Math.min(fit / 2, 1), max: Math.max(64, fit) };
}

/** The whole image, fitted and centred. */
export function fitView(image: Size, frame: Size): View {
  return { cx: image.width / 2, cy: image.height / 2, scale: fitScale(image, frame) };
}

/** Keeps the view on the image: centred where the image is smaller than the frame. */
export function clampView(view: View, image: Size, frame: Size): View {
  const { min, max } = scaleLimits(image, frame);
  const scale = Math.min(max, Math.max(min, view.scale));
  const axis = (centre: number, length: number, room: number) => {
    const half = room / (2 * scale);
    if (length * scale <= room) return length / 2;
    return Math.min(length - half, Math.max(half, centre));
  };
  return {
    scale,
    cx: axis(view.cx, image.width, frame.width),
    cy: axis(view.cy, image.height, frame.height),
  };
}

/** Zooms by `factor` keeping the image point under `at` (CSS pixels in the frame) still. */
export function zoomAt(
  view: View,
  factor: number,
  at: { x: number; y: number },
  image: Size,
  frame: Size,
): View {
  const { min, max } = scaleLimits(image, frame);
  const scale = Math.min(max, Math.max(min, view.scale * factor));
  const ix = view.cx + (at.x - frame.width / 2) / view.scale;
  const iy = view.cy + (at.y - frame.height / 2) / view.scale;
  return clampView(
    {
      scale,
      cx: ix - (at.x - frame.width / 2) / scale,
      cy: iy - (at.y - frame.height / 2) / scale,
    },
    image,
    frame,
  );
}

/** Moves the view by CSS pixels on screen. */
export function pan(view: View, dx: number, dy: number, image: Size, frame: Size): View {
  return clampView(
    { ...view, cx: view.cx - dx / view.scale, cy: view.cy - dy / view.scale },
    image,
    frame,
  );
}

/** What part of the image is in view, in full-resolution pixels. */
export function visibleRect(
  view: View,
  frame: Size,
): { x0: number; y0: number; x1: number; y1: number } {
  const halfW = frame.width / (2 * view.scale);
  const halfH = frame.height / (2 * view.scale);
  return { x0: view.cx - halfW, y0: view.cy - halfH, x1: view.cx + halfW, y1: view.cy + halfH };
}

/** One tile or strip of a level. */
export interface TileRef {
  /** Its place in a plane's tiles, row by row. */
  index: number;
  col: number;
  row: number;
  /** Its top-left corner in the level's pixels. */
  x: number;
  y: number;
  /** Its size as stored, as the reader is told it. */
  width: number;
  height: number;
  /** How much of it is the image: tiles at the right and bottom edges are padded. */
  shownWidth: number;
  shownHeight: number;
}

/** The tile at a column and row. */
export function tileAt(stored: TiffStored, col: number, row: number): TileRef {
  const { across } = grid(stored);
  const x = col * stored.tile_width;
  const y = row * stored.tile_height;
  const shownWidth = Math.min(stored.tile_width, stored.width - x);
  const shownHeight = Math.min(stored.tile_height, stored.height - y);
  return {
    index: row * across + col,
    col,
    row,
    x,
    y,
    // A tile is whole even at the edge; the last strip has only the rows left.
    width: stored.tiled ? stored.tile_width : stored.width,
    height: stored.tiled ? stored.tile_height : shownHeight,
    shownWidth,
    shownHeight,
  };
}

/** The tiles of a level that a rectangle of the full-resolution image overlaps. */
export function tilesIn(
  level: Level,
  rect: { x0: number; y0: number; x1: number; y1: number },
): TileRef[] {
  const { stored } = level;
  const { across, down } = grid(stored);
  const x0 = rect.x0 / level.downsampleX;
  const x1 = rect.x1 / level.downsampleX;
  const y0 = rect.y0 / level.downsampleY;
  const y1 = rect.y1 / level.downsampleY;
  if (x1 <= 0 || y1 <= 0 || x0 >= stored.width || y0 >= stored.height) return [];
  const tw = stored.tiled ? stored.tile_width : stored.width;
  const c0 = Math.max(0, Math.floor(x0 / tw));
  const c1 = Math.min(across - 1, Math.ceil(x1 / tw) - 1);
  const r0 = Math.max(0, Math.floor(y0 / stored.tile_height));
  const r1 = Math.min(down - 1, Math.ceil(y1 / stored.tile_height) - 1);
  const tiles: TileRef[] = [];
  for (let row = r0; row <= r1; row++) {
    for (let col = c0; col <= c1; col++) tiles.push(tileAt(stored, col, row));
  }
  return tiles;
}

/** Which of the file's chunks hold a tile: one, or one per sample where planes are apart. */
export function chunksOf(stored: TiffStored, tile: number): number[] {
  const { across, down } = grid(stored);
  const perPlane = across * down;
  return Array.from({ length: planes(stored) }, (_, plane) => plane * perPlane + tile);
}

/** The compressed bytes a set of tiles is. */
export function bytesOf(stored: TiffStored, tiles: readonly TileRef[]): number {
  let sum = 0;
  for (const tile of tiles) {
    for (const chunk of chunksOf(stored, tile.index)) sum += stored.byte_counts[chunk] ?? 0;
  }
  return sum;
}

/** Where a tile goes on the canvas, in whole device pixels so neighbours meet without seams. */
export function placement(
  level: Level,
  tile: TileRef,
  view: View,
  frame: Size,
  ratio: number,
): { dx: number; dy: number; dw: number; dh: number; magnified: boolean } {
  const rect = visibleRect(view, frame);
  const left = (tile.x * level.downsampleX - rect.x0) * view.scale * ratio;
  const top = (tile.y * level.downsampleY - rect.y0) * view.scale * ratio;
  const right = ((tile.x + tile.shownWidth) * level.downsampleX - rect.x0) * view.scale * ratio;
  const bottom = ((tile.y + tile.shownHeight) * level.downsampleY - rect.y0) * view.scale * ratio;
  const dx = Math.round(left);
  const dy = Math.round(top);
  return {
    dx,
    dy,
    dw: Math.round(right) - dx,
    dh: Math.round(bottom) - dy,
    // Past one device pixel per stored pixel, each pixel is drawn as a block, not blurred.
    magnified: level.downsampleX * view.scale * ratio > 1,
  };
}

// --- OME-TIFF planes ---------------------------------------------------------------------

/** How many IFDs a time point and focal plane take: OME's SizeC counts samples. */
export function omeChannels(ome: Ome, samplesPerPixel: number): number {
  const spp = Math.max(1, samplesPerPixel);
  return ome.size_c % spp === 0 ? Math.max(1, ome.size_c / spp) : Math.max(1, ome.size_c);
}

/**
 * Which IFD holds channel `c`, focal plane `z` and time point `t`, and which of its samples
 * the channel is when an IFD holds several (as Bio-Formats reads them). From `TiffData`
 * where OME-XML gives it, else from `DimensionOrder`. Null if it names none.
 */
export function omePlane(
  ome: Ome,
  c: number,
  z: number,
  t: number,
  pixels: Pick<TiffPixels, "samples_per_pixel" | "photometric">,
): { ifd: number; sample: number | null } | null {
  const spp = Math.max(1, pixels.samples_per_pixel);
  const grouped = spp > 1 && ome.size_c % spp === 0;
  const channel = grouped ? Math.floor(c / spp) : c;
  // An RGB IFD shows its samples together; other samples are shown one at a time.
  const sample = grouped && pixels.photometric !== 2 ? c % spp : null;
  if (ome.planes.length) {
    const plane = ome.planes.find((p) => p.c === channel && p.z === z && p.t === t);
    return plane ? { ifd: plane.ifd, sample } : null;
  }
  const order = ome.dimension_order.toUpperCase();
  const dims = order.slice(2);
  if (
    !order.startsWith("XY") ||
    dims.length !== 3 ||
    new Set(dims).size !== 3 ||
    /[^ZCT]/.test(dims)
  ) {
    return null;
  }
  const at: Record<string, number> = { Z: z, C: channel, T: t };
  const size: Record<string, number> = {
    Z: Math.max(1, ome.size_z),
    C: grouped ? omeChannels(ome, spp) : Math.max(1, ome.size_c),
    T: Math.max(1, ome.size_t),
  };
  const [a, b, d] = dims.split("") as [string, string, string];
  const index = (at[a] ?? 0) + (size[a] ?? 1) * ((at[b] ?? 0) + (size[b] ?? 1) * (at[d] ?? 0));
  return { ifd: index, sample };
}

/** "0.325 µm per pixel", or "0.325 × 0.33 µm per pixel", or null. */
export function physicalSize(ome: Ome | null): string | null {
  const sizeX = ome?.physical_size_x ?? null;
  if (!ome || sizeX === null) return null;
  const unit = ome.physical_size_unit ?? "µm";
  const number = (n: number) =>
    new Intl.NumberFormat(undefined, { maximumSignificantDigits: 4 }).format(n);
  const x = number(sizeX);
  const y = ome.physical_size_y === null ? x : number(ome.physical_size_y);
  return x === y ? `${x} ${unit} per pixel` : `${x} × ${y} ${unit} per pixel`;
}

// --- Pixels and contrast -----------------------------------------------------------------

/** Whether tiles need one window for black and white, so neighbouring tiles match. */
export function needsWindow(pixels: TiffPixels): boolean {
  return pixels.photometric !== 3 && (pixels.bits_per_sample > 8 || pixels.sample_format === 3);
}

/** Whether a grey image's samples are shown one at a time. */
export function samplesShownApart(pixels: TiffPixels): boolean {
  return pixels.samples_per_pixel > 1 && (pixels.photometric === 0 || pixels.photometric === 1);
}

/** "16-bit unsigned, grey" */
export function describePixels(pixels: TiffPixels): string {
  const format =
    pixels.sample_format === 3
      ? "floating-point"
      : pixels.sample_format === 2
        ? "signed"
        : "unsigned";
  const colour: Record<number, string> = { 0: "grey", 1: "grey", 2: "RGB", 3: "palette" };
  const kind = colour[pixels.photometric] ?? `photometric ${String(pixels.photometric)}`;
  const samples =
    pixels.samples_per_pixel > 1 && pixels.photometric !== 2
      ? `, ${String(pixels.samples_per_pixel)} samples`
      : "";
  return `${String(pixels.bits_per_sample)}-bit ${format}, ${kind}${samples}`;
}

/** A tile decoded with its own minimum and maximum as black and white, to measure. */
export interface Measured {
  pixels: Uint8Array;
  /** Its width as stored, and how much of it is the image. */
  width: number;
  shownWidth: number;
  shownHeight: number;
  min: number;
  max: number;
  /** Min-is-white: bright is low. */
  inverted: boolean;
}

const BINS = 4096;

/**
 * Black and white for a set of tiles, clipping the darkest and brightest 0.1% as image
 * viewers' auto contrast does, so a few hot pixels don't wash the image out. Each tile came
 * back as 8 bits between its own minimum and maximum, so values are known to 1/255 of that
 * range: enough to choose a window, which is all this is for. Null if no pixels were seen.
 */
export function windowFrom(
  tiles: readonly Measured[],
  integer: boolean,
  clip = 0.001,
): [number, number] | null {
  const seen = tiles.filter(
    (t) =>
      Number.isFinite(t.min) && Number.isFinite(t.max) && t.shownWidth > 0 && t.shownHeight > 0,
  );
  if (!seen.length) return null;
  const low = Math.min(...seen.map((t) => t.min));
  const high = Math.max(...seen.map((t) => t.max));
  let window: [number, number];
  if (high <= low) {
    window = [low, low + 1];
  } else {
    const bins = new Float64Array(BINS);
    let total = 0;
    const span = high - low;
    for (const tile of seen) {
      for (let y = 0; y < tile.shownHeight; y++) {
        for (let x = 0; x < tile.shownWidth; x++) {
          const grey = tile.pixels[(y * tile.width + x) * 4] ?? 0;
          const value =
            tile.min + ((tile.inverted ? 255 - grey : grey) / 255) * (tile.max - tile.min);
          const bin = Math.min(BINS - 1, Math.max(0, Math.floor(((value - low) / span) * BINS)));
          bins[bin] = (bins[bin] ?? 0) + 1;
          total++;
        }
      }
    }
    let lowBin = 0;
    let highBin = BINS - 1;
    let sum = 0;
    for (let i = 0; i < BINS; i++) {
      sum += bins[i] ?? 0;
      if (sum > total * clip) {
        lowBin = i;
        break;
      }
    }
    sum = 0;
    for (let i = BINS - 1; i >= 0; i--) {
      sum += bins[i] ?? 0;
      if (sum > total * clip) {
        highBin = i;
        break;
      }
    }
    window = [low + (lowBin / BINS) * span, low + ((highBin + 1) / BINS) * span];
    if (!(window[1] > window[0])) window = [low, high];
  }
  if (integer) {
    window = [Math.floor(window[0]), Math.ceil(window[1])];
    if (window[1] <= window[0]) window[1] = window[0] + 1;
  }
  return window;
}

// --- Caching -----------------------------------------------------------------------------

/** A least-recently-used cache, bounded by bytes. */
export class Lru<V> {
  private readonly items = new Map<string, { value: V; bytes: number }>();
  private held = 0;

  constructor(
    private readonly max: number,
    private readonly evicted: (value: V) => void = () => undefined,
  ) {}

  get bytes(): number {
    return this.held;
  }

  get size(): number {
    return this.items.size;
  }

  has(key: string): boolean {
    return this.items.has(key);
  }

  /** The value, now the most recently used. */
  get(key: string): V | undefined {
    const item = this.items.get(key);
    if (!item) return undefined;
    this.items.delete(key);
    this.items.set(key, item);
    return item.value;
  }

  set(key: string, value: V, bytes: number): void {
    this.delete(key);
    this.items.set(key, { value, bytes });
    this.held += bytes;
    for (const [oldest, item] of this.items) {
      if (this.held <= this.max || oldest === key) break;
      this.items.delete(oldest);
      this.held -= item.bytes;
      this.evicted(item.value);
    }
  }

  delete(key: string): void {
    const item = this.items.get(key);
    if (!item) return;
    this.items.delete(key);
    this.held -= item.bytes;
    this.evicted(item.value);
  }

  clear(): void {
    for (const key of [...this.items.keys()]) this.delete(key);
  }
}

// --- Reading tiles -----------------------------------------------------------------------

/** What `tiff_tile` is asked to decode (`tiff::TileRequest`). */
export interface TileRequest extends TiffPixels {
  width: number;
  height: number;
  jpeg_tables: string | null;
  sample: number | null;
  window: [number, number] | null;
  /** Where planes are stored apart, each plane's bytes' length, in order: they follow
   * one after another. */
  plane_lengths?: number[];
}

/** The request for one tile. */
export function tileRequest(
  stored: TiffStored,
  tile: TileRef,
  options: {
    jpegTables: string | null;
    sample: number | null;
    window: [number, number] | null;
    planeLengths?: number[];
  },
): TileRequest {
  const request: TileRequest = {
    compression: stored.compression,
    predictor: stored.predictor,
    photometric: stored.photometric,
    bits_per_sample: stored.bits_per_sample,
    samples_per_pixel: stored.samples_per_pixel,
    sample_format: stored.sample_format,
    planar: stored.planar,
    colormap: stored.colormap,
    little_endian: stored.little_endian,
    width: tile.width,
    height: tile.height,
    jpeg_tables: options.jpegTables,
    sample: options.sample,
    window: options.window,
  };
  if (options.planeLengths) request.plane_lengths = options.planeLengths;
  return request;
}

/** `tiff_tile`'s input: the request's JSON, then the tile's bytes. And the JSON's length. */
export function packTile(
  request: TileRequest,
  bytes: Uint8Array,
): { input: Uint8Array; requestLength: number } {
  const json = new TextEncoder().encode(JSON.stringify(request));
  const input = new Uint8Array(json.byteLength + bytes.byteLength);
  input.set(json, 0);
  input.set(bytes, json.byteLength);
  return { input, requestLength: json.byteLength };
}

export function base64(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.byteLength; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

/** One tile the view wants. */
export interface TileJob {
  /** The tile, whatever its contrast: image, level, tile and sample. */
  key: string;
  stored: TiffStored;
  tile: TileRef;
  sample: number | null;
  window: [number, number] | null;
}

/** A decoded tile, ready to draw. */
export interface Decoded<B> {
  /** What draws it, or null where there's nothing to draw (an empty tile, or no canvas). */
  bitmap: B | null;
  /** The window it was decoded with: a tile of another is shown until it's decoded again. */
  window: string;
  min: number;
  max: number;
}

export function windowKey(window: [number, number] | null): string {
  return window ? `${String(window[0])}:${String(window[1])}` : "own";
}

export interface LoaderDeps<B> {
  read: (offset: number, length: number, signal: AbortSignal) => Promise<Uint8Array>;
  decode: (
    input: Uint8Array,
    requestLength: number,
  ) => Promise<{ value: TiffTile; pixels: ArrayBuffer | null }>;
  /** Makes something drawable of RGBA pixels, or null if the page can't draw. */
  bitmap: (
    pixels: Uint8ClampedArray<ArrayBuffer>,
    width: number,
    height: number,
  ) => Promise<B | null>;
  /** Frees it, when it's evicted. */
  release?: (bitmap: B) => void;
  /** Called when a tile arrives, fails or the work changes. */
  changed: () => void;
}

/** Whether a signal has fired: a call, so a check after an `await` isn't narrowed away. */
function isAborted(signal: AbortSignal): boolean {
  return signal.aborted;
}

function aborted(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/**
 * Reads, decodes and keeps the tiles a view wants, a few at a time, nearest the centre
 * first. Tiles that leave the view stop being read; ones decoded with an earlier contrast
 * are drawn until they're decoded again, from bytes kept, not read again.
 */
export class TileLoader<B> {
  private readonly decoded: Lru<Decoded<B>>;
  private readonly raw = new Lru<Uint8Array>(RAW_CACHE_BYTES);
  private readonly tables = new Map<number, Promise<string>>();
  private queue: TileJob[] = [];
  private readonly running = new Map<string, { job: TileJob; abort: AbortController }>();
  private readonly failures = new Map<string, string>();
  private closed = false;

  constructor(
    private readonly deps: LoaderDeps<B>,
    private readonly limits: { fetches: number; cacheBytes: number } = {
      fetches: MAX_FETCHES,
      cacheBytes: TILE_CACHE_BYTES,
    },
  ) {
    this.decoded = new Lru<Decoded<B>>(limits.cacheBytes, (entry) => {
      if (entry.bitmap !== null) deps.release?.(entry.bitmap);
    });
  }

  /** A tile as decoded last, with whichever window. */
  entry(key: string): Decoded<B> | undefined {
    return this.decoded.get(key);
  }

  /** Tiles waiting or being read. */
  get loading(): number {
    return this.queue.length + this.running.size;
  }

  /** Why tiles couldn't be read: each different reason once, and how many tiles. */
  get problems(): { message: string; tiles: number }[] {
    const counts = new Map<string, number>();
    for (const message of this.failures.values())
      counts.set(message, (counts.get(message) ?? 0) + 1);
    return [...counts].map(([message, tiles]) => ({ message, tiles }));
  }

  /**
   * What the view wants now, nearest first. Reads under way for tiles in `keep` (the view
   * and a margin round it) go on, so a small pan doesn't throw away bytes already paid for.
   */
  want(
    jobs: readonly TileJob[],
    keep: ReadonlySet<string> = new Set(),
    window: [number, number] | null = jobs[0]?.window ?? null,
  ): void {
    if (this.closed) return;
    const wanted = new Set(jobs.map((job) => job.key));
    const current = windowKey(window);
    for (const [key, { job, abort }] of this.running) {
      // A read for another contrast is decoded again anyway; one far off isn't needed.
      if ((wanted.has(key) || keep.has(key)) && windowKey(job.window) === current) continue;
      abort.abort();
      this.running.delete(key);
    }
    this.queue = jobs.filter((job) => {
      const failed = this.failures.has(`${job.key}@${windowKey(job.window)}`);
      const fresh = this.decoded.get(job.key)?.window === windowKey(job.window);
      return !failed && !fresh && !this.running.has(job.key);
    });
    this.pump();
    this.deps.changed();
  }

  private pump(): void {
    while (!this.closed && this.running.size < this.limits.fetches) {
      const job = this.queue.shift();
      if (!job) return;
      const abort = new AbortController();
      this.running.set(job.key, { job, abort });
      void this.load(job, abort.signal).finally(() => {
        if (this.running.get(job.key)?.abort === abort) this.running.delete(job.key);
        this.pump();
        if (!this.closed) this.deps.changed();
      });
    }
  }

  private async load(job: TileJob, signal: AbortSignal): Promise<void> {
    try {
      const { value, pixels, empty } = await this.decode(job, job.window, signal);
      if (signal.aborted || this.closed) return;
      const bitmap =
        empty || !pixels
          ? null
          : await this.deps.bitmap(new Uint8ClampedArray(pixels), value.width, value.height);
      if (this.isClosed()) {
        if (bitmap !== null) this.deps.release?.(bitmap);
        return;
      }
      this.decoded.set(
        job.key,
        { bitmap, window: windowKey(job.window), min: value.min, max: value.max },
        pixels?.byteLength ?? 64,
      );
    } catch (error) {
      if (aborted(error) || signal.aborted || this.closed) return;
      this.failures.set(
        `${job.key}@${windowKey(job.window)}`,
        error instanceof Error ? error.message : String(error),
      );
    }
  }

  /** A tile's bytes as stored, from those kept or the file. */
  private async bytes(
    job: TileJob,
    signal: AbortSignal,
  ): Promise<{ parts: Uint8Array[]; empty: boolean }> {
    const { stored, tile } = job;
    const chunks = chunksOf(stored, tile.index);
    // A byte count of 0 is a tile the file leaves out (a sparse TIFF): there's nothing there.
    if (chunks.some((chunk) => !stored.byte_counts[chunk])) return { parts: [], empty: true };
    const parts = await Promise.all(
      chunks.map(async (chunk) => {
        const offset = stored.offsets[chunk] ?? 0;
        const length = stored.byte_counts[chunk] ?? 0;
        const key = `${String(offset)}+${String(length)}`;
        const kept = this.raw.get(key);
        if (kept) return kept;
        const read = await this.deps.read(offset, length, signal);
        if (read.byteLength !== length)
          throw new ReadError("A tile ends past the end of the file.");
        this.raw.set(key, read, read.byteLength);
        return read;
      }),
    );
    return { parts, empty: false };
  }

  private jpegTables(stored: TiffStored, signal: AbortSignal): Promise<string> | null {
    const span = stored.jpeg_tables;
    if (stored.compression !== 7 || !span) return null;
    let tables = this.tables.get(span.offset);
    if (!tables) {
      tables = this.deps.read(span.offset, span.length, signal).then(base64);
      // A failed read is tried again by the next tile, not kept.
      tables.catch(() => this.tables.delete(span.offset));
      this.tables.set(span.offset, tables);
    }
    return tables;
  }

  /** Reads and decodes one tile with a window, without keeping what it decodes. */
  private async decode(
    job: TileJob,
    window: [number, number] | null,
    signal: AbortSignal,
  ): Promise<{ value: TiffTile; pixels: ArrayBuffer | null; empty: boolean }> {
    const { parts, empty } = await this.bytes(job, signal);
    if (empty)
      return {
        value: { width: job.tile.width, height: job.tile.height, min: NaN, max: NaN },
        pixels: null,
        empty,
      };
    const jpegTables = (await this.jpegTables(job.stored, signal)) ?? null;
    const joined = new Uint8Array(parts.reduce((n, part) => n + part.byteLength, 0));
    let at = 0;
    for (const part of parts) {
      joined.set(part, at);
      at += part.byteLength;
    }
    const request = tileRequest(job.stored, job.tile, {
      jpegTables,
      sample: job.sample,
      window,
      ...(job.stored.planar === 2 && parts.length > 1
        ? { planeLengths: parts.map((p) => p.byteLength) }
        : {}),
    });
    const { input, requestLength } = packTile(request, joined);
    const { value, pixels } = await this.deps.decode(input, requestLength);
    if (
      value.width !== job.tile.width ||
      value.height !== job.tile.height ||
      pixels?.byteLength !== value.width * value.height * 4
    ) {
      throw new ReadError("Iki’s TIFF reader decoded a tile to the wrong size.");
    }
    return { value, pixels, empty: false };
  }

  /** Tiles decoded with their own black and white, to choose a window from. */
  async measure(jobs: readonly TileJob[], signal: AbortSignal): Promise<Measured[]> {
    const measured: Measured[] = [];
    for (const job of jobs) {
      if (signal.aborted) break;
      try {
        const { value, pixels, empty } = await this.decode(job, null, signal);
        if (empty || !pixels) continue;
        measured.push({
          pixels: new Uint8Array(pixels),
          width: value.width,
          shownWidth: job.tile.shownWidth,
          shownHeight: job.tile.shownHeight,
          min: value.min,
          max: value.max,
          inverted: job.stored.photometric === 0,
        });
      } catch (error) {
        if (aborted(error) || isAborted(signal)) throw error;
        // A tile that doesn't decode is left out of the measure, and fails again when drawn.
      }
    }
    return measured;
  }

  /** Forgets tiles that failed, so they're tried again. */
  retry(): void {
    this.failures.clear();
  }

  /** A call, so a check after an `await` isn't narrowed away. */
  private isClosed(): boolean {
    return this.closed;
  }

  close(): void {
    this.closed = true;
    for (const { abort } of this.running.values()) abort.abort();
    this.running.clear();
    this.queue = [];
    this.decoded.clear();
    this.raw.clear();
  }
}

/** The tiles nearest the centre of a level, for measuring its contrast. */
export function centreTiles(level: Level, count: number): TileRef[] {
  const { across, down } = grid(level.stored);
  const cx = (across - 1) / 2;
  const cy = (down - 1) / 2;
  const all: TileRef[] = [];
  // Only the middle of a big grid is looked at: the rest can't be nearer.
  const reach = Math.ceil(Math.sqrt(count)) + 1;
  for (
    let row = Math.max(0, Math.floor(cy) - reach);
    row <= Math.min(down - 1, Math.ceil(cy) + reach);
    row++
  ) {
    for (
      let col = Math.max(0, Math.floor(cx) - reach);
      col <= Math.min(across - 1, Math.ceil(cx) + reach);
      col++
    ) {
      all.push(tileAt(level.stored, col, row));
    }
  }
  return all
    .sort((a, b) => (a.col - cx) ** 2 + (a.row - cy) ** 2 - ((b.col - cx) ** 2 + (b.row - cy) ** 2))
    .slice(0, count);
}

/** The smallest level that can be drawn: the cheapest to measure. */
export function smallestLevel(levels: readonly Level[]): Level | undefined {
  return levels
    .filter((level) => level.unusable === null)
    .reduce<Level | undefined>(
      (best, level) =>
        !best || level.stored.width * level.stored.height < best.stored.width * best.stored.height
          ? level
          : best,
      undefined,
    );
}
