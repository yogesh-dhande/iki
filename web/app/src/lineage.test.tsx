/**
 * An entity's lineage (ADR 0123): the tree its walk makes, how its graph is laid out, and
 * its page, as a tree, a list and a graph, against a fake API.
 */

import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { entity, PROTEIN_ID, renderApp, withTypes } from "./entity-fixtures";
import {
  connected,
  entityKey,
  layout,
  runKey,
  stepWords,
  walkTree,
  type LineageGraph,
  type Walk,
  type WalkEntity,
  type WalkItem,
  type WalkStep,
} from "./lineage";
import { searchWords } from "./filters";
import { ACTION } from "./run-fixtures";
import { answer, calls } from "./test-server";

const PARENT = "00000000-0000-7000-8000-00000000e000";
const CHILD = "00000000-0000-7000-8000-00000000e002";
const GRANDCHILD = "00000000-0000-7000-8000-00000000e003";
const RUN = { id: "00000000-0000-7000-8000-0000000a0001", hrid: "RUN-000001", action: ACTION.name };

function node(id: string, hrid: string, title = hrid): WalkEntity {
  return {
    id,
    hrid,
    title,
    type: "iki/protein.Protein",
    archived: false,
    purged: false,
    entry: false,
    project: { id: "00000000-0000-7000-8000-0000000000p1", name: "Kinase Atlas", slug: "kinase" },
  };
}

function step(to: WalkEntity, overrides: Partial<WalkStep> = {}): WalkStep {
  return {
    kind: "reference",
    field: "source",
    run: null,
    hidden_run: false,
    file: null,
    revision: null,
    entity: to,
    ...overrides,
  };
}

const child = node(CHILD, "PRT-000002", "ABL1 mutant");
const grandchild = { ...node(GRANDCHILD, "PRT-000003"), archived: true };

/** Downstream of PRT-000001: PRT-000002 refers to it; a run made PRT-000003 from that. */
function items(): WalkItem[] {
  return [
    { entity: child, depth: 1, path: [step(child)] },
    {
      entity: grandchild,
      depth: 2,
      path: [
        step(child),
        step(grandchild, { kind: "run", field: "protein", run: RUN, revision: 2 }),
      ],
    },
  ];
}

function second(): WalkItem {
  const [, found] = items();
  if (!found) throw new Error("No second item");
  return found;
}

function walk(overrides: Partial<Walk> = {}): Walk {
  return {
    root: node(PROTEIN_ID, "PRT-000001", "ABL1 kinase"),
    direction: "downstream",
    depth: 10,
    items: items(),
    next_cursor: null,
    total: 2,
    hidden: 0,
    beyond: false,
    ...overrides,
  };
}

/** PRT-000000 → RUN-000001 → PRT-000001 (the page's) → PRT-000002 by reference. */
function graph(overrides: Partial<LineageGraph> = {}): LineageGraph {
  const at = (e: WalkEntity, n: number) => ({ ...e, at: n });
  return {
    root: PROTEIN_ID,
    entities: [
      at(node(PARENT, "PRT-000000", "ABL1 precursor"), -1),
      at(node(PROTEIN_ID, "PRT-000001", "ABL1 kinase"), 0),
      at({ ...child, archived: true }, 1),
    ],
    runs: [{ key: "RUN-000001", run: RUN }],
    edges: [
      {
        node: PROTEIN_ID,
        on: PARENT,
        kind: "run",
        field: "protein",
        run: "RUN-000001",
        file: null,
        revision: 2,
      },
      {
        node: CHILD,
        on: PROTEIN_ID,
        kind: "reference",
        field: "source",
        run: null,
        file: null,
        revision: null,
      },
    ],
    hidden: 0,
    earlier: true,
    later: false,
    capped: false,
    ...overrides,
  };
}

