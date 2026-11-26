import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ByteSource } from "./previews/bytes";
import type { PreviewReader } from "./previews/reader";
import type {
  Ome,
  TiffImage,
  TiffLayout,
  TiffStep,
  TiffStored,
  TiffTile,
} from "./previews/readings";
import type { StoredBytes } from "./previews/source";
import {
  base64,
  chooseLevel,
  clampView,
  describePixels,
  fitView,
  grid,
  levelsOf,
  Lru,
  needsWindow,
  omePlane,
  physicalSize,
  placement,
  Ranges,
  readLayout,
  TileLoader,
  tilesIn,
  whyUnusable,
  windowFrom,
  zoomAt,
  type Level,
  type Measured,
  type TileJob,
  type TileRequest,
} from "./previews/tiff";
import TiffViewer from "./previews/tiff-viewer";
import { CLEAN_SCAN } from "./test-server";

const MiB = 1024 * 1024;

/** A stored image: 16-bit grey tiles, with offsets and byte counts made up from `base`. */
function stored(over: Partial<TiffStored> = {}, base = 10_000): TiffStored {
  const s: TiffStored = {
    width: 1024,
    height: 1024,
    tiled: true,
    tile_width: 256,
    tile_height: 256,
    compression: 8,
    predictor: 2,
    photometric: 1,
    bits_per_sample: 16,
    samples_per_pixel: 1,
    sample_format: 1,
    planar: 1,
    colormap: null,
    little_endian: true,
    jpeg_tables: null,
    unsupported: null,
    offsets: [],
    byte_counts: [],
    ...over,
  };
  if (!over.offsets) {
    const { across, down } = grid(s);
    const n = across * down * (s.planar === 2 ? s.samples_per_pixel : 1);
    s.offsets = Array.from({ length: n }, (_, i) => base + i * 1000);
    s.byte_counts = Array.from({ length: n }, () => 500);
  }
  return s;
}

function pyramid(index: number, sizes: number[], over: Partial<TiffStored> = {}): TiffImage {
  const [first = 1024, ...rest] = sizes;
  const base = (index + 1) * 10_000_000;
  return {
    ...stored({ width: first, height: first, ...over }, base),
    index,
    levels: rest.map((size, i) =>
      stored({ width: size, height: size, ...over }, base + (i + 1) * 1_000_000),
    ),
  };
}

function ome(over: Partial<Ome> = {}): Ome {
  return {
    size_x: 4096,
    size_y: 4096,
    size_c: 2,
    size_z: 3,
    size_t: 1,
    dimension_order: "XYZCT",
    channels: [
      { name: "DAPI", color: null },
      { name: "GFP", color: null },
    ],
    planes: [],
    physical_size_x: 0.325,
    physical_size_y: 0.325,
    physical_size_unit: "µm",
    ...over,
  };
}

/** A byte source of zeros, as a large ranged file is: reads are recorded. */
function zeros(size: number, verified = false): ByteSource & { reads: [number, number][] } {
  const reads: [number, number][] = [];
  return {
    size,
    verified,
    bytesRead: 0,
    reads,
    read(offset: number, length: number) {
      reads.push([offset, length]);
      return Promise.resolve(new Uint8Array(Math.max(0, Math.min(length, size - offset))));
    },
  };
}

// --- The layout --------------------------------------------------------------------------

describe("the ranges a layout reads", () => {
  it("packs the file's size, then each range's offset, length and bytes, little-endian", () => {
    const ranges = new Ranges();
    ranges.add(0x10, new Uint8Array([7]));
    ranges.add(0, new Uint8Array([1, 2, 3]));
    const packed = ranges.pack(0x1_0000_0002);
    const view = new DataView(packed.buffer);
    expect(packed.byteLength).toBe(12 + 12 + 3 + 12 + 1);
    // [size u64][0 u32]: a BigTIFF's size needs all 64 bits.
    expect(view.getBigUint64(0, true)).toBe(0x1_0000_0002n);
    expect(view.getUint32(8, true)).toBe(0);
    // The ranges in the file's order, whatever order they were read in.
    expect(view.getBigUint64(12, true)).toBe(0n);
    expect(view.getUint32(20, true)).toBe(3);
    expect([...packed.subarray(24, 27)]).toEqual([1, 2, 3]);
    expect(view.getBigUint64(27, true)).toBe(0x10n);
    expect(view.getUint32(35, true)).toBe(1);
    expect(packed[39]).toBe(7);
  });

  it("merges ranges that overlap or touch, and says what's missing", () => {
    const ranges = new Ranges();
    ranges.add(0, new Uint8Array([1, 2]));
    ranges.add(2, new Uint8Array([3, 4]));
    ranges.add(10, new Uint8Array([9, 9]));
    ranges.add(3, new Uint8Array([4, 5, 6]));
    expect(ranges.spans).toEqual([
      { offset: 0, length: 6 },
      { offset: 10, length: 2 },
    ]);
    expect(ranges.total).toBe(8);
    expect(ranges.missing({ offset: 4, length: 10 })).toEqual([
      { offset: 6, length: 4 },
      { offset: 12, length: 2 },
    ]);
    expect(ranges.missing({ offset: 1, length: 3 })).toEqual([]);
    const packed = ranges.pack(100);
    expect([...packed.subarray(24, 30)]).toEqual([1, 2, 3, 4, 5, 6]);
  });
});

