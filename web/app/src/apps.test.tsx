/** Apps (core.App, ADR 0090): panels that read the ones before them, rendered with the
 * real iki-wasm, run here without a worker. */

import { screen, waitFor, within } from "@testing-library/react";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";

import type { Offers, Rendered } from "./apps";
import { entity, field, PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import { answer, calls } from "./test-server";

vi.mock("./apps", async (original) => {
  const real = await original<typeof import("./apps")>();
  // Built by scripts/build-wasm.sh, as the TypeScript job does before the tests.
  const here = join(process.cwd(), "wasm", "iki_wasm.wasm");
  const bytes = readFileSync(existsSync(here) ? here : join("web", "app", "wasm", "iki_wasm.wasm"));
  const { instance } = await WebAssembly.instantiate(bytes, {});
  const wasm = instance.exports as unknown as {
    memory: WebAssembly.Memory;
    iki_input: (len: number) => number;
    iki_render: () => void;
    iki_output: () => number;
    iki_output_len: () => number;
  };
  return {
    ...real,
    renderTemplates: (templates: string[], variables: Offers, rows?: unknown[]) => {
      const request = new TextEncoder().encode(
        JSON.stringify({ templates, variables, ...(rows ? { rows } : {}) }),
      );
      const at = wasm.iki_input(request.length);
      new Uint8Array(wasm.memory.buffer, at, request.length).set(request);
      wasm.iki_render();
      const out = new Uint8Array(wasm.memory.buffer, wasm.iki_output(), wasm.iki_output_len());
      const found = JSON.parse(new TextDecoder().decode(out)) as { values: Rendered[][] };
      return Promise.resolve(found.values);
    },
  };
});

const PLASMID = {
  ...PROTEIN,
  name: "dna.DNASequence",
  plugin: "dna",
  label: "DNA sequence",
  label_plural: "DNA sequences",
  hrid_prefix: "DNA",
  fields: [
    field({ name: "name", label: "Name", type: "string" }),
    field({ name: "bases", label: "Bases", type: "string" }),
  ],
  viewers: [
    {
      viewer: "sequence" as const,
      label: "Sequence",
      sequence: "bases",
      alphabet: "dna" as const,
      features: null,
      tracks: [],
    },
  ],
};

const APP = {
  ...PROTEIN,
  name: "core.App",
  plugin: "core",
  label: "App",
  label_plural: "Apps",
  hrid_prefix: "APP",
  fields: [
    field({ name: "name", label: "Name", type: "string", plugin: "core" }),
    field({ name: "description", label: "Description", type: "text", plugin: "core" }),
    field({ name: "spec", label: "Spec", type: "json", plugin: "core" }),
  ],
  viewers: [],
};

function plasmid(n: number, name: string, bases: string) {
  return entity({
    id: `0190f5c2-0000-7000-8000-00000000d0a${String(n)}`,
    hrid: `DNA-00000${String(n)}`,
    type: "dna.DNASequence",
    values: { name, bases },
  });
}

const PTEST = plasmid(1, "pTest", "ACGT".repeat(40));
const POTHER = plasmid(2, "pOther", "ACGT".repeat(10));

function app(panels: unknown[]) {
  return entity({
    id: "0190f5c2-0000-7000-8000-0000000a0001",
    hrid: "APP-000001",
    type: "core.App",
    values: { name: "Plasmid QC", spec: { version: 1, panels } },
  });
}

const GRID = {
  id: "plasmids",
  title: "Plasmids",
  width: 6,
  component: "iki.entities",
  props: {
    type: "dna.DNASequence",
    where: { all: [{ field: "name", op: "starts_with", value: "p" }] },
    sort: "name",
    columns: [{ field: "name" }, { label: "Length", value: "{{ size(row.values.bases) }} bp" }],
  },
};
const VIEWER = {
  id: "shown",
  title: "Picked",
  width: 6,
  component: "iki.viewer",
  props: { entity: "{{ plasmids.selection }}" },
};
const TEXT = {
  id: "summary",
  component: "iki.text",
  props: { text: "{{ size(plasmids.selection) }} of {{ size(plasmids.rows) }} picked." },
};

function onTypes() {
  withTypes();
  answer("GET /api/v1/entity-types", [PROTEIN, PLASMID, APP]);
}

describe("an app", () => {
  it("shows panels that read what the ones before them offer", async () => {
    onTypes();
    answer("GET /api/v1/entities/APP-000001", app([GRID, VIEWER, TEXT]));
    answer("POST /api/v1/entities/search", { items: [PTEST, POTHER], next_cursor: null });
    answer("GET /api/v1/entities/DNA-000001", PTEST);
    const { user } = await renderApp("/entities/APP-000001/");

    const grid = await screen.findByRole("region", { name: "Plasmids" });
    const rows = await within(grid).findAllByRole("row");
    expect(rows.map((r) => r.textContent)).toEqual([
      "PickIDNameLength",
      "DNA-000001pTest160 bp",
      "DNA-000002pOther40 bp",
    ]);
    expect(await screen.findByText("0 of 2 picked.")).toBeInTheDocument();
    const picked = screen.getByRole("region", { name: "Picked" });
    expect(within(picked).getByText("Nothing to show yet.")).toBeInTheDocument();

    // The grid searches as the app says, in the app's project, as the person viewing it.
    const search = calls("POST", "/api/v1/entities/search")[0]?.body as Record<string, unknown>;
    expect(search).toMatchObject({
      type: "dna.DNASequence",
      project: app([]).owner_project?.id,
      where: GRID.props.where,
      sort: { field: "name", descending: false },
    });

    await user.click(within(grid).getByRole("radio", { name: "Pick pTest" }));
    expect(await screen.findByText("1 of 2 picked.")).toBeInTheDocument();
    expect(await within(picked).findByRole("link", { name: "pTest" })).toHaveAttribute(
      "href",
      "/entities/DNA-000001/",
    );
    expect(within(picked).getByText(/Sequence of/)).toBeInTheDocument();
    // Its spec is there to read, folded away.
    expect(screen.getByText("Its spec, as saved")).toBeInTheDocument();
  });

  it("says why an expression has no value, as text", async () => {
    onTypes();
    const broken = {
      ...TEXT,
      props: { text: "Ratio: {{ size(plasmids.rows) / size(plasmids.selection) }}" },
    };
    const html = { id: "html", component: "iki.text", props: { text: "<b>{{ 'bold' }}</b>" } };
    answer("GET /api/v1/entities/APP-000001", app([GRID, broken, html]));
    answer("POST /api/v1/entities/search", { items: [PTEST], next_cursor: null });
    await renderApp("/entities/APP-000001/");

    expect(
      await screen.findByText(
        "In `{{ size(plasmids.rows) / size(plasmids.selection) }}`: It divides by zero.",
      ),
    ).toBeInTheDocument();
    // What it makes is text, never HTML.
    expect(await screen.findByText("<b>bold</b>")).toBeInTheDocument();
    expect(document.querySelector(".app-grid b")).toBeNull();
  });

  it("says when it's an app this version can't show", async () => {
    onTypes();
    answer(
      "GET /api/v1/entities/APP-000001",
      app([{ id: "x", component: "iki.script", props: {} }]),
    );
    await renderApp("/entities/APP-000001/");
    expect(await screen.findByText("This version of Iki can’t show this app.")).toBeInTheDocument();
    await waitFor(() => {
      expect(calls("POST", "/api/v1/entities/search")).toHaveLength(0);
    });
  });
});
