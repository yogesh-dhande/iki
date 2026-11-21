/**
 * Components that show many entities (ADR 0117): what fills each place, the frame's
 * paging and refining, which can only narrow what it was given, and the places
 * themselves: an entity's tab, the grid's results, a project's page and a person's home.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  gridViews,
  MAX_REFINE_CONDITIONS,
  MAX_SECTIONS,
  readMessage,
  readRefinement,
  sectionsFor,
  type ComponentEntity,
  type PluginComponent,
} from "./components";
import { entity, PROTEIN, renderApp, STRUCTURE, withTypes } from "./entity-fixtures";
import { connectFrame, lastEntities } from "./frame-fixtures";
import { answer, calls, page, project, type Sent } from "./test-server";

const KINASE = project();
const BOARD_PATH = "/api/v1/components/iki/protein.board/entities";

const BOARD: PluginComponent = {
  name: "iki/protein.board",
  plugin: "iki/protein",
  label: "Board",
  description: "Proteins as cards.",
  element: "protein-board",
  slots: [
    {
      slot: "entity.tab",
      type: "iki/protein.ProteinStructure",
      label: "Chains",
      search: {
        where: { field: "structures", op: "has", value: "{{ entity }}" },
        sort: "name",
        descending: false,
      },
    },
    { slot: "search.results", type: "iki/protein.Protein", label: "Board", search: null },
    {
      slot: "project.page",
      type: "iki/protein.Protein",
      label: "Protein board",
      search: { where: null, sort: "name", descending: false },
    },
    {
      slot: "person.home",
      type: "iki/protein.Protein",
      label: "Proteins you added",
      search: {
        where: { field: "created_by", op: "eq", value: "{{ me }}" },
        sort: "name",
        descending: false,
      },
    },
  ],
  entities: { type: "iki/protein.Protein", fields: ["name", "sequence"], limit: 3 },
  selects: "iki/protein.Protein",
  runs: [],
  sha256: "cd".repeat(32),
  frame: `/plugin-components/iki/protein.board/${"cd".repeat(32)}/`,
};

function item(n: number): ComponentEntity {
  return {
    id: `00000000-0000-7000-8000-0000000c000${String(n)}`,
    hrid: `PRT-00000${String(n)}`,
    title: `Protein ${String(n)}`,
    type: "iki/protein.Protein",
    revision: 1,
    project: { id: KINASE.id, name: KINASE.name, slug: KINASE.slug },
    values: { name: `Protein ${String(n)}`, sequence: "MKV" },
    refs: {},
  };
}

describe("refinements", () => {
  it("name only the fields the component reads, and hrid", () => {
    const name = { field: "name", op: "starts_with", value: "AB" };
    expect(readRefinement(BOARD, { where: name })).toEqual({ where: name });
    expect(readRefinement(BOARD, { where: { field: "hrid", op: "eq", value: "PRT-1" } })).toEqual({
      where: { field: "hrid", op: "eq", value: "PRT-1" },
    });
    expect(readRefinement(BOARD, { where: { field: "lims_id", op: "eq", value: "L" } })).toBe(
      undefined,
    );
    expect(
      readRefinement(BOARD, { where: { any: [name, { not: { field: "notes", op: "eq" } }] } }),
    ).toBe(undefined);
    expect(readRefinement(BOARD, { sort: { field: "molecular_weight" } })).toBe(undefined);
    expect(readRefinement(BOARD, { sort: { field: "name", descending: true } })).toEqual({
      sort: { field: "name", descending: true },
    });
  });

  it("are within bounds, and nothing else", () => {
    const leaf = { field: "name", op: "eq", value: "x" };
    let deep: unknown = leaf;
    for (let i = 0; i < 3; i += 1) deep = { not: deep };
    expect(readRefinement(BOARD, { where: deep })).not.toBe(undefined);
    expect(readRefinement(BOARD, { where: { not: deep } })).toBe(undefined);
    const many = Array.from({ length: MAX_REFINE_CONDITIONS + 1 }, () => leaf);
    expect(readRefinement(BOARD, { where: { any: many } })).toBe(undefined);
    expect(readRefinement(BOARD, { where: { any: many.slice(1) } })).not.toBe(undefined);
    expect(readRefinement(BOARD, { where: leaf, type: "iki/dna.DNASequence" })).toBe(undefined);
    expect(readRefinement(BOARD, { where: { ...leaf, project: "elsewhere" } })).toBe(undefined);
    expect(readRefinement(BOARD, { where: { all: [leaf], any: [leaf] } })).toBe(undefined);
    expect(readRefinement(BOARD, { q: "x".repeat(201) })).toBe(undefined);
    expect(readRefinement(BOARD, { where: { ...leaf, value: "x".repeat(10_001) } })).toBe(
      undefined,
    );
    expect(readRefinement(BOARD, null)).toBeNull();
    expect(readRefinement(BOARD, { q: "  " })).toBeNull();
    expect(readRefinement({ ...BOARD, entities: null }, { q: "x" })).toBe(undefined);
  });

  it("and asking for more are for components that show many; opening is by HRID", () => {
    const one = { ...BOARD, entities: null };
    expect(readMessage(BOARD, { type: "more" })).toEqual({ type: "more" });
    expect(readMessage(one, { type: "more" })).toBeNull();
    expect(readMessage(BOARD, { type: "refine", refinement: { q: "ab" } })).toEqual({
      type: "refine",
      refinement: { q: "ab" },
    });
    expect(readMessage(BOARD, { type: "refine", refinement: { sort: { field: "x" } } })).toBeNull();
    expect(readMessage(BOARD, { type: "open", entity: "prt-000001" })).toEqual({
      type: "open",
      entity: "PRT-000001",
    });
    expect(readMessage(BOARD, { type: "open", entity: "/projects/x" })).toBeNull();
  });
});

describe("places", () => {
  it("are the slots each component declares", () => {
    const types = [PROTEIN, STRUCTURE];
    expect(gridViews("iki/protein.Protein", types, [BOARD])).toEqual([
      { component: BOARD, label: "Board" },
    ]);
    expect(gridViews("iki/protein.ProteinStructure", types, [BOARD])).toEqual([]);
    expect(gridViews(undefined, types, [BOARD])).toEqual([]);
    expect(sectionsFor("project.page", [BOARD])).toEqual([
      { component: BOARD, label: "Protein board" },
    ]);
    const many = Array.from({ length: MAX_SECTIONS + 2 }, (_, i) => ({
      ...BOARD,
      name: `iki/protein.board_${String(i)}`,
    }));
    expect(sectionsFor("person.home", many)).toHaveLength(MAX_SECTIONS);
  });
});

function structure(): Schemas["EntityOut"] {
  return entity({
    id: "00000000-0000-7000-8000-00000000e00s",
    hrid: "PST-000001",
    type: "iki/protein.ProteinStructure",
    values: { method: "xray" },
  });
}

describe("an entity's tab", () => {
  function onTab(answers: (call: Sent) => unknown) {
    withTypes();
    answer("GET /api/v1/components", [BOARD]);
    answer("GET /api/v1/entities/PST-000001", structure());
    answer(`POST ${BOARD_PATH}`, answers);
  }

  it("gets what its search finds about the entity, a page at a time, up to its limit", async () => {
    onTab((call) => {
      const body = call.body as { cursor?: string };
      return body.cursor
        ? { items: [item(3), item(4)], next_cursor: "c2" }
        : { items: [item(1), item(2)], next_cursor: "c1" };
    });
    await renderApp("/entities/PST-000001/components/iki/protein.board/");
    const frame = await screen.findByTitle<HTMLIFrameElement>("Chains");
    const { port, received } = connectFrame(frame);
    await vi.waitFor(() => {
      expect(lastEntities(received)?.items).toHaveLength(2);
    });
    expect(received[0]).toEqual({ type: "place", place: { slot: "entity.tab", label: "Chains" } });
    expect(received.some((m) => m["type"] === "entity")).toBe(true);
    const [first] = calls("POST", BOARD_PATH);
    expect(first?.body).toEqual({ home: false, entity: structure().id, limit: 3 });
    expect(lastEntities(received)).toMatchObject({ more: true, limited: false });

    port.postMessage({ type: "more" });
    await vi.waitFor(() => {
      expect(lastEntities(received)?.items.map((e) => e.hrid)).toEqual([
        "PRT-000001",
        "PRT-000002",
        "PRT-000003",
      ]);
    });
    // It holds its limit, so it gets no more.
    expect(lastEntities(received)).toMatchObject({ more: false, limited: true });
    expect(calls("POST", BOARD_PATH)[1]?.body).toMatchObject({ cursor: "c1" });
    port.postMessage({ type: "more" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(calls("POST", BOARD_PATH)).toHaveLength(2);
  });

  it("narrows its place as the frame asks, and refuses what it can't ask for", async () => {
    onTab(() => page([item(1)]));
    await renderApp("/entities/PST-000001/components/iki/protein.board/");
    const { port, received } = connectFrame(await screen.findByTitle<HTMLIFrameElement>("Chains"));
    await vi.waitFor(() => {
      expect(lastEntities(received)?.items).toHaveLength(1);
    });
    // A field it doesn't read: refused by the page, so never asked for.
    port.postMessage({
      type: "refine",
      refinement: { where: { field: "lims_id", op: "eq", value: "L-1" } },
    });
    port.postMessage({ type: "refine", refinement: { q: "abl" } });
    await vi.waitFor(() => {
      expect(calls("POST", BOARD_PATH)).toHaveLength(2);
    });
    expect(calls("POST", BOARD_PATH)[1]?.body).toEqual({
      home: false,
      entity: structure().id,
      refine: { q: "abl" },
      limit: 3,
    });
    await vi.waitFor(() => {
      expect(lastEntities(received)?.refinement).toEqual({ q: "abl" });
    });
  });

  it("opens only an entity it was given, as Iki's page", async () => {
    onTab(() => page([item(1)]));
    answer("GET /api/v1/entities/PRT-000001", entity());
    const { router } = await renderApp("/entities/PST-000001/components/iki/protein.board/");
    const { port, received } = connectFrame(await screen.findByTitle<HTMLIFrameElement>("Chains"));
    await vi.waitFor(() => {
      expect(lastEntities(received)?.items).toHaveLength(1);
    });
    port.postMessage({ type: "open", entity: "PRT-000009" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(router.state.location.pathname).toBe(
      "/entities/PST-000001/components/iki/protein.board/",
    );
    port.postMessage({ type: "open", entity: "PRT-000001" });
    await vi.waitFor(() => {
      expect(router.state.location.pathname).toBe("/entities/PRT-000001/");
    });
    expect(router.state.location.search).toEqual({ project: "kinase-atlas" });
  });
});

describe("the grid's results", () => {
  it("can be shown in a component, given the grid's search", async () => {
    withTypes();
    answer("GET /api/v1/components", [BOARD]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/saved-searches", []);
    answer("POST /api/v1/entities/search", page([entity()]));
    answer(`POST ${BOARD_PATH}`, page([item(1)]));
    const { user } = await renderApp(
      "/projects/kinase-atlas/entities/?type=iki/protein.Protein&q=abl",
    );
    const showAs = await screen.findByRole("combobox", { name: "Show as" });
    await user.selectOptions(showAs, "iki/protein.board");
    const frame = await screen.findByTitle<HTMLIFrameElement>("Board");
    const { received } = connectFrame(frame);
    await vi.waitFor(() => {
      expect(lastEntities(received)?.items).toHaveLength(1);
    });
    expect(calls("POST", BOARD_PATH)[0]?.body).toEqual({
      home: false,
      search: {
        exact_type: false,
        project: KINASE.id,
        type: "iki/protein.Protein",
        archived: false,
        q: "abl",
      },
      limit: 3,
    });
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Show as" }), "");
    expect(await screen.findByRole("table")).toBeInTheDocument();
  });
});

describe("sections", () => {
  function onProject() {
    withTypes();
    answer("GET /api/v1/components", [BOARD]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer(`GET /api/v1/projects/${KINASE.id}/dashboard`, {
      app: null,
      set_by: null,
      set_at: null,
    });
  }

  it("show on a project's page once their search finds something there", async () => {
    onProject();
    answer(`POST ${BOARD_PATH}`, page([item(1)]));
    await renderApp("/projects/kinase-atlas/");
    const section = await screen.findByRole("region", { name: "Protein board" });
    expect(within(section).getByTitle("Protein board")).toHaveAttribute("sandbox", "allow-scripts");
    expect(calls("POST", BOARD_PATH)[0]?.body).toEqual({
      home: false,
      project: KINASE.id,
      limit: 3,
    });
  });

  it("take no room when they find nothing", async () => {
    onProject();
    answer(`POST ${BOARD_PATH}`, page([]));
    await renderApp("/projects/kinase-atlas/");
    await vi.waitFor(() => {
      expect(calls("POST", BOARD_PATH)).toHaveLength(1);
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole("region", { name: "Protein board" })).not.toBeInTheDocument();
  });

  it("show on a person's home, of what names them", async () => {
    withTypes();
    answer("GET /api/v1/components", [BOARD]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/registries", page([]));
    answer(`POST ${BOARD_PATH}`, page([item(1), item(2)]));
    await renderApp("/");
    const section = await screen.findByRole("region", { name: "Proteins you added" });
    expect(within(section).getByTitle("Proteins you added")).toBeInTheDocument();
    expect(calls("POST", BOARD_PATH)[0]?.body).toEqual({ home: true, limit: 3 });
  });

  it("say so when their search is refused", async () => {
    onProject();
    answer(
      `POST ${BOARD_PATH}`,
      () =>
        new Response(
          JSON.stringify({
            type: "about:blank",
            title: "Unprocessable Content",
            status: 422,
            code: "component_misdeclared",
            detail: "Board’s search doesn’t work here: tell whoever looks after the plugin.",
          }),
          { status: 422, headers: { "Content-Type": "application/problem+json" } },
        ),
    );
    await renderApp("/projects/kinase-atlas/");
    const section = await screen.findByRole("region", { name: "Protein board" });
    expect(await within(section).findByText(/search doesn’t work here/)).toBeInTheDocument();
  });
});
