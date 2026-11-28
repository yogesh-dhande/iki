import type { Schemas } from "@ikisci/sdk";
import { describe, expect, it } from "vitest";

import { entity, field, nth } from "./entity-fixtures";
import {
  AMOUNT,
  baseText,
  cellText,
  checkRow,
  clipboardBlock,
  did,
  EMPTY,
  fillDown,
  ID,
  LOCATION,
  moved,
  NONE_LOADED,
  parseHtmlTable,
  parseTsv,
  placePaste,
  problemsAt,
  rangeOf,
  redo,
  reloaded,
  saveRow,
  sheetColumns,
  textAt,
  toTsv,
  undo,
  whyLocked,
  whyNotInACell,
  withNewRows,
  withoutNewRows,
  written,
  type History,
  type SheetRow,
} from "./sheet";

/** A kinase lab's samples: a label, the protein, purity, concentration in mg/mL (kept
 * in g/L), tags, a due date, who prepared it, a status that locks, and an amount. */
const SAMPLE: Schemas["EntityTypeOut"] = {
  name: "acme/protein.Sample",
  plugin: "acme/protein",
  label: "Sample",
  label_plural: "Samples",
  description: null,
  extends: null,
  abstract: false,
  sample: true,
  lab_list: false,
  detached: false,
  subtypes: [],
  hrid_prefix: "SMP",
  identity: [],
  viewers: [],
  notifications: [],
  values_schema: {},
  storage: { label: null, barcode: null, confirm: [], grid: null, holds: [] },
  amount: {
    label: "Volume",
    unit: "uL",
    display_unit: null,
    concentration: null,
    aliquots: null,
    aliquots_of: null,
    description: null,
    material: null,
    whole_units: false,
  },
  fields: [
    field({ name: "label", label: "Label", type: "string", required: true, unique: true }),
    field({ name: "protein", label: "Protein", type: "ref", to: ["acme/protein.Protein"] }),
    field({ name: "purity", label: "Purity", type: "float" }),
    field({
      name: "concentration",
      label: "Concentration",
      type: "quantity",
      unit: "g/L",
      unit_symbol: "g/L",
      display_unit: "mg/mL",
      display_unit_symbol: "mg/mL",
      display_scale: 1,
      display_offset: 0,
    }),
    field({ name: "tags", label: "Tags", type: "list", items: "string" }),
    field({ name: "due_on", label: "Due on", type: "date" }),
    field({ name: "prepared_by", label: "Prepared by", type: "person" }),
    field({ name: "lot_code", label: "Lot code", type: "string", immutable: true }),
    field({ name: "status", label: "Status", type: "state" }),
    field({ name: "mass", label: "Mass", type: "quantity", unit: "u", computed: true }),
  ],
};

const ABL1 = "00000000-0000-7000-8000-00000000a001";
const ANA = "00000000-0000-7000-8000-00000000b001";

function sample(hrid: string, values: Record<string, unknown> = {}, extra = {}) {
  return entity({
    id: `00000000-0000-7000-8000-${hrid.slice(-6).padStart(12, "0")}`,
    hrid,
    type: SAMPLE.name,
    revision: 3,
    values: { label: hrid, ...values },
    refs: {
      [ABL1]: {
        hrid: "PRT-000001",
        title: "ABL1",
        type: "acme/protein.Protein",
        archived: false,
        purged: false,
      },
    },
    people: { [ANA]: { id: ANA, name: "Ana Lima", email: "ana@example.com", active: true } },
    ...extra,
  });
}

const columns = sheetColumns(SAMPLE);
const col = (key: string) => {
  const found = columns.findIndex((c) => c.key === key);
  if (found < 0) throw new Error(`No column ${key}`);
  return found;
};
const column = (key: string) => nth(columns, col(key));

