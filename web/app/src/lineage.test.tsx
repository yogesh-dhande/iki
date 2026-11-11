/**
 * An entity's lineage: how its graph is laid out, and its page, against a fake
 * API.
 */

import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { entity, PROTEIN_ID, renderApp, withTypes } from "./entity-fixtures";
import { connected, entityKey, layout, runKey, type LineageGraph } from "./lineage";
import { ACTION } from "./run-fixtures";
import { answer, calls } from "./test-server";

const PARENT = "00000000-0000-7000-8000-00000000e000";
const CHILD = "00000000-0000-7000-8000-00000000e002";

function node(id: string, hrid: string, title = hrid) {
  return { id, hrid, title, type: "protein.Protein", archived: false };
}

/** DNA-000001 → RUN-000001 → PRT-000001 (the page's) → a hidden run → PRT-000002. */
function graph(overrides: Partial<LineageGraph> = {}): LineageGraph {
  return {
    root: PROTEIN_ID,
    entities: [
      node(PARENT, "PRT-000000", "ABL1 precursor"),
      node(PROTEIN_ID, "PRT-000001", "ABL1 kinase"),
      { ...node(CHILD, "PRT-000002"), archived: true },
    ],
    runs: [
      {
        key: "RUN-000001",
        run: {
          id: "00000000-0000-7000-8000-0000000a0001",
          hrid: "RUN-000001",
          action: ACTION.name,
        },
      },
      { key: "hidden-1", run: null },
    ],
    links: [
      { parent: PARENT, entity: PROTEIN_ID, run: "RUN-000001", input: "protein", revision: 2 },
      { parent: PROTEIN_ID, entity: CHILD, run: "hidden-1", input: "protein", revision: 1 },
    ],
    earlier: true,
    later: false,
    capped: false,
    ...overrides,
  };
}

describe("laying out a lineage", () => {
  it("puts entities and runs in columns, earliest on the left", () => {
    const shape = layout(graph());
    const columns = shape.nodes.map((n) => [n.key, n.x]);
    expect(columns).toEqual([
      [entityKey(PARENT), 0],
      [runKey("RUN-000001"), 16],
      [entityKey(PROTEIN_ID), 32],
      [runKey("hidden-1"), 48],
      [entityKey(CHILD), 64],
    ]);
    expect(shape.edges.map((e) => [e.from, e.to])).toEqual([
      [entityKey(PARENT), runKey("RUN-000001")],
      [runKey("RUN-000001"), entityKey(PROTEIN_ID)],
      [entityKey(PROTEIN_ID), runKey("hidden-1")],
      [runKey("hidden-1"), entityKey(CHILD)],
    ]);
    expect(shape.width).toBe(5 * 13 + 4 * 3);
    // Pointing at the run lights up where it came from and what came of it.
    expect(connected(shape.edges, runKey("RUN-000001"))).toEqual(
      new Set(shape.nodes.map((n) => n.key)),
    );
  });

  it("keeps a run's inputs side by side, and copes with a loop", () => {
    const shape = layout(
      graph({
        entities: [
          node(PARENT, "PRT-000000"),
          node(PROTEIN_ID, "PRT-000001"),
          node(CHILD, "PRT-000002"),
        ],
        runs: [{ key: "hidden-1", run: null }],
        links: [
          { parent: PARENT, entity: CHILD, run: "hidden-1", input: "a", revision: 1 },
          { parent: PROTEIN_ID, entity: CHILD, run: "hidden-1", input: "b", revision: 1 },
          // A run that reused an entity made from it.
          { parent: CHILD, entity: PARENT, run: "hidden-1", input: "a", revision: 1 },
        ],
      }),
    );
    expect(shape.nodes).toHaveLength(4);
    expect(new Set(shape.nodes.map((n) => n.x)).size).toBeGreaterThan(1);
  });
});

