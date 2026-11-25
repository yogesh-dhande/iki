import type { Schemas } from "@ikisci/sdk";
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
  detached: false,
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

function open(records: Schemas["EntityOut"][]) {
  const onClose = vi.fn();
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={queryClient}>
      <RecordSheet
        type={SAMPLE}
        project={KINASE}
        records={records}
        more={{ hasNextPage: false, isFetchingNextPage: false, fetchNextPage: () => undefined }}
        onClose={onClose}
      />
    </QueryClientProvider>,
  );
  return { user, onClose, grid: screen.getByRole("grid", { name: "Editing samples as a sheet" }) };
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
    const { user, grid } = open([kd1, kd2]);
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
    const { user, grid, onClose } = open([kd1]);
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
    const { user, grid } = open([kd1]);
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
