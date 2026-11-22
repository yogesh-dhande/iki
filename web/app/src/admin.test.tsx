/** The admin area's frame (ADR 0176): its sidebar, breadcrumb and account, per person. */

import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ADMIN_GROUPS } from "./admin";
import { swatchOf } from "./project-color";
import { renderApp } from "./entity-fixtures";
import { answer, ME, signedIn } from "./test-server";

const NONE = ME.can;
const EVERYTHING = {
  manage_org: true,
  read_audit: true,
  create_projects: true,
  manage_webhooks: true,
  read_costs: true,
  manage_compute: true,
};

/** Compute's overview, with nothing going on: a page of the area to open. */
function onCompute() {
  answer("GET /api/v1/compute/overview", {
    queues: [],
    workers: [],
    machines: [],
    as_of: new Date().toISOString(),
  });
}

/** Each group the sidebar shows, with its links' names and addresses. */
function sidebar(): [string, [string, string | null][]][] {
  const nav = screen.getByRole("navigation", { name: "Admin" });
  return within(nav)
    .getAllByRole("list")
    .map((list) => [
      list.getAttribute("aria-labelledby")
        ? (document.getElementById(list.getAttribute("aria-labelledby") ?? "")?.textContent ?? "")
        : "",
      within(list)
        .getAllByRole("link")
        .map((a) => [a.textContent, a.getAttribute("href")]),
    ]);
}

describe("the admin area", () => {
  it("offers someone who may open everything each page, in the ADR's four groups", async () => {
    signedIn({ can: EVERYTHING });
    onCompute();
    await renderApp("/admin/compute/");
    expect(sidebar()).toEqual([
      [
        "Organization",
        [
          ["Sign-in and provisioning", "/admin/sign-in/"],
          ["People", "/admin/people/"],
          ["Apps", "/admin/apps/"],
          ["Email", "/admin/email/"],
        ],
      ],
      [
        "Data",
        [
          ["Registries", "/admin/registries/"],
          ["Plugins", "/admin/plugins/"],
          ["Outside APIs", "/admin/outside-apis/"],
          ["Webhooks", "/admin/webhooks/"],
        ],
      ],
      [
        "Compute and cost",
        [
          ["Compute", "/admin/compute/"],
          ["Costs and budgets", "/admin/costs/"],
        ],
      ],
      [
        "Compliance",
        [
          ["Audit log", "/admin/audit/"],
          ["Electronic signatures", "/admin/signatures/"],
          ["Legal holds", "/admin/legal-holds/"],
        ],
      ],
    ]);
  });

  it("shows an auditor only what they may open, and no group left empty", async () => {
    signedIn({ can: { ...NONE, read_audit: true, read_costs: true } });
    await renderApp("/admin/compute/");
    expect(sidebar()).toEqual([
      ["Compute and cost", [["Costs and budgets", "/admin/costs/"]]],
      [
        "Compliance",
        [
          ["Audit log", "/admin/audit/"],
          ["Electronic signatures", "/admin/signatures/"],
          ["Legal holds", "/admin/legal-holds/"],
        ],
      ],
    ]);
    // The page itself still checks: Compute isn't theirs.
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Not found");
  });

  it("leads back to Iki, says where you are, and has your account at its foot", async () => {
    signedIn({ can: { ...NONE, manage_org: true } });
    const { user } = await renderApp("/admin/compute/");
    const nav = screen.getByRole("navigation", { name: "Admin" });
    expect(within(nav).getByRole("link", { name: "Back to Iki" })).toHaveAttribute("href", "/");
    // The same account menu as the main sidebar's.
    const account = within(nav).getByRole("button", { name: "Rosalind Franklin" });
    expect(account).toHaveTextContent("RFRosalind Franklin");
    expect(account.querySelector(".avatar")).toHaveAttribute(
      "data-swatch",
      String(swatchOf(ME.id)),
    );
    await user.click(account);
    expect(within(nav).getByRole("link", { name: "Notification preferences" })).toHaveFocus();
    const signOut = within(nav).getByRole("button", { name: "Sign out" });
    expect(signOut.closest("form")).toHaveAttribute("action", "/logout/");
    await user.keyboard("{Escape}");
    expect(account).toHaveFocus();
    expect(within(nav).queryByRole("button", { name: "Sign out" })).toBeNull();
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(crumbs).getByRole("link", { name: "Admin" })).toHaveAttribute("href", "/admin/");
    // The main navigation isn't there: the sidebar is the area's own.
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
  });

  it("folds its pages under a menu button, for narrow screens", async () => {
    signedIn({ can: EVERYTHING });
    onCompute();
    const { user } = await renderApp("/admin/compute/");
    const menu = screen.getByRole("button", { name: "Menu" });
    expect(menu).toHaveAttribute("aria-expanded", "false");
    expect(menu).toHaveAttribute("aria-controls", "admin-menu");
    await user.click(menu);
    expect(menu).toHaveAttribute("aria-expanded", "true");
  });

  it("lists, for every item, what it needs, an icon and an address", () => {
    const can = new Set(Object.keys(EVERYTHING));
    for (const item of ADMIN_GROUPS.flatMap((g) => g.items)) {
      expect(item.requires.length).toBeGreaterThan(0);
      for (const need of item.requires) expect(can).toContain(need);
      expect(item.icon.length).toBeGreaterThan(0);
      expect(item.href).toMatch(/^\/.*\/$/);
    }
  });
});
