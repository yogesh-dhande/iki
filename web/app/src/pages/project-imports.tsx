/**
 * Importing a bundle from another Iki deployment (ADR 0096): the Import card in a project's
 * settings uploads one, and the import's page shows what Iki found when it checked it, and
 * the plan for each entity and person, before anything changes.
 */
import { isProblem, unwrap, type Schemas } from "@ikisci/sdk";
import {
  infiniteQueryOptions,
  queryOptions,
  useInfiniteQuery,
  useQuery,
} from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useId, useRef, useState, type SubmitEvent } from "react";

import { whileWorking } from "../activity";
import { iki, problemOf, queryClient, useApiMutation, type Project } from "../api";
import { formatSize } from "../entities";
import { required, useForm } from "../forms";
import { UploadStatus } from "../file-upload";
import { usePageTitle } from "../layout";
import {
  Badge,
  Button,
  ConfirmDialog,
  Loading,
  ProblemAlert,
  RelativeTime,
  SelectField,
  TextField,
  Time,
  useMessages,
} from "../ui";
import { sendParts, sha256Of, uploadError, type Progress } from "../uploads";
import { useProject } from "./project-overview";

type Import = Schemas["ImportOut"];
type Planned = Schemas["ImportEntityOut"];
type Action = Planned["action"];

const STATES: Record<Import["state"], string> = {
  uploading: "Uploading",
  planning: "Checking…",
  planned: "Planned",
  refused: "Refused",
  applying: "Applying…",
  applied: "Applied",
  failed: "Failed",
  discarded: "Discarded",
};

const ACTIONS: Record<Action, string> = {
  new: "New",
  update: "Newer revisions",
  skip: "Here already",
  conflict: "Conflict",
};

const WHY_OUTSIDE: Record<Schemas["OutsideRef"]["why"], string> = {
  "another-project": "in another project there",
  history: "named only by earlier revisions",
  purged: "its plugin was purged there",
  run: "only a run read it: the run’s record keeps it",
  unreadable: "the exporter couldn’t read it",
};

const IMPORTS = "imports";

function importsQuery(projectId: string) {
  return queryOptions({
    queryKey: [IMPORTS, "project", projectId],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/projects/{project_id}/imports", {
          params: { path: { project_id: projectId } },
          signal,
        }),
      ).items,
    refetchInterval: (query) =>
      query.state.data?.some((i) => i.state === "planning" || i.state === "applying")
        ? 2000
        : false,
  });
}

function importQuery(id: string) {
  return queryOptions({
    queryKey: [IMPORTS, id],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/imports/{import_id}", {
          params: { path: { import_id: id } },
          signal,
        }),
      ),
    refetchInterval: (query) =>
      query.state.data?.state === "planning" ||
      query.state.data?.state === "uploading" ||
      query.state.data?.state === "applying"
        ? 2000
        : false,
  });
}

function planResponse<T>(id: string, result: { data?: T; error?: unknown; response: Response }): T {
  if (isProblem(result.error) && result.error.code === "stale_plan") {
    void queryClient.invalidateQueries({ queryKey: importQuery(id).queryKey, exact: true });
  }
  return unwrap(result);
}

