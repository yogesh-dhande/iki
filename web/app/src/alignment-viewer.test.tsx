/** The alignment viewer: rows linked to the sequences they are, a pinned reference, a cursor
 * the keys move, and the sequences picked to run an action on. iki-wasm's worker is stood in
 * for here; the browser tests run the real one. */

import type { Schemas } from "@ikisci/sdk";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { colourOf, moved, residueNumber, shownRows } from "./alignment";
import { entity, PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import type { Reading } from "./reading";
import { answer } from "./test-server";
import { readViewState } from "./views";

const read = vi.hoisted(() =>
  vi.fn<(bytes: ArrayBuffer, alphabet?: string | null) => Promise<Reading>>(),
);
vi.mock("./reading", async (original) => ({
  ...(await original<typeof import("./reading")>()),
  readInWorker: read,
}));

const FILE = "0b6f1c1e-5d2c-4a51-9a63-2c1f6a1b9e02";
const ABL1 = "00000000-0000-7000-8000-00000000a001";
const ABL2 = "00000000-0000-7000-8000-00000000a002";
const FASTA = new TextEncoder().encode(">PRT-000001\nMK-LV\n>PRT-000002\nMKALV\n>read_7\nMR-LV\n");

function field(overrides: Partial<Schemas["FieldOut"]>): Schemas["FieldOut"] {
  const base = PROTEIN.fields[0];
  if (!base) throw new Error("PROTEIN has no fields");
  return { ...base, ...overrides };
}

const ALIGNMENT_TYPE: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  name: "iki/core.Alignment",
  plugin: "iki/core",
  label: "Alignment",
  label_plural: "Alignments",
  hrid_prefix: "ALN",
  identity: [],
  fields: [
    field({ name: "name", label: "Name", type: "string", required: true }),
    field({ name: "reference", label: "Reference", type: "ref", to: ["iki/core.Sequence"] }),
    field({
      name: "members",
      label: "Members",
      type: "list",
      items: "ref",
      to: ["iki/core.Sequence"],
    }),
    field({ name: "aligned", label: "Aligned", type: "file", required: true }),
  ],
  viewers: [
    {
      viewer: "alignment",
      label: "Alignment",
      alignment: "aligned",
      rows: "members",
      reference: "reference",
    },
  ],
};

const ALIGNED: Extract<Reading, { kind: "alignment" }> = {
  kind: "alignment",
  rows: [
    { id: "PRT-000001", description: "", aligned: "MK-LV", residues: 4, start: 1 },
    { id: "PRT-000002", description: "", aligned: "MKALV", residues: 5, start: 1 },
    { id: "read_7", description: "a read", aligned: "MR-LV", residues: 4, start: 1 },
  ],
  length: 5,
  alphabet: "protein",
  alphabet_guessed: false,
  conservation: "*   *",
  consensus: "MK-LV",
  agreement: [100, 66, 66, 100, 100],
  identical: 3,
  gapped: 1,
};

async function sha256(bytes: Uint8Array<ArrayBuffer>): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

async function onAlignment(
  values: Record<string, unknown> = {},
  size = FASTA.byteLength,
  mediaType = "chemical/seq-aa-fasta",
) {
  withTypes();
  answer("GET /api/v1/entity-types", [PROTEIN, ALIGNMENT_TYPE]);
  answer(
    "GET /api/v1/entities/ALN-000001",
    entity({
      hrid: "ALN-000001",
      type: "iki/core.Alignment",
      values: {
        name: "ABL kinases",
        reference: ABL2,
        members: [ABL1, ABL2],
        aligned: FILE,
        ...values,
      },
      refs: {
        [ABL1]: {
          hrid: "PRT-000001",
          title: "ABL1 kinase",
          type: "iki/protein.Protein",
          archived: false,
          purged: false,
        },
        [ABL2]: {
          hrid: "PRT-000002",
          title: "ABL2 kinase",
          type: "iki/protein.Protein",
          archived: false,
          purged: false,
        },
      },
      files: { [FILE]: { name: "abl.afa", media_type: mediaType, size } },
    }),
  );
  answer(`GET /api/v1/files/${FILE}/content-url`, {
    url: "https://store.example.com/iki-files/blobs/ab/ce?X-Amz-Signature=x",
    expires_at: "2026-11-14T12:05:00Z",
    sha256: await sha256(FASTA),
  });
  answer("GET /iki-files/blobs/ab/ce", () => new Response(FASTA.slice()));
}

