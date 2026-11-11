/**
 * Starting a run from the web app, against a fake API: from an entity's page,
 * on one entity or on several at once.
 */

import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { entity, field, KINASE, PROTEIN_ID, renderApp, withTypes } from "./entity-fixtures";
import { ACTION, run } from "./run-fixtures";
import { answer, calls, page, problem, project } from "./test-server";
import type { Schemas } from "@ikisci/sdk";

const SECOND_ID = "00000000-0000-7000-8000-00000000e002";
const FILE_ID = "00000000-0000-7000-8000-00000000f001";

const COMPUTE: Schemas["ActionOut"] = {
  ...ACTION,
  description: "Weighs a protein from its sequence.",
  updates: [{ input: "protein", fields: ["molecular_weight"] }],
};

const PREDICT: Schemas["ActionOut"] = {
  ...ACTION,
  name: "structure_prediction.predict_structure",
  label: "Predict structure",
  tier: "vm",
  needs: "Runner",
  params: [],
};

const OFFLINE: Schemas["ActionOut"] = {
  ...ACTION,
  name: "protein.relax",
  label: "Relax",
  available: false,
  unavailable_reason: "There are no backend workers for protein.protein-tools.",
  params: [],
};

function onProject(can: Partial<Schemas["ProjectOut"]["can"]> = {}): void {
  withTypes();
  const atlas = project({ can: { ...KINASE.can, run: ["client", "backend"], ...can } });
  answer("GET /api/v1/projects", page([atlas]));
  answer("GET /api/v1/actions", [COMPUTE, PREDICT, OFFLINE]);
  answer("POST /api/v1/entities/search", (r: { body: { where: { value: string[] } } }) =>
    page(
      [
        entity(),
        entity({ id: SECOND_ID, hrid: "PRT-000002", values: { name: "SRC kinase" } }),
      ].filter((e) => r.body.where.value.includes(e.hrid)),
    ),
  );
}

