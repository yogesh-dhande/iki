/**
 * Compute → Workers (ADR 0043, 0112): every registered worker, what it can run and whether
 * it's online, and the tokens workers register with. Compute admins revoke a worker (its
 * jobs are retried elsewhere), change a self-hosted one's labels and teams, make a token
 * (shown once), and revoke one, with its workers if it leaked.
 */

import { unwrap } from "@ikisci/sdk";
import { useInfiniteQuery, useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useDeferredValue, useState, type SubmitEvent } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { iki, meQuery, problemOf, useApiMutation } from "../api";
import {
  COMPUTE,
  quotaScopesQuery,
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
  CheckboxesField,
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
  { value: "self_hosted", label: "Self-hosted" },
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
  const [changing, setChanging] = useState<Worker | undefined>();

  return (
    <section className="card card-wide" aria-labelledby="workers">
      <h2 className="section-title" id="workers">
        Workers
      </h2>
      <p className="muted">
        Each runs the jobs of its tier it has the code and room for. A self-hosted one runs only its
        teams’ runs, in the projects it’s for. Revoking one stops its token at once; the jobs it
        holds are retried on other workers if their action allows.
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
                  <th scope="col">For</th>
                  <th scope="col">Running</th>
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
                    <WorkerRow
                      key={worker.id}
                      worker={worker}
                      onRevoke={setRevoking}
                      onChange={setChanging}
                    />
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
      {changing ? (
        <ChangeWorker
          worker={changing}
          onDone={() => {
            setChanging(undefined);
          }}
        />
      ) : null}
    </section>
  );
}

function WorkerRow({
  worker,
  onRevoke,
  onChange,
}: {
  worker: Worker;
  onRevoke: (w: Worker) => void;
  onChange: (w: Worker) => void;
}) {
  const status = STATUS[worker.status];
  const selfHosted = worker.tier === "self_hosted";
  return (
    <tr>
      <th scope="row">
        {worker.name}
        <span className="cell-meta">
          iki-worker {worker.version} · token …{worker.hint}
        </span>
      </th>
      <td>{TIER_NAME[worker.tier] ?? worker.tier}</td>
      <td>
        <span className={`badge ${status.tone}`}>{status.label}</span>
      </td>
      <td>
        {selfHosted ? (
          <>
            {scopeText(worker.teams, worker.projects)}
            {worker.labels.length ? <Labels labels={worker.labels} /> : null}
          </>
        ) : (
          "Any project"
        )}
      </td>
      <td>
        {worker.running.length
          ? worker.running.map((run, index) => (
              <span key={run.id}>
                {index ? ", " : null}
                <Link to="/runs/$ref/" params={{ ref: run.hrid }}>
                  {run.hrid}
                </Link>
                <span className="cell-meta">
                  {run.action} in {run.project.name}
                </span>
              </span>
            ))
          : worker.jobs
            ? `${String(worker.jobs)} ${worker.jobs === 1 ? "job" : "jobs"}`
            : "Nothing"}
      </td>
      <td>{capacityText(worker.capacity)}</td>
      <td>
        <RelativeTime value={worker.last_seen_at} />
      </td>
      <td>
        {worker.status === "revoked" ? null : (
          <div className="row-actions">
            {selfHosted ? (
              <Button
                variant="quiet"
                label={`Change ${worker.name}`}
                onClick={() => {
                  onChange(worker);
                }}
              >
                Change
              </Button>
            ) : null}
            <Button
              variant="quiet"
              label={`Revoke ${worker.name}`}
              onClick={() => {
                onRevoke(worker);
              }}
            >
              Revoke
            </Button>
          </div>
        )}
      </td>
    </tr>
  );
}

interface Named {
  id: string;
  name: string;
}

interface ScopeProject extends Named {
  /** Its team's ID. */
  team_id: string;
  /** Its team's name. */
  team: string;
}

/** "Structural Biology; Chemistry: Assays, Screens": its teams, each narrowed or whole. */
export function scopeText(
  teams: readonly Named[],
  projects: readonly { name: string; team: Named }[],
): string {
  return teams
    .map((team) => {
      const listed = projects.filter((p) => p.team.id === team.id).map((p) => p.name);
      return listed.length ? `${team.name}: ${listed.join(", ")}` : team.name;
    })
    .join("; ");
}

function Labels({ labels }: { labels: readonly string[] }) {
  return (
    <span className="cell-meta">
      {labels.map((label, index) => (
        <span key={label}>
          {index ? " " : null}
          <code>{label}</code>
        </span>
      ))}
    </span>
  );
}

