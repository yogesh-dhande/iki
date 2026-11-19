/**
 * Compute → Quotas, against a fake API: what's limited and how much is in use, setting a
 * quota on a project the admin isn't in, changing and removing one.
 */

import type { Schemas } from "@ikisci/sdk";
import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderApp } from "./entity-fixtures";
import { usage } from "./pages/compute-quotas";
import { answer, calls, ME, signedIn } from "./test-server";

const COMPUTE_ADMIN = { ...ME.can, manage_compute: true };
const NOW = new Date().toISOString();
const PROJECT = { id: "00000000-0000-7000-8000-0000000000p1", name: "Cryo-EM screening" };

function quota(overrides: Partial<Schemas["QuotaOut"]> = {}): Schemas["QuotaOut"] {
  return {
    id: "00000000-0000-7000-8000-0000000000q1",
    team: null,
    project: PROJECT,
    tier: "vm",
    max_running: 4,
    max_queued: null,
    running: 3,
    queued: 12,
    created_by: { id: ME.id, name: ME.name },
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function onQuotas(quotas: Schemas["QuotaOut"][]) {
  signedIn({ can: COMPUTE_ADMIN });
  answer("GET /api/v1/compute/quotas", () => quotas);
}

function card() {
  return within(screen.getByRole("region", { name: "Run quotas" }));
}

describe("how much of a limit is in use", () => {
  it("reads as so many of the limit, no limit, or paused", () => {
    expect(usage(3, 4)).toBe("3 of 4");
    expect(usage(12, null)).toBe("12, no limit");
    expect(usage(0, 0)).toBe("Paused");
    expect(usage(2, 0)).toBe("Paused (2 still running)");
  });
});

describe("Compute → Quotas", () => {
  it("lists each quota with how much of it is in use", async () => {
    onQuotas([quota(), quota({ id: "q2", project: null, tier: null, max_running: 0, running: 0 })]);
    await renderApp("/compute/quotas/");
    const row = (await card().findByRole("rowheader", { name: /Cryo-EM screening/ })).closest("tr");
    if (!row) throw new Error("no row");
    expect(
      within(row)
        .getAllByRole("cell")
        .map((c) => c.textContent),
    ).toEqual(["VM", "3 of 4", "12, no limit", "ChangeRemove"]);
    const org = card()
      .getByRole("rowheader", { name: /The organization/ })
      .closest("tr");
    expect(org).toHaveTextContent("Every tier");
    expect(org).toHaveTextContent("Paused");
  });

  it("sets a quota on any project, found by name", async () => {
    const quotas: Schemas["QuotaOut"][] = [];
    onQuotas(quotas);
    answer("GET /api/v1/compute/quota-scopes", (r: { query: URLSearchParams }) =>
      r.query.get("search") === "cryo" ? [{ ...PROJECT, team: "Structural biology" }] : [],
    );
    answer("POST /api/v1/compute/quotas", () => {
      quotas.push(quota());
      return quota();
    });
    const { user } = await renderApp("/compute/quotas/");
    await card().findByText(/No quotas/);
    await user.click(card().getByRole("button", { name: "Set a quota" }));
    const dialog = within(screen.getByRole("dialog", { name: "Set a quota" }));

    // Nothing to send yet: no limit set.
    await user.click(dialog.getByRole("button", { name: "Set quota" }));
    expect(
      dialog.getByRole("textbox", { name: /Jobs running at once/ }),
    ).toHaveAccessibleDescription(/Set at least one limit\./);

    await user.click(dialog.getByRole("radio", { name: /A project/ }));
    await user.type(dialog.getByRole("textbox", { name: /Find a project/ }), "cryo");
    await user.click(await dialog.findByRole("radio", { name: /Cryo-EM screening/ }));
    await user.selectOptions(dialog.getByRole("combobox", { name: "Tier" }), "vm");
    await user.clear(dialog.getByRole("textbox", { name: /Jobs running at once/ }));
    await user.type(dialog.getByRole("textbox", { name: /Jobs running at once/ }), "4");
    await user.click(dialog.getByRole("button", { name: "Set quota" }));

    expect(await screen.findByText("Set a quota for Cryo-EM screening.")).toBeVisible();
    expect(calls("POST", "/api/v1/compute/quotas").map((c) => c.body)).toEqual([
      { project: PROJECT.id, tier: "vm", max_running: 4, max_queued: null },
    ]);
  });

  it("lists only the teams found for what's typed, saying it's searching till then", async () => {
    onQuotas([]);
    const team = (n: number, name: string) => ({
      id: `00000000-0000-7000-8000-0000000000t${String(n)}`,
      name,
    });
    const teams = [team(1, "Antibodies"), team(2, "Biologics")];
    let release = (): void => undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    answer("GET /api/v1/compute/quota-scopes", async (r: { query: URLSearchParams }) => {
      const search = r.query.get("search") ?? "";
      if (!search) return teams;
      await held;
      return teams.filter((t) => t.name.toLowerCase().includes(search));
    });
    const { user } = await renderApp("/compute/quotas/");
    await card().findByText(/No quotas/);
    await user.click(card().getByRole("button", { name: "Set a quota" }));
    const dialog = within(screen.getByRole("dialog", { name: "Set a quota" }));
    await user.click(dialog.getByRole("radio", { name: /A team/ }));
    await dialog.findByRole("radio", { name: "Antibodies" });
    await user.type(dialog.getByRole("textbox", { name: /Find a team/ }), "bio");
    // The empty search's teams would move under the pointer when "bio"'s came: not shown.
    expect(dialog.queryByRole("radio", { name: "Antibodies" })).not.toBeInTheDocument();
    expect(dialog.getByRole("status")).toHaveTextContent("Searching…");
    await act(async () => {
      release();
      await held;
    });
    expect(await dialog.findByRole("radio", { name: "Biologics" })).toBeInTheDocument();
    expect(dialog.queryByRole("radio", { name: "Antibodies" })).not.toBeInTheDocument();
  });

  it("asks for a whole number", async () => {
    onQuotas([]);
    const { user } = await renderApp("/compute/quotas/");
    await user.click(await card().findByRole("button", { name: "Set a quota" }));
    const dialog = within(screen.getByRole("dialog", { name: "Set a quota" }));
    await user.type(dialog.getByRole("textbox", { name: /Runs waiting/ }), "2.5");
    await user.click(dialog.getByRole("button", { name: "Set quota" }));
    expect(dialog.getByRole("textbox", { name: /Runs waiting/ })).toHaveAccessibleDescription(
      /Enter a whole number from 0 to 1,000,000/,
    );
    expect(calls("POST", "/api/v1/compute/quotas")).toEqual([]);
  });

  it("changes a quota, a blank limit removing it", async () => {
    onQuotas([quota({ max_queued: 50 })]);
    answer("PATCH /api/v1/compute/quotas/00000000-0000-7000-8000-0000000000q1", () =>
      quota({ max_running: 8 }),
    );
    const { user } = await renderApp("/compute/quotas/");
    await user.click(
      await card().findByRole("button", { name: "Change Cryo-EM screening’s quota" }),
    );
    const dialog = within(screen.getByRole("dialog", { name: "Change Cryo-EM screening’s quota" }));
    expect(dialog.getByText(/On the VM tier/)).toBeVisible();
    await user.clear(dialog.getByRole("textbox", { name: /Jobs running at once/ }));
    await user.type(dialog.getByRole("textbox", { name: /Jobs running at once/ }), "8");
    await user.clear(dialog.getByRole("textbox", { name: /Runs waiting/ }));
    await user.click(dialog.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/Changed Cryo-EM screening’s quota\./)).toBeVisible();
    expect(
      calls("PATCH", "/api/v1/compute/quotas/00000000-0000-7000-8000-0000000000q1")[0]?.body,
    ).toEqual({ max_running: 8, max_queued: null });
  });

  it("removes a quota, saying a paused scope starts again", async () => {
    onQuotas([quota({ max_running: 0 })]);
    answer(
      "DELETE /api/v1/compute/quotas/00000000-0000-7000-8000-0000000000q1",
      () => new Response(null, { status: 204 }),
    );
    const { user } = await renderApp("/compute/quotas/");
    await user.click(
      await card().findByRole("button", { name: "Remove Cryo-EM screening’s quota" }),
    );
    const dialog = screen.getByRole("alertdialog", { name: "Remove Cryo-EM screening’s quota?" });
    expect(within(dialog).getByText(/the paused scope starts again/)).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Remove quota" }));
    await waitFor(() => {
      expect(
        calls("DELETE", "/api/v1/compute/quotas/00000000-0000-7000-8000-0000000000q1"),
      ).toHaveLength(1);
    });
    expect(await screen.findByText("Removed Cryo-EM screening’s quota.")).toBeVisible();
  });

  it("isn't anyone else's", async () => {
    signedIn();
    await renderApp("/compute/quotas/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Not found");
    expect(calls("GET", "/api/v1/compute/quotas")).toEqual([]);
  });
});