describe("a walk's results", () => {
  it("make a tree, each under what it was reached from", () => {
    const tree = walkTree(items());
    expect(tree.map((n) => n.item.entity.hrid)).toEqual(["PRT-000002"]);
    expect(tree[0]?.children.map((n) => n.item.entity.hrid)).toEqual(["PRT-000003"]);
    // One whose path runs through something not shown goes to the top.
    const orphan = { ...second(), path: [step(node(PARENT, "X")), step(grandchild)] };
    expect(walkTree([orphan]).map((n) => n.item.entity.hrid)).toEqual(["PRT-000003"]);
  });

  it("say why each is there, either way", () => {
    const [first, by] = second().path as [WalkStep, WalkStep];
    expect(stepWords(first, "PRT-000001", "downstream")).toBe("PRT-000002’s source is PRT-000001");
    expect(stepWords(by, "PRT-000002", "downstream")).toBe(
      "RUN-000001 made PRT-000003 from PRT-000002 (its protein, at revision 2)",
    );
    expect(stepWords(by, "PRT-000002", "upstream")).toBe(
      "RUN-000001 made PRT-000002 from PRT-000003 (its protein, at revision 2)",
    );
    const draw = step(node(CHILD, "STK-000001"), { kind: "used", field: "" });
    expect(stepWords(draw, "RES-000001", "upstream")).toBe(
      "RES-000001 used material drawn from STK-000001",
    );
    expect(
      stepWords({ ...draw, entity: node(PARENT, "RES-000001") }, "STK-000001", "downstream"),
    ).toBe("RES-000001 used material drawn from STK-000001");
    expect(stepWords({ ...by, run: null, hidden_run: true }, "X", "downstream")).toMatch(
      /^A run you can’t see made/,
    );
  });
});

describe("searching by lineage", () => {
  it("reads downstream: and upstream: in the search box as conditions", () => {
    expect(searchWords("downstream:DNR-000001 lot 7")).toEqual({
      text: "lot 7",
      conditions: [{ field: "downstream", op: "of", value: "DNR-000001" }],
    });
    expect(searchWords("upstream:res-000004").conditions).toEqual([
      { field: "upstream", op: "of", value: "res-000004" },
    ]);
    expect(searchWords("downstream:").text).toBe("downstream:");
  });
});

describe("laying out a lineage", () => {
  it("puts what each depends on to its left, with runs between", () => {
    const shape = layout(graph());
    expect(shape.nodes.map((n) => [n.key, n.x])).toEqual([
      [entityKey(PARENT), 0],
      [runKey("RUN-000001"), 16],
      [entityKey(PROTEIN_ID), 32],
      [entityKey(CHILD), 48],
    ]);
    expect(shape.edges.map((e) => [e.from, e.to])).toEqual([
      [entityKey(PARENT), runKey("RUN-000001")],
      [runKey("RUN-000001"), entityKey(PROTEIN_ID)],
      [entityKey(PROTEIN_ID), entityKey(CHILD)],
    ]);
    expect(connected(shape.edges, runKey("RUN-000001"))).toEqual(
      new Set(shape.nodes.map((n) => n.key)),
    );
  });
});

