/**
 * A project's triggers, against a fake API: which are on, as whom; turning them on and
 * off; and what they fired (ADR 0080).
 */

import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { KINASE, renderApp, withTypes } from "./entity-fixtures";
import { answer, calls, page, project } from "./test-server";
import type { Schemas } from "@ikisci/sdk";

const OFF: Schemas["ProjectTriggerOut"] = {
  name: "protein.compute_new_protein_properties",
  plugin: "protein",
  label: "Compute each new protein's properties",
  description: null,
  on: "entity.created",
  type: "protein.Protein",
  schedule: "",
  timezone: "",
  fields: [],
  action: "protein.compute_properties",
  action_label: "Compute properties",
  input: "protein",
  params: {},
  tier: "backend",
  enabled: false,
  enabled_by: null,
  enabled_at: null,
  run_as: null,
  next_run_at: null,
  run_as_choices: [],
  disabled_by: null,
  disabled_at: null,
  disabled_reason: "",
  last_fired_at: null,
  why_not_enable: null,
  can: { enable: true, disable: false },
};

const ON: Schemas["ProjectTriggerOut"] = {
  ...OFF,
  enabled: true,
  enabled_by: { id: "00000000-0000-7000-8000-00000000a11c", name: "Alice Admin" },
  enabled_at: "2026-03-14T09:00:00Z",
  last_fired_at: "2026-03-14T10:00:00Z",
  can: { enable: true, disable: true },
};

const RELAX: Schemas["ProjectTriggerOut"] = {
  ...OFF,
  name: "protein.relax_changed_structures",
  label: "Relax changed structures",
  on: "entity.updated",
  type: "protein.ProteinStructure",
  fields: ["coordinates", "chains"],
  action: "protein.relax_structure",
  action_label: "Relax structure",
  tier: "container",
};

const ROBOT = { id: "00000000-0000-7000-8000-0000000000b7", name: "Nightly robot" };

const NIGHTLY: Schemas["ProjectTriggerOut"] = {
  ...OFF,
  name: "protein.fetch_hemoglobin_nightly",
  label: "Fetch hemoglobin nightly",
  on: "schedule",
  type: "",
  schedule: "0 3 * * *",
  timezone: "Europe/Zurich",
  action: "protein.fetch_proteins",
  action_label: "Fetch proteins",
  input: "",
  run_as_choices: [{ id: "00000000-0000-7000-8000-0000000000b6", name: "Ingest bot" }, ROBOT],
};

const FIRING: Schemas["FiringOut"] = {
  id: "00000000-0000-7000-8000-0000000000f1",
  trigger: OFF.name,
  label: OFF.label,
  event: "entity.created",
  entity: { id: "00000000-0000-7000-8000-0000000000e1", hrid: "PRT-000007", type: OFF.type },
  revision: 1,
  scheduled_for: null,
  since: null,
  state: "launched",
  run: { id: "00000000-0000-7000-8000-0000000000r9", hrid: "RUN-000009", status: "queued" },
  error: null,
  created_at: "2026-03-14T10:00:00Z",
  fired_at: "2026-03-14T10:00:01Z",
};

function must<T>(value: T | undefined): T {
  if (value === undefined) throw new Error("missing");
  return value;
}

function onProject(triggers: Schemas["ProjectTriggerOut"][], firings = [FIRING]): void {
  withTypes();
  answer("GET /api/v1/projects", page([project()]));
  answer(`GET /api/v1/projects/${KINASE.id}/triggers`, triggers);
  answer(`GET /api/v1/projects/${KINASE.id}/trigger-firings`, page(firings));
}