describe("a sheet's columns", () => {
  it("are the ID, the fields a cell changes in the type's order, place and amount, then the rest", () => {
    expect(columns.map((c) => c.label)).toEqual([
      "ID",
      "Label",
      "Protein",
      "Purity",
      "Concentration (mg/mL)",
      "Tags",
      "Due on",
      "Prepared by",
      "Lot code",
      "Location",
      "Volume (uL)",
      "Status",
      "Mass (u)",
    ]);
    expect(column("status").readOnly).toMatch(/a state/);
    expect(column("mass").readOnly).toMatch(/computed/);
    expect(column(LOCATION).newOnly).toBe(true);
  });

  it("say a field worked out by an expression is, not that an action sets it", () => {
    const worked = field({
      name: "fraction",
      label: "Fraction",
      type: "float",
      computed: true,
      cel: "record.purity / 100.0",
      cel_reads: ["purity"],
    });
    expect(whyNotInACell(worked)).toBe(
      "It’s worked out from the record’s other values as it’s saved.",
    );
  });

  it("leave out a place for a type whose records are scanned to be put away", () => {
    const storage = {
      label: null,
      barcode: null,
      grid: null,
      holds: [],
      confirm: ["move" as const],
    };
    const scanned = { ...SAMPLE, storage };
    expect(sheetColumns(scanned).some((c) => c.key === LOCATION)).toBe(false);
  });
});

describe("a cell's text", () => {
  const record = sample("SMP-000004", {
    protein: ABL1,
    purity: 97.5,
    concentration: 1.25,
    tags: ["kinase", "crystallography"],
    due_on: "2027-04-03",
    prepared_by: ANA,
  });

  it("is what would be typed back: HRIDs, emails, numbers in full, ISO dates, lists by ;", () => {
    const row: SheetRow = { key: record.id, entity: record };
    expect(columns.slice(0, 8).map((c) => baseText(row, c))).toEqual([
      "SMP-000004",
      "SMP-000004",
      "PRT-000001",
      "97.5",
      "1.25",
      "kinase; crystallography",
      "2027-04-03",
      "ana@example.com",
    ]);
  });

  it("is in the unit its heading says", () => {
    const weight = field({
      name: "mw",
      label: "MW",
      type: "quantity",
      unit: "u",
      display_unit: "kDa",
      display_scale: 0.001,
      display_offset: 0,
    });
    expect(cellText(weight, 11981)).toBe("11.981");
    expect(cellText(field({ name: "ok", label: "OK", type: "bool" }), false)).toBe("No");
  });
});

describe("which cells can change", () => {
  const open = sample("SMP-000001", { lot_code: "L-17" });
  const row = { key: open.id, entity: open };

  it("is every field a cell holds, but one set once and set", () => {
    expect(whyLocked(row, column("purity"), SAMPLE, "Kinase Atlas")).toBeNull();
    expect(whyLocked(row, column("lot_code"), SAMPLE, "Kinase Atlas")).toBe(
      "Set once: it can’t change.",
    );
    expect(whyLocked({ key: "new-1", number: 1 }, column("lot_code"), SAMPLE, "")).toBeNull();
  });

  it("says why a record's can't", () => {
    const locked = sample("SMP-000002", {}, { locked: true, locked_by: ["status"] });
    expect(whyLocked({ key: locked.id, entity: locked }, column("purity"), SAMPLE, "")).toBe(
      "SMP-000002 is locked by Status.",
    );
    const theirs = sample(
      "SMP-000003",
      {},
      {
        can: { ...open.can, edit: false },
        owner_project: { id: "p2", name: "Structural Biology", slug: "sb" },
      },
    );
    expect(whyLocked({ key: theirs.id, entity: theirs }, column("purity"), SAMPLE, "")).toBe(
      "Only Editors in its owner project, Structural Biology, can change it.",
    );
  });

  it("keeps an existing record's place and amount to its page", () => {
    expect(whyLocked(row, column(LOCATION), SAMPLE, "")).toMatch(/in Storage/);
    expect(whyLocked(row, column(AMOUNT), SAMPLE, "")).toMatch(/ledger/);
    expect(whyLocked({ key: "new-1", number: 1 }, column(AMOUNT), SAMPLE, "")).toBeNull();
  });
});

