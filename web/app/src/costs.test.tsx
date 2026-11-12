/**
 * What runs cost (ADR 0070), against a fake API: the organization's report for those who
 * answer for it, a project's on its overview, and the formats both use.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { queryClient } from "./api";
import { machineTime, money, shiftMonth, thisMonth } from "./costs";
import { createAppRouter } from "./router";
import { answer, calls, ME, page, project, signedIn } from "./test-server";
import type { Schemas } from "@ikisci/sdk";

async function renderApp(path: string) {
  const router = createAppRouter(createMemoryHistory({ initialEntries: [path] }));
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  await screen.findByRole("heading", { level: 1 });
  return { router, user: userEvent.setup() };
}

const PROJECT = project();

function summary(overrides: Partial<Schemas["CostSummaryOut"]> = {}): Schemas["CostSummaryOut"] {
  return {
    since: "2026-11-01",
    until: "2026-12-01",
    by: "project",
    totals: [{ currency: "USD", amount: 41.5, seconds: 90_000 }],
    groups: [
      {
        key: PROJECT.id,
        label: "Kinase Atlas",
        currency: "USD",
        amount: 40.25,
        seconds: 80_000,
        runs: 12,
      },
      {
        key: null,
        label: "Idle machines",
        currency: "USD",
        amount: 1.25,
        seconds: 10_000,
        runs: 0,
      },
    ],
    ...overrides,
  };
}

const ADMIN = { ...ME.can, read_costs: true };

describe("how costs read", () => {
  it("shows money to the cent, and machine time in hours and minutes", () => {
    expect(money(41.5, "USD")).toBe("$41.50");
    expect(money(0, "USD")).toBe("$0.00");
    expect(money(0.004, "USD")).toBe("less than $0.01");
    expect(money(3, "EUR")).toBe("€3.00");
    expect(machineTime(45)).toBe("45 s");
    expect(machineTime(720)).toBe("12 min");
    expect(machineTime(7200)).toBe("2 h");
    expect(machineTime(11_100)).toBe("3 h 5 min");
  });

  it("steps through months in UTC", () => {
    expect(thisMonth(new Date("2026-11-30T23:30:00-05:00"))).toEqual({
      since: "2026-12-01",
      until: "2027-01-01",
    });
    expect(shiftMonth("2026-01-01", -1)).toEqual({ since: "2025-12-01", until: "2026-01-01" });
  });
});

describe("the organization's costs", () => {
  it("are in the navigation only for those who may read them", async () => {
    signedIn({ can: ADMIN });
    answer("GET /api/v1/compute/costs", summary());
    await renderApp("/costs/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Costs" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("show this month by project, with idle machines apart, and download as CSV", async () => {
    signedIn({ can: ADMIN });
    answer("GET /api/v1/compute/costs", summary());
    await renderApp("/costs/");
    const table = await screen.findByRole("table");
    const rows = within(table)
      .getAllByRole("row")
      .map((row) => row.textContent);
    expect(rows).toEqual([
      "ProjectRunsMachine timeCost",
      "Kinase Atlas1222 h 13 min$40.25",
      "Idle machinesMachines up with no run to do: the organization’s, not a project’s02 h 47 min$1.25",
      "Total25 h$41.50",
    ]);
    const { since, until } = thisMonth();
    expect(calls("GET", "/api/v1/compute/costs").at(-1)?.query.toString()).toBe(
      `by=project&since=${since}&until=${until}`,
    );
    expect(screen.getByRole("link", { name: "Download records (CSV)" })).toHaveAttribute(
      "href",
      `/api/v1/compute/costs/records.csv?since=${since}&until=${until}`,
    );
    // There's no next month yet.
    expect(screen.getByRole("button", { name: /→$/ })).toBeDisabled();
  });

  it("go back a month, and group another way, in the address", async () => {
    signedIn({ can: ADMIN });
    answer("GET /api/v1/compute/costs", summary({ by: "team", groups: [] }));
    const { user, router } = await renderApp("/costs/?month=2026-03");
    expect(await screen.findByText("No machine time was recorded in March 2026.")).toBeVisible();
    await user.selectOptions(screen.getByRole("combobox", { name: "Group by" }), "Team");
    expect(router.state.location.search).toEqual({ month: "2026-03", by: "team" });
    await user.click(screen.getByRole("button", { name: "← February 2026" }));
    expect(router.state.location.search).toEqual({ month: "2026-02", by: "team" });
    expect(calls("GET", "/api/v1/compute/costs").at(-1)?.query.toString()).toBe(
      "by=team&since=2026-02-01&until=2026-03-01",
    );
  });

  it("aren't anyone else's", async () => {
    signedIn();
    await renderApp("/costs/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Not found");
    expect(calls("GET", "/api/v1/compute/costs")).toEqual([]);
  });
});

describe("a project's costs", () => {
  function onProject() {
    answer("GET /api/v1/projects", page([PROJECT]));
  }

  it("show on its overview, by action", async () => {
    signedIn();
    onProject();
    answer(`GET /api/v1/projects/${PROJECT.id}/costs`, {
      ...summary({ by: "action" }),
      totals: [{ currency: "USD", amount: 40.25, seconds: 80_000 }],
      groups: [
        {
          key: "protein.relax",
          label: "protein.relax",
          currency: "USD",
          amount: 40.25,
          seconds: 80_000,
          runs: 12,
        },
      ],
    });
    await renderApp("/projects/kinase-atlas/");
    const card = (
      await screen.findByRole("heading", { name: "What runs cost in November 2026" })
    ).closest("section");
    if (!card) throw new Error("no costs card");
    expect(within(card).getByRole("row", { name: /protein\.relax/ })).toHaveTextContent(
      "protein.relax1222 h 13 min$40.25",
    );
  });

  it("aren't shown when its runs cost nothing", async () => {
    signedIn();
    onProject();
    answer(`GET /api/v1/projects/${PROJECT.id}/costs`, summary({ totals: [], groups: [] }));
    await renderApp("/projects/kinase-atlas/");
    await screen.findByText("14 Mar 2026");
    expect(screen.queryByRole("heading", { name: /What runs cost/ })).not.toBeInTheDocument();
  });
});
