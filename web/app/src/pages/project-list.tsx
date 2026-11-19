import { useSuspenseInfiniteQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { meQuery, projectPagesQuery } from "../api";
import { usePageTitle } from "../layout";
import { Button, buttonClass, PageHeader } from "../ui";
import { ProjectRows } from "./project-rows";

export function ProjectList() {
  usePageTitle("Projects");
  const me = useSuspenseQuery(meQuery).data;
  // The first page shows at once; more as they're asked for.
  const pages = useSuspenseInfiniteQuery(projectPagesQuery());
  const projects = pages.data.pages.flatMap((p) => p.items);
  return (
    <>
      <PageHeader
        title="Projects"
        actions={
          me.can.create_projects ? (
            <Link to="/projects/new/" className={buttonClass("primary")}>
              New project
            </Link>
          ) : null
        }
      />
      {projects.length > 0 ? (
        <>
          <ProjectRows projects={projects} />
          {pages.hasNextPage ? (
            <div className="actions">
              <Button
                busy={pages.isFetchingNextPage}
                busyText="Loading…"
                onClick={() => void pages.fetchNextPage()}
              >
                Show more
              </Button>
            </div>
          ) : null}
        </>
      ) : (
        <section className="card empty">
          <p>
            You’re not in any projects yet.{" "}
            {me.can.create_projects ? (
              <Link to="/projects/new/">Create one</Link>
            ) : (
              "Ask a project Admin to add you, or an Org Admin to add you to a team."
            )}
          </p>
        </section>
      )}
    </>
  );
}