describe("reading a layout", () => {
  const layout: TiffLayout = {
    bigtiff: false,
    images: [],
    description: null,
    ome: null,
    truncated: false,
  };

  it("reads the header, then what the reader asks for, until it has the layout", async () => {
    const source = zeros(10 * MiB);
    const inputs: Uint8Array[] = [];
    const steps: TiffStep[] = [
      {
        need: [
          { offset: 1_000_000, length: 200 },
          { offset: 60_000, length: 10_000 },
        ],
      },
      { layout },
    ];
    const read = vi.fn((input: Uint8Array) => {
      inputs.push(input);
      const step = steps[inputs.length - 1];
      if (!step) throw new Error("asked once too often");
      return Promise.resolve(step);
    });
    await expect(readLayout(source, read, new AbortController().signal)).resolves.toBe(layout);
    // The first 64 KiB, then only what wasn't read: 60,000 to 65,536 was.
    expect(source.reads).toEqual([
      [0, 65_536],
      [1_000_000, 200],
      [65_536, 4_464],
    ]);
    const second = new Ranges();
    second.add(0, new Uint8Array(70_000));
    second.add(1_000_000, new Uint8Array(200));
    expect(inputs[1]?.byteLength).toBe(second.pack(10 * MiB).byteLength);
  });

  it("hands a file read whole over whole, in one round", async () => {
    const source = zeros(300_000, true);
    const read = vi.fn(() => Promise.resolve({ layout } as TiffStep));
    await readLayout(source, read, new AbortController().signal);
    expect(source.reads).toEqual([[0, 300_000]]);
    expect(read).toHaveBeenCalledTimes(1);
  });

  it("stops after too many rounds, or too many bytes, or bytes asked for twice", async () => {
    const signal = new AbortController().signal;
    let next = 100_000;
    const endless = () =>
      Promise.resolve({ need: [{ offset: (next += 10), length: 10 }] } as TiffStep);
    await expect(
      readLayout(zeros(10 * MiB), endless, signal, { rounds: 5, bytes: MiB }),
    ).rejects.toThrow("bigger than a preview reads");
    const greedy = () =>
      Promise.resolve({ need: [{ offset: 100_000, length: 2 * MiB }] } as TiffStep);
    await expect(
      readLayout(zeros(10 * MiB), greedy, signal, { rounds: 5, bytes: MiB }),
    ).rejects.toThrow("bigger than a preview reads");
    const again = () => Promise.resolve({ need: [{ offset: 0, length: 8 }] } as TiffStep);
    await expect(readLayout(zeros(10 * MiB), again, signal)).rejects.toThrow("already had");
    const past = () => Promise.resolve({ need: [{ offset: 20 * MiB, length: 8 }] } as TiffStep);
    await expect(readLayout(zeros(10 * MiB), past, signal)).rejects.toThrow("cut short");
  });
});

// --- Levels and tiles --------------------------------------------------------------------

describe("pyramid levels", () => {
  const levels = levelsOf(pyramid(0, [8192, 4096, 2048, 1024]));

  it("draws the coarsest level that still has a pixel for each screen pixel", () => {
    const at = (scale: number) => chooseLevel(levels, scale)?.index;
    expect(at(1)).toBe(0);
    expect(at(4)).toBe(0);
    expect(at(0.5)).toBe(1);
    // 1 / 0.3 = 3.3: level 2 (1:4) would be coarser than the screen; 1:2 isn't.
    expect(at(0.3)).toBe(1);
    expect(at(0.25)).toBe(2);
    expect(at(0.01)).toBe(3);
  });

  it("skips levels that can't be drawn", () => {
    const image = pyramid(0, [8192, 4096, 2048]);
    const [, middle] = image.levels;
    if (middle) middle.unsupported = "Its compression (JPEG 2000) isn’t one Iki reads.";
    const some = levelsOf(image);
    expect(some[2]?.unusable).toContain("JPEG 2000");
    expect(chooseLevel(some, 0.25)?.index).toBe(1);
  });

  it("refuses strips or tiles too big to decode or read", () => {
    const strip = stored({
      width: 40_000,
      height: 40_000,
      tiled: false,
      tile_width: 40_000,
      tile_height: 1000,
    });
    expect(whyUnusable(strip)).toContain("strips of more than 16 megapixels");
    const heavy = stored({ tile_width: 1024, tile_height: 1024 });
    heavy.byte_counts = [65 * MiB];
    expect(whyUnusable(heavy)).toContain("more than 64 MiB");
    const short = stored();
    short.offsets = short.offsets.slice(0, 3);
    expect(whyUnusable(short)).toContain("list of tiles");
    expect(whyUnusable(stored())).toBeNull();
  });
});