describe("a project's triggers", () => {
  it("says what each fires, whether it's on and as whom", async () => {
    onProject([ON, RELAX]);
    await renderApp("/projects/kinase-atlas/triggers/");
    const rows = await screen.findByRole("list", { name: "Triggers" });
    const [first, second] = within(rows).getAllByRole("listitem");
    expect(first).toHaveTextContent(
      "Each new protein in Kinase Atlas starts a run of Compute properties.",
    );
    expect(first).toHaveTextContent(/On since .*, running as Alice Admin · last fired/);
    expect(within(must(first)).getByText("On")).toBeInTheDocument();
    expect(within(must(first)).getByRole("button", { name: "Run as me" })).toBeInTheDocument();
    expect(second).toHaveTextContent(
      "A change to a protein structure’s coordinates or chains in Kinase Atlas starts a run of " +
        "Relax structure, on the container tier: each waits for approval.",
    );
    expect(within(must(second)).getByText("Off")).toBeInTheDocument();
  });

  it("turns one on, saying it runs as you", async () => {
    onProject([OFF]);
    answer(`POST /api/v1/projects/${KINASE.id}/triggers/${OFF.name}/enable`, () => ({
      ...ON,
      can: { enable: false, disable: true },
    }));
    const { user } = await renderApp("/projects/kinase-atlas/triggers/");
    await user.click(await screen.findByRole("button", { name: "Turn on" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(
      "Each new protein in Kinase Atlas will start a run of Compute properties, launched as you",
    );
    expect(dialog).toHaveTextContent("start without asking");
    await user.click(within(dialog).getByRole("button", { name: "Turn on" }));
    await waitFor(() => {
      expect(
        calls("POST", `/api/v1/projects/${KINASE.id}/triggers/${OFF.name}/enable`),
      ).toHaveLength(1);
    });
    expect(
      await screen.findByText("“Compute each new protein's properties” is on, and runs as you."),
    ).toBeInTheDocument();
  });

  it("turns one off", async () => {
    onProject([{ ...ON, can: { enable: false, disable: true } }]);
    answer(`POST /api/v1/projects/${KINASE.id}/triggers/${OFF.name}/disable`, () => OFF);
    const { user } = await renderApp("/projects/kinase-atlas/triggers/");
    await user.click(await screen.findByRole("button", { name: "Turn off" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Runs it already started carry on");
    await user.click(within(dialog).getByRole("button", { name: "Turn off" }));
    await waitFor(() => {
      expect(
        calls("POST", `/api/v1/projects/${KINASE.id}/triggers/${OFF.name}/disable`),
      ).toHaveLength(1);
    });
  });

  it("says why Iki turned one off, and why you can't turn it on", async () => {
    onProject([
      {
        ...OFF,
        enabled_by: ON.enabled_by,
        enabled_at: ON.enabled_at,
        disabled_at: "2026-03-15T09:00:00Z",
        disabled_reason:
          "Alice Admin can no longer run `protein.compute_properties` in Kinase Atlas.",
        why_not_enable:
          "Its runs would be launched as you, and you can't run backend-tier actions in this project.",
        can: { enable: false, disable: false },
      },
    ]);
    await renderApp("/projects/kinase-atlas/triggers/");
    const rows = await screen.findByRole("list", { name: "Triggers" });
    expect(rows).toHaveTextContent(/Iki turned it off .*: Alice Admin can no longer run/);
    expect(rows).toHaveTextContent("You can’t turn it on: Its runs would be launched as you");
    expect(within(rows).queryByRole("button")).not.toBeInTheDocument();
  });

  it("says when a scheduled one runs, as which service account", async () => {
    onProject([
      {
        ...NIGHTLY,
        enabled: true,
        enabled_by: ON.enabled_by,
        enabled_at: ON.enabled_at,
        run_as: ROBOT,
        next_run_at: "2026-03-15T02:00:00Z",
        can: { enable: true, disable: true },
      },
    ]);
    await renderApp("/projects/kinase-atlas/triggers/");
    const rows = await screen.findByRole("list", { name: "Triggers" });
    const [row] = within(rows).getAllByRole("listitem");
    expect(row).toHaveTextContent(
      "On the schedule 0 3 * * * (Europe/Zurich), it starts a run of Fetch proteins in Kinase Atlas.",
    );
    expect(row).toHaveTextContent(/running as Nightly robot on Alice Admin’s authority · next /);
    expect(within(must(row)).getByRole("button", { name: "Change" })).toBeInTheDocument();
  });

  it("turns a scheduled one on as the service account you choose", async () => {
    onProject([NIGHTLY]);
    const path = `/api/v1/projects/${KINASE.id}/triggers/${NIGHTLY.name}/enable`;
    answer(`POST ${path}`, () => ({ ...NIGHTLY, enabled: true, run_as: ROBOT }));
    const { user } = await renderApp("/projects/kinase-atlas/triggers/");
    await user.click(await screen.findByRole("button", { name: "Turn on" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("as the service account you choose, on your authority");
    expect(dialog).toHaveTextContent("One never starts while the last is still running");
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Runs as" }), ROBOT.name);
    await user.click(within(dialog).getByRole("button", { name: "Turn on" }));
    await waitFor(() => {
      expect(calls("POST", path)).toHaveLength(1);
    });
    expect(calls("POST", path)[0]?.body).toEqual({ run_as: ROBOT.id });
    expect(
      await screen.findByText("“Fetch hemoglobin nightly” is on, and runs as Nightly robot."),
    ).toBeInTheDocument();
  });

  it("lists a scheduled firing by the time it was for", async () => {
    onProject(
      [NIGHTLY],
      [
        {
          ...FIRING,
          trigger: NIGHTLY.name,
          label: NIGHTLY.label,
          event: "schedule",
          entity: null,
          revision: null,
          scheduled_for: "2026-03-14T02:00:00Z",
          since: "2026-03-13T02:00:00Z",
        },
        {
          ...FIRING,
          id: "f3",
          trigger: NIGHTLY.name,
          label: NIGHTLY.label,
          event: "schedule",
          entity: null,
          revision: null,
          scheduled_for: "2026-03-15T02:00:00Z",
          since: null,
          state: "skipped",
          run: null,
          error: { code: "still_running", message: "Its last run hadn't finished." },
        },
      ],
    );
    await renderApp("/projects/kinase-atlas/triggers/");
    const firings = await screen.findByRole("list", { name: "Firings" });
    const [launched, skipped] = within(firings).getAllByRole("listitem");
    expect(launched).toHaveTextContent(/Scheduled for .*since the run for/);
    expect(launched).not.toHaveTextContent("revision");
    expect(skipped).toHaveTextContent("Its last run hadn't finished.");
  });

  it("lists what they fired, and narrows it to one trigger", async () => {
    onProject(
      [ON],
      [
        FIRING,
        {
          ...FIRING,
          id: "f2",
          state: "failed",
          run: null,
          error: { code: "invalid", message: "Its run couldn't start: no." },
        },
      ],
    );
    const { user } = await renderApp("/projects/kinase-atlas/triggers/");
    const firings = await screen.findByRole("list", { name: "Firings" });
    const [launched, failed] = within(firings).getAllByRole("listitem");
    expect(within(must(launched)).getByRole("link", { name: "PRT-000007" })).toHaveAttribute(
      "href",
      "/entities/PRT-000007/",
    );
    expect(launched).toHaveTextContent("PRT-000007 was created");
    expect(within(must(launched)).getByRole("link", { name: "RUN-000009" })).toHaveAttribute(
      "href",
      "/runs/RUN-000009/",
    );
    expect(within(must(launched)).getByText("Launched")).toBeInTheDocument();
    expect(failed).toHaveTextContent("Its run couldn't start: no.");
    expect(within(must(failed)).getByText("Failed")).toBeInTheDocument();

    await user.selectOptions(screen.getByRole("combobox", { name: "Trigger" }), ON.label);
    await waitFor(() => {
      expect(
        calls("GET", `/api/v1/projects/${KINASE.id}/trigger-firings`).at(-1)?.query.get("trigger"),
      ).toBe(ON.name);
    });
  });
});
