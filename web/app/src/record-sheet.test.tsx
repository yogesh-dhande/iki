import type { Schemas } from "@ikisci/sdk";
import { QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { queryClient } from "./api";
import { entity, field, KINASE, nth } from "./entity-fixtures";
import { RecordSheet } from "./pages/record-sheet";
import { answer, calls, problem } from "./test-server";

// jsdom lays nothing out; the sheet draws the rows that fit in its scrolling region.
const sizes = { clientHeight: 600, clientWidth: 1000, offsetHeight: 600, offsetWidth: 1000 };
const saved: Record<string, PropertyDescriptor | undefined> = {};
beforeAll(() => {
  for (const [name, value] of Object.entries(sizes)) {
    saved[name] = Object.getOwnPropertyDescriptor(HTMLElement.prototype, name);
    Object.defineProperty(HTMLElement.prototype, name, { configurable: true, get: () => value });
  }
  Element.prototype.scrollIntoView = () => undefined;
});
afterAll(() => {
  for (const [name, descriptor] of Object.entries(saved)) {
    if (descriptor) Object.defineProperty(HTMLElement.prototype, name, descriptor);
  }
});

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
  ancestors: ["iki/core.Entity"],
  subtypes: [],
  hrid_prefix: "SMP",
  identity: [],
  viewers: [],
  notifications: [],
  values_schema: {},
  fields: [
    field({ name: "label", label: "Label", type: "string", required: true }),
    field({ name: "purity", label: "Purity", type: "float" }),
    field({
      name: "status",
      label: "Status",
      type: "state",
      states: [
        { name: "received", label: "Received", locks: false },
        { name: "reviewed", label: "Reviewed", locks: true },
      ].map((s) => ({ ...s, holds_amount: false, reason: null, set_by: [], signatures: [] })),
    }),
  ],
};

function sample(n: number, values: Record<string, unknown>, extra = {}) {
  return entity({
    id: `00000000-0000-7000-8000-${String(n).padStart(12, "0")}`,
    hrid: `SMP-00000${String(n)}`,
    type: SAMPLE.name,
    revision: 4,
    values,
    ...extra,
  });
}

const PATH = `/api/v1/projects/${KINASE.id}/sheet-edits/check`;
/** Times a cell gives without a zone are in the browser's. */
const ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;

function checked(row: Schemas["SheetEditCheckRowIn"], over: Partial<Schemas["SheetEditRowOut"]>) {
  return {
    key: row.key,
    kind: "edited",
    entity: row.entity ?? null,
    hrid: null,
    revision: row.revision ?? null,
    values: { purity: 98.5 },
    shown: { purity: "98.5" },
    location: null,
    amount: null,
    amount_shown: null,
    problems: [],
    held: [],
    ...over,
  };
}

/**
 * The sheet, as a page of an app with somewhere else to go, over `records` as the table
 * loaded them. `refetched` hands it the table's records as a refetch would: the same
 * records, reordered or changed, some gone, some new.
 */
