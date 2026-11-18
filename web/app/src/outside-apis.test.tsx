/**
 * Outside APIs in the web app (ADR 0130), against a fake API: agreeing to what a launch
 * sends, refusing to start one an Org Admin hasn't approved, a parked run and its calls on
 * its page, and approving an API for a project in Settings.
 */

import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Schemas } from "@ikisci/sdk";

import { entity, KINASE, PROTEIN_ID, renderApp, withTypes } from "./entity-fixtures";
import { ACTION, approval, run } from "./run-fixtures";
import { answer, calls, page, project } from "./test-server";

const API = "iki/example_models.foldcloud";

const STATEMENT: Schemas["ConsentStatementOut"] = {
  api: API,
  label: "FoldCloud",
  host: "api.foldcloud.example",
  purpose: "Predicts protein structures from their sequences.",
  inputs: { protein: ["sequence"] },
  params: ["model"],
  statement:
    "This sends your data to FoldCloud (api.foldcloud.example): each Protein’s sequence and the model param. Predicts protein structures from their sequences.",
};

const PREDICT: Schemas["ActionOut"] = {
  ...ACTION,
  name: "iki/example_models.predict_structure",
  label: "Predict a structure with FoldCloud",
  params: [],
  apis: [{ ...STATEMENT, priced: true, parks: true }],
};

function outsideApi(approved: boolean): Schemas["OutsideApiOut"] {
  return {
    api: API,
    plugin: "iki/example_models",
    label: "FoldCloud",
    host: "api.foldcloud.example",
    purpose: STATEMENT.purpose,
    credential: {
      key: "iki__example_models.FOLDCLOUD_API_KEY",
      set: true,
      header: "Authorization",
      description: "",
    },
    jobs: true,
    webhook: true,
    price: { per_call: 0.01, usage: { gpu_seconds: 0.0011 }, currency: "USD" },
    actions: [{ action: PREDICT.name, label: PREDICT.label, statement: STATEMENT.statement }],
    approvals: approved
      ? [
          {
            project: KINASE.id,
            project_name: KINASE.name,
            host: "api.foldcloud.example",
            purpose: STATEMENT.purpose,
            note: "Our DPA covers it.",
            approved_by: { id: "00000000-0000-7000-8000-0000000000u9", name: "Grace Hopper" },
            approved_at: "2026-03-10T09:00:00Z",
            current: true,
          },
        ]
      : [],
  };
}

function onLaunch(approved: boolean): void {
  withTypes();
  const atlas = project({ can: { ...KINASE.can, run: ["self_hosted", "backend"] } });
  answer("GET /api/v1/projects", page([atlas]));
  answer("GET /api/v1/actions", [PREDICT]);
  answer("POST /api/v1/entities/search", page([entity()]));
  answer(`GET /api/v1/projects/${KINASE.id}/outside-apis`, [outsideApi(approved)]);
}

const LAUNCH = `/projects/kinase-atlas/runs/new/?entities=PRT-000001&action=${PREDICT.name}`;

