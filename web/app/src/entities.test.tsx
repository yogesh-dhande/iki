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
import { answer, calls, ME, page, problem, project, respond } from "./test-server";

const SECOND_ID = "00000000-0000-7000-8000-00000000e002";

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
    answer("POST /api/v1/entities/search", page([entity()]));
    await renderApp("/projects/kinase-atlas/entities/?type=protein.Protein");
    const grid = await screen.findByRole("table", { name: "1 protein" });
    const rows = within(grid).getAllByRole("row");
    const head = nth(rows, 0);
    const first = nth(rows, 1);
    expect(
      within(head)
        .getAllByRole("columnheader")
        .map((c) => c.textContent),
    ).toEqual(["", "ID", "Name", "Sequence", "Molecular weight (ku)", "Last changed"]);
    const cells = within(first)
      .getAllByRole("cell")
      .map((c) => c.textContent);
    expect(cells.slice(1, 5)).toEqual(["PRT-000001", "ABL1 kinase", "MLEICLKLVG", "52.3415"]);
    expect(within(first).getByRole("link", { name: "PRT-000001" })).toHaveAttribute(
      "href",
      "/entities/PRT-000001/",
    );
    const call = nth(calls("POST", "/api/v1/entities/search"), 0);
    expect(call.body).toEqual({
      limit: 100,
      exact_type: false,
      project: KINASE.id,
      type: "protein.Protein",
      archived: false,
    });
  });

  it("filters by type and by archived, in the address", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
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
    const sent = calls("POST", "/api/v1/entities/search");
    const last = nth(sent, sent.length - 1).body as Record<string, unknown>;
    expect(last["type"]).toBe("protein.ProteinStructure");
    expect(last).not.toHaveProperty("archived");
  });

  it("picks rows to run an action on", async () => {
    onProject();
    answer(
      "POST /api/v1/entities/search",
      page([entity(), entity({ id: SECOND_ID, hrid: "PRT-000002" })]),
    );
    const { user } = await renderApp("/projects/kinase-atlas/entities/?type=protein.Protein");
    await screen.findByRole("table", { name: "2 proteins" });
    expect(screen.queryByRole("link", { name: "Run action" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: "Select PRT-000002" }));
    expect(screen.getByText("1 selected")).toHaveAttribute("role", "status");
    const all = screen.getByRole<HTMLInputElement>("checkbox", { name: "Select all 2 shown" });
    expect(all.indeterminate).toBe(true);
    await user.click(all);
    expect(all).toBeChecked();
    expect(all.indeterminate).toBe(false);
    expect(screen.getByText("2 selected")).toBeVisible();
    expect(screen.getByRole("link", { name: "Run action" })).toHaveAttribute(
      "href",
      "/projects/kinase-atlas/runs/new/?entities=PRT-000002%2CPRT-000001",
    );

    await user.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(screen.queryByText(/selected/)).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Select PRT-000001" })).not.toBeChecked();

    // A different list starts afresh.
    await user.click(screen.getByRole("checkbox", { name: "Select PRT-000001" }));
    await user.type(screen.getByRole("searchbox", { name: "Search" }), "abl");
    await waitFor(() => {
      expect(screen.queryByText("1 selected")).not.toBeInTheDocument();
    });
  });

  it("says when more are picked than an action runs on at once", async () => {
    onProject();
    const many = Array.from({ length: 101 }, (_, i) =>
      entity({
        id: `00000000-0000-7000-8000-0000000${String(i).padStart(5, "0")}`,
        hrid: `PRT-${String(i + 1).padStart(6, "0")}`,
      }),
    );
    answer("POST /api/v1/entities/search", { items: many, next_cursor: null });
    const { user } = await renderApp("/projects/kinase-atlas/entities/?type=protein.Protein");
    await screen.findByRole("table", { name: "101 proteins" });
    await user.click(screen.getByRole("checkbox", { name: "Select all 101 shown" }));
    expect(
      screen.getByText("101 selected: an action runs on at most 100 at once"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Run action" })).not.toBeInTheDocument();
  });

  it("offers no picking to those who can't run actions in the project", async () => {
    withTypes();
    answer("GET /api/v1/projects", page([project({ can: { ...KINASE.can, run: [] } })]));
    answer("POST /api/v1/entities/search", page([entity()]));
    await renderApp("/projects/kinase-atlas/entities/?type=protein.Protein");
    await screen.findByRole("table", { name: "1 protein" });
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("fetches more as the grid scrolls towards the end", async () => {
    onProject();
    const many = Array.from({ length: 40 }, (_, i) =>
      entity({
        id: `00000000-0000-7000-8000-0000000${String(i).padStart(5, "0")}`,
        hrid: `PRT-${String(i + 1).padStart(6, "0")}`,
      }),
    );
    answer("POST /api/v1/entities/search", ({ body }: { body: unknown }) =>
      (body as { cursor?: string }).cursor
        ? page([entity({ id: "00000000-0000-7000-8000-0000000fffff", hrid: "PRT-000041" })])
        : { items: many, next_cursor: "next" },
    );
    await renderApp("/projects/kinase-atlas/entities/?type=protein.Protein");
    await screen.findByRole("table", { name: "41 proteins" });
    const sent = calls("POST", "/api/v1/entities/search");
    expect(nth(sent, sent.length - 1).body).toMatchObject({ cursor: "next" });
  });

  it("searches by ID or name as people type", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
    const { router, user } = await renderApp(
      "/projects/kinase-atlas/entities/?type=protein.Protein",
    );
    await screen.findByRole("table", { name: "1 protein" });
    await user.type(screen.getByRole("searchbox", { name: "Search" }), "abl");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ type: "protein.Protein", q: "abl" });
    });
    const sent = calls("POST", "/api/v1/entities/search");
    expect(nth(sent, sent.length - 1).body).toMatchObject({ q: "abl" });
    expect(await screen.findByRole("table", { name: "1 protein match" })).toBeInTheDocument();
  });

  it("filters by fields, kept in the address", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
    const { router, user } = await renderApp(
      "/projects/kinase-atlas/entities/?type=protein.Protein",
    );
    await screen.findByRole("table", { name: "1 protein" });
    await user.click(screen.getByRole("button", { name: "Filters" }));
    const panel = screen.getByRole("region", { name: "Filters" });
    await user.click(within(panel).getByRole("button", { name: "Add a filter" }));
    await user.selectOptions(within(panel).getByLabelText("Filter 1: field"), "Molecular weight");
    await user.selectOptions(within(panel).getByLabelText("Filter 1: condition"), "is at least");
    await user.type(within(panel).getByLabelText("Filter 1: value"), "50");
    const want = [{ field: "molecular_weight", op: "gte", value: 50 }];
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ type: "protein.Protein", filters: want });
    });
    const sent = calls("POST", "/api/v1/entities/search");
    expect(nth(sent, sent.length - 1).body).toMatchObject({ where: { all: want } });
    expect(screen.getByRole("button", { name: "Filters (1)" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );

    // What can't be searched for says why, and isn't sent.
    await user.click(within(panel).getByRole("button", { name: "Add a filter" }));
    await user.selectOptions(within(panel).getByLabelText("Filter 2: field"), "Last changed");
    await user.selectOptions(within(panel).getByLabelText("Filter 2: condition"), "is after");
    expect(within(panel).getByLabelText("Filter 2: value")).toBeInTheDocument();
    await user.click(within(panel).getByRole("button", { name: "Remove filter 1" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ type: "protein.Protein" });
    });
  });

  it("opens with the address's filters, and changing type clears them", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
    const filters = encodeURIComponent(
      JSON.stringify([{ field: "name", op: "contains", value: "abl" }]),
    );
    const { router, user } = await renderApp(
      `/projects/kinase-atlas/entities/?type=protein.Protein&filters=${filters}`,
    );
    const panel = await screen.findByRole("region", { name: "Filters" });
    expect(within(panel).getByLabelText("Filter 1: value")).toHaveValue("abl");
    expect(within(panel).getByLabelText("Filter 1: condition")).toHaveDisplayValue("contains");
    await user.selectOptions(screen.getByLabelText("Type"), "Protein structure");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ type: "protein.ProteinStructure" });
    });
  });

  it("sorts by a column's heading", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
    const { router, user } = await renderApp(
      "/projects/kinase-atlas/entities/?type=protein.Protein",
    );
    const grid = await screen.findByRole("table", { name: "1 protein" });
    const heading = within(grid).getByRole("columnheader", { name: /Name/ });
    expect(heading).toHaveAttribute("aria-sort", "none");
    await user.click(within(heading).getByRole("button", { name: "Name" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ type: "protein.Protein", sort: "name" });
    });
    expect(within(grid).getByRole("columnheader", { name: /Name/ })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
    await user.click(within(grid).getByRole("button", { name: "Name" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        type: "protein.Protein",
        sort: "name",
        desc: true,
      });
    });
    const sent = calls("POST", "/api/v1/entities/search");
    expect(nth(sent, sent.length - 1).body).toMatchObject({
      sort: { field: "name", descending: true },
    });
  });

  it("saves a search, and lists saved ones as links to them", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
    const human = {
      id: "00000000-0000-7000-8000-0000000a0001",
      project: KINASE.id,
      name: "Human proteins",
      query: {
        type: "protein.Protein",
        archived: false,
        where: { all: [{ field: "name", op: "contains", value: "abl" }] },
      },
      shared: true,
      created_by: { id: ME.id, name: ME.name },
      created_at: "2026-03-14T09:00:00Z",
      updated_at: "2026-03-14T09:00:00Z",
      can: { change: true, delete: true },
    };
    answer("GET /api/v1/saved-searches", [human]);
    answer("POST /api/v1/saved-searches", respond(201, { ...human, name: "Sorted" }));
    const { router, user } = await renderApp(
      "/projects/kinase-atlas/entities/?type=protein.Protein",
    );
    const saved = await screen.findByRole("navigation", { name: "Saved searches" });
    await user.click(within(saved).getByRole("link", { name: "Human proteins" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        type: "protein.Protein",
        filters: [{ field: "name", op: "contains", value: "abl" }],
      });
    });
    expect(within(saved).getByRole("link", { name: "Human proteins" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    await user.click(screen.getByRole("button", { name: "Save this search" }));
    const dialog = screen.getByRole("alertdialog", { name: "Save this search" });
    await user.type(within(dialog).getByLabelText("Name"), "Sorted");
    await user.click(within(dialog).getByLabelText(/Share with everyone in Kinase Atlas/));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await screen.findByText("Saved “Sorted”.");
    expect(nth(calls("POST", "/api/v1/saved-searches"), 0).body).toEqual({
      project: KINASE.id,
      name: "Sorted",
      shared: true,
      query: {
        exact_type: false,
        type: "protein.Protein",
        archived: false,
        where: { all: [{ field: "name", op: "contains", value: "abl" }] },
      },
    });
  });

  it("says when a search can't be run", async () => {
    onProject();
    answer(
      "POST /api/v1/entities/search",
      problem(422, "search_too_slow", "This search took too long. Narrow it."),
    );
    await renderApp("/projects/kinase-atlas/entities/?type=protein.Protein&q=a");
    expect(await screen.findByText("This search took too long. Narrow it.")).toBeInTheDocument();
  });

  it("says when there's nothing to show", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([]));
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
      structure: revision({ number: 1, via: "mcp", client: "Claude Code (c-81f3)" }),
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
    // An agent's change names the person it acted for and the app it came through.
    expect(await within(values).findByText(/Claude Code/)).toHaveTextContent(
      /^Set by Rosalind Franklin .+ by an AI assistant \(MCP\), through Claude Code \(c-81f3\) \(revision 1\)$/,
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

  it("says where an imported entity came from", async () => {
    withTypes();
    answer("GET /api/v1/entities/PRT-000001", entity());
    answer("GET /api/v1/entities/PRT-000001/provenance", {});
    answer("GET /api/v1/entities/PRT-000001/projects", []);
    const origin = {
      source: "UniProtKB",
      id: "P00519",
      version: "2026_04",
      url: "https://www.uniprot.org/uniprotkb/P00519",
      retrieved_at: "2026-11-11T09:30:00Z",
      sha256: "ab12".repeat(16),
      file: null,
      run: { id: "r1", hrid: "RUN-000007", action: "protein.fetch_uniprot" },
      revision: 1,
      recorded_at: "2026-11-11T09:31:00Z",
    };
    const fromFile = {
      ...origin,
      source: "FASTA file",
      id: "ABL1",
      version: "",
      url: "",
      retrieved_at: null,
      file: { id: FILE_ID, name: "kinases.fasta" },
      run: null,
      revision: null,
    };
    answer("GET /api/v1/entities/PRT-000001/origins", [origin, fromFile]);
    await renderApp("/entities/PRT-000001/");
    const details = screen.getByRole("region", { name: "Details" });
    const record = await within(details).findByRole("link", { name: "P00519 (2026_04)" });
    expect(record).toHaveAttribute("href", "https://www.uniprot.org/uniprotkb/P00519");
    expect(record).toHaveAttribute("rel", "noreferrer noopener");
    const [fetched, read] = within(details).getAllByRole("listitem") as [HTMLElement, HTMLElement];
    expect(fetched).toHaveTextContent(
      /^UniProtKB: P00519 \(2026_04\), retrieved .+, by RUN-000007 · SHA-256 ab12ab12ab12…$/,
    );
    expect(within(fetched).getByRole("link", { name: "RUN-000007" })).toHaveAttribute(
      "href",
      "/runs/RUN-000007/",
    );
    // A run the reader can't see isn't named; a record found again says so.
    expect(read).toHaveTextContent(
      /^FASTA file: ABL1 in kinases.fasta \(found again: it was already here\)/,
    );
    expect(within(read).getByRole("link", { name: "kinases.fasta" })).toHaveAttribute(
      "href",
      `/api/v1/files/${FILE_ID}/content`,
    );
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