describe("the tiles in view", () => {
  const level = (s: TiffStored): Level => ({
    stored: s,
    index: 0,
    downsampleX: 1,
    downsampleY: 1,
    unusable: null,
  });

  it("covers the view, with edge tiles shown only as far as the image goes", () => {
    const image = stored({ width: 1000, height: 700 });
    const all = tilesIn(level(image), { x0: -50, y0: -50, x1: 2000, y1: 2000 });
    expect(all).toHaveLength(4 * 3);
    const corner = all.at(-1);
    expect(corner).toMatchObject({
      index: 11,
      col: 3,
      row: 2,
      x: 768,
      y: 512,
      width: 256,
      height: 256,
    });
    expect(corner).toMatchObject({ shownWidth: 232, shownHeight: 188 });
    // A view that ends exactly on a tile's edge doesn't take the next tile.
    expect(tilesIn(level(image), { x0: 0, y0: 0, x1: 256, y1: 256 }).map((t) => t.index)).toEqual([
      0,
    ]);
    expect(tilesIn(level(image), { x0: 255, y0: 0, x1: 257, y1: 1 }).map((t) => t.index)).toEqual([
      0, 1,
    ]);
    expect(tilesIn(level(image), { x0: 1000, y0: 0, x1: 1100, y1: 10 })).toEqual([]);
  });

  it("treats strips as tiles the image's width, the last with only the rows left", () => {
    const strips = stored({
      width: 1000,
      height: 700,
      tiled: false,
      tile_width: 1000,
      tile_height: 300,
    });
    const all = tilesIn(level(strips), { x0: 0, y0: 0, x1: 1000, y1: 700 });
    expect(all.map((t) => [t.index, t.y, t.width, t.height, t.shownHeight])).toEqual([
      [0, 0, 1000, 300, 300],
      [1, 300, 1000, 300, 300],
      [2, 600, 1000, 100, 100],
    ]);
    expect(
      tilesIn(level(strips), { x0: 400, y0: 310, x1: 500, y1: 320 }).map((t) => t.index),
    ).toEqual([1]);
  });

  it("maps a view onto a reduced level's tiles", () => {
    const levels = levelsOf(pyramid(0, [4096, 1024]));
    const coarse = levels[1];
    if (!coarse) throw new Error("no level");
    // The full image's 1024–2048 is the reduced level's 256–512: its second column.
    expect(tilesIn(coarse, { x0: 1100, y0: 0, x1: 2000, y1: 100 }).map((t) => t.index)).toEqual([
      1,
    ]);
  });

  it("places tiles on whole device pixels, and draws magnified pixels as blocks", () => {
    const levels = levelsOf(pyramid(0, [1024, 512]));
    const coarse = levels[1];
    if (!coarse) throw new Error("no level");
    const view = { cx: 512, cy: 512, scale: 0.3 };
    const frame = { width: 333, height: 333 };
    const [first] = tilesIn(coarse, { x0: 0, y0: 0, x1: 1024, y1: 1024 });
    if (!first) throw new Error("no tile");
    const at = placement(coarse, first, view, frame, 2);
    expect([at.dx, at.dy, at.dw, at.dh].every(Number.isInteger)).toBe(true);
    // 2 full pixels per stored pixel × 0.3 × 2 device pixels: 1.2 device pixels each.
    expect(at.magnified).toBe(true);
    expect(placement(coarse, first, { ...view, scale: 0.2 }, frame, 2).magnified).toBe(false);
  });

  it("zooms round a point and keeps the image in view", () => {
    const image = { width: 1000, height: 1000 };
    const frame = { width: 500, height: 500 };
    const fitted = fitView(image, frame);
    expect(fitted).toEqual({ cx: 500, cy: 500, scale: 0.5 });
    const zoomed = zoomAt(fitted, 2, { x: 0, y: 0 }, image, frame);
    // The image's top-left corner stays under the pointer.
    expect(zoomed).toEqual({ cx: 250, cy: 250, scale: 1 });
    expect(clampView({ cx: -400, cy: 2000, scale: 1 }, image, frame)).toEqual({
      cx: 250,
      cy: 750,
      scale: 1,
    });
    // Smaller than the frame, it's centred.
    expect(clampView({ cx: 0, cy: 0, scale: 0.4 }, image, frame)).toEqual({
      cx: 500,
      cy: 500,
      scale: 0.4,
    });
  });
});

// --- OME-TIFF ----------------------------------------------------------------------------

