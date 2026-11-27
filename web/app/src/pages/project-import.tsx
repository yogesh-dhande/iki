/**
 * An import's page (ADR 0096): what Iki found when it checked the bundle, and the plan for
 * each entity and person, before anything changes; then applying it, or discarding it.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useState, type SubmitEvent } from "react";

import { iki, problemOf, useApiMutation, type Project } from "../api";
import { formatSize } from "../entities";
import { required, useForm } from "../forms";
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
  useMessages,
} from "../ui";
import { useProject } from "../project";
import {
  IMPORTS,
  importQuery,
  peopleQuery,
  planResponse,
  plannedQuery,
  STATES,
  type Action,
  type Import,
} from "./project-imports";

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

function plural(count: number, one: string, many: string): string {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`;
}

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
      {made.state === "scanning" ? <Loading label="Scanning the bundle…" /> : null}
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
      {plan.signing_keys.length ? <SigningKeys made={made} plan={plan} /> : null}
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

/** The signing keys it brings that aren't trusted here yet: who each signs for, and
 * whether the deployment it's from vouches for another's. */
function SigningKeys({ made, plan }: { made: Import; plan: Schemas["ImportPlan"] }) {
  const from = made.trusted_as || "the deployment it’s from";
  return (
    <section className="card">
      <h3 className="section-title">Signing keys it brings</h3>
      <p className="muted">
        {made.state === "applied"
          ? "Its electronic signatures were checked with these keys, which this deployment now trusts for them. Each is in the audit log."
          : "Its electronic signatures are checked with these keys, which this deployment doesn’t trust yet. Importing trusts each for the signatures it brings, and records that in the audit log."}
      </p>
      <ul className="plain-list">
        {plan.signing_keys.map((k) => (
          <li key={k.key}>
            <code>{k.key}</code>:{" "}
            {k.source ? `${k.deployment}’s own` : `${k.deployment}’s, vouched for by ${from}`}
          </li>
        ))}
      </ul>
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
