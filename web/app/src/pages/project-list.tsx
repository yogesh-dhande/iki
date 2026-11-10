import { useSuspenseQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { meQuery, projectsQuery } from "../api";
import { usePageTitle } from "../layout";
import { buttonClass, PageHeader } from "../ui";
import { ProjectRows, sortProjects } from "./project-rows";

export function ProjectList() {
  usePageTitle("Projects");
  const me = useSuspenseQuery(meQuery).data;
  const projects = sortProjects(useSuspenseQuery(projectsQuery).data);
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
        <ProjectRows projects={projects} />
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
