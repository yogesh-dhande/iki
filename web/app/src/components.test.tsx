/**
 * Plugins' components (ADR 0084): their tabs on entity pages, the sandboxed frame they
 * run in, and the page's checks on every message a frame sends.
 */

import { MAX_FRAME_HEIGHT, MAX_SELECTED, MIN_FRAME_HEIGHT, PROTOCOL } from "@ikisci/component";
import { readMessage } from "@ikisci/component/host";
import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { START_TIMEOUT_MS } from "./component-frame";
import {
  entityTabs,
  EVERY_RECORD,
  lineageOf,
  pinnedRevision,
  type PluginComponent,
} from "./components";
import { entity, PROTEIN, PROTEIN_ID, renderApp, STRUCTURE, withTypes } from "./entity-fixtures";
import { ACTION } from "./run-fixtures";
import { answer, calls, page, project } from "./test-server";

const RESIDUES: PluginComponent = {
  name: "iki/protein.residues",
  plugin: "iki/protein",
  label: "Residues",
  description: "How many residues a protein has.",
  element: "protein-residues",
  slots: [{ slot: "entity.tab", type: "iki/protein.Protein", label: "Residues", search: null }],
  entities: null,
  selects: "iki/protein.Protein",
  runs: [],
  sha256: "ab".repeat(32),
  frame: `/plugin-components/iki/protein.residues/${"ab".repeat(32)}/`,
};

const CHILD = { ...STRUCTURE, name: "iki/protein.Mutant", extends: "iki/protein.Protein" };

const EVIDENCE: PluginComponent = {
  ...RESIDUES,
  name: "iki/equipment.instrument_uses",
  label: "Instrument evidence",
  slots: [
    {
      slot: "entity.tab",
      type: EVERY_RECORD,
      label: "Instrument evidence",
      search: {
        where: { field: "result", op: "eq", value: "{{ entity }}" },
        sort: null,
        descending: false,
      },
    },
  ],
  entities: { type: "iki/equipment.InstrumentUse", fields: ["result"], limit: 1000 },
};
const EVIDENCE_PATH = "/api/v1/components/iki/equipment.instrument_uses/entities";

/** A protein's page, with a tab on every record whose search finds `found` for it. */
function onEvidence(found: unknown[]) {
  onResidues();
  answer("GET /api/v1/components", page([RESIDUES, EVIDENCE]));
  answer(`POST ${EVIDENCE_PATH}`, page(found));
}

function onResidues() {
  withTypes();
  answer("GET /api/v1/components", page([RESIDUES]));
  answer("GET /api/v1/entities/PRT-000001", entity());
}

