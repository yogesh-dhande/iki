import { ApiError } from "@ikisci/sdk";
import { Link, type ErrorComponentProps } from "@tanstack/react-router";

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
