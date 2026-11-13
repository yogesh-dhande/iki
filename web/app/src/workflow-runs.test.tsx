/**
 * A workflow run's page, against a fake API: its steps, how far they've got, and
 * cancelling it (ADR 0079).
 */

import { act, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { KINASE, renderApp, withTypes } from "./entity-fixtures";
import { ACTION, run, WORKFLOW_ID, workflowRun } from "./run-fixtures";
import { answer, calls, FakeEventSource, page } from "./test-server";

const EVENTS = "/api/v1/workflow-runs/WF-000001/events";

function onWorkflow(found: () => ReturnType<typeof workflowRun>): void {
  withTypes();
  answer("GET /api/v1/projects", page([KINASE]));
  answer("GET /api/v1/actions", [ACTION]);
  answer("GET /api/v1/workflow-runs/WF-000001", () => found());
}

function stream(): FakeEventSource {
  const opened = FakeEventSource.opened.filter((s) => s.url === EVENTS).at(-1);
  if (!opened) throw new Error("The workflow run's page has no stream open");
  return opened;
}

beforeEach(() => {
  FakeEventSource.opened = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a workflow run's page", () => {
  it("shows its steps in order, each with its run", async () => {
    onWorkflow(workflowRun);
    await renderApp("/workflow-runs/WF-000001/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Translate and characterize WF-000001",
    );
    const summary = screen.getByRole("region", { name: "Running" });
    expect(within(summary).getByRole("progressbar", { name: "WF-000001 progress" })).toHaveValue(1);
    expect(summary).toHaveTextContent("1 of 2 steps finished");
    expect(summary).toHaveTextContent("The kinase panel");
    expect(within(summary).getByRole("link", { name: "DNA-000001" })).toHaveAttribute(
      "href",
      "/entities/DNA-000001/",
    );
    expect(within(summary).getByRole("list", { name: "Params" })).toHaveTextContent("frame: 1");
    const steps = screen.getByRole("region", { name: "Steps" });
    const links = within(steps).getAllByRole("link");
    expect(links.map((l) => [l.textContent, l.getAttribute("href")])).toEqual([
      ["Translate", "/runs/RUN-000001/"],
      ["Properties", "/runs/RUN-000002/"],
    ]);
    // A running step shows how its run is getting on.
    const states = within(steps)
      .getAllByRole("listitem")
      .map((li) => li.querySelector(".badge")?.textContent);
    expect(states).toEqual(["Succeeded", "Queued"]);
    expect(steps).toHaveTextContent("after translate");
    expect(stream().init).toEqual({ withCredentials: true });
  });

  it("links a step run for each entity to its batch", async () => {
    const [translate, properties] = workflowRun().steps;
    if (!translate || !properties) throw new Error("The fixture has two steps");
    onWorkflow(() =>
      workflowRun({
        steps: [
          translate,
          {
            ...properties,
            each: "protein",
            run: null,
            batch: {
              id: "00000000-0000-7000-8000-0000000000b1",
              hrid: "BAT-000001",
              size: 3,
              statuses: { succeeded: 1, running: 2 },
            },
          },
        ],
      }),
    );
    await renderApp("/workflow-runs/WF-000001/");
    const steps = screen.getByRole("region", { name: "Steps" });
    expect(within(steps).getByRole("link", { name: "Properties" })).toHaveAttribute(
      "href",
      "/batches/BAT-000001/",
    );
    expect(steps).toHaveTextContent(
      "protein.compute_properties for each protein · BAT-000001, 1 of 3 runs finished",
    );
  });

  it("says what a step's condition is, and why it was skipped", async () => {
    const [translate, properties] = workflowRun().steps;
    if (!translate || !properties) throw new Error("The fixture has two steps");
    onWorkflow(() =>
      workflowRun({
        status: "succeeded",
        finished_at: "2026-03-14T09:02:00Z",
        can: { cancel: false },
        steps: [
          translate,
          {
            ...properties,
            state: "skipped",
            run: null,
            when: [
              {
                value: "$steps.translate.metrics.records",
                operator: "at_least",
                operand: "$params.min_records",
              },
              { value: "$params.mode", operator: "in", operand: ["fast", "$$cheap"] },
            ],
            error: {
              code: "condition_false",
              message:
                "Its condition didn't hold: `$steps.translate.metrics.records` is 0, and it must be at least 1.",
            },
          },
        ],
      }),
    );
    await renderApp("/workflow-runs/WF-000001/");
    const steps = screen.getByRole("region", { name: "Steps" });
    expect(steps).toHaveTextContent(
      'Runs if translate.records is at least min_records and mode is one of "fast", "$cheap"',
    );
    expect(steps).toHaveTextContent("Its condition didn't hold");
    expect(within(steps).queryByRole("link", { name: "Properties" })).toBeNull();
    const states = within(steps)
      .getAllByRole("listitem")
      .map((li) => li.querySelector(".badge")?.textContent);
    expect(states).toEqual(["Succeeded", "Skipped"]);
  });

  it("reads it again when it changes, until it ends", async () => {
    let current = workflowRun();
    onWorkflow(() => current);
    await renderApp("/workflow-runs/WF-000001/");
    current = workflowRun({
      status: "failed",
      finished_at: "2026-03-14T09:02:00Z",
      can: { cancel: false },
      error: {
        code: "step_failed",
        step: "properties",
        message: "Step `properties` didn't succeed. Its run, RUN-000002, failed.",
      },
      steps: workflowRun().steps.map((s) =>
        s.id === "properties"
          ? {
              ...s,
              state: "failed" as const,
              run: s.run ? { ...s.run, status: "failed" as const } : null,
              error: { code: "run_failed", message: "Its run, RUN-000002, failed." },
            }
          : s,
      ),
    });
    const opened = stream();
    act(() => {
      opened.send("workflow", { id: WORKFLOW_ID });
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("Step `properties` didn't succeed.");
    expect(screen.getByRole("region", { name: "Failed" })).toHaveTextContent("Finished");
    expect(screen.getByRole("region", { name: "Steps" })).toHaveTextContent(
      "Its run, RUN-000002, failed.",
    );
    await waitFor(() => {
      expect(opened.readyState).toBe(FakeEventSource.CLOSED);
    });
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });

  it("cancels it, with a reason for the record", async () => {
    onWorkflow(workflowRun);
    answer(`POST /api/v1/workflow-runs/${WORKFLOW_ID}/cancel`, () =>
      workflowRun({ cancel_requested_at: "2026-03-14T09:01:00Z", can: { cancel: false } }),
    );
    const { user } = await renderApp("/workflow-runs/WF-000001/");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    const dialog = screen.getByRole("alertdialog", { name: "Cancel WF-000001?" });
    expect(dialog).toHaveTextContent(/Steps that have finished keep their results/);
    await user.type(within(dialog).getByRole("textbox", { name: /Reason/ }), "Wrong sequence");
    await user.click(within(dialog).getByRole("button", { name: "Cancel it" }));
    expect(
      await screen.findByText("Cancelling WF-000001. Workers stop the steps they have."),
    ).toBeVisible();
    expect(calls("POST").at(-1)?.body).toEqual({ reason: "Wrong sequence" });
    expect(screen.getByRole("region", { name: "Running: cancelling" })).toBeInTheDocument();
  });

  it("says when there's no such workflow run", async () => {
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
    await renderApp("/workflow-runs/WF-000009/");
    expect(
      screen.getByText("There’s nothing here, or you don’t have access to it."),
    ).toBeInTheDocument();
  });
});

describe("a run that's a workflow's step", () => {
  it("links to its workflow run", async () => {
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/actions", [ACTION]);
    const step = run({ workflow: { id: WORKFLOW_ID, hrid: "WF-000001", step: "properties" } });
    answer("GET /api/v1/runs/RUN-000001", step);
    answer("GET /api/v1/runs/RUN-000001/attempts", []);
    answer("GET /api/v1/runs/RUN-000001/artifacts", []);
    await renderApp("/runs/RUN-000001/");
    const link = await screen.findByRole("link", { name: "WF-000001" });
    expect(link).toHaveAttribute("href", "/workflow-runs/WF-000001/");
    expect(link.closest("dd")).toHaveTextContent("Step properties of WF-000001");
  });
});