describe("OME-TIFF planes", () => {
  const grey = { samples_per_pixel: 1, photometric: 1 };
  const sizes = { size_z: 3, size_c: 2, size_t: 4, channels: [] };

  it("follows each DimensionOrder", () => {
    // Channel 1, focal plane 0, time point 2, of 2 channels, 3 planes and 4 time points.
    const expected: Record<string, number> = {
      XYZCT: 0 + 3 * (1 + 2 * 2),
      XYZTC: 0 + 3 * (2 + 4 * 1),
      XYCZT: 1 + 2 * (0 + 3 * 2),
      XYCTZ: 1 + 2 * (2 + 4 * 0),
      XYTCZ: 2 + 4 * (1 + 2 * 0),
      XYTZC: 2 + 4 * (0 + 3 * 1),
    };
    expect(expected).toEqual({ XYZCT: 15, XYZTC: 18, XYCZT: 13, XYCTZ: 5, XYTCZ: 6, XYTZC: 14 });
    for (const [order, ifd] of Object.entries(expected)) {
      expect(omePlane(ome({ ...sizes, dimension_order: order }), 1, 0, 2, grey)).toEqual({
        ifd,
        sample: null,
      });
    }
    expect(omePlane(ome({ ...sizes, dimension_order: "XYZZT" }), 0, 0, 0, grey)).toBeNull();
  });

  it("uses TiffData's planes where OME-XML gives them", () => {
    const given = ome({ planes: [{ c: 1, z: 2, t: 0, ifd: 7 }] });
    expect(omePlane(given, 1, 2, 0, grey)).toEqual({ ifd: 7, sample: null });
    expect(omePlane(given, 0, 0, 0, grey)).toBeNull();
  });

  it("counts an IFD's samples as channels, as Bio-Formats does", () => {
    // RGB: SizeC 3 is one IFD per plane, its samples shown together.
    const rgb = ome({ size_c: 3, size_z: 2, channels: [] });
    expect(omePlane(rgb, 0, 1, 0, { samples_per_pixel: 3, photometric: 2 })).toEqual({
      ifd: 1,
      sample: null,
    });
    // Two grey samples to an IFD: channel 3 is the second IFD's second sample.
    const pairs = ome({ size_c: 4, size_z: 1, dimension_order: "XYCZT", channels: [] });
    expect(omePlane(pairs, 3, 0, 0, { samples_per_pixel: 2, photometric: 1 })).toEqual({
      ifd: 1,
      sample: 1,
    });
  });

  it("says a pixel's physical size", () => {
    expect(physicalSize(ome())).toBe("0.325 µm per pixel");
    expect(physicalSize(ome({ physical_size_y: 0.5, physical_size_unit: null }))).toBe(
      "0.325 × 0.5 µm per pixel",
    );
    expect(physicalSize(ome({ physical_size_x: null }))).toBeNull();
    expect(physicalSize(null)).toBeNull();
  });
});

// --- Contrast ----------------------------------------------------------------------------

function measured(greys: number[], min: number, max: number, inverted = false): Measured {
  const pixels = new Uint8Array(greys.length * 4);
  greys.forEach((g, i) => {
    pixels.set([g, g, g, 255], i * 4);
  });
  return {
    pixels,
    width: greys.length,
    shownWidth: greys.length,
    shownHeight: 1,
    min,
    max,
    inverted,
  };
}

describe("the contrast window", () => {
  it("is needed for more than 8 bits, or floats, but not palettes", () => {
    expect(needsWindow(stored())).toBe(true);
    expect(needsWindow(stored({ bits_per_sample: 8 }))).toBe(false);
    expect(needsWindow(stored({ bits_per_sample: 32, sample_format: 3 }))).toBe(true);
    expect(needsWindow(stored({ photometric: 3 }))).toBe(false);
    expect(describePixels(stored())).toBe("16-bit unsigned, grey");
    expect(describePixels(stored({ bits_per_sample: 32, sample_format: 3 }))).toBe(
      "32-bit floating-point, grey",
    );
  });

  it("clips the darkest and brightest 0.1%, so hot pixels don't wash it out", () => {
    // 10,000 pixels: most from 200 to 600, ten dead and ten hot. Greys are 1/255 of 0–1020.
    const greys = [
      ...Array.from({ length: 9980 }, (_, i) => 50 + (i % 101)),
      ...Array.from({ length: 10 }, () => 0),
      ...Array.from({ length: 10 }, () => 255),
    ];
    const window = windowFrom([measured(greys, 0, 1020)], true);
    expect(window).not.toBeNull();
    const [low, high] = window ?? [0, 0];
    expect(low).toBeGreaterThanOrEqual(199);
    expect(low).toBeLessThanOrEqual(200);
    expect(high).toBeGreaterThanOrEqual(600);
    expect(high).toBeLessThanOrEqual(601);
    // Without clipping, the hot pixels set it.
    expect(windowFrom([measured(greys, 0, 1020)], true, 0)).toEqual([0, 1020]);
  });

  it("combines tiles of different ranges, reads min-is-white inverted, and copes with flat tiles", () => {
    const a = measured(
      Array.from({ length: 2000 }, () => 0),
      100,
      200,
    );
    const b = measured(
      Array.from({ length: 2000 }, () => 0),
      300,
      400,
      true,
    );
    // a is all 100; b (inverted) all 400.
    expect(windowFrom([a, b], true, 0.001)).toEqual([100, 400]);
    expect(windowFrom([measured([7, 7], 42, 42)], true)).toEqual([42, 43]);
    expect(windowFrom([], true)).toBeNull();
  });
});

// --- Caching and loading -----------------------------------------------------------------

describe("the tile cache", () => {
  it("evicts the least recently used past its bytes, and frees what it evicts", () => {
    const freed: string[] = [];
    const lru = new Lru<string>(10, (v) => freed.push(v));
    lru.set("a", "A", 4);
    lru.set("b", "B", 4);
    expect(lru.get("a")).toBe("A");
    lru.set("c", "C", 4);
    expect(freed).toEqual(["B"]);
    expect(lru.has("b")).toBe(false);
    expect(lru.bytes).toBe(8);
    lru.set("a", "A2", 4);
    expect(freed).toEqual(["B", "A"]);
    expect(lru.size).toBe(2);
  });
});

