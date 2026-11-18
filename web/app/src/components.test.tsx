/**
 * Plugins' components (ADR 0084): their tabs on entity pages, the sandboxed frame they
 * run in, and the page's checks on every message a frame sends.
 */

import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { START_TIMEOUT_MS } from "./component-frame";
import {
  entityTabs,
  lineageOf,
  MAX_FRAME_HEIGHT,
  MAX_SELECTED,
  MIN_FRAME_HEIGHT,
  readMessage,
  type PluginComponent,
} from "./components";
import { entity, PROTEIN, PROTEIN_ID, renderApp, STRUCTURE, withTypes } from "./entity-fixtures";
import { answer, calls, page } from "./test-server";

const RESIDUES: PluginComponent = {
  name: "iki/protein.residues",
  plugin: "iki/protein",
  label: "Residues",
  description: "How many residues a protein has.",
  element: "protein-residues",
  slots: [{ slot: "entity.tab", type: "iki/protein.Protein", label: "Residues", search: null }],
  entities: null,
  selects: "iki/protein.Protein",
  sha256: "ab".repeat(32),
  frame: `/plugin-components/iki/protein.residues/${"ab".repeat(32)}/`,
};

const CHILD = { ...STRUCTURE, name: "iki/protein.Mutant", extends: "iki/protein.Protein" };

function onResidues() {
  withTypes();
  answer("GET /api/v1/components", [RESIDUES]);
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

  it("say so when there's no such component for the entity", async () => {
    onResidues();
    answer("GET /api/v1/components", []);
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
    expect(message).toEqual({ type: "iki-connect" });
    expect(origin).toBe("*");
    port.postMessage({ type: "ready" });
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
    port.postMessage({ type: "ready" });
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

describe("messages", () => {
  it("are only those a component may send, within bounds", () => {
    expect(readMessage(RESIDUES, { type: "ready" })).toEqual({ type: "ready" });
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
