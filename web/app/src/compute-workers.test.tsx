/**
 * Compute → Workers, against a fake API: the workers and their state, revoking one,
 * changing a self-hosted one's teams and labels, and making and revoking registration
 * tokens, a Slurm cluster's among them.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderApp } from "./entity-fixtures";
import { capacityText, parseLabels } from "./compute";
import { scopeText } from "./pages/compute-workers";
import { answer, calls, ME, page, signedIn } from "./test-server";

const COMPUTE_ADMIN = { ...ME.can, manage_compute: true };
const GI = 1024 ** 3;
const NOW = new Date().toISOString();

function worker(overrides: Partial<Schemas["WorkerOut"]> = {}): Schemas["WorkerOut"] {
  return {
    id: "00000000-0000-7000-8000-0000000000w1",
    name: "gpu-pool-7f9c",
    tier: "backend",
    hint: "x9Qa",
    version: "0.4.0",
    capacity: { cpu: 8, memory: 32 * GI, gpu_count: 1, gpu_memory: 80 * GI },
    plugins: [],
    registration: "00000000-0000-7000-8000-0000000000t1",
    teams: [],
    projects: [],
    labels: [],
    slurm_cluster: "",
    running: [],
    registered_at: NOW,
    last_seen_at: NOW,
    revoked_at: null,
    status: "online",
    jobs: 2,
    ...overrides,
  };
}

function token(
  overrides: Partial<Schemas["WorkerRegistrationOut"]> = {},
): Schemas["WorkerRegistrationOut"] {
  return {
    id: "00000000-0000-7000-8000-0000000000t1",
    label: "GPU pool (us-east-1)",
    tier: "backend",
    hint: "k3Pz",
    created_by: { id: ME.id, name: ME.name },
    created_at: NOW,
    expires_at: null,
    last_used_at: NOW,
    revoked_at: null,
    workers: 3,
    teams: [],
    projects: [],
    labels: [],
    slurm_cluster: "",
    ...overrides,
  };
}

function onWorkers(workers: Schemas["WorkerOut"][], tokens: Schemas["WorkerRegistrationOut"][]) {
  signedIn({ can: COMPUTE_ADMIN });
  answer("GET /api/v1/compute/workers", () => page(workers));
  answer("GET /api/v1/compute/registration-tokens", () => page(tokens));
}

function section(name: string) {
  return within(screen.getByRole("region", { name }));
}

describe("what one job may use", () => {
  it("reads as CPUs, memory and GPUs", () => {
    expect(capacityText({ cpu: 8, memory: 32 * GI, gpu_count: 1, gpu_memory: 80 * GI })).toBe(
      "8 CPUs, 32 GiB, 1 GPU (80 GiB)",
    );
    expect(capacityText({ cpu: 1 })).toBe("1 CPU");
    expect(capacityText({})).toBe("—");
  });
});

const BIO = { id: "00000000-0000-7000-8000-0000000000a1", name: "Structural Biology" };
const CHEM = { id: "00000000-0000-7000-8000-0000000000a2", name: "Chemistry" };
const ASSAYS = { id: "00000000-0000-7000-8000-0000000000p1", name: "Assays", team: CHEM };

describe("what a self-hosted worker is for", () => {
  it("names each team, narrowed to its projects or whole", () => {
    expect(scopeText([BIO, CHEM], [ASSAYS])).toBe("Structural Biology; Chemistry: Assays");
    expect(scopeText([BIO], [])).toBe("Structural Biology");
  });

  it("takes labels separated by commas or spaces", () => {
    expect(parseLabels(" Chromeleon, lab-3  gpu,,")).toEqual(["chromeleon", "lab-3", "gpu"]);
    expect(parseLabels("")).toEqual([]);
  });
});

function selfHosted(overrides: Partial<Schemas["WorkerOut"]> = {}): Schemas["WorkerOut"] {
  return worker({
    id: "00000000-0000-7000-8000-0000000000w2",
    name: "hplc-pc-1",
    tier: "self_hosted",
    teams: [BIO, CHEM],
    projects: [ASSAYS],
    labels: ["chromeleon", "lab-3"],
    jobs: 1,
    running: [
      {
        id: "00000000-0000-7000-8000-0000000000r1",
        hrid: "RUN-000042",
        action: "chem.hplc-purity",
        project: { id: ASSAYS.id, name: ASSAYS.name },
      },
    ],
    ...overrides,
  });
}

function onScopes() {
  answer("GET /api/v1/compute/quota-scopes", (r: { query: URLSearchParams }) =>
    page<Schemas["QuotaScopeOut"]>(
      r.query.get("kind") === "team"
        ? [BIO, CHEM].map((t) => ({ ...t, team: null, team_id: null }))
        : r.query.getAll("team").includes(CHEM.id)
          ? [{ ...ASSAYS, team: CHEM.name, team_id: CHEM.id }]
          : [],
    ),
  );
}

describe("Compute → Workers", () => {
  it("lists workers with their state and what they can run", async () => {
    onWorkers([worker(), worker({ id: "w2", name: "old-box", status: "offline", jobs: 0 })], []);
    await renderApp("/admin/compute/workers/");
    const workers = section("Workers");
    const pool = (await workers.findByRole("rowheader", { name: /gpu-pool-7f9c/ })).closest("tr");
    if (!pool) throw new Error("no row");
    expect(
      within(pool)
        .getAllByRole("cell")
        .map((c) => c.textContent),
    ).toEqual([
      "Backend",
      "Online",
      "Any project",
      "2 jobs",
      "8 CPUs, 32 GiB, 1 GPU (80 GiB)",
      "just now",
      "Revoke",
    ]);
    expect(workers.getByRole("rowheader", { name: /old-box/ })).toBeVisible();
    const tabs = screen.getByRole("navigation", { name: "Compute" });
    expect(within(tabs).getByRole("link", { name: "Workers" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("filters by tier, and shows revoked ones when asked", async () => {
    onWorkers([worker()], []);
    const { user } = await renderApp("/admin/compute/workers/");
    await section("Workers").findByRole("rowheader", { name: /gpu-pool/ });
    await user.selectOptions(section("Workers").getByRole("combobox", { name: "Tier" }), "vm");
    await user.click(section("Workers").getByRole("checkbox", { name: "Show revoked ones" }));
    await waitFor(() => {
      const last = calls("GET", "/api/v1/compute/workers").at(-1);
      expect(last?.query.get("tier")).toBe("vm");
      expect(last?.query.get("revoked")).toBe("true");
    });
  });

  it("opens filtered to the tier its address names, as the overview links it", async () => {
    onWorkers([worker()], []);
    const { router, user } = await renderApp("/admin/compute/workers/?tier=backend");
    const tier = section("Workers").getByRole("combobox", { name: "Tier" });
    expect(tier).toHaveValue("backend");
    await waitFor(() => {
      expect(calls("GET", "/api/v1/compute/workers").at(-1)?.query.get("tier")).toBe("backend");
    });
    await user.selectOptions(tier, "all");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
    expect(calls("GET", "/api/v1/compute/workers").at(-1)?.query.get("tier")).toBeNull();
  });

  it("revokes a worker, saying what happens to its jobs", async () => {
    onWorkers([worker()], []);
    answer("POST /api/v1/compute/workers/00000000-0000-7000-8000-0000000000w1/revoke", () =>
      worker({ status: "revoked", revoked_at: NOW }),
    );
    const { user } = await renderApp("/admin/compute/workers/");
    await user.click(
      await section("Workers").findByRole("button", { name: "Revoke gpu-pool-7f9c" }),
    );
    const dialog = screen.getByRole("alertdialog", { name: "Revoke gpu-pool-7f9c?" });
    expect(within(dialog).getByText(/The 2 jobs it holds are interrupted/)).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Revoke worker" }));
    expect(await screen.findByText(/Revoked gpu-pool-7f9c\./)).toBeVisible();
    expect(
      calls("POST", "/api/v1/compute/workers/00000000-0000-7000-8000-0000000000w1/revoke"),
    ).toHaveLength(1);
  });

  it("shows a self-hosted worker's teams, labels and what it's running", async () => {
    onWorkers([selfHosted()], []);
    await renderApp("/admin/compute/workers/");
    const workers = section("Workers");
    const row = (await workers.findByRole("rowheader", { name: /hplc-pc-1/ })).closest("tr");
    if (!row) throw new Error("no row");
    const cells = within(row).getAllByRole("cell");
    expect(cells[0]).toHaveTextContent("Self-hosted");
    expect(cells[2]).toHaveTextContent("Structural Biology; Chemistry: Assays");
    expect(cells[2]).toHaveTextContent("chromeleon lab-3");
    expect(within(row).getByRole("link", { name: "RUN-000042" })).toHaveAttribute(
      "href",
      "/runs/RUN-000042/",
    );
    expect(cells[3]).toHaveTextContent("chem.hplc-purity in Assays");
  });

  it("changes a self-hosted worker's teams, projects and labels", async () => {
    onWorkers([selfHosted()], []);
    onScopes();
    const path = "/api/v1/compute/workers/00000000-0000-7000-8000-0000000000w2";
    answer(`PATCH ${path}`, () => selfHosted({ teams: [CHEM], projects: [], labels: ["empower"] }));
    const { user } = await renderApp("/admin/compute/workers/");
    await user.click(await section("Workers").findByRole("button", { name: "Change hplc-pc-1" }));
    const dialog = within(screen.getByRole("dialog", { name: "Change hplc-pc-1" }));
    const teams = within(dialog.getByRole("group", { name: /Teams/ }));
    expect(teams.getByRole("checkbox", { name: "Chemistry" })).toBeChecked();
    // Leaving a team drops what was chosen of its projects.
    await user.click(teams.getByRole("checkbox", { name: "Structural Biology" }));
    const projects = within(dialog.getByRole("group", { name: /Only these projects/ }));
    // Named by the project alone; its team describes it.
    const assays = projects.getByRole("checkbox", { name: "Assays" });
    expect(assays).toHaveAccessibleDescription("Chemistry team");
    await user.click(assays);
    const labels = dialog.getByRole("textbox", { name: /Labels/ });
    await user.clear(labels);
    await user.type(labels, "Empower");
    await user.click(dialog.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/Changed hplc-pc-1\./)).toBeVisible();
    expect(calls("PATCH", path).map((c) => c.body)).toEqual([
      { labels: ["empower"], teams: [CHEM.id], projects: [] },
    ]);
  });

  it("says how to get workers when there are none", async () => {
    onWorkers([], []);
    await renderApp("/admin/compute/workers/");
    expect(await screen.findByText(/No workers have registered yet/)).toBeVisible();
    expect(section("Registration tokens").getByText("None yet.")).toBeVisible();
  });

  it("makes a registration token and shows it once", async () => {
    const tokens: Schemas["WorkerRegistrationOut"][] = [];
    onWorkers([], tokens);
    answer("POST /api/v1/compute/registration-tokens", () => {
      tokens.push(token({ workers: 0 }));
      return { ...token({ workers: 0 }), token: "iki_reg_secret-value" };
    });
    const { user } = await renderApp("/admin/compute/workers/");
    const tokensCard = section("Registration tokens");
    await user.click(await tokensCard.findByRole("button", { name: "Make token" }));
    expect(tokensCard.getByRole("textbox", { name: /What it’s for/ })).toHaveAccessibleDescription(
      /Say what it’s for\./,
    );
    expect(calls("POST", "/api/v1/compute/registration-tokens")).toEqual([]);

    await user.type(tokensCard.getByRole("textbox", { name: /What it’s for/ }), "GPU pool");
    await user.selectOptions(tokensCard.getByRole("combobox", { name: /Expires/ }), "7");
    await user.click(tokensCard.getByRole("button", { name: "Make token" }));
    expect(await tokensCard.findByText("Copy the token now.")).toBeVisible();
    expect(tokensCard.getByText("iki_reg_secret-value")).toBeVisible();
    const [made] = calls("POST", "/api/v1/compute/registration-tokens");
    const body = made?.body as { label: string; tier: string; expires_at: string };
    expect(body.label).toBe("GPU pool");
    expect(body.tier).toBe("backend");
    const days = (Date.parse(body.expires_at) - Date.now()) / 86_400_000;
    expect(days).toBeGreaterThan(6.9);
    expect(days).toBeLessThan(7.1);

    // Once it's done, the value is gone for good.
    await user.click(tokensCard.getByRole("button", { name: "Done" }));
    expect(tokensCard.queryByText("iki_reg_secret-value")).not.toBeInTheDocument();
  });

  it("makes a self-hosted token for teams, with labels, and says how to register", async () => {
    onWorkers([], []);
    onScopes();
    answer("POST /api/v1/compute/registration-tokens", () => ({
      ...token({ tier: "self_hosted", label: "Lab 3", teams: [CHEM], labels: ["chromeleon"] }),
      workers: 0,
      token: "iki_wreg_secret-value",
    }));
    const { user } = await renderApp("/admin/compute/workers/");
    const card = section("Registration tokens");
    await user.type(await card.findByRole("textbox", { name: /What it’s for/ }), "Lab 3");
    await user.selectOptions(card.getByRole("combobox", { name: /Tier/ }), "self_hosted");
    // A team first.
    await user.click(card.getByRole("button", { name: "Make token" }));
    expect(await card.findByText("Choose the teams whose runs it takes.")).toBeVisible();
    expect(calls("POST", "/api/v1/compute/registration-tokens")).toEqual([]);
    await user.click(await card.findByRole("checkbox", { name: "Chemistry" }));
    await user.click(await card.findByRole("checkbox", { name: "Assays" }));
    await user.type(card.getByRole("textbox", { name: /Labels/ }), "chromeleon, Lab-3");
    await user.click(card.getByRole("button", { name: "Make token" }));
    expect(await card.findByText("Copy the token now.")).toBeVisible();
    expect(
      card.getByText(
        `iki-worker register --url ${window.location.origin} --token iki_wreg_secret-value`,
      ),
    ).toBeVisible();
    const [made] = calls("POST", "/api/v1/compute/registration-tokens");
    expect(made?.body).toMatchObject({
      label: "Lab 3",
      tier: "self_hosted",
      teams: [CHEM.id],
      projects: [ASSAYS.id],
      labels: ["chromeleon", "lab-3"],
    });
  });

  it("makes a Container token for a Slurm cluster, and says to register on its login node", async () => {
    const tokens: Schemas["WorkerRegistrationOut"][] = [];
    onWorkers([], tokens);
    answer(
      "GET /api/v1/compute/slurm-clusters",
      page([{ name: "hpc", templates: [], workers_online: 0, accounts: 0 }]),
    );
    const made = token({
      label: "HPC login node",
      tier: "container",
      slurm_cluster: "hpc",
      workers: 0,
    });
    answer("POST /api/v1/compute/registration-tokens", () => {
      tokens.push(made);
      return { ...made, token: "iki_wreg_slurm-value" };
    });
    const { user } = await renderApp("/admin/compute/workers/");
    const card = section("Registration tokens");
    await user.type(await card.findByRole("textbox", { name: /What it’s for/ }), "HPC login node");
    // Only the container tier has Slurm clusters.
    expect(card.queryByRole("combobox", { name: /Slurm cluster/ })).not.toBeInTheDocument();
    await user.selectOptions(card.getByRole("combobox", { name: /Tier/ }), "container");
    const cluster = await card.findByRole("combobox", { name: /Slurm cluster/ });
    expect(cluster).toHaveAccessibleDescription(/submits the runs of the cluster’s templates/);
    await user.selectOptions(cluster, "hpc");
    await user.click(card.getByRole("button", { name: "Make token" }));
    expect(await card.findByText(/On a login node of hpc/)).toBeVisible();
    expect(
      card.getByText(
        `iki-worker register --url ${window.location.origin} --token iki_wreg_slurm-value`,
      ),
    ).toBeVisible();
    expect(calls("POST", "/api/v1/compute/registration-tokens")[0]?.body).toMatchObject({
      tier: "container",
      slurm_cluster: "hpc",
      teams: [],
      labels: [],
    });
    const row = (await card.findByRole("rowheader", { name: /HPC login node/ })).closest("tr");
    if (!row) throw new Error("no row");
    expect(within(row).getAllByRole("cell")[0]).toHaveTextContent(
      "ContainerOn the Slurm cluster hpc",
    );
    expect(within(row).getByRole("link", { name: "hpc" })).toHaveAttribute(
      "href",
      "/admin/compute/slurm/",
    );
  });

  it("sends no Slurm cluster for other tiers", async () => {
    onWorkers([], []);
    answer("POST /api/v1/compute/registration-tokens", () => ({ ...token(), token: "x" }));
    const { user } = await renderApp("/admin/compute/workers/");
    const card = section("Registration tokens");
    await user.type(await card.findByRole("textbox", { name: /What it’s for/ }), "GPU pool");
    await user.click(card.getByRole("button", { name: "Make token" }));
    await card.findByText("Copy the token now.");
    expect(calls("GET", "/api/v1/compute/slurm-clusters")).toEqual([]);
    expect(calls("POST", "/api/v1/compute/registration-tokens")[0]?.body).toMatchObject({
      tier: "backend",
      slurm_cluster: "",
    });
  });

  it("shows the Slurm cluster a worker submits jobs to", async () => {
    onWorkers(
      [worker({ name: "hpc-login-1", tier: "container", slurm_cluster: "hpc", jobs: 14 })],
      [],
    );
    await renderApp("/admin/compute/workers/");
    const row = (await section("Workers").findByRole("rowheader", { name: /hpc-login-1/ })).closest(
      "tr",
    );
    if (!row) throw new Error("no row");
    const cells = within(row).getAllByRole("cell");
    expect(cells[0]).toHaveTextContent("ContainerOn the Slurm cluster hpc");
    expect(cells[3]).toHaveTextContent("14 jobs");
  });

  it("revokes a token, and its workers too if asked", async () => {
    onWorkers([], [token()]);
    answer(
      "POST /api/v1/compute/registration-tokens/00000000-0000-7000-8000-0000000000t1/revoke",
      () => token({ revoked_at: NOW }),
    );
    const { user } = await renderApp("/admin/compute/workers/");
    await user.click(
      await section("Registration tokens").findByRole("button", {
        name: "Revoke GPU pool (us-east-1)",
      }),
    );
    const dialog = screen.getByRole("alertdialog", { name: "Revoke “GPU pool (us-east-1)”?" });
    await user.click(
      within(dialog).getByRole("checkbox", {
        name: "Also revoke the 3 workers that registered with it",
      }),
    );
    await user.click(within(dialog).getByRole("button", { name: "Revoke token" }));
    expect(await screen.findByText(/and the workers that registered with it\./)).toBeVisible();
    const [sentRevoke] = calls(
      "POST",
      "/api/v1/compute/registration-tokens/00000000-0000-7000-8000-0000000000t1/revoke",
    );
    expect(sentRevoke?.body).toEqual({ workers: true });
  });

  it("isn't anyone else's", async () => {
    signedIn();
    await renderApp("/admin/compute/workers/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Not found");
    expect(calls("GET", "/api/v1/compute/workers")).toEqual([]);
  });
});
