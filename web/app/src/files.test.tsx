/**
 * Files (ADR 0177), against a fake API: the file picker in an entity form, and how a list
 * of files says what it holds.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { field, KINASE, NO_FACTS, PROTEIN, renderApp, STRUCTURE } from "./entity-fixtures";
import {
  acquiredText,
  factsLine,
  filterOf,
  readGroupColumns,
  searchOf,
  spanText,
  statedFacts,
  usedByText,
} from "./files";
import { answer, calls, CLEAN_SCAN, ME, page, project, signedIn } from "./test-server";

type Row = Schemas["FileRowOut"];

function file(id: string, name: string, overrides: Partial<Row> = {}): Row {
  return {
    id: `00000000-0000-7000-8000-0000000f${id.padStart(4, "0")}`,
    name,
    media_type: "chemical/x-pdb",
    size: 2048,
    sha256: "a".repeat(64),
    project: { id: KINASE.id, name: KINASE.name, slug: KINASE.slug },
    created_by: null,
    agent: null,
    created_at: "2026-11-11T09:00:00Z",
    external: false,
    scan: CLEAN_SCAN,
    acquired: null,
    facts: NO_FACTS,
    used_by: { records: 0, runs: 0 },
    ...overrides,
  };
}

const A = file("1", "1abc.pdb", { used_by: { records: 2, runs: 1 } });
const B = file("2", "2xyz.pdb");
const C = file("3", "3def.pdb");

const EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

/** An empty file that already has a scan decision has no parts to send. */
function answerUpload(name: string) {
  answer("POST /api/v1/uploads", {
    id: "00000000-0000-7000-8000-0000000a0001",
    project: KINASE.id,
    name,
    media_type: "text/csv",
    size: 0,
    sha256: EMPTY,
    state: "ready",
    file: { ...file("9", name, { media_type: "text/csv", size: 0, sha256: EMPTY }) },
    parts: [],
    part_size: 0,
    part_count: 0,
    error: "",
    created_at: "2026-03-14T09:00:00Z",
    expires_at: "2026-03-15T09:00:00Z",
  });
}

function answerFiles(items: Row[]) {
  answer("GET /api/v1/files", page(items));
  answer("GET /api/v1/files/count", { total: items.length, more_than_total: false });
  answer("GET /api/v1/files/formats", [
    { id: "iki/core.pdb", label: "PDB", media_type: "chemical/x-pdb", count: items.length },
  ]);
}