/** "chromeleon, lab-3" (or spaces) as labels. The server checks and sorts them. */
export function parseLabels(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((label) => label.trim().toLowerCase())
    .filter(Boolean);
}

const LABELS_HELP =
  "What the machine has, such as chromeleon, gpu or lab-3, separated by commas: lowercase letters, digits, dots, dashes and underscores.";

/**
 * The teams a self-hosted worker runs runs for, and perhaps only some of their projects:
 * found by name, any of them, as a quota's are.
 */
function ScopeFields({
  teams,
  projects,
  onTeams,
  onProjects,
  errors,
}: {
  teams: readonly Named[];
  projects: readonly ScopeProject[];
  onTeams: (teams: Named[]) => void;
  onProjects: (projects: ScopeProject[]) => void;
  errors: Record<string, string | undefined>;
}) {
  const [teamSearch, setTeamSearch] = useState("");
  const [projectSearch, setProjectSearch] = useState("");
  const teamTerm = useDeferredValue(teamSearch);
  const projectTerm = useDeferredValue(projectSearch);
  const teamIds = teams.map((t) => t.id);
  const foundTeams = useQuery(quotaScopesQuery("team", teamTerm));
  const foundProjects = useQuery({
    ...quotaScopesQuery("project", projectTerm, teamIds),
    enabled: teams.length > 0,
  });
  // What's chosen stays listed, whatever the search finds.
  const teamOptions = [...teams, ...(foundTeams.data ?? []).filter((t) => !teamIds.includes(t.id))];
  const projectOptions: ScopeProject[] = [
    ...projects,
    ...(foundProjects.data ?? [])
      .filter((p) => !projects.some((q) => q.id === p.id))
      .map((p) => ({ id: p.id, name: p.name, team: p.team ?? "", team_id: p.team_id ?? "" })),
  ];
  return (
    <>
      <TextField
        label="Find a team"
        value={teamSearch}
        onChange={setTeamSearch}
        maxLength={100}
        optional
        help="Any team, whether you’re a member or not. Archived ones aren’t listed."
      />
      {foundTeams.isError ? <ProblemAlert problem={problemOf(foundTeams.error)} /> : null}
      {teamOptions.length ? (
        <CheckboxesField
          label="Teams"
          literal={false}
          value={teamIds}
          onChange={(ids) => {
            onTeams(teamOptions.filter((t) => ids.includes(t.id)));
            onProjects(projects.filter((p) => ids.includes(p.team_id)));
          }}
          options={teamOptions.map((t) => ({ value: t.id, label: t.name }))}
          help="It runs these teams’ self-hosted runs."
          error={errors["teams"]}
        />
      ) : foundTeams.isPending ? (
        <Loading label="Loading teams…" />
      ) : (
        <p className="muted" role="status">
          No teams{teamTerm.trim() ? ` with “${teamTerm.trim()}” in the name` : ""}.
        </p>
      )}
      {teams.length ? (
        <>
          <TextField
            label="Find a project"
            value={projectSearch}
            onChange={setProjectSearch}
            maxLength={100}
            optional
            help="In the teams you chose."
          />
          {foundProjects.isError ? <ProblemAlert problem={problemOf(foundProjects.error)} /> : null}
          {projectOptions.length ? (
            <CheckboxesField
              label="Only these projects"
              literal={false}
              value={projects.map((p) => p.id)}
              onChange={(ids) => {
                onProjects(projectOptions.filter((p) => ids.includes(p.id)));
              }}
              options={projectOptions.map((p) => ({
                value: p.id,
                label: p.name,
                description: `${p.team} team`,
              }))}
              help="Leave a team’s projects unticked for all of them, new ones included. Tick some to run only those of that team."
              error={errors["projects"]}
            />
          ) : null}
        </>
      ) : null}
    </>
  );
}

