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
  name: "iki/protein.compute_new_protein_properties",
  plugin: "iki/protein",
  label: "Compute each new protein's properties",
  description: null,
  on: "entity.created",
  recheck: false,
  type: "iki/protein.Protein",
  schedule: "",
  timezone: "",
  fields: [],
  field: "",
  states: [],
  action: "iki/protein.compute_properties",
  action_label: "Compute properties",
  input: "protein",
  related: {},
  params: {},
  tier: "backend",
  runs_as_account: false,
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
  name: "iki/protein.relax_changed_structures",
  label: "Relax changed structures",
  on: "entity.updated",
  type: "iki/protein.ProteinStructure",
  fields: ["coordinates", "chains"],
  action: "iki/protein.relax_structure",
  action_label: "Relax structure",
  tier: "container",
};

const ROBOT = { id: "00000000-0000-7000-8000-0000000000b7", name: "Nightly robot" };

const NIGHTLY: Schemas["ProjectTriggerOut"] = {
  ...OFF,
  name: "iki/protein.fetch_hemoglobin_nightly",
  label: "Fetch hemoglobin nightly",
  on: "schedule",
  type: "",
  schedule: "0 3 * * *",
  timezone: "Europe/Zurich",
  action: "iki/protein.fetch_proteins",
  action_label: "Fetch proteins",
  input: "",
  runs_as_account: true,
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
  related: null,
  run: { id: "00000000-0000-7000-8000-0000000000r9", hrid: "RUN-000009", status: "queued" },
  error: null,
  created_at: "2026-03-14T10:00:00Z",
  fired_at: "2026-03-14T10:00:01Z",
};

/** One that gives its runs what's related (ADR 0123): a tube's derived stock. */
const HOLD: Schemas["ProjectTriggerOut"] = {
  ...OFF,
  name: "iki/inventory.hold_derived_tubes",
  label: "Hold portions of withdrawn stock",
  on: "entity.updated",
  type: "iki/inventory.Tube",
  fields: ["on_hold"],
  action: "iki/inventory.hold_withdrawn",
  action_label: "Hold withdrawn",
  input: "source",
  related: {
    tubes: { direction: "downstream", depth: 10, edges: ["aliquot", "transfer"] },
  },
};

function must<T>(value: T | undefined): T {
  if (value === undefined) throw new Error("missing");
  return value;
}

function onProject(triggers: Schemas["ProjectTriggerOut"][], firings = [FIRING]): void {
  withTypes();
  answer("GET /api/v1/projects", page([project()]));
  answer(`GET /api/v1/projects/${KINASE.id}/triggers`, page(triggers));
  answer(`GET /api/v1/projects/${KINASE.id}/trigger-firings`, page(firings));
}

describe("a project's triggers", () => {
  it("says what each fires, whether it's on and as whom", async () => {
    onProject([ON, RELAX]);
    await renderApp("/projects/kinase-atlas/automation/triggers/");
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
    const { user } = await renderApp("/projects/kinase-atlas/automation/triggers/");
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

  it("says what else a trigger gives its runs, and how many each got", async () => {
    const held: Schemas["FiringOut"] = {
      ...FIRING,
      trigger: HOLD.name,
      label: HOLD.label,
      event: "entity.updated",
      entity: { id: "00000000-0000-7000-8000-0000000000e2", hrid: "TUB-000001", type: HOLD.type },
      related: { tubes: 3 },
    };
    onProject([HOLD], [held]);
    const { user } = await renderApp("/projects/kinase-atlas/automation/triggers/");
    const rows = await screen.findByRole("list", { name: "Triggers" });
    expect(rows).toHaveTextContent(
      "Each run is also given, from Kinase Atlas: tubes, what was derived from it, up to 10 steps away",
    );
    const fired = await screen.findByRole("list", { name: "Firings" });
    expect(fired).toHaveTextContent("given 3 tubes");
    await user.click(within(rows).getByRole("button", { name: "Turn on" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(
      "Each run is also given what you can read in Kinase Atlas of: tubes,",
    );
    expect(dialog).toHaveTextContent("If there are more than its action takes");
  });

  it("turns one off", async () => {
    onProject([{ ...ON, can: { enable: false, disable: true } }]);
    answer(`POST /api/v1/projects/${KINASE.id}/triggers/${OFF.name}/disable`, () => OFF);
    const { user } = await renderApp("/projects/kinase-atlas/automation/triggers/");
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
          "Alice Admin can no longer run `iki/protein.compute_properties` in Kinase Atlas.",
        why_not_enable:
          "Its runs would be launched as you, and you can't run backend-tier actions in this project.",
        can: { enable: false, disable: false },
      },
    ]);
    await renderApp("/projects/kinase-atlas/automation/triggers/");
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
    await renderApp("/projects/kinase-atlas/automation/triggers/");
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
    const { user } = await renderApp("/projects/kinase-atlas/automation/triggers/");
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
    await renderApp("/projects/kinase-atlas/automation/triggers/");
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
    const { user } = await renderApp("/projects/kinase-atlas/automation/triggers/");
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

/** One that fires as another project sends the project a request (ADR 0232). */
const RANKING: Schemas["ProjectTriggerOut"] = {
  ...OFF,
  name: "acme/request_priority.rank_arriving_requests",
  label: "Rank requests as they arrive",
  on: "request.received",
  type: "iki/requests.PlainRequest",
  action: "acme/request_priority.rank_requests",
  action_label: "Rank requests by project",
  input: "requests",
  runs_as_account: true,
  run_as_choices: [ROBOT],
};

describe("a trigger on a request's arrival", () => {
  it("runs as the service account you choose, on your authority", async () => {
    onProject([RANKING], []);
    const path = `/api/v1/projects/${KINASE.id}/triggers/${RANKING.name}/enable`;
    answer(`POST ${path}`, () => ({ ...RANKING, enabled: true, run_as: ROBOT }));
    const { user } = await renderApp("/projects/kinase-atlas/automation/triggers/");
    const rows = await screen.findByRole("list", { name: "Triggers" });
    const [row] = within(rows).getAllByRole("listitem");
    expect(row).toHaveTextContent(
      /^Rank requests as they arrive.*another project sends to Kinase Atlas starts a run of Rank requests by project\./,
    );
    await user.click(await screen.findByRole("button", { name: "Turn on" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("as the service account you choose, on your authority");
    expect(dialog).toHaveTextContent("Another project sent the request");
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Runs as" }), ROBOT.name);
    await user.click(within(dialog).getByRole("button", { name: "Turn on" }));
    await waitFor(() => {
      expect(calls("POST", path)).toHaveLength(1);
    });
    expect(calls("POST", path)[0]?.body).toEqual({ run_as: ROBOT.id });
    expect(
      await screen.findByText("“Rank requests as they arrive” is on, and runs as Nightly robot."),
    ).toBeInTheDocument();
  });

  it("lists a firing by the request that arrived", async () => {
    onProject(
      [
        {
          ...RANKING,
          enabled: true,
          run_as: ROBOT,
          enabled_by: ON.enabled_by,
          enabled_at: ON.enabled_at,
        },
      ],
      [
        {
          ...FIRING,
          trigger: RANKING.name,
          label: RANKING.label,
          event: "request.received",
          entity: { id: FIRING.entity?.id ?? "", hrid: "SEQ-000041", type: RANKING.type },
        },
      ],
    );
    await renderApp("/projects/kinase-atlas/automation/triggers/");
    expect(await screen.findByText(/arrived/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "SEQ-000041" })).toBeInTheDocument();
  });
});
