/**
 * Compute → Volumes (ADR 0075), against a fake API: environments' volumes and the version
 * runs get, asking for a setup build, approving someone else's, retiring a version, and a
 * build's log as its script writes it.
 */

import type { Schemas } from "@ikisci/sdk";
import { act, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderApp } from "./entity-fixtures";
import { answer, calls, FakeEventSource, ME, page, problem, signedIn } from "./test-server";
import { timeoutText } from "./volumes";

const ENVIRONMENT = "structure_prediction.alphafold-gpu";
const COMPUTE_ADMIN = { ...ME.can, manage_compute: true };
const ADA = { id: "00000000-0000-7000-8000-0000000000a2", name: "Ada Lovelace" };
const BUILD_ID = "00000000-0000-7000-8000-0000000000s1";

function environment(
  overrides: Partial<Schemas["EnvironmentVolumesOut"]> = {},
): Schemas["EnvironmentVolumesOut"] {
  return {
    environment: ENVIRONMENT,
    label: "AlphaFold (GPU)",
    volumes: [
      {
        name: "af-db",
        size: 3 * 1024 ** 4,
        mount: "/data/af",
        access: "read_only_many",
        filled_by_setup: true,
      },
    ],
    setup: {
      script: "setup/download_dbs.sh",
      timeout: 12 * 3600,
      requires_approval: true,
      network: ["storage.googleapis.com"],
    },
    places: [
      {
        place: "iki-runs",
        build: null,
        hrid: null,
        version: null,
        data_version: null,
        ready_at: null,
      },
    ],
    waiting:
      "Waiting for its reference data (af-db): no version is ready. A Compute admin can ask for a setup build.",
    ...overrides,
  };
}

function build(overrides: Partial<Schemas["SetupBuildOut"]> = {}): Schemas["SetupBuildOut"] {
  return {
    id: BUILD_ID,
    hrid: "SETUP-000001",
    environment: ENVIRONMENT,
    place: "iki-runs",
    template: "gpu-a100",
    version: 1,
    state: "awaiting_approval",
    reason: "AlphaFold’s 2026-10 databases",
    image: "ghcr.io/acme/alphafold@sha256:9f86",
    plugin_version: "1.0.0",
    plugin_sha: "",
    requested_by: ADA,
    approved_by: null,
    approved_at: null,
    cancel_requested_at: null,
    data_version: "",
    retired_at: null,
    retired_by: null,
    current: false,
    created_at: "2026-11-13T09:00:00Z",
    started_at: null,
    finished_at: null,
    timeout: 12 * 3600,
    progress: null,
    progress_message: "",
    log_size: 0,
    error: null,
    volumes: [],
    egress: [],
    ...overrides,
  };
}

function onVolumes(builds: () => Schemas["SetupBuildOut"][]): void {
  signedIn({ can: COMPUTE_ADMIN });
  answer("GET /api/v1/compute/volumes", [environment()]);
  answer("GET /api/v1/compute/setups", () => page(builds()));
}

function card() {
  return within(screen.getByRole("region", { name: /AlphaFold \(GPU\)/ }));
}

/** Waits for the environment's card, and its builds, to load. */
async function loaded() {
  await screen.findByRole("region", { name: /AlphaFold \(GPU\)/ });
  await waitFor(() => {
    expect(card().queryByText("Loading setup builds…")).not.toBeInTheDocument();
  });
}

