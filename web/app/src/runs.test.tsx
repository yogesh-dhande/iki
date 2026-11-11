/**
 * A project's runs and a run's page, against a fake API: the page follows a
 * run that's going through its live updates.
 */

import type { Schemas } from "@ikisci/sdk";
import { act, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { entity, KINASE, nth, renderApp, revision, withTypes } from "./entity-fixtures";
import { ACTION, attempt, run, RUN_ID } from "./run-fixtures";
import { answer, calls, FakeEventSource, page } from "./test-server";

const EVENTS = "/api/v1/runs/RUN-000001/events";

/** The log, as the server keeps it: answers from any offset. */
function withLog(text: () => string, ended: () => boolean = () => false): void {
  answer("GET /api/v1/runs/RUN-000001/attempts/1/log", (r: { query: URLSearchParams }) => {
    const all = new TextEncoder().encode(text());
    const offset = Number(r.query.get("offset") ?? 0);
    return {
      offset,
      text: new TextDecoder().decode(all.slice(offset)),
      next_offset: all.length,
      more: false,
      size: all.length,
      ended: ended(),
    };
  });
}

function onRun(found: () => Schemas["RunOut"], attempts: () => Schemas["RunAttemptOut"][]): void {
  withTypes();
  answer("GET /api/v1/projects", page([KINASE]));
  answer("GET /api/v1/actions", [ACTION]);
  answer("GET /api/v1/runs/RUN-000001", () => found());
  answer("GET /api/v1/runs/RUN-000001/attempts", () => attempts());
}

function runStream(): FakeEventSource {
  const stream = FakeEventSource.opened.filter((s) => s.url === EVENTS).at(-1);
  if (!stream) throw new Error("The run's page has no stream open");
  return stream;
}

beforeEach(() => {
  FakeEventSource.opened = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a project's runs", () => {
  it("lists them newest first, by action, with their status", async () => {
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/actions", [ACTION]);
    answer(
      "GET /api/v1/runs",
      page([
        run({ status: "succeeded", finished_at: "2026-03-14T09:01:00Z", reason: "Weekly batch" }),
        run({ id: "00000000-0000-7000-8000-0000000000r0", hrid: "RUN-000000", status: "failed" }),
      ]),
    );
    const { user } = await renderApp("/projects/kinase-atlas/runs/");
    expect(screen.getByRole("link", { name: "Runs" })).toHaveAttribute("aria-current", "page");
    const links = await screen.findAllByRole("link", { name: "Compute properties" });
    expect(links.map((l) => l.getAttribute("href"))).toEqual([
      "/runs/RUN-000001/",
      "/runs/RUN-000000/",
    ]);
    const row = (index: number) => within(nth(links, index).closest("li") ?? document.body);
    expect(row(0).getByText("Succeeded")).toHaveClass("badge-good");
    expect(row(1).getByText("Failed")).toHaveClass("badge-bad");
    expect(screen.getByText(/Weekly batch/)).toBeInTheDocument();
    expect(calls("GET", "/api/v1/runs").at(-1)?.query.get("project")).toBe(KINASE.id);
    expect(document.title).toBe("Runs · Kinase Atlas · Example Bio");

    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "Failed");
    await waitFor(() => {
      expect(calls("GET", "/api/v1/runs").at(-1)?.query.get("status")).toBe("failed");
    });
  });

  it("says when there are none", async () => {
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/actions", [ACTION]);
    answer("GET /api/v1/runs", page([]));
    await renderApp("/projects/kinase-atlas/runs/");
    expect(
      await screen.findByText(/No runs yet\. When someone runs an action in this project/),
    ).toBeInTheDocument();
  });
});