describe("the file picker", () => {
  afterEach(() => {
    window.history.replaceState(null, "", "/");
  });

  it("chooses several files already in the project for a list, up to what it takes", async () => {
    signedIn();
    const traces: Schemas["EntityTypeOut"] = {
      ...STRUCTURE,
      fields: [field({ name: "traces", label: "Traces", type: "list", items: "file" })],
      values_schema: {
        properties: {
          traces: {
            type: "array",
            maxItems: 2,
            items: { type: "string", "x-iki-media-types": ["chemical/x-pdb"] },
          },
        },
      },
    };
    answer("GET /api/v1/entity-types", [PROTEIN, traces]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/entities", page([]));
    answerFiles([A, B, C]);
    const { user } = await renderApp(
      "/projects/kinase-atlas/entities/new/?type=iki/protein.ProteinStructure",
    );
    const group = screen.getByRole("group", { name: /^Traces/ });
    await user.click(within(group).getByRole("button", { name: "Choose files already here" }));
    const picker = await screen.findByRole("dialog", { name: "Choose files for Traces" });
    expect(within(picker).getByText(/Choose up to 2\./)).toBeInTheDocument();
    // What it lists is the server's, held to what the list's items take.
    await within(picker).findByRole("checkbox", { name: "1abc.pdb" });
    const asked = calls("GET", "/api/v1/files").at(-1)?.query;
    expect(asked?.get("project")).toBe(KINASE.id);
    expect(asked?.getAll("accepts")).toEqual(["chemical/x-pdb"]);
    expect(within(picker).getByRole("cell", { name: "2 records · 1 run" })).toBeInTheDocument();

    await user.click(within(picker).getByRole("checkbox", { name: "1abc.pdb" }));
    // Clicking a row chooses it too.
    const row = within(picker).getByRole("row", { name: /2xyz\.pdb/ });
    await user.click(within(row).getByRole("cell", { name: "2.0 KiB" }));
    // Two is all it takes: the third can't be chosen.
    expect(within(picker).getByRole("checkbox", { name: "3def.pdb" })).toBeDisabled();
    await user.click(within(picker).getByRole("button", { name: "Choose 2 files" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(within(group).getByText("1abc.pdb")).toBeInTheDocument();
    expect(within(group).getByText("2xyz.pdb")).toBeInTheDocument();
    expect(
      within(group)
        .getAllByRole("button", { name: /^Remove / })
        .map((b) => b.getAttribute("aria-label")),
    ).toEqual(["Remove 1abc.pdb", "Remove 2xyz.pdb"]);

    // Opened again, those already in the list show as chosen, and it holds no more.
    await user.click(within(group).getByRole("button", { name: "Choose files already here" }));
    const again = await screen.findByRole("dialog", { name: "Choose files for Traces" });
    expect(
      within(again).getByText(/It holds as many as it takes: remove some first\./),
    ).toBeInTheDocument();
    const held = await within(again).findByRole("checkbox", { name: /1abc\.pdb/ });
    expect(held).toBeChecked();
    expect(held).toBeDisabled();
  });

  it("searches as its Files tab does, and says when nothing matches", async () => {
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN, STRUCTURE]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/entities", page([]));
    answerFiles([A]);
    const { user } = await renderApp(
      "/projects/kinase-atlas/entities/new/?type=iki/protein.ProteinStructure",
    );
    const structure = screen.getByRole("group", { name: "Structure" });
    await user.click(within(structure).getByRole("button", { name: "Or choose one already here" }));
    const picker = await screen.findByRole("dialog", { name: "Choose a file for Structure" });
    await within(picker).findByRole("radio", { name: "1abc.pdb" });
    answerFiles([]);
    await user.type(within(picker).getByRole("searchbox", { name: "Search" }), "clone-17");
    expect(
      await within(picker).findByText(
        "No files match. Try another part of the name or instrument, or fewer filters.",
      ),
    ).toBeInTheDocument();
    expect(calls("GET", "/api/v1/files").at(-1)?.query.get("q")).toBe("clone-17");
    await user.click(within(picker).getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });
  it("uploads a new one from the picker, saying when its data was acquired", async () => {
    signedIn();
    const plate: Schemas["EntityTypeOut"] = {
      ...STRUCTURE,
      fields: [field({ name: "reading", label: "Reading", type: "file" })],
      values_schema: { properties: { reading: { type: "string" } } },
    };
    answer("GET /api/v1/entity-types", [PROTEIN, plate]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/entities", page([]));
    answerFiles([A]);
    answerUpload("plate-7.csv");
    const { user } = await renderApp(
      "/projects/kinase-atlas/entities/new/?type=iki/protein.ProteinStructure",
    );
    const group = screen.getByRole("group", { name: /^Reading/ });
    await user.click(within(group).getByRole("button", { name: "Or choose one already here" }));
    const picker = await screen.findByRole("dialog", { name: "Choose a file for Reading" });
    await user.upload(
      within(picker).getByLabelText("Upload a new one"),
      new File([], "plate-7.csv", { type: "text/csv" }),
    );
    // Before it's sent, the picker asks when its data was acquired: a plate reader's CSV
    // doesn't say.
    const step = await screen.findByRole("dialog", { name: "Upload a file for Reading" });
    expect(within(step).getByText("plate-7.csv")).toBeInTheDocument();
    expect(calls("POST", "/api/v1/uploads")).toEqual([]);
    await user.type(within(step).getByLabelText(/When its data was acquired/), "2026-03-14");
    await user.click(within(step).getByRole("button", { name: "Upload it" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(await within(group).findByRole("status")).toHaveTextContent("Chosen: plate-7.csv");
    expect(calls("POST", "/api/v1/uploads")[0]?.body).toMatchObject({
      name: "plate-7.csv",
      acquired_at: "2026-03-14",
    });
  });
});

describe("a project's Files", () => {
  it("lists its files, keeps its search and filters in the address, and links what uses each", async () => {
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN, STRUCTURE]);
    answer("GET /api/v1/projects", page([KINASE]));
    const acquired = file("4", "A01_clone-17-F.ab1", {
      media_type: "application/x-abi",
      acquired: {
        on: "2026-03-14",
        at: "2026-03-14T09:21:07",
        source: "abif",
        source_label: "from the ABIF trace’s run start (RUND and RUNT)",
      },
    });
    const tube = file("5", "Tube_003.fcs", {
      media_type: "application/vnd.isac.fcs",
      facts: {
        ...NO_FACTS,
        instrument: { value: "LSRFortessa X-20", stated_in: "FCS keyword $CYT" },
        serial: { value: "R658222R1012", stated_in: "FCS keyword $CYTSN" },
        operator: { value: "Priya Raman", stated_in: "FCS keyword $OP" },
      },
    });
    answerFiles([acquired, A, tube]);
    const { user, router } = await renderApp("/projects/kinase-atlas/files/");
    const table = await screen.findByRole("table", { name: "Files in Kinase Atlas" });
    const rows = within(table).getAllByRole("row");
    expect(rows.map((r) => r.textContent)).toEqual([
      "NameFormatSizeAcquiredAddedUsed by",
      expect.stringContaining("A01_clone-17-F.ab1"),
      expect.stringContaining("1abc.pdb"),
      expect.stringContaining("Tube_003.fcs"),
    ]);
    // What a file states of its instrument, beneath its name; nothing where it states none.
    const stating = rows[3];
    if (!stating) throw new Error("no fourth row");
    expect(within(stating).getByText("LSRFortessa X-20 · R658222R1012 · Priya Raman")).toHaveClass(
      "cell-meta",
    );
    expect(rows[2]?.querySelector(".list-fill .cell-meta")).toBeNull();
    expect(screen.getByRole("searchbox", { name: "Search" })).toHaveAttribute(
      "placeholder",
      "Search by name, instrument or operator",
    );
    expect(within(table).getByText("2026-03-14")).toHaveAttribute(
      "title",
      "2026-03-14 09:21:07, from the ABIF trace’s run start (RUND and RUNT)",
    );
    // Unknown, never when it was added.
    const unknown = rows[2];
    if (!unknown) throw new Error("no third row");
    expect(within(unknown).getByRole("cell", { name: "Unknown" })).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "2 records · 1 run" })).toHaveAttribute(
      "href",
      `/files/${A.id}/#used-by`,
    );
    expect(within(table).getByRole("link", { name: "1abc.pdb" })).toHaveAttribute(
      "href",
      `/files/${A.id}/`,
    );
    expect(screen.getByText("3 files")).toBeInTheDocument();

    await user.type(screen.getByRole("searchbox", { name: "Search" }), "clone");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ q: "clone" });
    });
    expect(calls("GET", "/api/v1/files").at(-1)?.query.get("q")).toBe("clone");
    await user.selectOptions(screen.getByRole("combobox", { name: "Sort" }), "Latest acquired");
    await user.click(screen.getByRole("button", { name: "Filters" }));
    const filters = screen.getByRole("group", { name: "Filters" });
    // fireEvent-free: a date input takes typed text in jsdom.
    await user.type(within(filters).getByLabelText("Acquired from"), "2026-03-01");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        q: "clone",
        sort: "acquired",
        acquired_from: "2026-03-01",
      });
    });
    const asked = calls("GET", "/api/v1/files").at(-1)?.query;
    expect(asked?.get("sort")).toBe("acquired");
    expect(asked?.get("acquired_from")).toBe("2026-03-01");
    // No type here holds samples.
    expect(
      within(filters).getByText("No type here holds samples, so there are none to filter by."),
    ).toBeInTheDocument();
    await user.type(within(filters).getByRole("textbox", { name: "Run" }), "run-000007{Enter}");
    await waitFor(() => {
      expect(router.state.location.search).toMatchObject({ run: "RUN-000007" });
    });
    expect(screen.getByRole("button", { name: /Filters/ })).toHaveTextContent("Filters2");
  });

  it("uploads files to the project, each said to be acquired on the day given", async () => {
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN, STRUCTURE]);
    answer("GET /api/v1/projects", page([KINASE]));
    answerFiles([A]);
    answerUpload("plate-7.csv");
    const { user } = await renderApp("/projects/kinase-atlas/files/");
    await screen.findByRole("table", { name: "Files in Kinase Atlas" });
    await user.upload(screen.getByLabelText("Upload files"), [
      new File([], "plate-7.csv", { type: "text/csv" }),
      new File([], "plate-8.csv", { type: "text/csv" }),
    ]);
    const dialog = await screen.findByRole("dialog", { name: "Upload files to Kinase Atlas" });
    expect(
      within(dialog).getByText(/Formats Iki checks include .*ABI trace.*FCS.*mzML/),
    ).toBeInTheDocument();
    const day = within(dialog).getByLabelText(/When their data was acquired/);
    // Not a day to come.
    await user.type(day, "2999-01-01");
    expect(within(dialog).getByText(/can’t have been acquired after today/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Upload 2 files" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    await user.clear(day);
    await user.type(day, "2026-03-14");
    const asked = calls("GET", "/api/v1/files").length;
    await user.click(within(dialog).getByRole("button", { name: "Upload 2 files" }));
    expect(await screen.findByText("Added 2 files.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(calls("POST", "/api/v1/uploads").map((c) => c.body)).toEqual([
      expect.objectContaining({ name: "plate-7.csv", acquired_at: "2026-03-14" }),
      expect.objectContaining({ name: "plate-8.csv", acquired_at: "2026-03-14" }),
    ]);
    // The list is asked again, with them in it.
    await waitFor(() => {
      expect(calls("GET", "/api/v1/files").length).toBeGreaterThan(asked);
    });
  });

  it("offers no upload to someone who can only read the project", async () => {
    signedIn();
    const reader = project({ can: { ...KINASE.can, edit: false } });
    answer("GET /api/v1/entity-types", [PROTEIN, STRUCTURE]);
    answer("GET /api/v1/projects", page([reader]));
    answerFiles([A]);
    await renderApp("/projects/kinase-atlas/files/");
    await screen.findByRole("table", { name: "Files in Kinase Atlas" });
    expect(screen.queryByRole("button", { name: "Upload files" })).toBeNull();
  });

  it("shows that a pending file is scanning with no download action", async () => {
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN, STRUCTURE]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer(`GET /api/v1/files/${A.id}`, {
      ...A,
      scan: { state: "pending", usable: false },
      origin: { kind: "upload" },
      samples: [],
      read_groups: [],
    });
    answer(`GET /api/v1/files/${A.id}/records`, page([]));
    answer(`GET /api/v1/files/${A.id}/runs`, page([]));
    await renderApp(`/files/${A.id}/`);
    expect(
      await screen.findByText("Scanning… It can be used once the scanner finds it clean."),
    ).toBeVisible();
    expect(screen.queryByRole("link", { name: "Download" })).toBeNull();
  });

  const MACRO: Schemas["FileScanOut"] = {
    state: "infected",
    usable: false,
    decision_id: "00000000-0000-7000-8000-0000000d0007",
    scanner: "ClamAV 1.4.3",
    signature_version: "27453",
    scanned_at: "2026-11-24T08:12:00Z",
    reason: "Xls.Downloader.Agent-7372345-0",
    released: false,
  };

  function onHeldFile(scan: Schemas["FileScanOut"]) {
    answer("GET /api/v1/entity-types", [PROTEIN, STRUCTURE]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer(`GET /api/v1/files/${A.id}`, {
      ...A,
      name: "EnVision_plate-07.xlsm",
      scan,
      origin: { kind: "upload", run: null, uri: null, version: null },
      samples: [],
      read_groups: [],
    });
    answer(`GET /api/v1/files/${A.id}/records`, page([]));
    answer(`GET /api/v1/files/${A.id}/runs`, page([]));
  }

  it("says a quarantined file is held and why, and sends Quarantine Admins to review it", async () => {
    signedIn({ can: { ...ME.can, manage_quarantine: true } });
    onHeldFile(MACRO);
    await renderApp(`/files/${A.id}/`);
    const scan = (await screen.findByText("Malware scan", { selector: "dt" }))
      .nextElementSibling as HTMLElement;
    expect(scan).toHaveTextContent(
      "Quarantined: the scanner found Xls.Downloader.Agent-7372345-0. It can’t be downloaded or used unless a Quarantine Admin finds it a false positive.",
    );
    expect(
      within(scan).getByRole("link", { name: "Review it in Admin → Quarantine" }),
    ).toHaveAttribute("href", `/admin/quarantine/${MACRO.decision_id ?? ""}/`);
    expect(screen.queryByRole("link", { name: "Download" })).toBeNull();
  });

  it("gives everyone else the hold, with no link they couldn’t open", async () => {
    signedIn();
    onHeldFile({ ...MACRO, state: "not_scanned", reason: "Heuristics.Encrypted.Zip" });
    await renderApp(`/files/${A.id}/`);
    const scan = (await screen.findByText("Malware scan", { selector: "dt" }))
      .nextElementSibling as HTMLElement;
    expect(scan).toHaveTextContent(
      "Held: Encrypted (Heuristics.Encrypted.Zip). It can’t be downloaded or used: the scanner couldn’t inspect all of it.",
    );
    expect(within(scan).queryByRole("link")).toBeNull();
  });

  it("says a released file’s finding stays with it", async () => {
    signedIn({ can: { ...ME.can, manage_quarantine: true } });
    onHeldFile({ ...MACRO, usable: true, released: true });
    await renderApp(`/files/${A.id}/`);
    const scan = (await screen.findByText("Malware scan", { selector: "dt" }))
      .nextElementSibling as HTMLElement;
    expect(scan).toHaveTextContent(
      "Released from quarantine: the scanner found Xls.Downloader.Agent-7372345-0, and a Quarantine Admin found it a false positive.",
    );
    expect(
      within(scan).getByRole("link", { name: "See its release in Admin → Quarantine" }),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "Download" })).toBeVisible();
  });

  it("marks held and scanning files in a picker, which can’t choose them", async () => {
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN, STRUCTURE]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/entities", page([]));
    answerFiles([
      A,
      file("5", "5held.pdb", { scan: MACRO }),
      file("6", "6new.pdb", { scan: { ...CLEAN_SCAN, state: "pending", usable: false } }),
    ]);
    const { user } = await renderApp(
      "/projects/kinase-atlas/entities/new/?type=iki/protein.ProteinStructure",
    );
    const structure = screen.getByRole("group", { name: "Structure" });
    await user.click(within(structure).getByRole("button", { name: "Or choose one already here" }));
    const picker = await screen.findByRole("dialog", { name: "Choose a file for Structure" });
    expect(await within(picker).findByRole("radio", { name: "1abc.pdb" })).toBeEnabled();
    expect(within(picker).getByRole("radio", { name: "5held.pdb" })).toBeDisabled();
    expect(within(picker).getByRole("radio", { name: "6new.pdb" })).toBeDisabled();
    expect(within(picker).getByRole("row", { name: /5held\.pdb/ })).toHaveTextContent(
      "Quarantined",
    );
    expect(within(picker).getByRole("row", { name: /6new\.pdb/ })).toHaveTextContent("Scanning");
    // A click on its row doesn't choose it either.
    await user.click(within(picker).getByRole("row", { name: /5held\.pdb/ }));
    expect(within(picker).getByRole("radio", { name: "5held.pdb" })).not.toBeChecked();
  });

  it("shows a file: what it is, when its data was acquired, and what uses it", async () => {
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN, STRUCTURE]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer(`GET /api/v1/files/${A.id}`, {
      ...A,
      acquired: {
        on: "2026-03-14",
        at: "2026-03-14",
        source: "given",
        source_label: "as given when it was added",
      },
      origin: { kind: "upload", run: null, uri: null, version: null },
      samples: [],
      read_groups: [],
    });
    answer(
      `GET /api/v1/files/${A.id}/records`,
      page([
        {
          id: "00000000-0000-7000-8000-00000000e009",
          hrid: "PST-000009",
          title: "ABL1 structure",
          type: "iki/protein.ProteinStructure",
          revision: 6,
          spans: [
            { first: 1, last: 2, fields: ["structure"] },
            { first: 5, last: null, fields: ["structure"] },
          ],
        },
      ]),
    );
    answer(
      `GET /api/v1/files/${A.id}/runs`,
      page([
        {
          id: "00000000-0000-7000-8000-00000000r001",
          hrid: "RUN-000004",
          action: "iki/protein.fold",
          label: "Fold",
          status: "succeeded",
          project: { id: KINASE.id, name: KINASE.name, slug: KINASE.slug },
          roles: ["param"],
          created_at: "2026-11-11T10:00:00Z",
        },
      ]),
    );
    await renderApp(`/files/${A.id}/`);
    expect(screen.getByRole("heading", { level: 1, name: "1abc.pdb" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download" })).toHaveAttribute(
      "href",
      `/api/v1/files/${A.id}/content`,
    );
    expect(screen.getByText("as given when it was added")).toBeInTheDocument();
    expect(screen.getByText("Uploaded")).toBeInTheDocument();
    // Its Copy button says "Copy" once, as a screen reader reads it.
    expect(screen.getByRole("button", { name: "Copy the SHA-256" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Copy Copy/ })).not.toBeInTheDocument();
    const records = await screen.findByRole("table", { name: "Records that hold 1abc.pdb" });
    expect(within(records).getByRole("link", { name: "ABL1 structure" })).toHaveAttribute(
      "href",
      "/entities/00000000-0000-7000-8000-00000000e009/",
    );
    expect(
      within(records)
        .getAllByRole("link", { name: /^PST-000009 revisions/ })
        .map((l) => [l.textContent, l.getAttribute("href")]),
    ).toEqual([
      ["r1–r2", "/entities/00000000-0000-7000-8000-00000000e009/audit/?field=structure"],
      ["r5 on", "/entities/00000000-0000-7000-8000-00000000e009/audit/?field=structure"],
    ]);
    const runs = await screen.findByRole("table", { name: "Runs that read or made 1abc.pdb" });
    expect(within(runs).getByRole("link", { name: "RUN-000004" })).toHaveAttribute(
      "href",
      "/runs/RUN-000004/",
    );
    expect(within(runs).getByText("Given as a param")).toBeInTheDocument();
  });

  it("shows what a file states of its instrument, and where it states it", async () => {
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN, STRUCTURE]);
    answer("GET /api/v1/projects", page([KINASE]));
    const fcs = file("6", "Tube_003.fcs", {
      media_type: "application/vnd.isac.fcs",
      facts: {
        ...NO_FACTS,
        instrument: { value: "LSRFortessa X-20", stated_in: "FCS keyword $CYT" },
        operator: { value: "Priya Raman", stated_in: "FCS keyword $OP" },
      },
    });
    answer(`GET /api/v1/files/${fcs.id}`, {
      ...fcs,
      origin: { kind: "upload", run: null, uri: null, version: null },
      samples: [],
      read_groups: [],
    });
    answer(`GET /api/v1/files/${fcs.id}/records`, page([]));
    answer(`GET /api/v1/files/${fcs.id}/runs`, page([]));
    await renderApp(`/files/${fcs.id}/`);
    await screen.findByRole("heading", { level: 1, name: "Tube_003.fcs" });
    const summary = screen.getByText("Instrument").closest("dl");
    if (!summary) throw new Error("no summary");
    expect(within(summary).getByText("LSRFortessa X-20").closest("dd")).toHaveTextContent(
      "LSRFortessa X-20 from FCS keyword $CYT",
    );
    expect(within(summary).getByText("from FCS keyword $OP")).toHaveClass("muted");
    // What it doesn't state has no row, and a file without read groups no table of them.
    expect(within(summary).queryByText("Serial number")).toBeNull();
    expect(screen.queryByRole("heading", { name: /Read groups/ })).toBeNull();
  });

  it("shows an alignment's read groups as its header gives them", async () => {
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN, STRUCTURE]);
    answer("GET /api/v1/projects", page([KINASE]));
    const bam = file("7", "NA12878.bam", { media_type: "application/x-bam" });
    answer(`GET /api/v1/files/${bam.id}`, {
      ...bam,
      origin: { kind: "upload", run: null, uri: null, version: null },
      samples: [],
      read_groups: [
        { ID: "HV7KCDSX5.1", SM: "NA12878", LB: "lib1", PL: "ILLUMINA", DT: "2026-03-15" },
        { ID: "HV7KCDSX5.2", SM: "NA12878", LB: "lib1", PL: "ILLUMINA" },
      ],
    });
    answer(`GET /api/v1/files/${bam.id}/records`, page([]));
    answer(`GET /api/v1/files/${bam.id}/runs`, page([]));
    await renderApp(`/files/${bam.id}/`);
    await screen.findByRole("heading", { level: 1, name: "NA12878.bam" });
    expect(screen.getByRole("heading", { name: "Read groups 2" })).toBeInTheDocument();
    const groups = screen.getByRole("table", { name: "Read groups in NA12878.bam" });
    expect(
      within(groups)
        .getAllByRole("row")
        .map((r) => r.textContent),
    ).toEqual([
      "IDSampleLibraryPlatformRun date",
      "HV7KCDSX5.1NA12878lib1ILLUMINA2026-03-15",
      "HV7KCDSX5.2NA12878lib1ILLUMINA—",
    ]);
    expect(within(groups).getByRole("columnheader", { name: "Sample" })).toHaveAttribute(
      "title",
      "@RG SM",
    );
    expect(screen.queryByText("Instrument")).toBeNull();
  });
});

