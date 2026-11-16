/**
 * Compute → Workers (ADR 0043): every registered worker, what it can run and whether it's
 * online, and the tokens workers register with. Compute admins revoke a worker (its jobs
 * are retried elsewhere), make a token (shown once), and revoke one, with its workers if
 * it leaked.
 */

import { unwrap } from "@ikisci/sdk";
import { useInfiniteQuery, useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { useState, type SubmitEvent } from "react";

import { iki, meQuery, problemOf, useApiMutation } from "../api";
import {
  COMPUTE,
  registrationsQuery,
  TIER_NAME,
  workersQuery,
  type Registration,
  type RegistrationCreated,
  type Worker,
  type WorkerFilter,
} from "../compute";
import { formatSize } from "../entities";
import { required, useForm } from "../forms";
import { usePageTitle } from "../layout";
import {
  Button,
  CheckboxField,
  ConfirmDialog,
  CopyValue,
  Loading,
  PageHeader,
  ProblemAlert,
  RelativeTime,
  SelectField,
  TextField,
  useMessages,
} from "../ui";
import { ComputeTabs } from "./compute";
import { NotFound } from "./errors";

export function ComputeWorkersPage() {
  usePageTitle("Workers");
  const me = useSuspenseQuery(meQuery).data;
  if (!me.can.manage_compute) return <NotFound />;
  return (
    <>
      <PageHeader title="Workers" eyebrow="Compute" />
      <ComputeTabs />
      <div className="stack">
        <Workers />
        <Registrations />
      </div>
    </>
  );
}

// --- Workers --------------------------------------------------------------------------

const TIER_FILTER = [
  { value: "all", label: "Every tier" },
  { value: "client", label: "Client" },
  { value: "backend", label: "Backend" },
  { value: "container", label: "Container" },
  { value: "vm", label: "VM" },
] as const;

const STATUS = {
  online: { label: "Online", tone: "badge-good" },
  offline: { label: "Offline", tone: "badge-bad" },
  revoked: { label: "Revoked", tone: "badge-muted" },
} as const;

function Workers() {
  const [tier, setTier] = useState<WorkerFilter["tier"]>("all");
  const [revoked, setRevoked] = useState(false);
  const found = useInfiniteQuery(workersQuery({ tier, revoked }));
  const [revoking, setRevoking] = useState<Worker | undefined>();

  return (
    <section className="card card-wide" aria-labelledby="workers">
      <h2 className="section-title" id="workers">
        Workers
      </h2>
      <p className="muted">
        Each runs the jobs of its tier it has the code and room for. Revoking one stops its token at
        once; the jobs it holds are retried on other workers if their action allows.
      </p>
      <div className="toolbar">
        <SelectField label="Tier" value={tier} onChange={setTier} options={TIER_FILTER} compact />
        <CheckboxField label="Show revoked ones" checked={revoked} onChange={setRevoked} />
      </div>
      {found.isPending ? (
        <Loading label="Loading workers…" />
      ) : found.isError ? (
        <ProblemAlert problem={problemOf(found.error)} />
      ) : found.data.pages[0]?.items.length ? (
        <>
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Worker</th>
                  <th scope="col">Tier</th>
                  <th scope="col">Status</th>
                  <th scope="col">Jobs</th>
                  <th scope="col">Room for one job</th>
                  <th scope="col">Last seen</th>
                  <th scope="col">
                    <span className="visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {found.data.pages.flatMap((p) =>
                  p.items.map((worker) => (
                    <WorkerRow key={worker.id} worker={worker} onRevoke={setRevoking} />
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
          {tier === "all"
            ? "No workers have registered yet. Make a registration token below, and give it to them."
            : `No ${TIER_NAME[tier] ?? tier} workers${revoked ? "" : " that aren’t revoked"}.`}
        </p>
      )}
      {revoking ? (
        <RevokeWorker
          worker={revoking}
          onDone={() => {
            setRevoking(undefined);
          }}
        />
      ) : null}
    </section>
  );
}

function WorkerRow({ worker, onRevoke }: { worker: Worker; onRevoke: (w: Worker) => void }) {
  const status = STATUS[worker.status];
  return (
    <tr>
      <th scope="row">
        {worker.name}
        <span className="cell-meta">
          {worker.owner ? `${worker.owner.name}’s · ` : ""}iki-worker {worker.version} · token …
          {worker.hint}
        </span>
      </th>
      <td>{TIER_NAME[worker.tier] ?? worker.tier}</td>
      <td>
        <span className={`badge ${status.tone}`}>{status.label}</span>
      </td>
      <td>{worker.jobs}</td>
      <td>{capacityText(worker.capacity)}</td>
      <td>
        <RelativeTime value={worker.last_seen_at} />
      </td>
      <td>
        {worker.status === "revoked" ? null : (
          <Button
            variant="quiet"
            label={`Revoke ${worker.name}`}
            onClick={() => {
              onRevoke(worker);
            }}
          >
            Revoke
          </Button>
        )}
      </td>
    </tr>
  );
}

/** "8 CPUs, 32 GiB, 1 GPU (80 GiB)": what one job may use on it. */
export function capacityText(capacity: Record<string, unknown>): string {
  const parts: string[] = [];
  const cpu = Number(capacity["cpu"] ?? 0);
  if (cpu) parts.push(`${String(cpu)} CPU${cpu === 1 ? "" : "s"}`);
  const memory = Number(capacity["memory"] ?? 0);
  if (memory) parts.push(formatSize(memory));
  const gpus = Number(capacity["gpu_count"] ?? 0);
  if (gpus) {
    const each = Number(capacity["gpu_memory"] ?? 0);
    parts.push(
      `${String(gpus)} GPU${gpus === 1 ? "" : "s"}${each ? ` (${formatSize(each)})` : ""}`,
    );
  }
  return parts.join(", ") || "—";
}

function RevokeWorker({ worker, onDone }: { worker: Worker; onDone: () => void }) {
  const { say } = useMessages();
  const revoke = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/compute/workers/{worker_id}/revoke", {
          params: { path: { worker_id: worker.id } },
        }),
      ),
    [COMPUTE],
  );
  return (
    <ConfirmDialog
      isOpen
      title={`Revoke ${worker.name}?`}
      confirmLabel="Revoke worker"
      busy={revoke.isPending}
      problem={revoke.error ? problemOf(revoke.error) : undefined}
      onCancel={onDone}
      onConfirm={() => {
        revoke.mutate(undefined, {
          onSuccess: () => {
            say(`Revoked ${worker.name}. It can’t lease or report on jobs any more.`);
            onDone();
          },
        });
      }}
    >
      <p>
        Its token stops working at once.
        {worker.jobs
          ? ` The ${worker.jobs === 1 ? "job it holds is" : `${String(worker.jobs)} jobs it holds are`} interrupted, and retried on other workers if their action allows.`
          : " It holds no jobs now."}{" "}
        To bring it back, register it again.
      </p>
    </ConfirmDialog>
  );
}