async function mount(type: Schemas["EntityTypeOut"], records: Schemas["EntityOut"][]) {
  const onClose = vi.fn();
  const user = userEvent.setup();
  let refetch: (next: Schemas["EntityOut"][]) => void = () => undefined;
  function Sheet() {
    const [loaded, setLoaded] = useState(records);
    refetch = setLoaded;
    return (
      <RecordSheet
        type={type}
        project={KINASE}
        records={loaded}
        more={{ hasNextPage: false, isFetchingNextPage: false, fetchNextPage: () => undefined }}
        onClose={onClose}
      />
    );
  }
  const root = createRootRoute({ component: () => <Outlet /> });
  const router = createRouter({
    routeTree: root.addChildren([
      createRoute({ getParentRoute: () => root, path: "/", component: Sheet }),
      createRoute({
        getParentRoute: () => root,
        path: "elsewhere",
        component: () => <h1>Elsewhere</h1>,
      }),
    ]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  const grid = await screen.findByRole("grid", { name: /^Editing .+ as a sheet$/ });
  const refetched = (next: Schemas["EntityOut"][]) => {
    act(() => {
      refetch(next);
    });
  };
  return { user, onClose, grid, router, refetched };
}

function open(records: Schemas["EntityOut"][]) {
  return mount(SAMPLE, records);
}

/** The text of each of a row's cells, by the record's ID. */
function rowOf(grid: HTMLElement, hrid: string): string[] {
  const row = within(grid)
    .getAllByRole("row")
    .find((r) => within(r).queryAllByRole("gridcell")[0]?.textContent === hrid);
  if (!row) throw new Error(`No row for ${hrid}`);
  return within(row)
    .getAllByRole("gridcell")
    .map((c) => c.textContent);
}

/** The IDs of the sheet's rows, top to bottom. */
function ids(grid: HTMLElement): string[] {
  return within(grid)
    .getAllByRole("row")
    .flatMap((r) => {
      const first = within(r).queryAllByRole("gridcell")[0];
      return first ? [first.textContent] : [];
    });
}

describe("a sheet of records", () => {
  it("is a grid that moves by keys, edits by typing, and says why a cell can't change", async () => {
    const kd1 = sample(1, { label: "ABL1-KD-001", purity: 90, status: "received" });
    const kd2 = sample(
      2,
      { label: "ABL1-KD-002", status: "reviewed" },
      {
        locked: true,
        locked_by: ["status"],
      },
    );
    answer(`POST ${PATH}`, (request: { body: { rows: Schemas["SheetEditCheckRowIn"][] } }) => ({
      rows: request.body.rows.map((r) =>
        checked(r, {
          problems: [
            { at: "values/purity", detail: "Purity must be at most 100.", kind: "invalid" },
          ],
          kind: "failed",
        }),
      ),
    }));
    const { user, grid } = await open([kd1, kd2]);
    expect(
      within(grid)
        .getAllByRole("columnheader")
        .map((h) => h.textContent),
    ).toEqual(["ID", "Label", "Purity", "Status"]);
    const cells = within(grid).getAllByRole("gridcell");
    // One tab stop: the active cell, the first record's label.
    expect(cells.filter((c) => c.tabIndex === 0)).toHaveLength(1);
    expect(cells[1]).toHaveAttribute("tabindex", "0");
    // The state reads as the table shows it, and can't change.
    expect(cells[3]).toHaveTextContent("Received");
    expect(cells[3]).toHaveAttribute("aria-readonly", "true");
    // A locked record's cells say why.
    expect(cells[6]).toHaveAttribute("title", "SMP-000002 is locked by Status.");

    await user.click(nth(cells, 1));
    await user.keyboard("{ArrowRight}");
    expect(within(grid).getAllByRole("gridcell")[2]).toHaveFocus();
    await user.keyboard("140{Enter}");
    expect(within(grid).getAllByRole("gridcell")[2]).toHaveTextContent("140");
    // Checked once typing settles: its problem at its cell, and in the list.
    await waitFor(() => {
      expect(calls("POST", PATH)).toHaveLength(1);
    });
    expect(calls("POST", PATH)[0]?.body).toEqual({
      type: SAMPLE.name,
      locale: { decimal: null, dates: null, serial_dates: false, time_zone: ZONE },
      rows: [{ key: kd1.id, entity: kd1.id, revision: 4, cells: { purity: "140" } }],
    });
    const problems = await screen.findByRole("region", { name: "1 problem to put right" });
    expect(problems).toHaveTextContent("SMP-000001, Purity: Purity must be at most 100.");
    expect(within(grid).getAllByRole("gridcell")[2]).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("button", { name: "Review and save" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    // Enter moved down, to the locked record's purity: typing changes nothing, and says why.
    expect(within(grid).getAllByRole("gridcell")[6]).toHaveFocus();
    await user.keyboard("5");
    expect(screen.getByText(/SMP-000002, Purity: SMP-000002 is locked by Status\./)).toBeVisible();

    // Undone, the change goes, and so does its problem.
    await user.keyboard("{Control>}z{/Control}");
    expect(within(grid).getAllByRole("gridcell")[2]).toHaveTextContent("90");
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: /problem/ })).not.toBeInTheDocument();
    });
  });

  it("reviews what's checked, and saves it with a reason", async () => {
    const kd1 = sample(1, { label: "ABL1-KD-001", purity: 90 });
    answer(`POST ${PATH}`, (request: { body: { rows: Schemas["SheetEditCheckRowIn"][] } }) => ({
      rows: request.body.rows.map((r) => checked(r, {})),
    }));
    answer(`POST /api/v1/projects/${KINASE.id}/sheet-edits`, { rows: [], edited: 1, created: 0 });
    const { user, grid, onClose } = await open([kd1]);
    await user.click(nth(within(grid).getAllByRole("gridcell"), 2));
    await user.keyboard("98.5{Enter}");
    const save = screen.getByRole("button", { name: "Review and save" });
    await waitFor(() => {
      expect(save).not.toHaveAttribute("aria-disabled");
    });
    await user.click(save);
    const dialog = screen.getByRole("alertdialog", { name: "Save 1 row?" });
    expect(within(dialog).getByRole("row", { name: "SMP-000001 Purity 90 98.5" })).toBeVisible();
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "Purity by SEC");
    await user.click(within(dialog).getByRole("button", { name: "Save 1 row" }));
    await waitFor(() => {
      expect(onClose).toHaveBeenCalled();
    });
    expect(calls("POST", `/api/v1/projects/${KINASE.id}/sheet-edits`)[0]?.body).toEqual({
      type: SAMPLE.name,
      reason: "Purity by SEC",
      hold_reason: null,
      locale: { decimal: null, dates: null, serial_dates: false, time_zone: ZONE },
      rows: [
        {
          key: kd1.id,
          entity: kd1.id,
          revision: 4,
          values: { purity: 98.5 },
          location: null,
          amount: null,
        },
      ],
    });
  });

  it("says which records changed since they were read, and saves nothing", async () => {
    const kd1 = sample(1, { label: "ABL1-KD-001", purity: 90 });
    answer(`POST ${PATH}`, (request: { body: { rows: Schemas["SheetEditCheckRowIn"][] } }) => ({
      rows: request.body.rows.map((r) => checked(r, {})),
    }));
    answer(
      `POST /api/v1/projects/${KINASE.id}/sheet-edits`,
      problem(412, "stale", "SMP-000001 changed since you read it: nothing was saved.", undefined, {
        rows: [{ key: kd1.id, entity: kd1.id, hrid: "SMP-000001", revision: 5 }],
      }),
    );
    const { user, grid } = await open([kd1]);
    await user.click(nth(within(grid).getAllByRole("gridcell"), 2));
    await user.keyboard("98.5{Enter}");
    const save = screen.getByRole("button", { name: "Review and save" });
    await waitFor(() => {
      expect(save).not.toHaveAttribute("aria-disabled");
    });
    await user.click(save);
    const dialog = screen.getByRole("alertdialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "QC");
    await user.click(within(dialog).getByRole("button", { name: "Save 1 row" }));
    expect(
      await within(dialog).findByText(/SMP-000001 changed since you opened the sheet/),
    ).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Bring in the latest" })).toBeVisible();
  });
});

