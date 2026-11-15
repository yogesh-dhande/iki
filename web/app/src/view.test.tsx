/** Saved views (core.View): saving what a viewer shows, and opening it on its revision. */

import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { entity, field, PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import { answer, calls } from "./test-server";

const PLASMID = {
  ...PROTEIN,
  name: "dna.DNASequence",
  plugin: "dna",
  label: "DNA sequence",
  label_plural: "DNA sequences",
  hrid_prefix: "DNA",
  fields: [
    field({ name: "bases", label: "Bases", type: "string" }),
    field({ name: "features", label: "Features", type: "json" }),
  ],
  viewers: [
    {
      viewer: "sequence" as const,
      label: "Sequence",
      sequence: "bases",
      alphabet: "dna" as const,
      features: "features",
      tracks: [],
    },
  ],
};

const VIEW = {
  ...PROTEIN,
  name: "core.View",
  plugin: "core",
  label: "View",
  label_plural: "Views",
  hrid_prefix: "VIEW",
  fields: [
    field({ name: "name", label: "Name", type: "string", plugin: "core" }),
    field({ name: "target", label: "Target", type: "ref", plugin: "core", to: "core.Entity" }),
    field({ name: "target_revision", label: "Revision", type: "int", plugin: "core" }),
    field({ name: "viewer", label: "Viewer", type: "enum", plugin: "core" }),
    field({ name: "state", label: "State", type: "json", plugin: "core" }),
  ],
  viewers: [],
};

const PLASMID_ID = "0190f5c2-0000-7000-8000-00000000d0a1";
const VIEW_ID = "0190f5c2-0000-7000-8000-00000000f1e1";
const TET = { type: "CDS", name: "tet", start: 5, end: 130, strand: "forward" };
const BLA = { type: "CDS", name: "bla", start: 140, end: 160, strand: "reverse" };

function plasmid(revision: number, features: unknown[]) {
  return entity({
    id: PLASMID_ID,
    hrid: "DNA-000001",
    type: "dna.DNASequence",
    revision,
    values: { name: "pTest", bases: "ACGT".repeat(40), features },
  });
}

function onTypes() {
  withTypes();
  answer("GET /api/v1/entity-types", [PROTEIN, PLASMID, VIEW]);
}

describe("saving a view", () => {
  it("saves what the viewer shows, on the revision it shows", async () => {
    onTypes();
    answer("GET /api/v1/entities/DNA-000001", plasmid(3, [TET, BLA]));
    answer("POST /api/v1/entities/search", { items: [], next_cursor: null });
    answer("POST /api/v1/entities", { id: "v1", hrid: "VIEW-000001", revision: 1 });
    answer("GET /api/v1/entities/VIEW-000001", entity({ hrid: "VIEW-000001", type: "core.View" }));
    const { user } = await renderApp("/entities/DNA-000001/viewers/sequence/");
    expect(await screen.findByText(/None yet\. A saved view keeps/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show bla" }));
    await user.click(screen.getByRole("button", { name: "Save view" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Save this view" });
    expect(dialog).toHaveTextContent("on revision 3 of DNA-000001");
    expect(within(dialog).getByRole("textbox", { name: "Name" })).toHaveValue("Sequence of pTest");
    await user.click(within(dialog).getByRole("button", { name: "Save view" }));
    await waitFor(() => {
      expect(calls("POST", "/api/v1/entities")).toHaveLength(1);
    });
    const [saved] = calls("POST", "/api/v1/entities");
    expect(saved?.body).toMatchObject({
      type: "core.View",
      values: {
        name: "Sequence of pTest",
        target: PLASMID_ID,
        target_revision: 3,
        viewer: "sequence",
        state: { version: 1, feature: { type: "CDS", name: "bla", start: 140, end: 160 } },
      },
    });
  });

  it("lists the views saved before", async () => {
    onTypes();
    answer("GET /api/v1/entities/DNA-000001", plasmid(3, [TET]));
    answer("POST /api/v1/entities/search", {
      items: [
        entity({
          hrid: "VIEW-000002",
          type: "core.View",
          values: { name: "tet for the report", target_revision: 2 },
        }),
      ],
      next_cursor: null,
    });
    await renderApp("/entities/DNA-000001/viewers/sequence/");
    expect(await screen.findByRole("link", { name: "tet for the report" })).toHaveAttribute(
      "href",
      "/entities/VIEW-000002/",
    );
    expect(screen.getByText(/revision 2/)).toBeInTheDocument();
    const [search] = calls("POST", "/api/v1/entities/search");
    expect(search?.body).toMatchObject({
      type: "core.View",
      where: {
        all: [
          { field: "target", op: "eq", value: PLASMID_ID },
          { field: "viewer", op: "eq", value: "sequence" },
        ],
      },
    });
  });
});

describe("a saved view's page", () => {
  function onView() {
    onTypes();
    answer(
      "GET /api/v1/entities/VIEW-000001",
      entity({
        id: VIEW_ID,
        hrid: "VIEW-000001",
        type: "core.View",
        revision: 1,
        values: {
          name: "tet",
          target: PLASMID_ID,
          target_revision: 2,
          viewer: "sequence",
          state: { version: 1, feature: { type: "CDS", name: "tet", start: 5, end: 130 } },
        },
        refs: {
          [PLASMID_ID]: {
            hrid: "DNA-000001",
            title: "pTest",
            type: "dna.DNASequence",
            archived: false,
          },
        },
      }),
    );
    // Revision 2 had only tet; now there's bla too.
    answer("GET /api/v1/entities/DNA-000001/revisions/2", {
      number: 2,
      kind: "edited",
      values: { name: "pTest", bases: "ACGT".repeat(40), features: [TET] },
      changes: {},
      refs: {},
      files: {},
    });
    answer("GET /api/v1/entities/DNA-000001", plasmid(5, [TET, BLA]));
    answer(`PATCH /api/v1/entities/${VIEW_ID}`, entity({ hrid: "VIEW-000001", revision: 2 }));
  }

  it("opens on its revision, showing what it saved", async () => {
    onView();
    await renderApp("/entities/VIEW-000001/");
    expect(
      await screen.findByText("Revision 2 of DNA-000001, as the view saved it."),
    ).toBeVisible();
    expect(await screen.findByText("160 bp · 1 feature")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide tet" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("shows the latest revision, and saves the view on it", async () => {
    onView();
    const { user } = await renderApp("/entities/VIEW-000001/");
    await screen.findByText("160 bp · 1 feature");
    await user.click(screen.getByRole("switch", { name: "Show the latest revision" }));
    expect(await screen.findByText("160 bp · 2 features")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide tet" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.click(screen.getByRole("button", { name: "Save on the latest revision" }));
    await waitFor(() => {
      expect(calls("PATCH", `/api/v1/entities/${VIEW_ID}`)).toHaveLength(1);
    });
    const [saved] = calls("PATCH", `/api/v1/entities/${VIEW_ID}`);
    expect(saved?.headers.get("If-Match")).toBe('"1"');
    expect(saved?.body).toMatchObject({
      values: {
        target_revision: 5,
        state: { version: 1, feature: { type: "CDS", name: "tet", start: 5, end: 130 } },
      },
    });
  });
});