// Applying a reviewed plan also fills in the generated HRIDs and placeholder accounts.
function plannedQuery(made: Import, action: Action | undefined) {
  return infiniteQueryOptions({
    queryKey: [IMPORTS, made.id, made.planned_at, made.state, "entities", action ?? "all"],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam, signal }) =>
      planResponse(
        made.id,
        await iki.GET("/api/v1/imports/{import_id}/entities", {
          params: {
            path: { import_id: made.id },
            query: {
              limit: 100,
              ...(made.planned_at ? { planned_at: made.planned_at } : {}),
              ...(action ? { action } : {}),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

function peopleQuery(made: Import) {
  return infiniteQueryOptions({
    queryKey: [IMPORTS, made.id, made.planned_at, made.state, "people"],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam, signal }) =>
      planResponse(
        made.id,
        await iki.GET("/api/v1/imports/{import_id}/people", {
          params: {
            path: { import_id: made.id },
            query: {
              limit: 100,
              ...(made.planned_at ? { planned_at: made.planned_at } : {}),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** Uploads a bundle to import into the project, as a file is uploaded (ADR 0036). Iki then
 * checks it and plans the import. The person waits on it, so they count as active while it
 * runs (ADR 0153). */
export function importBundle(
  file: File,
  projectId: string,
  onProgress: (progress: Progress) => void,
  signal?: AbortSignal,
): Promise<Import> {
  return whileWorking(() => sendBundle(file, projectId, onProgress, signal));
}

async function sendBundle(
  file: File,
  projectId: string,
  onProgress: (progress: Progress) => void,
  signal?: AbortSignal,
): Promise<Import> {
  onProgress({ phase: "hashing", done: 0, total: file.size });
  const hash = await sha256Of(
    file,
    (done) => {
      onProgress({ phase: "hashing", done, total: file.size });
    },
    signal,
  );
  const started = unwrap(
    await iki.POST("/api/v1/projects/{project_id}/imports", {
      params: { path: { project_id: projectId } },
      body: { name: file.name, size: file.size, sha256: hash },
    }),
  );
  const path = { import_id: started.id };
  try {
    await sendParts(
      file,
      started.parts,
      started.parts[0]?.size ?? file.size,
      async (numbers) =>
        unwrap(
          await iki.POST("/api/v1/imports/{import_id}/parts", {
            params: { path },
            body: { numbers },
          }),
        ),
      onProgress,
      signal,
    );
    onProgress({ phase: "checking" });
    return unwrap(await iki.POST("/api/v1/imports/{import_id}/complete", { params: { path } }));
  } catch (error) {
    if (signal?.aborted) {
      // Nothing half-sent is kept.
      void iki.DELETE("/api/v1/imports/{import_id}", { params: { path } });
    }
    throw error;
  }
}

function plural(count: number, one: string, many: string): string {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`;
}

// --- The card in settings -----------------------------------------------------------------

export function Imports({ project }: { project: Project }) {
  const { say } = useMessages();
  const navigate = useNavigate();
  const id = useId();
  const imports = useQuery(importsQuery(project.id));
  const [file, setFile] = useState<File>();
  const [progress, setProgress] = useState<Progress>();
  const [error, setError] = useState<string>();
  const transfer = useRef<AbortController>(undefined);

  async function send(chosen: File) {
    setFile(chosen);
    setError(undefined);
    const controller = new AbortController();
    transfer.current = controller;
    try {
      const made = await importBundle(chosen, project.id, setProgress, controller.signal);
      say(`Uploaded ${chosen.name}. Iki is checking it and planning the import.`);
      await navigate({
        to: "/projects/$slug/imports/$importId/",
        params: { slug: project.slug, importId: made.id },
      });
    } catch (caught) {
      if (!controller.signal.aborted) setError(uploadError(caught));
    } finally {
      setProgress(undefined);
      setFile(undefined);
    }
  }

  return (
    <section className={imports.data?.length ? "card card-wide" : "card"}>
      <h2 className="section-title">Import</h2>
      <p>
        Bring in a bundle exported from another Iki deployment that this one trusts, such as from
        staging. Iki checks it and shows what importing it would do before anything changes.
      </p>
      {file && progress ? (
        <div className="upload">
          <p className="visually-hidden" role="status">
            {progress.phase === "hashing"
              ? `Reading ${file.name}`
              : progress.phase === "sending"
                ? `Uploading ${file.name}`
                : `Checking ${file.name}`}
          </p>
          <p aria-hidden="true">
            <UploadStatus name={file.name} progress={progress} />
          </p>
          {progress.phase === "checking" ? (
            <progress aria-label={`Checking ${file.name}`} />
          ) : (
            <>
              <progress
                aria-label={`${progress.phase === "hashing" ? "Reading" : "Uploading"} ${file.name}`}
                max={progress.total || 1}
                value={progress.done}
              />
              <Button
                variant="quiet"
                label={`Cancel uploading ${file.name}`}
                onClick={() => {
                  transfer.current?.abort();
                }}
              >
                Cancel
              </Button>
            </>
          )}
        </div>
      ) : (
        <div className={error ? "field field-invalid" : "field"}>
          <label htmlFor={`${id}-bundle`}>Bundle</label>
          <input
            id={`${id}-bundle`}
            type="file"
            accept=".tar.gz,.tgz,application/gzip"
            aria-invalid={error ? "true" : undefined}
            aria-describedby={error ? `${id}-error` : `${id}-help`}
            onChange={(event) => {
              const chosen = event.target.files?.[0];
              event.target.value = "";
              if (chosen) void send(chosen);
            }}
          />
          {error ? (
            <p className="field-error" id={`${id}-error`}>
              {error}
            </p>
          ) : (
            <p className="field-help" id={`${id}-help`}>
              A .iki-bundle.tar.gz file, from another deployment’s Export card.
            </p>
          )}
        </div>
      )}
      <ImportList project={project} query={imports} />
    </section>
  );
}

function ImportList({
  project,
  query,
}: {
  project: Project;
  query: ReturnType<typeof useQuery<Import[]>>;
}) {
  if (query.isError) return <ProblemAlert problem={problemOf(query.error)} />;
  if (!query.data) return <Loading label="Finding its imports…" />;
  if (query.data.length === 0) return null;
  return (
    <table className="table">
      <caption className="visually-hidden">Imports, newest first</caption>
      <thead>
        <tr>
          <th scope="col">Uploaded</th>
          <th scope="col">From</th>
          <th scope="col">State</th>
        </tr>
      </thead>
      <tbody>
        {query.data.map((made) => (
          <tr key={made.id}>
            <td>
              <Link
                to="/projects/$slug/imports/$importId/"
                params={{ slug: project.slug, importId: made.id }}
              >
                {made.name}
              </Link>
              <br />
              <span className="muted">
                <Time value={made.created_at} /> by {made.requested_by.name}
              </span>
            </td>
            <td>
              {made.source ? (
                <>
                  {made.source.deployment}
                  <br />
                  <span className="muted">{made.source.selection}</span>
                </>
              ) : (
                <span className="muted">Not read yet</span>
              )}
            </td>
            <td>
              <Badge muted={made.state !== "planned"}>{STATES[made.state]}</Badge>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// --- An import's page ---------------------------------------------------------------------

export function ImportPage() {
  const project = useProject();
  const { importId } = useParams({ from: "/projects/$slug/imports/$importId" });
  const found = useQuery(importQuery(importId));
  usePageTitle(found.data ? `Import of ${found.data.name} · ${project.name}` : project.name);
  const back = (
    <p className="eyebrow">
      <Link to="/projects/$slug/settings/" params={{ slug: project.slug }}>
        Settings
      </Link>
    </p>
  );
  if (!project.can.import_bundles) {
    return (
      <section className="card">
        <p>Only an Org Admin who is also the project’s Admin can import into it.</p>
      </section>
    );
  }
  if (found.isPending) return <Loading label="Loading the import…" />;
  if (found.isError) {
    return (
      <>
        {back}
        <ProblemAlert problem={problemOf(found.error)} />
      </>
    );
  }
  const made = found.data;
  return (
    <>
      {back}
      <h2>
        Import of {made.name} <Badge muted={made.state !== "planned"}>{STATES[made.state]}</Badge>
      </h2>
      {made.state === "uploading" || made.state === "planning" ? (
        <Loading label="Iki is checking the bundle and planning the import…" />
      ) : null}
      {made.state === "applying" ? (
        <Loading label="Iki is importing it: everything at once, or nothing…" />
      ) : null}
      {made.error ? (
        <div className="alert alert-error" role="alert">
          <p>{made.error}</p>
        </div>
      ) : null}
      {made.state === "applied" ? <Applied made={made} /> : null}
      <Source made={made} />
      {made.plan ? <Plan made={made} plan={made.plan} /> : null}
      {made.planned_at &&
      made.plan?.applicable &&
      (made.state === "planned" || made.state === "failed") ? (
        <Apply
          key={`${made.id}/${made.planned_at}`}
          made={made}
          plannedAt={made.planned_at}
          plan={made.plan}
          project={project}
        />
      ) : null}
      <Discard made={made} project={project} />
    </>
  );
}

function Source({ made }: { made: Import }) {
  const source = made.source;
  return (
    <section className="card">
      <h3 className="section-title">The bundle</h3>
      <dl className="summary">
        <dt>Uploaded</dt>
        <dd>
          By {made.requested_by.name} <RelativeTime value={made.created_at} />
        </dd>
        <dt>Archive</dt>
        <dd>
          {formatSize(made.size)}, SHA-256 <code>{made.sha256}</code>
        </dd>
        {source ? (
          <>
            <dt>From</dt>
            <dd>
              {made.trusted_as ? `${made.trusted_as}, a trusted deployment` : source.deployment}
              {source.url ? (
                <>
                  {" "}
                  (<span className="muted">{source.url}</span>)
                </>
              ) : null}
              , on Iki {source.iki}
            </dd>
            <dt>Exported</dt>
            <dd>
              By {source.exported_by["name"]} ({source.exported_by["email"]}){" "}
              <RelativeTime value={source.exported_at} />, from {source.project["team"]} /{" "}
              {source.project["name"]}
            </dd>
            <dt>What</dt>
            <dd>{source.selection}</dd>
            <dt>Why</dt>
            <dd className="preserve-lines">{source.reason}</dd>
          </>
        ) : null}
      </dl>
    </section>
  );
}

function Plan({ made, plan }: { made: Import; plan: Schemas["ImportPlan"] }) {
  const c = plan.counts;
  // Once applied, the plan is what it did: the same counts, said in the past.
  const done = made.state === "applied";
  const get = (n: number) =>
    done ? `${n.toLocaleString()} of them got` : plural(n, "of them gets", "of them get");
  return (
    <>
      <section className="card">
        <h3 className="section-title">{done ? "What it imported" : "The plan"}</h3>
        {done ? null : plan.problems.length ? (
          <div className="alert alert-warning" role="status">
            <p>It can’t be imported as it is:</p>
            <ul>
              {plan.problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          </div>
        ) : (
          <div className="alert alert-success" role="status">
            <p>Nothing in it conflicts with what’s here: it can be imported as planned.</p>
          </div>
        )}
        <ul className="plain-list">
          <li>
            {plural(c.new, "new entity", "new entities")}
            {c.renumbered ? `, ${get(c.renumbered)} a new HRID` : ""}
          </li>
          <li>
            {done
              ? plural(c.update, "entity got", "entities got")
              : plural(c.update, "entity gets", "entities get")}{" "}
            newer revisions
          </li>
          <li>
            {done
              ? plural(c.skip, "entity was", "entities were")
              : plural(c.skip, "entity is", "entities are")}{" "}
            here already
          </li>
          {done ? null : <li>{plural(c.conflict, "conflict", "conflicts")}</li>}
          <li>
            {plural(c.revisions, "revision", "revisions")} added, with{" "}
            {plural(c.files, "file", "files")}
          </li>
          {c.runs || c.runs_here ? (
            <li>
              {plural(c.runs, "run", "runs")} that made or changed them, with their logs
              {c.runs_renumbered ? `, ${get(c.runs_renumbered)} a new HRID` : ""}
              {c.runs_here
                ? `; ${done ? plural(c.runs_here, "was", "were") : plural(c.runs_here, "is", "are")} here already`
                : ""}
            </li>
          ) : null}
          <li>
            {plural(c.people, "person", "people")} named
            {c.placeholders
              ? done
                ? `: ${plural(c.placeholders, "was", "were")} nobody here, and became${c.placeholders === 1 ? " a placeholder" : " placeholders"} who can’t sign in`
                : `: ${plural(c.placeholders, "is", "are")} nobody here, and become${c.placeholders === 1 ? "s a placeholder" : " placeholders"} who can’t sign in`
              : ", each matched by email to someone here"}
          </li>
        </ul>
      </section>
      {made.state !== "discarded" ? (
        <>
          <Entities made={made} />
          <People made={made} />
        </>
      ) : null}
      <Types plan={plan} />
      {plan.outside.count ? <Outside plan={plan} /> : null}
    </>
  );
}

function Entities({ made }: { made: Import }) {
  const [action, setAction] = useState<Action | "">("");
  const list = useInfiniteQuery(plannedQuery(made, action || undefined));
  const rows = list.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <section className="card">
      <h3 className="section-title">Entities</h3>
      <SelectField
        label="Show"
        value={action}
        onChange={setAction}
        options={[
          { value: "", label: "All of them" },
          ...(Object.entries(ACTIONS) as [Action, string][]).map(([value, label]) => ({
            value,
            label,
          })),
        ]}
      />
      {list.isError ? <ProblemAlert problem={problemOf(list.error)} /> : null}
      {list.isPending ? <Loading label="Loading the plan…" /> : null}
      {rows.length ? (
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Entity</th>
              <th scope="col">Here as</th>
              <th scope="col">{made.state === "applied" ? "What happened" : "What happens"}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td>
                  {row.hrid}
                  {row.title !== row.hrid ? <span className="muted"> {row.title}</span> : null}
                  <br />
                  <span className="muted">
                    <code>{row.type}</code>, {plural(row.revisions, "revision", "revisions")}
                  </span>
                </td>
                <td>
                  {made.state === "applied" &&
                  row.hrid_here &&
                  (row.action === "new" || row.action === "update") ? (
                    <Link to="/entities/$ref/" params={{ ref: row.hrid_here }}>
                      {row.hrid_here}
                    </Link>
                  ) : (
                    row.hrid_here || <span className="muted">A new HRID</span>
                  )}
                </td>
                <td>
                  <Badge muted={row.action !== "conflict"}>{ACTIONS[row.action]}</Badge>
                  {row.action === "update"
                    ? ` ${plural(row.adds, "revision", "revisions")} added`
                    : ""}
                  {row.why ? <p className="muted">{row.why}</p> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : list.isSuccess ? (
        <p className="muted">None.</p>
      ) : null}
      {list.hasNextPage ? (
        <div className="actions">
          <Button
            busy={list.isFetchingNextPage}
            busyText="Loading…"
            onClick={() => void list.fetchNextPage()}
          >
            Show more
          </Button>
        </div>
      ) : null}
    </section>
  );
}

function People({ made }: { made: Import }) {
  const list = useInfiniteQuery(peopleQuery(made));
  const rows = list.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <section className="card">
      <h3 className="section-title">People</h3>
      <p className="muted">
        Their records keep who made each change. Someone with the same email address here is them;
        anyone else becomes a placeholder who can’t sign in, until someone with their email address
        joins.
      </p>
      {list.isError ? <ProblemAlert problem={problemOf(list.error)} /> : null}
      {rows.length ? (
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Email address</th>
              <th scope="col">Here</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => (
              <tr key={p.id}>
                <td>
                  {p.name}
                  {p.kind === "service" ? <span className="muted"> (service account)</span> : null}
                </td>
                <td>{p.email}</td>
                <td>
                  {p.user && !p.placeholder ? (
                    p.user.name
                  ) : (
                    <span className="muted">
                      {made.state === "applied" ? "Their placeholder" : "A placeholder"}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {list.hasNextPage ? (
        <div className="actions">
          <Button
            busy={list.isFetchingNextPage}
            busyText="Loading…"
            onClick={() => void list.fetchNextPage()}
          >
            Show more
          </Button>
        </div>
      ) : null}
    </section>
  );
}

function Types({ plan }: { plan: Schemas["ImportPlan"] }) {
  return (
    <section className="card">
      <h3 className="section-title">Types</h3>
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Type</th>
            <th scope="col">Plugin</th>
            <th scope="col">Schema version</th>
          </tr>
        </thead>
        <tbody>
          {plan.types.map((t) => (
            <tr key={t.type}>
              <td>
                <code>{t.type}</code>
              </td>
              <td>{t.plugin}</td>
              <td>
                {t.schema_version} in the bundle,{" "}
                {t.here === null ? "not here" : `${String(t.here)} here`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Outside({ plan }: { plan: Schemas["ImportPlan"] }) {
  const o = plan.outside;
  return (
    <section className="card">
      <h3 className="section-title">What it refers to, outside it</h3>
      <p className="muted">
        Its values refer to {plural(o.count, "entity", "entities")} it doesn’t carry.{" "}
        {plural(o.here, "is", "are")} here; references to the others point to nothing here until
        they’re imported too.
      </p>
      <ul className="plain-list">
        {o.items.map((item) => (
          <li key={item.id}>
            {item.hrid ?? <code>{item.id}</code>}
            {item.type ? (
              <>
                {" "}
                <code>{item.type}</code>
              </>
            ) : null}
            : {WHY_OUTSIDE[item.why]}
            {item.here ? ", and here" : ", not here"}
          </li>
        ))}
      </ul>
      {o.count > o.items.length ? (
        <p className="muted">And {(o.count - o.items.length).toLocaleString()} more.</p>
      ) : null}
    </section>
  );
}

function Applied({ made }: { made: Import }) {
  return (
    <section className="card">
      <h3 className="section-title">Imported</h3>
      <dl className="summary">
        <dt>By</dt>
        <dd>
          {made.applied_by?.name ?? "Someone"}{" "}
          {made.applied_at ? <RelativeTime value={made.applied_at} /> : null}
        </dd>
        <dt>Why</dt>
        <dd>{made.reason}</dd>
      </dl>
      <p className="muted">
        Each entity it made, or added revisions to, says on its page where it’s from.
      </p>
    </section>
  );
}

function Apply({
  made,
  plannedAt,
  plan,
  project,
}: {
  made: Import;
  plannedAt: string;
  plan: NonNullable<Import["plan"]>;
  project: Project;
}) {
  const { say } = useMessages();
  const [reason, setReason] = useState("");
  const [asking, setAsking] = useState(false);
  const apply = useApiMutation(
    async (why: string) =>
      planResponse(
        made.id,
        await iki.POST("/api/v1/imports/{import_id}/apply", {
          params: { path: { import_id: made.id } },
          body: { reason: why, planned_at: plannedAt },
        }),
      ),
    [[IMPORTS]],
  );
  // While asking, the dialog shows what went wrong.
  const form = useForm(asking ? null : apply.error, apply.isPending);
  const c = plan.counts;

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form.validate({ reason: required(reason, "Say why you’re importing it.") })) return;
    setAsking(true);
  }

  return (
    <section className="card">
      <h3 className="section-title">Import</h3>
      <p>
        Iki imports it whole, or nothing of it, and checks the plan again first: if anything here
        changed since, it shows you the new plan instead.
      </p>
      {form.alert}
      <form {...form.props} onSubmit={submit}>
        <TextField
          label="Why"
          value={reason}
          onChange={setReason}
          error={form.errors["reason"]}
          maxLength={2000}
          help="Such as “Moving the kinase work to production”. The import and the audit log keep it."
        />
        <Button type="submit" variant="primary">
          Import
        </Button>
      </form>
      <ConfirmDialog
        isOpen={asking}
        title={`Import into ${project.name}?`}
        confirmLabel="Import"
        confirmVariant="primary"
        busy={apply.isPending}
        problem={apply.error ? problemOf(apply.error) : undefined}
        onCancel={() => {
          setAsking(false);
        }}
        onConfirm={() => {
          apply.mutate(reason.trim(), {
            onSuccess: () => {
              setAsking(false);
              setReason("");
              say(`Importing ${made.name} into ${project.name}.`);
            },
            onError: (error) => {
              if (error.code === "stale_plan") {
                setAsking(false);
                say(error.problem.detail);
              }
            },
          });
        }}
      >
        <p>
          {plural(c.new, "new entity", "new entities")} and {plural(c.update, "entity", "entities")}{" "}
          with newer revisions, from {made.trusted_as || "another deployment"}.
          {c.placeholders
            ? ` ${plural(c.placeholders, "placeholder is", "placeholders are")} added for people nobody here is.`
            : ""}
        </p>
        <p>What it imports stays, with its history: it can’t be undone.</p>
      </ConfirmDialog>
    </section>
  );
}

function Discard({ made, project }: { made: Import; project: Project }) {
  const { say } = useMessages();
  const [asking, setAsking] = useState(false);
  const discard = useApiMutation(async () => {
    unwrap(
      await iki.DELETE("/api/v1/imports/{import_id}", {
        params: { path: { import_id: made.id } },
      }),
    );
  }, [[IMPORTS]]);
  if (made.state === "applying" || made.state === "applied" || made.state === "discarded") {
    return null;
  }
  return (
    <section className="card">
      <h3 className="section-title">Discard</h3>
      <p>Deletes the uploaded bundle and its plan, and imports nothing. The record of it stays.</p>
      <Button
        variant="danger"
        onClick={() => {
          setAsking(true);
        }}
      >
        Discard import
      </Button>
      <ConfirmDialog
        isOpen={asking}
        title={`Discard ${made.name}?`}
        confirmLabel="Discard import"
        busy={discard.isPending}
        problem={discard.error ? problemOf(discard.error) : undefined}
        onCancel={() => {
          setAsking(false);
        }}
        onConfirm={() => {
          discard.mutate(undefined, {
            onSuccess: () => {
              setAsking(false);
              say(`Discarded the import of ${made.name} into ${project.name}.`);
            },
          });
        }}
      >
        <p>Nothing from it is imported. To import it later, upload it again.</p>
      </ConfirmDialog>
    </section>
  );
}
