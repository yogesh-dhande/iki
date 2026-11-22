/**
 * Files (ADR 0177), against a fake API: the file picker in an entity form, and how a list
 * of files says what it holds.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { field, KINASE, PROTEIN, renderApp, STRUCTURE } from "./entity-fixtures";
import { acquiredText, filterOf, searchOf, spanText, usedByText } from "./files";
import { answer, calls, page, signedIn } from "./test-server";

type Row = Schemas["FileRowOut"];

function file(id: string, name: string, overrides: Partial<Row> = {}): Row {
  return {
    id: `00000000-0000-7000-8000-0000000f${id.padStart(4, "0")}`,
    name,
    media_type: "chemical/x-pdb",
    size: 2048,
    sha256: "a".repeat(64),
    project: { id: KINASE.id, name: KINASE.name, slug: KINASE.slug },
    created_by: null,
    agent: null,
    created_at: "2026-11-11T09:00:00Z",
    external: false,
    acquired: null,
    used_by: { records: 0, runs: 0 },
    ...overrides,
  };
}

const A = file("1", "1abc.pdb", { used_by: { records: 2, runs: 1 } });
const B = file("2", "2xyz.pdb");
const C = file("3", "3def.pdb");

function answerFiles(items: Row[]) {
  answer("GET /api/v1/files", page(items));
  answer("GET /api/v1/files/count", { total: items.length, more_than_total: false });
  answer("GET /api/v1/files/formats", [{ media_type: "chemical/x-pdb", count: items.length }]);
}

describe("the file picker", () => {
  afterEach(() => {
    window.history.replaceState(null, "", "/");
  });

  it("chooses several files already in the project for a list, up to what it takes", async () => {
    signedIn();
    const traces: Schemas["EntityTypeOut"] = {
      ...STRUCTURE,
      fields: [field({ name: "traces", label: "Traces", type: "list", items: "file" })],
      values_schema: {
        properties: {
          traces: {
            type: "array",
            maxItems: 2,
            items: { type: "string", "x-iki-media-types": ["chemical/x-pdb"] },
          },
        },
      },
    };
    answer("GET /api/v1/entity-types", [PROTEIN, traces]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/entities", page([]));
    answerFiles([A, B, C]);
    const { user } = await renderApp(
      "/projects/kinase-atlas/entities/new/?type=iki/protein.ProteinStructure",
    );
    const group = screen.getByRole("group", { name: /^Traces/ });
    await user.click(within(group).getByRole("button", { name: "Choose files already here" }));
    const picker = await screen.findByRole("dialog", { name: "Choose files for Traces" });
    expect(within(picker).getByText(/Choose up to 2\./)).toBeInTheDocument();
    // What it lists is the server's, held to what the list's items take.
    await within(picker).findByRole("checkbox", { name: "1abc.pdb" });
    const asked = calls("GET", "/api/v1/files").at(-1)?.query;
    expect(asked?.get("project")).toBe(KINASE.id);
    expect(asked?.getAll("accepts")).toEqual(["chemical/x-pdb"]);
    expect(within(picker).getByRole("cell", { name: "2 records · 1 run" })).toBeInTheDocument();

    await user.click(within(picker).getByRole("checkbox", { name: "1abc.pdb" }));
    // Clicking a row chooses it too.
    const row = within(picker).getByRole("row", { name: /2xyz\.pdb/ });
    await user.click(within(row).getByRole("cell", { name: "2.0 KiB" }));
    // Two is all it takes: the third can't be chosen.
    expect(within(picker).getByRole("checkbox", { name: "3def.pdb" })).toBeDisabled();
    await user.click(within(picker).getByRole("button", { name: "Choose 2 files" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(within(group).getByText("1abc.pdb")).toBeInTheDocument();
    expect(within(group).getByText("2xyz.pdb")).toBeInTheDocument();
    expect(
      within(group)
        .getAllByRole("button", { name: /^Remove / })
        .map((b) => b.getAttribute("aria-label")),
    ).toEqual(["Remove 1abc.pdb", "Remove 2xyz.pdb"]);

    // Opened again, those already in the list show as chosen, and it holds no more.
    await user.click(within(group).getByRole("button", { name: "Choose files already here" }));
    const again = await screen.findByRole("dialog", { name: "Choose files for Traces" });
    expect(
      within(again).getByText(/It holds as many as it takes: remove some first\./),
    ).toBeInTheDocument();
    const held = await within(again).findByRole("checkbox", { name: /1abc\.pdb/ });
    expect(held).toBeChecked();
    expect(held).toBeDisabled();
  });

  it("searches as its Files tab does, and says when nothing matches", async () => {
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN, STRUCTURE]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/entities", page([]));
    answerFiles([A]);
    const { user } = await renderApp(
      "/projects/kinase-atlas/entities/new/?type=iki/protein.ProteinStructure",
    );
    const structure = screen.getByRole("group", { name: "Structure" });
    await user.click(within(structure).getByRole("button", { name: "Or choose one already here" }));
    const picker = await screen.findByRole("dialog", { name: "Choose a file for Structure" });
    await within(picker).findByRole("radio", { name: "1abc.pdb" });
    answerFiles([]);
    await user.type(within(picker).getByRole("searchbox", { name: "Search" }), "clone-17");
    expect(
      await within(picker).findByText(
        "No files match. Try another part of the name, or fewer filters.",
      ),
    ).toBeInTheDocument();
    expect(calls("GET", "/api/v1/files").at(-1)?.query.get("q")).toBe("clone-17");
    await user.click(within(picker).getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });
});

describe("a list of files says", () => {
  it("when its data was acquired, as precisely as its source gave it", () => {
    expect(acquiredText("2026-03-14")).toBe("2026-03-14");
    expect(acquiredText("2026-03-14T09:21:07.25")).toBe("2026-03-14 09:21:07.25");
    expect(acquiredText("2026-03-14T09:21Z")).toBe("2026-03-14 09:21 UTC");
    expect(acquiredText("2026-03-14T09:21:07+01:00")).toBe("2026-03-14 09:21:07 +01:00");
  });

  it("what uses a file, and which revisions held it", () => {
    expect(usedByText({ records: 0, runs: 0 })).toBe("Not used");
    expect(usedByText({ records: 1, runs: 0 })).toBe("1 record");
    expect(usedByText({ records: 2, runs: 3 })).toBe("2 records · 3 runs");
    expect(spanText({ first: 1, last: 2 })).toBe("r1–r2");
    expect(spanText({ first: 3, last: 3 })).toBe("r3");
    expect(spanText({ first: 5, last: null })).toBe("r5 on");
  });

  it("its filter in the address, without the defaults", () => {
    const filter = filterOf({ q: "clone", format: "image/*,application/pdf", sort: "name" });
    expect(filter).toEqual({
      q: "clone",
      format: ["image/*", "application/pdf"],
      acquiredFrom: undefined,
      acquiredTo: undefined,
      sample: undefined,
      run: undefined,
      sort: "name",
    });
    expect(searchOf(filter)).toEqual({
      q: "clone",
      format: "image/*,application/pdf",
      sort: "name",
    });
    expect(searchOf(filterOf({}))).toEqual({});
  });
});