describe("launching a run that sends data outside Iki", () => {
  it("says what it sends and where, and starts only once the person agrees", async () => {
    onLaunch(true);
    answer(`POST /api/v1/actions/${PREDICT.name}/runs`, () => run({ action: PREDICT.name }));
    answer("GET /api/v1/runs/RUN-000001", run({ action: PREDICT.name }));
    answer("GET /api/v1/runs/RUN-000001/attempts", []);
    answer("GET /api/v1/runs/RUN-000001/artifacts", []);
    const { user, router } = await renderApp(LAUNCH);
    const sends = await screen.findByRole("group", { name: "It sends data outside Iki" });
    expect(sends).toHaveTextContent(STATEMENT.statement);
    expect(screen.getByText(/^Its calls to outside APIs cost money/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Start run" }));
    expect(
      await within(sends).findByText("Agree to send this data, or don’t start the run."),
    ).toBeVisible();
    expect(calls("POST", `/api/v1/actions/${PREDICT.name}/runs`)).toHaveLength(0);

    await user.click(
      within(sends).getByRole("checkbox", {
        name: "I agree to send this to FoldCloud (api.foldcloud.example)",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Start run" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/runs/RUN-000001/");
    });
    expect(calls("POST", `/api/v1/actions/${PREDICT.name}/runs`).at(-1)?.body).toMatchObject({
      inputs: { protein: PROTEIN_ID },
      consent: [API],
    });
  });

  it("can’t start until an Org Admin approves the API for the project", async () => {
    onLaunch(false);
    await renderApp(LAUNCH);
    const sends = await screen.findByRole("group", { name: "It sends data outside Iki" });
    expect(await within(sends).findByRole("alert")).toHaveTextContent(
      "FoldCloud (api.foldcloud.example) isn’t approved for Kinase Atlas, so this can’t start. An Org Admin approves outside APIs for each project, in Settings → Outside APIs.",
    );
    expect(within(sends).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start run" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });
});

describe("a run that calls an outside API", () => {
  const parked = run({
    action: PREDICT.name,
    status: "parked",
    apis: [
      {
        api: API,
        label: "FoldCloud",
        host: "api.foldcloud.example",
        purpose: STATEMENT.purpose,
      },
    ],
    consent: {
      id: "00000000-0000-7000-8000-0000000000c1",
      given_by: { id: "00000000-0000-7000-8000-0000000000u1", name: "Ada Lovelace" },
      given_at: "2026-03-14T09:00:00Z",
      via: "web",
      agent: null,
      statements: [STATEMENT],
    },
    park: {
      api: API,
      job: "pred_1a2b3c",
      status: "running",
      message: "",
      outcome: "",
      polls: 2,
      parked_at: "2026-03-14T09:00:10Z",
      last_polled_at: "2026-03-14T09:01:10Z",
      next_poll_at: "2099-03-14T09:01:40Z",
      deadline: "2099-03-15T09:00:10Z",
      ended_at: null,
    },
  });

  it("shows the job it waits on, who agreed, and each call with its model", async () => {
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/actions", [PREDICT]);
    answer("GET /api/v1/runs/RUN-000001", parked);
    answer("GET /api/v1/runs/RUN-000001/attempts", []);
    answer("GET /api/v1/runs/RUN-000001/artifacts", []);
    answer(
      "GET /api/v1/runs/RUN-000001/api-calls",
      page([
        {
          id: 1,
          attempt: 1,
          api: API,
          host: "api.foldcloud.example",
          kind: "call",
          method: "POST",
          path: "/v1/predictions",
          status: 201,
          error: "",
          model: "fold-2",
          version: "2.1.0",
          parameters: { "/model": "fold-2" },
          usage: {},
          request_bytes: 64,
          response_bytes: 80,
          request_sha256: "a".repeat(64),
          response_sha256: "b".repeat(64),
          started_at: "2026-03-14T09:00:05Z",
          duration_ms: 120,
          cost: 0.01,
          currency: "USD",
        },
      ]),
    );
    await renderApp("/runs/RUN-000001/");
    expect(await screen.findByText("Waiting on FoldCloud")).toBeInTheDocument();
    expect(screen.getByText(/^Its job/)).toHaveTextContent(
      /Its job pred_1a2b3c started .*No machine is held while it waits\./,
    );
    const section = screen.getByRole("region", { name: "Outside APIs" });
    expect(section).toHaveTextContent("By Ada Lovelace");
    expect(section).toHaveTextContent(STATEMENT.statement);
    const row = (await within(section).findByText("fold-2")).closest("tr");
    expect(row).toHaveTextContent("Call to FoldCloud");
    expect(row).toHaveTextContent("POST /v1/predictions");
    expect(row).toHaveTextContent("2.1.0");
    expect(row).toHaveTextContent("aaaaaaaaaaaa…");
    expect(row).toHaveTextContent("$0.01");
  });

  it("asks the person approving an agent’s run to agree to what it sends", async () => {
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/actions", [PREDICT]);
    answer(
      "GET /api/v1/runs/RUN-000001",
      run({
        action: PREDICT.name,
        status: "awaiting_approval",
        approval: approval({ consent: [STATEMENT] }),
      }),
    );
    answer("GET /api/v1/runs/RUN-000001/attempts", []);
    answer("GET /api/v1/runs/RUN-000001/artifacts", []);
    await renderApp("/runs/RUN-000001/");
    const request = await screen.findByRole("region", { name: "Waiting for your approval" });
    expect(request).toHaveTextContent(
      "It sends data outside Iki, to FoldCloud (api.foldcloud.example), which an AI agent can’t agree to for anyone",
    );
    expect(request).toHaveTextContent("Approving agrees, for you, to what it sends:");
    expect(request).toHaveTextContent(STATEMENT.statement);
  });
});

describe("Settings → Outside APIs", () => {
  it("lists each API with its key, price and approvals, and approves one for a project", async () => {
    const other = project({
      id: "00000000-0000-7000-8000-0000000000p2",
      slug: "assays",
      name: "Assays",
    });
    withTypes();
    answer("GET /api/v1/projects", page([KINASE, other]));
    answer("GET /api/v1/outside-apis", [outsideApi(true)]);
    answer(`POST /api/v1/outside-apis/${API}/approvals`, () => ({
      ...outsideApi(true).approvals[0],
      project: other.id,
      project_name: "Assays",
    }));
    const { user } = await renderApp("/settings/outside-apis/");
    const card = await screen.findByRole("region", { name: /FoldCloud/ });
    expect(card).toHaveTextContent("Set, as iki__example_models.FOLDCLOUD_API_KEY");
    expect(card).toHaveTextContent("$0.01 a call");
    expect(within(card).getByRole("table", { name: "Approved for" })).toHaveTextContent(
      "Kinase Atlas",
    );

    await user.click(within(card).getByRole("button", { name: "Approve for a project" }));
    const dialog = await screen.findByRole("dialog", { name: "Approve FoldCloud for a project" });
    // Only projects it isn't approved for yet.
    expect(
      within(dialog)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["Assays"]);
    await user.type(within(dialog).getByRole("textbox", { name: /Note/ }), "DPA signed");
    await user.click(within(dialog).getByRole("button", { name: "Approve" }));
    expect(await screen.findByText("Approved FoldCloud for Assays.")).toBeVisible();
    expect(calls("POST", `/api/v1/outside-apis/${API}/approvals`).at(-1)?.body).toEqual({
      project: other.id,
      note: "DPA signed",
    });
  });
});