interface Deferred {
  offset: number;
  signal: AbortSignal;
  resolve: () => void;
}

function loaderWith(options: { fetches?: number } = {}) {
  const pending: Deferred[] = [];
  const requests: { request: TileRequest; bytes: number }[] = [];
  const read = vi.fn((offset: number, length: number, signal: AbortSignal) => {
    return new Promise<Uint8Array>((resolve, reject) => {
      signal.addEventListener("abort", () => {
        reject(new DOMException("Aborted", "AbortError"));
      });
      pending.push({
        offset,
        signal,
        resolve: () => {
          resolve(new Uint8Array(length).fill(offset % 251));
        },
      });
    });
  });
  const loader = new TileLoader<{ width: number }>(
    {
      read,
      decode: (input, requestLength) => {
        const request = JSON.parse(
          new TextDecoder().decode(input.subarray(0, requestLength)),
        ) as TileRequest;
        requests.push({ request, bytes: input.byteLength - requestLength });
        const tile: TiffTile = { width: request.width, height: request.height, min: 10, max: 20 };
        return Promise.resolve({
          value: tile,
          pixels: new ArrayBuffer(request.width * request.height * 4),
        });
      },
      bitmap: (_pixels, width) => Promise.resolve({ width }),
      changed: () => undefined,
    },
    { fetches: options.fetches ?? 2, cacheBytes: 64 * MiB },
  );
  return { loader, read, pending, requests };
}

