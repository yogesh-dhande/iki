/**
 * A container's page (ADR 0180) against a fake API: a scanner's code finds a tube on its
 * map wherever focus is, several are selected and moved together with a reason after a
 * preview, and a record's page says where it is.
 */

import type { Schemas } from "@ikisci/sdk";
import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { entity, PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import { answer, calls, page } from "./test-server";

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

function onBox(found = container()) {
  withTypes();
  answer("GET /api/v1/entity-types", [PROTEIN, TUBE_TYPE]);
  answer("GET /api/v1/storage/containers/BOX-000014", found);
  answer(`GET /api/v1/storage/containers/${BOX_ID}`, found);
}

/** Types `code` as a keyboard-mode scanner does, at whatever has focus. */
function wedge(code: string, gaps: number[] = [], startAt = 1000) {
  let t = startAt;
  const target = document.activeElement ?? document.body;
  const press = (key: string, gap = 4) => {
    const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
    Object.defineProperty(event, "timeStamp", { value: (t += gap) });
    return !target.dispatchEvent(event);
  };
  const keys = Array.from(code);
  keys.forEach((key, i) => press(key, gaps[i - 1]));
  return press("Enter", gaps[keys.length - 1]);
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

  it.each([
    { when: "one 250 ms stall between characters", gaps: [4, 4, 250], accepted: true },
    { when: "one 250 ms stall before Enter", gaps: [4, 4, 4, 4, 4, 4, 250], accepted: true },
    { when: "all remaining gaps are 50 ms", gaps: [250, 50, 50, 50, 50, 50, 50], accepted: true },
    { when: "a 251 ms stall leaves a fast suffix", gaps: [4, 251], accepted: false },
    { when: "Enter arrives after a long pause", gaps: [4, 4, 4, 4, 4, 4, 1000], accepted: false },
    { when: "two gaps exceed 50 ms", gaps: [4, 51, 51], accepted: false },
  ])("protects the focused button with $when", async ({ gaps, accepted }) => {
    onBox();
    await renderApp("/storage/BOX-000014/");
    const button = screen.getByRole("button", { name: "Show free positions" });
    button.focus();
    let enterReachedButton = false;
    button.addEventListener("keydown", (event) => {
      if (event.key === "Enter") enterReachedButton = true;
    });
    act(() => {
      expect(wedge("LB-0023", gaps)).toBe(true);
    });
    expect(enterReachedButton).toBe(false);
    expect(button).toHaveAttribute("aria-pressed", "false");
    if (accepted) {
      expect(await screen.findByText("TUBE-000023 is at B3.")).toBeInTheDocument();
    } else {
      expect(button).toHaveFocus();
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Couldn’t read the scan. Scan again or type the code.",
      );
      expect(screen.queryByText("TUBE-000023 is at B3.")).toBeNull();
    }
  });

  it.each([
    { code: "XRC001", gaps: [251], accepted: false },
    { code: "ABXRC001", gaps: [90, 90, 251], accepted: false },
    { code: "XRC001", gaps: [250], accepted: true },
  ])(
    "keeps the whole code or requests a rescan for $code with gaps $gaps",
    async ({ code, gaps, accepted }) => {
      const target = { ...occupant(23, 2, 3), code };
      onBox({ ...container(), contents: [target] });
      const lookup = "/api/v1/storage/scan";
      answer(`GET ${lookup}`, { code: "RC001", items: [] });
      await renderApp("/storage/BOX-000014/");
      const button = screen.getByRole("button", { name: "Show free positions" });
      button.focus();
      let enters = 0;
      let activations = 0;
      button.addEventListener("click", () => {
        activations++;
      });
      button.addEventListener("keydown", (event) => {
        if (event.key === "Enter") enters++;
      });
      await act(async () => {
        expect(wedge(code, gaps)).toBe(true);
        // Let a wrongly accepted suffix reach the API before checking its requests.
        await Promise.resolve();
      });
      expect(calls("GET", lookup).map((call) => call.query.get("code"))).toEqual([]);
      expect(enters).toBe(0);
      expect(button).toHaveAttribute("aria-pressed", "false");
      if (!accepted) {
        expect(button).toHaveFocus();
        expect(screen.getByRole("alert")).toHaveTextContent(
          "A key arrived before the rest of the scan. Scan again or type the code.",
        );
        expect(screen.queryByText("TUBE-000023 is at B3.")).toBeNull();
        act(() => {
          expect(wedge(code, [], 3000)).toBe(true);
        });
      }
      expect(await screen.findByText("TUBE-000023 is at B3.")).toBeInTheDocument();
      expect(screen.getByRole("gridcell", { name: /^B3, TUBE-000023/ })).toHaveFocus();
      expect(screen.queryByRole("alert")).toBeNull();
      expect(calls("GET", lookup)).toHaveLength(0);
      expect(activations).toBe(0);
      expect(enters).toBe(0);
      expect(button).toHaveAttribute("aria-pressed", "false");
    },
  );

  it("lets a person type at a steady 90 ms per key and use Enter", async () => {
    onBox();
    await renderApp("/storage/BOX-000014/");
    const button = screen.getByRole("button", { name: "Show free positions" });
    button.focus();
    act(() => {
      expect(wedge("LB-0023", Array<number>(7).fill(90))).toBe(false);
    });
    expect(button).toHaveFocus();
    expect(screen.queryByRole("alert")).toBeNull();
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