describe("a sheet over records the table reads again", () => {
  // The table reads its records again as the window gets focus, and sorted by when they
  // changed, someone else's save moves a record to the top. The sheet's rows stay put.
  const kd1 = sample(1, { label: "ABL1-KD-001", purity: 90 });
  const kd2 = sample(2, { label: "ABL1-KD-002", purity: 80 });
  const anyChecked = () => {
    answer(`POST ${PATH}`, (request: { body: { rows: Schemas["SheetEditCheckRowIn"][] } }) => ({
      rows: request.body.rows.map((r) => checked(r, {})),
    }));
  };

  it("keeps its rows where they were, adding records it hadn't loaded at the end", async () => {
    anyChecked();
    const { grid, refetched } = await open([kd1, kd2]);
    const kd2Changed = sample(2, { label: "ABL1-KD-002", purity: 82 }, { revision: 5 });
    const kd3 = sample(3, { label: "ABL1-KD-003", purity: 70 });
    refetched([kd3, kd2Changed, kd1]);
    expect(ids(grid)).toEqual(["SMP-000001", "SMP-000002", "SMP-000003"]);
    // A record no one's typed in shows what it is now.
    expect(rowOf(grid, "SMP-000002")).toEqual(["SMP-000002", "ABL1-KD-002", "82", ""]);
  });

  it("types into the record selected, though the table's order changed", async () => {
    anyChecked();
    const { user, grid, refetched } = await open([kd1, kd2]);
    await user.click(nth(within(grid).getAllByRole("gridcell"), 2));
    refetched([sample(2, { label: "ABL1-KD-002", purity: 80 }, { revision: 5 }), kd1]);
    await user.keyboard("77{Enter}");
    expect(rowOf(grid, "SMP-000001")).toEqual(["SMP-000001", "ABL1-KD-001", "77", ""]);
    expect(rowOf(grid, "SMP-000002")).toEqual(["SMP-000002", "ABL1-KD-002", "80", ""]);
  });

  it("pastes into the records selected, though the table's order changed", async () => {
    anyChecked();
    const { user, grid, refetched } = await open([kd1, kd2]);
    await user.click(nth(within(grid).getAllByRole("gridcell"), 2));
    refetched([sample(2, { label: "ABL1-KD-002", purity: 80 }, { revision: 5 }), kd1]);
    await user.paste("77\n78");
    expect(rowOf(grid, "SMP-000001")[2]).toBe("77");
    expect(rowOf(grid, "SMP-000002")[2]).toBe("78");
  });

  it("keeps a changed record, and its change, when it's no longer in the table", async () => {
    anyChecked();
    const { user, grid, refetched } = await open([kd1, kd2]);
    await user.click(nth(within(grid).getAllByRole("gridcell"), 6));
    await user.keyboard("91{Enter}");
    expect(screen.getByRole("status")).toHaveTextContent("1 row changed");
    // Archived, say, or someone else's change took it out of the table's filter.
    refetched([kd1]);
    expect(screen.getByRole("status")).toHaveTextContent("1 row changed");
    expect(rowOf(grid, "SMP-000002")[2]).toBe("91");
    const save = screen.getByRole("button", { name: "Review and save" });
    await waitFor(() => {
      expect(save).not.toHaveAttribute("aria-disabled");
    });
    await user.click(save);
    expect(
      within(screen.getByRole("alertdialog", { name: "Save 1 row?" })).getByRole("row", {
        name: "SMP-000002 Purity 80 98.5",
      }),
    ).toBeVisible();
  });

  it("stays on the record selected when an added row is undone", async () => {
    anyChecked();
    const { user, grid } = await open([kd1, kd2]);
    await user.click(screen.getByRole("button", { name: "Add row" }));
    // The new row is at the top: the first record is now the second row.
    await user.click(nth(within(grid).getAllByRole("gridcell"), 6));
    await user.keyboard("{Control>}z{/Control}");
    expect(ids(grid)).toEqual(["SMP-000001", "SMP-000002"]);
    await user.keyboard("77{Enter}");
    expect(rowOf(grid, "SMP-000001")[2]).toBe("77");
    expect(rowOf(grid, "SMP-000002")[2]).toBe("80");
  });
});