describe("the clipboard", () => {
  it("reads Excel's tab-separated text, quoted cells and all", () => {
    const copied =
      'ABL1-KD-001\t97,5\t"Aggregates\nabove 2 mg/mL"\r\nABL1-KD-002\t\t"say ""hi"""\r\n';
    expect(parseTsv(copied)).toEqual([
      ["ABL1-KD-001", "97,5", "Aggregates\nabove 2 mg/mL"],
      ["ABL1-KD-002", "", 'say "hi"'],
    ]);
    expect(parseTsv("98.5")).toEqual([["98.5"]]);
    expect(parseTsv("a\tb\nc")).toEqual([
      ["a", "b"],
      ["c", ""],
    ]);
  });

  it("writes what it copies so a spreadsheet reads it back the same", () => {
    const block = [
      ["SMP-000001", "Aggregates\nabove 2 mg/mL"],
      ["SMP-000002", 'say "hi"'],
    ];
    expect(parseTsv(toTsv(block))).toEqual(block);
  });

  it("reads a web page's table when its text has fewer columns", () => {
    const html =
      "<table><tr><th>Label</th><th>Purity</th></tr>" +
      "<tr><td>ABL1-KD-001</td><td>97.5</td></tr>" +
      '<tr><td colspan="2">EGFR-KD-001<br>rerun</td></tr></table>';
    expect(parseHtmlTable(html)).toEqual([
      ["Label", "Purity"],
      ["ABL1-KD-001", "97.5"],
      ["EGFR-KD-001\nrerun", ""],
    ]);
    expect(clipboardBlock("Label Purity\nABL1-KD-001 97.5", html)).toHaveLength(3);
    expect(clipboardBlock("a\tb", "<table><tr><td>a</td></tr></table>")).toEqual([["a", "b"]]);
  });
});

describe("a paste", () => {
  const size = { rows: 5, cols: 4 };

  it("fills right and down from the selected cell, in the copied order", () => {
    const placed = placePaste(
      [
        ["1", "2"],
        ["3", "4"],
      ],
      rangeOf({ row: 1, col: 1 }, { row: 1, col: 1 }),
      size,
    );
    expect(placed.writes).toEqual([
      { row: 1, col: 1, text: "1" },
      { row: 1, col: 2, text: "2" },
      { row: 2, col: 1, text: "3" },
      { row: 2, col: 2, text: "4" },
    ]);
    expect([placed.rowsPast, placed.colsPast]).toEqual([0, 0]);
  });

  it("fills a selection with one value, or with a block it's a multiple of", () => {
    const one = placePaste([["95"]], rangeOf({ row: 0, col: 2 }, { row: 2, col: 2 }), size);
    expect(one.writes.map((w) => [w.row, w.text])).toEqual([
      [0, "95"],
      [1, "95"],
      [2, "95"],
    ]);
    const tiled = placePaste([["a"], ["b"]], rangeOf({ row: 0, col: 0 }, { row: 3, col: 0 }), size);
    expect(tiled.writes.map((w) => w.text)).toEqual(["a", "b", "a", "b"]);
  });

  it("says how far a block runs past the sheet", () => {
    const placed = placePaste(
      [
        ["1", "2", "3"],
        ["4", "5", "6"],
      ],
      rangeOf({ row: 4, col: 2 }, { row: 4, col: 2 }),
      size,
    );
    expect([placed.rowsPast, placed.colsPast]).toEqual([1, 1]);
    expect(placed.writes).toEqual([
      { row: 4, col: 2, text: "1" },
      { row: 4, col: 3, text: "2" },
    ]);
  });
});

