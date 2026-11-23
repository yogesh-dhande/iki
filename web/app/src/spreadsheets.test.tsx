/**
 * Importing a spreadsheet (ADR 0178): what the steps work out themselves, where an import
 * starts from, and each step against a fake API.
 */

import type { Schemas } from "@ikisci/sdk";
import { act, screen, waitFor, within } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { queryClient } from "./api";
import { entity, KINASE, PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import {
  columnName,
  columnsWith,
  counted,
  importQuery,
  importable,
  parseRanges,
  rangesText,
  type ColumnChoice,
  type SheetColumn,
  type SheetImport,
} from "./spreadsheets";
import { answer, calls, page, problem, respond } from "./test-server";

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
    expect(input.getAttribute("accept")).toContain(".xlsx");
    await user.upload(input, new File(["a,b\n1,2\n"], "plates.csv", { type: "text/csv" }));
    expect(await screen.findByText("Choose what its rows are first.")).toBeTruthy();
    expect(calls("POST")).toEqual([]);
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
      used_by: { records: 0, runs: 0 },
    };
    answer("GET /api/v1/files", page([workbook]));
    answer("GET /api/v1/files/count", { total: 1, more_than_total: false });
    answer("GET /api/v1/files/formats", [{ media_type: workbook.media_type, count: 1 }]);
    answer(`POST /api/v1/projects/${KINASE.id}/spreadsheet-imports`, respond(201, sheet()));
    answer(`GET ${BASE}`, sheet());
    const { router, user } = await renderApp(
      `/projects/kinase-atlas/entities/import/?type=${PROTEIN.name}`,
    );
    await user.click(screen.getByRole("button", { name: "Or choose one already in Kinase Atlas" }));
    const picker = await screen.findByRole("dialog", { name: "Choose a file for the import" });
    expect(
      within(picker).getByText("It takes Excel (.xlsx) or CSV (.csv) files, up to 100 MiB."),
    ).toBeTruthy();
    // Only workbooks are listed.
    await waitFor(() => {
      const asked = calls("GET", "/api/v1/files").at(-1)?.query;
      expect(asked?.get("project")).toBe(KINASE.id);
      expect(asked?.get("max_size")).toBe(String(100 * 1024 * 1024));
      expect(asked?.getAll("accepts").sort()).toEqual([
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv",
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

describe("an import's steps", () => {
  it("keeps a rejected header visible until it is fixed or restored", async () => {
    onImport(sheet());
    answer(`PATCH ${BASE}`, () =>
      problem(422, "validation_failed", "Some fields are invalid.", [
        { pointer: "#/header_row", detail: "The header is outside this sheet." },
      ]),
    );
    const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    const header = await screen.findByRole("spinbutton", { name: "Header row" });
    const next = screen.getByRole("button", { name: "Next: columns" });
    await user.clear(header);
    await user.type(header, "99{Enter}");
    await waitFor(() => expect(header).toHaveAttribute("aria-invalid", "true"));
    expect(header).toHaveValue(99);
    expect(header).toHaveAccessibleDescription(/outside this sheet.*Escape.*columns’ names/);
    expect(next).toHaveAttribute("aria-disabled", "true");
    await user.click(next);
    expect(calls("PATCH", BASE)).toHaveLength(1);
    expect(calls("POST", `${BASE}/preview`)).toHaveLength(0);
    expect(screen.getByRole("heading", { name: "Sheet and rows" })).toBeVisible();
    // A save to another field must not hide the rejected header or allow advancement.
    const last = screen.getByRole("spinbutton", { name: /Last row/ });
    answer(`PATCH ${BASE}`, sheet({ last_row: 4 }));
    await user.type(last, "4{Enter}");
    await waitFor(() => expect(last).toBeEnabled());
    expect(header).toHaveAttribute("aria-invalid", "true");
    expect(next).toHaveAttribute("aria-disabled", "true");
    // Returning to the acknowledged header needs no duplicate save.
    await user.clear(header);
    await user.type(header, "1");
    expect(header).not.toHaveAttribute("aria-invalid");
    expect(next).not.toHaveAttribute("aria-disabled");
    await user.click(next);
    expect(await screen.findByRole("button", { name: "Check every row" })).toBeVisible();
    expect(calls("PATCH", BASE)).toHaveLength(2);
  });

  it("restores invalid local ranges and releases an old rejection on a genuine sheet switch", async () => {
    onImport(sheet());
    const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    const skip = await screen.findByRole("textbox", { name: /Rows to leave out/ });
    const next = screen.getByRole("button", { name: "Next: columns" });
    await user.type(skip, "5-{Enter}");
    expect(skip).toHaveAccessibleDescription(/isn’t a row.*Escape.*Such as/);
    expect(next).toHaveAttribute("aria-disabled", "true");
    await user.keyboard("{Escape}");
    expect(skip).toHaveValue("");
    expect(next).not.toHaveAttribute("aria-disabled");
    expect(calls("PATCH", BASE)).toHaveLength(0);
    answer(`PATCH ${BASE}`, () =>
      problem(422, "validation_failed", "Invalid", [
        { pointer: "#/skip/0", detail: "The range is outside this sheet." },
      ]),
    );
    await user.type(skip, "98-99{Enter}");
    await waitFor(() => expect(skip).toHaveAttribute("aria-invalid", "true"));
    expect(skip).toHaveAccessibleDescription(/range is outside this sheet/);
    answer(`PATCH ${BASE}`, sheet({ sheet: "Notes" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Sheet" }), "Notes");
    await waitFor(() => expect(next).not.toHaveAttribute("aria-disabled"));
    expect(screen.getByRole("textbox", { name: /Rows to leave out/ })).toHaveValue("");
    expect(calls("PATCH", BASE)).toHaveLength(2);
  });

  it("requires a deliberate reload to review a stale sheet and header basis", async () => {
    onImport(sheet());
    const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    const header = await screen.findByRole("spinbutton", { name: "Header row" });
    answer(`PATCH ${BASE}`, problem(409, "conflict", "The selected sheet or header has changed."));
    await user.clear(header);
    await user.type(header, "3{Enter}");
    expect(await screen.findByText("The selected sheet or header has changed.")).toBeVisible();
    await user.keyboard("{Escape}");
    const next = screen.getByRole("button", { name: "Next: columns" });
    expect(next).toHaveAttribute("aria-disabled", "true");
    answer(`GET ${BASE}`, sheet({ sheet: "Notes", header_row: 0, first_row: 1 }));
    await user.click(screen.getByRole("button", { name: "Reload saved choices" }));
    await waitFor(() => expect(next).not.toHaveAttribute("aria-disabled"));
    expect(screen.getByRole("combobox", { name: "Sheet" })).toHaveValue("Notes");
    expect(screen.getByRole("spinbutton", { name: "Header row" })).toHaveValue(0);
    expect(calls("PATCH", BASE)).toHaveLength(1);
  });

  it("waits for a sheet switch before accepting choices relative to its header", async () => {
    onImport(sheet({ header_row: 3, first_row: 4 }));
    let finishSwitch!: (snapshot: SheetImport) => void;
    const pending = new Promise<SheetImport>((resolve) => {
      finishSwitch = resolve;
    });
    answer(`PATCH ${BASE}`, () => pending);
    const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    await user.selectOptions(await screen.findByRole("combobox", { name: "Sheet" }), "Notes");
    expect(screen.getByRole("spinbutton", { name: "Header row" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next: columns" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(calls("PATCH", BASE).map((c) => c.body)).toEqual([
      { sheet: "Notes", basis: { sheet: "Kinases", header_row: 3 } },
    ]);
    await act(async () => {
      finishSwitch(sheet({ sheet: "Notes" }));
      await pending;
    });
    expect(await screen.findByText(/Switched to Notes.*from 3 to 1/)).toBeVisible();
    const first = screen.getByRole("spinbutton", { name: "First row" });
    answer(`PATCH ${BASE}`, sheet({ sheet: "Notes", first_row: 1, header_row: 0 }));
    const header = screen.getByRole("spinbutton", { name: "Header row" });
    await user.clear(header);
    await user.type(header, "0{Enter}");
    await waitFor(() => expect(first).toHaveValue(1));
    expect(nthCall(1).body).toEqual({ header_row: 0, basis: { sheet: "Notes", header_row: 1 } });
  });

  it("keeps an acknowledged header when an older refresh arrives", async () => {
    const initial = sheet();
    const newer = sheet({ header_row: 3, first_row: 4 });
    onImport(initial);
    const { user } = await renderApp(`/projects/kinase-atlas/entities/import/${ID}/`);
    const header = await screen.findByRole("spinbutton", { name: "Header row" });
    let finishRead!: (snapshot: SheetImport) => void;
    const slowRead = new Promise<SheetImport>((resolve) => {
      finishRead = resolve;
    });
    answer(`GET ${BASE}`, () => slowRead);
    const refresh = queryClient.refetchQueries({ queryKey: importQuery(ID).queryKey });
    await waitFor(() => {
      expect(calls("GET", BASE)).toHaveLength(2);
    });
    answer(`PATCH ${BASE}`, newer);
    await user.clear(header);
    await user.type(header, "3{Enter}");
    const first = screen.getByRole("spinbutton", { name: "First row" });
    await waitFor(() => expect(first).toHaveValue(4));
    await act(async () => {
      finishRead(initial);
      await refresh;
    });
    expect(queryClient.getQueryData(importQuery(ID).queryKey)).toEqual(newer);
    expect(header).toHaveValue(3);
    expect(first).toHaveValue(4);
  });

  it.each([200, 422])("ignores an earlier visit's late %i", async (status) => {
    onImport(sheet());
    let finishOld!: (snapshot: SheetImport | Response) => void;
    const old = new Promise<SheetImport | Response>((resolve) => {
      finishOld = resolve;
    });
    answer(`PATCH ${BASE}`, () => old);
    const path = `/projects/kinase-atlas/entities/import/${ID}/`;
    const { router, user } = await renderApp(path);
    const header = await screen.findByRole("spinbutton", { name: "Header row" });
    await user.clear(header);
    await user.type(header, "2{Enter}");
    await waitFor(() => {
      expect(calls("PATCH", BASE)).toHaveLength(1);
    });

    await act(() => router.navigate({ to: "/" }));
    await screen.findByRole("heading", { name: /Welcome/ });
    await act(() => router.navigate({ href: path }));
    const newer = sheet({ header_row: 3, first_row: 4 });
    answer(`PATCH ${BASE}`, newer);
    answer(`GET ${BASE}`, newer);
    const again = await screen.findByRole("spinbutton", { name: "Header row" });
    await user.clear(again);
    await user.type(again, "3{Enter}");
    const first = screen.getByRole("spinbutton", { name: "First row" });
    await waitFor(() => expect(first).toHaveValue(4));
    await act(async () => {
      finishOld(
        status === 200
          ? sheet({ header_row: 2, first_row: 3 })
          : problem(422, "validation_failed", "Old header rejected", [
              { pointer: "#/header_row", detail: "The old header is invalid." },
            ]),
      );
      await old;
    });
    await waitFor(() => {
      expect(queryClient.isMutating()).toBe(0);
    });
    expect(queryClient.getQueryData(importQuery(ID).queryKey)).toEqual(newer);
    expect(again).toHaveValue(3);
    expect(first).toHaveValue(4);
    expect(again).not.toHaveAttribute("aria-invalid");
    expect(screen.getByRole("button", { name: "Next: columns" })).not.toHaveAttribute(
      "aria-disabled",
    );
  });

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
      expect(calls("PATCH", BASE).map((c) => c.body)).toEqual([
        { skip: [[4, 4]], basis: { sheet: "Kinases", header_row: 1 } },
      ]);
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
        {
          locale: { decimal: "point", dates: null, serial_dates: false },
          basis: { sheet: "Kinases", header_row: 1 },
        },
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
