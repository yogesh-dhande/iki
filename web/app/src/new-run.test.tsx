/**
 * Starting a run from the web app, against a fake API: from an entity's page,
 * on one entity or on several at once.
 */

import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { entity, field, KINASE, PROTEIN_ID, renderApp, withTypes } from "./entity-fixtures";
import { ACTION, batch, run } from "./run-fixtures";
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
  name: "iki/protein.relax",
  label: "Relax",
  available: false,
  unavailable_reason: "There are no backend workers for iki/protein.protein-tools.",
  params: [],
};

function onProject(can: Partial<Schemas["ProjectOut"]["can"]> = {}): void {
  withTypes();
  const atlas = project({ can: { ...KINASE.can, run: ["self_hosted", "backend"], ...can } });
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

  it("requires the operator's reason before launching on a reopened target", async () => {
    onProject();
    answer(
      "POST /api/v1/entities/search",
      page([entity({ can: { ...entity().can, edit_reason_required: true } })]),
    );
    const { user } = await renderApp(
      "/projects/kinase-atlas/runs/new/?entities=PRT-000001&action=" + ACTION.name,
    );
    const reason = await screen.findByRole("textbox", { name: "Reason" });
    expect(reason).toBeRequired();
    await user.click(screen.getByRole("button", { name: "Start run" }));
    expect(calls("POST", `/api/v1/actions/${ACTION.name}/runs`)).toHaveLength(0);
    expect(reason).toHaveAccessibleDescription(/reopened record|lock was lifted/);
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
      "iki/protein.Protein",
    ]);
    expect(
      within(select)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["Choose an action", "Compute properties"]);
    const more = screen.getByText("2 more actions you can’t run here").closest("details");
    expect(more).toHaveTextContent("Predict structure: needs the Runner role in Kinase Atlas.");
    expect(more).toHaveTextContent(
      "Relax: There are no backend workers for iki/protein.protein-tools.",
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
      reuse: true,
      add_to_project: false,
      labels: [],
      consent: [],
    });
    expect(await screen.findByText("Started RUN-000001.")).toBeVisible();
  });

  it("shows the alignment verdict and requires a reason to override it", async () => {
    onProject();
    const prep = entity({ hrid: "PREP-000001", type: "iki/lots.PlasmidPrep" });
    const alignment = entity({
      id: SECOND_ID,
      hrid: "ALN-000001",
      type: "iki/core.Alignment",
      values: { name: "Synthetic alignment", verdict: "differs" },
    });
    const actionName = "iki/lots.record_plasmid_verification";
    const param = {
      default: null,
      min: null,
      max: null,
      min_length: null,
      max_length: null,
      pattern: null,
    };
    answer("GET /api/v1/actions", [
      {
        ...ACTION,
        name: actionName,
        label: "Record plasmid verification",
        inputs: [
          {
            name: "prep",
            label: "Physical prep",
            type: prep.type,
            optional: false,
            cardinality: "one",
            max: null,
            description: null,
          },
          {
            name: "alignment",
            label: "Alignment",
            type: alignment.type,
            optional: true,
            cardinality: "one",
            max: null,
            description: null,
          },
        ],
        params: [
          {
            ...param,
            ...field({
              name: "outcome",
              label: "Outcome",
              type: "enum",
              values: ["match", "discrepancy", "inconclusive"],
              required: true,
            }),
          },
          {
            ...param,
            ...field({
              name: "override_reason",
              label: "Reason for overriding the verdict",
              type: "string",
            }),
          },
        ],
        params_schema: {},
      },
    ]);
    answer("POST /api/v1/entities/search", page([prep]));
    answer("GET /api/v1/entities", page([alignment]));
    answer("GET /api/v1/entities/" + SECOND_ID, alignment);
    const launched = "POST /api/v1/actions/" + actionName + "/runs";
    answer(launched, run({ action: actionName }));
    answer("GET /api/v1/runs/RUN-000001", run({ action: actionName }));
    answer("GET /api/v1/runs/RUN-000001/attempts", []);
    const { user } = await renderApp(
      "/projects/kinase-atlas/runs/new/?entities=PREP-000001&action=" + actionName,
    );
    await user.type(await screen.findByRole("combobox", { name: /^Alignment/ }), "synthetic");
    await user.click(await screen.findByRole("option", { name: /Synthetic alignment/ }));
    const outcome = screen.getByRole("combobox", { name: "Outcome" });
    await screen.findByText("The alignment reports “differs”.");
    expect(outcome).toHaveAccessibleDescription("The alignment reports “differs”.");
    await user.selectOptions(outcome, "match");
    await user.click(screen.getByRole("button", { name: "Start run" }));
    expect(screen.getByText(/Give a reason to record a match/)).toBeVisible();
    expect(calls("POST", "/api/v1/actions/" + actionName + "/runs")).toHaveLength(0);

    await user.type(
      screen.getByRole("textbox", { name: /^Reason for overriding/ }),
      "The second report explains this discrepancy.",
    );
    await user.click(screen.getByRole("button", { name: "Start run" }));
    await waitFor(() => {
      expect(calls("POST", "/api/v1/actions/" + actionName + "/runs")[0]?.body).toMatchObject({
        inputs: { prep: PROTEIN_ID, alignment: SECOND_ID },
        params: {
          outcome: "match",
          override_reason: "The second report explains this discrepancy.",
        },
      });
    });
  });

  it("offers to add inputs from other projects to this one, then starts the run", async () => {
    onProject();
    answer(
      `POST /api/v1/actions/${ACTION.name}/runs`,
      (r: { body: { add_to_project: boolean } }) =>
        r.body.add_to_project
          ? run()
          : problem(409, "inputs_outside_project", "PRT-000001 isn't in Kinase Atlas.", undefined, {
              entities: [{ id: PROTEIN_ID, hrid: "PRT-000001", type: "iki/protein.Protein" }],
            }),
    );
    answer("GET /api/v1/runs/RUN-000001", run());
    answer("GET /api/v1/runs/RUN-000001/attempts", []);
    const { user, router } = await renderApp(
      `/projects/kinase-atlas/runs/new/?entities=PRT-000001&action=${ACTION.name}`,
    );
    await user.click(await screen.findByRole("button", { name: "Start run" }));

    const dialog = await screen.findByRole("alertdialog", { name: "Add to Kinase Atlas?" });
    expect(dialog).toHaveTextContent(
      "PRT-000001 is in other projects. A run reads only what’s in its project, so it’ll be " +
        "added to Kinase Atlas first, and its members will be able to read it.",
    );
    await user.click(within(dialog).getByRole("button", { name: "Add and start run" }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/runs/RUN-000001/");
    });
    const sent = calls("POST", `/api/v1/actions/${ACTION.name}/runs`).map(
      (c) => (c.body as { add_to_project: boolean }).add_to_project,
    );
    expect(sent).toEqual([false, true]);
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
      reuse: true,
      add_to_project: false,
      labels: [],
      consent: [],
    });

    answer(`POST /api/v1/actions/${ACTION.name}/runs/batch`, () => ({
      batch: batch({ size: 2, statuses: { queued: 2 } }),
      runs: [run(), run({ hrid: "RUN-000002" })],
    }));
    answer("GET /api/v1/batches/BAT-000001", batch({ size: 2, statuses: { queued: 2 } }));
    answer("GET /api/v1/runs", page([]));
    await user.click(screen.getByRole("button", { name: "Start 2 runs" }));
    expect(
      await screen.findByText("Started 2 runs of Compute properties, as BAT-000001."),
    ).toBeVisible();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Compute properties BAT-000001",
    );
  });

  it("says what a run may cost, from its recent runs and its timeout", async () => {
    onProject({ run: ["self_hosted", "backend", "container"] });
    const RELAX: Schemas["ActionOut"] = {
      ...ACTION,
      tier: "container",
      estimate: {
        template: "container-small",
        price_per_hour: 1.8,
        currency: "USD",
        typical: 0.2,
        runs_seen: 12,
        timeout_cost: 0.3,
      },
    };
    answer("GET /api/v1/actions", [RELAX]);
    await renderApp(
      `/projects/kinase-atlas/runs/new/?entities=PRT-000001,PRT-000002&action=${ACTION.name}`,
    );
    expect(await screen.findByText(/^A run typically costs/)).toHaveTextContent(
      "A run typically costs about $0.20, $0.40 for all 2 (the median of its last 12 that succeeded), and at most $0.30 each if it runs for its whole timeout.",
    );
  });

  it("says what its machine costs an hour, before any run has", async () => {
    onProject({ run: ["self_hosted", "backend", "container"] });
    answer("GET /api/v1/actions", [
      {
        ...ACTION,
        tier: "container",
        estimate: {
          template: "container-small",
          price_per_hour: 1.8,
          currency: "USD",
          typical: null,
          runs_seen: 0,
          timeout_cost: 0.3,
        },
      },
    ]);
    await renderApp(`/projects/kinase-atlas/runs/new/?entities=PRT-000001&action=${ACTION.name}`);
    expect(await screen.findByText(/^Its machine costs/)).toHaveTextContent(
      "Its machine costs $1.80 an hour: at most $0.30 if it runs for its whole timeout.",
    );
  });

  it("imports from a file in the project, with no entities to choose", async () => {
    onProject();
    const IMPORT: Schemas["ActionOut"] = {
      ...ACTION,
      name: "iki/protein.import_proteins",
      label: "Import proteins",
      inputs: [],
      creates: [
        { type: "iki/protein.Protein", cardinality: "many", derived_from: [] },
        { type: "iki/protein.Protein", cardinality: "one", derived_from: [] },
      ],
      updates: [],
      network: ["rest.uniprot.org"],
      secrets: [
        {
          name: "UNIPROT_TOKEN",
          required: false,
          description: "",
          key: "iki__protein.UNIPROT_TOKEN",
          set: true,
        },
        {
          name: "UNSET_TOKEN",
          required: false,
          description: "",
          key: "iki__protein.UNSET_TOKEN",
          set: false,
        },
      ],
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
      agent: null,
      acquired: null,
      used_by: { records: 0, runs: 0 },
    };
    answer("GET /api/v1/files", page([fasta]));
    answer("GET /api/v1/files/count", { total: 1, more_than_total: false });
    answer("GET /api/v1/files/formats", [
      {
        id: "iki/core.protein-fasta",
        label: "protein FASTA",
        media_type: "chemical/seq-aa-fasta",
        count: 1,
      },
    ]);
    answer("POST /api/v1/actions/iki/protein.import_proteins/runs", () => run());
    answer("GET /api/v1/runs/RUN-000001", run());
    answer("GET /api/v1/runs/RUN-000001/attempts", []);
    const { user, router } = await renderApp("/projects/kinase-atlas/runs/new/");

    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Action" }),
      "Import proteins",
    );
    expect(await screen.findByText("It makes new proteins and one protein.")).toBeInTheDocument();
    expect(screen.getByText("It can reach rest.uniprot.org on the internet.")).toBeInTheDocument();
    // Only the secrets this deployment has set: the others it runs without.
    expect(screen.getByText(/^It gets/)).toHaveTextContent(
      "It gets UNIPROT_TOKEN from this deployment’s secrets, which its log never shows.",
    );
    const file = screen.getByRole("group", { name: /FASTA file/ });
    await user.click(screen.getByRole("button", { name: "Start run" }));
    expect(await within(file).findByText("Choose FASTA file.")).toBeInTheDocument();
    // The picker: the project's files, as its Files tab finds them, held to what the
    // param takes.
    await user.click(within(file).getByRole("button", { name: "Or choose one already here" }));
    const picker = await screen.findByRole("dialog", { name: "Choose a file for FASTA file" });
    expect(
      within(picker).getByText("It takes protein FASTA (.fasta, .fa or .faa) files."),
    ).toBeInTheDocument();
    await user.click(await within(picker).findByRole("radio", { name: "kinases.fasta" }));
    expect(calls("GET", "/api/v1/files").at(-1)?.query.getAll("accepts")).toEqual([
      "chemical/seq-aa-fasta",
    ]);
    expect(calls("GET", "/api/v1/files").at(-1)?.query.get("project")).toBe(KINASE.id);
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(within(file).getByRole("status")).toHaveTextContent("Chosen: kinases.fasta");
    await user.click(screen.getByRole("button", { name: "Start run" }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/runs/RUN-000001/");
    });
    expect(calls("POST", "/api/v1/actions/iki/protein.import_proteins/runs").at(-1)?.body).toEqual({
      project: KINASE.id,
      inputs: {},
      params: { file: FILE_ID },
      reason: "",
      reuse: true,
      add_to_project: false,
      labels: [],
      consent: [],
    });
  });

  it("narrows a self-hosted run to machines with the labels its launcher gives", async () => {
    const hplc: Schemas["ActionOut"] = {
      ...COMPUTE,
      tier: "self_hosted",
      environment: "chromatography",
      labels: ["chromeleon"],
    };
    onProject();
    answer("GET /api/v1/actions", [hplc]);
    answer(`POST /api/v1/actions/${ACTION.name}/runs`, () => run());
    answer("GET /api/v1/runs/RUN-000001", run());
    answer("GET /api/v1/runs/RUN-000001/attempts", []);
    const { user } = await renderApp("/projects/kinase-atlas/runs/new/?entities=PRT-000001");
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Action" }),
      "Compute properties",
    );
    expect(await screen.findByText(/^It runs on a machine/)).toHaveTextContent(
      "It runs on a machine its team registered with Iki labelled chromeleon, in the chromatography environment",
    );
    await user.type(screen.getByRole("textbox", { name: /Machine labels/ }), "Lab-3, bay-2");
    await user.click(screen.getByRole("button", { name: "Start run" }));
    await waitFor(() => {
      expect(calls("POST", `/api/v1/actions/${ACTION.name}/runs`).at(-1)?.body).toMatchObject({
        labels: ["lab-3", "bay-2"],
      });
    });
  });

  it("asks for machine labels only for self-hosted runs", async () => {
    onProject();
    const { user } = await renderApp("/projects/kinase-atlas/runs/new/?entities=PRT-000001");
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Action" }),
      "Compute properties",
    );
    expect(await screen.findByText("Weighs a protein from its sequence.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /Machine labels/ })).not.toBeInTheDocument();
  });

  it("says when the person can't run anything in the project", async () => {
    onProject({ run: [] });
    await renderApp("/projects/kinase-atlas/runs/new/?entities=PRT-000001");
    expect(screen.getByText(/You can’t run actions in Kinase Atlas\./)).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Action" })).not.toBeInTheDocument();
  });

  it("runs on the project's own record of an entry other projects hold too", async () => {
    // Registry entries are shared: DNA-000004 here is Kinase Atlas's record of pUC19, and
    // another project the person is in holds its own record of the same entry.
    withTypes();
    const atlas = project({ can: { ...KINASE.can, run: ["self_hosted", "backend"] } });
    answer("GET /api/v1/projects", page([atlas]));
    answer("GET /api/v1/actions", [COMPUTE]);
    const ours = entity();
    const theirs = entity({
      id: SECOND_ID,
      values: { name: "Their ABL1" },
      owner_project: { id: "00000000-0000-7000-8000-0000000000f2", name: "Other", slug: "other" },
    });
    answer("POST /api/v1/entities/search", (r: { body: { project?: string } }) =>
      page(r.body.project === KINASE.id ? [ours] : [ours, theirs]),
    );
    await renderApp(`/projects/kinase-atlas/runs/new/?entities=PRT-000001&action=${ACTION.name}`);
    const protein = await screen.findByRole("group", { name: "Protein" });
    expect(within(protein).getAllByRole("listitem")).toHaveLength(1);
    expect(within(protein).getByRole("listitem")).toHaveTextContent("PRT-000001 ABL1 kinase");
    expect(screen.getByRole("button", { name: "Start run" })).toBeInTheDocument();
    expect(calls("POST", "/api/v1/entities/search").map((c) => c.body)).toEqual([
      {
        where: { field: "hrid", op: "in", value: ["PRT-000001"] },
        project: KINASE.id,
        exact_type: false,
        limit: 100,
      },
    ]);
  });

  it("looks in other projects for what this one doesn't hold, and never guesses", async () => {
    withTypes();
    const atlas = project({ can: { ...KINASE.can, run: ["self_hosted", "backend"] } });
    answer("GET /api/v1/projects", page([atlas]));
    answer("GET /api/v1/actions", [COMPUTE]);
    const elsewhere = (id: string, hrid: string, slug: string) =>
      entity({
        id,
        hrid,
        owner_project: { id: `00000000-0000-7000-8000-0000000000${slug}`, name: slug, slug },
      });
    answer("POST /api/v1/entities/search", (r: { body: { project?: string } }) =>
      page(
        r.body.project
          ? []
          : [
              elsewhere(PROTEIN_ID, "PRT-000001", "f2"),
              elsewhere(SECOND_ID, "PRT-000002", "f2"),
              elsewhere("00000000-0000-7000-8000-00000000e003", "PRT-000002", "f3"),
            ],
      ),
    );
    await renderApp(
      `/projects/kinase-atlas/runs/new/?entities=PRT-000001,PRT-000002&action=${ACTION.name}`,
    );
    expect(
      await screen.findByText(
        "PRT-000002 is in more than one of your other projects, so it’s left out: add the " +
          "record you mean to Kinase Atlas, then start the run here.",
      ),
    ).toBeInTheDocument();
    const protein = screen.getByRole("group", { name: "Protein" });
    expect(within(protein).getAllByRole("listitem")).toHaveLength(1);
    expect(within(protein).getByRole("listitem")).toHaveTextContent("PRT-000001");
  });

  it("says why an action it's opened for can't run here, before anything is filled in", async () => {
    withTypes();
    const atlas = project({ can: { ...KINASE.can, run: ["self_hosted", "backend"] } });
    answer("GET /api/v1/projects", page([atlas]));
    const why =
      "It needs the iki/dna plugin's `ncbi_email` setting, which this deployment hasn't " +
      "set. An Iki administrator can set it in the plugin's settings, in the deployment's " +
      "config repository.";
    const FETCH: Schemas["ActionOut"] = {
      ...ACTION,
      name: "iki/dna.fetch_sequences",
      label: "Fetch DNA sequences from NCBI",
      inputs: [],
      available: false,
      unavailable_reason: why,
    };
    answer("GET /api/v1/actions", [COMPUTE, FETCH]);
    await renderApp("/projects/kinase-atlas/runs/new/?action=iki/dna.fetch_sequences");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(`Fetch DNA sequences from NCBI can’t run here. ${why}`);
    expect(screen.queryByRole("button", { name: "Start run" })).not.toBeInTheDocument();
    expect(screen.getByText("1 more action you can’t run here")).toBeInTheDocument();
  });

  it("says which entities it can't read", async () => {
    onProject();
    await renderApp("/projects/kinase-atlas/runs/new/?entities=PRT-000001,PRT-999999");
    expect(
      await screen.findByText("PRT-999999 isn’t an entity you can read, so it’s left out."),
    ).toBeInTheDocument();
  });
});
