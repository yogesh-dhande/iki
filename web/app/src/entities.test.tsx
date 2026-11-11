/**
 * The type browser, a project's entity grid and an entity's page, against a
 * fake API.
 */

import { screen, waitFor, within } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import {
  ALICE,
  entity,
  FILE_ID,
  KINASE,
  nth,
  PROTEIN_ID,
  renderApp,
  revision,
  withTypes,
} from "./entity-fixtures";
import { answer, calls, page, problem } from "./test-server";

// jsdom lays nothing out; the grid draws the rows that fit in its scrolling region.
const sizes = { clientHeight: 600, clientWidth: 1000, offsetHeight: 600, offsetWidth: 1000 };
const saved: Record<string, PropertyDescriptor | undefined> = {};
beforeAll(() => {
  for (const [name, value] of Object.entries(sizes)) {
    saved[name] = Object.getOwnPropertyDescriptor(HTMLElement.prototype, name);
    Object.defineProperty(HTMLElement.prototype, name, { configurable: true, get: () => value });
  }
});
afterAll(() => {
  for (const [name, descriptor] of Object.entries(saved)) {
    if (descriptor) Object.defineProperty(HTMLElement.prototype, name, descriptor);
  }
});

describe("the type browser", () => {
  it("lists the types by plugin, and shows a type's fields and rules", async () => {
    withTypes();
    const { user } = await renderApp("/types/");
    expect(screen.getByRole("heading", { level: 2, name: "protein" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Types" })).toHaveAttribute("aria-current", "page");
    await user.click(screen.getByRole("link", { name: "Protein structure" }));
    await screen.findByRole("heading", { level: 1, name: "Protein structure" });
    const rows = within(screen.getByRole("table")).getAllByRole("row");
    expect(rows.map((r) => r.textContent)).toEqual([
      "FieldHoldsRules",
      "StructurestructureFileRequired",
      "ResolutionresolutionQuantity in Å",
      "ChainschainsList of: Reference to Protein",
    ]);
    expect(document.title).toBe("Protein structure · Example Bio");
  });

  it("says what makes two the same, and what creating a duplicate does", async () => {
    withTypes();
    await renderApp("/types/protein.Protein/");
    expect(
      screen.getByText(/Two are the same when their Sequence match\. Creating one that’s already/),
    ).toBeInTheDocument();
    expect(screen.getByText("Shown in ku")).toBeInTheDocument();
  });

  it("says when there's no such type", async () => {
    withTypes();
    await renderApp("/types/nope.Nope/");
    expect(
      screen.getByText("There’s nothing here, or you don’t have access to it."),
    ).toBeInTheDocument();
  });
});

describe("a project's entities", () => {
  function onProject(): void {
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
  }

  it("shows one type's fields as columns, in their display units", async () => {
    onProject();
    answer("GET /api/v1/entities", page([entity()]));
    await renderApp("/projects/kinase-atlas/entities/?type=protein.Protein");
    const grid = await screen.findByRole("table", { name: "1 protein" });
    const rows = within(grid).getAllByRole("row");
    const head = nth(rows, 0);
    const first = nth(rows, 1);
    expect(
      within(head)
        .getAllByRole("columnheader")
        .map((c) => c.textContent),
    ).toEqual(["ID", "Name", "Sequence", "Molecular weight (ku)", "Last changed"]);
    const cells = within(first)
      .getAllByRole("cell")
      .map((c) => c.textContent);
    expect(cells.slice(0, 4)).toEqual(["PRT-000001", "ABL1 kinase", "MLEICLKLVG", "52.3415"]);
    expect(within(first).getByRole("link", { name: "PRT-000001" })).toHaveAttribute(
      "href",
      "/entities/PRT-000001/",
    );
    const call = nth(calls("GET", "/api/v1/entities"), 0);
    expect(Object.fromEntries(call.query)).toEqual({
      limit: "100",
      project: KINASE.id,
      type: "protein.Protein",
      archived: "false",
    });
  });

  it("filters by type and by archived, in the address", async () => {
    onProject();
    answer("GET /api/v1/entities", page([entity()]));
    const { router, user } = await renderApp("/projects/kinase-atlas/entities/");
    await screen.findByRole("table", { name: "1 entity" });
    expect(screen.getByRole("link", { name: "Entities" })).toHaveAttribute("aria-current", "page");
    await user.selectOptions(screen.getByLabelText("Type"), "Protein structure");
    await user.selectOptions(screen.getByLabelText("Show"), "Current and archived");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        type: "protein.ProteinStructure",
        show: "all",
      });
    });
    const sent = calls("GET", "/api/v1/entities");
    const last = nth(sent, sent.length - 1);
    expect(last.query.get("type")).toBe("protein.ProteinStructure");
    expect(last.query.has("archived")).toBe(false);
  });

  it("fetches more as the grid scrolls towards the end", async () => {
    onProject();
    const many = Array.from({ length: 40 }, (_, i) =>
      entity({
        id: `00000000-0000-7000-8000-0000000${String(i).padStart(5, "0")}`,
        hrid: `PRT-${String(i + 1).padStart(6, "0")}`,
      }),
    );
    answer("GET /api/v1/entities", ({ query }: { query: URLSearchParams }) =>
      query.get("cursor")
        ? page([entity({ id: "00000000-0000-7000-8000-0000000fffff", hrid: "PRT-000041" })])
        : { items: many, next_cursor: "next" },
    );
    await renderApp("/projects/kinase-atlas/entities/?type=protein.Protein");
    await screen.findByRole("table", { name: "41 proteins" });
    const sent = calls("GET", "/api/v1/entities");
    expect(nth(sent, sent.length - 1).query.get("cursor")).toBe("next");
  });

  it("says when there's nothing to show", async () => {
    onProject();
    answer("GET /api/v1/entities", page([]));
    await renderApp("/projects/kinase-atlas/entities/?type=protein.Protein");
    expect(await screen.findByText("No proteins in this project.")).toBeInTheDocument();
  });
});

