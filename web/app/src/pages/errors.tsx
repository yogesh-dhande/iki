import { ApiError } from "@ikisci/sdk";
import { Link, useParams, type ErrorComponentProps } from "@tanstack/react-router";

import { usePageTitle } from "../layout";
import { buttonClass, PageHeader } from "../ui";

export function NotFound() {
  usePageTitle("Not found");
  return (
    <>
      <PageHeader title="Not found" />
      <section className="card">
        <p>There’s nothing here, or you don’t have access to it.</p>
        <p>
          <Link to="/projects/" className={buttonClass("primary")}>
            Go to your projects
          </Link>
        </p>
      </section>
    </>
  );
}

export function ErrorPage({ error, reset }: ErrorComponentProps) {
  const status = error instanceof ApiError ? error.status : 0;
  usePageTitle(status === 403 ? "Not allowed" : status === 410 ? "Purged" : "Something went wrong");
  if (status === 404) {
    return <NotFound />;
  }
  if (status === 410) {
    return <Gone error={error as ApiError} />;
  }
  if (status === 403) {
    return (
      <>
        <PageHeader title="Not allowed" />
        <section className="card">
          <p>{(error as ApiError).problem.detail}</p>
        </section>
      </>
    );
  }
  const requestId = error instanceof ApiError ? error.problem.request_id : undefined;
  return (
    <>
      <PageHeader title="Something went wrong" />
      <section className="card">
        <p>
          {error instanceof ApiError
            ? error.problem.detail
            : "Iki couldn’t load this page. Check your connection and try again."}
        </p>
        {requestId ? (
          <p className="muted">
            If it keeps happening, give your administrator this reference: <code>{requestId}</code>
          </p>
        ) : null}
        <p>
          <button className="button button-primary" type="button" onClick={reset}>
            Try again
          </button>
        </p>
      </section>
    </>
  );
}

/** What a purge left of an entity (ADR 0092): its HRID, type and a fingerprint of its values. */
function Gone({ error }: { error: ApiError }) {
  const entity = (
    error.problem as { entity?: { hrid: string; type: string; content_sha256: string | null } }
  ).entity;
  return (
    <>
      <PageHeader eyebrow={entity?.type} title={entity ? `${entity.hrid} was purged` : "Purged"} />
      <section className="card">
        <p>{error.problem.detail}</p>
        {entity?.content_sha256 ? (
          <p className="muted">
            A SHA-256 of its last values, to check an export against:{" "}
            <code>{entity.content_sha256}</code>
          </p>
        ) : null}
      </section>
    </>
  );
}

/** The records an HRID names, one in each of the reader's projects (ADR 0115). */
export interface Choice {
  entity: string;
  project: { id: string; name: string; slug: string };
}

/** The records to choose between, when the API answered that an HRID names several. */
export function choicesIn(error: unknown): Choice[] | null {
  return error instanceof ApiError && error.problem.code === "choose_project"
    ? ((error.problem as { records?: Choice[] }).records ?? [])
    : null;
}

/** An entity page's error: when its HRID names several projects' records of a registry
 * entry, which project's to open; anything else as any page's. */
export function EntityError(props: ErrorComponentProps) {
  const records = choicesIn(props.error);
  return records ? <ChooseProject records={records} /> : <ErrorPage {...props} />;
}

export function ChooseProject({ records }: { records: Choice[] }) {
  const { ref = "" } = useParams({ strict: false });
  const hrid = ref.toUpperCase();
  usePageTitle(`${hrid}: which project?`);
  const sorted = [...records].sort((a, b) => a.project.name.localeCompare(b.project.name));
  return (
    <>
      <PageHeader title={hrid} />
      <section className="card" aria-labelledby="choose-project-title">
        <h2 className="section-title" id="choose-project-title">
          Which project’s record?
        </h2>
        <p>
          {hrid} is in {sorted.length} of your projects. Each has its own record of it, with its own
          values and history.
        </p>
        <ul className="rows">
          {sorted.map((r) => (
            <li className="row" key={r.entity}>
              <div className="row-main">
                {/* The same page (overview, history, …), as that project's record. */}
                <Link className="row-title" to="./" search={{ project: r.project.slug }}>
                  {r.project.name}
                </Link>
              </div>
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}
