/**
 * File previews (ADR 0226, ADR 0227), against a fake API and a fake file store: which
 * viewer a file gets, that every read starts at the audited content link, that bytes are
 * checked (whole files against their SHA-256, ranges against what was asked), that a file
 * the scan holds shows why and never its bytes, and what each viewer says it left out. The
 * readers' worker is stood in for here; previews-wasm.test.ts runs the real readers.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { entity, renderApp, withTypes } from "./entity-fixtures";
import { summarise, sequenceLines } from "./previews/chromatogram-viewer";
import { highest, lowerBound, sortSeries, tickLabel, ticks } from "./previews/plot";
import { OPEN_WITH_PAGE } from "./previews/preview-places";
import type { Answers } from "./previews/reader";
import type { Chromatogram, Table } from "./previews/readings";
import { parseContentRange, ReadError, StoredBytes, type PreviewFile } from "./previews/source";
import { elementSpan, findSpectra, reversedAxis } from "./previews/spectrum-viewer";
import { columnName, CSV_BYTES, kindOf } from "./previews/table-viewer";
import { answer, calls, page, problem, signedIn } from "./test-server";

// --- The readers' worker, stood in for ---------------------------------------------------

type Reading = {
  [O in keyof Answers]?: (args: readonly number[], input: Uint8Array) => Answers[O];
};
const readers: {
  now: Reading;
  inputs: { operation: string; args: readonly number[]; input: Uint8Array }[];
} = {
  now: {},
  inputs: [],
};

vi.mock("./previews/reader", () => ({
  READ_TIMEOUT_MS: 30_000,
  PreviewReader: class {
    read(operation: keyof Answers, args: readonly number[], input: Uint8Array) {
      readers.inputs.push({ operation, args, input: input.slice() });
      const reader = readers.now[operation] as
        ((args: readonly number[], input: Uint8Array) => unknown) | undefined;
      if (!reader) return Promise.reject(new Error(`No fake reader for ${operation}`));
      try {
        return Promise.resolve({ value: reader(args, input), pixels: null });
      } catch (error) {
        return Promise.reject(error instanceof Error ? error : new Error(String(error)));
      }
    }
    close() {
      return undefined;
    }
  },
}));

beforeEach(() => {
  readers.now = {};
  readers.inputs = [];
});

// jsdom lays nothing out; the table draws the rows that fit in its scrolling region.
const sizes = { clientHeight: 600, clientWidth: 1000, offsetHeight: 600, offsetWidth: 1000 };
const saved: Record<string, PropertyDescriptor | undefined> = {};
beforeAll(() => {
  for (const [name, value] of Object.entries(sizes)) {
    saved[name] = Object.getOwnPropertyDescriptor(HTMLElement.prototype, name);
    Object.defineProperty(HTMLElement.prototype, name, { configurable: true, get: () => value });
  }
});
afterAll(() => {
  for (const [name, descriptor] of Object.entries(saved)) {
    if (descriptor) Object.defineProperty(HTMLElement.prototype, name, descriptor);
  }
});

// --- A file and its store ----------------------------------------------------------------

const ID = "00000000-0000-7000-8000-0000000fc5v1";
const STORE = "https://store.example.com/iki-files/blobs/c5/v1?X-Amz-Signature=x";

async function sha256(bytes: Uint8Array<ArrayBuffer>): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

const PLATE = new TextEncoder().encode(
  "well,compound,inhibition_pct\nA1,IKI-0001,12.5\nA2,IKI-0002,87.1\nA3,IKI-0003,45.0\n",
);

type Detail = Schemas["FileDetailOut"];

function detail(overrides: Partial<Detail> = {}): Detail {
  return {
    id: ID,
    name: "plate-07-inhibition.csv",
    media_type: "text/csv",
    size: PLATE.byteLength,
    sha256: "0".repeat(64),
    project: { id: "00000000-0000-7000-8000-0000000000p1", name: "Kinase", slug: "kinase" },
    created_by: null,
    agent: null,
    created_at: "2026-11-11T09:00:00Z",
    external: false,
    acquired: null,
    used_by: { records: 0, runs: 0 },
    origin: { kind: "upload", run: null, uri: null, version: null },
    samples: [],
    ...overrides,
  };
}

/** The file's page, with its bytes in the store and the link that reads them. */
async function onFile(bytes: Uint8Array<ArrayBuffer>, overrides: Partial<Detail> = {}) {
  signedIn();
  const sha = await sha256(bytes);
  const file = detail({ size: bytes.byteLength, sha256: sha, ...overrides });
  answer(`GET /api/v1/files/${ID}`, file);
  answer(`GET /api/v1/files/${ID}/records`, page([]));
  answer(`GET /api/v1/files/${ID}/runs`, page([]));
  answer(`GET /api/v1/files/${ID}/content-url`, {
    url: STORE,
    expires_at: new Date(Date.now() + 300_000).toISOString(),
    sha256: sha,
  });
  answer("GET /iki-files/blobs/c5/v1", (request: { headers: Headers }) => {
    const range = request.headers.get("Range");
    if (!range) return new Response(bytes.slice());
    const [, first, last] = /bytes=(\d+)-(\d+)/.exec(range) ?? [];
    const from = Number(first);
    const to = Math.min(Number(last), bytes.byteLength - 1);
    return new Response(bytes.slice(from, to + 1), {
      status: 206,
      headers: {
        "Content-Range": `bytes ${String(from)}-${String(to)}/${String(bytes.byteLength)}`,
      },
    });
  });
  return file;
}