describe("leaving a sheet with changes unsaved", () => {
  const kd1 = sample(1, { label: "ABL1-KD-001", purity: 90 });

  it("asks first, and stays with the changes kept, or goes and discards them", async () => {
    answer(`POST ${PATH}`, (request: { body: { rows: Schemas["SheetEditCheckRowIn"][] } }) => ({
      rows: request.body.rows.map((r) => checked(r, {})),
    }));
    const { user, grid, router, onClose } = await open([kd1]);
    await user.click(nth(within(grid).getAllByRole("gridcell"), 2));
    await user.keyboard("98.5{Enter}");
    // A link elsewhere in the app, Back, or a notification's link.
    act(() => {
      router.history.push("/elsewhere");
    });
    let dialog = await screen.findByRole("alertdialog", { name: "Discard your changes?" });
    expect(dialog).toHaveTextContent("1 row has changes that aren’t saved.");
    await user.click(within(dialog).getByRole("button", { name: "Keep editing" }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    });
    expect(router.state.location.pathname).toBe("/");
    expect(nth(within(grid).getAllByRole("gridcell"), 2)).toHaveTextContent("98.5");

    act(() => {
      router.history.push("/elsewhere");
    });
    dialog = await screen.findByRole("alertdialog", { name: "Discard your changes?" });
    await user.click(within(dialog).getByRole("button", { name: "Discard changes" }));
    expect(await screen.findByRole("heading", { name: "Elsewhere" })).toBeVisible();
    expect(onClose).toHaveBeenCalled();
  });

  it("goes without asking when nothing's changed", async () => {
    const { router } = await open([kd1]);
    act(() => {
      router.history.push("/elsewhere");
    });
    expect(await screen.findByRole("heading", { name: "Elsewhere" })).toBeVisible();
  });
});

