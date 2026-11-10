import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { meQuery, projectsQuery } from "../api";
import { usePageTitle } from "../layout";
import { buttonClass, Loading, PageHeader } from "../ui";
import { ProjectRows, sortProjects } from "./project-rows";

export function Home() {
  usePageTitle("Home");
  const me = useSuspenseQuery(meQuery).data;
  const projects = useQuery(projectsQuery);
  const fullName = me.name.trim();
  const firstName = fullName === "" ? me.email : (fullName.split(/\s+/)[0] ?? fullName);
  const mine = sortProjects((projects.data ?? []).filter((p) => p.my_role && !p.archived));
  const others = (projects.data ?? []).filter((p) => !p.my_role && !p.archived).length;
  const newProject = me.can.create_projects ? (
    <Link to="/projects/new/" className={buttonClass("primary")}>
      New project
    </Link>
  ) : null;

  return (
    <>
      <PageHeader title={`Welcome, ${firstName}`} actions={newProject} />
      {projects.isPending ? (
        <Loading label="Loading your projects…" />
      ) : mine.length > 0 ? (
        <>
          <h2>Your projects</h2>
          <ProjectRows projects={mine} />
        </>
      ) : (
        <section className="card empty">
          {me.can.create_projects ? (
            <>
              <h2>No projects yet</h2>
              <p>
                Projects hold your entities, runs and views. Create one for your team, then invite
                people to it.
              </p>
              <p>
                <Link to="/projects/new/" className={buttonClass("primary")}>
                  Create a project
                </Link>
              </p>
            </>
          ) : me.can.manage_org ? (
            <>
              <h2>Start by creating a team</h2>
              <p>
                Every project belongs to a team. Create a team, add people once they’ve signed in,
                and its members can create projects.
              </p>
              <p>
                <a className="button button-primary" href="/teams/">
                  Go to teams
                </a>
              </p>
            </>
          ) : (
            <>
              <h2>You’re not in any projects yet</h2>
              <p>
                Ask a project Admin to add you, or ask an Org Admin to add you to a team so you can
                create projects.
              </p>
            </>
          )}
        </section>
      )}
      {others > 0 ? (
        <p className="muted">
          As an Org Admin you can also see {others} project{others === 1 ? "" : "s"} you’re not a
          member of. <Link to="/projects/">See all projects</Link>
        </p>
      ) : null}
    </>
  );
}
