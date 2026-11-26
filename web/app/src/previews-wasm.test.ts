/**
 * The previews' real readers (iki_previews.wasm, built from worker/crates/iki-previews) on
 * real files, through the page's own code: what each viewer is handed is what the page's
 * types say, and the page's steps (a TIFF's layout rounds, an mzML file's index or scan)
 * reach the readers as the readers expect. The files are the readers' own test samples,
 * server/tests/data/previews (their sources and licences are in its README).
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { beforeAll, describe, expect, it } from "vitest";

import { WholeFile, type ByteSource } from "./previews/bytes";
import type { Answers, PreviewReader } from "./previews/reader";
import { elementSpan, findSpectra, MAX_POINTS } from "./previews/spectrum-viewer";
import { MAX_COLUMNS, MAX_ROWS } from "./previews/table-viewer";
import { levelsOf, packTile, readLayout, tileAt, tileRequest } from "./previews/tiff";
import { call, type Exports, type Operation } from "./previews/wasm";

const SAMPLES = resolve(__dirname, "../../../server/tests/data/previews");
const sample = (name: string) => new Uint8Array(readFileSync(resolve(SAMPLES, name)));

let wasm: Exports;

beforeAll(async () => {
  const module = readFileSync(resolve(__dirname, "../wasm/iki_previews.wasm"));
  const { instance } = await WebAssembly.instantiate(module, {});
  wasm = instance.exports as unknown as Exports;
});

/** The worker, without the worker: the real readers, run here. */
function read<O extends Operation>(
  operation: O,
  args: readonly number[],
  input: Uint8Array,
): { value: Answers[O]; pixels: ArrayBuffer | null } {
  const { json, pixels } = call(wasm, operation, args, input);
  const value = JSON.parse(json) as Answers[O] | { error: string };
  if ("error" in value && typeof value.error === "string") throw new Error(value.error);
  return { value: value as Answers[O], pixels };
}

const reader = {
  read: <O extends Operation>(operation: O, args: readonly number[], input: Uint8Array) =>
    Promise.resolve(read(operation, args, input)),
} as unknown as PreviewReader;

/** A file read in ranges, as a large one is, counting what it reads. */
function ranged(bytes: Uint8Array): ByteSource & { reads: [number, number][] } {
  const reads: [number, number][] = [];
  return {
    size: bytes.byteLength,
    verified: false,
    get bytesRead() {
      return reads.reduce((sum, [, length]) => sum + length, 0);
    },
    reads,
    read: (offset: number, length: number) => {
      reads.push([offset, length]);
      return Promise.resolve(bytes.slice(offset, offset + length));
    },
  };
}

const signal = new AbortController().signal;

describe("a Sanger trace", () => {
  it("gives the calls, peaks and qualities Biopython reads", () => {
    const expected = JSON.parse(
      new TextDecoder().decode(sample("biopython-abi-expected.json")),
    ) as Record<string, { sequence: string }>;
    for (const name of ["310.ab1", "3730.ab1"]) {
      const { value } = read("abif", [], sample(name));
      expect(value.bases).toBe(expected[name]?.sequence);
      expect(value.peaks).toHaveLength(value.bases.length);
      expect(value.quality).toHaveLength(value.bases.length);
      for (const base of ["A", "C", "G", "T"] as const) {
        expect(value.traces[base]).toHaveLength(value.points);
      }
      // Each call is at a peak inside the traces, in order.
      expect(
        value.peaks.every(
          (p, i) => p < value.points && (i === 0 || p >= (value.peaks[i - 1] ?? 0)),
        ),
      ).toBe(true);
    }
  });

  it("refuses what isn't one", () => {
    expect(() => read("abif", [], new TextEncoder().encode("ABIF but not really"))).toThrow();
  });
});

describe("a JCAMP-DX file", () => {
  it("gives a continuous spectrum as x and y, and a peak table as peaks", () => {
    const spectrum = read("jcamp", [], sample("jcamp-difdup.dx")).value;
    const [block] = spectrum.blocks;
    expect(block?.kind).toBe("xy");
    expect(block?.x).toHaveLength(block?.y.length ?? -1);
    expect(block?.x.length).toBeGreaterThan(100);
    const peaks = read("jcamp", [], sample("pktab1.jdx")).value;
    expect(peaks.blocks.some((b) => b.kind === "peaks" && b.x.length > 0)).toBe(true);
  });
});