beforeEach(() => {
  read.mockReset();
  read.mockResolvedValue(ALIGNED);
  // jsdom has no canvas: the viewer draws nothing, and everything else still works.
  HTMLCanvasElement.prototype.getContext = (() => null) as never;
});

describe("the alignment viewer", () => {
  it("links the rows that are sequences in Iki, and pins the reference", async () => {
    await onAlignment();
    await renderApp("/entities/ALN-000001/viewers/alignment/");
    const grid = await screen.findByRole("application", { name: "Alignment of abl.afa" });
    expect(read).toHaveBeenCalledTimes(1);
    expect(new TextDecoder().decode(read.mock.calls[0]?.[0])).toContain(">read_7");
    // Its media type says it holds amino acids, so its letters are read as them.
    expect(read.mock.calls[0]?.[1]).toBe("protein");
    expect(screen.queryByText(/from its letters/)).not.toBeInTheDocument();
    // Compared with the reference: PRT-000001 has a gap at its A, read_7 that and an R.
    expect(
      screen.getByText(
        "3 rows, 5 columns · the others cover 5 of PRT-000002’s 5 residues (100%) and differ " +
          "from it at 2 columns",
      ),
    ).toBeVisible();

    const reference = within(grid).getByText("Reference").closest(".alignment-label");
    expect(reference).toHaveTextContent("PRT-000002");
    const links = within(grid).getAllByRole("link");
    expect(links.map((l) => l.textContent)).toEqual(["PRT-000002", "PRT-000001"]);
    expect(links[1]).toHaveAttribute("href", "/entities/PRT-000001/");
    // A read isn't a sequence in Iki: it's named by its header, and can't be picked.
    expect(within(grid).getByText("read_7")).toHaveAttribute("title", "a read");
    expect(within(grid).getAllByRole("checkbox")).toHaveLength(2);
    expect(screen.getByRole("switch", { name: "Only where they differ from PRT-000002" })).toBe(
      screen.getByRole("switch"),
    );
  });

  it("moves its cursor with the keys, and says where it is", async () => {
    await onAlignment();
    await renderApp("/entities/ALN-000001/viewers/alignment/");
    const grid = await screen.findByRole("application", { name: "Alignment of abl.afa" });
    const at = () => document.querySelector(".alignment-at")?.textContent ?? "";
    // The reference is the first row.
    expect(at()).toContain("PRT-000002, column 1: M, residue 1 of 5.");
    fireEvent.keyDown(grid, { key: "ArrowDown" });
    fireEvent.keyDown(grid, { key: "ArrowRight" });
    fireEvent.keyDown(grid, { key: "ArrowRight" });
    expect(at()).toContain("PRT-000001, column 3: a gap.");
    expect(at()).toContain("Consensus -, where 66% of rows agree.");
    fireEvent.keyDown(grid, { key: "End" });
    expect(at()).toContain("PRT-000001, column 5: V, residue 4 of 4.");
    fireEvent.keyDown(grid, { key: "ArrowDown" });
    expect(at()).toContain("read_7, column 5");
  });

  it("says when it guessed what the residues are, the file not saying", async () => {
    await onAlignment({}, FASTA.byteLength, "text/plain");
    read.mockResolvedValue({ ...ALIGNED, alphabet_guessed: true });
    await renderApp("/entities/ALN-000001/viewers/alignment/");
    expect(
      await screen.findByText(
        "Read as amino acids from its letters: the file doesn’t say which it holds.",
      ),
    ).toBeVisible();
    expect(read.mock.calls[0]?.[1]).toBeNull();
  });

  it("numbers a row that's part of its sequence from where that part starts", async () => {
    await onAlignment();
    read.mockResolvedValue({
      ...ALIGNED,
      rows: ALIGNED.rows.map((r) => (r.id === "PRT-000002" ? { ...r, start: 31 } : r)),
    });
    await renderApp("/entities/ALN-000001/viewers/alignment/");
    await screen.findByRole("application");
    expect(document.querySelector(".alignment-at")).toHaveTextContent(
      "PRT-000002, column 1: M, residue 31 of 31–35.",
    );
    expect(screen.getByText(/cover 5 of PRT-000002’s residues 31–35 \(100%\)/)).toBeVisible();
  });

  it("goes to a column", async () => {
    await onAlignment();
    await renderApp("/entities/ALN-000001/viewers/alignment/");
    await screen.findByRole("application");
    fireEvent.change(screen.getByRole("textbox", { name: "Go to column" }), {
      target: { value: "4" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Go" }));
    expect(document.querySelector(".alignment-at")).toHaveTextContent(
      "PRT-000002, column 4: L, residue 4 of 5.",
    );
  });

  it("runs an action on the sequences picked", async () => {
    await onAlignment();
    await renderApp("/entities/ALN-000001/viewers/alignment/");
    await screen.findByRole("application");
    fireEvent.click(screen.getByRole("checkbox", { name: "Select PRT-000001" }));
    expect(screen.getByText("1 sequence selected")).toHaveAttribute("role", "status");
    expect(screen.getByRole("link", { name: "Run action on it" })).toHaveAttribute(
      "href",
      "/projects/kinase-atlas/runs/new/?entities=PRT-000001",
    );
    fireEvent.click(screen.getByRole("button", { name: "Select every sequence in Iki (2)" }));
    expect(screen.getByRole("link", { name: "Run action on them" })).toHaveAttribute(
      "href",
      "/projects/kinase-atlas/runs/new/?entities=PRT-000002%2CPRT-000001",
    );
    fireEvent.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(screen.queryByRole("link", { name: "Run action on them" })).not.toBeInTheDocument();
  });

  it("says how alike its columns are, as Clustal counts, without a reference", async () => {
    await onAlignment({ reference: null });
    await renderApp("/entities/ALN-000001/viewers/alignment/");
    expect(
      await screen.findByText("3 rows, 5 columns · 3 identical (60%) · 1 with gaps"),
    ).toBeVisible();
  });

  it("says when its reference isn't one of its rows", async () => {
    await onAlignment({ members: [ABL1] });
    read.mockResolvedValue({
      ...ALIGNED,
      rows: ALIGNED.rows.filter((r) => r.id !== "PRT-000002"),
    });
    await renderApp("/entities/ALN-000001/viewers/alignment/");
    expect(
      await screen.findByText(
        "Its reference, PRT-000002, isn’t one of its rows: no row is headed by its HRID.",
      ),
    ).toBeVisible();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  });

  it("says what the parsers couldn't read, and when the sequences aren't aligned", async () => {
    await onAlignment();
    read.mockResolvedValue({ kind: "error", message: "a record has no header", line: 1 });
    await renderApp("/entities/ALN-000001/viewers/alignment/");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "abl.afa isn’t FASTA the viewer can read: a record has no header (line 1).",
    );
  });

  it("lists sequences that aren't aligned", async () => {
    await onAlignment();
    read.mockResolvedValue({
      kind: "sequences",
      alphabet: "protein",
      records: [
        { id: "a", description: "", length: 4 },
        { id: "b", description: "", length: 5 },
      ],
    });
    await renderApp("/entities/ALN-000001/viewers/alignment/");
    expect(await screen.findByText(/abl.afa’s 2 sequences aren’t aligned/)).toBeVisible();
    expect(screen.getByText("b")).toBeVisible();
  });

  it("leaves files too big for the browser to a desktop viewer", async () => {
    await onAlignment({}, 65 * 1024 * 1024);
    await renderApp("/entities/ALN-000001/viewers/alignment/");
    expect(await screen.findByText(/It’s bigger than the alignment viewer opens/)).toBeVisible();
    await waitFor(() => {
      expect(read).not.toHaveBeenCalled();
    });
  });
});

describe("the alignment viewer's parts", () => {
  it("links only the rows the alignment lists", () => {
    const rows = shownRows(
      [
        { id: "PRT-000001", description: "", aligned: "MK", residues: 2, start: 1 },
        { id: "PRT-000009", description: "", aligned: "MK", residues: 2, start: 1 },
      ],
      [ABL1],
      {
        [ABL1]: {
          hrid: "PRT-000001",
          title: "ABL1",
          type: "iki/protein.Protein",
          archived: false,
          purged: false,
        },
        [ABL2]: {
          hrid: "PRT-000009",
          title: "Not listed",
          type: "iki/protein.Protein",
          archived: false,
          purged: false,
        },
      },
    );
    expect(rows.map((r) => r.entity?.hrid ?? null)).toEqual(["PRT-000001", null]);
  });

  it("links a row named by the HRID an imported entity had where it was made", () => {
    const rows = shownRows(
      [{ id: "PRT-000012", description: "", aligned: "MK", residues: 2, start: 1 }],
      [ABL1],
      {
        [ABL1]: {
          hrid: "PRT-000107",
          title: "ABL1",
          type: "iki/protein.Protein",
          archived: false,
          purged: false,
          formerly: ["PRT-000012"],
        },
      },
    );
    expect(rows.map((r) => r.entity?.hrid ?? null)).toEqual(["PRT-000107"]);
  });

  it("numbers residues without their gaps", () => {
    expect(residueNumber("M-K-L", 0)).toBe(1);
    expect(residueNumber("M-K-L", 1)).toBeNull();
    expect(residueNumber("M-K-L", 4)).toBe(3);
    expect(residueNumber("M-K-L", 9)).toBeNull();
    // A row that's residues 31 onwards of its sequence; `.` is no residue either.
    expect(residueNumber("M-K.L", 4, 31)).toBe(33);
    expect(residueNumber("M-K.L", 3, 31)).toBeNull();
  });

  it("colours residues by kind where their column agrees", () => {
    expect(colourOf("L", "I", 60, "protein", "agreeing")).toBe("hydrophobic");
    expect(colourOf("K", "I", 60, "protein", "agreeing")).toBeNull();
    expect(colourOf("L", "L", 40, "protein", "agreeing")).toBeNull();
    expect(colourOf("K", "I", 10, "protein", "all")).toBe("positive");
    expect(colourOf("L", "L", 100, "protein", "none")).toBeNull();
    expect(colourOf("a", "A", 100, "nucleotide", "agreeing")).toBe("polar");
    expect(colourOf("X", "X", 100, "protein", "all")).toBeNull();
  });

  it("keeps the cursor in the alignment", () => {
    expect(moved("ArrowLeft", { row: 0, column: 0 }, 3, 5, 2)).toEqual({ row: 0, column: 0 });
    expect(moved("PageDown", { row: 0, column: 2 }, 3, 5, 2)).toEqual({ row: 2, column: 2 });
    expect(moved("End", { row: 1, column: 0 }, 3, 5, 2)).toEqual({ row: 1, column: 4 });
    expect(moved("a", { row: 1, column: 0 }, 3, 5, 2)).toBeNull();
  });

  it("reads a saved position, and only a sound one", () => {
    const position = (value: unknown) => readViewState(value).targets[0]?.position;
    expect(position({ version: 1, position: { column: 7, row: 2 } })).toEqual({
      column: 7,
      row: 2,
    });
    expect(position({ version: 2, targets: [{ position: { column: 7, row: 2 } }] })).toEqual({
      column: 7,
      row: 2,
    });
    expect(position({ version: 1, position: { column: 0, row: 2 } })).toBeUndefined();
    expect(position({ version: 1, position: { column: 1.5, row: 2 } })).toBeUndefined();
  });
});
