/**
 * Compute → Slurm (ADR 0133): the Slurm clusters container-tier runs go to, each with its
 * templates as the deployment's compute.yaml says them, its workers online, and the Slurm
 * accounts projects' jobs are charged to there. Templates change only in compute.yaml,
 * which is reviewed like code; Compute admins set, change and clear projects' accounts
 * here, and every change is audited.
 */

import { unwrap } from "@ikisci/sdk";
import { useInfiniteQuery, useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useDeferredValue, useState, type SubmitEvent } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { iki, meQuery, problemOf, useApiMutation } from "../api";
import {
  capacityText,
  COMPUTE,
  quotaScopesQuery,
  SLURM_ACCOUNT,
  slurmAccountsQuery,
  slurmClustersQuery,
  walltimeText,
  type SlurmAccount,
  type SlurmCluster,
  type SlurmTemplate,
} from "../compute";
import { formatSize } from "../entities";
import { required, useForm } from "../forms";
import { usePageTitle } from "../layout";
import {
  Button,
  ConfirmDialog,
  Loading,
  ProblemAlert,
  RadioField,
  RelativeTime,
  TextField,
  useMessages,
} from "../ui";
import { ComputeHeader } from "./compute-header";
import { NotFound } from "./errors";

export function ComputeSlurmPage() {
  usePageTitle("Slurm");
  const me = useSuspenseQuery(meQuery).data;
  const clusters = useQuery({ ...slurmClustersQuery, enabled: me.can.manage_compute });
  if (!me.can.manage_compute) return <NotFound />;
  return (
    <>
      <ComputeHeader />
      <p className="muted">
        Container-tier runs can run on a Slurm cluster: a worker on its login node submits each one
        with <code>sbatch</code>, sized to what its action needs, and reports what Slurm says of it.
        The clusters and their templates come from this deployment’s <code>compute.yaml</code>,
        which is reviewed like code: change them there.
      </p>
      {clusters.isPending ? (
        <Loading label="Loading Slurm clusters…" />
      ) : clusters.isError ? (
        <ProblemAlert problem={problemOf(clusters.error)} />
      ) : clusters.data.length === 0 ? (
        <section className="card">
          <p className="muted">
            This deployment has no Slurm clusters. To add one, give a template in its{" "}
            <code>compute.yaml</code> the <code>slurm</code> provisioner and the cluster’s name.
            Then make a Container token for the cluster under{" "}
            <Link to="/admin/compute/workers/">Workers</Link>, and register a worker with it on the
            cluster’s login node.
          </p>
        </section>
      ) : (
        <div className="stack">
          {clusters.data.map((cluster) => (
            <ClusterCard key={cluster.name} cluster={cluster} />
          ))}
        </div>
      )}
    </>
  );
}

function plural(n: number, one: string, many: string): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

function ClusterCard({ cluster }: { cluster: SlurmCluster }) {
  const titleId = `slurm-${cluster.name}`;
  return (
    <section className="card card-wide" aria-labelledby={titleId}>
      <h2 className="section-title" id={titleId}>
        {cluster.name}
      </h2>
      {cluster.workers_online ? (
        <p className="muted">
          {plural(cluster.workers_online, "worker", "workers")} online on its login nodes.
        </p>
      ) : (
        <div className="alert alert-warning" role="status">
          <p>
            No worker on its login nodes is online, so its runs wait. Make a Container token for{" "}
            {cluster.name} under <Link to="/admin/compute/workers/">Workers</Link>, and register a
            worker with it on a login node.
          </p>
        </div>
      )}
      <Templates cluster={cluster} />
      <Accounts cluster={cluster} />
    </section>
  );
}

// --- Templates ------------------------------------------------------------------------

/** A rate per hour, to the fraction of a cent rates are often set in. */
export function rateText(amount: number, currency: string): string {
  return new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency,
    currencyDisplay: "narrowSymbol",
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(amount);
}

/** "£0.04 per CPU, £1.20 per GPU, £0.005 per GiB of memory, an hour", or null if unpriced. */
export function priceText(price: SlurmTemplate["price"]): string | null {
  if (!price) return null;
  const currency = price["currency"];
  if (typeof currency !== "string" || !currency) return null;
  const parts: string[] = [];
  for (const [key, what] of [
    ["per_cpu_hour", "CPU"],
    ["per_gpu_hour", "GPU"],
    ["per_memory_gib_hour", "GiB of memory"],
  ] as const) {
    const amount = price[key];
    if (typeof amount === "number") parts.push(`${rateText(amount, currency)} per ${what}`);
  }
  return parts.length ? `${parts.join(", ")}, an hour` : null;
}

/** "3 of 10", or "3, no limit". */
export function jobsText(jobs: number, most: number | null): string {
  return most === null ? `${String(jobs)}, no limit` : `${String(jobs)} of ${String(most)}`;
}