describe("filling down", () => {
  const text = (row: number, col: number) => `r${String(row)}c${String(col)}`;

  it("copies the selection's top row into the rows below it", () => {
    expect(fillDown(rangeOf({ row: 1, col: 2 }, { row: 3, col: 2 }), text)).toEqual([
      { row: 2, col: 2, text: "r1c2" },
      { row: 3, col: 2, text: "r1c2" },
    ]);
  });

  it("copies the row above into a single row", () => {
    expect(fillDown(rangeOf({ row: 2, col: 1 }, { row: 2, col: 1 }), text)).toEqual([
      { row: 2, col: 1, text: "r1c1" },
    ]);
    expect(fillDown(rangeOf({ row: 0, col: 1 }, { row: 0, col: 1 }), text)).toEqual([]);
  });
});

describe("moving the active cell", () => {
  const sheet = { ctrl: false, rows: 10, cols: 5, page: 4 };

  it("moves by arrows, Home, End and pages, and stops at the edges", () => {
    expect(moved({ row: 0, col: 0 }, "ArrowUp", sheet)).toEqual({ row: 0, col: 0 });
    expect(moved({ row: 3, col: 2 }, "ArrowRight", sheet)).toEqual({ row: 3, col: 3 });
    expect(moved({ row: 3, col: 2 }, "End", sheet)).toEqual({ row: 3, col: 4 });
    expect(moved({ row: 3, col: 2 }, "Home", { ...sheet, ctrl: true })).toEqual({ row: 0, col: 0 });
    expect(moved({ row: 8, col: 2 }, "PageDown", sheet)).toEqual({ row: 9, col: 2 });
    expect(moved({ row: 3, col: 2 }, "a", sheet)).toBeNull();
  });
});

describe("changes, and undoing them", () => {
  const kd1 = sample("SMP-000001", { purity: 90 });
  const kd2 = sample("SMP-000002", { purity: 91 });
  const rows: SheetRow[] = [
    { key: kd1.id, entity: kd1 },
    { key: kd2.id, entity: kd2 },
  ];

  it("keeps what's typed, and forgets a cell that's put back", () => {
    const one = written(EMPTY, [{ row: 0, col: col("purity"), text: "98.5" }], rows, columns);
    expect(textAt(one, nth(rows, 0), column("purity"))).toBe("98.5");
    const back = written(one, [{ row: 0, col: col("purity"), text: "90" }], rows, columns);
    expect(back.edits).toEqual({});
  });

  it("undoes and redoes each step", () => {
    let history: History = { past: [], present: EMPTY, future: [] };
    history = did(
      history,
      written(history.present, [{ row: 0, col: col("purity"), text: "98" }], rows, columns),
    );
    history = did(
      history,
      written(history.present, [{ row: 1, col: col("purity"), text: "97" }], rows, columns),
    );
    history = undo(history);
    expect(Object.keys(history.present.edits)).toEqual([kd1.id]);
    history = redo(history);
    expect(Object.keys(history.present.edits)).toEqual([kd1.id, kd2.id]);
    // A step that changes nothing isn't one.
    expect(did(history, { ...history.present })).toBe(history);
    // A new step after an undo can't be redone past.
    history = did(undo(history), EMPTY);
    expect(redo(history)).toBe(history);
  });

  it("adds new rows and takes them away", () => {
    const two = withNewRows(EMPTY, 2);
    expect(two.added).toEqual(["new-1", "new-2"]);
    const between = withNewRows(two, 1, 1);
    expect(between.added).toEqual(["new-1", "new-3", "new-2"]);
    expect(withoutNewRows(between, new Set(["new-3"])).added).toEqual(["new-1", "new-2"]);
  });
});

