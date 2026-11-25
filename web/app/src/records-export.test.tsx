/** Exporting many records as one file (ADR 0229): those picked in a project's table, or
 * everything its search finds, as FASTA or SDF, written at once or in the background. */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import {
  COLLECTION_TYPE,
  entity,
  field,
  KINASE,
  PROTEIN,
  PROTEIN_ID,
  renderApp,
  STRUCTURE,
  withTypes,
} from "./entity-fixtures";
import { answer, calls, page, problem, project } from "./test-server";

const SECOND_ID = "00000000-0000-7000-8000-00000000e002";
const EXPORT_ID = "00000000-0000-7000-8000-00000000f001";

const PROTEIN_FASTA: Schemas["FileFormatOut"] = {
  format: "protein-fasta",
  label: "FASTA",
  extension: "fasta",
  media_type: "chemical/seq-aa-fasta",
  description: "Its residues in one-letter codes, its ID, name and organism in its header.",
  many: "file",
};

const EXPORTING: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  exports: [
    {
      export: "protein",
      formats: [PROTEIN_FASTA],
      sequence: "sequence",
    },
  ],
};

const COMPOUND: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  name: "iki/small_molecule.Compound",
  plugin: "iki/small_molecule",
  label: "Compound",
  label_plural: "Compounds",
  hrid_prefix: "CPD",
  fields: [
    field({ name: "name", label: "Name", type: "string" }),
    field({ name: "smiles", label: "SMILES", type: "string" }),
    field({ name: "cas_number", label: "CAS number", type: "string" }),
  ],
  exports: [
    {
      export: "compound",
      formats: [
        {
          format: "sdf",
          label: "SDF",
          extension: "sdf",
          media_type: "chemical/x-mdl-sdfile",
          description: "Its structure as a Molfile, and the fields chosen as data.",
          many: "file",
        },
        {
          format: "molfile",
          label: "Molfile",
          extension: "mol",
          media_type: "chemical/x-mdl-molfile",
          description: "Its structure alone.",
          many: "one",
        },
      ],
      structure: "molfile",
      data: ["name", "smiles", "cas_number"],
    },
  ],
};

function made(overrides: Partial<Schemas["RecordExportOut"]> = {}): Schemas["RecordExportOut"] {
  return {
    id: EXPORT_ID,
    state: "ready",
    format: "protein-fasta",
    selection: "PRT-000001, PRT-000002",
    count: 2,
    done: 2,
    file_name: "Kinase Atlas Proteins (2).fasta",
    size: 2048,
    sha256: "a".repeat(64),
    fields: [],
    notes: {},
    refused: {},
    error: "",
    writer: { builtin: "protein-fasta", version: "0.1.0" },
    created_at: "2026-11-25T09:00:00Z",
    finished_at: "2026-11-25T09:00:01Z",
    expires_at: "2026-12-02T09:00:01Z",
    ...overrides,
  };
}

// jsdom lays nothing out; the grid draws the rows that fit in its scrolling region.
const sizes = { clientHeight: 600, clientWidth: 1000, offsetHeight: 600, offsetWidth: 1000 };
const kept: Record<string, PropertyDescriptor | undefined> = {};
beforeAll(() => {
  for (const [name, value] of Object.entries(sizes)) {
    kept[name] = Object.getOwnPropertyDescriptor(HTMLElement.prototype, name);
    Object.defineProperty(HTMLElement.prototype, name, { configurable: true, get: () => value });
  }
});
afterAll(() => {
  for (const [name, descriptor] of Object.entries(kept)) {
    if (descriptor) Object.defineProperty(HTMLElement.prototype, name, descriptor);
  }
});

let downloads: { name: string; href: string }[] = [];
beforeEach(() => {
  downloads = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    downloads.push({ name: this.download, href: new URL(this.href).pathname });
  });
});
afterEach(() => {
  vi.restoreAllMocks();
});

/** A Viewer's project: they run nothing and edit nothing, but read and export. */
function asViewer(types: Schemas["EntityTypeOut"][] = [COLLECTION_TYPE, EXPORTING, STRUCTURE]) {
  withTypes();
  answer("GET /api/v1/entity-types", types);
  const viewing = project({
    my_role: "viewer",
    can: { ...KINASE.can, edit: false, manage: false, run: [] },
  });
  answer("GET /api/v1/projects", page([viewing]));
  answer("GET /api/v1/entities/counts", { types: [], total: null });
  answer("GET /api/v1/entity-exports", page([]));
  answer(
    "POST /api/v1/entities/search",
    page([
      entity(),
      entity({
        id: SECOND_ID,
        hrid: "PRT-000002",
        values: { name: "HBB", sequence: "MVHLTPEEK" },
      }),
    ]),
  );
}

