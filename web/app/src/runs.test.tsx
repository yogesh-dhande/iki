/**
 * A project's runs and a run's page, against a fake API: the page follows a
 * run that's going through its live updates.
 */

import type { Schemas } from "@ikisci/sdk";
import { act, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { entity, KINASE, nth, renderApp, revision, withTypes } from "./entity-fixtures";
import { ACTION, approval, attempt, run, RUN_ID } from "./run-fixtures";
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
  answer("GET /api/v1/runs/RUN-000001/artifacts", []);
}

function artifact(overrides: Partial<Schemas["RunArtifactOut"]> = {}): Schemas["RunArtifactOut"] {
  return {
    id: "00000000-0000-7000-8000-0000000000a1",
    attempt: 1,
    path: "alignment.txt",
    name: "alignment.txt",
    media_type: "text/plain",
    size: 2048,
    sha256: "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    state: "kept",
    created_at: "2026-03-14T09:01:00Z",
    expires_at: "2026-06-12T09:01:00Z",
    deleted_at: null,
    ...overrides,
  };
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

  it("names an import's file, and the plugin settings it ran with", async () => {
    const FILE = "00000000-0000-7000-8000-00000000f001";
    const HIDDEN = "00000000-0000-7000-8000-00000000f002";
    const [ph] = ACTION.params as [Schemas["ParamOut"]];
    const IMPORT = {
      ...ACTION,
      name: "protein.import_proteins",
      inputs: [],
      params: [
        { ...ph, name: "file", label: "FASTA file", type: "file" as const },
        { ...ph, name: "extra", label: "Extra file", type: "file" as const },
      ],
    };
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/actions", [IMPORT]);
    const imported = run({
      action: IMPORT.name,
      inputs: [],
      params: { file: FILE, extra: HIDDEN },
      files: { [FILE]: { name: "kinases.fasta", media_type: "chemical/seq-aa-fasta", size: 2048 } },
      settings: { default_model: "colabfold", allowed_presets: ["monomer"] },
    });
    answer("GET /api/v1/runs/RUN-000001", imported);
    answer("GET /api/v1/runs/RUN-000001/attempts", []);
    await renderApp("/runs/RUN-000001/");
    const inputs = screen.getByRole("region", { name: "Inputs" });
    expect(await within(inputs).findByRole("link", { name: "kinases.fasta" })).toHaveAttribute(
      "href",
      `/api/v1/files/${FILE}/content`,
    );
    expect(within(inputs).getByText("2.0 KiB · chemical/seq-aa-fasta")).toBeInTheDocument();
    expect(within(inputs).getByText("Extra file").nextSibling).toHaveTextContent(
      "A file you can’t read",
    );
    const details = screen.getByRole("region", { name: "Details" });
    expect(within(details).getByText("Plugin settings").nextSibling).toHaveTextContent(
      'default_model: colabfold; allowed_presets: ["monomer"]',
    );
  });

  it("says what a run may reach, and what each attempt reached or was refused", async () => {
    const at = "2026-03-14T09:00:10Z";
    const reached = { port: 443, first_at: at, last_at: at };
    onRun(
      () => run({ action: "protein.fetch_proteins", network: ["rest.uniprot.org"] }),
      () => [
        attempt({
          secrets: ["UNIPROT_TOKEN"],
          network: [
            {
              ...reached,
              host: "rest.uniprot.org",
              refused: null,
              connections: 2,
              bytes_sent: 1024,
              bytes_received: 3 * 1024 * 1024,
            },
            {
              ...reached,
              host: "www.uniprot.org",
              refused: "protein.fetch_proteins doesn't declare www.uniprot.org.",
              connections: 1,
              bytes_sent: 0,
              bytes_received: 0,
            },
          ],
        }),
      ],
    );
    withLog(() => "");
    await renderApp("/runs/RUN-000001/");
    const details = screen.getByRole("region", { name: "Details" });
    expect(await within(details).findByText("Network")).toBeInTheDocument();
    expect(within(details).getByText("Network").nextSibling).toHaveTextContent(
      "May reach rest.uniprot.org, through Iki’s egress proxy",
    );
    const used = await screen.findByRole("list", { name: "Network use of attempt 1" });
    expect(screen.getByText("UNIPROT_TOKEN").closest("p")).toHaveTextContent(
      "Given UNIPROT_TOKEN from this deployment’s secrets",
    );
    const [ok, refused] = within(used).getAllByRole("listitem");
    expect(ok).toHaveTextContent(
      "rest.uniprot.org · 2 connections, 3.0 MiB received, 1.0 KiB sent",
    );
    expect(refused).toHaveTextContent(
      "www.uniprot.org · refused: protein.fetch_proteins doesn't declare www.uniprot.org.",
    );
  });

  it("says when a run couldn't reach the internet", async () => {
    onRun(
      () => run(),
      () => [attempt()],
    );
    withLog(() => "");
    await renderApp("/runs/RUN-000001/");
    const details = screen.getByRole("region", { name: "Details" });
    expect(await within(details).findByText("Network")).toBeInTheDocument();
    expect(within(details).getByText("Network").nextSibling).toHaveTextContent(
      "None: it couldn’t reach the internet",
    );
    expect(screen.queryByRole("list", { name: /Network use/ })).not.toBeInTheDocument();
  });

  it("says what its machine has cost so far, and in the end", async () => {
    let cost: Schemas["RunOut"]["cost"] = {
      amount: 0.42,
      currency: "USD",
      seconds: 1260,
      final: false,
    };
    onRun(
      () => run({ tier: "container", cost }),
      () => [attempt()],
    );
    withLog(() => "");
    await renderApp("/runs/RUN-000001/");
    const details = screen.getByRole("region", { name: "Details" });
    expect(await within(details).findByText("Cost")).toBeInTheDocument();
    expect(within(details).getByText("Cost").nextSibling).toHaveTextContent(
      "$0.42 so far for 21 min of machine time",
    );
    cost = { ...cost, amount: 0.5, seconds: 1500, final: true };
    act(() => {
      runStream().send("run", { id: RUN_ID });
    });
    await waitFor(() => {
      expect(within(details).getByText("Cost").nextSibling).toHaveTextContent(
        "$0.50 for 25 min of machine time",
      );
    });
  });

  it("says a run's machine time when its template has no price, and nothing on the backend", async () => {
    onRun(
      () =>
        run({ tier: "container", cost: { amount: 0, currency: null, seconds: 60, final: true } }),
      () => [attempt()],
    );
    withLog(() => "");
    await renderApp("/runs/RUN-000001/");
    const details = screen.getByRole("region", { name: "Details" });
    expect(await within(details).findByText("Cost")).toBeInTheDocument();
    expect(within(details).getByText("Cost").nextSibling).toHaveTextContent(
      "1 min of machine time (its machine’s template has no price)",
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
      can: { cancel: false, reproduce: false, run_again: false },
      effects: [
        {
          entity: "00000000-0000-7000-8000-00000000e001",
          hrid: "PRT-000001",
          type: "protein.Protein",
          kind: "updated",
          revision: 3,
        },
      ],
      effect_counts: { created: 0, updated: 1, reused: 0 },
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

  it("pages through what an import made, and says how far importing has got", async () => {
    const effect = (n: number): Schemas["EffectOut"] => ({
      entity: `00000000-0000-7000-8000-${String(n).padStart(12, "0")}`,
      hrid: `PRT-${String(n).padStart(6, "0")}`,
      type: "protein.Protein",
      kind: "created",
      revision: 1,
    });
    const range = (from: number, to: number) =>
      Array.from({ length: to - from }, (_, i) => effect(from + i));
    onRun(
      () =>
        run({
          status: "succeeded",
          finished_at: "2026-03-14T09:01:00Z",
          can: { cancel: false, reproduce: false, run_again: false },
          effects: range(1, 101),
          effects_next_cursor: "after-100",
          effect_counts: { created: 250, updated: 0, reused: 3 },
        }),
      () => [
        attempt({
          state: "succeeded",
          finished_at: "2026-03-14T09:01:00Z",
          bulk_import: {
            phase: "ended",
            size: 3_000_000,
            read: 3_000_000,
            lines: 253,
            staged: 253,
            created: 250,
            reused: 3,
          },
        }),
      ],
    );
    answer("GET /api/v1/runs/RUN-000001/effects", (r: { query: URLSearchParams }) =>
      r.query.get("cursor") === "after-100"
        ? { items: range(101, 201), next_cursor: "after-200" }
        : { items: range(201, 254), next_cursor: null },
    );
    await renderApp("/runs/RUN-000001/");
    const results = await screen.findByRole("region", { name: "Results" });
    expect(results).toHaveTextContent(
      "It created 250 entities and found 3 that already existed. Showing 100.",
    );
    const list = within(results).getByRole("list", { name: "Entities it changed" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(100);
    expect(screen.getByText(/Gave its creates in a file/)).toHaveTextContent(
      "Gave its creates in a file of 2.9 MiB, 253 lines.",
    );

    within(results).getByRole("button", { name: "Show more" }).click();
    await waitFor(() => {
      expect(within(list).getAllByRole("listitem")).toHaveLength(200);
    });
    within(results).getByRole("button", { name: "Show more" }).click();
    await waitFor(() => {
      expect(within(list).getAllByRole("listitem")).toHaveLength(253);
    });
    expect(within(results).queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
    expect(results).toHaveTextContent("Showing 253.");
    expect(
      calls("GET", "/api/v1/runs/RUN-000001/effects").map((c) => c.query.get("cursor")),
    ).toEqual(["after-100", "after-200"]);
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
          can: { cancel: false, reproduce: false, run_again: false },
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

  it("says what each attempt saved, and what it resumed from", async () => {
    const saved = {
      attempt: 1,
      saved_at: "2026-03-14T09:00:30Z",
      size: 3 * 1024 ** 2,
      sha256: "a".repeat(64),
      kept: true,
    };
    onRun(run, () => [
      attempt({ state: "interrupted", finished_at: "2026-03-14T09:00:40Z", checkpoint: saved }),
      attempt({ attempt: 2, resumed_from: saved }),
    ]);
    await renderApp("/runs/RUN-000001/");
    const attempts = await screen.findByRole("region", { name: "Attempts" });
    expect(await within(attempts).findByText(/^Saved a checkpoint/)).toHaveTextContent(
      /^Saved a checkpoint of 3\.0 MiB .+, for a later attempt to resume from$/,
    );
    expect(within(attempts).getByText(/^Resumed from/)).toHaveTextContent(
      /^Resumed from the checkpoint attempt 1 saved .+/,
    );
  });

  it("lists the artifacts it kept, until when, and those deleted since", async () => {
    onRun(
      () => run({ status: "succeeded", finished_at: "2026-03-14T09:01:00Z" }),
      () => [attempt({ state: "succeeded", finished_at: "2026-03-14T09:01:00Z" })],
    );
    withLog(
      () => "",
      () => true,
    );
    answer("GET /api/v1/runs/RUN-000001/artifacts", [
      artifact({
        id: "00000000-0000-7000-8000-0000000000a0",
        path: "msas/uniref90.a3m",
        name: "uniref90.a3m",
        state: "deleted",
        deleted_at: "2026-06-12T10:17:00Z",
      }),
      artifact(),
    ]);
    await renderApp("/runs/RUN-000001/");
    const card = await screen.findByRole("region", { name: "Artifacts" });
    const kept = within(card).getByRole("link", { name: "alignment.txt" });
    expect(kept).toHaveAttribute(
      "href",
      "/api/v1/runs/RUN-000001/artifacts/00000000-0000-7000-8000-0000000000a1/content",
    );
    expect(kept).toHaveAttribute("download", "alignment.txt");
    const [, deleted, rest] = within(card).getAllByRole("row");
    if (!deleted || !rest) throw new Error("Not two artifacts");
    const rows = [undefined, deleted, rest];
    expect(rows[1]).toHaveTextContent("msas/uniref90.a3m");
    expect(rows[1]).toHaveTextContent("Deleted 12 Jun 2026, its retention over");
    expect(within(deleted).queryByRole("link")).toBeNull();
    expect(rows[2]).toHaveTextContent("2.0 KiB");
    expect(rows[2]).toHaveTextContent("Until 12 Jun 2026");
    expect(rows[2]).toHaveTextContent("SHA-256 9f86d081884c…");
    // One attempt: no column for it.
    expect(within(card).queryByRole("columnheader", { name: "Attempt" })).toBeNull();
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

  it("says a client-tier run waits for a worker on its requester's computer", async () => {
    const queued = { state: "queued", worker: null, started_at: null, progress: null } as const;
    onRun(
      () => run({ status: "queued", started_at: null, tier: "client", needs_worker: "yours" }),
      () => [attempt({ ...queued, log_size: 0 })],
    );
    withLog(() => "");
    await renderApp("/runs/RUN-000001/");
    const yours = screen.getByText(/Waiting for a worker on your computer/).closest(".alert");
    expect(yours).toHaveTextContent("Start iki-worker, or sign one in from Your workers.");
    expect(screen.getByRole("link", { name: "Your workers" })).toHaveAttribute(
      "href",
      "/account/workers/",
    );
  });

  it("names whose computer a run waits for when it isn't yours", async () => {
    onRun(
      () => run({ status: "queued", started_at: null, tier: "client", needs_worker: "theirs" }),
      () => [
        attempt({ state: "queued", worker: null, started_at: null, progress: null, log_size: 0 }),
      ],
    );
    withLog(() => "");
    await renderApp("/runs/RUN-000001/");
    const name = run().requested_by.name;
    expect(
      screen.getByText(new RegExp(`Waiting for a worker on ${name}’s computer`)),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Your workers" })).not.toBeInTheDocument();
  });

  it("cancels, with a reason for the record", async () => {
    onRun(run, () => [attempt()]);
    withLog(() => "");
    answer(`POST /api/v1/runs/${RUN_ID}/cancel`, () =>
      run({
        cancel_requested_at: "2026-03-14T09:00:30Z",
        can: { cancel: false, reproduce: false, run_again: false },
      }),
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

  describe("reuse and Reproduce exactly", () => {
    const AGAIN = "00000000-0000-7000-8000-0000000000r2";
    const done = {
      status: "succeeded" as const,
      finished_at: "2026-03-14T09:01:00Z",
      plugin_sha: "a".repeat(40),
    };
    const original = { id: RUN_ID, hrid: "RUN-000001", action: ACTION.name };

    function onAgain(found: () => Schemas["RunOut"]): void {
      answer("GET /api/v1/runs/RUN-000002", () => found());
      answer("GET /api/v1/runs/RUN-000002/attempts", () => [attempt({ state: "queued" })]);
      answer("GET /api/v1/runs/RUN-000002/attempts/1/log", {
        offset: 0,
        text: "",
        next_offset: 0,
        more: false,
        size: 0,
        ended: false,
      });
    }

    it("reproduces a run exactly, with a reason for the record", async () => {
      onRun(
        () => run({ ...done, can: { cancel: false, reproduce: true, run_again: false } }),
        () => [attempt({ state: "succeeded" })],
      );
      withLog(
        () => "",
        () => true,
      );
      const queued = () =>
        run({ id: AGAIN, hrid: "RUN-000002", status: "queued", reproduces: original });
      answer(`POST /api/v1/runs/${RUN_ID}/reproduce`, queued);
      onAgain(queued);
      const { user } = await renderApp("/runs/RUN-000001/");
      await user.click(await screen.findByRole("button", { name: "Reproduce exactly" }));
      const dialog = screen.getByRole("alertdialog", { name: "Reproduce RUN-000001 exactly?" });
      expect(dialog).toHaveTextContent(/It changes no entities/);
      await user.type(within(dialog).getByRole("textbox", { name: /Reason/ }), "Audit");
      await user.click(within(dialog).getByRole("button", { name: "Reproduce" }));
      expect(await screen.findByText("Started RUN-000002, reproducing RUN-000001.")).toBeVisible();
      expect(calls("POST").at(-1)?.body).toEqual({ reason: "Audit" });
      expect(screen.getByText(/^Reproduces/)).toHaveTextContent(
        /When it ends, it compares its results with those/,
      );
      expect(screen.getByRole("link", { name: "RUN-000001" })).toHaveAttribute(
        "href",
        "/runs/RUN-000001/",
      );
    });

    it("says whether a reproduction's results match, and where they differ", async () => {
      const builds = { original: "sha256:b", reproduced: "sha256:b" };
      const reproduction: Schemas["RunOut"]["reproduction"] = {
        matches: true,
        differences: [],
        more_differences: 0,
        environment_build: builds,
      };
      onRun(
        () =>
          run({
            ...done,
            reproduces: { ...original, id: AGAIN, hrid: "RUN-000000" },
            reproduction,
          }),
        () => [attempt({ state: "succeeded" })],
      );
      withLog(
        () => "",
        () => true,
      );
      await renderApp("/runs/RUN-000001/");
      expect(await screen.findByText("Reproduced")).toBeVisible();
      expect(screen.getByText("Reproduced").closest(".alert")).toHaveTextContent(
        "Its results are the same as RUN-000000’s, in the same environment build.",
      );
    });

    it("lists a reproduction's differences", async () => {
      onRun(
        () =>
          run({
            ...done,
            reproduces: { ...original, id: AGAIN, hrid: "RUN-000000" },
            reproduction: {
              matches: false,
              differences: [
                { pointer: "#/metrics/score", original: 0.91, reproduced: 0.93 },
                { pointer: "#/creates/0/values/name", original: "ABL1", reproduced: null },
              ],
              more_differences: 3,
              environment_build: { original: "sha256:b", reproduced: "sha256:c" },
            },
          }),
        () => [attempt({ state: "succeeded" })],
      );
      withLog(
        () => "",
        () => true,
      );
      await renderApp("/runs/RUN-000001/");
      expect(await screen.findByText("Didn’t reproduce")).toBeVisible();
      expect(
        screen.getByText(
          "Its results differ from RUN-000000’s in 5 places, and it ran in another environment build.",
        ),
      ).toBeVisible();
      expect(screen.getByText("#/metrics/score").closest("li")).toHaveTextContent(
        "#/metrics/score: 0.91 → 0.93",
      );
      expect(screen.getByText("#/creates/0/values/name").closest("li")).toHaveTextContent(
        "ABL1 → nothing",
      );
      expect(screen.getByText("and 3 more places")).toBeVisible();
    });

    it("says which run's results a run reused, and runs it again when asked", async () => {
      onRun(
        () =>
          run({
            ...done,
            attempts: 0,
            reason: "Weights",
            reused: true,
            reused_from: { ...original, id: AGAIN, hrid: "RUN-000000" },
            can: { cancel: false, reproduce: true, run_again: true },
          }),
        () => [],
      );
      const queued = () => run({ id: AGAIN, hrid: "RUN-000002", status: "queued" });
      answer(`POST /api/v1/actions/${ACTION.name}/runs`, queued);
      onAgain(queued);
      const { user } = await renderApp("/runs/RUN-000001/");
      expect(await screen.findByText(/^Reused the results of/)).toHaveTextContent(
        "Reused the results of RUN-000000, an identical run",
      );
      await user.click(screen.getByRole("button", { name: "Run again" }));
      expect(await screen.findByText("Started RUN-000002.")).toBeVisible();
      expect(calls("POST").at(-1)?.body).toEqual({
        project: KINASE.id,
        inputs: { protein: ["00000000-0000-7000-8000-00000000e001"] },
        params: { ph: 7.4 },
        reason: "Weights",
        reuse: false,
        add_to_project: false,
      });
    });

    it("shows the seed its command was given", async () => {
      onRun(
        () => run(done),
        () => [attempt({ state: "succeeded" })],
      );
      withLog(
        () => "",
        () => true,
      );
      await renderApp("/runs/RUN-000001/");
      expect(await screen.findByText("Seed")).toBeVisible();
      expect(screen.getByText("1234567")).toBeVisible();
    });
  });

  describe("that an AI agent asked for, on a tier that needs approval", () => {
    const APPROVAL = "/api/v1/run-approvals/00000000-0000-7000-8000-0000000000a1";
    const waiting = {
      status: "awaiting_approval" as const,
      tier: "container" as const,
      via: "mcp",
      client: "Claude",
      started_at: null,
    };
    const unstarted = () => [
      attempt({
        state: "awaiting_approval",
        worker: null,
        started_at: null,
        progress: null,
        log_size: 0,
      }),
    ];

    it("waits for the person's approval, and starts when they give it", async () => {
      let current = run({ ...waiting, approval: approval() });
      onRun(() => current, unstarted);
      withLog(() => "");
      answer(`POST ${APPROVAL}/approve`, () => {
        const approved = approval({
          state: "approved",
          decided_by: current.requested_by,
          decided_at: "2026-03-14T09:05:00Z",
          can: { decide: false },
        });
        current = run({ ...waiting, status: "queued", approval: approved });
        return approved;
      });
      const { user } = await renderApp("/runs/RUN-000001/");
      expect(screen.getByText("Awaiting approval")).toBeInTheDocument();
      const request = screen.getByRole("region", { name: "Waiting for your approval" });
      expect(request).toHaveTextContent(
        /Claude asked to start this run for Rosalind Franklin .* on the container tier, which an AI agent can’t start by itself, so it waits for you\./,
      );
      expect(request).toHaveTextContent(/If nobody decides, the request expires/);
      expect(screen.getByText(/by an AI assistant \(MCP\), through Claude/)).toBeInTheDocument();

      await user.click(within(request).getByRole("button", { name: "Approve" }));
      expect(await screen.findByText("Approved RUN-000001.")).toBeVisible();
      await waitFor(() => {
        expect(screen.queryByRole("region", { name: /approval/ })).not.toBeInTheDocument();
      });
      expect(screen.getByText("Queued")).toBeInTheDocument();
      expect(screen.getByText(/^Approved by Rosalind Franklin/)).toBeInTheDocument();
    });

    it("declines, with a note for the record", async () => {
      let current = run({ ...waiting, approval: approval() });
      onRun(() => current, unstarted);
      withLog(() => "");
      answer(`POST ${APPROVAL}/decline`, () => {
        const declined = approval({
          state: "declined",
          decided_by: current.requested_by,
          decided_at: "2026-03-14T09:05:00Z",
          note: "Not on the big machines",
          can: { decide: false },
        });
        current = run({ ...waiting, status: "declined", approval: declined });
        return declined;
      });
      const { user } = await renderApp("/runs/RUN-000001/");
      await user.click(screen.getByRole("button", { name: "Decline" }));
      const dialog = screen.getByRole("alertdialog", { name: "Decline RUN-000001?" });
      expect(dialog).toHaveTextContent(/It ends without running/);
      await user.type(
        within(dialog).getByRole("textbox", { name: /Note/ }),
        "Not on the big machines",
      );
      await user.click(within(dialog).getByRole("button", { name: "Decline" }));
      expect(await screen.findByText("Declined RUN-000001.")).toBeVisible();
      expect(calls("POST").at(-1)?.body).toEqual({ note: "Not on the big machines" });
      expect(await screen.findByText(/^Declined by Rosalind Franklin/)).toHaveTextContent(
        /: Not on the big machines$/,
      );
      expect(screen.getAllByText("Declined").length).toBeGreaterThan(0);
    });

    it("decides a batch together", async () => {
      const runs = Array.from({ length: 8 }, (_, i) => ({
        id: i ? `00000000-0000-7000-8000-00000000000${String(i)}` : RUN_ID,
        hrid: `RUN-00000${String(i + 1)}`,
      }));
      onRun(() => run({ ...waiting, approval: approval({ runs }) }), unstarted);
      withLog(() => "");
      await renderApp("/runs/RUN-000001/");
      const request = screen.getByRole("region", { name: "Waiting for your approval" });
      expect(request).toHaveTextContent(
        /start these 8 runs .* They run on the container tier, which an AI agent can’t start by itself, so they wait for you\./,
      );
      expect(request).toHaveTextContent(
        /one of 8 runs asked for together, and they’re decided together: RUN-000002, RUN-000003, RUN-000004, RUN-000005, RUN-000006 and 2 more\./,
      );
      expect(within(request).getByRole("link", { name: "RUN-000002" })).toHaveAttribute(
        "href",
        "/runs/RUN-000002/",
      );
      expect(within(request).getByRole("button", { name: "Approve 8 runs" })).toBeVisible();
    });

    it("says who decides when it isn't the reader", async () => {
      onRun(
        () =>
          run({
            ...waiting,
            requested_by: { id: "someone", name: "Ada Lovelace" },
            approval: approval({
              requested_by: { id: "someone", name: "Ada Lovelace" },
              can: { decide: false },
            }),
          }),
        unstarted,
      );
      withLog(() => "");
      await renderApp("/runs/RUN-000001/");
      const request = screen.getByRole("region", { name: "Waiting for approval" });
      expect(request).toHaveTextContent(/so it waits for Ada Lovelace to approve it in Iki\./);
      expect(within(request).queryByRole("button")).not.toBeInTheDocument();
    });

    it("says when nobody approved it in time", async () => {
      onRun(
        () => run({ ...waiting, status: "declined", approval: approval({ state: "expired" }) }),
        unstarted,
      );
      withLog(() => "");
      await renderApp("/runs/RUN-000001/");
      expect(screen.getByText("Expired: nobody approved it in time")).toBeInTheDocument();
      expect(screen.queryByRole("region", { name: /approval/ })).not.toBeInTheDocument();
    });
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