// --- Registration tokens --------------------------------------------------------------

const TOKEN_TIERS = [
  { value: "backend", label: "Backend: workers you run" },
  { value: "container", label: "Container" },
  { value: "vm", label: "VM" },
] as const;

const EXPIRY = [
  { value: "never", label: "Never" },
  { value: "1", label: "In a day" },
  { value: "7", label: "In a week" },
  { value: "30", label: "In 30 days" },
  { value: "90", label: "In 90 days" },
] as const;

function Registrations() {
  const tokens = useQuery(registrationsQuery);
  const [made, setMade] = useState<RegistrationCreated | undefined>();
  const [revoking, setRevoking] = useState<Registration | undefined>();
  return (
    <section className="card card-wide" aria-labelledby="registration-tokens">
      <h2 className="section-title" id="registration-tokens">
        Registration tokens
      </h2>
      <p className="muted">
        A worker registers once with a token, and gets one of its own. Give a token to your backend
        workers as a secret (the chart’s <code>backendWorkers.registrationToken</code>). Machines
        Iki starts get theirs without one.
      </p>
      {made ? (
        <MadeToken
          token={made}
          onDone={() => {
            setMade(undefined);
          }}
        />
      ) : (
        <NewToken onMade={setMade} />
      )}
      {tokens.isPending ? (
        <Loading label="Loading registration tokens…" />
      ) : tokens.isError ? (
        <ProblemAlert problem={problemOf(tokens.error)} />
      ) : tokens.data.length ? (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Token</th>
                <th scope="col">Tier</th>
                <th scope="col">Workers</th>
                <th scope="col">Last used</th>
                <th scope="col">Expires</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {tokens.data.map((token) => (
                <tr key={token.id}>
                  <th scope="row">
                    {token.label}
                    <span className="cell-meta">
                      …{token.hint} · made by {token.created_by?.name ?? "an operator"}{" "}
                      <RelativeTime value={token.created_at} />
                    </span>
                  </th>
                  <td>{TIER_NAME[token.tier] ?? token.tier}</td>
                  <td>{token.workers}</td>
                  <td>
                    {token.last_used_at ? <RelativeTime value={token.last_used_at} /> : "Never"}
                  </td>
                  <td>
                    {token.revoked_at ? (
                      <span className="badge badge-muted">Revoked</span>
                    ) : token.expires_at ? (
                      <RelativeTime value={token.expires_at} />
                    ) : (
                      "Never"
                    )}
                  </td>
                  <td>
                    {token.revoked_at ? null : (
                      <Button
                        variant="quiet"
                        label={`Revoke ${token.label}`}
                        onClick={() => {
                          setRevoking(token);
                        }}
                      >
                        Revoke
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">None yet.</p>
      )}
      {revoking ? (
        <RevokeToken
          token={revoking}
          onDone={() => {
            setRevoking(undefined);
          }}
        />
      ) : null}
    </section>
  );
}

function NewToken({ onMade }: { onMade: (token: RegistrationCreated) => void }) {
  const { say } = useMessages();
  const [label, setLabel] = useState("");
  const [tier, setTier] = useState<(typeof TOKEN_TIERS)[number]["value"]>("backend");
  const [expiry, setExpiry] = useState<(typeof EXPIRY)[number]["value"]>("never");
  const make = useApiMutation(
    async (body: { label: string; tier: typeof tier; expires_at: string | null }) =>
      unwrap(await iki.POST("/api/v1/compute/registration-tokens", { body })),
    [COMPUTE],
  );
  const form = useForm(make.error);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form.validate({ label: required(label, "Say what it’s for.") })) return;
    const expires_at =
      expiry === "never" ? null : new Date(Date.now() + Number(expiry) * 86_400_000).toISOString();
    make.mutate(
      { label: label.trim(), tier, expires_at },
      {
        onSuccess: (token) => {
          setLabel("");
          onMade(token);
          say(`Made the registration token “${token.label}”. Copy it now.`);
        },
      },
    );
  }

  return (
    <>
      {form.alert}
      <form {...form.props} onSubmit={submit}>
        <TextField
          label="What it’s for"
          value={label}
          onChange={setLabel}
          error={form.errors["label"]}
          help="Such as “GPU pool (us-east-1)”, so you can tell it apart later."
          maxLength={100}
        />
        <SelectField label="Tier" value={tier} onChange={setTier} options={TOKEN_TIERS} />
        <SelectField
          label="Expires"
          value={expiry}
          onChange={setExpiry}
          options={EXPIRY}
          help="Workers that registered keep working once it expires; new ones can’t register. A pool that scales registers new workers all the time."
        />
        <Button type="submit" variant="primary" busy={make.isPending} busyText="Making…">
          Make token
        </Button>
      </form>
    </>
  );
}

/** The token, shown once, just after it's made. */
function MadeToken({ token, onDone }: { token: RegistrationCreated; onDone: () => void }) {
  return (
    <div className="alert alert-success" role="status">
      <p>
        <strong>Copy the token now.</strong> It won’t be shown again: Iki keeps only a hash of it.
      </p>
      <CopyValue value={token.token} label="registration token" />
      <div className="actions">
        <Button variant="quiet" onClick={onDone}>
          Done
        </Button>
      </div>
    </div>
  );
}

function RevokeToken({ token, onDone }: { token: Registration; onDone: () => void }) {
  const { say } = useMessages();
  const [alsoWorkers, setAlsoWorkers] = useState(false);
  const revoke = useApiMutation(
    async (workers: boolean) =>
      unwrap(
        await iki.POST("/api/v1/compute/registration-tokens/{token_id}/revoke", {
          params: { path: { token_id: token.id } },
          body: { workers },
        }),
      ),
    [COMPUTE],
  );
  return (
    <ConfirmDialog
      isOpen
      title={`Revoke “${token.label}”?`}
      confirmLabel="Revoke token"
      busy={revoke.isPending}
      problem={revoke.error ? problemOf(revoke.error) : undefined}
      onCancel={onDone}
      onConfirm={() => {
        revoke.mutate(alsoWorkers, {
          onSuccess: () => {
            say(
              alsoWorkers
                ? `Revoked “${token.label}” and the workers that registered with it.`
                : `Revoked “${token.label}”. Its workers keep working.`,
            );
            onDone();
          },
        });
      }}
    >
      <p>No new worker can register with it.</p>
      {token.workers ? (
        <CheckboxField
          label={`Also revoke the ${token.workers === 1 ? "worker" : `${String(token.workers)} workers`} that registered with it`}
          checked={alsoWorkers}
          onChange={setAlsoWorkers}
          help="If the token leaked. Their jobs are interrupted, and retried on other workers if their action allows."
        />
      ) : null}
    </ConfirmDialog>
  );
}
