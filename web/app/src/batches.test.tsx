/**
 * A batch's page, against a fake API: its runs, how far they've got, and
 * cancelling the rest (ADR 0055).
 */

import type { Schemas } from "@ikisci/sdk";
import { act, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { KINASE, renderApp, withTypes } from "./entity-fixtures";
import { ACTION, batch, BATCH_ID, run } from "./run-fixtures";
import { BATCH_REFRESH_MS } from "./runs";
import { answer, calls, FakeEventSource, page } from "./test-server";

const EVENTS = "/api/v1/batches/BAT-000001/events";
const BATCHED = { id: BATCH_ID, hrid: "BAT-000001" };

function member(n: number, overrides: Partial<Schemas["RunOut"]> = {}): Schemas["RunOut"] {
  return run({
    id: `00000000-0000-7000-8000-0000000000r${String(n)}`,
    hrid: `RUN-00000${String(n)}`,
    batch: BATCHED,
    inputs: [
      {
        name: "protein",
        entity: `00000000-0000-7000-8000-00000000e00${String(n)}`,
        hrid: `PRT-00000${String(n)}`,
        revision: 1,
      },
    ],
    ...overrides,
  });
}

function onBatch(found: () => Schemas["BatchOut"], runs: () => Schemas["RunOut"][]): void {
  withTypes();
  answer("GET /api/v1/projects", page([KINASE]));
  answer("GET /api/v1/actions", [{ ...ACTION, batchable: 100 }]);
  answer("GET /api/v1/batches/BAT-000001", () => found());
  answer("GET /api/v1/runs", (r: { query: URLSearchParams }) => {
    const status = r.query.get("status");
    return page(runs().filter((x) => !status || x.status === status));
  });
}

function batchStream(): FakeEventSource {
  const stream = FakeEventSource.opened.filter((s) => s.url === EVENTS).at(-1);
  if (!stream) throw new Error("The batch's page has no stream open");
  return stream;
}

beforeEach(() => {
  FakeEventSource.opened = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("a batch's page", () => {
  it("shows its runs, one for each entity, and how far they've got", async () => {
    onBatch(batch, () => [
      member(1, { status: "succeeded", finished_at: "2026-03-14T09:01:00Z" }),
      member(2),
      member(3, { status: "failed", error: { code: "failed", message: "Bad residue X" } }),
    ]);
    await renderApp("/batches/BAT-000001/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Compute properties BAT-000001",
    );
    const progress = screen.getByRole("region", { name: "3 runs, one for each Protein" });
    expect(within(progress).getByRole("progressbar", { name: "BAT-000001 progress" })).toHaveValue(
      1,
    );
    expect(progress).toHaveTextContent("1 of 3 finished");
    expect(
      within(progress).getByRole("list", { name: "Runs of BAT-000001 by status" }),
    ).toHaveTextContent("Running 2Succeeded 1");
    expect(progress).toHaveTextContent("Weights for the kinase panel");
    expect(progress).toHaveTextContent(/its runs go up to 100 to a job/);
    const runs = await screen.findByRole("region", { name: "Runs" });
    const links = within(runs).getAllByRole("link");
    expect(links.map((l) => l.textContent)).toEqual([
      "On PRT-000001",
      "On PRT-000002",
      "On PRT-000003",
    ]);
    expect(links[0]).toHaveAttribute("href", "/runs/RUN-000001/");
    expect(runs).toHaveTextContent("Bad residue X");
    expect(calls("GET", "/api/v1/runs")[0]?.query.get("batch")).toBe("BAT-000001");
    expect(batchStream().init).toEqual({ withCredentials: true });
  });

  it("shows only its runs with a status", async () => {
    onBatch(
      () => batch({ statuses: { succeeded: 1, failed: 1, running: 1 } }),
      () => [member(1, { status: "succeeded" }), member(2), member(3, { status: "failed" })],
    );
    const { user } = await renderApp("/batches/BAT-000001/");
    await screen.findAllByRole("link", { name: /^On PRT/ });
    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "failed");
    await waitFor(() => {
      expect(screen.getAllByRole("link", { name: /^On PRT/ }).map((l) => l.textContent)).toEqual([
        "On PRT-000003",
      ]);
    });
    expect(screen.getByRole("option", { name: "Failed (1)" })).toBeInTheDocument();
  });

  it("reads its runs again as they end, once for news that comes together", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let current = batch();
    let runs = [member(1, { status: "succeeded" }), member(2), member(3)];
    onBatch(
      () => current,
      () => runs,
    );
    await renderApp("/batches/BAT-000001/");
    await screen.findAllByRole("link", { name: /^On PRT/ });
    const stream = batchStream();
    const before = calls("GET", "/api/v1/batches/BAT-000001").length;
    current = batch({
      statuses: { succeeded: 3 },
      finished_at: "2026-03-14T09:02:00Z",
      can: { cancel: false },
    });
    runs = [1, 2, 3].map((n) => member(n, { status: "succeeded" }));
    act(() => {
      stream.send("run", { id: runs[1]?.id });
      stream.send("run", { id: runs[2]?.id });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(BATCH_REFRESH_MS);
    });
    await waitFor(() => {
      expect(
        screen.getByRole("region", { name: "3 runs, one for each Protein" }),
      ).toHaveTextContent("All finished");
    });
    expect(calls("GET", "/api/v1/batches/BAT-000001")).toHaveLength(before + 1);
    await waitFor(() => {
      expect(stream.readyState).toBe(FakeEventSource.CLOSED);
    });
    expect(screen.queryByRole("button", { name: "Cancel the rest" })).not.toBeInTheDocument();
  });

  it("cancels the rest, with a reason for the record", async () => {
    onBatch(batch, () => [member(1, { status: "succeeded" }), member(2), member(3)]);
    answer(`POST /api/v1/batches/${BATCH_ID}/cancel`, () =>
      batch({
        statuses: { succeeded: 1, cancelled: 2 },
        can: { cancel: false },
        finished_at: "2026-03-14T09:02:00Z",
      }),
    );
    const { user } = await renderApp("/batches/BAT-000001/");
    await user.click(screen.getByRole("button", { name: "Cancel the rest" }));
    const dialog = screen.getByRole("alertdialog", { name: "Cancel 2 runs of BAT-000001?" });
    expect(dialog).toHaveTextContent(/Runs that have finished keep their results/);
    await user.type(within(dialog).getByRole("textbox", { name: /Reason/ }), "Wrong panel");
    await user.click(within(dialog).getByRole("button", { name: "Cancel them" }));
    expect(await screen.findByText("Cancelled the rest of BAT-000001.")).toBeVisible();
    expect(calls("POST").at(-1)?.body).toEqual({ reason: "Wrong panel" });
  });

  it("says when there's no such batch", async () => {
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/actions", [ACTION]);
    await renderApp("/batches/BAT-000009/");
    expect(
      screen.getByText("There’s nothing here, or you don’t have access to it."),
    ).toBeInTheDocument();
  });
});

describe("a run in a batch", () => {
  it("links to its batch, and says when it ran in one job with others", async () => {
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/actions", [ACTION]);
    answer("GET /api/v1/runs/RUN-000002", member(2));
    answer("GET /api/v1/runs/RUN-000002/attempts", [
      {
        attempt: 1,
        state: "running",
        worker: "pod-7",
        environment_build: "",
        queued_at: "2026-03-14T09:00:00Z",
        started_at: "2026-03-14T09:00:05Z",
        finished_at: null,
        progress: 0.5,
        progress_message: "",
        log_size: 0,
        error: null,
        network: [],
        secrets: [],
        volumes: {},
        pack: { runs: 3, reported_as: "RUN-000001" },
      },
    ]);
    answer("GET /api/v1/runs/RUN-000002/attempts/1/log", {
      offset: 0,
      text: "",
      next_offset: 0,
      more: false,
      size: 0,
      ended: false,
    });
    await renderApp("/runs/RUN-000002/");
    const details = screen.getByRole("region", { name: "Details" });
    expect(within(details).getByRole("link", { name: "BAT-000001" })).toHaveAttribute(
      "href",
      "/batches/BAT-000001/",
    );
    const attempts = await screen.findByRole("region", { name: "Attempts" });
    await waitFor(() => {
      expect(attempts).toHaveTextContent(
        "Ran in one job with 2 other runs of its batch, led by RUN-000001. They share its log, progress and network use.",
      );
    });
  });
});
