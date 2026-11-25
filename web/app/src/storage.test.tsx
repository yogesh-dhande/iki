/**
 * A container's page (ADR 0180) against a fake API: a scanner's code finds a tube on its
 * map wherever focus is, several are selected and moved together with a reason after a
 * preview, and a record's page says where it is.
 */

import type { Schemas } from "@ikisci/sdk";
import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { entity, PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import { IKI_ID_REFUSED, SCAN_REFUSED } from "./storage";
import { answer, calls, ME, page, problem, type Sent } from "./test-server";

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

const PROJECT = { id: "00000000-0000-7000-8000-0000000000c1", name: "Kinase screen" };

/** An occupant as a scan's candidate: its project, and its box and position. */
function candidateOf(o: Schemas["OccupantOut"], box = container()): Schemas["ScanCandidateOut"] {
  return {
    id: o.id ?? "",
    hrid: o.ref?.hrid ?? "",
    title: o.ref?.title ?? "",
    type: "acme/lims.Tube",
    owner_project: PROJECT,
    box: { id: box.id, hrid: box.hrid, title: box.title },
    position: o.position,
  };
}

/**
 * The resolver, as the server answers in a box: the readable occupants whose code or HRID
 * the scan is (only HRIDs for an Iki ID), and the chosen one, if one was.
 */
function inBox(found: Schemas["ContainerOut"]) {
  return (call: Sent) => {
    const body = call.body as Schemas["ScanResolveIn"];
    const code = body.code.trim().toUpperCase();
    const hits = found.contents.filter(
      (o) =>
        (o.ref?.hrid.toUpperCase() === code ||
          (body.mode !== "iki_id" && o.code?.toUpperCase() === code)) &&
        (!body.choice || o.id === body.choice),
    );
    return {
      status: hits.length === 1 ? "one" : hits.length ? "ambiguous" : "none",
      candidates: hits.map((o) => candidateOf(o, found)),
    };
  };
}

function onBox(found = container()) {
  withTypes();
  answer("GET /api/v1/entity-types", [PROTEIN, TUBE_TYPE]);
  answer("GET /api/v1/storage/containers/BOX-000014", found);
  answer(`GET /api/v1/storage/containers/${BOX_ID}`, found);
  answer("POST /api/v1/storage/scan/resolve", inBox(found));
}

const resolved = () =>
  calls("POST", "/api/v1/storage/scan/resolve").map((c) => c.body as Schemas["ScanResolveIn"]);

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
    // Identified among what's in this box, by the server.
    expect(resolved()).toEqual([
      { scope: "box", container: BOX_ID, code: "LB-0023", mode: "label" },
    ]);
  });

  it("asks which tube a label two readable tubes share is, with each one’s project and position", async () => {
    const twin = { ...occupant(12, 1, 2), code: "LB-0011" };
    onBox({ ...container(), contents: [occupant(11, 1, 1), twin, occupant(23, 2, 3)] });
    const { user } = await renderApp("/storage/BOX-000014/");
    screen.getByRole("button", { name: "Show free positions" }).focus();
    act(() => {
      wedge("LB-0011");
    });
    const chooser = await screen.findByRole("region", {
      name: "“LB-0011” matches more than one tube",
    });
    const choices = within(chooser).getAllByRole("button", { name: /^TUBE-/ });
    expect(choices.map((c) => c.textContent)).toEqual([
      "TUBE-000011 Lysate 11Kinase screenBOX-000014 Kinase retains, A1",
      "TUBE-000012 Lysate 12Kinase screenBOX-000014 Kinase retains, A2",
    ]);
    // Said, and where the keyboard goes next.
    expect(within(chooser).getByRole("heading")).toHaveFocus();
    await user.click(within(chooser).getByRole("button", { name: /^TUBE-000012/ }));
    expect(await screen.findByText("TUBE-000012 is at A2.")).toBeInTheDocument();
    expect(screen.getByRole("gridcell", { name: /^A2, TUBE-000012/ })).toHaveFocus();
    // The choice goes back with the code read, for the server to check them together.
    expect(resolved().at(-1)).toEqual({
      scope: "box",
      container: BOX_ID,
      code: "LB-0011",
      mode: "label",
      choice: twin.id,
    });
    expect(screen.queryByRole("region", { name: /matches more than one/ })).toBeNull();
  });

  it("says only that a scan can’t identify a tube when one you can’t read is involved", async () => {
    onBox();
    answer("POST /api/v1/storage/scan/resolve", problem(409, "scan_unavailable", SCAN_REFUSED));
    await renderApp("/storage/BOX-000014/");
    screen.getByRole("button", { name: "Show free positions" }).focus();
    act(() => {
      wedge("LB-0011");
    });
    const said = await screen.findByText(SCAN_REFUSED);
    expect(said.closest("[role=alert]")).not.toBeNull();
    // No count, no candidates, nothing selected: the Iki ID is the way on.
    expect(screen.queryByText(/more than one/)).toBeNull();
    expect(screen.queryByRole("button", { name: /^TUBE-/ })).toBeNull();
    expect(screen.getByRole("textbox", { name: "Iki ID" })).toBeInTheDocument();
    expect(calls("GET", "/api/v1/storage/scan")).toHaveLength(0);
  });

  it("says a typed Iki ID none has, or one you can’t read has, in the same words", async () => {
    onBox();
    answer("POST /api/v1/storage/scan/resolve", problem(409, "scan_unavailable", SCAN_REFUSED));
    const { user } = await renderApp("/storage/BOX-000014/");
    await user.click(screen.getByRole("button", { name: "Type its printed Iki ID instead" }));
    await user.type(screen.getByRole("textbox", { name: "Iki ID" }), "TUBE-000099{Enter}");
    const said = await screen.findByText(IKI_ID_REFUSED);
    expect(said.closest("[role=alert]")).not.toBeNull();
    expect(screen.queryByText(SCAN_REFUSED)).toBeNull();
    expect(resolved().at(-1)).toMatchObject({ code: "TUBE-000099", mode: "iki_id" });
    expect(calls("GET", "/api/v1/storage/scan")).toHaveLength(0);
  });

  it("finds a typed Iki ID by Iki IDs alone, never by a vendor barcode", async () => {
    const twin = { ...occupant(12, 1, 2), code: "TUBE-000011" };
    onBox({ ...container(), contents: [occupant(11, 1, 1), twin] });
    const { user } = await renderApp("/storage/BOX-000014/");
    await user.click(screen.getByRole("button", { name: "Type its printed Iki ID instead" }));
    const field = screen.getByRole("textbox", { name: "Iki ID" });
    expect(field).toHaveFocus();
    await user.type(field, "TUBE-000011{Enter}");
    expect(await screen.findByText("TUBE-000011 is at A1.")).toBeInTheDocument();
    expect(resolved().at(-1)).toEqual({
      scope: "box",
      container: BOX_ID,
      code: "TUBE-000011",
      mode: "iki_id",
    });
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
      // Nothing of a refused burst reaches the server: not its suffix, either.
      expect(resolved().map((b) => b.code)).toEqual(accepted ? [code] : []);
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
      expect(resolved().map((b) => b.code)).toEqual([code]);
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
    // The box they go to, as the resolver finds it by what was typed.
    const spare = {
      id: "00000000-0000-7000-8000-0000000b0002",
      hrid: "BOX-000015",
      title: "Spare box",
      type: "acme/lims.Box",
      owner_project: PROJECT,
      box: null,
      position: null,
    };
    answer("POST /api/v1/storage/scan/resolve", { status: "one", candidates: [spare] });
    // C1 free, C2 taken, so the second goes in C3.
    answer(`GET /api/v1/storage/containers/${spare.id}`, {
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
    expect(resolved()).toEqual([{ scope: "destination", code: "BOX-000015" }]);
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

describe("moving tubes whose type asks for each to be confirmed", () => {
  const MOVED: Schemas["EntityTypeOut"] = {
    ...TUBE_TYPE,
    storage: { holds: [], grid: null, barcode: "barcode", label: null, confirm: ["draw", "move"] },
  };
  const first = { ...occupant(11, 1, 1), code: "SHARED-42" };
  const second = { ...occupant(12, 1, 2), code: "SHARED-42" };
  const hex = (id: string | null) => (id ?? "").replaceAll("-", "").toUpperCase();
  const SPARE = {
    id: "00000000-0000-7000-8000-0000000b0002",
    hrid: "BOX-000015",
    title: "Spare box",
    type: "acme/lims.Box",
    owner_project: PROJECT,
    box: null,
    position: null,
  };
  const OTHER = { ...SPARE, id: "00000000-0000-7000-8000-0000000b0003", hrid: "BOX-000016" };

  // A label only the first has, until the test gives the second the same one.
  let solo = [first];

  /** The resolver, for a confirmation and a destination, as the server answers them. */
  function resolver(call: Sent) {
    const body = call.body as Schemas["ScanResolveIn"];
    const code = body.code.trim().toUpperCase();
    if (body.scope === "destination") {
      if (code === "RACK-SHELF") {
        const both = [SPARE, OTHER].filter((b) => !body.choice || b.id === body.choice);
        return { status: both.length === 1 ? "one" : "ambiguous", candidates: both };
      }
      return { status: "one", candidates: [SPARE] };
    }
    // One question for the whole selection, never for one record of it.
    expect(body.record ?? null).toBeNull();
    const among = [first, second].filter((o) => body.records?.includes(o.id ?? ""));
    const found = (them: typeof among) => ({
      status: them.length === 1 ? "one" : them.length ? "ambiguous" : "none",
      candidates: them.map((o) => candidateOf(o)),
    });
    if (body.mode === "iki_id") return found(among.filter((o) => code === o.ref?.hrid));
    if (code === "SHARED-42") return found(among);
    if (code === "SOLO-7") return found(solo);
    return found(among.filter((o) => code === `IKI:${hex(o.id)}`));
  }

  function plan(call: Sent) {
    const body = call.body as { preview: boolean; items: { to_choice?: string }[] };
    const to = body.items[0]?.to_choice === OTHER.id ? OTHER : SPARE;
    return {
      done: !body.preview,
      operation: body.preview ? null : "00000000-0000-7000-8000-000000000099",
      plan: body.preview ? "e5".repeat(32) : null,
      items: [first, second].map((o, i) => ({
        record: {
          id: o.id ?? "",
          hrid: o.ref?.hrid ?? "",
          title: "",
          type: "acme/lims.Tube",
          archived: false,
          purged: false,
          position: null,
        },
        from_place: { container: BOX_ID, ref: null, position: o.position },
        to: { ...to, archived: false, purged: false, position: `A${String(i + 1)}` },
        kind: "moved",
        problems: [],
      })),
    };
  }

  async function openMove() {
    solo = [first];
    onBox({ ...container(), contents: [first, second] });
    answer("GET /api/v1/entity-types", [PROTEIN, MOVED]);
    answer("POST /api/v1/storage/scan/resolve", resolver);
    answer("POST /api/v1/storage/moves", plan);
    const { user } = await renderApp("/storage/BOX-000014/");
    await user.click(screen.getByRole("button", { name: "Select several" }));
    const grid = screen.getByRole("grid");
    await user.click(within(grid).getByRole("gridcell", { name: /^A1,/ }));
    await user.click(within(grid).getByRole("gridcell", { name: /^A2,/ }));
    await user.click(
      within(screen.getByRole("region", { name: "Selected" })).getByRole("button", {
        name: "Move…",
      }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Move 2 records" });
    return { user, dialog };
  }

  it("confirms neither by a label both share, and each by its Iki label or typed Iki ID", async () => {
    const { user, dialog } = await openMove();
    const scan = within(dialog).getByRole("textbox", { name: "Scan each label to confirm" });
    await user.type(scan, "SHARED-42{Enter}");
    const shared = await within(dialog).findByRole("region", {
      name: "“SHARED-42” matches more than one tube",
    });
    // Each is shown, with its project and position, but neither can be picked.
    expect(within(shared).queryByRole("button", { name: /^TUBE-/ })).toBeNull();
    expect(
      within(shared)
        .getAllByRole("listitem")
        .map((l) => l.textContent),
    ).toEqual([
      "TUBE-000011 Lysate 11Kinase screenBOX-000014 Kinase retains, A1",
      "TUBE-000012 Lysate 12Kinase screenBOX-000014 Kinase retains, A2",
    ]);
    const list = within(dialog).getByRole("list", { name: "Confirmed" });
    expect(list).toHaveTextContent("TUBE-000011 not scanned yet");
    expect(list).toHaveTextContent("TUBE-000012 not scanned yet");
    // One question, with the whole selection.
    expect(resolved().filter((b) => b.scope === "confirmation")).toEqual([
      { scope: "confirmation", code: "SHARED-42", mode: "label", records: [first.id, second.id] },
    ]);

    // The Iki ID typed from the second tube: matched only against Iki IDs.
    const typed = within(dialog).getByRole("textbox", { name: "Iki ID" });
    await user.type(typed, "TUBE-000012{Enter}");
    await waitFor(() => {
      expect(list).toHaveTextContent("TUBE-000012 confirmed");
    });
    expect(list).toHaveTextContent("TUBE-000011 not scanned yet");
    expect(resolved().at(-1)).toEqual({
      scope: "confirmation",
      code: "TUBE-000012",
      mode: "iki_id",
      records: [first.id, second.id],
    });

    // The first, by its own Iki label; then the same again ticks nothing more.
    const own = `IKI:${hex(first.id)}`;
    await user.type(scan, `${own}{Enter}`);
    await waitFor(() => {
      expect(list).toHaveTextContent("TUBE-000011 confirmed");
    });
    const before = resolved().length;
    await user.type(scan, `${own}{Enter}`);
    expect(
      await within(dialog).findByText("TUBE-000011 is already confirmed. Nothing changed."),
    ).toBeInTheDocument();
    expect(resolved()).toHaveLength(before);

    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "Consolidating");
    await user.click(within(dialog).getByRole("button", { name: "Check where they go" }));
    await within(dialog).findByRole("table", { name: "What moving them does" });
    await user.click(within(dialog).getByRole("button", { name: "Move all 2" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    const sent = calls("POST", "/api/v1/storage/moves").map((c) => c.body);
    expect(sent.at(-1)).toMatchObject({
      confirmations: {
        [first.id ?? ""]: own,
        [second.id ?? ""]: { code: "TUBE-000012", mode: "iki_id" },
      },
      preview: false,
    });
  });

  it("takes back a tick when the move finds its label now shared, and keeps the selection", async () => {
    const { user, dialog } = await openMove();
    const scan = within(dialog).getByRole("textbox", { name: "Scan each label to confirm" });
    const list = within(dialog).getByRole("list", { name: "Confirmed" });
    await user.type(scan, "SOLO-7{Enter}");
    await waitFor(() => {
      expect(list).toHaveTextContent("TUBE-000011 confirmed");
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Type its printed Iki ID instead" }),
    );
    await user.type(within(dialog).getByRole("textbox", { name: "Iki ID" }), "TUBE-000012{Enter}");
    await waitFor(() => {
      expect(list).toHaveTextContent("TUBE-000012 confirmed");
    });
    await user.click(within(dialog).getByRole("button", { name: "Check where they go" }));
    await within(dialog).findByRole("table", { name: "What moving them does" });
    // Between the preview and the move, the second tube is given the first's label.
    solo = [first, second];
    const refused =
      "Could not confirm TUBE-000011. Scan its record link or enter its printed Iki ID.";
    answer("POST /api/v1/storage/moves", (call: Sent) =>
      (call.body as { preview: boolean }).preview
        ? plan(call)
        : problem(422, "validation_failed", "Some fields are invalid.", [
            { pointer: "#/items/0", detail: refused },
          ]),
    );
    await user.click(within(dialog).getByRole("button", { name: "Move all 2" }));
    const now = await within(dialog).findByRole("region", {
      name: "“SOLO-7” matches more than one tube",
    });
    expect(now).toHaveTextContent(
      "This label now matches more than one tube, so nothing was moved and TUBE-000011 isn’t confirmed.",
    );
    expect(within(now).queryByRole("button", { name: /^TUBE-/ })).toBeNull();
    // Its tick is taken back; the other's stays, and both are still to be moved.
    expect(list).toHaveTextContent("TUBE-000011 not scanned yet");
    expect(list).toHaveTextContent("TUBE-000012 confirmed");
    expect(within(dialog).getByRole("button", { name: "Move all 2" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(
      resolved()
        .filter((b) => b.scope === "confirmation")
        .at(-1),
    ).toEqual({
      scope: "confirmation",
      code: "SOLO-7",
      mode: "label",
      records: [first.id, second.id],
    });
    await user.type(within(dialog).getByRole("textbox", { name: "Iki ID" }), "TUBE-000011{Enter}");
    await waitFor(() => {
      expect(list).toHaveTextContent("TUBE-000011 confirmed");
    });
    answer("POST /api/v1/storage/moves", plan);
    // What it shows is checked again with the scans as they are now.
    await waitFor(() => {
      expect(calls("POST", "/api/v1/storage/moves").at(-1)?.body).toMatchObject({
        confirmations: { [first.id ?? ""]: { code: "TUBE-000011", mode: "iki_id" } },
        preview: true,
      });
    });
    await user.click(within(dialog).getByRole("button", { name: "Move all 2" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(calls("POST", "/api/v1/storage/moves").at(-1)?.body).toMatchObject({
      confirmations: {
        [first.id ?? ""]: { code: "TUBE-000011", mode: "iki_id" },
        [second.id ?? ""]: { code: "TUBE-000012", mode: "iki_id" },
      },
      preview: false,
    });
  });

  it("moves nothing the preview didn't show, and shows where they'd go now", async () => {
    const { user, dialog } = await openMove();
    await user.click(
      within(dialog).getByRole("button", { name: "Type its printed Iki ID instead" }),
    );
    const typed = within(dialog).getByRole("textbox", { name: "Iki ID" });
    const list = within(dialog).getByRole("list", { name: "Confirmed" });
    for (const hrid of ["TUBE-000011", "TUBE-000012"]) {
      await user.type(typed, `${hrid}{Enter}`);
      await waitFor(() => {
        expect(list).toHaveTextContent(`${hrid} confirmed`);
      });
    }
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "Consolidating");
    await user.click(within(dialog).getByRole("button", { name: "Check where they go" }));
    const table = await within(dialog).findByRole("table", { name: "What moving them does" });
    expect(
      within(table)
        .getAllByRole("row")
        .map((r) => r.textContent),
    ).toEqual([
      "RecordFromToProblems",
      "TUBE-000011A place you can’t open, A1BOX-000015, A1None",
      "TUBE-000012A place you can’t open, A2BOX-000015, A2None",
    ]);
    // Meanwhile another bench puts a tube in BOX-000015's A1: the first would now go to A3.
    const shown = (calls("POST", "/api/v1/storage/moves").at(-1)?.body ?? {}) as Sent["body"];
    const now = plan({ body: shown } as Sent);
    const [mine, theirs] = now.items;
    const moved = {
      ...now,
      plan: "f6".repeat(32),
      items: [{ ...mine, to: mine?.to ? { ...mine.to, position: "A3" } : null }, theirs],
    };
    answer("POST /api/v1/storage/moves", (call: Sent) =>
      (call.body as { plan?: string }).plan === moved.plan
        ? plan(call)
        : problem(
            409,
            "moves_plan_changed",
            "Where they go changed since you checked.",
            undefined,
            {
              moves: moved,
            },
          ),
    );
    await user.click(within(dialog).getByRole("button", { name: "Move all 2" }));
    const note = await within(dialog).findByRole("alert");
    expect(note).toHaveTextContent("Where they go changed while you were looking at it.");
    expect(note).toHaveTextContent("TUBE-000011: goes to BOX-000015, A3, not BOX-000015, A1.");
    expect(note).not.toHaveTextContent("TUBE-000012");
    expect(note).toHaveTextContent("If it’s still what you want, move them.");
    expect(within(table).getAllByRole("row")[1]).toHaveTextContent("BOX-000015, A3");
    expect(screen.getByRole("dialog", { name: "Move 2 records" })).toBeInTheDocument();
    // Moving them again makes what's shown now.
    await user.click(within(dialog).getByRole("button", { name: "Move all 2" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    const sent = calls("POST", "/api/v1/storage/moves")
      .map((c) => c.body as { preview: boolean; plan?: string })
      .filter((b) => !b.preview)
      .map((b) => b.plan);
    expect(sent).toEqual(["e5".repeat(32), moved.plan]);
  });

  it("takes back a tick when a check-in finds its label now shared", async () => {
    solo = [first];
    const custodian = {
      id: ME.id,
      name: "Ada",
      email: "ada@example.com",
      active: true,
      kind: "person" as const,
      imported_from: "",
    };
    onBox({
      ...container(),
      contents: [
        { ...first, custodian, custody_since: "2026-11-23T09:00:00Z" },
        { ...second, custodian, custody_since: "2026-11-23T09:00:00Z" },
      ],
    });
    answer("GET /api/v1/entity-types", [PROTEIN, MOVED]);
    answer("POST /api/v1/storage/scan/resolve", resolver);
    answer(
      "POST /api/v1/storage/check-in",
      problem(422, "validation_failed", "Some fields are invalid.", [
        { pointer: "#/items/0", detail: "Could not confirm TUBE-000011." },
      ]),
    );
    const { user } = await renderApp("/storage/BOX-000014/");
    await user.click(screen.getByRole("button", { name: "Select several" }));
    const grid = screen.getByRole("grid");
    await user.click(within(grid).getByRole("gridcell", { name: /^A1,/ }));
    await user.click(within(grid).getByRole("gridcell", { name: /^A2,/ }));
    await user.click(
      within(screen.getByRole("region", { name: "Selected" })).getByRole("button", {
        name: "Check in",
      }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Check in 2 records" });
    const list = within(dialog).getByRole("list", { name: "Confirmed" });
    await user.type(
      within(dialog).getByRole("textbox", { name: "Scan each label to confirm" }),
      "SOLO-7{Enter}",
    );
    await waitFor(() => {
      expect(list).toHaveTextContent("TUBE-000011 confirmed");
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Type its printed Iki ID instead" }),
    );
    await user.type(within(dialog).getByRole("textbox", { name: "Iki ID" }), "TUBE-000012{Enter}");
    await waitFor(() => {
      expect(list).toHaveTextContent("TUBE-000012 confirmed");
    });
    solo = [first, second];
    await user.click(within(dialog).getByRole("button", { name: "Check in" }));
    const now = await within(dialog).findByRole("region", {
      name: "“SOLO-7” matches more than one tube",
    });
    expect(now).toHaveTextContent("This label now matches more than one tube");
    expect(list).toHaveTextContent("TUBE-000011 not scanned yet");
    expect(list).toHaveTextContent("TUBE-000012 confirmed");
    expect(within(dialog).getByRole("button", { name: "Check in" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });

  it("says only that a scan can’t identify a tube when the server refuses it", async () => {
    const { user, dialog } = await openMove();
    answer("POST /api/v1/storage/scan/resolve", () =>
      problem(409, "scan_unavailable", SCAN_REFUSED),
    );
    await user.type(
      within(dialog).getByRole("textbox", { name: "Scan each label to confirm" }),
      "SHARED-42{Enter}",
    );
    expect(await within(dialog).findByText(SCAN_REFUSED)).toBeInTheDocument();
    expect(within(dialog).queryByText(/more than one/)).toBeNull();
    expect(within(dialog).getByRole("list", { name: "Confirmed" })).not.toHaveTextContent(
      "confirmed ",
    );
    // A typed Iki ID refused too is said in words that don't tell a missing one apart.
    await user.type(within(dialog).getByRole("textbox", { name: "Iki ID" }), "TUBE-000099{Enter}");
    expect(await within(dialog).findByText(IKI_ID_REFUSED)).toBeInTheDocument();
    expect(within(dialog).queryByText(SCAN_REFUSED)).toBeNull();
  });

  it("asks which container a destination code two share is, and sends the choice with it", async () => {
    const { user, dialog } = await openMove();
    const to = within(dialog).getByRole("textbox", { name: "Container" });
    await user.clear(to);
    await user.type(to, "RACK-SHELF");
    await user.click(within(dialog).getByRole("button", { name: "Check where they go" }));
    const chooser = await within(dialog).findByRole("region", {
      name: "“RACK-SHELF” matches more than one container",
    });
    expect(calls("POST", "/api/v1/storage/moves")).toHaveLength(0);
    await user.click(within(chooser).getByRole("button", { name: /^BOX-000016/ }));
    const table = await within(dialog).findByRole("table", { name: "What moving them does" });
    expect(table).toHaveTextContent("BOX-000016, A1");
    expect(calls("POST", "/api/v1/storage/moves").at(-1)?.body).toMatchObject({
      items: [
        { record: first.id, to: "RACK-SHELF", to_choice: OTHER.id },
        { record: second.id, to: "RACK-SHELF", to_choice: OTHER.id },
      ],
      preview: true,
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