function jobs(
  s: TiffStored,
  indices: number[],
  window: [number, number] | null = [0, 100],
): TileJob[] {
  const level: Level = { stored: s, index: 0, downsampleX: 1, downsampleY: 1, unusable: null };
  const all = tilesIn(level, { x0: 0, y0: 0, x1: s.width, y1: s.height });
  return indices.map((i) => {
    const tile = all[i];
    if (!tile) throw new Error(`no tile ${String(i)}`);
    return { key: `0/0/${String(i)}/-`, stored: s, tile, sample: null, window };
  });
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("loading tiles", () => {
  it("reads a few at a time, and stops reading tiles that leave the view", async () => {
    const { loader, read, pending } = loaderWith({ fetches: 2 });
    const s = stored();
    loader.want(jobs(s, [0, 1, 2, 3]));
    expect(read).toHaveBeenCalledTimes(2);
    expect(loader.loading).toBe(4);
    pending[0]?.resolve();
    await settle();
    expect(read).toHaveBeenCalledTimes(3);
    expect(loader.entry("0/0/0/-")).toMatchObject({
      bitmap: { width: 256 },
      window: "0:100",
      min: 10,
      max: 20,
    });
    // The view moves on: tile 1's read stops, and 2 (still in view) goes on.
    loader.want(jobs(s, [2, 5]));
    expect(pending[1]?.signal.aborted).toBe(true);
    expect(pending[2]?.signal.aborted).toBe(false);
    expect(read.mock.calls.map((c) => c[0])).toEqual([
      s.offsets[0],
      s.offsets[1],
      s.offsets[2],
      s.offsets[5],
    ]);
    await settle();
    expect(loader.problems).toEqual([]);
  });

  it("decodes again for a new window from the bytes it kept, drawing the old meanwhile", async () => {
    const { loader, read, pending, requests } = loaderWith();
    const s = stored();
    loader.want(jobs(s, [0], [0, 100]));
    pending[0]?.resolve();
    await settle();
    expect(requests.map((r) => r.request.window)).toEqual([[0, 100]]);
    loader.want(jobs(s, [0], [5, 50]));
    expect(loader.entry("0/0/0/-")?.window).toBe("0:100");
    await settle();
    expect(read).toHaveBeenCalledTimes(1);
    expect(requests.map((r) => r.request.window)).toEqual([
      [0, 100],
      [5, 50],
    ]);
    expect(loader.entry("0/0/0/-")?.window).toBe("5:50");
  });

  it("asks for the stored tile's size and pixels, JPEG tables once, and each plane's bytes", async () => {
    const { loader, pending, requests, read } = loaderWith({ fetches: 4 });
    const jpeg = stored({
      compression: 7,
      bits_per_sample: 8,
      jpeg_tables: { offset: 50, length: 4 },
      width: 300,
    });
    loader.want(jobs(jpeg, [0, 1]));
    await settle();
    for (const p of [...pending]) p.resolve();
    await settle();
    for (const p of [...pending]) p.resolve();
    await settle();
    expect(read.mock.calls.filter((c) => c[0] === 50)).toHaveLength(1);
    const tables = base64(new Uint8Array(4).fill(50));
    expect(requests.map((r) => [r.request.jpeg_tables, r.request.width, r.request.height])).toEqual(
      [
        [tables, 256, 256],
        [tables, 256, 256],
      ],
    );
    expect(requests[0]?.request).toMatchObject({
      compression: 7,
      little_endian: true,
      sample: null,
      window: [0, 100],
    });
    expect(requests[0]?.request.plane_lengths).toBeUndefined();

    const planar = stored({ photometric: 2, samples_per_pixel: 3, planar: 2, bits_per_sample: 8 });
    const second = loaderWith({ fetches: 4 });
    second.loader.want(jobs(planar, [5]));
    expect(second.read.mock.calls.map((c) => c[0])).toEqual([
      planar.offsets[5],
      planar.offsets[21],
      planar.offsets[37],
    ]);
    for (const p of second.pending) p.resolve();
    await settle();
    expect(second.requests[0]).toMatchObject({
      bytes: 1500,
      request: { plane_lengths: [500, 500, 500] },
    });
  });

  it("reads nothing for a tile the file leaves out, and says why tiles fail", async () => {
    const s = stored();
    s.byte_counts[1] = 0;
    const failing = new TileLoader<object>({
      read: (offset) =>
        offset === s.offsets[2]
          ? Promise.reject(new Error("The file store answered 500."))
          : Promise.resolve(new Uint8Array(500)),
      decode: () =>
        Promise.resolve({
          value: { width: 256, height: 256, min: 0, max: 0 },
          pixels: new ArrayBuffer(10),
        }),
      bitmap: () => Promise.resolve({}),
      changed: () => undefined,
    });
    failing.want(jobs(s, [1, 2, 3]));
    await settle();
    await settle();
    expect(failing.entry("0/0/1/-")).toMatchObject({ bitmap: null });
    expect(failing.problems).toEqual([
      { message: "The file store answered 500.", tiles: 1 },
      // Tile 3's pixels weren't 256 × 256 × 4 bytes.
      { message: "Iki’s TIFF reader decoded a tile to the wrong size.", tiles: 1 },
    ]);
  });
});

// --- The viewer --------------------------------------------------------------------------

const FILE = {
  id: "00000000-0000-0000-0000-00000000f11e",
  name: "hela-dapi-gfp.ome.tif",
  media_type: "image/tiff",
  size: 900 * MiB,
  sha256: "a".repeat(64),
  scan: CLEAN_SCAN,
};

/** The test's stand-in for the store: zeros, in ranges, each recorded. */
function fakeStored(): StoredBytes & { ranges: [number, number][] } {
  const ranges: [number, number][] = [];
  const fake = {
    file: FILE,
    read: 0,
    ranges,
    range(offset: number, length: number) {
      ranges.push([offset, length]);
      fake.read += length;
      return Promise.resolve(new Uint8Array(length));
    },
    whole() {
      return Promise.reject(new Error("A 900 MiB file isn’t read whole."));
    },
  };
  return fake as unknown as StoredBytes & { ranges: [number, number][] };
}

/** A reader that answers a layout after one round, and decodes tiles to a ramp of greys. */
function fakeReader(layout: TiffLayout) {
  const tiles: TileRequest[] = [];
  let rounds = 0;
  const read = vi.fn((operation: string, args: readonly number[], input: Uint8Array) => {
    if (operation === "tiff_layout") {
      rounds++;
      return Promise.resolve({
        value: rounds === 1 ? { need: [{ offset: 200_000, length: 4096 }] } : { layout },
        pixels: null,
      });
    }
    const length = args[0] ?? 0;
    const request = JSON.parse(new TextDecoder().decode(input.subarray(0, length))) as TileRequest;
    tiles.push(request);
    const pixels = new Uint8Array(request.width * request.height * 4);
    for (let i = 0; i < pixels.length; i += 4) pixels[i] = (i / 4) % 256;
    return Promise.resolve({
      value: { width: request.width, height: request.height, min: 100, max: 1120 },
      pixels: pixels.buffer,
    });
  });
  return { reader: { read, close: () => undefined } as unknown as PreviewReader, read, tiles };
}

function omeLayout(): TiffLayout {
  // 2 channels × 3 focal planes, XYZCT: IFD = z + 3c. Each a 4096² pyramid down to 1024².
  const images = Array.from({ length: 6 }, (_, ifd) =>
    pyramid(ifd, [4096, 2048, 1024], { tile_width: 512, tile_height: 512 }),
  );
  return { bigtiff: true, images, description: "<OME …>", ome: ome(), truncated: false };
}

let observed: { width: number; height: number } = { width: 800, height: 600 };

class FakeResizeObserver {
  constructor(private readonly callback: ResizeObserverCallback) {}
  observe() {
    queueMicrotask(() => {
      this.callback([{ contentRect: observed } as ResizeObserverEntry], this);
    });
  }
  disconnect() {
    return undefined;
  }
  unobserve() {
    return undefined;
  }
}

const original = {
  ResizeObserver: globalThis.ResizeObserver,
};

beforeEach(() => {
  observed = { width: 800, height: 600 };
  globalThis.ResizeObserver = FakeResizeObserver;
  // jsdom has no canvas: the viewer draws nothing, and everything else still works.
  HTMLCanvasElement.prototype.getContext = (() => null) as never;
});

afterEach(() => {
  globalThis.ResizeObserver = original.ResizeObserver;
});

function show(layout: TiffLayout) {
  const stored = fakeStored();
  const reader = fakeReader(layout);
  render(
    <TiffViewer
      file={FILE}
      stored={stored}
      reader={reader.reader}
      label="TIFF image"
      viewer="image-tiff"
    />,
  );
  return { stored, ...reader };
}

/** The image IFDs the tiles asked for were read from, as the fake layout numbers offsets. */
function ifdsRead(stored: { ranges: [number, number][] }): number[] {
  return [
    ...new Set(
      stored.ranges.filter(([o]) => o >= 10_000_000).map(([o]) => Math.floor(o / 10_000_000) - 1),
    ),
  ];
}

describe("the TIFF viewer", () => {
  it("shows an OME-TIFF's channels and planes, measures its contrast, and reads only the tiles in view", async () => {
    const { stored, tiles } = show(omeLayout());
    const frame = await screen.findByRole("img", {
      name: /hela-dapi-gfp\.ome\.tif: 4,096 × 4,096 pixels.*at 14\.6%/,
    });
    expect(frame).toHaveAccessibleName(
      "hela-dapi-gfp.ome.tif: 4,096 × 4,096 pixels, channel DAPI, Z 1 of 3, at 14.6%",
    );
    expect(frame).toHaveAttribute("tabindex", "0");
    expect(frame).toHaveAccessibleDescription(/arrow keys/);

    const toolbar = screen.getByRole("toolbar", { name: "View of hela-dapi-gfp.ome.tif" });
    const channel = within(toolbar).getByRole("combobox", { name: "Channel" });
    expect(
      within(channel)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["DAPI", "GFP"]);
    expect(within(toolbar).getByRole("spinbutton", { name: "Z plane" })).toHaveValue(1);
    expect(
      within(toolbar).queryByRole("spinbutton", { name: "Time point" }),
    ).not.toBeInTheDocument();
    for (const name of [
      "Zoom out",
      "Zoom in",
      "Fit the image to the frame",
      "100%: one image pixel to a screen pixel",
    ]) {
      expect(within(toolbar).getByRole("button", { name })).toBeInTheDocument();
    }
    expect(screen.getByText("0.325 µm per pixel")).toBeInTheDocument();
    expect(screen.getByText("16-bit unsigned, grey")).toBeInTheDocument();
    expect(screen.getByText("3 levels, down to 1,024 × 1,024")).toBeInTheDocument();
    expect(screen.getByText("2 channels, 3 Z planes")).toBeInTheDocument();

    // A first window, from the smallest level's middle tiles decoded with their own range...
    const minimum = await screen.findByRole("spinbutton", { name: "Minimum" });
    const maximum = screen.getByRole("spinbutton", { name: "Maximum" });
    expect(screen.getByRole("group", { name: "Display range" })).toBeInTheDocument();
    const measuring = tiles.filter((t) => t.window === null);
    expect(measuring).toHaveLength(4);
    const window: [number, number] = [
      Number((minimum as HTMLInputElement).value),
      Number((maximum as HTMLInputElement).value),
    ];
    expect(window[0]).toBeGreaterThanOrEqual(100);
    expect(window[1]).toBeLessThanOrEqual(1120);
    expect(window[0]).toBeLessThan(window[1]);

    // ...then the view's tiles, all with that window. Fitted at 14.6%, level 3 (1:4) is the
    // coarsest with a pixel per screen pixel: its 2 × 2 tiles, from IFD 0 alone.
    await waitFor(() => {
      expect(tiles.filter((t) => t.window !== null)).toHaveLength(4);
    });
    expect(
      tiles
        .filter((t) => t.window !== null)
        .every((t) => t.window?.[0] === window[0] && t.window[1] === window[1]),
    ).toBe(true);
    expect(tiles.every((t) => t.width === 512 && t.height === 512 && t.little_endian)).toBe(true);
    expect(ifdsRead(stored)).toEqual([0]);
    // The layout's ranges (64 KiB, then what it asked for), and tiles of level 3 only.
    expect(stored.ranges.slice(0, 2)).toEqual([
      [0, 65_536],
      [200_000, 4096],
    ]);
    expect(stored.ranges.slice(2).every(([o]) => o >= 12_000_000 && o < 13_000_000)).toBe(true);
    await screen.findByText("Showing level 3 of 3 (1:4)");
    expect(screen.getByText(/Read in parts/)).toBeInTheDocument();

    // Another channel: XYZCT puts channel 1, plane 0 in IFD 3.
    fireEvent.change(channel, { target: { value: "1" } });
    await waitFor(() => {
      expect(ifdsRead(stored)).toEqual([0, 3]);
    });
    expect(frame).toHaveAccessibleName(/channel GFP, Z 1 of 3/);
    // Plane 3 of channel GFP: IFD 2 + 3.
    fireEvent.change(within(toolbar).getByRole("spinbutton", { name: "Z plane" }), {
      target: { value: "3" },
    });
    await waitFor(() => {
      expect(ifdsRead(stored)).toEqual([0, 3, 5]);
    });
  });

  it("zooms and pans with the keys, reading finer levels as it zooms in", async () => {
    const { stored } = show(omeLayout());
    const frame = await screen.findByRole("img", { name: /at 14.6%/ });
    await screen.findByText("Showing level 3 of 3 (1:4)");
    act(() => {
      frame.focus();
    });
    fireEvent.keyDown(frame, { key: "+" });
    expect(frame).toHaveAccessibleName(/at 29.3%/);
    expect(screen.getByRole("status", { name: "Zoom" })).toHaveTextContent("29.3%");
    await screen.findByText("Showing level 2 of 3 (1:2)");
    expect(stored.ranges.some(([o]) => o >= 11_000_000 && o < 12_000_000)).toBe(true);
    fireEvent.keyDown(frame, { key: "ArrowRight" });
    fireEvent.keyDown(frame, { key: "1" });
    expect(frame).toHaveAccessibleName(/at 100%/);
    await screen.findByText("Showing level 1 of 3 (1:1)");
    // At 100%, an 800 × 600 view of 512-pixel tiles needs at most 3 × 3 of them.
    const full = stored.ranges.filter(([o]) => o >= 10_000_000 && o < 11_000_000);
    expect(full.length).toBeGreaterThan(0);
    expect(full.length).toBeLessThanOrEqual(9);
    fireEvent.keyDown(frame, { key: "0" });
    expect(frame).toHaveAccessibleName(/at 14.6%/);
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(frame).toHaveAccessibleName(/at 29.3%/);
  });

  it("applies a typed display range once typing stops, and refuses one upside down", async () => {
    const { tiles } = show(omeLayout());
    const minimum = await screen.findByRole("spinbutton", { name: "Minimum" });
    const maximum = screen.getByRole("spinbutton", { name: "Maximum" });
    await waitFor(() => {
      expect(tiles.filter((t) => t.window !== null)).toHaveLength(4);
    });
    fireEvent.change(minimum, { target: { value: "5000" } });
    expect(minimum).toHaveAttribute("aria-invalid", "true");
    expect(minimum).toHaveAccessibleDescription(
      "The minimum must be a number less than the maximum.",
    );
    fireEvent.change(minimum, { target: { value: "150" } });
    fireEvent.change(maximum, { target: { value: "900" } });
    expect(minimum).not.toHaveAttribute("aria-invalid");
    await waitFor(() => {
      expect(tiles.filter((t) => t.window?.[0] === 150 && t.window[1] === 900)).toHaveLength(4);
    });
  });

  it("lists a multi-image TIFF's images, and says why one can't be shown", async () => {
    const shown = pyramid(0, [512], { bits_per_sample: 8 });
    const jpeg2000 = {
      ...pyramid(1, [256], { bits_per_sample: 8 }),
      unsupported: "Its compression (JPEG 2000) isn’t one Iki reads.",
    };
    const { tiles } = show({
      bigtiff: false,
      images: [shown, jpeg2000],
      description: null,
      ome: null,
      truncated: true,
    });
    const image = await screen.findByRole("combobox", { name: "Image" });
    expect(
      within(image)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["1: 512 × 512", "2: 256 × 256 (can’t be shown)"]);
    // 8 bits: no window to measure, so no contrast controls.
    expect(screen.queryByRole("group", { name: "Display range" })).not.toBeInTheDocument();
    await waitFor(() => {
      expect(tiles).toHaveLength(4);
    });
    expect(tiles.every((t) => t.window === null)).toBe(true);
    expect(screen.getByText(/past what a preview lists/)).toBeInTheDocument();
    fireEvent.change(image, { target: { value: "1" } });
    expect(
      await screen.findByText(/Iki can’t show this image\. Its compression \(JPEG 2000\)/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Download hela-dapi-gfp.ome.tif" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("opens a huge image without a pyramid at 100%, not fitted, and won't read all of it", async () => {
    // 40,000 × 40,000 in 512-pixel tiles of 500 KB: fitted, all 6,084 tiles would be in view.
    const huge = stored(
      { width: 40_000, height: 40_000, tile_width: 512, tile_height: 512 },
      10_000_000,
    );
    huge.byte_counts = huge.byte_counts.map(() => 500_000);
    const { stored: bytes } = show({
      bigtiff: true,
      images: [{ ...huge, index: 0, levels: [] }],
      description: null,
      ome: null,
      truncated: false,
    });
    const frame = await screen.findByRole("img", { name: /40,000 × 40,000 pixels, at 100%/ });
    await screen.findByText("Showing full resolution");
    expect(bytes.ranges.filter(([o]) => o >= 10_000_000).length).toBeLessThanOrEqual(9 + 9);
    fireEvent.keyDown(frame, { key: "0" });
    expect(
      await screen.findByText(/more than a preview reads at once\. Zoom in to see part of it\./),
    ).toBeInTheDocument();
  });

  it("says why a file has no image it can show", async () => {
    show({ bigtiff: false, images: [], description: null, ome: null, truncated: false });
    expect(
      await screen.findByText("Iki couldn’t find an image in hela-dapi-gfp.ome.tif."),
    ).toBeInTheDocument();
  });
});
