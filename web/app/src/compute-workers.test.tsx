/**
 * Compute → Workers, against a fake API: the workers and their state, revoking one, and
 * making and revoking registration tokens.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderApp } from "./entity-fixtures";
import { capacityText } from "./pages/compute-workers";
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
    owner: null,
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

describe("Compute → Workers", () => {
  it("lists workers with their state and what they can run", async () => {
    onWorkers([worker(), worker({ id: "w2", name: "old-box", status: "offline", jobs: 0 })], []);
    await renderApp("/compute/workers/");
    const workers = section("Workers");
    const pool = (await workers.findByRole("rowheader", { name: /gpu-pool-7f9c/ })).closest("tr");
    if (!pool) throw new Error("no row");
    expect(
      within(pool)
        .getAllByRole("cell")
        .map((c) => c.textContent),
    ).toEqual(["Backend", "Online", "2", "8 CPUs, 32 GiB, 1 GPU (80 GiB)", "just now", "Revoke"]);
    expect(workers.getByRole("rowheader", { name: /old-box/ })).toBeVisible();
    const tabs = screen.getByRole("navigation", { name: "Compute" });
    expect(within(tabs).getByRole("link", { name: "Workers" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("filters by tier, and shows revoked ones when asked", async () => {
    onWorkers([worker()], []);
    const { user } = await renderApp("/compute/workers/");
    await section("Workers").findByRole("rowheader", { name: /gpu-pool/ });
    await user.selectOptions(section("Workers").getByRole("combobox", { name: "Tier" }), "vm");
    await user.click(section("Workers").getByRole("checkbox", { name: "Show revoked ones" }));
    await waitFor(() => {
      const last = calls("GET", "/api/v1/compute/workers").at(-1);
      expect(last?.query.get("tier")).toBe("vm");
      expect(last?.query.get("revoked")).toBe("true");
    });
  });

  it("revokes a worker, saying what happens to its jobs", async () => {
    onWorkers([worker()], []);
    answer("POST /api/v1/compute/workers/00000000-0000-7000-8000-0000000000w1/revoke", () =>
      worker({ status: "revoked", revoked_at: NOW }),
    );
    const { user } = await renderApp("/compute/workers/");
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

  it("says how to get workers when there are none", async () => {
    onWorkers([], []);
    await renderApp("/compute/workers/");
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
    const { user } = await renderApp("/compute/workers/");
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

  it("revokes a token, and its workers too if asked", async () => {
    onWorkers([], [token()]);
    answer(
      "POST /api/v1/compute/registration-tokens/00000000-0000-7000-8000-0000000000t1/revoke",
      () => token({ revoked_at: NOW }),
    );
    const { user } = await renderApp("/compute/workers/");
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
    await renderApp("/compute/workers/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Not found");
    expect(calls("GET", "/api/v1/compute/workers")).toEqual([]);
  });
});