describe("a run's page", () => {
  it("shows what it ran on, with which inputs, and how far it's got", async () => {
    onRun(run, () => [attempt()]);
    withLog(() => "hello\n");
    await renderApp("/runs/RUN-000001/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      /^Compute properties\s*RUN-000001\s*Running$/,
    );
    expect(document.title).toBe("Compute properties (RUN-000001) · Example Bio");
    expect(await screen.findByRole("link", { name: "Kinase Atlas" })).toHaveAttribute(
      "href",
      "/projects/kinase-atlas/runs/",
    );
    const inputs = screen.getByRole("region", { name: "Inputs" });
    expect(within(inputs).getByRole("link", { name: "PRT-000001" })).toHaveAttribute(
      "href",
      "/entities/PRT-000001/",
    );
    expect(within(inputs).getByText("as of revision 2")).toBeInTheDocument();
    expect(within(inputs).getByText("pH").nextSibling).toHaveTextContent("7.4");
    const attempts = screen.getByRole("region", { name: "Attempts" });
    expect(await within(attempts).findByText(/On pod-7/)).toHaveTextContent(
      /environment 4f2a9c1e7b3d/,
    );
    expect(within(attempts).getByRole("progressbar", { name: "Attempt 1 progress" })).toHaveValue(
      0.25,
    );
    expect(within(attempts).getByText("25% · Weighing")).toBeInTheDocument();
    expect(await screen.findByRole("log", { name: "Log of attempt 1" })).toHaveTextContent("hello");
    expect(screen.getByRole("link", { name: "Download log" })).toHaveAttribute(
      "href",
      "/api/v1/runs/RUN-000001/attempts/1/log.txt",
    );
    const details = screen.getByRole("region", { name: "Details" });
    expect(within(details).getByText("Requested").nextSibling).toHaveTextContent(
      /^By Rosalind Franklin .+ in the web app$/,
    );
    expect(within(details).getByText(/A backend worker, in the/)).toHaveTextContent(
      "A backend worker, in the sequence-tools environment",
    );
  });

  it("follows the log as it grows, and the run until it ends", async () => {
    let log = "hello\n";
    let current = run();
    let attempts = [attempt()];
    onRun(
      () => current,
      () => attempts,
    );
    withLog(() => log);
    await renderApp("/runs/RUN-000001/");
    const box = await screen.findByRole("log", { name: "Log of attempt 1" });
    expect(box).toHaveTextContent(/^hello$/);
    const stream = runStream();
    expect(stream.init).toEqual({ withCredentials: true });

    log += "wörld\n";
    act(() => {
      stream.send("log", { attempt: 1, size: 13 });
    });
    await waitFor(() => {
      expect(box.textContent).toBe("hello\nwörld\n");
    });
    // It asked for what was new, not all of it again.
    expect(
      calls("GET", "/api/v1/runs/RUN-000001/attempts/1/log").map((c) => c.query.get("offset")),
    ).toEqual(["0", "6"]);

    current = run({
      status: "succeeded",
      finished_at: "2026-03-14T09:01:00Z",
      can: { cancel: false },
      effects: [
        {
          entity: "00000000-0000-7000-8000-00000000e001",
          hrid: "PRT-000001",
          type: "protein.Protein",
          kind: "updated",
          revision: 3,
        },
      ],
      messages: [{ level: "warning", text: "pI is outside the usual range." }],
    });
    attempts = [attempt({ state: "succeeded", finished_at: "2026-03-14T09:01:00Z", log_size: 13 })];
    act(() => {
      stream.send("run", { id: RUN_ID });
    });
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/Succeeded$/);
    });
    await waitFor(() => {
      expect(stream.readyState).toBe(FakeEventSource.CLOSED);
    });
    const results = screen.getByRole("region", { name: "Results" });
    expect(within(results).getByText(/^Updated/)).toHaveTextContent(
      "Updated PRT-000001 Protein, revision 3",
    );
    expect(screen.getByRole("region", { name: "Messages" })).toHaveTextContent(
      "Warning pI is outside the usual range.",
    );
    expect(screen.queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("reads everything again when its stream reconnects", async () => {
    onRun(run, () => [attempt()]);
    withLog(() => "hello\n");
    await renderApp("/runs/RUN-000001/");
    await screen.findByRole("log", { name: "Log of attempt 1" });
    const stream = runStream();
    const before = calls("GET", "/api/v1/runs/RUN-000001").length;
    act(() => {
      stream.send("ready", { id: RUN_ID });
    });
    expect(calls("GET", "/api/v1/runs/RUN-000001")).toHaveLength(before);
    act(() => {
      stream.send("ready", { id: RUN_ID });
    });
    await waitFor(() => {
      expect(calls("GET", "/api/v1/runs/RUN-000001")).toHaveLength(before + 1);
    });
  });

  it("doesn't open a stream for a run that has ended, and says why it failed", async () => {
    onRun(
      () =>
        run({
          status: "failed",
          finished_at: "2026-03-14T09:01:00Z",
          can: { cancel: false },
          error: {
            code: "invalid_output",
            message: "Its output didn't match what the action declares.",
            errors: [{ pointer: "/updates/0/values/pi", detail: "Not a number." }],
          },
        }),
      () => [attempt({ state: "failed", finished_at: "2026-03-14T09:01:00Z", progress: null })],
    );
    withLog(
      () => "",
      () => true,
    );
    await renderApp("/runs/RUN-000001/");
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Its output didn't match what the action declares.");
    expect(alert).toHaveTextContent("/updates/0/values/pi: Not a number.");
    expect(await screen.findByText("It printed nothing.")).toBeInTheDocument();
    expect(FakeEventSource.opened.filter((s) => s.url === EVENTS)).toEqual([]);
  });

  it("says why a queued run is waiting", async () => {
    onRun(
      () =>
        run({
          status: "queued",
          started_at: null,
          waiting: "The team’s 4 backend runs at once are all in use.",
        }),
      () => [
        attempt({ state: "queued", worker: null, started_at: null, progress: null, log_size: 0 }),
      ],
    );
    withLog(() => "");
    await renderApp("/runs/RUN-000001/");
    expect(
      screen.getByText("Waiting to start: The team’s 4 backend runs at once are all in use."),
    ).toBeInTheDocument();
    expect(await screen.findByText(/Not picked up yet/)).toBeInTheDocument();
    expect(await screen.findByText("Nothing printed yet.")).toBeInTheDocument();
  });

  it("cancels, with a reason for the record", async () => {
    onRun(run, () => [attempt()]);
    withLog(() => "");
    answer(`POST /api/v1/runs/${RUN_ID}/cancel`, () =>
      run({ cancel_requested_at: "2026-03-14T09:00:30Z", can: { cancel: false } }),
    );
    const { user } = await renderApp("/runs/RUN-000001/");
    await user.click(screen.getByRole("button", { name: "Cancel run" }));
    const dialog = screen.getByRole("alertdialog", { name: "Cancel RUN-000001?" });
    expect(dialog).toHaveTextContent(/Its worker stops the command/);
    await user.type(within(dialog).getByRole("textbox", { name: /Reason/ }), "Wrong pH");
    await user.click(within(dialog).getByRole("button", { name: "Cancel run" }));
    expect(await screen.findByText("Asked RUN-000001 to stop. Its worker ends it.")).toBeVisible();
    expect(calls("POST").at(-1)?.body).toEqual({ reason: "Wrong pH" });
    expect(screen.getByText(/^Cancelling: asked to stop/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();
  });

  it("says when there's no such run", async () => {
    withTypes();
    answer("GET /api/v1/actions", [ACTION]);
    await renderApp("/runs/RUN-999999/");
    expect(
      screen.getByText("There’s nothing here, or you don’t have access to it."),
    ).toBeInTheDocument();
  });
});

describe("an entity's provenance", () => {
  it("links to the run that set a value", async () => {
    withTypes();
    answer("GET /api/v1/entities/PRT-000001", entity());
    answer("GET /api/v1/entities/PRT-000001/provenance", {
      molecular_weight: revision({
        number: 3,
        kind: "edited",
        via: "run",
        run: { id: RUN_ID, hrid: "RUN-000001", action: "protein.compute_properties" },
      }),
      name: revision({ via: "run", run: null }),
    });
    answer("GET /api/v1/entities/PRT-000001/projects", []);
    await renderApp("/entities/PRT-000001/");
    const values = screen.getByRole("region", { name: "Values" });
    expect(await within(values).findByRole("link", { name: "RUN-000001" })).toHaveAttribute(
      "href",
      "/runs/RUN-000001/",
    );
    expect(
      within(values)
        .getByText(/RUN-000001/)
        .closest(".cell-meta"),
    ).toHaveTextContent(/^Changed by Rosalind Franklin .+ by RUN-000001 \(revision 3\)$/);
    expect(within(values).getByText(/by a run in a project you can’t see/)).toBeInTheDocument();
  });
});