describe("starting a run", () => {
  it("opens from an entity's page, for the entity", async () => {
    withTypes();
    answer("GET /api/v1/entities/PRT-000001", entity());
    answer("GET /api/v1/entities/PRT-000001/provenance", {});
    answer("GET /api/v1/entities/PRT-000001/projects", []);
    await renderApp("/entities/PRT-000001/");
    expect(screen.getByRole("link", { name: "Run action" })).toHaveAttribute(
      "href",
      "/projects/kinase-atlas/runs/new/?entities=PRT-000001",
    );
  });

  it("offers the actions that take it, then starts one run with its params", async () => {
    onProject();
    answer(`POST /api/v1/actions/${ACTION.name}/runs`, () => run());
    answer("GET /api/v1/runs/RUN-000001", run());
    answer("GET /api/v1/runs/RUN-000001/attempts", []);
    const { user, router } = await renderApp(
      "/projects/kinase-atlas/runs/new/?entities=PRT-000001",
    );
    const select = await screen.findByRole("combobox", { name: "Action" });
    expect(calls("GET", "/api/v1/actions").at(-1)?.query.getAll("takes")).toEqual([
      "protein.Protein",
    ]);
    expect(
      within(select)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["Choose an action", "Compute properties"]);
    const more = screen.getByText("2 more actions you can’t run here").closest("details");
    expect(more).toHaveTextContent("Predict structure: needs the Runner role in Kinase Atlas.");
    expect(more).toHaveTextContent(
      "Relax: There are no backend workers for protein.protein-tools.",
    );

    await user.selectOptions(select, "Compute properties");
    expect(await screen.findByText("Weighs a protein from its sequence.")).toBeInTheDocument();
    expect(screen.getByText(/^It runs on one of Iki’s backend workers/)).toHaveTextContent(
      "It runs on one of Iki’s backend workers, in the sequence-tools environment, for up to 10 minutes each time.",
    );
    expect(screen.getByText(/^It changes/)).toHaveTextContent(
      "It changes its molecular weight on the protein.",
    );
    expect(screen.getByText("It can’t reach the internet.")).toBeInTheDocument();
    const protein = screen.getByRole("group", { name: "Protein" });
    expect(within(protein).getByRole("link", { name: "PRT-000001" })).toBeInTheDocument();
    const ph = screen.getByRole("textbox", { name: /pH/ });
    expect(ph).toHaveValue("7");
    await user.clear(ph);
    await user.type(ph, "7.4");
    await user.type(screen.getByRole("textbox", { name: /Reason/ }), "Weekly batch");
    await user.click(screen.getByRole("button", { name: "Start run" }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/runs/RUN-000001/");
    });
    expect(calls("POST", `/api/v1/actions/${ACTION.name}/runs`).at(-1)?.body).toEqual({
      project: KINASE.id,
      inputs: { protein: PROTEIN_ID },
      params: { ph: 7.4 },
      reason: "Weekly batch",
    });
    expect(await screen.findByText("Started RUN-000001.")).toBeVisible();
  });

  it("starts one run for each of several, all or none", async () => {
    onProject();
    answer(`POST /api/v1/actions/${ACTION.name}/runs/batch`, () =>
      problem(422, "validation_failed", "Some inputs or params are invalid.", [
        { pointer: "#/entities", detail: "PRT-000002 is archived." },
      ]),
    );
    const { user } = await renderApp(
      `/projects/kinase-atlas/runs/new/?entities=PRT-000001,PRT-000002&action=${ACTION.name}`,
    );
    expect(
      await screen.findByText("One run for each of these 2: if any can’t run, none is started."),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start 2 runs" }));
    expect(await screen.findByText("PRT-000002 is archived.")).toBeInTheDocument();
    expect(calls("POST", `/api/v1/actions/${ACTION.name}/runs/batch`).at(-1)?.body).toEqual({
      project: KINASE.id,
      each: "protein",
      entities: [PROTEIN_ID, SECOND_ID],
      inputs: {},
      params: { ph: 7 },
      reason: "",
    });

    answer(`POST /api/v1/actions/${ACTION.name}/runs/batch`, () => ({
      runs: [run(), run({ hrid: "RUN-000002" })],
    }));
    answer("GET /api/v1/runs", page([]));
    await user.click(screen.getByRole("button", { name: "Start 2 runs" }));
    expect(await screen.findByText("Started 2 runs of Compute properties.")).toBeVisible();
    expect(screen.getByRole("link", { name: "Runs" })).toHaveAttribute("aria-current", "page");
  });

  it("imports from a file in the project, with no entities to choose", async () => {
    onProject();
    const IMPORT: Schemas["ActionOut"] = {
      ...ACTION,
      name: "protein.import_proteins",
      label: "Import proteins",
      inputs: [],
      creates: [{ type: "protein.Protein", cardinality: "many", derived_from: [] }],
      updates: [],
      params: [
        {
          ...field({ name: "file", label: "FASTA file", type: "file", required: true }),
          default: null,
          min: null,
          max: null,
          min_length: null,
          max_length: null,
          pattern: null,
        },
      ],
      params_schema: {
        type: "object",
        properties: {
          file: {
            type: "string",
            format: "uuid",
            title: "FASTA file",
            "x-iki-media-types": ["chemical/seq-aa-fasta"],
          },
        },
        required: ["file"],
        additionalProperties: false,
      },
    };
    answer("GET /api/v1/actions", [COMPUTE, IMPORT]);
    const fasta = {
      id: FILE_ID,
      name: "kinases.fasta",
      media_type: "chemical/seq-aa-fasta",
      size: 2048,
      sha256: "x".repeat(64),
      project: null,
      created_by: null,
      created_at: "2026-11-11T09:00:00Z",
      external: false,
    };
    const notes = { ...fasta, id: SECOND_ID, name: "notes.txt", media_type: "text/plain" };
    answer("GET /api/v1/files", page([fasta, notes]));
    answer("POST /api/v1/actions/protein.import_proteins/runs", () => run());
    answer("GET /api/v1/runs/RUN-000001", run());
    answer("GET /api/v1/runs/RUN-000001/attempts", []);
    const { user, router } = await renderApp("/projects/kinase-atlas/runs/new/");

    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Action" }),
      "Import proteins",
    );
    expect(await screen.findByText("It makes new proteins.")).toBeInTheDocument();
    const file = screen.getByRole("group", { name: /FASTA file/ });
    const existing = within(file).getByRole("combobox", { name: /already in the project/ });
    // Only files the param takes are offered.
    await waitFor(() => {
      expect(
        within(existing)
          .getAllByRole("option")
          .map((o) => o.textContent),
      ).toEqual(["No file", "kinases.fasta (2.0 KiB)"]);
    });
    await user.click(screen.getByRole("button", { name: "Start run" }));
    expect(await within(file).findByText("Choose FASTA file.")).toBeInTheDocument();
    await user.selectOptions(existing, FILE_ID);
    await user.click(screen.getByRole("button", { name: "Start run" }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/runs/RUN-000001/");
    });
    expect(calls("POST", "/api/v1/actions/protein.import_proteins/runs").at(-1)?.body).toEqual({
      project: KINASE.id,
      inputs: {},
      params: { file: FILE_ID },
      reason: "",
    });
  });

  it("says when the person can't run anything in the project", async () => {
    onProject({ run: [] });
    await renderApp("/projects/kinase-atlas/runs/new/?entities=PRT-000001");
    expect(screen.getByText(/You can’t run actions in Kinase Atlas\./)).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Action" })).not.toBeInTheDocument();
  });

  it("says which entities it can't read", async () => {
    onProject();
    await renderApp("/projects/kinase-atlas/runs/new/?entities=PRT-000001,PRT-999999");
    expect(
      await screen.findByText("PRT-999999 isn’t an entity you can read, so it’s left out."),
    ).toBeInTheDocument();
  });
});
