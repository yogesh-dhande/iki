/**
 * Collections: saving the grid's search as one, a collection's page, and
 * the grid showing its members, against a fake API.
 */

import { screen, waitFor, within } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { entity, KINASE, nth, PROTEIN_ID, renderApp, withTypes } from "./entity-fixtures";
import { answer, calls, page, respond } from "./test-server";

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

const SEARCH = {
  project: KINASE.id,
  type: "protein.Protein",
  archived: false,
  where: { all: [{ field: "name", op: "contains", value: "abl" }] },
  sort: "name",
  descending: true,
};

function collection(values: Record<string, unknown>) {
  return entity({
    id: "00000000-0000-7000-8000-00000000c001",
    hrid: "COL-000001",
    type: "core.Collection",
    title: "Kinase picks",
    values: { name: "Kinase picks", ...values },
  });
}

function onProject(): void {
  withTypes();
  answer("GET /api/v1/projects", page([KINASE]));
  answer("GET /api/v1/saved-searches", []);
}

function onCollection(values: Record<string, unknown>): void {
  withTypes();
  answer("GET /api/v1/entities/COL-000001", collection(values));
  answer("GET /api/v1/entities/COL-000001/provenance", {});
  answer("GET /api/v1/entities/COL-000001/projects", [
    {
      project: { id: KINASE.id, name: KINASE.name, slug: KINASE.slug },
      owner: true,
      linked_at: "2026-03-14T09:00:00Z",
      linked_by: null,
    },
  ]);
}

describe("collections", () => {
  it("saves the grid's search as a dynamic collection", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
    answer("POST /api/v1/collections", respond(201, collection({ search: SEARCH })));
    onCollection({ search: SEARCH });
    const filters = encodeURIComponent(
      JSON.stringify([{ field: "name", op: "contains", value: "abl" }]),
    );
    const { router, user } = await renderApp(
      `/projects/kinase-atlas/entities/?type=protein.Protein&filters=${filters}&sort=name&desc=true`,
    );
    await screen.findByRole("table");
    await user.click(screen.getByRole("button", { name: "Save as collection" }));
    const dialog = screen.getByRole("alertdialog", { name: "Save as a collection" });
    await user.type(within(dialog).getByLabelText("Name"), "Kinase picks");
    await user.click(within(dialog).getByRole("button", { name: "Save collection" }));
    await screen.findByText("Saved the collection COL-000001.");
    expect(router.state.location.pathname).toBe("/entities/COL-000001/");
    expect(nth(calls("POST", "/api/v1/collections"), 0).body).toEqual({
      project: KINASE.id,
      name: "Kinase picks",
      search: SEARCH,
      keep: "search",
      reason: "",
    });
  });

  it("can save a fixed list, from every project", async () => {
    onProject();
    answer("POST /api/v1/entities/search", page([entity()]));
    answer("POST /api/v1/collections", respond(201, collection({ members: [PROTEIN_ID] })));
    onCollection({ members: [PROTEIN_ID] });
    const { user } = await renderApp("/projects/kinase-atlas/entities/?show=all");
    await screen.findByRole("table");
    await user.click(screen.getByRole("button", { name: "Save as collection" }));
    const dialog = screen.getByRole("alertdialog", { name: "Save as a collection" });
    await user.type(within(dialog).getByLabelText("Name"), "Everything");
    await user.click(within(dialog).getByLabelText(/These entities/));
    await user.click(within(dialog).getByLabelText(/Only entities in Kinase Atlas/));
    await user.click(within(dialog).getByRole("button", { name: "Save collection" }));
    await screen.findByText("Saved the collection COL-000001.");
    expect(nth(calls("POST", "/api/v1/collections"), 0).body).toMatchObject({
      name: "Everything",
      search: {},
      keep: "members",
    });
  });

  it("shows what a dynamic collection holds, and links to its members", async () => {
    onCollection({ search: SEARCH });
    await renderApp("/entities/COL-000001/");
    const members = await screen.findByRole("region", { name: "Members" });
    expect(members).toHaveTextContent("Everything this search finds when it’s opened.");
    expect(members).toHaveTextContent("TypeProtein");
    expect(members).toHaveTextContent("WhereIn one project");
    expect(members).toHaveTextContent("Conditions1 condition");
    const link = within(members).getByRole("link", { name: "Show its members" });
    const href = new URL(link.getAttribute("href") ?? "", "http://localhost");
    expect(href.pathname).toBe("/projects/kinase-atlas/entities/");
    expect(Object.fromEntries(href.searchParams)).toEqual({
      collection: "COL-000001",
      type: "protein.Protein",
      show: "all",
      sort: "name",
      desc: "true",
    });
    // Its search isn't shown again as a raw value.
    const values = screen.getByRole("region", { name: "Values" });
    expect(within(values).queryByText("Search")).not.toBeInTheDocument();
  });

  it("freezes a dynamic collection into a fixed list", async () => {
    onCollection({ search: SEARCH });
    answer(
      "POST /api/v1/collections/00000000-0000-7000-8000-00000000c001/freeze",
      collection({ members: [PROTEIN_ID], search: null }),
    );
    const { user } = await renderApp("/entities/COL-000001/");
    await user.click(await screen.findByRole("button", { name: "Freeze as a fixed list" }));
    const dialog = screen.getByRole("alertdialog", { name: "Freeze Kinase picks?" });
    await user.type(within(dialog).getByLabelText(/Reason/), "For the paper");
    await user.click(within(dialog).getByRole("button", { name: "Freeze" }));
    await screen.findByText("Froze COL-000001: it lists 1 entity.");
    const call = nth(
      calls("POST", "/api/v1/collections/00000000-0000-7000-8000-00000000c001/freeze"),
      0,
    );
    expect(call.body).toEqual({ reason: "For the paper" });
    expect(call.headers.get("If-Match")).toBe('"2"');
    expect(await screen.findByText("It lists 1 entity. You see those you can read.")).toBeVisible();
  });

  it("says how many entities a static collection lists", async () => {
    onCollection({ members: [PROTEIN_ID, "00000000-0000-7000-8000-00000000e009"] });
    await renderApp("/entities/COL-000001/");
    const members = await screen.findByRole("region", { name: "Members" });
    expect(members).toHaveTextContent("It lists 2 entities. You see those you can read.");
    expect(within(members).getByRole("link", { name: "Show its members" })).toHaveAttribute(
      "href",
      "/projects/kinase-atlas/entities/?collection=COL-000001",
    );
  });

  it("shows a collection's members in the grid, from every project", async () => {
    onProject();
    onCollection({ members: [PROTEIN_ID] });
    answer("POST /api/v1/entities/search", page([entity()]));
    await renderApp("/projects/kinase-atlas/entities/?collection=COL-000001&type=protein.Protein");
    await screen.findByRole("table");
    const note = await screen.findByText(/Members of/);
    await waitFor(() => {
      expect(note).toHaveTextContent(
        "Members of Kinase picks: the entities it lists, from every project you can read.",
      );
    });
    expect(screen.getByRole("link", { name: "Show all of Kinase Atlas’s entities" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Save as collection" })).not.toBeInTheDocument();
    expect(nth(calls("POST", "/api/v1/entities/search"), 0).body).toEqual({
      limit: 100,
      exact_type: false,
      collection: "COL-000001",
      type: "protein.Protein",
      archived: false,
    });
  });
});
