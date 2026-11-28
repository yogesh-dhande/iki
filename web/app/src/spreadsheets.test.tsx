/**
 * Importing a spreadsheet (ADR 0178): what the steps work out themselves, where an import
 * starts from, and each step against a fake API.
 */

import type { Schemas } from "@ikisci/sdk";
import { act, screen, waitFor, within } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { queryClient } from "./api";
import { entity, KINASE, NO_FACTS, PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import {
  columnName,
  columnsWith,
  counted,
  importable,
  moved,
  older,
  overlaid,
  parseRanges,
  rangesText,
  type ColumnChoice,
  type SheetChange,
  type SheetColumn,
  type SheetImport,
} from "./spreadsheets";
import { answer, calls, CLEAN_SCAN, made, page, problem, respond, type Sent } from "./test-server";

// jsdom lays nothing out; the grid draws the rows that fit in its scrolling region.
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

const ID = "00000000-0000-7000-8000-0000000051a1";
const BASE = `/api/v1/spreadsheet-imports/${ID}`;
const PERSON = {
  id: "00000000-0000-7000-8000-000000000001",
  email: "rosalind@example.com",
  name: "Rosalind Franklin",
  active: true,
  kind: "person" as const,
  imported_from: "",
};

function column(overrides: Partial<SheetColumn> & Pick<SheetColumn, "index" | "letter">) {
  return {
    header: `Column ${overrides.letter}`,
    field: null,
    unit: null,
    separator: ";",
    dates: null,
    time_zone: null,
    decimal: null,
    serial_dates: null,
    values: {},
    examples: [],
    ...overrides,
  };
}

const COLUMNS: SheetColumn[] = [
  column({ index: 0, letter: "A", header: "Name", field: "name", examples: ["ABL1", "EGFR"] }),
  column({
    index: 1,
    letter: "B",
    header: "Sequence",
    field: "sequence",
    examples: ["MLEICLKLVG", "MRPSGTAGAA"],
  }),
  column({ index: 2, letter: "C", header: "Yield (mg)", examples: ["1,234", "2.5"] }),
];

function field(
  overrides: Partial<Schemas["FieldTargetOut"]> & Pick<Schemas["FieldTargetOut"], "name">,
) {
  return {
    label: overrides.name,
    type: "string",
    item_type: null,
    unit: null,
    units: [],
    choices: [],
    to: [],
    required: false,
    identity: false,
    unique: false,
    why_not: null,
    ...overrides,
  };
}

function sheet(overrides: Partial<SheetImport> = {}): SheetImport {
  return {
    id: ID,
    revision: 2,
    project: KINASE.id,
    type: PROTEIN.name,
    file: {
      id: "00000000-0000-7000-8000-0000000051f1",
      name: "kinases.xlsx",
      size: 18_432,
      media_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    },
    state: "mapping",
    error: "",
    kind: "xlsx",
    encoding: "",
    separator: "",
    requested_by: PERSON,
    created_at: "2026-11-22T09:00:00Z",
    updated_at: "2026-11-22T09:00:00Z",
    sheets: [
      { name: "Kinases", rows: 4, columns: 3, hidden: false },
      { name: "Notes", rows: 2, columns: 1, hidden: true },
    ],
    sheet: "Kinases",
    header_row: 1,
    first_row: 2,
    last_row: null,
    skip: [],
    locale: { decimal: null, dates: null, serial_dates: false },
    readings: [
      {
        column: 2,
        field: "yield",
        kind: "numbers",
        cells: [
          {
            row: 2,
            cell: "1,234",
            stored: false,
            reads_as: null,
            ambiguous: true,
            problem: "Reads as 1,234 with a decimal point, and 1.234 with a decimal comma.",
          },
          { row: 3, cell: "2.5", stored: true, reads_as: "2.5", ambiguous: false, problem: null },
        ],
      },
    ],
    sample: [
      ["Name", "Sequence", "Yield (mg)"],
      ["ABL1", "MLEICLKLVG", "1,234"],
      ["EGFR", "MRPSGTAGAA", "2.5"],
      ["Total", "", "1236.5"],
    ],
    columns: COLUMNS,
    fields: [
      field({ name: "name", label: "Name" }),
      field({ name: "sequence", label: "Sequence", required: true, identity: true }),
      field({
        name: "molecular_weight",
        label: "Molecular weight",
        type: "quantity",
        why_not: "It's computed.",
      }),
    ],
    mapping: null,
    mappings: [],
    warnings: [],
    preview: null,
    skip_problems: false,
    run: null,
    run_problems: [],
    can: { change: true, preview: true, commit: false, discard: true },
    ...overrides,
  };
}

const PREVIEWED = sheet({
  state: "previewed",
  preview: {
    phase: "done",
    progress: 1,
    message: "",
    counts: { create: 2, reuse: 1, same: 1, problem: 1, ambiguous: 0 },
  },
  can: { change: true, preview: true, commit: true, discard: true },
});

function onImport(made: SheetImport): void {
  withTypes();
  answer("GET /api/v1/projects", page([KINASE]));
  answer(`GET ${BASE}`, made);
}

describe("what the steps work out", () => {
  it("reads rows to leave out as people write them", () => {
    expect(parseRanges("5-7, 12;  20–21")).toEqual([
      [5, 7],
      [12, 12],
      [20, 21],
    ]);
    expect(parseRanges("")).toEqual([]);
    expect(() => parseRanges("5-")).toThrow("“5-” isn’t a row or rows");
    expect(() => parseRanges("7-5")).toThrow("isn’t rows from first to last");
    expect(() => parseRanges("0")).toThrow("isn’t rows from first to last");
    expect(
      rangesText([
        [5, 7],
        [12, 12],
      ]),
    ).toBe("5-7, 12");
  });

  it("names a column by its letter, then its header if it has one", () => {
    expect(columnName({ letter: "C", header: "Yield (mg)" })).toBe("C: Yield (mg)");
    expect(columnName({ letter: "D", header: "Column D" })).toBe("Column D");
  });

  it("gives a column another field afresh, and keeps the other columns as they are", () => {
    const columns = [
      { ...nthColumn(0), unit: "g", values: { "circ.": "circular" } },
      { ...nthColumn(1), separator: "|" },
    ];
    const next = columnsWith(columns, 0, { field: "sequence" });
    expect(next[0]).toEqual({ index: 0, field: "sequence" });
    expect(next[1]).toMatchObject({ index: 1, field: "sequence", separator: "|", values: null });
    // The same field, with another option, keeps its other options.
    expect(columnsWith(columns, 0, { unit: "mg" })[0]).toMatchObject({
      field: "name",
      unit: "mg",
      values: { "circ.": "circular" },
    });
  });

  it("never takes an import back to a revision older than one shown", () => {
    const shown = sheet({ revision: 4, updated_at: "2026-11-22T09:00:02Z" });
    expect(older(shown, sheet({ revision: 3, updated_at: "2026-11-22T09:00:05Z" }))).toBe(true);
    expect(older(shown, sheet({ revision: 5, updated_at: "2026-11-22T09:00:01Z" }))).toBe(false);
    // The same revision, as it was before the one shown was written: its preview started.
    expect(older(shown, sheet({ revision: 4, updated_at: "2026-11-22T09:00:01Z" }))).toBe(true);
    expect(older(shown, sheet({ revision: 4, updated_at: "2026-11-22T09:00:03Z" }))).toBe(false);
    expect(older(undefined, shown)).toBe(false);
  });

  it("says which choices changed between two revisions", () => {
    const before = sheet();
    expect([...moved(before, sheet({ revision: 3 }))]).toEqual([]);
    const after = sheet({
      sheet: "Notes",
      header_row: 2,
      columns: COLUMNS.map((c) => (c.index === 0 ? { ...c, field: null } : c)),
    });
    expect([...moved(before, after)].sort()).toEqual(["columns", "header_row", "sheet"]);
  });

  it("shows a choice on its way on what's saved", () => {
    const made = sheet();
    expect(overlaid(made, { header_row: 3, first_row: 4 })).toMatchObject({
      header_row: 3,
      first_row: 4,
      sheet: "Kinases",
    });
    expect(overlaid(made, { last_row: 0 }).last_row).toBeNull();
    const columns = overlaid(made, { columns: [{ index: 2, field: "name", unit: "mg" }] }).columns;
    expect(columns.map((c) => [c.index, c.field, c.unit])).toEqual([
      [0, null, null],
      [1, null, null],
      [2, "name", "mg"],
    ]);
    expect(overlaid(made, { locale: { decimal: "comma", serial_dates: false } }).locale).toEqual({
      decimal: "comma",
      dates: null,
      serial_dates: false,
    });
  });

  it("counts what importing would do, and what it would write or keep", () => {
    expect(counted(PREVIEWED)).toEqual([
      { outcome: "create", count: 2 },
      { outcome: "reuse", count: 1 },
      { outcome: "same", count: 1 },
      { outcome: "problem", count: 1 },
    ]);
    expect(importable(PREVIEWED)).toBe(4);
    expect(importable(sheet())).toBe(0);
  });
});

function nthColumn(index: number): SheetColumn {
  const found = COLUMNS[index];
  if (!found) throw new Error(`No column ${String(index)}`);
  return found;
}

describe("where an import starts", () => {
  it("is offered on a project's Entities tab, as the type shown", async () => {
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
    answer("POST /api/v1/entities/search", page([entity()]));
    answer("GET /api/v1/entities/counts", { types: [], total: 1 });
    answer("GET /api/v1/saved-searches", []);
    await renderApp(`/projects/kinase-atlas/entities/?type=${PROTEIN.name}`);
    const link = await screen.findByRole("link", { name: "Import" });
    expect(link.getAttribute("href")).toBe(
      `/projects/kinase-atlas/entities/import/?type=${encodeURIComponent(PROTEIN.name)}`,
    );
  });

  it("isn't offered to a project's Viewers", async () => {
    const viewing = { ...KINASE, my_role: "viewer", can: { ...KINASE.can, edit: false } };
    withTypes();
    answer("GET /api/v1/projects", page([viewing]));
    answer("POST /api/v1/entities/search", page([entity()]));
    answer("GET /api/v1/entities/counts", { types: [], total: 1 });
    answer("GET /api/v1/saved-searches", []);
    await renderApp(`/projects/kinase-atlas/entities/?type=${PROTEIN.name}`);
    await screen.findByRole("table");
    expect(screen.queryByRole("link", { name: "Import" })).toBeNull();
  });

  it("asks what the rows are before a workbook is uploaded", async () => {
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
    const { user } = await renderApp("/projects/kinase-atlas/entities/import/");
    expect(screen.getByRole("heading", { level: 2, name: "Import a spreadsheet" })).toBeTruthy();
    const steps = screen.getByRole("list", { name: "Importing a spreadsheet" });
    expect(within(steps).getByText("Workbook").closest("li")?.getAttribute("aria-current")).toBe(
      "step",
    );
    const input = screen.getByLabelText("Upload a workbook");
    await waitFor(() => {
      expect(input.getAttribute("accept")?.split(",")).toEqual(
        expect.arrayContaining([".xlsx", ".xlsm", ".csv", ".tsv", ".txt"]),
      );
    });
    await user.upload(input, new File(["a,b\n1,2\n"], "plates.csv", { type: "text/csv" }));
    expect(await screen.findByText("Choose what its rows are first.")).toBeTruthy();
    expect(calls("POST")).toEqual([]);
  });

  it("uploads a delimited spreadsheet with its registered media type", async () => {
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
    answer("POST /api/v1/uploads", problem(503, "unavailable", "Storage is resting."));
    const { user } = await renderApp(
      `/projects/kinase-atlas/entities/import/?type=${PROTEIN.name}`,
    );
    const input = screen.getByLabelText("Upload a workbook");
    await waitFor(() => {
      expect(input.getAttribute("accept")).toContain(".tsv");
    });
    await user.upload(input, new File(["name\tlot\nExample\t1\n"], "inventory.tsv"));
    await waitFor(() => {
      expect(calls("POST", "/api/v1/uploads")[0]?.body).toMatchObject({
        name: "inventory.tsv",
        media_type: "text/tab-separated-values",
      });
    });
  });

  it("imports a workbook the project has already, picked from its Files", async () => {
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
    const workbook = {
      id: "00000000-0000-7000-8000-0000000051f1",
      name: "kinases.xlsx",
      media_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      size: 18_432,
      sha256: "a".repeat(64),
      project: { id: KINASE.id, name: KINASE.name, slug: KINASE.slug },
      created_by: null,
      agent: null,
      created_at: "2026-11-11T09:00:00Z",
      external: false,
      acquired: null,
      facts: NO_FACTS,
      scan: CLEAN_SCAN,
      used_by: { records: 0, runs: 0 },
    };
    answer("GET /api/v1/files", page([workbook]));
    answer("GET /api/v1/files/count", { total: 1, more_than_total: false });
    answer("GET /api/v1/files/formats", [
      { id: "iki/core.xlsx", label: "Excel", media_type: workbook.media_type, count: 1 },
    ]);
    answer(`POST /api/v1/projects/${KINASE.id}/spreadsheet-imports`, respond(201, sheet()));
    answer(`GET ${BASE}`, sheet());
    const { router, user } = await renderApp(
      `/projects/kinase-atlas/entities/import/?type=${PROTEIN.name}`,
    );
    await user.click(screen.getByRole("button", { name: "Or choose one already in Kinase Atlas" }));
    const picker = await screen.findByRole("dialog", { name: "Choose a file for the import" });
    expect(
      await within(picker).findByText(/It takes .*\.xlsx.*\.xlsm.*\.csv.*\.tsv.*\.txt.*100 MiB/),
    ).toBeTruthy();
    // Only workbooks are listed.
    await waitFor(() => {
      const asked = calls("GET", "/api/v1/files").at(-1)?.query;
      expect(asked?.get("project")).toBe(KINASE.id);
      expect(asked?.get("max_size")).toBe(String(100 * 1024 * 1024));
      expect(asked?.getAll("accepts").sort()).toEqual([
        "iki/core.csv",
        "iki/core.text",
        "iki/core.tsv",
        "iki/core.xlsm",
        "iki/core.xlsx",
      ]);
    });
    await user.click(await within(picker).findByRole("radio", { name: "kinases.xlsx" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/projects/kinase-atlas/entities/import/${ID}/`);
    });
    // Nothing is uploaded: the import reads the project's file as it is.
    expect(calls("POST", "/api/v1/uploads")).toEqual([]);
    expect(calls("POST", `/api/v1/projects/${KINASE.id}/spreadsheet-imports`)[0]?.body).toEqual({
      file: workbook.id,
      type: PROTEIN.name,
    });
  });

  it("starts with the type it was asked for", async () => {
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
    await renderApp(`/projects/kinase-atlas/entities/import/?type=${PROTEIN.name}`);
    expect(screen.getByRole("combobox", { name: "Its rows are" })).toHaveProperty(
      "value",
      PROTEIN.name,
    );
  });
});

describe("saving an import's choices", () => {
  it("saves them one at a time, each from the revision the one before left", async () => {
    onImport(sheet({ sheet: "Notes", header_row: 1, first_row: 2 }));
    let release: (() => void) | undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    answer(`PATCH ${BASE}`, async (call: Sent) => {
      const body = call.body as SheetChange;
      if (body.sheet) {
        // The sheet's answer is slow: the header row is typed meanwhile.
        await held;
        return sheet({ revision: 3, sheet: "Kinases", header_row: 1, first_row: 2 });
      }
      return sheet({ revision: 4, sheet: "Kinases", header_row: 3, first_row: 4 });
    });
    const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    const chosen = await screen.findByRole("combobox", { name: "Sheet" });
    await user.selectOptions(chosen, "Kinases");
    // What the person chose shows while it's saved.
    expect(chosen).toHaveProperty("value", "Kinases");
    expect(screen.getByText("Saving…")).toBeTruthy();
    const header = screen.getByRole("spinbutton", { name: "Header row" });
    await user.clear(header);
    await user.type(header, "3{Enter}");
    // Not sent until the change before it is saved.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(calls("PATCH", BASE).map((c) => c.body)).toEqual([{ sheet: "Kinases" }]);
    release?.();
    await waitFor(() => {
      expect(calls("PATCH", BASE).map((c) => c.body)).toEqual([
        { sheet: "Kinases" },
        { header_row: 3 },
      ]);
    });
    expect(calls("PATCH", BASE).map((c) => c.headers.get("If-Match"))).toEqual(['"2"', '"3"']);
    await waitFor(() => {
      expect(screen.getByRole("spinbutton", { name: "First row" })).toHaveProperty("value", "4");
    });
    expect(screen.getByRole("spinbutton", { name: "Header row" })).toHaveProperty("value", "3");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows what's saved when it changed elsewhere, and keeps what wasn't saved", async () => {
    onImport(sheet());
    answer(
      `PATCH ${BASE}`,
      problem(412, "stale", "It changed since you read it, in another tab or session."),
    );
    const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    // Someone changed its first row in another tab.
    answer(`GET ${BASE}`, sheet({ revision: 5, first_row: 3, updated_at: "2026-11-22T09:05:00Z" }));
    const skip = await screen.findByRole("textbox", { name: /Rows to leave out/ });
    await user.type(skip, "4{Enter}");
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("changed in another tab or session");
    expect(alert.textContent).toContain("with its first row as changed there");
    expect(within(alert).getByText("Rows to leave out: 4")).toBeTruthy();
    // What's saved, with what the person chose that wasn't, shown on it.
    expect(screen.getByRole("spinbutton", { name: "First row" })).toHaveProperty("value", "3");
    expect(skip).toHaveProperty("value", "4");
    answer(`PATCH ${BASE}`, sheet({ revision: 6, first_row: 3, skip: [[4, 4]] }));
    await user.click(within(alert).getByRole("button", { name: "Save my change" }));
    await waitFor(() => {
      expect(calls("PATCH", BASE)).toHaveLength(2);
    });
    const again = calls("PATCH", BASE)[1];
    expect(again?.headers.get("If-Match")).toBe('"5"');
    expect(again?.body).toEqual({ skip: [[4, 4]] });
    await waitFor(() => {
      expect(screen.queryByRole("alert")).toBeNull();
    });
  });

  it("lets go of a choice that was changed elsewhere too", async () => {
    onImport(sheet());
    answer(
      `PATCH ${BASE}`,
      problem(412, "stale", "It changed since you read it, in another tab or session."),
    );
    const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    answer(`GET ${BASE}`, sheet({ revision: 5, first_row: 4, updated_at: "2026-11-22T09:05:00Z" }));
    const first = await screen.findByRole("spinbutton", { name: "First row" });
    await user.clear(first);
    await user.type(first, "3{Enter}");
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("First row: 3")).toBeTruthy();
    expect(alert.textContent).toContain("Changed there too");
    await waitFor(() => {
      expect(first).toHaveProperty("value", "4");
    });
    await user.click(within(alert).getByRole("button", { name: "OK" }));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(calls("PATCH", BASE)).toHaveLength(1);
  });

  it("keeps a choice that's refused, with what's wrong with it", async () => {
    onImport(sheet());
    answer(
      `PATCH ${BASE}`,
      problem(422, "validation_failed", "Some fields are invalid.", [
        { pointer: "#/header_row", detail: "must be a row of the sheet, from 1 to 4, or 0." },
      ]),
    );
    const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    const header = await screen.findByRole("spinbutton", { name: "Header row" });
    await user.clear(header);
    await user.type(header, "9{Enter}");
    expect(await screen.findByText("must be a row of the sheet, from 1 to 4, or 0.")).toBeTruthy();
    expect(header).toHaveProperty("value", "9");
    await user.click(screen.getByRole("button", { name: "Next: columns" }));
    expect(
      screen.getByRole("button", { name: "Check every row" }).getAttribute("aria-disabled"),
    ).toBe("true");
  });
});

describe("an import's steps", () => {
  it("shows the sheet's first rows, its header marked and the rows it leaves out", async () => {
    onImport(sheet({ skip: [[4, 4]] }));
    await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    expect(
      await screen.findByRole("heading", { level: 2, name: /^Import kinases\.xlsx/ }),
    ).toBeTruthy();
    const table = screen.getByRole("table", { name: "Its first rows" });
    expect(within(table).getByRole("rowheader", { name: /^1\s*\(header\)$/ })).toBeTruthy();
    expect(within(table).getByRole("rowheader", { name: /^4\s*\(not imported\)$/ })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: /Rows to leave out/ })).toHaveProperty("value", "4");
  });

  it("says the rows to leave out, and what's wrong with how they're written", async () => {
    onImport(sheet());
    answer(`PATCH ${BASE}`, sheet({ skip: [[4, 4]] }));
    const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    const box = await screen.findByRole("textbox", { name: /Rows to leave out/ });
    await user.type(box, "4-{Enter}");
    expect(screen.getByText(/“4-” isn’t a row or rows/)).toBeTruthy();
    expect(calls("PATCH")).toEqual([]);
    await user.clear(box);
    await user.type(box, "4{Enter}");
    await waitFor(() => {
      expect(calls("PATCH", BASE).map((c) => c.body)).toEqual([{ skip: [[4, 4]] }]);
    });
  });

  it("maps a column to a field, and leaves another out", async () => {
    onImport(sheet());
    answer(`PATCH ${BASE}`, (call: { body: unknown }) =>
      sheet({ columns: (call.body as { columns: ColumnChoice[] }).columns.map(withExamples) }),
    );
    const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    await user.click(await screen.findByRole("button", { name: "Next: columns" }));
    const name = screen.getByRole("combobox", { name: "Field for A: Name" });
    // A field mapped already, and one a column can't set, can't be chosen.
    const options = within(name).getAllByRole("option");
    expect(options.find((o) => o.textContent === "Sequence (required) (mapped)")).toHaveProperty(
      "disabled",
      true,
    );
    expect(
      options.find((o) => o.textContent.startsWith("Molecular weight: It's computed")),
    ).toHaveProperty("disabled", true);
    await user.selectOptions(name, "");
    await waitFor(() => {
      expect(calls("PATCH", BASE)).toHaveLength(1);
    });
    const columns = (nthCall(0).body as { columns: { index: number; field: string | null }[] })
      .columns;
    expect(columns.map((c) => [c.index, c.field])).toEqual([
      [0, null],
      [1, "sequence"],
      [2, null],
    ]);
  });

  it("waits for the field edit before reviewing and saving a new mapping", async () => {
    onImport(sheet());
    let release!: (value: SheetImport) => void;
    answer(
      `PATCH ${BASE}`,
      () =>
        new Promise<SheetImport>((resolve) => {
          release = resolve;
        }),
    );
    answer(`POST ${BASE}/mappings`, respond(201, {}));
    const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    await user.click(await screen.findByRole("button", { name: "Next: columns" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Field for A: Name" }), "");
    const open = screen.getByRole("button", { name: "Save this mapping…" });
    expect(open).toHaveAttribute("aria-disabled", "true");
    await user.click(open);
    expect(screen.queryByRole("alertdialog")).toBeNull();
    const updated = sheet({
      revision: 3,
      columns: COLUMNS.map((c) => (c.index === 0 ? { ...c, field: null } : c)),
    });
    answer(`GET ${BASE}`, updated);
    act(() => {
      release(updated);
    });
    await user.click(open);
    const dialog = within(await screen.findByRole("alertdialog"));
    const source = within(dialog.getByRole("region", { name: "Mapping to save" }));
    expect(source.getByRole("rowheader", { name: "Sequence" })).toBeVisible();
    expect(source.queryByRole("rowheader", { name: "Name" })).toBeNull();
    await user.type(dialog.getByRole("textbox", { name: "Name" }), "New layout");
    await user.click(dialog.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(calls("POST", `${BASE}/mappings`).map((c) => c.body)).toEqual([
        { name: "New layout" },
      ]);
      expect(calls("POST", `${BASE}/mappings`)[0]?.headers.get("If-Match")).toBe('"3"');
    });
  });

  it.each(["source", "target", "deleted"] as const)(
    "keeps the reviewed mapping when the %s changes",
    async (which) => {
      const match = {
        id: "00000000-0000-7000-8000-0000000051aa",
        name: "Vendor layout",
        revision: 3,
        match: 1,
      };
      const lookup = `GET /api/v1/projects/${KINASE.id}/spreadsheet-mappings`;
      const original = sheet({ mapping: match, mappings: [match] });
      onImport(original);
      const target = {
        ...match,
        type: original.type,
        columns: [{ header: "Old label", field: "name" }],
        headers: ["Old label"],
        header_row: 1,
        locale: original.locale,
        created_by: PERSON,
        updated_by: PERSON,
        updated_at: original.updated_at,
      };
      answer(lookup, [target]);
      const detail =
        which === "source"
          ? "The source sheet changed. Review its mapping."
          : "The saved mapping changed. Review the current mapping.";
      // Replacing it is a PUT of the saved mapping, from its revision, naming the import
      // reviewed (ADR 0289): either changed is 412, and one deleted 404.
      const mappingAt = (mapping: string) => `/api/v1/spreadsheet-mappings/${mapping}`;
      const replaced = () =>
        calls("PUT").filter((c) => c.path.startsWith("/api/v1/spreadsheet-mappings/"));
      answer(`PUT ${mappingAt(match.id)}`, () =>
        which === "deleted" ? problem(404, "not_found", detail) : problem(412, "stale", detail),
      );
      const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
      await user.click(await screen.findByRole("button", { name: "Next: columns" }));
      await user.click(screen.getByRole("button", { name: "Save this mapping…" }));
      const dialog = within(await screen.findByRole("alertdialog"));
      const originalTarget = await dialog.findByRole("region", { name: "Current saved mapping" });
      expect(within(originalTarget).getByRole("rowheader", { name: "Old label" })).toBeVisible();
      const changedMatch =
        which === "source"
          ? match
          : {
              ...match,
              revision: 4,
              name: which === "deleted" ? match.name : "Revised vendor layout",
            };
      const latest = sheet({
        revision: which === "source" ? 3 : 2,
        mapping: match, // The historical source remains pinned even when its live target disappears.
        mappings: [which === "deleted" ? { ...changedMatch, id: ID } : changedMatch],
        ...(which === "source"
          ? {
              header_row: 2,
              columns: COLUMNS.map((c) =>
                c.index === 0 ? { ...c, header: "Current source label" } : c,
              ),
            }
          : {}),
      });
      answer(`GET ${BASE}`, latest);
      answer(
        lookup,
        which === "source"
          ? [target]
          : [
              {
                ...target,
                ...changedMatch,
                id: which === "deleted" ? ID : match.id,
                columns: [{ header: "Current target label", field: "name" }],
                locale: { ...original.locale, decimal: "comma" },
              },
            ],
      );
      await act(async () => {
        await queryClient.invalidateQueries();
      });
      await user.click(dialog.getByRole("button", { name: "Save changes" }));
      await dialog.findByText(detail);
      const first = {
        "If-Match": '"3"',
        name: match.name,
        import: ID,
        import_revision: 2,
      };
      expect(replaced().map((c) => c.path)).toEqual([mappingAt(match.id)]);
      expect(replaced().map(made)).toEqual([first]);
      expect(calls("POST", `${BASE}/mappings`)).toHaveLength(0);
      expect(dialog.getByRole("button", { name: "Save changes" })).toHaveAttribute(
        "aria-disabled",
        "true",
      );
      await user.click(dialog.getByRole("button", { name: "Review latest mapping" }));
      if (which === "deleted") {
        await dialog.findByText(/no longer available/i);
        expect(dialog.getByRole("button", { name: "Save changes" })).toHaveAttribute(
          "aria-disabled",
          "true",
        );
        await user.selectOptions(
          dialog.getByRole("combobox", { name: "Choose a different mapping" }),
          ID,
        );
        const current = within(dialog.getByRole("region", { name: "Current saved mapping" }));
        expect(current.getByRole("rowheader", { name: "Current target label" })).toBeVisible();
        expect(replaced()).toHaveLength(1);
        answer(`PUT ${mappingAt(ID)}`, respond(200, {}));
        await user.click(dialog.getByRole("button", { name: "Save changes" }));
        await waitFor(() => {
          expect(replaced().map((c) => c.path)).toEqual([mappingAt(match.id), mappingAt(ID)]);
          expect(replaced().map(made)).toEqual([
            first,
            { "If-Match": '"4"', name: match.name, import: ID, import_revision: latest.revision },
          ]);
        });
        return;
      }
      const currentTarget = within(
        await dialog.findByRole("region", { name: "Current saved mapping" }),
      );
      await currentTarget.findByRole("rowheader", {
        name: which === "source" ? "Old label" : "Current target label",
      });
      if (which === "target") expect(currentTarget.getByText(/decimal comma/i)).toBeVisible();
      const currentSource = within(dialog.getByRole("region", { name: "Mapping to save" }));
      expect(
        currentSource.getByRole("rowheader", {
          name: which === "source" ? "Current source label" : "Name",
        }),
      ).toBeVisible();
      expect(replaced()).toHaveLength(1);
      answer(`PUT ${mappingAt(match.id)}`, respond(200, {}));
      await user.click(dialog.getByRole("button", { name: "Save changes" }));
      await waitFor(() => {
        expect(replaced().map(made)).toEqual([
          first,
          {
            "If-Match": `"${String(changedMatch.revision)}"`,
            name: changedMatch.name,
            import: ID,
            import_revision: latest.revision,
          },
        ]);
      });
    },
  );

  it("flags cells that read two ways until the sheet's locale is said", async () => {
    onImport(sheet());
    answer(
      `PATCH ${BASE}`,
      sheet({ locale: { decimal: "point", dates: null, serial_dates: false } }),
    );
    const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    await user.click(await screen.findByRole("button", { name: "Next: columns" }));
    expect(
      screen.getByText("1 cell below reads two ways: say how the sheet is written."),
    ).toBeTruthy();
    const readings = screen.getByRole("table", { name: /How numbers in the mapped columns read/ });
    expect(within(readings).getByText("Reads two ways")).toBeTruthy();
    expect(within(readings).getByText("stored as such")).toBeTruthy();
    await user.click(screen.getByRole("radio", { name: "A decimal point: 1,234.5" }));
    await waitFor(() => {
      expect(calls("PATCH", BASE).map((c) => c.body)).toEqual([
        { locale: { decimal: "point", dates: null, serial_dates: false } },
      ]);
    });
  });

  it("shows what importing would do, its problems by row and column, then imports", async () => {
    onImport(PREVIEWED);
    answer(`GET ${BASE}/rows`, (call: { query: URLSearchParams }) =>
      page(
        call.query.get("outcome") === "problem"
          ? [
              {
                row: 5,
                outcome: "problem",
                match: "",
                match_id: null,
                same_as: null,
                problems: [
                  {
                    row: 5,
                    column: 1,
                    field: "sequence",
                    detail: "Sequence is required.",
                    kind: null,
                  },
                ],
                held: [],
              },
            ]
          : [],
      ),
    );
    answer(
      `POST ${BASE}/commit`,
      respond(
        202,
        sheet({
          state: "committing",
          run: { id: "r1", hrid: "RUN-000042", status: "queued", progress: null, message: "" },
        }),
      ),
    );
    const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    expect(await screen.findByText(/1 row has problems/)).toBeTruthy();
    const counts = screen.getByText("new records").closest("ul");
    expect(counts?.textContent).toContain("2 new records");
    expect(counts?.textContent).toContain("1 record the project has already");
    expect(counts?.textContent).toContain("1 row the same as an earlier one");
    const rows = await screen.findByRole("table", { name: /Has problems/ });
    expect(within(rows).getByRole("rowheader", { name: "5" })).toBeTruthy();
    expect(within(rows).getByText("B")).toBeTruthy();
    expect(within(rows).getByText(/Sequence is required/)).toBeTruthy();
    // Each problem by its sheet, row and column.
    expect(screen.getByRole("heading", { level: 3, name: "Rows of Kinases" })).toBeTruthy();
    expect(screen.getByText(/One run writes 4 rows as proteins, in row order/)).toBeTruthy();
    // Nothing is imported while a row has problems, unless they're left out.
    const importing = screen.getByRole("button", { name: "Import 4 rows" });
    expect(importing.getAttribute("aria-disabled")).toBe("true");
    await user.click(screen.getByRole("checkbox", { name: "Leave out the 1 row with problems" }));
    await user.type(screen.getByRole("textbox", { name: /Why/ }), "Batch 12");
    await user.click(screen.getByRole("button", { name: "Import 4 rows" }));
    await waitFor(() => {
      expect(calls("POST", `${BASE}/commit`).map((c) => c.body)).toEqual([
        { skip_problems: true, reason: "Batch 12" },
      ]);
    });
    expect(await screen.findByRole("link", { name: "RUN-000042" })).toBeTruthy();
  });

  it.each(["refresh", "refusal"])(
    "reviews the current preview after a %s, even when its totals match",
    async (cause) => {
      const first = PREVIEWED;
      const next = { ...first, revision: first.revision + 1 };
      let current = first;
      onImport(first);
      answer(`GET ${BASE}`, () => current);
      answer(`GET ${BASE}/rows`, () =>
        page([
          {
            row: current.revision + 10,
            outcome: "problem",
            match: "",
            match_id: null,
            same_as: null,
            problems: [
              {
                row: current.revision + 10,
                column: 0,
                field: "name",
                detail: `Problem from preview ${String(current.revision)}.`,
                kind: null,
              },
            ],
            held: [],
          },
        ]),
      );
      answer(`POST ${BASE}/commit`, (call: Sent) =>
        call.headers.get("If-Match") === `"${String(current.revision)}"`
          ? { ...current, state: "committing", can: {} }
          : problem(412, "stale", "The preview changed. Review its current rows."),
      );
      const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
      await screen.findByText(/Problem from preview 2/);
      await user.click(screen.getByRole("checkbox", { name: /Leave out the/ }));
      await user.type(screen.getByRole("textbox", { name: /Why/ }), "Old reason");
      current = next;
      if (cause === "refusal") {
        await user.click(screen.getByRole("button", { name: "Import 4 rows" }));
      } else {
        await act(() =>
          queryClient.invalidateQueries({ queryKey: ["spreadsheet-imports", ID], exact: true }),
        );
      }
      const review = await screen.findByRole("button", { name: "Review current preview" });
      await screen.findByText(/Problem from preview 3/);
      expect(calls("GET", `${BASE}/rows`).map((call) => call.headers.get("If-Match"))).toContain(
        '"3"',
      );
      expect(screen.queryByRole("button", { name: "Import 4 rows" })).not.toBeInTheDocument();
      expect(calls("POST", `${BASE}/commit`)).toHaveLength(cause === "refusal" ? 1 : 0);
      await user.click(review);
      const form = screen.getByRole("form", { name: "Import" });
      expect(within(form).getByRole("checkbox")).not.toBeChecked();
      expect(within(form).getByRole("textbox", { name: /Why/ })).toHaveValue("");
      await user.click(within(form).getByRole("checkbox"));
      await user.type(within(form).getByRole("textbox", { name: /Why/ }), "Fresh review");
      await user.click(within(form).getByRole("button", { name: "Import 4 rows" }));
      await waitFor(() => {
        const sent = calls("POST", `${BASE}/commit`).at(-1);
        expect(sent?.headers.get("If-Match")).toBe('"3"');
        expect(sent?.body).toEqual({ skip_problems: true, reason: "Fresh review" });
      });
    },
  );

  it("stops reviewing a superseded preview when loading the next page of rows", async () => {
    let current = PREVIEWED;
    onImport(current);
    answer(`GET ${BASE}`, () => current);
    answer(`GET ${BASE}/rows`, (call: Sent) => {
      if (call.headers.get("If-Match") !== `"${String(current.revision)}"`) {
        return problem(412, "stale", "These preview rows have changed.");
      }
      return {
        items: [
          {
            row: current.revision,
            outcome: "problem",
            match: "",
            match_id: null,
            same_as: null,
            problems: [
              {
                row: current.revision,
                column: 0,
                field: "name",
                detail: `Missing name in revision ${String(current.revision)}.`,
                kind: null,
              },
            ],
            held: [],
          },
        ],
        next_cursor: current.revision === 2 ? "next-page" : null,
      };
    });
    const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    await screen.findByText(/Missing name in revision 2/);
    await user.click(screen.getByRole("checkbox", { name: /Leave out the/ }));
    current = { ...current, revision: 3 };
    await user.click(screen.getByRole("button", { name: "Show more rows" }));
    await screen.findByText(/Missing name in revision 3/);
    expect(screen.queryByText(/Missing name in revision 2/)).not.toBeInTheDocument();
    expect(
      calls("GET", `${BASE}/rows`).map((call) => [
        call.headers.get("If-Match"),
        call.query.get("cursor"),
      ]),
    ).toEqual([
      ['"2"', null],
      ['"2"', "next-page"],
      ['"3"', null],
    ]);
    await user.click(screen.getByRole("button", { name: "Review current preview" }));
    expect(screen.getByRole("checkbox", { name: /Leave out the/ })).not.toBeChecked();
    expect(calls("POST", `${BASE}/commit`)).toHaveLength(0);
  });

  it("holds approval while the rows belonging to this preview are unavailable", async () => {
    onImport(PREVIEWED);
    answer(`GET ${BASE}/rows`, problem(403, "forbidden", "The preview rows are unavailable."));
    const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    await screen.findByText("The preview rows are unavailable.");
    await user.click(screen.getByRole("checkbox", { name: /Leave out the/ }));
    const button = screen.getByRole("button", { name: "Import 4 rows" });
    expect(button).toHaveAttribute("aria-disabled", "true");
    await user.click(button);
    expect(calls("POST", `${BASE}/commit`)).toHaveLength(0);
    expect(calls("GET", `${BASE}/rows`)[0]?.headers.get("If-Match")).toBe('"2"');
  });

  it("links to the records an import wrote", async () => {
    const run = { id: "r1", hrid: "RUN-000042", status: "succeeded", progress: 1, message: "" };
    onImport({ ...PREVIEWED, state: "imported", run, can: {} });
    answer("POST /api/v1/entities/search", page([entity()]));
    answer("GET /api/v1/entities/counts", { types: [], total: 1 });
    answer("GET /api/v1/saved-searches", []);
    const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    expect(
      await screen.findByText("Imported 4 rows of kinases.xlsx into Kinase Atlas."),
    ).toBeTruthy();
    await user.click(screen.getByRole("link", { name: "Show the imported records" }));
    expect(await screen.findByText(/Records written by run/)).toBeTruthy();
    await waitFor(() => {
      expect(calls("POST", "/api/v1/entities/search").at(-1)?.body).toMatchObject({
        run: "RUN-000042",
        type: PROTEIN.name,
      });
    });
  });

  it("says why an import failed, by row and column", async () => {
    const run = { id: "r1", hrid: "RUN-000042", status: "failed", progress: 1, message: "" };
    onImport({
      ...PREVIEWED,
      state: "failed",
      error: "Nothing was imported: 1 row no longer fits.",
      run,
      run_problems: [
        {
          row: 3,
          column: 0,
          field: "name",
          detail: "Another record has Name EGFR.",
          kind: null,
        },
      ],
      can: {},
    });
    await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("1 row no longer fits");
    const why = screen.getByRole("table", { name: "Why" });
    expect(within(why).getByRole("rowheader", { name: "3" })).toBeTruthy();
    expect(within(why).getByText(/Another record has Name EGFR/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Import kinases.xlsx again" })).toBeTruthy();
  });
});

/** A column as PATCH took it, as the import then says it. */
function withExamples(c: ColumnChoice): SheetColumn {
  const was = nthColumn(c.index);
  return {
    ...was,
    field: c.field ?? null,
    separator: c.separator ?? ";",
    values: c.values ?? {},
  };
}

function nthCall(index: number) {
  const found = calls("PATCH", BASE)[index];
  if (!found) throw new Error(`No PATCH ${String(index)}`);
  return found;
}