describe("the records a sheet has, as the table reads them again", () => {
  const kd1 = sample("SMP-000001", { purity: 90 });
  const kd2 = sample("SMP-000002", { purity: 91 });
  const kd3 = sample("SMP-000003", { purity: 92 });

  it("keeps each where it was, as it is now, with records it hadn't at the end", () => {
    const first = reloaded(NONE_LOADED, [kd1, kd2]);
    expect(first.order).toEqual([kd1.id, kd2.id]);
    // Someone saved SMP-000002: sorted by when they changed, it's first in the table now.
    const kd2Now = { ...kd2, revision: kd2.revision + 1, values: { purity: 95 } };
    const next = reloaded(first, [kd3, kd2Now, kd1]);
    expect(next.order).toEqual([kd1.id, kd2.id, kd3.id]);
    expect(next.byId[kd2.id]).toBe(kd2Now);
    // A record that's gone from the table stays.
    expect(reloaded(next, [kd1]).order).toEqual([kd1.id, kd2.id, kd3.id]);
    expect(reloaded(next, [kd1]).byId[kd2.id]).toBe(kd2Now);
  });

  it("is the same when nothing's new, so reading again redraws nothing", () => {
    const first = reloaded(NONE_LOADED, [kd1, kd2]);
    expect(reloaded(first, [kd2, kd1])).toBe(first);
    expect(reloaded(first, [])).toBe(first);
  });
});

describe("what's checked and saved", () => {
  const kd1 = sample("SMP-000001", { purity: 90 });

  it("sends a record's changed cells, at the revision read; a new row's place and amount", () => {
    const rows: SheetRow[] = [
      { key: "new-1", number: 1 },
      { key: kd1.id, entity: kd1 },
    ];
    const state = written(
      withNewRows(EMPTY, 1),
      [
        { row: 0, col: col("label"), text: "EGFR-KD-002" },
        { row: 0, col: col("purity"), text: "" },
        { row: 0, col: col(LOCATION), text: "BOX-000002 A1" },
        { row: 0, col: col(AMOUNT), text: "500" },
        { row: 1, col: col("purity"), text: "98,5" },
        { row: 1, col: col("label"), text: "SMP-000001" },
      ],
      rows,
      columns,
    );
    expect(checkRow(state, nth(rows, 0), columns)).toEqual({
      key: "new-1",
      cells: { label: "EGFR-KD-002" },
      location: "BOX-000002 A1",
      amount: "500",
    });
    expect(checkRow(state, nth(rows, 1), columns)).toEqual({
      key: kd1.id,
      entity: kd1.id,
      revision: 3,
      cells: { purity: "98,5" },
    });
    expect(checkRow(EMPTY, nth(rows, 1), columns)).toBeNull();
  });

  it("saves what the check read, and the exact place it found", () => {
    const sent = { key: "new-1", cells: { label: "EGFR-KD-002" }, location: "BOX-000002" };
    const checked: Schemas["SheetEditRowOut"] = {
      key: "new-1",
      kind: "created",
      entity: null,
      hrid: null,
      revision: null,
      values: { label: "EGFR-KD-002" },
      shown: { label: "EGFR-KD-002" },
      location: {
        container: "00000000-0000-7000-8000-0000000000b0",
        hrid: "BOX-000002",
        title: "Batch 12 retains",
        position: "A2",
        shown: "BOX-000002 A2",
      },
      amount: null,
      amount_shown: null,
      problems: [{ at: "values/label", detail: "Label is taken.", kind: "invalid" }],
      held: [],
    };
    expect(saveRow(sent, checked)).toEqual({
      key: "new-1",
      entity: null,
      revision: null,
      values: { label: "EGFR-KD-002" },
      location: { container: "00000000-0000-7000-8000-0000000000b0", position: "A2" },
      amount: null,
    });
    expect(problemsAt(checked, column("label")).map((p) => p.detail)).toEqual(["Label is taken."]);
    expect(problemsAt(checked, column(ID))).toEqual([]);
    expect(saveRow(sent, { ...checked, kind: "unchanged" })).toBeNull();
  });
});