describe("exporting records as one file", () => {
  it("exports the rows picked, in the table's order, by someone who only reads", async () => {
    asViewer();
    answer(
      "POST /api/v1/entity-exports",
      made({ notes: { "PRT-000002": ["Its features aren't in FASTA."] } }),
    );
    const { user } = await renderApp("/projects/kinase-atlas/entities/?type=iki/protein.Protein");
    await screen.findByRole("table", { name: "2 proteins" });
    // Picking is for exporting here: nothing runs or is reassigned.
    await user.click(screen.getByRole("checkbox", { name: "Select PRT-000002" }));
    await user.click(screen.getByRole("checkbox", { name: "Select PRT-000001" }));
    expect(screen.getByText("2 selected")).toHaveAttribute("role", "status");
    expect(screen.queryByRole("link", { name: /Run action/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Export selected" }));
    const dialog = await screen.findByRole("dialog", { name: "Export 2 selected" });
    expect(within(dialog).getByRole("radio", { name: /^FASTA/ })).toBeChecked();
    expect(dialog).toHaveTextContent("editing one afterwards doesn’t change the file");
    await user.click(within(dialog).getByRole("button", { name: "Export" }));
    expect(await within(dialog).findByRole("status")).toHaveTextContent(
      "Downloading Kinase Atlas Proteins (2).fasta: 2 records, 2.0 KiB.",
    );
    const [sent] = calls("POST", "/api/v1/entity-exports");
    expect(sent?.body).toEqual({ format: "protein-fasta", entities: [PROTEIN_ID, SECOND_ID] });
    expect(downloads).toEqual([
      {
        name: "Kinase Atlas Proteins (2).fasta",
        href: `/api/v1/entity-exports/${EXPORT_ID}/content`,
      },
    ]);
    // What FASTA couldn't hold, which only the page and the audit log say.
    expect(dialog).toHaveTextContent("It couldn’t hold everything of 1 record:");
    expect(dialog).toHaveTextContent("PRT-000002: Its features aren't in FASTA.");
    await user.click(within(dialog).getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("exports the whole search in the background, showing how far it's got", async () => {
    asViewer();
    answer(
      "POST /api/v1/entity-exports",
      made({ state: "queued", done: 0, count: 1200, size: null, sha256: "", finished_at: null }),
    );
    let reads = 0;
    answer(`GET /api/v1/entity-exports/${EXPORT_ID}`, () => {
      reads += 1;
      return reads === 1
        ? made({ state: "writing", done: 300, count: 1200, size: null, sha256: "" })
        : made({ count: 1200, done: 1200 });
    });
    const { user } = await renderApp(
      "/projects/kinase-atlas/entities/?type=iki/protein.Protein&q=kinase",
    );
    await screen.findByRole("table", { name: "2 proteins match" });
    await user.click(screen.getByRole("button", { name: "Export" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Export all proteins the table shows",
    });
    await user.click(within(dialog).getByRole("button", { name: "Export" }));
    const progress = await within(dialog).findByRole("progressbar", {
      name: "Writing 300 of 1,200 records…",
    });
    expect(progress).toHaveAttribute("value", "300");
    expect(await within(dialog).findByRole("status", {}, { timeout: 3000 })).toHaveTextContent(
      "1,200 records",
    );
    const [sent] = calls("POST", "/api/v1/entity-exports");
    expect(sent?.body).toMatchObject({
      format: "protein-fasta",
      search: { type: "iki/protein.Protein", project: KINASE.id, q: "kinase" },
    });
    // Ready in the background: the person downloads it from its link.
    expect(within(dialog).getByRole("status")).toHaveTextContent(
      "Ready to download: Kinase Atlas Proteins (2).fasta: 1,200 records, 2.0 KiB.",
    );
    expect(downloads).toEqual([]);
    expect(
      within(dialog).getByRole("link", { name: "Kinase Atlas Proteins (2).fasta" }),
    ).toHaveAttribute("href", `/api/v1/entity-exports/${EXPORT_ID}/content`);
  });

  it("says which records couldn't be written, and makes no file", async () => {
    asViewer();
    answer(
      "POST /api/v1/entity-exports",
      made({
        state: "failed",
        size: null,
        sha256: "",
        error: "1 record can't be written, so no file was made: PRT-000002.",
        refused: { "PRT-000002": "Its sequence is empty." },
      }),
    );
    const { user } = await renderApp("/projects/kinase-atlas/entities/?type=iki/protein.Protein");
    await screen.findByRole("table", { name: "2 proteins" });
    await user.click(screen.getByRole("button", { name: "Export" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Export" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("1 record can't be written, so no file was made");
    expect(alert).toHaveTextContent("PRT-000002: Its sequence is empty.");
    expect(downloads).toEqual([]);
  });

  it("shows why it was refused, keeping the choices", async () => {
    asViewer();
    answer(
      "POST /api/v1/entity-exports",
      problem(
        422,
        "invalid",
        "The search finds more than 10,000 records, the most one file holds.",
        [
          {
            pointer: "#/search",
            detail: "The search finds more than 10,000 records, the most one file holds.",
          },
        ],
      ),
    );
    const { user } = await renderApp("/projects/kinase-atlas/entities/?type=iki/protein.Protein");
    await screen.findByRole("table", { name: "2 proteins" });
    await user.click(screen.getByRole("button", { name: "Export" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Export" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "The search finds more than 10,000 records, the most one file holds.",
    );
    expect(within(dialog).getByRole("radio", { name: /^FASTA/ })).toBeChecked();
  });

  it("chooses an SDF's data fields", async () => {
    asViewer([COLLECTION_TYPE, COMPOUND]);
    answer(
      "POST /api/v1/entities/search",
      page([
        entity({
          type: COMPOUND.name,
          hrid: "CPD-000001",
          values: { name: "aspirin", smiles: "CC(=O)Oc1ccccc1C(=O)O" },
        }),
      ]),
    );
    answer("POST /api/v1/entity-exports", made({ format: "sdf", count: 1, done: 1 }));
    const { user } = await renderApp(
      "/projects/kinase-atlas/entities/?type=iki/small_molecule.Compound",
    );
    await screen.findByRole("table", { name: "1 compound" });
    await user.click(screen.getByRole("button", { name: "Export" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Export all compounds the table shows",
    });
    // A Molfile holds one record: it isn't offered for many.
    expect(within(dialog).queryByRole("radio", { name: /^Molfile/ })).not.toBeInTheDocument();
    const fields = within(dialog).getByRole("group", { name: "Data fields" });
    expect(
      within(fields)
        .getAllByRole("checkbox")
        .map((c) => (c as HTMLInputElement).checked),
    ).toEqual([true, true, true]);
    await user.click(within(fields).getByRole("checkbox", { name: "CAS number" }));
    await user.click(within(dialog).getByRole("button", { name: "Export" }));
    await waitFor(() => {
      expect(calls("POST", "/api/v1/entity-exports")).toHaveLength(1);
    });
    expect(calls("POST", "/api/v1/entity-exports")[0]?.body).toMatchObject({
      format: "sdf",
      fields: ["name", "smiles"],
    });
  });

  it("isn't offered for records whose type exports nothing", async () => {
    asViewer([COLLECTION_TYPE, PROTEIN, STRUCTURE]);
    await renderApp("/projects/kinase-atlas/entities/?type=iki/protein.Protein");
    await screen.findByRole("table", { name: "2 proteins" });
    expect(screen.getByRole("link", { name: "Download CSV" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Export" })).not.toBeInTheDocument();
    // Nor is picking, for a Viewer: there's nothing to do with what's picked.
    expect(screen.queryByRole("checkbox", { name: "Select PRT-000001" })).not.toBeInTheDocument();
  });

  it("lists recent exports to download again", async () => {
    asViewer();
    answer(
      "GET /api/v1/entity-exports",
      page([made(), made({ id: SECOND_ID, state: "expired", file_name: "Older.fasta" })]),
    );
    const { user } = await renderApp("/projects/kinase-atlas/entities/?type=iki/protein.Protein");
    await screen.findByRole("table", { name: "2 proteins" });
    await user.click(screen.getByRole("button", { name: "Export" }));
    const recent = await screen.findByRole("region", { name: "Recent exports" });
    expect(
      within(recent).getByRole("link", { name: "Kinase Atlas Proteins (2).fasta" }),
    ).toHaveAttribute("href", `/api/v1/entity-exports/${EXPORT_ID}/content`);
    expect(recent).toHaveTextContent("Older.fasta · expired");
  });
});