beforeEach(() => {
  FakeEventSource.opened = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("how setups read", () => {
  it("says timeouts in hours, minutes or seconds", () => {
    expect(timeoutText(43_200)).toBe("12 h");
    expect(timeoutText(600)).toBe("10 min");
    expect(timeoutText(90)).toBe("90 s");
  });
});

describe("Compute → Volumes", () => {
  it("is in the navigation only for Compute admins", async () => {
    onVolumes(() => []);
    await renderApp("/compute/volumes/");
    await loaded();
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Compute" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("isn't anyone else's", async () => {
    signedIn();
    await renderApp("/compute/volumes/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Not found");
    expect(calls("GET", "/api/v1/compute/volumes")).toEqual([]);
  });

  it("shows an environment's volumes, its setup, and why its runs wait", async () => {
    onVolumes(() => []);
    await renderApp("/compute/volumes/");
    await loaded();
    expect(card().getByText(/Its runs are waiting/)).toHaveTextContent(
      "Its runs are waiting. Waiting for its reference data (af-db): no version is ready.",
    );
    expect(card().getByText("af-db").closest("li")).toHaveTextContent(
      "af-db · 3.0 TiB at /data/af · filled by its setup",
    );
    expect(card().getByText("Setup", { selector: "dt" }).nextElementSibling).toHaveTextContent(
      "Runs setup/download_dbs.sh for up to 12 h. Each build waits for another Compute admin’s approval. It may reach storage.googleapis.com.",
    );
    expect(card().getByText("no version yet")).toBeInTheDocument();
    expect(await card().findByText("None yet.")).toBeInTheDocument();
    expect(calls("GET", "/api/v1/compute/setups").at(-1)?.query.get("environment")).toBe(
      ENVIRONMENT,
    );
  });

  it("asks for a setup build, saying why", async () => {
    const builds: Schemas["SetupBuildOut"][] = [];
    onVolumes(() => builds);
    answer("POST /api/v1/compute/setups", () => {
      builds.push(build({ requested_by: { id: ME.id, name: ME.name } }));
      return builds;
    });
    const { user } = await renderApp("/compute/volumes/");
    await loaded();
    await card().findByText("None yet.");
    await user.click(card().getByRole("button", { name: "Ask for a setup build" }));
    const dialog = screen.getByRole("dialog", { name: "Ask for a setup build of AlphaFold (GPU)" });
    await user.type(within(dialog).getByRole("textbox", { name: /Why/ }), "New PDB release");
    await user.click(within(dialog).getByRole("button", { name: "Ask for it" }));
    expect(
      await screen.findByText(
        "Asked for SETUP-000001. It starts once another Compute admin approves it.",
      ),
    ).toBeInTheDocument();
    expect(calls("POST", "/api/v1/compute/setups")[0]?.body).toEqual({
      environment: ENVIRONMENT,
      reason: "New PDB release",
    });
    // One at a time: while it's going, another can't be asked for.
    const row = await card().findByText(/SETUP-000001/);
    expect(
      within(row.closest("li") ?? document.body).getByText(/Waiting for another Compute admin/),
    ).toBeInTheDocument();
    expect(card().getByRole("button", { name: "Ask for a setup build" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(card().queryByRole("button", { name: /^Approve/ })).not.toBeInTheDocument();
  });

  it("approves another Compute admin's build, saying what it'll do", async () => {
    let current = build();
    onVolumes(() => [current]);
    answer(`POST /api/v1/compute/setups/${BUILD_ID}/approve`, () => {
      current = build({
        state: "queued",
        approved_by: { id: ME.id, name: ME.name },
        approved_at: "2026-11-13T09:05:00Z",
      });
      return current;
    });
    const { user } = await renderApp("/compute/volumes/");
    await loaded();
    await user.click(
      await card().findByRole("button", { name: "Approve SETUP-000001, version 1" }),
    );
    const dialog = screen.getByRole("alertdialog", { name: "Approve SETUP-000001?" });
    expect(dialog).toHaveTextContent(
      "Ada Lovelace asked for it: “AlphaFold’s 2026-10 databases”. It starts a machine on gpu-a100 that runs setup/download_dbs.sh for up to 12 h, reaching storage.googleapis.com.",
    );
    await user.click(within(dialog).getByRole("button", { name: "Approve" }));
    expect(
      await screen.findByText("Approved SETUP-000001. It starts as soon as a machine is ready."),
    ).toBeInTheDocument();
    expect(await card().findByText("Queued")).toHaveClass("badge-busy");
    expect(card().getByText(/approved by/)).toHaveTextContent(`approved by ${ME.name}`);
  });

  it("says why an approval was refused", async () => {
    onVolumes(() => [build()]);
    answer(
      `POST /api/v1/compute/setups/${BUILD_ID}/approve`,
      problem(409, "refused", "It was cancelled."),
    );
    const { user } = await renderApp("/compute/volumes/");
    await loaded();
    await user.click(
      await card().findByRole("button", { name: "Approve SETUP-000001, version 1" }),
    );
    const dialog = screen.getByRole("alertdialog", { name: "Approve SETUP-000001?" });
    await user.click(within(dialog).getByRole("button", { name: "Approve" }));
    expect(await within(dialog).findByText("It was cancelled.")).toBeInTheDocument();
  });

  it("shows the version runs get, and retires it", async () => {
    let current = build({
      state: "succeeded",
      approved_by: { id: ME.id, name: ME.name },
      approved_at: "2026-11-13T09:05:00Z",
      started_at: "2026-11-13T09:06:00Z",
      finished_at: "2026-11-13T11:36:00Z",
      data_version: "AlphaFold DBs 2026-10",
      current: true,
      volumes: [
        {
          name: "af-db",
          state: "ready",
          size: 3 * 1024 ** 4,
          kind: "snapshot",
          handle: "iki-setup-x-0",
          message: "",
        },
      ],
    });
    signedIn({ can: COMPUTE_ADMIN });
    answer("GET /api/v1/compute/volumes", [
      environment({
        waiting: "",
        places: [
          {
            place: "iki-runs",
            build: BUILD_ID,
            hrid: "SETUP-000001",
            version: 1,
            data_version: "AlphaFold DBs 2026-10",
            ready_at: "2026-11-13T11:36:00Z",
          },
        ],
      }),
    ]);
    answer("GET /api/v1/compute/setups", () => page([current]));
    answer(`POST /api/v1/compute/setups/${BUILD_ID}/retire`, () => {
      current = {
        ...current,
        current: false,
        retired_at: "2026-11-14T09:00:00Z",
        retired_by: { id: ME.id, name: ME.name },
      };
      return current;
    });
    const { user } = await renderApp("/compute/volumes/");
    await loaded();
    expect(await card().findByText(/^version 1 \(SETUP-000001/)).toHaveTextContent(
      "version 1 (SETUP-000001, AlphaFold DBs 2026-10), ready",
    );
    expect(card().queryByText(/Its runs are waiting/)).not.toBeInTheDocument();
    expect(card().getByText("Runs get this")).toHaveClass("badge-good");
    expect(card().getByText(/took/)).toHaveTextContent("took 2 h 30 min");
    expect(card().getByText("AlphaFold DBs 2026-10", { selector: "strong" })).toBeInTheDocument();
    await user.click(card().getByRole("button", { name: "Retire SETUP-000001, version 1" }));
    const dialog = screen.getByRole("alertdialog", { name: "Retire version 1?" });
    expect(dialog).toHaveTextContent(
      "New machines get the newest version that isn’t retired; if there’s none, the environment’s runs wait for one.",
    );
    await user.click(within(dialog).getByRole("button", { name: "Retire version" }));
    expect(await screen.findByText("Retired version 1 (SETUP-000001).")).toBeInTheDocument();
    expect(await card().findByText("Retired")).toHaveClass("badge-muted");
    expect(card().queryByRole("button", { name: /^Retire/ })).not.toBeInTheDocument();
  });

  it("follows a running build's log, progress and network use as they change", async () => {
    let current = build({
      state: "running",
      requested_by: { id: ME.id, name: ME.name },
      approved_by: ADA,
      approved_at: "2026-11-13T09:05:00Z",
      started_at: "2026-11-13T09:06:00Z",
      progress: 0.4,
      progress_message: "Downloading uniref90",
      log_size: 9,
      egress: [
        {
          host: "storage.googleapis.com",
          port: 443,
          refused: "",
          connections: 2,
          bytes_received: 2048,
        },
        {
          host: "example.com",
          port: 443,
          refused: "Its setup doesn't declare example.com.",
          connections: 1,
          bytes_received: 0,
        },
      ],
    });
    let log = "fetching\n";
    onVolumes(() => [current]);
    answer(`GET /api/v1/compute/setups/${BUILD_ID}/log`, (r: { query: URLSearchParams }) => {
      const all = new TextEncoder().encode(log);
      const offset = Number(r.query.get("offset") ?? 0);
      return {
        offset,
        text: new TextDecoder().decode(all.slice(offset)),
        size: all.length,
        finished: false,
      };
    });
    const { user } = await renderApp("/compute/volumes/");
    await loaded();
    expect(
      await card().findByRole("progressbar", { name: "SETUP-000001 progress" }),
    ).toHaveAttribute("value", "0.4");
    expect(card().getByText(/Downloading uniref90/)).toHaveTextContent(
      "40% · Downloading uniref90",
    );
    await user.click(card().getByRole("button", { name: "Show log of SETUP-000001" }));
    expect(await card().findByRole("log", { name: "Log of SETUP-000001" })).toHaveTextContent(
      "fetching",
    );
    const network = card().getByRole("list", { name: "Network use of SETUP-000001" });
    expect(
      within(network)
        .getAllByRole("listitem")
        .map((i) => i.textContent),
    ).toEqual([
      "storage.googleapis.com · 2 connections, 2.0 KiB received",
      "example.com · refused: Its setup doesn't declare example.com.",
    ]);
    // Its stream says the log grew: what's new is read.
    const stream = FakeEventSource.opened.find(
      (s) => s.url === `/api/v1/compute/setups/${BUILD_ID}/events`,
    );
    if (!stream) throw new Error("no stream for the build");
    log = "fetching\nunpacking\n";
    current = { ...current, log_size: 19 };
    act(() => {
      stream.send("setup", { id: BUILD_ID });
    });
    await waitFor(() => {
      expect(card().getByRole("log", { name: "Log of SETUP-000001" })).toHaveTextContent(
        "fetching unpacking",
      );
    });
    expect(calls("GET", `/api/v1/compute/setups/${BUILD_ID}/log`).at(-1)?.query.get("offset")).toBe(
      "9",
    );
  });

  it("says what a build waits for before and after its script", async () => {
    const approved = { approved_by: ADA, approved_at: "2026-11-13T09:05:00Z" };
    onVolumes(() => [
      build({
        ...approved,
        state: "queued",
        place: "us-east-1",
        template: "a100-spot",
        progress_message:
          "Waiting for a machine: EC2 has no p4d.24xlarge free (on_demand in subnet-0a1b: InsufficientInstanceCapacity). Iki tries again shortly.",
      }),
      build({
        ...approved,
        id: "00000000-0000-7000-8000-0000000000s0",
        hrid: "SETUP-000000",
        state: "snapshotting",
        progress_message: "Indexed the databases",
        volumes: [
          {
            name: "af-db",
            state: "creating",
            size: 3 * 1024 ** 4,
            kind: "snapshot",
            handle: "snap-0123456789abcdef0",
            message: "Being made fast to restore: us-east-1a (optimizing)",
          },
        ],
      }),
    ]);
    await renderApp("/compute/volumes/");
    await loaded();
    expect(card().getByText(/EC2 has no p4d.24xlarge free/)).toBeInTheDocument();
    expect(
      card().getByText(
        "Its script succeeded; its volumes are being snapshotted. af-db: Being made fast to restore: us-east-1a (optimizing).",
      ),
    ).toBeInTheDocument();
    // What its script last said doesn't linger once it's done.
    expect(card().queryByText(/Indexed the databases/)).not.toBeInTheDocument();
  });

  it("says where a version on a shared file system is", async () => {
    const directory = "structure_prediction.alphafold-gpu/af-db/SETUP-000001";
    onVolumes(() => [
      build({
        approved_by: ADA,
        approved_at: "2026-11-13T09:05:00Z",
        started_at: "2026-11-13T09:06:00Z",
        state: "snapshotting",
        log_size: 0,
        volumes: [
          {
            name: "af-db",
            state: "ready",
            size: 3 * 1024 ** 4,
            kind: "directory",
            handle: directory,
            message: "",
          },
        ],
      }),
    ]);
    answer(`GET /api/v1/compute/setups/${BUILD_ID}/log`, () => ({
      offset: 0,
      text: "",
      size: 0,
      finished: false,
    }));
    const { user } = await renderApp("/compute/volumes/");
    await loaded();
    expect(
      card().getByText("Its script succeeded; it’s ready once its machine has stopped."),
    ).toBeInTheDocument();
    await user.click(card().getByRole("button", { name: "Show log of SETUP-000001" }));
    const volumes = card().getByRole("list", { name: "Volumes of SETUP-000001" });
    expect(within(volumes).getByRole("listitem")).toHaveTextContent(
      `af-db · 3.0 TiB · ready · ${directory}`,
    );
  });

  it("cancels a build, keeping it if you change your mind", async () => {
    let current = build({ state: "queued", approved_by: ADA, approved_at: "2026-11-13T09:05:00Z" });
    onVolumes(() => [current]);
    answer(`POST /api/v1/compute/setups/${BUILD_ID}/cancel`, () => {
      current = {
        ...current,
        state: "cancelled",
        finished_at: "2026-11-13T09:07:00Z",
        error: { code: "cancelled", message: "Cancelled by Sam." },
      };
      return current;
    });
    const { user } = await renderApp("/compute/volumes/");
    await loaded();
    await user.click(await card().findByRole("button", { name: "Cancel SETUP-000001, version 1" }));
    let dialog = screen.getByRole("alertdialog", { name: "Cancel SETUP-000001?" });
    await user.click(within(dialog).getByRole("button", { name: "Keep it" }));
    expect(calls("POST", `/api/v1/compute/setups/${BUILD_ID}/cancel`)).toEqual([]);
    await user.click(card().getByRole("button", { name: "Cancel SETUP-000001, version 1" }));
    dialog = screen.getByRole("alertdialog", { name: "Cancel SETUP-000001?" });
    expect(dialog).toHaveTextContent("It won’t start.");
    await user.click(within(dialog).getByRole("button", { name: "Cancel build" }));
    expect(await screen.findByText("Cancelled SETUP-000001.")).toBeInTheDocument();
    expect(await card().findByText("Cancelled", { selector: ".badge" })).toHaveClass("badge-quiet");
    expect(card().getByText("Cancelled by Sam.")).toBeInTheDocument();
  });
});
