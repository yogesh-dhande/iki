/**
 * The type browser, a project's entity grid and an entity's page, against a
 * fake API.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import {
  addFilter,
  ALICE,
  entity,
  field,
  fieldHistory,
  FILE_ID,
  KINASE,
  nth,
  PROTEIN,
  PROTEIN_ID,
  renderApp,
  revision,
  withTypes,
} from "./entity-fixtures";
import { formatNumber, quantity } from "./entities";
import { MAX_FILTERS } from "./filters";
import { MAX_BATCH } from "./runs";
import { answer, calls, ME, page, problem, project, respond, signedIn } from "./test-server";

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
    expect(screen.getByRole("heading", { level: 2, name: "iki/protein" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Types" })).toHaveAttribute("aria-current", "page");
    await user.click(
      within(screen.getByRole("main")).getByRole("link", { name: "Protein structure" }),
    );
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

  it("says what makes two the same, and where each field is kept", async () => {
    withTypes();
    await renderApp("/types/iki/protein.Protein/");
    expect(
      screen.getByText(
        /Two are the same when their Sequence match\. Each is registered once in a registry, with one ID/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Shown in ku")).toBeInTheDocument();
    // Its registry entry's fields, and each project's own (ADR 0115).
    const rows = within(screen.getByRole("table")).getAllByRole("row");
    expect(within(nth(rows, 0)).getAllByRole("columnheader").at(-1)).toHaveTextContent("Kept in");
    const kept = rows.slice(1).map((r) => r.querySelector("td:last-child .badge")?.textContent);
    expect(kept).toEqual(["Project record", "Identity", "Registry: derived"]);
    expect(screen.queryByText(/duplicate/i)).not.toBeInTheDocument();
  });

  it("previews whom a type's notifications tell, and when", async () => {
    withTypes();
    answer("GET /api/v1/entity-types", [
      {
        ...PROTEIN,
        notifications: [
          {
            name: "renamed",
            kind: "iki/protein.Protein.renamed",
            label: "Renamed",
            description: "Its owner hears when it's renamed.",
            declared_by: "iki/protein.Protein",
            when: "When its Name changes",
            only_some: true,
            tells: ["Whoever its Owner names", "Everyone with the QA role in its project"],
            email: false,
            required: true,
          },
        ],
      },
    ]);
    await renderApp("/types/iki/protein.Protein/");
    const section = screen.getByRole("list", { name: "Its notifications" });
    const row = within(section).getByRole("listitem");
    expect(row).toHaveTextContent("Renamed");
    expect(row).toHaveTextContent(
      "When its Name changes, for those its conditions find. Tells whoever its Owner names and everyone with the QA role in its project.",
    );
    expect(within(row).getByText("Required")).toBeVisible();
    expect(within(row).getByText("In Iki")).toBeVisible();
    expect(screen.getByRole("link", { name: "your notification settings" })).toHaveAttribute(
      "href",
      "/account/notifications/",
    );
  });

  it("says when there's no such type", async () => {
    withTypes();
    await renderApp("/types/acme/nope.Nope/");
    expect(
      screen.getByText("There’s nothing here, or you don’t have access to it."),
    ).toBeInTheDocument();
  });
});

describe("a project's entities", () => {
  function onProject(): void {
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/entities/counts", {
      types: [
        { type: "iki/protein.Protein", label: "Protein", count: 4 },
        { type: "iki/protein.ProteinStructure", label: "Protein structure", count: 1 },
      ],
      total: 5,
    });
  }

  it("shows one type's fields as columns, in their display units", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
    await renderApp("/projects/kinase-atlas/entities/?type=iki/protein.Protein");
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
      type: "iki/protein.Protein",
      archived: false,
    });
  });

  it("filters by type and by archived, in the address", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
    const { router, user } = await renderApp("/projects/kinase-atlas/entities/");
    await screen.findByRole("table", { name: "1 entity" });
    expect(screen.getByRole("link", { name: /^Entities\b/ })).toHaveAttribute(
      "aria-current",
      "page",
    );
    // The same pills as All entities (ADR 0176), counting the project's records.
    const pills = screen.getByRole("group", { name: "Type" });
    expect(
      await within(pills).findByRole("button", { name: "All types 5 records" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("combobox", { name: "Type" })).not.toBeInTheDocument();
    expect(calls("GET", "/api/v1/entities/counts").at(-1)?.query.get("project")).toBe(KINASE.id);
    await user.click(within(pills).getByRole("button", { name: "Protein structure 1 record" }));
    await user.selectOptions(screen.getByLabelText("Show"), "Current and archived");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        type: "iki/protein.ProteinStructure",
        show: "all",
      });
    });
    const sent = calls("POST", "/api/v1/entities/search");
    const last = nth(sent, sent.length - 1).body as Record<string, unknown>;
    expect(last["type"]).toBe("iki/protein.ProteinStructure");
    expect(last).not.toHaveProperty("archived");
  });

  it("downloads everything its search finds as CSV, from a link the keyboard reaches", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
    const { user } = await renderApp(
      "/projects/kinase-atlas/entities/?type=iki/protein.Protein&sort=name&desc=true" +
        `&filters=${encodeURIComponent(JSON.stringify([{ field: "sequence", op: "motif", value: "NXS" }]))}`,
    );
    await screen.findByRole("table", { name: "1 protein match" });
    const link = screen.getByRole("link", { name: "Download CSV" });
    // Its description is the grid's count, so a screen reader hears what it downloads.
    expect(link).toHaveAccessibleDescription("1 protein match");
    expect(link).toHaveAttribute("download");
    const address = new URL(link.getAttribute("href") ?? "", "https://iki.example.org");
    expect(address.pathname).toBe("/api/v1/entities/search.csv");
    // The columns the grid shows, in its order, then what a re-upload needs (ADR 0175).
    const columns = address.searchParams.getAll("columns");
    expect(columns.slice(0, 3)).toEqual(["hrid", "name", "sequence"]);
    expect(columns.slice(-3)).toEqual(["updated_at", "revision", "id"]);
    address.searchParams.delete("columns");
    expect(Object.fromEntries(address.searchParams)).toEqual({
      type: "iki/protein.Protein",
      project: KINASE.id,
      archived: "false",
      where: JSON.stringify({ all: [{ field: "sequence", op: "motif", value: "NXS" }] }),
      sort: "name",
      descending: "true",
    });
    // Tab reaches it from the search box, before the grid's own rows.
    screen.getByRole("searchbox", { name: "Search" }).focus();
    for (let presses = 0; presses < 10 && document.activeElement !== link; presses += 1) {
      await user.tab();
    }
    expect(link).toHaveFocus();
  });

  it("picks rows to run an action on", async () => {
    onProject();
    answer(
      "POST /api/v1/entities/search",
      page([entity(), entity({ id: SECOND_ID, hrid: "PRT-000002" })]),
    );
    const { user } = await renderApp("/projects/kinase-atlas/entities/?type=iki/protein.Protein");
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
    const many = Array.from({ length: MAX_BATCH + 1 }, (_, i) =>
      entity({
        id: `00000000-0000-7000-8000-0000000${String(i).padStart(5, "0")}`,
        hrid: `PRT-${String(i + 1).padStart(6, "0")}`,
      }),
    );
    answer("POST /api/v1/entities/search", { items: many, next_cursor: null });
    const { user } = await renderApp("/projects/kinase-atlas/entities/?type=iki/protein.Protein");
    await screen.findByRole("table", { name: "201 proteins" });
    await user.click(screen.getByRole("checkbox", { name: "Select all 201 shown" }));
    expect(
      screen.getByText("201 selected: an action runs on at most 200 at once"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Run action" })).not.toBeInTheDocument();
  });

  it("offers no picking to those who can't run actions in the project", async () => {
    withTypes();
    answer("GET /api/v1/projects", page([project({ can: { ...KINASE.can, run: [] } })]));
    answer("POST /api/v1/entities/search", page([entity()]));
    await renderApp("/projects/kinase-atlas/entities/?type=iki/protein.Protein");
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
    await renderApp("/projects/kinase-atlas/entities/?type=iki/protein.Protein");
    await screen.findByRole("table", { name: "41 proteins" });
    const sent = calls("POST", "/api/v1/entities/search");
    expect(nth(sent, sent.length - 1).body).toMatchObject({ cursor: "next" });
  });

  it("searches by ID or name as people type", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
    const { router, user } = await renderApp(
      "/projects/kinase-atlas/entities/?type=iki/protein.Protein",
    );
    await screen.findByRole("table", { name: "1 protein" });
    await user.type(screen.getByRole("searchbox", { name: "Search" }), "abl");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ type: "iki/protein.Protein", q: "abl" });
    });
    const sent = calls("POST", "/api/v1/entities/search");
    expect(nth(sent, sent.length - 1).body).toMatchObject({ q: "abl" });
    expect(await screen.findByRole("table", { name: "1 protein match" })).toBeInTheDocument();
  });

  it("filters by fields, kept in the address", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
    const { router, user } = await renderApp(
      "/projects/kinase-atlas/entities/?type=iki/protein.Protein",
    );
    await screen.findByRole("table", { name: "1 protein" });
    const editor = await addFilter(user, "Molecular weight");
    await user.selectOptions(within(editor).getByLabelText("Filter 1: condition"), "is at least");
    await user.type(within(editor).getByLabelText("Filter 1: value"), "50");
    const want = [{ field: "molecular_weight", op: "gte", value: 50 }];
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ type: "iki/protein.Protein", filters: want });
    });
    const sent = calls("POST", "/api/v1/entities/search");
    expect(nth(sent, sent.length - 1).body).toMatchObject({ where: { all: want } });
    await user.click(within(editor).getByRole("button", { name: "Done" }));
    const chips = screen.getByRole("group", { name: "Filters" });
    expect(
      within(chips).getByRole("button", {
        name: /^Filter 1: Molecular weight is at least 50/,
      }),
    ).toBeVisible();

    // What can't be searched for yet isn't sent.
    const second = await addFilter(user, "Last changed");
    await user.selectOptions(within(second).getByLabelText("Filter 2: condition"), "is after");
    expect(within(second).getByLabelText("Filter 2: value")).toBeInTheDocument();
    await user.click(within(second).getByRole("button", { name: "Done" }));
    await user.click(within(chips).getByRole("button", { name: /^Remove filter 1:/ }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ type: "iki/protein.Protein" });
    });
  });

  it("opens with the address's filters, and changing type clears them", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
    const filters = encodeURIComponent(
      JSON.stringify([{ field: "name", op: "contains", value: "abl" }]),
    );
    const { router, user } = await renderApp(
      `/projects/kinase-atlas/entities/?type=iki/protein.Protein&filters=${filters}`,
    );
    const chip = await screen.findByRole("button", { name: "Filter 1: Name contains abl" });
    await user.click(chip);
    const editor = await screen.findByRole("dialog");
    expect(within(editor).getByLabelText("Filter 1: value")).toHaveValue("abl");
    expect(within(editor).getByLabelText("Filter 1: condition")).toHaveDisplayValue("contains");
    await user.click(within(editor).getByRole("button", { name: "Done" }));
    await user.click(
      within(screen.getByRole("group", { name: "Type" })).getByRole("button", {
        name: "Protein structure 1 record",
      }),
    );
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ type: "iki/protein.ProteinStructure" });
    });
  });

  it("sorts by a column's heading", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
    const { router, user } = await renderApp(
      "/projects/kinase-atlas/entities/?type=iki/protein.Protein",
    );
    const grid = await screen.findByRole("table", { name: "1 protein" });
    const heading = within(grid).getByRole("columnheader", { name: /Name/ });
    expect(heading).toHaveAttribute("aria-sort", "none");
    await user.click(within(heading).getByRole("button", { name: "Name" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ type: "iki/protein.Protein", sort: "name" });
    });
    expect(within(grid).getByRole("columnheader", { name: /Name/ })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
    await user.click(within(grid).getByRole("button", { name: "Name" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        type: "iki/protein.Protein",
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
        type: "iki/protein.Protein",
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
      "/projects/kinase-atlas/entities/?type=iki/protein.Protein",
    );
    const saved = await screen.findByRole("navigation", { name: "Saved searches" });
    await user.click(within(saved).getByRole("link", { name: "Human proteins" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        type: "iki/protein.Protein",
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
        type: "iki/protein.Protein",
        archived: false,
        where: { all: [{ field: "name", op: "contains", value: "abl" }] },
      },
    });
  });

  it("saves the search the grid runs, its words as conditions, and opens it as it was", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
    const lineage = {
      exact_type: false,
      type: "iki/protein.Protein",
      archived: false,
      q: "abl",
      where: {
        all: [
          { field: "name", op: "contains", value: "kinase" },
          { all: [{ field: "downstream", op: "of", value: "DNR-000001" }] },
        ],
      },
    };
    const saved = (name: string, query: unknown) => ({
      id: `00000000-0000-7000-8000-0000000a000${String(name.length % 10)}`,
      project: KINASE.id,
      name,
      query,
      shared: false,
      created_by: { id: ME.id, name: ME.name },
      created_at: "2026-03-14T09:00:00Z",
      updated_at: "2026-03-14T09:00:00Z",
      can: { change: true, delete: true },
    });
    answer("GET /api/v1/saved-searches", [saved("From the donor", lineage)]);
    answer("POST /api/v1/saved-searches", respond(201, saved("Kinases", lineage)));
    const filters = encodeURIComponent(
      JSON.stringify([{ field: "name", op: "contains", value: "kinase" }]),
    );
    const { router, user } = await renderApp(
      `/projects/kinase-atlas/entities/?type=iki/protein.Protein&q=downstream:DNR-000001%20abl&filters=${filters}`,
    );
    await screen.findByRole("navigation", { name: "Saved searches" });
    await user.click(screen.getByRole("button", { name: "Save this search" }));
    const dialog = screen.getByRole("alertdialog", { name: "Save this search" });
    await user.type(within(dialog).getByLabelText("Name"), "Kinases");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await screen.findByText("Saved “Kinases”.");
    // What it keeps is what the grid ran, which the API runs the same.
    const kept = nth(calls("POST", "/api/v1/saved-searches"), 0).body as { query: unknown };
    expect(kept.query).toEqual(lineage);
    const ran = calls("POST", "/api/v1/entities/search").at(-1)?.body;
    expect(ran).toEqual({ ...lineage, limit: 100, project: KINASE.id });

    await user.click(
      within(screen.getByRole("main")).getByRole("link", { name: "From the donor" }),
    );
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        type: "iki/protein.Protein",
        q: "abl downstream:DNR-000001",
        filters: [{ field: "name", op: "contains", value: "kinase" }],
      });
    });
  });

  it("doesn't open a saved search the grid would run as another", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
    const made = (name: string, query: unknown) => ({
      id: `00000000-0000-7000-8000-0000000a00${String(name.length).padStart(2, "0")}`,
      project: KINASE.id,
      name,
      query,
      shared: true,
      created_by: { id: ME.id, name: ME.name },
      created_at: "2026-03-14T09:00:00Z",
      updated_at: "2026-03-14T09:00:00Z",
      can: { change: false, delete: false },
    });
    answer("GET /api/v1/saved-searches", [
      // Sequences, and not their subtypes: the grid shows a type's subtypes too.
      made("Plain sequences", { type: "iki/core.Sequence", exact_type: true }),
      // Text that the search box would read as a lineage condition.
      made("Notes naming a donor", { exact_type: false, q: "downstream:DNR-000001" }),
      made("Lineage", {
        exact_type: false,
        where: { all: [{ all: [{ field: "downstream", op: "of", value: "DNR-000001" }] }] },
      }),
    ]);
    await renderApp("/projects/kinase-atlas/entities/?type=iki/protein.Protein");
    const saved = await screen.findByRole("navigation", { name: "Saved searches" });
    expect(within(saved).queryByRole("link", { name: "Plain sequences" })).toBeNull();
    expect(within(saved).getByText("Plain sequences")).toHaveAttribute(
      "title",
      "Made with the API: the grid can’t show its conditions.",
    );
    expect(within(saved).queryByRole("link", { name: "Notes naming a donor" })).toBeNull();
    expect(within(saved).getByRole("link", { name: "Lineage" })).toHaveAttribute(
      "href",
      expect.stringContaining("q=downstream%3ADNR-000001"),
    );
  });

  it("opens a saved search as the database keeps it, and not one the grid would cut short", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
    const made = (name: string, query: unknown) => ({
      id: `00000000-0000-7000-8000-0000000b00${String(name.length).padStart(2, "0")}`,
      project: KINASE.id,
      name,
      query,
      shared: true,
      created_by: { id: ME.id, name: ME.name },
      created_at: "2026-03-14T09:00:00Z",
      updated_at: "2026-03-14T09:00:00Z",
      can: { change: false, delete: false },
    });
    // PostgreSQL's jsonb keeps an object's keys shorter first, not as they were written:
    // https://www.postgresql.org/docs/current/datatype-json.html
    const stored = {
      type: "iki/protein.Protein",
      where: {
        all: [
          { op: "contains", field: "name", value: "kinase" },
          {
            all: [
              { op: "of", field: "downstream", value: "DNR-000001" },
              { op: "has", field: "components", value: { part: "PRT-000001", role: "cargo" } },
            ],
          },
        ],
      },
      archived: false,
      exact_type: false,
    };
    // The API takes up to 50 conditions; the grid's filters stop at 20.
    const many = Array.from({ length: MAX_FILTERS + 1 }, (_, i) => ({
      field: "name",
      op: "contains",
      value: `part${String(i + 1)}`,
    }));
    answer("GET /api/v1/saved-searches", [
      made("Kinases from the donor", stored),
      made("Twenty-one names", { exact_type: false, where: { all: many } }),
      made("Long text", { exact_type: false, q: "kinase ".repeat(40).trim() }),
    ]);
    const { router, user } = await renderApp(
      "/projects/kinase-atlas/entities/?type=iki/protein.Protein",
    );
    const saved = await screen.findByRole("navigation", { name: "Saved searches" });
    expect(within(saved).queryByRole("link", { name: "Twenty-one names" })).toBeNull();
    expect(within(saved).getByText("Twenty-one names")).toHaveAttribute(
      "title",
      "Made with the API: the grid can’t show its conditions.",
    );
    expect(within(saved).queryByRole("link", { name: "Long text" })).toBeNull();
    await user.click(within(saved).getByRole("link", { name: "Kinases from the donor" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        type: "iki/protein.Protein",
        q: "downstream:DNR-000001 components.part:PRT-000001 components.role:cargo",
        filters: [{ field: "name", op: "contains", value: "kinase" }],
      });
    });
  });

  it("says when a search can't be run", async () => {
    onProject();
    answer(
      "POST /api/v1/entities/search",
      problem(422, "search_too_slow", "This search took too long. Narrow it."),
    );
    await renderApp("/projects/kinase-atlas/entities/?type=iki/protein.Protein&q=a");
    expect(await screen.findByText("This search took too long. Narrow it.")).toBeInTheDocument();
  });

  it("says when there's nothing to show", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([]));
    await renderApp("/projects/kinase-atlas/entities/?type=iki/protein.Protein");
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
        type: "iki/protein.ProteinStructure",
        values: { structure: FILE_ID, resolution: 2.1, chains: [PROTEIN_ID, "gone"] },
        refs: {
          [PROTEIN_ID]: {
            hrid: "PRT-000001",
            title: "ABL1 kinase",
            type: "iki/protein.Protein",
            archived: false,
            purged: false,
          },
        },
        files: { [FILE_ID]: { name: "1abc.pdb", media_type: "chemical/x-pdb", size: 52_428_800 } },
      }),
    );
    answer("GET /api/v1/entities/PST-000001/provenance", {
      structure: revision({
        number: 1,
        via: "mcp",
        client: "Claude Code (c-81f3)",
        agent: {
          client_id: "c-81f3",
          name: "Claude Code",
          grant: null,
          mcp_session: null,
          reported: { software: null, model: null },
        },
      }),
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

  it("shows each value, and who set it, how and when on asking", async () => {
    onStructure();
    const { user } = await renderApp("/entities/PST-000001/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("PST-000001");
    const values = screen.getByRole("region", { name: "Values" });
    const file = within(values).getByRole("link", { name: "1abc.pdb" });
    expect(file).toHaveAttribute("href", `/api/v1/files/${FILE_ID}/content`);
    expect(within(values).getByText("50 MiB · chemical/x-pdb")).toBeInTheDocument();
    expect(within(values).getByText("2.1 Å")).toBeInTheDocument();
    // A protein is a registry entry's record (ADR 0115): the link opens this project's.
    expect(within(values).getByRole("link", { name: "ABL1 kinase" })).toHaveAttribute(
      "href",
      "/entities/PRT-000001/?project=kinase-atlas",
    );
    expect(within(values).getByText("An entity you can’t read")).toBeInTheDocument();
    // No value says who set it (ADR 0176): its history does, on asking.
    expect(values).not.toHaveTextContent(/Rosalind Franklin/);
    // An agent's change names the person it acted for and the app it came through.
    const structure = await fieldHistory(user, "Structure");
    expect(structure).toHaveTextContent(
      /^Last change to StructureWhoRosalind FranklinHowby an AI assistant \(MCP\), through Claude CodeWhen\d+ \w+ \d{4}, \d\d:\d\d UTCRevision1/,
    );
    expect(
      within(structure).getByRole("link", { name: "Structure in the audit trail" }),
    ).toHaveAttribute("href", "/entities/PST-000001/audit/?field=structure");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "History of Structure" })).toHaveFocus();
    });
    const resolution = await fieldHistory(user, "Resolution");
    expect(resolution).toHaveTextContent("Howin the web app");
    expect(resolution).toHaveTextContent("“Refined”");
    await user.keyboard("{Escape}");
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
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/^ABL1 kinase$/);
    // Its ID, beside its title and in its breadcrumb.
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(crumbs).getByText("PRT-000001")).toHaveAttribute("aria-current", "page");
    expect(within(crumbs).getByRole("link", { name: "Entities" })).toHaveAttribute(
      "href",
      "/entities/",
    );
    expect(within(crumbs).getByRole("link", { name: "Proteins" })).toHaveAttribute(
      "href",
      "/entities/types/iki/protein.Protein/",
    );
    expect(screen.getByText("52.3415 ku")).toBeInTheDocument();
    expect(screen.getByText("(52,341.5 u)")).toBeInTheDocument();
  });

  it("shows a short JSON value, and folds a long one", async () => {
    withTypes();
    const json = (name: string, label: string) => field({ name, label, type: "json" });
    answer("GET /api/v1/entity-types", [
      {
        ...PROTEIN,
        fields: [...PROTEIN.fields, json("ends", "Ends"), json("positions", "Positions")],
      },
    ]);
    const nucleotides = Array.from({ length: 12 }, (_, i) => ({ position: i + 1, base: "A" }));
    answer(
      "GET /api/v1/entities/PRT-000001",
      entity({
        values: {
          name: "ABL1 kinase",
          sequence: "MKV",
          ends: { five: "FAM" },
          positions: { nucleotides },
        },
      }),
    );
    answer("GET /api/v1/entities/PRT-000001/provenance", {});
    answer("GET /api/v1/entities/PRT-000001/projects", []);
    const { user } = await renderApp("/entities/PRT-000001/");
    expect(screen.getByText(/"five": "FAM"/)).toBeVisible();
    const folded = screen.getByText("Show its JSON (52 lines)");
    expect(screen.getByText(/"position": 12/)).not.toBeVisible();
    await user.click(folded);
    expect(screen.getByText(/"position": 12/)).toBeVisible();
  });

  it("links an external ID to its record in the other system", async () => {
    withTypes();
    const lims = field({
      name: "lims_id",
      label: "LIMS ID",
      type: "string",
      unique: true,
      link: "https://lims.example.org/proteins/{value}",
    });
    answer("GET /api/v1/entity-types", [{ ...PROTEIN, fields: [...PROTEIN.fields, lims] }]);
    answer(
      "GET /api/v1/entities/PRT-000001",
      entity({ values: { name: "ABL1 kinase", sequence: "MKV", lims_id: "P 42/7" } }),
    );
    answer("GET /api/v1/entities/PRT-000001/provenance", {});
    answer("GET /api/v1/entities/PRT-000001/projects", []);
    await renderApp("/entities/PRT-000001/");
    const values = screen.getByRole("region", { name: "Values" });
    const record = within(values).getByRole("link", { name: "P 42/7" });
    expect(record).toHaveAttribute("href", "https://lims.example.org/proteins/P%2042%2F7");
    expect(record).toHaveAttribute("rel", "noreferrer noopener");
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
      run: { id: "r1", hrid: "RUN-000007", action: "iki/protein.fetch_uniprot" },
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

  it("says how it was made, and who says so", async () => {
    withTypes();
    answer("GET /api/v1/entities/PRT-000001", entity());
    answer("GET /api/v1/entities/PRT-000001/provenance", {});
    answer("GET /api/v1/entities/PRT-000001/projects", []);
    const run = {
      id: "r1",
      hrid: "RUN-000009",
      action: "iki/antibody.predict_structure_with_immunebuilder",
    };
    const recorded = {
      by: "iki",
      kind: "computational",
      method: "",
      software: "ImmuneBuilder",
      version: "1.2",
      run,
      source: null,
      recorded_at: "2026-11-16T09:31:00Z",
    };
    const stated = {
      ...recorded,
      by: "source",
      software: "AlphaFold",
      version: "2.3.2",
      run: null,
      source: {
        source: "AlphaFold DB",
        id: "AF-P69905-F1",
        version: "4",
        url: "https://alphafold.ebi.ac.uk/entry/P69905",
      },
    };
    const measured = {
      ...stated,
      kind: "experimental",
      method: "X-RAY DIFFRACTION",
      software: "",
      version: "",
      source: { source: "RCSB PDB", id: "4HHB", version: "", url: "" },
    };
    answer("GET /api/v1/entities/PRT-000001/making", [
      recorded,
      { ...recorded, software: "OpenMM", version: "8.6.1" },
      stated,
      measured,
    ]);
    await renderApp("/entities/PRT-000001/");
    const details = screen.getByRole("region", { name: "Details" });
    await within(details).findByText("How it was made");
    const [iki, alphafold, pdb] = within(details).getAllByRole("listitem") as [
      HTMLElement,
      HTMLElement,
      HTMLElement,
    ];
    // One line per run, its tools together.
    expect(iki).toHaveTextContent(
      "Computed with ImmuneBuilder 1.2, OpenMM 8.6.1, by RUN-000009 (iki/antibody.predict_structure_with_immunebuilder)Recorded by Iki",
    );
    expect(within(iki).getByRole("link", { name: "RUN-000009" })).toHaveAttribute(
      "href",
      "/runs/RUN-000009/",
    );
    // A source's word is said to be its own.
    expect(alphafold).toHaveTextContent(
      "Computed: AlphaFold 2.3.2, as AlphaFold DB AF-P69905-F1 (4) statesStated by its source",
    );
    expect(within(alphafold).getByRole("link", { name: "AF-P69905-F1 (4)" })).toHaveAttribute(
      "href",
      "https://alphafold.ebi.ac.uk/entry/P69905",
    );
    expect(pdb).toHaveTextContent("Experimental: X-RAY DIFFRACTION, as RCSB PDB 4HHB states");
  });

  it("says which bundle from another deployment it came in", async () => {
    withTypes();
    answer("GET /api/v1/entities/PRT-000001", entity());
    answer("GET /api/v1/entities/PRT-000001/provenance", {});
    answer("GET /api/v1/entities/PRT-000001/projects", []);
    answer("GET /api/v1/entities/PRT-000001/bundles", [
      {
        deployment: "staging",
        project: "Kinase Atlas",
        hrid: "PRT-000012",
        action: "new",
        revisions: 2,
        applied_by: { id: "u1", name: "Ada Lovelace" },
        applied_at: "2026-11-15T11:00:00Z",
        reason: "Moving the kinase work",
        bundle_import: "00000000-0000-7000-8000-0000000000i1",
        project_slug: "kinase-atlas",
      },
    ]);
    await renderApp("/entities/PRT-000001/");
    const details = screen.getByRole("region", { name: "Details" });
    const link = await within(details).findByRole("link", { name: "the import" });
    expect(link).toHaveAttribute(
      "href",
      "/projects/kinase-atlas/imports/00000000-0000-7000-8000-0000000000i1/",
    );
    expect(within(details).getByRole("listitem")).toHaveTextContent(
      /^staging’s Kinase Atlas, where it’s PRT-000012: 2 revisions, imported .+ by Ada Lovelace \(the import\)$/,
    );
  });

  it("shows its audit trail, and what each revision changed", async () => {
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
    const { user } = await renderApp("/entities/PST-000001/audit/");
    expect(await screen.findByText("Revision 2: Edited by Rosalind Franklin")).toBeInTheDocument();
    expect(screen.getByText("Revision 1: Created by Rosalind Franklin")).toBeInTheDocument();
    // Its tab counts the revisions, in words to screen readers.
    const tab = screen.getByRole("link", { name: /^Audit trail,\s*2\s*revisions$/ });
    expect(tab).toHaveAttribute("aria-current", "page");
    expect(screen.getByText("“Refined”")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Export for inspection/ })).toHaveAttribute(
      "href",
      "/api/v1/entities/00000000-0000-7000-8000-00000000e002/revisions.csv",
    );
    await user.click(screen.getByRole("button", { name: "Show changes in revision 2" }));
    const table = await screen.findByRole("table");
    expect(
      within(table)
        .getAllByRole("row")
        .map((r) => r.textContent),
    ).toEqual(["FieldBeforeAfter", "Resolution2.4 Å2.1 Å"]);
  });

  it("says what a field is, as its type describes it, on pressing beside its label", async () => {
    signedIn();
    const described: Schemas["EntityTypeOut"] = {
      ...PROTEIN,
      fields: PROTEIN.fields.map((f) =>
        f.name === "molecular_weight"
          ? { ...f, description: "Its average molecular weight, as the free acid." }
          : f,
      ),
    };
    answer("GET /api/v1/entity-types", [described]);
    answer("GET /api/v1/components", []);
    answer("GET /api/v1/entities/PRT-000001", entity());
    answer("GET /api/v1/entities/PRT-000001/provenance", {});
    answer("GET /api/v1/entities/PRT-000001/projects", []);
    const { user } = await renderApp("/entities/PRT-000001/");
    const values = await screen.findByRole("region", { name: "Values" });
    const about = within(values).getByRole("button", { name: "About Molecular weight" });
    expect(about).toHaveAttribute("aria-expanded", "false");
    expect(within(values).queryByText(/as the free acid/)).not.toBeVisible();
    // Fields without a description have none to open.
    expect(within(values).queryByRole("button", { name: "About Name" })).toBeNull();
    await user.click(about);
    expect(about).toHaveAttribute("aria-expanded", "true");
    expect(
      within(values).getByText("Its average molecular weight, as the free acid."),
    ).toBeVisible();
    await user.click(about);
    expect(within(values).queryByText(/as the free acid/)).not.toBeVisible();
  });

  it("cuts a long value short, with all of it on asking and in its viewer", async () => {
    signedIn();
    const viewed: Schemas["EntityTypeOut"] = {
      ...PROTEIN,
      viewers: [
        {
          viewer: "sequence",
          label: "Sequence",
          sequence: "sequence",
          alphabet: "protein",
          strands: null,
          topology: null,
          features: null,
          tracks: [],
        },
      ],
    };
    answer("GET /api/v1/entity-types", [viewed]);
    answer("GET /api/v1/components", []);
    // Titin's first 60 residues (UniProt Q8WZ42), seven times: longer than a page shows.
    const sequence = "MTTQAPTFTQPLQSVVVLEGSTATFEAHISGFPVPEVSWFRDGQVISTSTLPGVQISFSD".repeat(7);
    answer("GET /api/v1/entities/PRT-000001", entity({ values: { name: "Titin", sequence } }));
    answer("GET /api/v1/entities/PRT-000001/provenance", {});
    answer("GET /api/v1/entities/PRT-000001/projects", []);
    const { user } = await renderApp("/entities/PRT-000001/");
    const values = await screen.findByRole("region", { name: "Values" });
    const shown = within(values).getByText(/^MTTQAPTFTQ.+…$/);
    expect(shown.textContent).toHaveLength(201);
    expect(within(values).getByRole("link", { name: "Open in Sequence" })).toHaveAttribute(
      "href",
      "/entities/PRT-000001/viewers/sequence/",
    );
    await user.click(within(values).getByRole("button", { name: "Show all of Sequence" }));
    expect(within(values).getByText(sequence)).toBeInTheDocument();
    expect(within(values).getByRole("button", { name: "Show less of Sequence" })).toBeVisible();
    expect(within(values).getByText(/^420 residues\./)).toBeInTheDocument();
  });

  it("keeps each hold a revision opened, and who reviewed it, why and when", async () => {
    onStructure();
    const hold = {
      id: "00000000-0000-7000-8000-0000000000h1",
      rule: "purity_in_specification",
      label: "Purity in specification",
      message: "Purity must be at least 95%.",
      held_by: ALICE,
      agent: null,
      held_at: "2026-03-14T09:00:00Z",
      reason: "Kept: the measurement is valid.",
      revision: 1,
      last_revision: 2,
      reviewer: null,
      reviewed_at: null,
      review_reason: "",
    };
    answer(
      "GET /api/v1/entities/PST-000001/revisions",
      page([
        revision({ number: 3, kind: "edited", changed_fields: ["resolution"] }),
        revision({
          number: 2,
          kind: "edited",
          changed_fields: ["resolution"],
          holds: [
            {
              ...hold,
              id: "00000000-0000-7000-8000-0000000000h2",
              revision: 2,
              last_revision: 2,
              reason: "Retested.",
            },
          ],
        }),
        revision({
          number: 1,
          changed_fields: ["structure", "resolution"],
          holds: [
            {
              ...hold,
              reviewer: { ...ALICE, name: "Grace Hopper", email: "grace@example.com" },
              reviewed_at: "2026-03-15T09:00:00Z",
              review_reason: "SEC rerun within limits.",
            },
          ],
        }),
      ]),
    );
    await renderApp("/entities/PST-000001/audit/");
    await screen.findByText("Revision 1: Created by Rosalind Franklin");
    const first = screen.getByRole("list", { name: "Holds revision 1 opened" });
    expect(within(first).getByRole("listitem")).toHaveTextContent(
      /^Held for review: Purity in specification\. Purity must be at least 95%\. Saved anyway: “Kept: the measurement is valid\.” Revision 2 broke it again\. Reviewed by Grace Hopper .+: “SEC rerun within limits\.”$/,
    );
    const second = screen.getByRole("list", { name: "Holds revision 2 opened" });
    expect(within(second).getByRole("listitem")).toHaveTextContent(/Still held, for review\.$/);
    expect(screen.queryByRole("list", { name: "Holds revision 3 opened" })).toBeNull();
  });

  it("watches a record, or mutes it", async () => {
    onStructure();
    const watch = "/api/v1/entities/00000000-0000-7000-8000-00000000e002/watch";
    answer(`GET ${watch}`, { mode: "default", can_watch: true, can_mute: true });
    answer(`PUT ${watch}`, { mode: "muted", can_watch: true, can_mute: true });
    const { user } = await renderApp("/entities/PST-000001/");
    await user.click(
      await screen.findByRole("button", { name: "Watch: notifications about PST-000001" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Notifications about PST-000001",
    });
    expect(within(dialog).getByRole("radio", { name: /^As it names me/ })).toBeChecked();
    await user.click(within(dialog).getByRole("radio", { name: /^Muted/ }));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("You’ve muted it.")).toBeVisible();
    expect(calls("PUT", watch)[0]?.body).toEqual({ mode: "muted" });
    expect(
      screen.getByRole("button", { name: "Muted: notifications about PST-000001" }),
    ).toBeVisible();
  });

  it("offers no watching where a type tells nobody", async () => {
    onStructure();
    answer("GET /api/v1/entities/00000000-0000-7000-8000-00000000e002/watch", {
      mode: "default",
      can_watch: false,
      can_mute: false,
    });
    await renderApp("/entities/PST-000001/");
    await screen.findByRole("heading", { level: 1 });
    await waitFor(() => {
      expect(
        calls("GET", "/api/v1/entities/00000000-0000-7000-8000-00000000e002/watch"),
      ).toHaveLength(1);
    });
    expect(screen.queryByRole("button", { name: /notifications about/ })).toBeNull();
  });

  it("says how a record's identity was given, where its entry spells it otherwise", async () => {
    // ADR 0138: a protein given with its stop is the entry's protein, and keeps what was given.
    withTypes();
    answer("GET /api/v1/entities/PRT-000001", entity());
    answer(
      "GET /api/v1/entities/PRT-000001/revisions",
      page([revision({ number: 1, submitted: { sequence: "mkwv*" } })]),
    );
    await renderApp("/entities/PRT-000001/audit/");
    await screen.findByText("Revision 1: Created by Rosalind Franklin");
    const given = document.querySelector(".row-submitted");
    expect(given).toHaveTextContent(
      "Given as Sequence: mkwv*, which its registry entry spells another way.",
    );
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

describe("numbers", () => {
  it("shows small nonzero values in scientific notation, not as 0", () => {
    expect(formatNumber(0)).toBe("0");
    expect(formatNumber(1e-21)).toBe("1 × 10⁻²¹");
    expect(formatNumber(-6.02214076e-7)).toBe("-6.02214076 × 10⁻⁷");
    // Every digit survives: decimals capped at 20 places would drop the last ones.
    expect(formatNumber(1.234567890123e-16)).toBe("1.234567890123 × 10⁻¹⁶");
    expect(formatNumber(1.234567e-18)).toBe("1.234567 × 10⁻¹⁸");
    expect(formatNumber(0.000001)).toBe("0.000001");
    expect(formatNumber(52.341)).toBe("52.341");
    expect(formatNumber(1234567)).toBe("1,234,567");
  });

  it("shows a tiny concentration as itself, in both units", () => {
    const molar = field({
      name: "concentration",
      label: "Concentration",
      type: "quantity",
      unit: "mol/L",
      unit_symbol: "mol/L",
      display_unit: "umol/L",
      display_unit_symbol: "μmol/L",
      display_scale: 1e6,
    });
    expect(quantity(molar, 1e-21)).toEqual({
      shown: "1 × 10⁻¹⁵ μmol/L",
      stored: "1 × 10⁻²¹ mol/L",
    });
    expect(quantity(molar, 0)).toEqual({ shown: "0 μmol/L", stored: "0 mol/L" });
  });
});
