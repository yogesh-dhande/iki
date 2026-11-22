/**
 * Compute → Slurm, against a fake API: each cluster's templates as compute.yaml says them
 * and its workers, and giving projects their own Slurm accounts, changing and clearing them.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { slurmRequestText, walltimeText } from "./compute";
import { renderApp } from "./entity-fixtures";
import { accountError, jobsText, priceText } from "./pages/compute-slurm";
import { answer, calls, ME, page, problem, signedIn } from "./test-server";

const COMPUTE_ADMIN = { ...ME.can, manage_compute: true };
const GI = 1024 ** 3;
const NOW = new Date().toISOString();
const TEAM = { id: "00000000-0000-7000-8000-0000000000a1", name: "Structural Biology" };
const PROJECT = { id: "00000000-0000-7000-8000-0000000000p1", name: "Cryo-EM screening" };
const ACCOUNTS = "/api/v1/compute/slurm/hpc/accounts";

function template(
  overrides: Partial<Schemas["SlurmTemplateOut"]> = {},
): Schemas["SlurmTemplateOut"] {
  return {
    name: "hpc-gpu",
    partition: "gpu",
    qos: "normal",
    account: "iki-shared",
    constraint: "a100",
    gpu_type: "a100",
    max_walltime: 2 * 86_400,
    egress_proxy_url: "http://iki-egress.hpc.example.com:3128",
    capacity: { cpu: 32, memory: 256 * GI, gpu_count: 4, gpu_memory: 80 * GI, scratch_disk: 0 },
    max_concurrent: 10,
    price: { per_cpu_hour: 0.04, per_gpu_hour: 1.2, per_memory_gib_hour: 0.005, currency: "GBP" },
    jobs: 3,
    ...overrides,
  };
}

function cluster(overrides: Partial<Schemas["SlurmClusterOut"]> = {}): Schemas["SlurmClusterOut"] {
  return { name: "hpc", templates: [template()], workers_online: 2, accounts: 1, ...overrides };
}

function account(overrides: Partial<Schemas["SlurmAccountOut"]> = {}): Schemas["SlurmAccountOut"] {
  return {
    cluster: "hpc",
    project: { ...PROJECT, team: TEAM },
    account: "cryo-em",
    set_by: { id: ME.id, name: ME.name },
    set_at: NOW,
    ...overrides,
  };
}

function onSlurm(clusters: Schemas["SlurmClusterOut"][], accounts: Schemas["SlurmAccountOut"][]) {
  signedIn({ can: COMPUTE_ADMIN });
  answer("GET /api/v1/compute/slurm", () => clusters);
  answer(`GET ${ACCOUNTS}`, () => page(accounts));
}

async function card(name = "hpc") {
  return within(await screen.findByRole("region", { name }));
}

async function accounts() {
  return within(await screen.findByRole("group", { name: "Projects’ accounts" }));
}

describe("a Slurm template's facts", () => {
  it("reads rates, jobs and time limits", () => {
    expect(priceText(template().price)).toBe(
      "£0.04 per CPU, £1.20 per GPU, £0.005 per GiB of memory, an hour",
    );
    expect(priceText(null)).toBeNull();
    expect(jobsText(3, 10)).toBe("3 of 10");
    expect(jobsText(3, null)).toBe("3, no limit");
    expect(walltimeText(2 * 86_400)).toBe("2 days");
    expect(walltimeText(12 * 3600)).toBe("12 h");
    expect(walltimeText(90 * 60)).toBe("1 h 30 min");
  });

  it("says what a job asked for", () => {
    expect(
      slurmRequestText({ cpus: 8, memory: 32 * GI, gpus: 1, scratch_disk: 0, walltime: 43_200 }),
    ).toBe("8 CPUs, 32 GiB, 1 GPU, for up to 12 h");
    expect(slurmRequestText({})).toBe("Slurm’s defaults");
  });

  it("checks an account as sacctmgr names one", () => {
    expect(accountError("cryo-em_2.0")).toBe(false);
    expect(accountError(" ")).toBe("Enter the Slurm account its jobs are charged to.");
    expect(accountError("-cryo")).toMatch(/starting with a letter or digit/);
    expect(accountError("cryo em")).toMatch(/letters, digits, dots, dashes and underscores/);
  });
});

describe("Compute → Slurm", () => {
  it("shows each cluster's templates as compute.yaml says them, and its workers", async () => {
    onSlurm([cluster()], []);
    await renderApp("/admin/compute/slurm/");
    const row = (await (await card()).findByRole("rowheader", { name: "hpc-gpu" })).closest("tr");
    if (!row) throw new Error("no row");
    expect(
      within(row)
        .getAllByRole("cell")
        .map((c) => c.textContent),
    ).toEqual([
      "gpuQOS normalNodes with a100",
      "iki-shared",
      "2 days",
      "32 CPUs, 256 GiB, 4 GPUs (80 GiB)GPU type a100",
      "3 of 10",
      "£0.04 per CPU, £1.20 per GPU, £0.005 per GiB of memory, an hour",
      "Through the egress proxyhttp://iki-egress.hpc.example.com:3128",
    ]);
    expect((await card()).getByText("2 workers online on its login nodes.")).toBeVisible();
    expect(screen.getByText(/which is reviewed like code: change them there/)).toBeVisible();
    const tabs = screen.getByRole("navigation", { name: "Compute" });
    expect(within(tabs).getByRole("link", { name: "Slurm clusters" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("says what a template without an account, a price or the internet does", async () => {
    onSlurm(
      [
        cluster({
          templates: [
            template({
              account: "",
              qos: "",
              constraint: "",
              gpu_type: "",
              max_walltime: null,
              egress_proxy_url: "",
              capacity: { cpu: 4, memory: 16 * GI, scratch_disk: 100 * GI },
              max_concurrent: null,
              price: null,
            }),
          ],
        }),
      ],
      [],
    );
    await renderApp("/admin/compute/slurm/");
    const row = (await (await card()).findByRole("rowheader", { name: "hpc-gpu" })).closest("tr");
    if (!row) throw new Error("no row");
    expect(
      within(row)
        .getAllByRole("cell")
        .map((c) => c.textContent),
    ).toEqual([
      "gpu",
      "NoneOnly projects with their own account run here.",
      "The partition’s",
      "4 CPUs, 16 GiB100 GiB of scratch disk",
      "3, no limit",
      "No price",
      "NoneActions that reach the internet don’t run here.",
    ]);
  });

  it("warns when no worker on a cluster is online", async () => {
    onSlurm([cluster({ workers_online: 0 })], []);
    await renderApp("/admin/compute/slurm/");
    const alert = await (await card()).findByText(/No worker on its login nodes is online/);
    expect(alert).toHaveTextContent("so its runs wait");
    expect(within(alert).getByRole("link", { name: "Workers" })).toHaveAttribute(
      "href",
      "/admin/compute/workers/",
    );
  });

  it("says how to add a cluster when there are none", async () => {
    onSlurm([], []);
    await renderApp("/admin/compute/slurm/");
    expect(await screen.findByText(/This deployment has no Slurm clusters/)).toBeVisible();
    expect(calls("GET", ACCOUNTS)).toEqual([]);
  });

  it("lists the projects with their own account", async () => {
    onSlurm([cluster()], [account()]);
    await renderApp("/admin/compute/slurm/");
    await (await card()).findByRole("rowheader", { name: "hpc-gpu" });
    const row = (
      await (await accounts()).findByRole("rowheader", { name: /Cryo-EM screening/ })
    ).closest("tr");
    if (!row) throw new Error("no row");
    expect(row).toHaveTextContent("Structural Biology team");
    const cells = within(row).getAllByRole("cell");
    expect(cells[0]).toHaveTextContent("cryo-em");
    expect(cells[1]).toHaveTextContent(`By ${ME.name} just now`);
  });

  it("gives a project an account, found by name", async () => {
    const given: Schemas["SlurmAccountOut"][] = [];
    onSlurm([cluster({ accounts: 0 })], given);
    answer("GET /api/v1/compute/quota-scopes", (r: { query: URLSearchParams }) =>
      r.query.get("kind") === "project" && r.query.get("search") === "cryo"
        ? [{ ...PROJECT, team: TEAM.name, team_id: TEAM.id }]
        : [],
    );
    answer(`PUT ${ACCOUNTS}/${PROJECT.id}`, () => {
      given.push(account());
      return account();
    });
    const { user } = await renderApp("/admin/compute/slurm/");
    expect(await (await accounts()).findByText(/None yet/)).toBeVisible();
    await user.click((await accounts()).getByRole("button", { name: "Give a project an account" }));
    const dialog = within(screen.getByRole("dialog", { name: "Give a project an account on hpc" }));

    // Nothing to send yet: no account typed.
    await user.click(dialog.getByRole("button", { name: "Set account" }));
    expect(dialog.getByRole("textbox", { name: "Slurm account" })).toHaveAccessibleDescription(
      /Enter the Slurm account its jobs are charged to\./,
    );

    await user.type(dialog.getByRole("textbox", { name: /Find a project/ }), "cryo");
    const choice = await dialog.findByRole("radio", { name: /Cryo-EM screening/ });
    await user.click(choice);
    await user.type(dialog.getByRole("textbox", { name: "Slurm account" }), "cryo-em");
    await user.click(dialog.getByRole("button", { name: "Set account" }));

    expect(
      await screen.findByText(
        "Cryo-EM screening’s jobs on hpc are charged to cryo-em from their next attempt.",
      ),
    ).toBeVisible();
    expect(calls("PUT", `${ACCOUNTS}/${PROJECT.id}`).map((c) => c.body)).toEqual([
      { account: "cryo-em" },
    ]);
    expect(
      await (await accounts()).findByRole("rowheader", { name: /Cryo-EM screening/ }),
    ).toBeVisible();
  });

  it("asks for a project, and shows what the server says of the account", async () => {
    onSlurm([cluster()], [account()]);
    answer(`PUT ${ACCOUNTS}/${PROJECT.id}`, () =>
      problem(422, "validation_failed", "Some fields are invalid.", [
        { pointer: "#/account", detail: "Slurm has no such account." },
      ]),
    );
    const { user } = await renderApp("/admin/compute/slurm/");
    await user.click(
      await (await accounts()).findByRole("button", { name: "Change Cryo-EM screening’s account" }),
    );
    const dialog = within(
      screen.getByRole("dialog", { name: "Change Cryo-EM screening’s account on hpc" }),
    );
    // The project is fixed: no picker.
    expect(dialog.queryByRole("textbox", { name: /Find a project/ })).not.toBeInTheDocument();
    const field = dialog.getByRole("textbox", { name: "Slurm account" });
    expect(field).toHaveValue("cryo-em");
    await user.clear(field);
    await user.type(field, "cryo em");
    await user.click(dialog.getByRole("button", { name: "Save" }));
    expect(field).toHaveAccessibleDescription(/letters, digits, dots, dashes and underscores/);
    expect(calls("PUT", `${ACCOUNTS}/${PROJECT.id}`)).toEqual([]);

    await user.clear(field);
    await user.type(field, "cryo-em-2");
    await user.click(dialog.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(field).toHaveAccessibleDescription(/Slurm has no such account\./);
    });
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(calls("PUT", `${ACCOUNTS}/${PROJECT.id}`).map((c) => c.body)).toEqual([
      { account: "cryo-em-2" },
    ]);
  });

  it("asks which project when none is chosen", async () => {
    onSlurm([cluster()], []);
    answer("GET /api/v1/compute/quota-scopes", () => [
      { ...PROJECT, team: TEAM.name, team_id: TEAM.id },
    ]);
    const { user } = await renderApp("/admin/compute/slurm/");
    await user.click(
      await (await accounts()).findByRole("button", { name: "Give a project an account" }),
    );
    const dialog = within(screen.getByRole("dialog", { name: "Give a project an account on hpc" }));
    await dialog.findByRole("radio", { name: /Cryo-EM screening/ });
    await user.type(dialog.getByRole("textbox", { name: "Slurm account" }), "cryo-em");
    await user.click(dialog.getByRole("button", { name: "Set account" }));
    expect(dialog.getByRole("group", { name: "Project" })).toHaveAccessibleDescription(
      /Choose the project\./,
    );
    expect(calls("PUT")).toEqual([]);
  });

  it("clears a project's account, saying where its jobs are charged then", async () => {
    onSlurm([cluster()], [account()]);
    answer(`DELETE ${ACCOUNTS}/${PROJECT.id}`, () => new Response(null, { status: 204 }));
    const { user } = await renderApp("/admin/compute/slurm/");
    await user.click(
      await (await accounts()).findByRole("button", { name: "Clear Cryo-EM screening’s account" }),
    );
    const dialog = screen.getByRole("alertdialog", {
      name: "Clear Cryo-EM screening’s account on hpc?",
    });
    expect(within(dialog).getByText(/charged to their template’s default account/)).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Clear account" }));
    await waitFor(() => {
      expect(calls("DELETE", `${ACCOUNTS}/${PROJECT.id}`)).toHaveLength(1);
    });
    expect(await screen.findByText("Cleared Cryo-EM screening’s account on hpc.")).toBeVisible();
  });

  it("isn't anyone else's", async () => {
    signedIn();
    await renderApp("/admin/compute/slurm/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Not found");
    expect(calls("GET", "/api/v1/compute/slurm")).toEqual([]);
  });
});
