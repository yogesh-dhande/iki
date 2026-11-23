/**
 * A container's page (ADR 0180) against a fake API: a scanner's code finds a tube on its
 * map wherever focus is, several are selected and moved together with a reason after a
 * preview, and a record's page says where it is.
 */

import type { Schemas } from "@ikisci/sdk";
import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { entity, PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import { answer, calls, ME, page } from "./test-server";

const BOX_ID = "00000000-0000-7000-8000-0000000b0001";
const SHAPE = { rows: 9, columns: 9, labels: "letter_number", order: "by_row", size: 81 } as const;

const TUBE_TYPE: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  name: "acme/lims.Tube",
  plugin: "acme/lims",
  label: "Tube",
  label_plural: "Tubes",
  hrid_prefix: "TUBE",
  identity: [],
  fields: [],
  storage: { holds: [], grid: null, barcode: "barcode", label: null, confirm: ["draw"] },
};

function occupant(n: number, row: number, column: number): Schemas["OccupantOut"] {
  const id = `00000000-0000-7000-8000-0000000a00${String(n).padStart(2, "0")}`;
  return {
    id,
    ref: {
      hrid: `TUBE-0000${String(n).padStart(2, "0")}`,
      title: `Lysate ${String(n)}`,
      type: "acme/lims.Tube",
      archived: false,
      purged: false,
    },
    row,
    column,
    position: `${"ABCDEFGHI"[row - 1] ?? ""}${String(column)}`,
    code: `LB-00${String(n)}`,
    custodian: null,
    custody_since: null,
    container: false,
    count: 0,
  };
}

const FREEZER: Schemas["StepOut"] = {
  id: "00000000-0000-7000-8000-0000000f0001",
  hrid: "FRZ-000001",
  title: "Freezer 3",
  type: "acme/lims.Freezer",
  archived: false,
  purged: false,
  position: null,
};

function container(): Schemas["ContainerOut"] {
  return {
    id: BOX_ID,
    hrid: "BOX-000014",
    title: "Kinase retains",
    type: "acme/lims.Box",
    archived: false,
    purged: false,
    project: "00000000-0000-7000-8000-0000000000p1",
    shape: SHAPE,
    holds: ["acme/lims.Tube"],
    code: "BOX-000014",
    location: {
      path: [FREEZER],
      hidden: false,
      stored: true,
      position: null,
      custodian: null,
      custody_since: null,
    },
    contents: [occupant(11, 1, 1), occupant(12, 1, 2), occupant(23, 2, 3)],
    free: 78,
    reconciliation: null,
    can: { keep: true, label: true },
  };
}

function onBox() {
  withTypes();
  answer("GET /api/v1/entity-types", [PROTEIN, TUBE_TYPE]);
  answer("GET /api/v1/storage/containers/BOX-000014", container());
  answer(`GET /api/v1/storage/containers/${BOX_ID}`, container());
}

/** Types `code` as a keyboard-mode scanner does, at whatever has focus. */
function wedge(code: string) {
  let t = performance.now();
  const target = document.activeElement ?? document.body;
  for (const key of [...Array.from(code), "Enter"]) {
    const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
    Object.defineProperty(event, "timeStamp", { value: (t += 4) });
    target.dispatchEvent(event);
  }
}