function table(rows: string[][], overrides: Partial<Table> = {}): Table {
  return {
    sheets: [],
    sheet: 0,
    rows,
    first_row: 1,
    first_column: 1,
    columns: Math.max(0, ...rows.map((r) => r.length)),
    more_rows: false,
    more_columns: false,
    cut_cells: 0,
    encoding: "UTF-8",
    problems: [],
    ...overrides,
  };
}

const PLATE_ROWS = [
  ["well", "compound", "inhibition_pct"],
  ["A1", "IKI-0001", "12.5"],
  ["A2", "IKI-0002", "87.1"],
  ["A3", "IKI-0003", "45.0"],
];

// --- Reading bytes -----------------------------------------------------------------------

describe("a preview's bytes", () => {
  it("reads Content-Range as a store sends it", () => {
    expect(parseContentRange("bytes 0-99/1234")).toEqual({ first: 0, last: 99, total: 1234 });
    expect(parseContentRange("bytes 100-99/1234")).toBeNull();
    expect(parseContentRange("bytes 0-1234/1234")).toBeNull();
    expect(parseContentRange("bytes */1234")).toBeNull();
    expect(parseContentRange(null)).toBeNull();
  });

  async function stored(bytes: Uint8Array<ArrayBuffer>, recorded?: string): Promise<StoredBytes> {
    const file = await onFile(bytes);
    const preview: PreviewFile = { ...file, sha256: recorded ?? file.sha256 };
    return new StoredBytes(preview);
  }

  it("reads a whole file through the audited link and checks its SHA-256", async () => {
    const bytes = await (await stored(PLATE)).whole(new AbortController().signal);
    expect(new TextDecoder().decode(bytes)).toContain("IKI-0002");
    expect(calls("GET", `/api/v1/files/${ID}/content-url`)).toHaveLength(1);
  });

  it("refuses bytes that don't hash to what Iki recorded", async () => {
    const file = await stored(PLATE);
    answer(`GET /api/v1/files/${ID}/content-url`, {
      url: STORE,
      expires_at: new Date(Date.now() + 300_000).toISOString(),
      sha256: file.file.sha256,
    });
    answer("GET /iki-files/blobs/c5/v1", () => new Response(new TextEncoder().encode("tampered")));
    await expect(file.whole(new AbortController().signal)).rejects.toThrow(ReadError);
  });

  it("refuses a link for other bytes than the file's", async () => {
    const file = await stored(PLATE, "f".repeat(64));
    await expect(file.whole(new AbortController().signal)).rejects.toThrow(ReadError);
  });

  it("reads a range, and checks the store sent the bytes asked for", async () => {
    const file = await stored(PLATE);
    const part = await file.range(5, 8, new AbortController().signal);
    expect(new TextDecoder().decode(part)).toBe("compound");
    const range = calls("GET", "/iki-files/blobs/c5/v1")[0]?.headers.get("Range");
    expect(range).toBe("bytes=5-12");
    // One link serves both reads.
    await file.range(0, 4, new AbortController().signal);
    expect(calls("GET", `/api/v1/files/${ID}/content-url`)).toHaveLength(1);
  });

  it("says what to do when the store hides which bytes it sent", async () => {
    const file = await stored(PLATE);
    answer("GET /iki-files/blobs/c5/v1", () => new Response(PLATE.slice(0, 8), { status: 206 }));
    await expect(file.range(0, 8, new AbortController().signal)).rejects.toThrow(
      /iki-server setup_storage/,
    );
  });

  it("refuses a range of a file of another size, or a whole file sent for a range", async () => {
    const file = await stored(PLATE);
    answer(
      "GET /iki-files/blobs/c5/v1",
      () =>
        new Response(PLATE.slice(0, 8), {
          status: 206,
          headers: { "Content-Range": "bytes 0-7/9999" },
        }),
    );
    await expect(file.range(0, 8, new AbortController().signal)).rejects.toThrow(ReadError);
    answer("GET /iki-files/blobs/c5/v1", () => new Response(PLATE.slice()));
    await expect(file.range(0, 8, new AbortController().signal)).rejects.toThrow(ReadError);
  });
});