function Templates({ cluster }: { cluster: SlurmCluster }) {
  return (
    <>
      <h3 className="section-title">Templates</h3>
      <p className="muted">
        Each run goes to the first of its environment’s templates it fits. The most one job may ask
        for is what its action may need at most; each asks Slurm for just what it needs.
      </p>
      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Template</th>
              <th scope="col">Partition</th>
              <th scope="col">Default account</th>
              <th scope="col">Time limit</th>
              <th scope="col">Most one job may ask for</th>
              <th scope="col">Jobs now</th>
              <th scope="col">Price</th>
              <th scope="col">Internet</th>
            </tr>
          </thead>
          <tbody>
            {cluster.templates.map((template) => (
              <TemplateRow key={template.name} template={template} />
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function TemplateRow({ template }: { template: SlurmTemplate }) {
  const scratch = Number(template.capacity["scratch_disk"] ?? 0);
  const price = priceText(template.price);
  return (
    <tr>
      <th scope="row">
        <code>{template.name}</code>
      </th>
      <td>
        <code>{template.partition}</code>
        {template.qos ? (
          <span className="cell-meta">
            QOS <code>{template.qos}</code>
          </span>
        ) : null}
        {template.constraint ? (
          <span className="cell-meta">
            Nodes with <code>{template.constraint}</code>
          </span>
        ) : null}
      </td>
      <td>
        {template.account ? (
          <code>{template.account}</code>
        ) : (
          <>
            None
            <span className="cell-meta">Only projects with their own account run here.</span>
          </>
        )}
      </td>
      <td>{template.max_walltime ? walltimeText(template.max_walltime) : "The partition’s"}</td>
      <td>
        {capacityText(template.capacity)}
        {template.gpu_type ? (
          <span className="cell-meta">
            GPU type <code>{template.gpu_type}</code>
          </span>
        ) : null}
        {scratch ? <span className="cell-meta">{formatSize(scratch)} of scratch disk</span> : null}
      </td>
      <td>{jobsText(template.jobs, template.max_concurrent)}</td>
      <td>{price ?? "No price"}</td>
      <td>
        {template.egress_proxy_url ? (
          <>
            Through the egress proxy
            <span className="cell-meta">
              <code>{template.egress_proxy_url}</code>
            </span>
          </>
        ) : (
          <>
            None
            <span className="cell-meta">Actions that reach the internet don’t run here.</span>
          </>
        )}
      </td>
    </tr>
  );
}

// --- Projects' accounts ---------------------------------------------------------------

function Accounts({ cluster }: { cluster: SlurmCluster }) {
  const found = useInfiniteQuery(slurmAccountsQuery(cluster.name));
  const [setting, setSetting] = useState<SlurmAccount | "new" | undefined>();
  const [clearing, setClearing] = useState<SlurmAccount | undefined>();
  const titleId = `slurm-${cluster.name}-accounts`;
  return (
    <div role="group" aria-labelledby={titleId}>
      {/* By the heading, not below the list, so it stays put when the list arrives. */}
      <div className="section-header">
        <h3 className="section-title" id={titleId}>
          Projects’ accounts
        </h3>
        <Button
          onClick={() => {
            setSetting("new");
          }}
        >
          Give a project an account
        </Button>
      </div>
      <p className="muted">
        A project’s jobs here are charged to its own Slurm account if it has one, or else to their
        template’s default. A change applies from each run’s next attempt; jobs already submitted
        keep theirs.
      </p>
      {found.isPending ? (
        <Loading label="Loading projects’ accounts…" />
      ) : found.isError ? (
        <ProblemAlert problem={problemOf(found.error)} />
      ) : found.data.pages[0]?.items.length ? (
        <>
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Project</th>
                  <th scope="col">Account</th>
                  <th scope="col">Set</th>
                  <th scope="col">
                    <span className="visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {found.data.pages.flatMap((p) =>
                  p.items.map((given) => (
                    <tr key={given.project.id}>
                      <th scope="row">
                        {given.project.name}
                        <span className="cell-meta">{given.project.team.name} team</span>
                      </th>
                      <td>
                        <code>{given.account}</code>
                      </td>
                      <td>
                        {given.set_by ? `By ${given.set_by.name} ` : null}
                        <RelativeTime value={given.set_at} />
                      </td>
                      <td className="row-actions">
                        <Button
                          variant="quiet"
                          label={`Change ${given.project.name}’s account`}
                          onClick={() => {
                            setSetting(given);
                          }}
                        >
                          Change
                        </Button>
                        <Button
                          variant="quiet"
                          label={`Clear ${given.project.name}’s account`}
                          onClick={() => {
                            setClearing(given);
                          }}
                        >
                          Clear
                        </Button>
                      </td>
                    </tr>
                  )),
                )}
              </tbody>
            </table>
          </div>
          {found.hasNextPage ? (
            <Button
              variant="quiet"
              busy={found.isFetchingNextPage}
              busyText="Loading…"
              onClick={() => void found.fetchNextPage()}
            >
              Show more
            </Button>
          ) : null}
        </>
      ) : (
        <p className="muted">
          None yet: every project’s jobs here are charged to their template’s default account.
        </p>
      )}
      {setting ? (
        <AccountDialog
          cluster={cluster.name}
          given={setting === "new" ? undefined : setting}
          onClose={() => {
            setSetting(undefined);
          }}
        />
      ) : null}
      {clearing ? (
        <ClearAccount
          given={clearing}
          onClose={() => {
            setClearing(undefined);
          }}
        />
      ) : null}
    </div>
  );
}

/** Why an account as typed won't do, or false. */
export function accountError(text: string): string | false {
  return (
    required(text, "Enter the Slurm account its jobs are charged to.") ||
    (!SLURM_ACCOUNT.test(text.trim()) &&
      "Use up to 64 letters, digits, dots, dashes and underscores, starting with a letter or digit.")
  );
}

function AccountDialog({
  cluster,
  given,
  onClose,
}: {
  cluster: string;
  given: SlurmAccount | undefined;
  onClose: () => void;
}) {
  const { say } = useMessages();
  const [search, setSearch] = useState("");
  const [picked, setPicked] = useState("");
  const [account, setAccount] = useState(given?.account ?? "");
  const term = useDeferredValue(search);
  const projects = useQuery({
    ...quotaScopesQuery("project", term),
    enabled: given === undefined,
  });

  const save = useApiMutation(
    async (projectId: string) =>
      unwrap(
        await iki.PUT("/api/v1/compute/slurm/{cluster}/accounts/{project_id}", {
          params: { path: { cluster, project_id: projectId } },
          body: { account: account.trim() },
        }),
      ),
    [COMPUTE],
  );
  const form = useForm(save.error, save.isPending);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const ok = form.validate({
      project: given === undefined && picked === "" && "Choose the project.",
      account: accountError(account),
    });
    if (!ok) return;
    save.mutate(given?.project.id ?? picked, {
      onSuccess: (saved) => {
        say(
          `${saved.project.name}’s jobs on ${cluster} are charged to ${saved.account} from their next attempt.`,
        );
        onClose();
      },
    });
  }

  const title = given
    ? `Change ${given.project.name}’s account on ${cluster}`
    : `Give a project an account on ${cluster}`;
  return (
    <ModalOverlay
      className="dialog-overlay"
      isOpen
      isDismissable={!save.isPending}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <Modal className="dialog">
        <Dialog className="dialog-body">
          <Heading slot="title" className="section-title">
            {title}
          </Heading>
          {form.alert}
          <form {...form.props} onSubmit={submit}>
            {given ? null : (
              <>
                <TextField
                  label="Find a project"
                  value={search}
                  onChange={setSearch}
                  maxLength={100}
                  help="Any of them, whether you’re a member or not. Archived ones aren’t listed."
                  optional
                />
                {projects.isPending ? (
                  <Loading label="Loading projects…" />
                ) : projects.isError ? (
                  <ProblemAlert problem={problemOf(projects.error)} />
                ) : projects.data.length ? (
                  <RadioField
                    label="Project"
                    value={picked}
                    onChange={setPicked}
                    error={form.errors["project"]}
                    help="One that has an account here already gets this one instead."
                    options={projects.data.map((p) => ({
                      value: p.id,
                      label: p.name,
                      ...(p.team ? { description: `${p.team} team` } : {}),
                    }))}
                  />
                ) : (
                  <p className="muted" role="status">
                    No projects{term.trim() ? ` with “${term.trim()}” in the name` : ""}.
                  </p>
                )}
              </>
            )}
            <TextField
              label="Slurm account"
              value={account}
              onChange={setAccount}
              error={form.errors["account"]}
              maxLength={64}
              help="As sacctmgr names it. The user the cluster’s worker runs as must be able to charge jobs to it, or Slurm refuses them."
            />
            <div className="actions">
              <Button type="submit" variant="primary" busy={save.isPending} busyText="Saving…">
                {given ? "Save" : "Set account"}
              </Button>
              <Button variant="quiet" onClick={onClose} disabled={save.isPending}>
                Cancel
              </Button>
            </div>
          </form>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}

function ClearAccount({ given, onClose }: { given: SlurmAccount; onClose: () => void }) {
  const { say } = useMessages();
  const name = given.project.name;
  const clear = useApiMutation(async () => {
    unwrap(
      await iki.DELETE("/api/v1/compute/slurm/{cluster}/accounts/{project_id}", {
        params: { path: { cluster: given.cluster, project_id: given.project.id } },
      }),
    );
  }, [COMPUTE]);
  return (
    <ConfirmDialog
      isOpen
      title={`Clear ${name}’s account on ${given.cluster}?`}
      confirmLabel="Clear account"
      busy={clear.isPending}
      problem={clear.error ? problemOf(clear.error) : undefined}
      onCancel={onClose}
      onConfirm={() => {
        clear.mutate(undefined, {
          onSuccess: () => {
            say(`Cleared ${name}’s account on ${given.cluster}.`);
            onClose();
          },
        });
      }}
    >
      <p>
        From their next attempt, its jobs there are charged to their template’s default account
        instead of <code>{given.account}</code>. A template without one doesn’t run them.
      </p>
    </ConfirmDialog>
  );
}
