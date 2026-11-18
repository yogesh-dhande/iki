/**
 * A run's outside APIs on its page (ADR 0130): while it's parked, the job it waits on;
 * who agreed to send its data, and what they agreed to; and each call the egress proxy
 * made for it, with the model and version that answered and fingerprints of what went
 * each way.
 */

import { unwrap } from "@ikisci/sdk";
import { infiniteQueryOptions, useInfiniteQuery } from "@tanstack/react-query";

import { AgentRows, Through } from "../agent";
import { iki, problemOf } from "../api";
import { money } from "../costs";
import { callKind, type ApiCall } from "../outside-apis";
import { RUNS, type Run } from "../runs";
import { Button, Loading, ProblemAlert, RelativeTime, Time } from "../ui";
import { VIA } from "./runs";

export function apiCallsQuery(ref: string) {
  return infiniteQueryOptions({
    queryKey: [...RUNS, "api-calls", ref.toUpperCase()],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await iki.GET("/api/v1/runs/{ref}/api-calls", {
          params: {
            path: { ref },
            query: { limit: 100, ...(pageParam ? { cursor: pageParam } : {}) },
          },
        }),
      ),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
}

function labelOf(run: Run, api: string): string {
  return run.apis.find((a) => a.api === api)?.label ?? api;
}

/** While a run is parked: the outside API's job it waits on, and when it's asked next. */
export function ParkedNote({ run }: { run: Run }) {
  const park = run.park;
  if (run.status !== "parked" || !park) return null;
  return (
    <div className="alert alert-info" role="status">
      <p className="alert-title">Waiting on {labelOf(run, park.api)}</p>
      <p>
        Its job <code>{park.job}</code> started <RelativeTime value={park.parked_at} />. No machine
        is held while it waits.{" "}
        {park.last_polled_at ? (
          <>
            Asked <RelativeTime value={park.last_polled_at} />
            {park.status ? (
              <>
                , it said <code>{park.status}</code>
              </>
            ) : null}
            .{" "}
          </>
        ) : null}
        {park.next_poll_at ? (
          <>
            Iki asks again <RelativeTime value={park.next_poll_at} />, or as soon as{" "}
            {labelOf(run, park.api)} says it’s done.{" "}
          </>
        ) : null}
        If it isn’t done by <Time value={park.deadline} />, the run times out and the job is
        cancelled.
      </p>
    </div>
  );
}

function fingerprint(sha: string): string {
  return sha ? `${sha.slice(0, 12)}…` : "–";
}

function parameters(call: ApiCall): string {
  const entries = Object.entries(call.parameters);
  return entries.length
    ? entries
        .map(([name, value]) => `${name.replace(/^\//, "")} ${JSON.stringify(value)}`)
        .join(", ")
    : "";
}

function CallRow({ run, call }: { run: Run; call: ApiCall }) {
  return (
    <tr>
      <td>
        <Time value={call.started_at} />
        {run.attempts > 1 ? <span className="muted"> (attempt {call.attempt})</span> : null}
      </td>
      <td>
        {callKind(call.kind)} to {labelOf(run, call.api)}
        <br />
        <code>
          {call.method} {call.path}
        </code>
      </td>
      <td>
        {call.status ?? "–"}
        {call.error ? <span className="muted"> {call.error}</span> : null}
      </td>
      <td>
        {call.model || "–"}
        {call.version ? <span className="muted"> {call.version}</span> : null}
        {parameters(call) ? (
          <>
            <br />
            <span className="muted">{parameters(call)}</span>
          </>
        ) : null}
      </td>
      <td>
        <span title={`Sent: SHA-256 ${call.request_sha256}`}>
          <code>{fingerprint(call.request_sha256)}</code>
        </span>
        <br />
        <span title={`Answered: SHA-256 ${call.response_sha256}`}>
          <code>{fingerprint(call.response_sha256)}</code>
        </span>
      </td>
      <td>{call.cost !== null && call.currency ? money(call.cost, call.currency) : "–"}</td>
    </tr>
  );
}

/** Who agreed to send the run's data, what to, and each call made for it. */
export function RunOutsideApis({ run }: { run: Run }) {
  const calls = useInfiniteQuery({ ...apiCallsQuery(run.hrid), enabled: run.apis.length > 0 });
  if (!run.apis.length) return null;
  const items = calls.data?.pages.flatMap((p) => p.items) ?? [];
  const consent = run.consent;
  return (
    <section className="card" aria-labelledby="outside-apis-title">
      <h2 className="section-title" id="outside-apis-title">
        Outside APIs
      </h2>
      <dl className="summary">
        <dt>Sends data to</dt>
        <dd>
          {run.apis.map((a, i) => (
            <span key={a.api}>
              {i ? "; " : ""}
              {a.label} (<code>{a.host}</code>): {a.purpose}
            </span>
          ))}
        </dd>
        {consent ? (
          <>
            <dt>Agreed</dt>
            <dd>
              By {consent.given_by.name} <RelativeTime value={consent.given_at} />{" "}
              {VIA[consent.via] ?? consent.via}
              <Through agent={consent.agent} />
              <ul>
                {consent.statements.map((s) => (
                  <li key={`${s.step ?? ""}:${s.api}`}>{s.statement}</li>
                ))}
              </ul>
            </dd>
            <AgentRows agent={consent.agent} />
          </>
        ) : null}
      </dl>
      <h3 className="section-title">Calls</h3>
      {calls.isPending ? <Loading label="Loading calls…" /> : null}
      {calls.error ? <ProblemAlert problem={problemOf(calls.error)} /> : null}
      {calls.data && !items.length ? <p className="muted">None yet.</p> : null}
      {items.length ? (
        <div className="table-scroll">
          <table className="table">
            <caption className="visually-hidden">
              Each call to an outside API, as the egress proxy recorded it
            </caption>
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">What</th>
                <th scope="col">Status</th>
                <th scope="col">Model</th>
                <th scope="col">
                  <abbr title="SHA-256 of what was sent, then of what was answered">
                    Fingerprints
                  </abbr>
                </th>
                <th scope="col">Cost</th>
              </tr>
            </thead>
            <tbody>
              {items.map((call) => (
                <CallRow key={call.id} run={run} call={call} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {calls.hasNextPage ? (
        <div className="actions">
          <Button
            busy={calls.isFetchingNextPage}
            onClick={() => {
              void calls.fetchNextPage();
            }}
          >
            Show more calls
          </Button>
        </div>
      ) : null}
    </section>
  );
}
