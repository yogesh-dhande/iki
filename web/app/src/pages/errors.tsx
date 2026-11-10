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
  usePageTitle(status === 403 ? "Not allowed" : "Something went wrong");
  if (status === 404) {
    return <NotFound />;
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