describe("an mzML file", () => {
  it("is read from its index, a spectrum at a time", async () => {
    const bytes = sample("tiny.pwiz.1.1.mzML");
    const source = ranged(bytes);
    const found = await findSpectra(source, reader, signal);
    expect(found.scanned).toBe(false);
    expect(found.index.spectra.length).toBeGreaterThan(0);
    const [first] = found.index.spectra;
    if (!first) throw new Error("no spectrum");
    const span = elementSpan(found, first);
    const element = read(
      "mzml_element",
      [MAX_POINTS],
      bytes.slice(span.offset, span.offset + span.length),
    ).value;
    expect(element.kind).toBe("spectrum");
    if (element.kind !== "spectrum") return;
    expect(element.id).toBe(first.id);
    expect(element.mz).toHaveLength(element.intensity.length);
  });

  it("is scanned when its index points past its end", async () => {
    const found = await findSpectra(
      new WholeFile(sample("pyteomics-test.mzML").buffer),
      reader,
      signal,
    );
    expect(found.scanned).toBe(true);
    expect(found.index.spectra.length).toBeGreaterThan(0);
  });
});

describe("a TIFF", () => {
  it("reads an OME-TIFF pyramid's layout in rounds of ranges, and decodes a tile", async () => {
    const bytes = sample("ome_pyramid.ome.tif");
    const source = ranged(bytes);
    const layout = await readLayout(
      source,
      (input) => Promise.resolve(read("tiff_layout", [], input).value),
      signal,
    );
    expect(layout.ome?.channels.map((c) => c.name)).toEqual(["DAPI", "GFP"]);
    expect(layout.ome?.size_z).toBe(2);
    const [image] = layout.images;
    if (!image) throw new Error("no image");
    expect(image.width).toBe(128);
    expect(levelsOf(image).length).toBe(3);
    // A small file's first read holds all of it: one round.
    expect(source.reads).toEqual([[0, bytes.byteLength]]);

    const tile = tileAt(image, 0, 0);
    const chunk = bytes.slice(
      image.offsets[0],
      (image.offsets[0] ?? 0) + (image.byte_counts[0] ?? 0),
    );
    const { input, requestLength } = packTile(
      tileRequest(image, tile, { jpegTables: null, sample: null, window: null }),
      chunk,
    );
    const decoded = read("tiff_tile", [requestLength], input);
    expect(decoded.value).toMatchObject({ width: 64, height: 64 });
    expect(decoded.pixels?.byteLength).toBe(64 * 64 * 4);
    expect(decoded.value.max).toBeGreaterThan(decoded.value.min);
  });
});

describe("a table", () => {
  it("reads a workbook's saved values, sheet by sheet", () => {
    const { value } = read("xlsx", [0, MAX_ROWS, MAX_COLUMNS], sample("qpcr_plate_results.xlsx"));
    expect(value.sheets.length).toBeGreaterThan(0);
    expect(value.rows.length).toBeGreaterThan(1);
    expect(value.more_rows).toBe(false);
  });

  it("refuses an encrypted workbook, saying why", () => {
    expect(() =>
      read("xlsx", [0, MAX_ROWS, MAX_COLUMNS], sample("calamine_password_protected.xlsx")),
    ).toThrow(/encrypt|password/i);
  });

  it("reads CSV as the importer does: its separator sniffed, a cut-off last row dropped", () => {
    const text = new TextEncoder().encode(
      "well;compound;inhibition_pct\nA1;IKI-0001;12,5\nA2;IKI-0002;87,1\nA3;IKI-00",
    );
    const whole = read("csv", [0, MAX_ROWS, MAX_COLUMNS, 1], text).value;
    expect(whole.rows).toEqual([
      ["well", "compound", "inhibition_pct"],
      ["A1", "IKI-0001", "12,5"],
      ["A2", "IKI-0002", "87,1"],
      // Read whole, a short last row is a row, padded to the table's width.
      ["A3", "IKI-00", ""],
    ]);
    const start = read("csv", [0, MAX_ROWS, MAX_COLUMNS, 0], text).value;
    expect(start.rows).toHaveLength(3);
    expect(start.more_rows).toBe(true);
  });

  it("stops at the rows asked for, and says there are more", () => {
    const lines = ["n", ...Array.from({ length: 200 }, (_, i) => String(i))].join("\n");
    const { value } = read("csv", [0, 100, MAX_COLUMNS, 1], new TextEncoder().encode(lines));
    expect(value.rows).toHaveLength(100);
    expect(value.more_rows).toBe(true);
  });
});
