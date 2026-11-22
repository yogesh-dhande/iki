/**
 * Compute → Overview: what compute is doing now. The runs waiting and running and the
 * workers online, each tier's workers, each environment's runs, and the machines Iki
 * started. It reads the numbers
 * again every 15 seconds; Iki exports the same ones as metrics, for dashboards and alerts.
 */

import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { AdminActions } from "../admin";
import { meQuery, problemOf } from "../api";
import {
  overviewQuery,
  TIER_NAME,
  TIER_WORKERS,
  type ComputeOverview,
  type Tier,
} from "../compute";
import { machineTime } from "../costs";
import { usePageTitle } from "../layout";
import { buttonClass, Loading, PageTitle, ProblemAlert, RelativeTime } from "../ui";
import { NotFound } from "./errors";

/** Compute's own header, on each of its pages: its name, what it is, and its pages. */
export function ComputeHeader() {
  const tab = { activeProps: { "aria-current": "page" as const } };
  return (
    <>
      <div className="admin-lede">
        <PageTitle>Compute</PageTitle>
        <p>Where runs execute, and how much each project may use</p>
      </div>
      <nav className="admin-tabs" aria-label="Compute">
        <Link to="/admin/compute/" activeOptions={{ exact: true }} {...tab}>
          Overview
        </Link>
        <Link to="/admin/compute/workers/" {...tab}>
          Workers
        </Link>
        <Link to="/admin/compute/slurm/" {...tab}>
          Slurm clusters
        </Link>
        <Link to="/admin/compute/quotas/" {...tab}>
          Quotas
        </Link>
        <Link to="/admin/compute/volumes/" {...tab}>
          Volumes
        </Link>
      </nav>
    </>
  );
}

export function ComputeOverviewPage() {
  usePageTitle("Compute");
  const me = useSuspenseQuery(meQuery).data;
  const overview = useQuery({ ...overviewQuery, enabled: me.can.manage_compute });
  if (!me.can.manage_compute) return <NotFound />;
  return (
    <>
      <AdminActions>
        <Link
          className={buttonClass("primary")}
          to="/admin/compute/workers/"
          hash="registration-tokens"
        >
          Register a worker
        </Link>
      </AdminActions>
      <ComputeHeader />
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

function sum<T>(rows: T[], count: (row: T) => number): number {
  return rows.reduce((total, row) => total + count(row), 0);
}

function Overview({ overview }: { overview: ComputeOverview }) {
  const awaiting = sum(overview.queues, (q) => q.awaiting_approval);
  return (
    <>
      <dl className="tiles">
        <div className="tile">
          <dt>Runs waiting</dt>
          <dd>
            {sum(overview.queues, (q) => q.queued)}
            {awaiting ? <span className="tile-note">and {awaiting} awaiting approval</span> : null}
          </dd>
        </div>
        <div className="tile">
          <dt>Runs running</dt>
          <dd>{sum(overview.queues, (q) => q.running)}</dd>
        </div>
        <div className="tile">
          <dt>Workers online</dt>
          <dd>{sum(overview.workers, (w) => w.busy + w.idle)}</dd>
        </div>
        <div className="tile">
          <dt>Workers offline</dt>
          <dd>{sum(overview.workers, (w) => w.offline)}</dd>
        </div>
      </dl>
      <Workers workers={overview.workers} />
      <Queues queues={overview.queues} />
      <Machines machines={overview.machines} />
      <p className="muted">
        As of <RelativeTime value={overview.as_of} />. It updates by itself while it’s open.
      </p>
    </>
  );
}

function SectionHead({ id, title, note }: { id: string; title: string; note?: string }) {
  return (
    <div className="admin-section-head">
      <h2 id={id}>{title}</h2>
      {note ? <p>{note}</p> : null}
    </div>
  );
}

function Workers({ workers }: { workers: ComputeOverview["workers"] }) {
  return (
    <section aria-labelledby="workers">
      <SectionHead
        id="workers"
        title="Workers by tier"
        note="Busy ones are running a job. Offline means not heard from for two minutes."
      />
      <div className="table-scroll">
        <table className="table table-compact">
          <thead>
            <tr>
              <th scope="col">Tier</th>
              <th scope="col" className="tier-what">
                What it is
              </th>
              <th scope="col" className="num">
                Busy
              </th>
              <th scope="col" className="num">
                Idle
              </th>
              <th scope="col" className="num">
                Offline
              </th>
              <th scope="col">
                <span className="visually-hidden">Workers</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {workers.map((w) => {
              const name = TIER_NAME[w.tier] ?? w.tier;
              return (
                <tr key={w.tier}>
                  <th scope="row">
                    {name}
                    {/* On a phone, what it is goes under its name, so the counts fit. */}
                    <span className="cell-meta tier-what-under">{TIER_WORKERS[w.tier]}</span>
                  </th>
                  <td className="muted cell-wide tier-what">{TIER_WORKERS[w.tier]}</td>
                  <td className="num">{w.busy}</td>
                  <td className="num">{w.idle}</td>
                  <td className="num">{w.offline}</td>
                  <td className="num">
                    <Link
                      to="/admin/compute/workers/"
                      search={{ tier: w.tier as Tier }}
                      aria-label={`Manage ${name} workers`}
                    >
                      Manage
                    </Link>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Queues({ queues }: { queues: ComputeOverview["queues"] }) {
  return (
    <section aria-labelledby="queues">
      <SectionHead id="queues" title="Runs by environment" />
      {queues.length === 0 ? (
        <p className="muted">Nothing is waiting or running.</p>
      ) : (
        <div className="table-scroll">
          <table className="table table-compact">
            <thead>
              <tr>
                <th scope="col">Environment</th>
                <th scope="col">Tier</th>
                <th scope="col" className="num">
                  Awaiting approval
                </th>
                <th scope="col" className="num">
                  Queued
                </th>
                <th scope="col" className="num">
                  Running
                </th>
                <th scope="col" className="num">
                  Longest wait
                </th>
              </tr>
            </thead>
            <tbody>
              {queues.map((q) => (
                <tr key={`${q.tier}:${q.environment}`}>
                  <th scope="row">
                    <code>{q.environment}</code>
                  </th>
                  <td>{TIER_NAME[q.tier] ?? q.tier}</td>
                  <td className="num">{q.awaiting_approval}</td>
                  <td className="num">{q.queued}</td>
                  <td className="num">{q.running}</td>
                  <td className="num">{q.queued ? machineTime(q.waiting_seconds) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Machines({ machines }: { machines: ComputeOverview["machines"] }) {
  return (
    <section aria-labelledby="machines">
      <SectionHead id="machines" title="Machines Iki started" />
      {machines.length === 0 ? (
        <p className="muted">
          None now. Iki starts pods and virtual machines on its compute templates as runs need them.
        </p>
      ) : (
        <div className="table-scroll">
          <table className="table table-compact">
            <thead>
              <tr>
                <th scope="col">Template</th>
                <th scope="col" className="num">
                  Starting
                </th>
                <th scope="col" className="num">
                  Running
                </th>
              </tr>
            </thead>
            <tbody>
              {machines.map((m) => (
                <tr key={`${m.template}:${m.provisioner}`}>
                  <th scope="row">
                    <code>{m.template}</code>
                    <span className="cell-meta">{PROVISIONER[m.provisioner] ?? m.provisioner}</span>
                  </th>
                  <td className="num">{m.starting}</td>
                  <td className="num">{m.running}</td>
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
  gcp_gce: "Compute Engine instances",
};