describe("an entity's page", () => {
  function onStructure(): void {
    withTypes();
    answer(
      "GET /api/v1/entities/PST-000001",
      entity({
        id: "00000000-0000-7000-8000-00000000e002",
        hrid: "PST-000001",
        type: "protein.ProteinStructure",
        values: { structure: FILE_ID, resolution: 2.1, chains: [PROTEIN_ID, "gone"] },
        refs: {
          [PROTEIN_ID]: {
            hrid: "PRT-000001",
            title: "ABL1 kinase",
            type: "protein.Protein",
            archived: false,
          },
        },
        files: { [FILE_ID]: { name: "1abc.pdb", media_type: "chemical/x-pdb", size: 52_428_800 } },
      }),
    );
    answer("GET /api/v1/entities/PST-000001/provenance", {
      structure: revision({ number: 1, via: "api" }),
      resolution: revision({ number: 2, kind: "edited", reason: "Refined" }),
    });
    answer("GET /api/v1/entities/PST-000001/projects", [
      {
        project: { id: KINASE.id, name: KINASE.name, slug: KINASE.slug },
        owner: true,
        linked_at: "2026-03-14T09:00:00Z",
        linked_by: ALICE,
      },
      {
        project: { id: "00000000-0000-7000-8000-0000000000p2", name: "Imatinib", slug: "imatinib" },
        owner: false,
        linked_at: "2026-03-14T09:00:00Z",
        linked_by: ALICE,
      },
    ]);
  }

  it("shows each value with who set it, how and when", async () => {
    onStructure();
    await renderApp("/entities/PST-000001/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("PST-000001");
    const values = screen.getByRole("region", { name: "Values" });
    const file = within(values).getByRole("link", { name: "1abc.pdb" });
    expect(file).toHaveAttribute("href", `/api/v1/files/${FILE_ID}/content`);
    expect(within(values).getByText("50 MiB · chemical/x-pdb")).toBeInTheDocument();
    expect(within(values).getByText("2.1 Å")).toBeInTheDocument();
    expect(within(values).getByRole("link", { name: "ABL1 kinase" })).toHaveAttribute(
      "href",
      "/entities/PRT-000001/",
    );
    expect(within(values).getByText("An entity you can’t read")).toBeInTheDocument();
    expect(await within(values).findByText(/through the API/)).toHaveTextContent(
      /^Set by Rosalind Franklin .+ through the API \(revision 1\)$/,
    );
    expect(within(values).getByText(/Refined/)).toHaveTextContent(
      /^Changed by Rosalind Franklin .+ in the web app: “Refined” \(revision 2\)$/,
    );
    const details = screen.getByRole("region", { name: "Details" });
    expect(await within(details).findByRole("link", { name: "Imatinib" })).toHaveAttribute(
      "href",
      "/projects/imatinib/",
    );
    expect(document.title).toBe("PST-000001 (PST-000001) · Example Bio");
  });

  it("titles a named entity by its name, and converts to the display unit", async () => {
    withTypes();
    answer("GET /api/v1/entities/PRT-000001", entity());
    answer("GET /api/v1/entities/PRT-000001/provenance", {});
    answer("GET /api/v1/entities/PRT-000001/projects", []);
    await renderApp("/entities/PRT-000001/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      /^ABL1 kinase\s*PRT-000001$/,
    );
    expect(screen.getByText("52.3415 ku")).toBeInTheDocument();
    expect(screen.getByText("(52,341.5 u)")).toBeInTheDocument();
  });

  it("shows its history, and what each revision changed", async () => {
    onStructure();
    answer(
      "GET /api/v1/entities/PST-000001/revisions",
      page([
        revision({ number: 2, kind: "edited", changed_fields: ["resolution"], reason: "Refined" }),
        revision({ number: 1, changed_fields: ["structure", "resolution"], via: "api" }),
      ]),
    );
    answer("GET /api/v1/entities/PST-000001/revisions/2", {
      ...revision({ number: 2, kind: "edited", changed_fields: ["resolution"] }),
      values: {},
      changes: { resolution: { before: 2.4, after: 2.1 } },
    });
    const { user } = await renderApp("/entities/PST-000001/history/");
    expect(await screen.findByText("Revision 2: Edited by Rosalind Franklin")).toBeInTheDocument();
    expect(screen.getByText("Revision 1: Created by Rosalind Franklin")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "History" })).toHaveAttribute("aria-current", "page");
    await user.click(screen.getByRole("button", { name: "Show changes in revision 2" }));
    const table = await screen.findByRole("table");
    expect(
      within(table)
        .getAllByRole("row")
        .map((r) => r.textContent),
    ).toEqual(["FieldBeforeAfter", "Resolution2.4 Å2.1 Å"]);
  });

  it("says when there's no such entity", async () => {
    withTypes();
    answer(
      "GET /api/v1/entities/PRT-999999",
      problem(404, "not_found", "No such entity, or you can’t read it."),
    );
    await renderApp("/entities/PRT-999999/");
    expect(
      screen.getByText("There’s nothing here, or you don’t have access to it."),
    ).toBeInTheDocument();
  });
});
