/**
 * Compute → Overview: what compute is doing now. Each tier and environment's runs waiting
 * and running, every tier's workers, and the machines Iki started. It reads the numbers
 * again every 15 seconds; Iki exports the same ones as metrics, for dashboards and alerts.
 */

import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { meQuery, problemOf } from "../api";
import { overviewQuery, TIER_NAME, TIER_WORKERS, type ComputeOverview } from "../compute";
import { machineTime } from "../costs";
import { usePageTitle } from "../layout";
import { Loading, PageHeader, ProblemAlert, RelativeTime } from "../ui";
import { NotFound } from "./errors";

/** Compute's pages, for Compute admins. */
export function ComputeTabs() {
  return (
    <nav className="tabs" aria-label="Compute">
      <Link to="/compute/" activeOptions={{ exact: true }} activeProps={{ "aria-current": "page" }}>
        Overview
      </Link>
      <Link to="/compute/volumes/" activeProps={{ "aria-current": "page" }}>
        Volumes
      </Link>
    </nav>
  );
}

export function ComputeOverviewPage() {
  usePageTitle("Compute");
  const me = useSuspenseQuery(meQuery).data;
  const overview = useQuery({ ...overviewQuery, enabled: me.can.manage_compute });
  if (!me.can.manage_compute) return <NotFound />;
  return (
    <>
      <PageHeader title="Overview" eyebrow="Compute" />
      <ComputeTabs />
      {overview.isPending ? (
        <Loading label="Loading what compute is doing…" />
      ) : overview.isError ? (
        <ProblemAlert problem={problemOf(overview.error)} />
      ) : (
        <Overview overview={overview.data} />
      )}
    </>
  );
}

function Overview({ overview }: { overview: ComputeOverview }) {
  return (
    <div className="stack">
      <Queues queues={overview.queues} />
      <Workers workers={overview.workers} />
      <Machines machines={overview.machines} />
      <p className="muted">
        As of <RelativeTime value={overview.as_of} />. It updates by itself while it’s open.
      </p>
    </div>
  );
}

function Queues({ queues }: { queues: ComputeOverview["queues"] }) {
  return (
    <section className="card" aria-labelledby="queues">
      <h2 id="queues">Runs</h2>
      {queues.length === 0 ? (
        <p className="muted">Nothing is waiting or running.</p>
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Environment</th>
                <th scope="col">Tier</th>
                <th scope="col">Awaiting approval</th>
                <th scope="col">Queued</th>
                <th scope="col">Running</th>
                <th scope="col">Longest wait</th>
              </tr>
            </thead>
            <tbody>
              {queues.map((q) => (
                <tr key={`${q.tier}:${q.environment}`}>
                  <th scope="row">
                    <code>{q.environment}</code>
                  </th>
                  <td>{TIER_NAME[q.tier] ?? q.tier}</td>
                  <td>{q.awaiting_approval}</td>
                  <td>{q.queued}</td>
                  <td>{q.running}</td>
                  <td>{q.queued ? machineTime(q.waiting_seconds) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Workers({ workers }: { workers: ComputeOverview["workers"] }) {
  return (
    <section className="card" aria-labelledby="workers">
      <h2 id="workers">Workers</h2>
      <p className="muted">
        Busy ones are running a job. Offline ones haven’t been heard from for two minutes.
      </p>
      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Tier</th>
              <th scope="col">Busy</th>
              <th scope="col">Idle</th>
              <th scope="col">Offline</th>
            </tr>
          </thead>
          <tbody>
            {workers.map((w) => (
              <tr key={w.tier}>
                <th scope="row">
                  {TIER_NAME[w.tier] ?? w.tier}
                  <span className="cell-meta">{TIER_WORKERS[w.tier]}</span>
                </th>
                <td>{w.busy}</td>
                <td>{w.idle}</td>
                <td>{w.offline}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Machines({ machines }: { machines: ComputeOverview["machines"] }) {
  return (
    <section className="card" aria-labelledby="machines">
      <h2 id="machines">Machines Iki started</h2>
      {machines.length === 0 ? (
        <p className="muted">
          None now. Iki starts pods and virtual machines on its compute templates as runs need them.
        </p>
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Template</th>
                <th scope="col">Starting</th>
                <th scope="col">Running</th>
              </tr>
            </thead>
            <tbody>
              {machines.map((m) => (
                <tr key={`${m.template}:${m.provisioner}`}>
                  <th scope="row">
                    <code>{m.template}</code>
                    <span className="cell-meta">{PROVISIONER[m.provisioner] ?? m.provisioner}</span>
                  </th>
                  <td>{m.starting}</td>
                  <td>{m.running}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

const PROVISIONER: Record<string, string> = {
  k8s_job: "Kubernetes Jobs, one per run",
  k8s_deployment: "Kubernetes pods kept running",
  aws_ec2: "EC2 instances",
};