describe("a list of files says", () => {
  it("what a file states, in the order its page shows it", () => {
    const facts = {
      ...NO_FACTS,
      operator: { value: "USER", stated_in: "BMG MARS export header, User" },
      protocol: { value: "472 ABS 384 QC", stated_in: "BMG MARS export header, Test name" },
      instrument_name: { value: "ABI-3730-XL-1404-021", stated_in: "ABIF tag MCHN 1" },
    };
    expect(statedFacts(facts).map((f) => [f.label, f.value])).toEqual([
      ["Instrument name", "ABI-3730-XL-1404-021"],
      ["Operator", "USER"],
      ["Protocol", "472 ABS 384 QC"],
    ]);
    // The instrument's name stands in for its model when that's all it states.
    expect(factsLine(facts)).toBe("ABI-3730-XL-1404-021 · USER");
    expect(factsLine(NO_FACTS)).toBe("");
    expect(statedFacts(NO_FACTS)).toEqual([]);
  });

  it("the read groups' tags, a column for each any of them gives", () => {
    expect(
      readGroupColumns([
        { ID: "a", DS: "Tumour" },
        { ID: "b", PU: "HV7KCDSX5.1" },
      ]).map(([tag]) => tag),
    ).toEqual(["ID", "PU", "DS"]);
  });

  it("when its data was acquired, as precisely as its source gave it", () => {
    expect(acquiredText("2026-03-14")).toBe("2026-03-14");
    expect(acquiredText("2026-03-14T09:21:07.25")).toBe("2026-03-14 09:21:07.25");
    expect(acquiredText("2026-03-14T09:21Z")).toBe("2026-03-14 09:21 UTC");
    expect(acquiredText("2026-03-14T09:21:07+01:00")).toBe("2026-03-14 09:21:07 +01:00");
  });

  it("what uses a file, and which revisions held it", () => {
    expect(usedByText({ records: 0, runs: 0 })).toBe("Not used");
    expect(usedByText({ records: 1, runs: 0 })).toBe("1 record");
    expect(usedByText({ records: 2, runs: 3 })).toBe("2 records · 3 runs");
    expect(spanText({ first: 1, last: 2 })).toBe("r1–r2");
    expect(spanText({ first: 3, last: 3 })).toBe("r3");
    expect(spanText({ first: 5, last: null })).toBe("r5 on");
  });

  it("its filter in the address, without the defaults", () => {
    const filter = filterOf({ q: "clone", format: "image/*,application/pdf", sort: "name" });
    expect(filter).toEqual({
      q: "clone",
      format: ["image/*", "application/pdf"],
      acquiredFrom: undefined,
      acquiredTo: undefined,
      sample: undefined,
      run: undefined,
      sort: "name",
    });
    expect(searchOf(filter)).toEqual({
      q: "clone",
      format: "image/*,application/pdf",
      sort: "name",
    });
    expect(searchOf(filterOf({}))).toEqual({});
  });
});