describe("a container's page", () => {
  it("shows its path and map, and a scan finds a tube on it wherever focus is", async () => {
    onBox();
    await renderApp("/storage/BOX-000014/");
    expect(screen.getByRole("heading", { level: 1, name: "Kinase retains" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Freezer 3" })).toBeInTheDocument();
    const grid = screen.getByRole("grid", { name: "Kinase retains’s positions" });
    expect(within(grid).getAllByRole("gridcell")).toHaveLength(81);
    expect(screen.getByText("78 free: the next is A3.")).toBeInTheDocument();
    // Focus on a button, not the scan box: the scan still lands.
    screen.getByRole("button", { name: "Show free positions" }).focus();
    act(() => {
      wedge("LB-0023");
    });
    const b3 = within(grid).getByRole("gridcell", { name: /^B3, TUBE-000023/ });
    await waitFor(() => {
      expect(document.activeElement).toBe(b3);
    });
    expect(screen.getByText("TUBE-000023 is at B3.")).toHaveAttribute("role", "status");
  });

  it("moves several selected, after saying where each goes, with one reason", async () => {
    onBox();
    const { user } = await renderApp("/storage/BOX-000014/");
    await user.click(screen.getByRole("button", { name: "Select several" }));
    const grid = screen.getByRole("grid");
    await user.click(within(grid).getByRole("gridcell", { name: /^A1,/ }));
    await user.click(within(grid).getByRole("gridcell", { name: /^A2,/ }));
    const bar = screen.getByRole("region", { name: "Selected" });
    expect(within(bar).getByText("2")).toBeInTheDocument();
    answer("POST /api/v1/storage/moves", (call: { body: unknown }) => {
      const body = call.body as { preview: boolean };
      return {
        done: !body.preview,
        operation: body.preview ? null : "00000000-0000-7000-8000-000000000099",
        items: [11, 12].map((n, i) => ({
          record: {
            id: occupant(n, 1, 1).id ?? "",
            hrid: `TUBE-0000${String(n)}`,
            title: "",
            type: "acme/lims.Tube",
            archived: false,
            purged: false,
            position: null,
          },
          from_place: { container: BOX_ID, ref: null, position: `A${String(i + 1)}` },
          to: {
            id: "00000000-0000-7000-8000-0000000b0002",
            hrid: "BOX-000015",
            title: "",
            type: "acme/lims.Box",
            archived: false,
            purged: false,
            position: `C${String(i + 1)}`,
          },
          kind: "moved",
          problems: [],
        })),
      };
    });
    // The box they go to: C1 free, C2 taken, so the second goes in C3.
    answer("GET /api/v1/storage/containers/BOX-000015", {
      ...container(),
      id: "00000000-0000-7000-8000-0000000b0002",
      hrid: "BOX-000015",
      title: "Spare box",
      contents: [{ ...occupant(40, 3, 2), position: "C2" }].map((o) => ({
        ...o,
        row: 3,
        column: 2,
      })),
      location: { ...container().location, path: [] },
    });
    await user.click(within(bar).getByRole("button", { name: "Move…" }));
    const dialog = await screen.findByRole("dialog", { name: "Move 2 records" });
    const to = within(dialog).getByRole("textbox", { name: "Container" });
    await user.clear(to);
    await user.type(to, "BOX-000015");
    await user.type(within(dialog).getByRole("textbox", { name: /^Starting at/ }), "C1");
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "Consolidating");
    await user.click(within(dialog).getByRole("button", { name: "Check where they go" }));
    const plan = await within(dialog).findByRole("table", { name: "What moving them does" });
    expect(within(plan).getByText("BOX-000015, C2")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Move all 2" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    const sent = calls("POST", "/api/v1/storage/moves").map((c) => c.body);
    expect(sent).toHaveLength(2);
    expect(sent[0]).toMatchObject({ preview: true });
    expect(sent[1]).toEqual({
      items: [
        { record: occupant(11, 1, 1).id, to: "BOX-000015", position: "C1" },
        { record: occupant(12, 1, 2).id, to: "BOX-000015", position: "C3" },
      ],
      reason: "Consolidating",
      confirmations: {},
      preview: false,
    });
  });
});

describe("a record's page", () => {
  it("says where it is, with its box's map and its own position marked", async () => {
    withTypes();
    const tube = entity({
      id: occupant(23, 2, 3).id ?? "",
      hrid: "TUBE-000023",
      title: "Lysate 23",
      type: "acme/lims.Tube",
      values: {},
    });
    answer("GET /api/v1/entity-types", [PROTEIN, TUBE_TYPE]);
    answer("GET /api/v1/entities/TUBE-000023", tube);
    answer("GET /api/v1/entities/TUBE-000023/provenance", {});
    answer("GET /api/v1/entities/TUBE-000023/projects", []);
    answer(`GET /api/v1/entities/${tube.id}/location`, {
      path: [FREEZER, { ...FREEZER, id: BOX_ID, hrid: "BOX-000014", title: "Kinase retains" }],
      hidden: false,
      stored: true,
      position: "B3",
      custodian: null,
      custody_since: null,
    });
    answer(`GET /api/v1/entities/${tube.id}/location/events`, page([]));
    answer("GET /api/v1/storage/containers/BOX-000014", container());
    await renderApp("/entities/TUBE-000023/");
    const card = await screen.findByRole("region", { name: "Where it is" });
    const path = within(card).getByRole("navigation", { name: "Where TUBE-000023 is" });
    expect(within(path).getByRole("link", { name: "Kinase retains" })).toBeInTheDocument();
    const here = await within(card).findByRole("gridcell", { name: /^B3,/ });
    expect(here).toHaveAttribute("aria-current", "location");
    expect(within(card).getByRole("button", { name: "Check out" })).toBeInTheDocument();
  });
});

describe("receiving without a storage location", () => {
  it("lists records awaiting put-away with their current custodian", async () => {
    withTypes();
    answer("GET /api/v1/storage/places", []);
    answer("GET /api/v1/storage/checked-out", []);
    const tube = occupant(23, 2, 3);
    answer("GET /api/v1/storage/awaiting-put-away", [
      { ...tube.ref, id: tube.id, custodian: ME, custody_since: "2026-03-14T09:00:00Z" },
    ]);

    await renderApp("/storage/");
    const waiting = await screen.findByRole("region", { name: "Awaiting put-away" });
    expect(await within(waiting).findByRole("link", { name: "TUBE-000023" })).toHaveAttribute(
      "href",
      "/entities/TUBE-000023/",
    );
    expect(within(waiting).getByText(ME.name)).toBeVisible();
  });

  it("distinguishes an unplaced receipt line from one with a hidden destination", async () => {
    withTypes();
    const ref = "REQ-000001";
    answer(`GET /api/v1/entities/${ref}`, entity({ hrid: ref }));
    answer(`GET /api/v1/entities/${ref}/receipts`, {
      expected: 0,
      can_receive: true,
      receipts: [
        {
          id: "receipt-1",
          status: "completed",
          received_by: ME,
          handed_over_by: "Courier",
          completed_at: "2026-03-14T09:00:00Z",
          outcomes: { matched: 2 },
          lines: [
            {
              id: "line-1",
              code: "LB-0023",
              outcome: "matched",
              put_away: null,
              received_unplaced: true,
            },
            {
              id: "line-2",
              code: "LB-0036",
              outcome: "matched",
              put_away: null,
              received_unplaced: false,
            },
          ],
        },
      ],
    });

    const { user } = await renderApp(`/storage/receive/${ref}/`);
    const received = screen.getByRole("region", { name: "Received" });
    await user.click(within(received).getByText(/from Courier/));
    const unplaced = within(received).getByRole("row", { name: /LB-0023/ });
    const hidden = within(received).getByRole("row", { name: /LB-0036/ });
    expect(within(unplaced).getByText("Received without a storage location")).toBeVisible();
    expect(within(hidden).getByText("—")).toBeVisible();
  });
});