function ChangeWorker({ worker, onDone }: { worker: Worker; onDone: () => void }) {
  const { say } = useMessages();
  const [teams, setTeams] = useState<Named[]>(worker.teams);
  const [projects, setProjects] = useState<ScopeProject[]>(
    worker.projects.map((p) => ({ id: p.id, name: p.name, team: p.team.name, team_id: p.team.id })),
  );
  const [labels, setLabels] = useState(worker.labels.join(", "));
  const save = useApiMutation(
    async () =>
      unwrap(
        await iki.PATCH("/api/v1/compute/workers/{worker_id}", {
          params: { path: { worker_id: worker.id } },
          body: {
            labels: parseLabels(labels),
            teams: teams.map((t) => t.id),
            projects: projects.map((p) => p.id),
          },
        }),
      ),
    [COMPUTE],
  );
  const form = useForm(save.error);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form.validate({ teams: teams.length === 0 && "Choose the teams whose runs it takes." }))
      return;
    save.mutate(undefined, {
      onSuccess: (saved) => {
        say(`Changed ${saved.name}. It takes what it’s for now from its next run.`);
        onDone();
      },
    });
  }

  return (
    <ModalOverlay
      className="dialog-overlay"
      isOpen
      isDismissable={!save.isPending}
      onOpenChange={(open) => {
        if (!open) onDone();
      }}
    >
      <Modal className="dialog">
        <Dialog className="dialog-body">
          <Heading slot="title" className="section-title">
            Change {worker.name}
          </Heading>
          {form.alert}
          <form {...form.props} onSubmit={submit}>
            <ScopeFields
              teams={teams}
              projects={projects}
              onTeams={setTeams}
              onProjects={setProjects}
              errors={form.errors}
            />
            <TextField
              label="Labels"
              value={labels}
              onChange={setLabels}
              error={form.errors["labels"]}
              help={LABELS_HELP}
              optional
            />
            <p className="muted">
              Runs it’s running in projects it’s no longer for are stopped, and retried on another
              worker if their action allows.
            </p>
            <div className="actions">
              <Button type="submit" variant="primary" busy={save.isPending} busyText="Saving…">
                Save
              </Button>
              <Button variant="quiet" onClick={onDone} disabled={save.isPending}>
                Cancel
              </Button>
            </div>
          </form>
        </Dialog>
      </Modal>
    </ModalOverlay>
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
  { value: "self_hosted", label: "Self-hosted: teams’ own machines" },
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
        workers as a secret (the chart’s <code>backendWorkers.registrationToken</code>). A
        self-hosted token is for some teams’ machines: each registers with{" "}
        <code>iki-worker register</code>. Machines Iki starts get theirs without one.
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
                    {token.tier === "self_hosted" ? (
                      <span className="cell-meta">
                        For {scopeText(token.teams, token.projects)}
                      </span>
                    ) : null}
                    {token.labels.length ? <Labels labels={token.labels} /> : null}
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
  const [teams, setTeams] = useState<Named[]>([]);
  const [projects, setProjects] = useState<ScopeProject[]>([]);
  const [labels, setLabels] = useState("");
  const selfHosted = tier === "self_hosted";
  const make = useApiMutation(
    async (body: {
      label: string;
      tier: typeof tier;
      expires_at: string | null;
      teams: string[];
      projects: string[];
      labels: string[];
    }) => unwrap(await iki.POST("/api/v1/compute/registration-tokens", { body })),
    [COMPUTE],
  );
  const form = useForm(make.error);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const ok = form.validate({
      label: required(label, "Say what it’s for."),
      teams: selfHosted && teams.length === 0 && "Choose the teams whose runs it takes.",
    });
    if (!ok) return;
    const expires_at =
      expiry === "never" ? null : new Date(Date.now() + Number(expiry) * 86_400_000).toISOString();
    make.mutate(
      {
        label: label.trim(),
        tier,
        expires_at,
        teams: selfHosted ? teams.map((t) => t.id) : [],
        projects: selfHosted ? projects.map((p) => p.id) : [],
        labels: selfHosted ? parseLabels(labels) : [],
      },
      {
        onSuccess: (token) => {
          setLabel("");
          setTeams([]);
          setProjects([]);
          setLabels("");
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
          help="Such as “GPU pool (us-east-1)” or “Lab 3 HPLC PCs”, so you can tell it apart later."
          maxLength={100}
        />
        <SelectField label="Tier" value={tier} onChange={setTier} options={TOKEN_TIERS} />
        {selfHosted ? (
          <>
            <ScopeFields
              teams={teams}
              projects={projects}
              onTeams={setTeams}
              onProjects={setProjects}
              errors={form.errors}
            />
            <TextField
              label="Labels"
              value={labels}
              onChange={setLabels}
              error={form.errors["labels"]}
              help={`${LABELS_HELP} Every machine that registers with it gets them.`}
              optional
            />
          </>
        ) : null}
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
      {token.tier === "self_hosted" ? (
        <>
          <p>On each machine, register it with:</p>
          <CopyValue
            value={`iki-worker register --url ${window.location.origin} --token ${token.token}`}
            label="register command"
          />
        </>
      ) : null}
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