describe("an entity's lineage page", () => {
  function onEntity(lineage: LineageGraph = graph()): void {
    withTypes();
    answer("GET /api/v1/entities/PRT-000001", entity());
    answer("GET /api/v1/entities/PRT-000001/lineage/graph", lineage);
    answer("GET /api/v1/actions", [ACTION]);
  }

  it("draws where it came from and what came of it, each linked", async () => {
    onEntity();
    const { user } = await renderApp("/entities/PRT-000001/");
    await user.click(screen.getByRole("link", { name: "Lineage" }));
    const figure = await screen.findByRole("group", { name: /^3 entities and 2 runs/ });
    expect(screen.getByRole("link", { name: "Lineage" })).toHaveAttribute("aria-current", "page");
    expect(document.title).toBe("Lineage of ABL1 kinase (PRT-000001) · Example Bio");
    expect(within(figure).getByRole("link", { name: /^ABL1 precursor/ })).toHaveAttribute(
      "href",
      "/entities/PRT-000000/lineage/",
    );
    expect(within(figure).getByRole("link", { name: /^Compute properties/ })).toHaveAttribute(
      "href",
      "/runs/RUN-000001/",
    );
    expect(within(figure).getByText("(this entity)")).toBeInTheDocument();
    expect(within(figure).queryByRole("link", { name: /ABL1 kinase/ })).not.toBeInTheDocument();
    expect(within(figure).getByText("A run you can’t see")).toBeInTheDocument();
    expect(within(figure).getByRole("link", { name: /^PRT-000002/ })).toHaveTextContent(
      "PRT-000002 Protein · Archived",
    );
    expect(calls("GET", "/api/v1/entities/PRT-000001/lineage/graph").at(-1)?.query.toString()).toBe(
      "up=3&down=3",
    );

    // Pointing at a node lights up its line of descent and dims the rest.
    const run = within(figure).getByRole("link", { name: /^Compute properties/ });
    await user.hover(run);
    expect(run).toHaveClass("is-lit");
    expect(within(figure).getByText("A run you can’t see").closest(".lineage-node")).toHaveClass(
      "is-lit",
    );
  });

  it("goes further back when asked", async () => {
    onEntity();
    const { user, router } = await renderApp("/entities/PRT-000001/lineage/");
    expect(screen.queryByRole("link", { name: "Show later" })).not.toBeInTheDocument();
    await user.click(await screen.findByRole("link", { name: "Show earlier" }));
    expect(router.state.location.search).toEqual({ up: 6 });
    await screen.findByRole("group", { name: /^3 entities/ });
    expect(calls("GET", "/api/v1/entities/PRT-000001/lineage/graph").at(-1)?.query.toString()).toBe(
      "up=6&down=3",
    );
  });

  it("reads as steps, for those who'd rather", async () => {
    onEntity();
    const { user } = await renderApp("/entities/PRT-000001/lineage/");
    await user.click(await screen.findByRole("link", { name: "List" }));
    const first = await screen.findByRole("heading", {
      level: 2,
      name: "RUN-000001 · Compute properties",
    });
    expect(first.closest("li")).toHaveTextContent(
      "GivenABL1 precursor PRT-000000 · Protein as protein, at revision 2MadeABL1 kinase PRT-000001 · Protein (this entity)",
    );
    expect(
      screen.getByRole("heading", {
        level: 2,
        name: "A run you can’t see, in a project you can’t read",
      }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "List" })).toHaveAttribute("aria-current", "page");
    // Following an entity keeps to the list.
    expect(screen.getByRole("link", { name: /^ABL1 precursor/ })).toHaveAttribute(
      "href",
      "/entities/PRT-000000/lineage/?view=list",
    );
  });

  it("says when it's cut short, or there's none", async () => {
    onEntity(graph({ capped: true, earlier: true }));
    await renderApp("/entities/PRT-000001/lineage/?up=10");
    expect(await screen.findByRole("note")).toHaveTextContent(
      "There’s more than fits on one page, so some is left out. Open an entity in it to follow its lineage from there.It goes further than 10 generations. Open the furthest entity to follow it on.",
    );
    expect(screen.queryByRole("link", { name: "Show earlier" })).not.toBeInTheDocument();
  });

  it("says when there's no lineage", async () => {
    onEntity(
      graph({
        entities: [node(PROTEIN_ID, "PRT-000001", "ABL1 kinase")],
        runs: [],
        links: [],
        earlier: false,
      }),
    );
    await renderApp("/entities/PRT-000001/lineage/");
    expect(
      await screen.findByText(
        "No lineage you can see: no run made ABL1 kinase, and nothing you can read has been made from it.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });
});
