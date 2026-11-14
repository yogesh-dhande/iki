/**
 * A project's workflows, against a fake API: its workflow runs, and starting one
 * (ADR 0079).
 */

import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { KINASE, renderApp, withTypes } from "./entity-fixtures";
import { ACTION, WORKFLOW, workflowRun } from "./run-fixtures";
import { answer, calls, page, project } from "./test-server";
import type { Schemas } from "@ikisci/sdk";

const ON_MACHINES: Schemas["WorkflowOut"] = {
  ...WORKFLOW,
  name: "structure_prediction.predict_all",
  label: "Predict all",
  tiers: ["backend", "vm"],
};

const OFFLINE: Schemas["WorkflowOut"] = {
  ...WORKFLOW,
  name: "protein.relax_all",
  label: "Relax all",
  available: false,
  unavailable_reason: "Step `relax` can't run here. There are no backend workers.",
};

function onProject(can: Partial<Schemas["ProjectOut"]["can"]> = {}): void {
  withTypes();
  const atlas = project({ can: { ...KINASE.can, run: ["client", "backend"], ...can } });
  answer("GET /api/v1/projects", page([atlas]));
  answer("GET /api/v1/actions", [ACTION]);
  answer("GET /api/v1/workflows", [WORKFLOW, ON_MACHINES, OFFLINE]);
}

describe("a project's workflows", () => {
  it("lists its workflow runs, newest first", async () => {
    onProject();
    answer("GET /api/v1/workflow-runs", page([workflowRun({ reason: "Kinase panel" })]));
    await renderApp("/projects/kinase-atlas/workflows/");
    const runs = await screen.findByRole("list", { name: "Workflow runs" });
    const link = within(runs).getByRole("link", { name: "Translate and characterize" });
    expect(link).toHaveAttribute("href", "/workflow-runs/WF-000001/");
    expect(runs).toHaveTextContent("WF-000001 · 1 of 2 steps finished");
    expect(runs).toHaveTextContent("“Kinase panel”");
    expect(within(runs).getByText("Running")).toBeInTheDocument();
    expect(calls("GET", "/api/v1/workflow-runs").at(-1)?.query.get("project")).toBe(KINASE.id);
    expect(screen.getByRole("link", { name: "Run a workflow" })).toHaveAttribute(
      "href",
      "/projects/kinase-atlas/workflows/new/",
    );
  });

  it("says when there are none", async () => {
    onProject({ run: [] });
    answer("GET /api/v1/workflow-runs", page([]));
    await renderApp("/projects/kinase-atlas/workflows/");
    expect(await screen.findByText(/No workflow runs yet/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Run a workflow" })).not.toBeInTheDocument();
  });

  it("starts one, after showing its steps", async () => {
    onProject();
    answer(`POST /api/v1/workflows/${WORKFLOW.name}/runs`, () => workflowRun());
    answer("GET /api/v1/workflow-runs/WF-000001", workflowRun());
    const { user, router } = await renderApp("/projects/kinase-atlas/workflows/new/");
    const chooser = await screen.findByRole("combobox", { name: "Workflow" });
    // Only those whose steps the person may run here, on this deployment.
    expect(
      within(chooser)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["Choose a workflow", "Weigh twice"]);
    expect(screen.getByText("2 more workflows you can’t run here")).toBeInTheDocument();
    expect(screen.getByText(/Predict all: its steps need the Runner role/)).toBeInTheDocument();
    expect(screen.getByText(/Relax all: Step `relax` can't run here/)).toBeInTheDocument();

    await user.selectOptions(chooser, "Weigh twice");
    const steps = await screen.findByRole("list", { name: "Steps" });
    expect(steps).toHaveTextContent("SecondCompute properties · after first");
    expect(steps).toHaveTextContent("Waits for approval by a Runner or above, other than you");
    await user.type(screen.getByRole("textbox", { name: /Reason/ }), "Check the weights");
    await user.click(screen.getByRole("button", { name: "Start workflow" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/workflow-runs/WF-000001/");
    });
    expect(calls("POST", `/api/v1/workflows/${WORKFLOW.name}/runs`).at(-1)?.body).toEqual({
      project: KINASE.id,
      inputs: {},
      params: { ph: 7 },
      reason: "Check the weights",
      add_to_project: false,
    });
    expect(await screen.findByText("Started WF-000001.")).toBeInTheDocument();
  });

  it("says when the person can't run workflows in the project", async () => {
    onProject({ run: [] });
    await renderApp("/projects/kinase-atlas/workflows/new/");
    expect(await screen.findByText(/You can’t run workflows in Kinase Atlas/)).toBeInTheDocument();
  });
});