// --- Where previews open -----------------------------------------------------------------

describe("a file's page", () => {
  it("shows a CSV file as a table, read through the audited link", async () => {
    await onFile(PLATE);
    readers.now.csv = () => table(PLATE_ROWS);
    await renderApp(`/files/${ID}/`);
    const grid = await screen.findByRole("table", { name: "plate-07-inhibition.csv" });
    expect(
      within(grid)
        .getAllByRole("columnheader")
        .map((h) => h.textContent),
    ).toEqual(["Row", "well", "compound", "inhibition_pct"]);
    expect(within(grid).getByRole("row", { name: /IKI-0002/ })).toHaveTextContent("A2IKI-000287.1");
    expect(screen.getByText(/^4 rows\. Read as UTF-8\./)).toBeInTheDocument();
    expect(screen.getByText(/checked against its SHA-256/)).toBeInTheDocument();
    expect(calls("GET", `/api/v1/files/${ID}/content-url`)).toHaveLength(1);
    // Read whole, it's read as complete; sniffing the separator (0) as the importer does.
    expect(readers.inputs[0]?.args).toEqual([0, 50_000, 500, 1]);
  });

  it("reads a large CSV file's start only, and says so", async () => {
    const big = new Uint8Array(CSV_BYTES + 1024).fill(0x41);
    await onFile(big);
    readers.now.csv = () => table(PLATE_ROWS, { more_rows: true });
    await renderApp(`/files/${ID}/`);
    await screen.findByRole("table", { name: "plate-07-inhibition.csv" });
    expect(readers.inputs[0]?.input.byteLength).toBe(CSV_BYTES);
    expect(readers.inputs[0]?.args.at(-1)).toBe(0);
    expect(screen.getByText(/Showing the 4 rows in its first 16 MiB/)).toBeInTheDocument();
    expect(screen.getByText(/Read in parts/)).toBeInTheDocument();
    expect(calls("GET", "/iki-files/blobs/c5/v1")[0]?.headers.get("Range")).toBe(
      `bytes=0-${String(CSV_BYTES - 1)}`,
    );
  });

  it("names a workbook's sheets, hidden ones too, and reads the one chosen", async () => {
    await onFile(PLATE, {
      name: "plate-07.xlsx",
      media_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    const sheets = [
      { name: "Results", hidden: false },
      { name: "Raw", hidden: true },
    ];
    readers.now.xlsx = (args) =>
      table(args[0] === 1 ? [["raw", "1"]] : PLATE_ROWS, { sheets, sheet: args[0] ?? 0 });
    const { user } = await renderApp(`/files/${ID}/`);
    await screen.findByRole("table", { name: "plate-07.xlsx, sheet Results" });
    // A workbook's columns are its letters: its first row is data, not a header.
    expect(screen.getByRole("columnheader", { name: "A" })).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Sheet" }), "Raw (hidden)");
    await screen.findByRole("table", { name: "plate-07.xlsx, sheet Raw" });
    expect(readers.inputs.map((i) => i.args[0])).toEqual([0, 1]);
  });

  it("shows why a file the scan holds can't be shown, with no download and no read", async () => {
    await onFile(PLATE, {
      scan: { state: "infected", usable: false },
    } as Partial<Detail>);
    await renderApp(`/files/${ID}/`);
    expect(await screen.findByText(/This file is quarantined/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Download plate-07/ })).not.toBeInTheDocument();
    expect(calls("GET", `/api/v1/files/${ID}/content-url`)).toHaveLength(0);
  });

  it("shows what the server says when it refuses the link for a held file", async () => {
    await onFile(PLATE);
    answer(
      `GET /api/v1/files/${ID}/content-url`,
      problem(409, "file_scanning", "This file is being scanned. Try again shortly."),
    );
    await renderApp(`/files/${ID}/`);
    expect(await screen.findByText("This file is being scanned. Try again shortly.")).toBeVisible();
    expect(screen.queryByRole("link", { name: /Download plate-07/ })).not.toBeInTheDocument();
  });

  it("offers the download for a format Iki doesn't show", async () => {
    await onFile(PLATE, { name: "notes.json", media_type: "application/json" });
    await renderApp(`/files/${ID}/`);
    expect(await screen.findByText(/doesn’t show JSON files in the browser/)).toBeVisible();
    expect(screen.getByRole("link", { name: "Download notes.json" })).toHaveAttribute(
      "href",
      `/api/v1/files/${ID}/content`,
    );
    expect(calls("GET", `/api/v1/files/${ID}/content-url`)).toHaveLength(0);
  });

  it("asks before reading a large file whole, and says a preview is recorded as a download", async () => {
    const big = new Uint8Array(OPEN_WITH_PAGE + 1);
    await onFile(big, {
      name: "plate-07.xlsx",
      media_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    readers.now.xlsx = () => table(PLATE_ROWS);
    const { user } = await renderApp(`/files/${ID}/`);
    expect(await screen.findByText(/recorded as one/)).toBeVisible();
    expect(calls("GET", `/api/v1/files/${ID}/content-url`)).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Show table" }));
    await screen.findByRole("table", { name: "plate-07.xlsx" });
    expect(calls("GET", `/api/v1/files/${ID}/content-url`)).toHaveLength(1);
  });

  it("says when a file is past what its viewer opens", async () => {
    await onFile(new Uint8Array(8), {
      name: "plate-07.xlsx",
      media_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      size: 65 * 1024 * 1024,
    });
    await renderApp(`/files/${ID}/`);
    expect(await screen.findByText(/bigger than the preview opens \(64 MiB\)/)).toBeVisible();
  });

  it("says what a reader couldn't read, and offers the download", async () => {
    await onFile(PLATE);
    readers.now.csv = () => {
      throw new ReadError("It isn’t text in an encoding Iki reads.");
    };
    await renderApp(`/files/${ID}/`);
    expect(
      await screen.findByText(/couldn’t show plate-07-inhibition.csv. It isn’t text/),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "Download plate-07-inhibition.csv" })).toBeVisible();
  });
});

describe("a record's file", () => {
  it("opens its preview in a dialog beside its link", async () => {
    await onFile(PLATE);
    readers.now.csv = () => table(PLATE_ROWS);
    withTypes();
    answer(
      "GET /api/v1/entities/PST-000001",
      entity({
        hrid: "PST-000001",
        type: "iki/protein.ProteinStructure",
        values: { name: "Crambin", structure: ID },
        files: {
          [ID]: { name: "plate-07-inhibition.csv", media_type: "text/csv", size: PLATE.byteLength },
        },
      }),
    );
    const { user } = await renderApp("/entities/PST-000001/");
    await user.click(
      await screen.findByRole("button", { name: "Preview plate-07-inhibition.csv" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "plate-07-inhibition.csv" });
    await within(dialog).findByRole("table", { name: "plate-07-inhibition.csv" });
    expect(within(dialog).getByRole("link", { name: "Open its page" })).toHaveAttribute(
      "href",
      `/files/${ID}/`,
    );
    expect(calls("GET", `/api/v1/files/${ID}`)).toHaveLength(1);
    expect(calls("GET", `/api/v1/files/${ID}/content-url`)).toHaveLength(1);
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("has no preview button for a format Iki doesn't show", async () => {
    signedIn();
    withTypes();
    answer(
      "GET /api/v1/entities/PST-000001",
      entity({
        hrid: "PST-000001",
        type: "iki/protein.ProteinStructure",
        values: { name: "Crambin", structure: ID },
        files: { [ID]: { name: "notes.json", media_type: "application/json", size: 10 } },
      }),
    );
    await renderApp("/entities/PST-000001/");
    await screen.findByRole("link", { name: "notes.json" });
    expect(screen.queryByRole("button", { name: /^Preview/ })).not.toBeInTheDocument();
  });
});

// --- What the viewers work out -----------------------------------------------------------

describe("the viewers' sums", () => {
  it("names spreadsheet columns", () => {
    expect([1, 26, 27, 52, 703].map(columnName)).toEqual(["A", "Z", "AA", "AZ", "AAA"]);
    expect(kindOf("table-xlsx")).toBe("xlsx");
    expect(kindOf("table-tsv")).toBe("tsv");
    expect(kindOf("table-csv")).toBe("csv");
  });

  it("summarises a Sanger read as a sequencing core does", () => {
    const read: Chromatogram = {
      sample: "pUC19-M13F",
      instrument: null,
      bases: "ACGTN",
      peaks: [5, 15, 25, 35, 45],
      quality: [40, 35, 25, 15, 2],
      traces: { A: [], C: [], G: [], T: [] },
      points: 50,
      problems: [],
    };
    expect(summarise(read)).toEqual({ bases: 5, mean: 23.4, q20: 3, q30: 2 });
    expect(summarise({ ...read, quality: null }).mean).toBeNull();
    expect(sequenceLines("ACGT".repeat(20))).toEqual([
      { start: 1, text: "ACGTACGTAC GTACGTACGT ACGTACGTAC GTACGTACGT ACGTACGTAC GTACGTACGT" },
      { start: 61, text: "ACGTACGTAC GTACGTACGT" },
    ]);
  });

  it("plots in round steps, and finds a spectrum's highest peaks", () => {
    expect(ticks(0, 10, 5)).toEqual([0, 2, 4, 6, 8, 10]);
    expect(ticks(400, 4000, 6)).toEqual([1000, 2000, 3000, 4000]);
    expect(tickLabel(2.5e7)).toBe("2.5e7");
    const sorted = sortSeries({ x: [300, 100, 200], y: [5, 9, 1], kind: "sticks" });
    expect(Array.from(sorted.x)).toEqual([100, 200, 300]);
    expect(Array.from(sorted.y)).toEqual([9, 1, 5]);
    expect(lowerBound(sorted.x, 150)).toBe(1);
    expect(highest(sorted, 2)).toEqual([0, 2]);
  });

  it("draws IR wavenumbers and NMR shifts high to low", () => {
    expect(reversedAxis("1/CM", "INFRARED SPECTRUM")).toBe(true);
    expect(reversedAxis("PPM", "NMR SPECTRUM")).toBe(true);
    expect(reversedAxis("NANOMETERS", "UV/VIS SPECTRUM")).toBe(false);
  });
});

describe("an mzML file's spectra", () => {
  const source = (size: number) => ({
    size,
    verified: false,
    bytesRead: 0,
    read: vi.fn((offset: number, length: number) =>
      Promise.resolve(new Uint8Array(Math.max(0, Math.min(length, size - offset)))),
    ),
  });

  it("reads its end, then its index: never the whole file", async () => {
    const file = source(2_000_000_000);
    readers.now.mzml_offset = () => ({ index_offset: 1_999_000_000 });
    readers.now.mzml_index = () => ({
      spectra: [
        { id: "scan=1", offset: 4_000 },
        { id: "scan=2", offset: 90_000 },
      ],
      chromatograms: [{ id: "TIC", offset: 1_998_000_000 }],
      truncated: false,
      declared_spectra: 2,
    });
    const { PreviewReader } = await import("./previews/reader");
    const found = await findSpectra(file, new PreviewReader(), new AbortController().signal);
    expect(file.read.mock.calls.map(([o, l]) => [o, l])).toEqual([
      [2_000_000_000 - 65_536, 65_536],
      [1_999_000_000, 1_000_000],
    ]);
    expect(found.scanned).toBe(false);
    // A spectrum ends where the next begins; the last element, where the index does.
    expect(elementSpan(found, { id: "scan=1", offset: 4_000 })).toEqual({
      offset: 4_000,
      length: 86_000,
    });
    expect(elementSpan(found, { id: "TIC", offset: 1_998_000_000 })).toEqual({
      offset: 1_998_000_000,
      length: 1_000_000,
    });
  });

  it("scans the start of a file with no index", async () => {
    const file = source(100_000_000);
    readers.now.mzml_offset = () => ({ index_offset: null });
    readers.now.mzml_scan = () => ({
      spectra: [{ id: "scan=1", offset: 2_000 }],
      chromatograms: [],
      truncated: true,
      declared_spectra: 4_000,
    });
    const { PreviewReader } = await import("./previews/reader");
    const found = await findSpectra(file, new PreviewReader(), new AbortController().signal);
    expect(found.scanned).toBe(true);
    expect(found.end).toBe(64 * 1024 * 1024);
    expect(readers.inputs.at(-1)?.args).toEqual([0]);
  });
});