describe("an entity's lineage page", () => {
  function onEntity(): void {
    withTypes();
    answer("GET /api/v1/entities/PRT-000001", entity());
    answer(
      "GET /api/v1/entities/PRT-000001/upstream",
      walk({ direction: "upstream", items: [], total: 0, hidden: 2 }),
    );
    answer("GET /api/v1/entities/PRT-000001/downstream", walk());
    answer("GET /api/v1/entities/PRT-000001/graph", graph());
    answer("GET /api/v1/actions", [ACTION]);
  }

  it("starts with what it was derived from, and says what it can't show", async () => {
    onEntity();
    const { user } = await renderApp("/entities/PRT-000001/");
    await user.click(screen.getByRole("link", { name: "Lineage" }));
    expect(
      await screen.findByText(
        "Nothing you can read that ABL1 kinase was derived from, within 10 steps.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent(
      "2 trails lead to entities you can’t read, so it stops there.",
    );
    expect(document.title).toBe("Lineage of ABL1 kinase (PRT-000001) · Example Bio");
    expect(screen.getByRole("link", { name: "Derived from" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(calls("GET", "/api/v1/entities/PRT-000001/upstream").at(-1)?.query.toString()).toBe(
      "depth=10&limit=50",
    );
  });

  it("shows what was derived from it as a tree people walk with the keyboard", async () => {
    onEntity();
    const { user, router } = await renderApp("/entities/PRT-000001/lineage/?direction=downstream");
    const tree = await screen.findByRole("tree", { name: "What was derived from it" });
    expect(
      screen.getByText(
        "2 entities you can read were derived from ABL1 kinase, within 10 steps. Nearest first.",
      ),
    ).toBeInTheDocument();
    const top = within(tree).getByRole("treeitem", { name: /^ABL1 mutant/ });
    expect(top).toHaveAttribute("aria-level", "1");
    expect(top).toHaveAttribute("aria-expanded", "true");
    expect(top).toHaveAttribute("tabindex", "0");
    expect(top).toHaveTextContent("PRT-000002’s source is PRT-000001");
    const under = within(tree).getByRole("treeitem", { name: /^PRT-000003/ });
    expect(under).toHaveAttribute("aria-level", "2");
    expect(under).toHaveAttribute("tabindex", "-1");
    expect(under).toHaveTextContent("Archived");

    top.focus();
    await user.keyboard("{ArrowDown}");
    expect(under).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    expect(top).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    expect(top).toHaveAttribute("aria-expanded", "false");
    expect(within(tree).queryByRole("treeitem", { name: /^PRT-000003/ })).not.toBeInTheDocument();
    await user.keyboard("{ArrowRight}");
    expect(top).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{End}");
    expect(within(tree).getByRole("treeitem", { name: /^PRT-000003/ })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(router.state.location.pathname).toBe("/entities/PRT-000003/");
  });

  it("lists the impact, with why each is there, narrowed and downloaded", async () => {
    onEntity();
    const { user, router } = await renderApp(
      "/entities/PRT-000001/lineage/?direction=downstream&view=list",
    );
    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row");
    expect(rows[2]).toHaveTextContent(
      "PRT-000002’s source is PRT-000001RUN-000001 made PRT-000003 from PRT-000002 (its protein, at revision 2)",
    );
    expect(screen.getByRole("link", { name: "Download all as CSV" })).toHaveAttribute(
      "href",
      "/api/v1/entities/PRT-000001/downstream.csv?depth=10",
    );
    await user.selectOptions(screen.getByLabelText("How far"), "2");
    await user.click(screen.getByRole("checkbox", { name: /References/ }));
    expect(router.state.location.search).toEqual({
      direction: "downstream",
      view: "list",
      depth: 2,
      edges: ["run", "file", "aliquot", "transfer", "used"],
    });
    expect(calls("GET", "/api/v1/entities/PRT-000001/downstream").at(-1)?.query.toString()).toBe(
      "depth=2&limit=50&edges=run&edges=file&edges=aliquot&edges=transfer&edges=used",
    );
    expect(screen.getByRole("link", { name: "Download all as CSV" })).toHaveAttribute(
      "href",
      "/api/v1/entities/PRT-000001/downstream.csv?depth=2&edges=run&edges=file&edges=aliquot&edges=transfer&edges=used",
    );
  });

  it("draws a few steps each way, and goes further when asked", async () => {
    onEntity();
    const { user, router } = await renderApp("/entities/PRT-000001/lineage/?view=graph");
    const figure = await screen.findByRole("group", { name: /^3 entities and 1 run/ });
    expect(within(figure).getByRole("link", { name: /^ABL1 precursor/ })).toHaveAttribute(
      "href",
      "/entities/PRT-000000/lineage/?view=graph",
    );
    expect(within(figure).getByRole("link", { name: /^Compute properties/ })).toHaveAttribute(
      "href",
      "/runs/RUN-000001/",
    );
    expect(within(figure).getByText("(this entity)")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Show later" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Show earlier" }));
    expect(router.state.location.search).toEqual({ view: "graph", up: 6 });
    await screen.findByRole("group", { name: /^3 entities/ });
    expect(calls("GET", "/api/v1/entities/PRT-000001/graph").at(-1)?.query.toString()).toBe(
      "up=6&down=3",
    );
  });
});