/** The frame's side of the channel: what the page sends it, and a way to answer. */
function connect(frame: HTMLIFrameElement) {
  const view = frame.contentWindow;
  if (!view) throw new Error("The frame has no window.");
  const post = vi.spyOn(view, "postMessage");
  fireEvent.load(frame);
  const [message, origin, transfer] = post.mock.calls[0] as unknown as [
    unknown,
    string,
    MessagePort[],
  ];
  const [port] = transfer as [MessagePort];
  const received: unknown[] = [];
  port.onmessage = (event: MessageEvent) => received.push(event.data);
  return { message, origin, port, received };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("tabs", () => {
  it("show on the pages of the type they name and its subtypes", () => {
    const types = [PROTEIN, STRUCTURE, CHILD];
    expect(lineageOf("iki/protein.Mutant", types)).toEqual([
      "iki/protein.Mutant",
      "iki/protein.Protein",
    ]);
    expect(entityTabs("iki/protein.Mutant", types, [RESIDUES])).toEqual([
      { component: RESIDUES, label: "Residues", slot: RESIDUES.slots[0] },
    ]);
    expect(entityTabs("iki/protein.ProteinStructure", types, [RESIDUES])).toEqual([]);
  });

  it("appear beside the entity's own, and open the component in a sandboxed frame", async () => {
    onResidues();
    await renderApp("/entities/PRT-000001/components/iki/protein.residues/");
    const tab = await screen.findByRole("link", { name: "Residues" });
    expect(tab).toHaveAttribute("href", "/entities/PRT-000001/components/iki/protein.residues/");
    expect(tab).toHaveAttribute("aria-current", "page");
    const frame = screen.getByTitle("Residues");
    expect(frame.tagName).toBe("IFRAME");
    expect(frame).toHaveAttribute("sandbox", "allow-scripts");
    expect(frame).toHaveAttribute("src", RESIDUES.frame);
    expect(frame).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(screen.getByText("How many residues a protein has.")).toBeInTheDocument();
    expect(screen.getByText("Starting Residues…")).toHaveAttribute("role", "status");
  });

  it("on every record give way to one for a nearer type", () => {
    const types = [PROTEIN, STRUCTURE, CHILD];
    const evidence: PluginComponent = {
      ...RESIDUES,
      name: "iki/equipment.instrument_uses",
      slots: [
        { slot: "entity.tab", type: "iki/protein.Protein", label: "Own uses", search: null },
        { slot: "entity.tab", type: EVERY_RECORD, label: "Evidence", search: null },
      ],
    };
    expect(entityTabs("iki/protein.Mutant", types, [evidence])).toEqual([
      { component: evidence, label: "Own uses", slot: evidence.slots[0] },
    ]);
    expect(entityTabs("iki/protein.ProteinStructure", types, [evidence])).toEqual([
      { component: evidence, label: "Evidence", slot: evidence.slots[1] },
    ]);
  });

  it("on every record show only where their search finds something for it", async () => {
    onEvidence([]);
    await renderApp("/entities/PRT-000001/");
    expect(await screen.findByRole("link", { name: "Residues" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Instrument evidence" })).toBeNull();
    const [asked] = calls("POST", EVIDENCE_PATH);
    expect(asked?.body).toEqual({ home: false, entity: PROTEIN_ID, limit: 1 });
  });

  it("on every record show where their search finds something for it", async () => {
    onEvidence([{ id: "u", hrid: "EQU-000001" }]);
    await renderApp("/entities/PRT-000001/");
    expect(await screen.findByRole("link", { name: "Instrument evidence" })).toHaveAttribute(
      "href",
      "/entities/PRT-000001/components/iki/equipment.instrument_uses/",
    );
  });

  it("say so when there's no such component for the entity", async () => {
    onResidues();
    answer("GET /api/v1/components", page([]));
    await renderApp("/entities/PRT-000001/components/iki/protein.residues/");
    expect(await screen.findByRole("heading", { name: "Not found" })).toBeInTheDocument();
  });
});

describe("the frame", () => {
  it("gets one port, then the entity once its component is ready", async () => {
    onResidues();
    await renderApp("/entities/PRT-000001/components/iki/protein.residues/");
    const frame = await screen.findByTitle<HTMLIFrameElement>("Residues");
    const { message, origin, port, received } = connect(frame);
    expect(message).toEqual({ type: "iki-connect", protocol: PROTOCOL });
    expect(origin).toBe("*");
    port.postMessage({ type: "ready", protocol: PROTOCOL });
    await vi.waitFor(() => {
      expect(received).toEqual([
        { type: "place", place: { slot: "entity.tab", label: "Residues" } },
        { type: "entity", entity: entity() },
      ]);
    });
    expect(screen.queryByText("Starting Residues…")).not.toBeInTheDocument();
    port.postMessage({ type: "resize", height: 432.4 });
    await vi.waitFor(() => {
      expect(frame.style.height).toBe("432px");
    });
  });

  it("is closed if it opens another page in its frame", async () => {
    onResidues();
    await renderApp("/entities/PRT-000001/components/iki/protein.residues/");
    const frame = await screen.findByTitle<HTMLIFrameElement>("Residues");
    connect(frame);
    fireEvent.load(frame);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Residues tried to open another page, so Iki closed it.",
    );
    expect(screen.queryByTitle("Residues")).not.toBeInTheDocument();
  });

  it("says so if its component doesn't start", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    onResidues();
    await renderApp("/entities/PRT-000001/components/iki/protein.residues/");
    await screen.findByTitle("Residues");
    act(() => {
      vi.advanceTimersByTime(START_TIMEOUT_MS);
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("Residues didn’t start.");
  });
});

describe("a selection", () => {
  async function pick(entities: unknown[]) {
    const { user } = await renderApp("/entities/PRT-000001/components/iki/protein.residues/");
    const frame = await screen.findByTitle<HTMLIFrameElement>("Residues");
    const { port } = connect(frame);
    port.postMessage({ type: "ready", protocol: PROTOCOL });
    port.postMessage({ type: "select", entities });
    return user;
  }

  it("is read back as the person may read it, and offered to the run page", async () => {
    onResidues();
    const other = entity({ id: "00000000-0000-7000-8000-00000000e002", hrid: "PRT-000002" });
    answer("POST /api/v1/entities/search", page([entity(), other]));
    const user = await pick(["PRT-000001", "PRT-000002", "PRT-000009"]);
    expect(await screen.findByText(/^2 proteins selected/)).toHaveTextContent(
      "2 proteins selected. 1 left out: not proteins you can read",
    );
    const [search] = calls("POST", "/api/v1/entities/search");
    expect(search?.body).toMatchObject({
      where: { field: "hrid", op: "in", value: ["PRT-000001", "PRT-000002", "PRT-000009"] },
    });
    expect(screen.getByRole("link", { name: "Run action on them" })).toHaveAttribute(
      "href",
      "/projects/kinase-atlas/runs/new/?entities=PRT-000001%2CPRT-000002",
    );
    await user.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(screen.queryByRole("link", { name: "Run action on them" })).not.toBeInTheDocument();
  });

  it("leaves out entities of other types", async () => {
    onResidues();
    const structure = entity({ hrid: "PST-000001", type: "iki/protein.ProteinStructure" });
    answer("POST /api/v1/entities/search", page([structure]));
    await pick(["PST-000001"]);
    expect(await screen.findByText(/^0 proteins selected/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^Run action on/ })).not.toBeInTheDocument();
  });

  it("of more than a run takes says so, without looking them up", async () => {
    onResidues();
    await pick(Array.from({ length: 201 }, (_, i) => `PRT-${String(i + 1)}`));
    expect(
      await screen.findByText("201 selected: an action runs on at most 200 at once"),
    ).toBeInTheDocument();
    expect(calls("POST", "/api/v1/entities/search")).toEqual([]);
  });
});

describe("an action it offers", () => {
  /** Residues offers compute_properties, which fills it while it's empty (ADR 0170). */
  const OFFERING: PluginComponent = { ...RESIDUES, runs: [ACTION.name] };

  async function open(can: string[]) {
    withTypes();
    answer("GET /api/v1/components", page([OFFERING]));
    answer("GET /api/v1/entities/PRT-000001", entity());
    answer("GET /api/v1/actions", page([ACTION]));
    answer(
      "GET /api/v1/projects",
      page([project({ can: { ...project().can, run: can as never } })]),
    );
    const { router } = await renderApp("/entities/PRT-000001/components/iki/protein.residues/");
    const frame = await screen.findByTitle<HTMLIFrameElement>("Residues");
    const { port, received } = connect(frame);
    port.postMessage({ type: "ready", protocol: PROTOCOL });
    return { router, port, received };
  }

  function places(received: unknown[]) {
    return received.filter((m) => (m as { type: string }).type === "place");
  }

  it("is offered to its element where the person may run it, and opens the run page", async () => {
    const { router, port, received } = await open(["backend"]);
    await vi.waitFor(() => {
      expect(places(received).at(-1)).toEqual({
        type: "place",
        place: { slot: "entity.tab", label: "Residues", runs: [ACTION.name] },
      });
    });
    port.postMessage({ type: "run", action: ACTION.name });
    await vi.waitFor(() => {
      expect(router.state.location.pathname).toBe("/projects/kinase-atlas/runs/new/");
    });
    // Iki's run page, with the action and the entity chosen: the person starts it there.
    expect(router.state.location.search).toEqual({ action: ACTION.name, entities: "PRT-000001" });
  });

  it("isn't, where the person may not run its tier, and asking for it does nothing", async () => {
    const { router, port, received } = await open(["self_hosted"]);
    await vi.waitFor(() => {
      expect(places(received).at(-1)).toEqual({
        type: "place",
        place: { slot: "entity.tab", label: "Residues", runs: [] },
      });
    });
    port.postMessage({ type: "run", action: ACTION.name });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(router.state.location.pathname).toBe(
      "/entities/PRT-000001/components/iki/protein.residues/",
    );
  });

  it("is only one it declared", () => {
    expect(readMessage(OFFERING, { type: "run", action: ACTION.name })).toEqual({
      type: "run",
      action: ACTION.name,
    });
    expect(readMessage(RESIDUES, { type: "run", action: ACTION.name })).toBeNull();
    expect(readMessage(OFFERING, { type: "run", action: "iki/protein.delete_all" })).toBeNull();
    expect(readMessage(OFFERING, { type: "run", action: 7 })).toBeNull();
  });
});

describe("historical record links", () => {
  const record = "00000000-0000-7000-8000-00000000e002";
  const entry = "00000000-0000-7000-8000-00000000e003";
  const at4 = { of: "record", id: record, revision: 4 };
  const ref = (hrid: string, title: string) => ({
    hrid,
    title,
    type: "iki/results.Result",
    archived: false,
    purged: false,
  });

  it("opens the exact stored record revision of a readable reference", () => {
    const visible = { purged: false };
    const pin = { record_id: record, record_revision: 4 };
    const given = { refs: { [record]: visible }, values: { pins: [pin] } };
    expect(pinnedRevision(given, record, 4)).toEqual(at4);
    expect(pinnedRevision(given, record, 5)).toBeNull();
    expect(pinnedRevision(given, entry, 4)).toBeNull();
    expect(pinnedRevision({ ...given, refs: {} }, record, 4)).toBeNull();
    expect(
      pinnedRevision({ ...given, refs: { [record]: { purged: true } } }, record, 4),
    ).toBeNull();
    expect(pinnedRevision(undefined, record, 4)).toBeNull();
    const registryPin = {
      values: { pins: [{ ...pin, entry_id: entry, entry_revision: 7 }] },
      refs: { [entry]: visible },
    };
    expect(pinnedRevision(registryPin, record, 4)).toEqual(at4);
    expect(pinnedRevision(registryPin, entry, 7)).toEqual({ of: "entry", id: entry, revision: 7 });
    expect(pinnedRevision(registryPin, entry, 4)).toBeNull();
  });

  it("opens Iki's page of a pinned revision, in the app, and nothing it doesn't pin", async () => {
    onResidues();
    const pins = [
      { record_id: record, record_revision: 4 },
      { record_id: record, record_revision: 2, entry_id: entry, entry_revision: 7 },
    ];
    answer(
      "GET /api/v1/entities/PRT-000001",
      entity({
        values: { ...entity().values, pins },
        refs: {
          [record]: ref("RES-000001", "Curve"),
          [entry]: ref("RES-000002", "Registered curve"),
        },
      }),
    );
    const assign = vi.fn();
    vi.stubGlobal("location", {
      origin: window.location.origin,
      assign,
      pathname: "/entities/PRT-000001/components/iki/protein.residues/",
      search: "",
    });
    const { router } = await renderApp("/entities/PRT-000001/components/iki/protein.residues/");
    const { port } = connect(await screen.findByTitle<HTMLIFrameElement>("Residues"));
    port.postMessage({ type: "ready" });
    // A revision no pin names, and a pinned record at another revision: neither opens,
    // nor falls back to the record as it is now.
    port.postMessage({ type: "open", entity: record, revision: 5 });
    port.postMessage({ type: "open", entity: entry, revision: 2 });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(router.state.location.pathname).toBe(
      "/entities/PRT-000001/components/iki/protein.residues/",
    );
    port.postMessage({ type: "open", entity: record, revision: 4 });
    await vi.waitFor(() => {
      expect(router.state.location.pathname).toBe(`/entities/${record}/revisions/4/`);
    });
    router.history.back();
    await vi.waitFor(() => {
      expect(router.state.location.pathname).toBe(
        "/entities/PRT-000001/components/iki/protein.residues/",
      );
    });
    const { port: again } = connect(await screen.findByTitle<HTMLIFrameElement>("Residues"));
    again.postMessage({ type: "ready" });
    again.postMessage({ type: "open", entity: entry, revision: 7 });
    await vi.waitFor(() => {
      expect(router.state.location.pathname).toBe(`/entries/${entry}/revisions/7/`);
    });
    // Through the router, never by sending the tab to the API's document.
    expect(assign).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("rejects invalid revision targets without falling back to a current record", () => {
    const message = { type: "open", entity: record, revision: 4 };
    expect(readMessage(RESIDUES, message)).toEqual(message);
    for (const revision of [0, -1, 2.5, Number.MAX_SAFE_INTEGER + 1, "4", null]) {
      expect(readMessage(RESIDUES, { ...message, revision })).toBeNull();
    }
    for (const entity of ["PRT-000001", "/entities/other/", "https://example.org", null]) {
      expect(readMessage(RESIDUES, { ...message, entity })).toBeNull();
    }
  });
});

describe("messages", () => {
  it("are only those a component may send, within bounds", () => {
    expect(readMessage(RESIDUES, { type: "ready", protocol: PROTOCOL })).toEqual({
      type: "ready",
      protocol: PROTOCOL,
    });
    // A bridge that speaks another version, or doesn't say, isn't one the page can use.
    expect(readMessage(RESIDUES, { type: "ready", protocol: PROTOCOL + 1 })).toBeNull();
    expect(readMessage(RESIDUES, { type: "ready" })).toBeNull();
    expect(readMessage(RESIDUES, { type: "resize", height: 1e9 })).toEqual({
      type: "resize",
      height: MAX_FRAME_HEIGHT,
    });
    expect(readMessage(RESIDUES, { type: "resize", height: -5 })).toEqual({
      type: "resize",
      height: MIN_FRAME_HEIGHT,
    });
    for (const bad of [null, "ready", { type: "navigate", to: "/" }, { type: "resize" }]) {
      expect(readMessage(RESIDUES, bad)).toBeNull();
    }
    expect(readMessage(RESIDUES, { type: "resize", height: Number.NaN })).toBeNull();
  });

  it("select only what it declared it selects, by HRID", () => {
    expect(
      readMessage(RESIDUES, { type: "select", entities: ["PRT-000001", "prt-000001"] }),
    ).toEqual({ type: "select", entities: ["PRT-000001"] });
    expect(readMessage(RESIDUES, { type: "select", entities: [PROTEIN_ID] })).toBeNull();
    expect(readMessage(RESIDUES, { type: "select", entities: ["PRT-1/../x"] })).toBeNull();
    expect(
      readMessage({ ...RESIDUES, selects: null }, { type: "select", entities: [] }),
    ).toBeNull();
    expect(readMessage(RESIDUES, { type: "select", entities: [7] })).toBeNull();
    expect(readMessage(RESIDUES, { type: "select", entities: ["x".repeat(101)] })).toBeNull();
    const many = Array.from({ length: MAX_SELECTED + 1 }, (_, i) => `PRT-${String(i)}`);
    expect(readMessage(RESIDUES, { type: "select", entities: many })).toBeNull();
  });
});
