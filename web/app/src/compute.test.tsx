/** Compute → Overview, against a fake API: the queues, workers and machines, for Compute admins. */

import type { Schemas } from "@ikisci/sdk";
import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderApp } from "./entity-fixtures";
import { answer, calls, ME, signedIn } from "./test-server";

const COMPUTE_ADMIN = { ...ME.can, manage_compute: true };

function overview(overrides: Partial<Schemas["OverviewOut"]> = {}): Schemas["OverviewOut"] {
  return {
    queues: [
      {
        tier: "container",
        environment: "structure_prediction.alphafold-gpu",
        awaiting_approval: 1,
        queued: 4,
        running: 2,
        oldest_queued_at: "2026-11-16T08:00:00Z",
        waiting_seconds: 750,
      },
    ],
    workers: [
      { tier: "self_hosted", busy: 0, idle: 0, offline: 0 },
      { tier: "backend", busy: 2, idle: 1, offline: 1 },
      { tier: "container", busy: 2, idle: 0, offline: 0 },
      { tier: "vm", busy: 0, idle: 0, offline: 0 },
    ],
    machines: [{ template: "gpu-a100", provisioner: "k8s_job", starting: 3, running: 2 }],
    as_of: new Date().toISOString(),
    ...overrides,
  };
}

function rowOf(table: string, header: string) {
  const region = screen.getByRole("region", { name: table });
  const row = within(region)
    .getByRole("rowheader", { name: new RegExp(header) })
    .closest("tr");
  if (!row) throw new Error(`no row for ${header}`);
  return within(row);
}

describe("Compute → Overview", () => {
  it("shows what's waiting, on which workers, and the machines Iki started", async () => {
    signedIn({ can: COMPUTE_ADMIN });
    answer("GET /api/v1/compute/overview", overview());
    await renderApp("/admin/compute/");

    expect(await screen.findByRole("heading", { level: 1, name: "Compute" })).toBeVisible();
    const tabs = screen.getByRole("navigation", { name: "Compute" });
    expect(within(tabs).getByRole("link", { name: "Overview" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    const nav = screen.getByRole("navigation", { name: "Admin" });
    expect(within(nav).getByRole("link", { name: "Compute" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(
      within(crumbs)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual(["Admin", "Compute"]);
    // Its own action, in the top bar: where tokens to register workers are made.
    expect(
      within(screen.getByRole("banner")).getByRole("link", { name: "Register a worker" }),
    ).toHaveAttribute("href", "/admin/compute/workers/#registration-tokens");

    // Across every tier and environment: 4 queued, 1 awaiting approval, 2 running; 5
    // workers heard from, 1 not.
    expect(await screen.findByText("Runs waiting")).toBeVisible();
    const tiles = screen
      .getAllByRole("term")
      .map((t) => [t.textContent, t.nextSibling?.textContent]);
    expect(tiles).toEqual([
      ["Runs waiting", "4and 1 awaiting approval"],
      ["Runs running", "2"],
      ["Workers online", "5"],
      ["Workers offline", "1"],
    ]);

    const alphafold = rowOf("Runs by environment", "structure_prediction.alphafold-gpu");
    expect(alphafold.getAllByRole("cell").map((c) => c.textContent)).toEqual([
      "Container",
      "1",
      "4",
      "2",
      "13 min",
    ]);
    const backend = rowOf("Workers by tier", "Backend");
    expect(backend.getAllByRole("cell").map((c) => c.textContent)).toEqual([
      "Workers you run and register with a token",
      "2",
      "1",
      "1",
      "Manage",
    ]);
    expect(backend.getByRole("link", { name: "Manage Backend workers" })).toHaveAttribute(
      "href",
      "/admin/compute/workers/?tier=backend",
    );
    // An empty tier is listed, and says what its workers would be.
    expect(
      rowOf("Workers by tier", "VM").getByRole("cell", { name: /Machines Iki starts for runs/ }),
    ).toBeVisible();
    const gpu = rowOf("Machines Iki started", "gpu-a100");
    expect(gpu.getByText("Kubernetes Jobs, one per run")).toBeVisible();
    expect(gpu.getAllByRole("cell").map((c) => c.textContent)).toEqual(["3", "2"]);
  });

  it("says so when nothing's waiting and no machines are up", async () => {
    signedIn({ can: COMPUTE_ADMIN });
    answer("GET /api/v1/compute/overview", overview({ queues: [], machines: [] }));
    await renderApp("/admin/compute/");
    expect(await screen.findByText("Nothing is waiting or running.")).toBeVisible();
    expect(screen.getByText(/None now\. Iki starts pods and virtual machines/)).toBeVisible();
  });

  it("leads to Volumes, which leads back", async () => {
    signedIn({ can: COMPUTE_ADMIN });
    answer("GET /api/v1/compute/overview", overview());
    answer("GET /api/v1/compute/volumes", []);
    await renderApp("/admin/compute/volumes/");
    const tabs = await screen.findByRole("navigation", { name: "Compute" });
    expect(within(tabs).getByRole("link", { name: "Volumes" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(tabs).getByRole("link", { name: "Overview" })).not.toHaveAttribute(
      "aria-current",
    );
  });

  it("isn't anyone else's", async () => {
    signedIn();
    await renderApp("/admin/compute/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Not found");
    expect(calls("GET", "/api/v1/compute/overview")).toEqual([]);
    // Someone with no page of the admin area gets the main frame.
    expect(screen.queryByRole("navigation", { name: "Admin" })).not.toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).queryByRole("link", { name: "Compute" })).not.toBeInTheDocument();
  });

  it("isn't an auditor's, who sees it isn't in the admin area they have", async () => {
    signedIn({ can: { ...ME.can, read_audit: true, read_costs: true } });
    await renderApp("/admin/compute/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Not found");
    expect(calls("GET", "/api/v1/compute/overview")).toEqual([]);
    const nav = screen.getByRole("navigation", { name: "Admin" });
    expect(within(nav).queryByRole("link", { name: "Compute" })).not.toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Costs and budgets" })).toBeVisible();
  });
});