describe("a sheet's lab list cells (ADR 0270)", () => {
  const METHOD = "acme/protein.PurificationMethod";
  const PREP: Schemas["EntityTypeOut"] = {
    ...SAMPLE,
    name: "acme/protein.Prep",
    label: "Prep",
    label_plural: "Preps",
    sample: false,
    fields: [
      field({ name: "name", label: "Name", type: "string", required: true }),
      field({ name: "method", label: "Method", type: "ref", to: [METHOD] }),
      field({ name: "polishing", label: "Polishing", type: "list", items: "ref", to: [METHOD] }),
    ],
  };
  const LIST: Schemas["EntityTypeOut"] = {
    ...SAMPLE,
    name: METHOD,
    label: "Purification method",
    label_plural: "Purification methods",
    sample: false,
    lab_list: true,
    fields: [],
  };
  const option = (n: number, code: string, label: string, archived = false) => ({
    id: `00000000-0000-7000-8000-0000000c000${String(n)}`,
    hrid: `PUM-00000${String(n)}`,
    code,
    label,
    description: "",
    position: n,
    archived,
    superseded_by: null,
    values: {},
    revision: 1,
    uses: null,
  });
  const PROTEIN_A = option(1, "protein_a", "Protein A affinity");
  const SEC = option(2, "sec", "Size exclusion");
  const DIALYSIS = option(3, "dialysis", "Dialysis", true);

  function prep() {
    return entity({
      id: "00000000-0000-7000-8000-00000000b001",
      hrid: "PRP-000001",
      type: PREP.name,
      revision: 2,
      values: { name: "Anti-HER2 prep 12", method: DIALYSIS.id, polishing: [] },
      refs: {
        [DIALYSIS.id]: {
          hrid: DIALYSIS.hrid,
          title: "Dialysis",
          type: METHOD,
          archived: true,
          code: "dialysis",
          superseded: false,
          purged: false,
        },
      },
    });
  }

  function openPreps() {
    answer("GET /api/v1/entity-types", [PREP, LIST]);
    answer(`GET /api/v1/lists/${METHOD}/options`, [PROTEIN_A, SEC]);
    answer(`POST ${PATH}`, (request: { body: { rows: Schemas["SheetEditCheckRowIn"][] } }) => ({
      rows: request.body.rows.map((r) => checked(r, { values: {}, shown: {} })),
    }));
    return mount(PREP, [prep()]);
  }

  it("reads an option by its label, marked when it's archived, and offers the list's options", async () => {
    const { user, grid } = await openPreps();
    const method = () => nth(within(grid).getAllByRole("gridcell"), 2);
    await waitFor(() => {
      expect(method()).toHaveTextContent("Dialysis (archived)");
    });
    // Alt+Down opens its options, as a spreadsheet's dropdown cell does.
    await user.click(method());
    await user.keyboard("{Alt>}{ArrowDown}{/Alt}");
    const box = screen.getByRole("combobox", { name: "Method, PRP-000001" });
    expect(box).toHaveValue("Dialysis");
    const options = await screen.findByRole("listbox", { name: "Purification methods" });
    await waitFor(() => {
      expect(
        within(options)
          .getAllByRole("option")
          .map((o) => o.textContent),
      ).toEqual(["Protein A affinity protein_a", "Size exclusion sec"]);
    });
    // Typed, it narrows them; Enter takes the highlighted one and moves down.
    await user.clear(box);
    await user.keyboard("size");
    expect(within(options).getAllByRole("option")).toHaveLength(1);
    expect(box).toHaveAttribute("aria-activedescendant");
    await user.keyboard("{Enter}");
    expect(method()).toHaveTextContent("Size exclusion");
    await waitFor(() => {
      expect(calls("POST", PATH).at(-1)?.body).toMatchObject({
        rows: [{ cells: { method: "Size exclusion" } }],
      });
    });
  });

  it("takes several options in a list's cell, apart by semicolons", async () => {
    const { user, grid } = await openPreps();
    const polishing = () => nth(within(grid).getAllByRole("gridcell"), 3);
    await user.click(polishing());
    await user.keyboard("{Enter}");
    const box = screen.getByRole("combobox", { name: "Polishing, PRP-000001" });
    await screen.findByRole("option", { name: /Protein A affinity/ });
    await user.click(screen.getByRole("option", { name: /Protein A affinity/ }));
    expect(box).toHaveValue("Protein A affinity; ");
    await user.keyboard("sec{Enter}");
    expect(polishing()).toHaveTextContent("Protein A affinity; Size exclusion");
    // Escape on an unchanged cell puts it back.
    await user.click(polishing());
    await user.keyboard("{Enter}{Escape}{Escape}");
    expect(polishing()).toHaveTextContent("Protein A affinity; Size exclusion");
  });
});
