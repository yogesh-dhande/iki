import { unwrap } from "@ikisci/sdk";
import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";

import {
  iki,
  meQuery,
  membersQuery,
  problemOf,
  projectQuery,
  rolesQuery,
  useApiMutation,
  type Project,
} from "../api";
import { usePageTitle } from "../layout";
import { ProjectCosts } from "./cost-table";
import { Badge, Button, PageHeader, ProblemAlert, Time, useMessages } from "../ui";

/** The project the current page is about, from the `$slug` in its address. */
export function useProject(): Project {
  const { slug } = useParams({ from: "/projects/$slug" });
  return useSuspenseQuery(projectQuery(slug)).data;
}

const tab = { "aria-current": "page" } as const;

/** The project's heading and tabs, shared by its pages. */
export function ProjectTabs() {
  const project = useProject();
  return (
    <>
      <PageHeader
        eyebrow={<a href={`/teams/${project.team.slug}/`}>{project.team.name}</a>}
        title={project.name}
        badge={
          project.archived || !project.allow_sharing ? (
            <>
              {project.archived ? <Badge muted>Archived</Badge> : null}
              {project.archived && !project.allow_sharing ? " " : null}
              {project.allow_sharing ? null : <Badge muted>Not shared outside this project</Badge>}
            </>
          ) : null
        }
      />
      <nav className="tabs" aria-label="Project">
        <Link
          to="/projects/$slug/"
          params={{ slug: project.slug }}
          activeOptions={{ exact: true }}
          activeProps={tab}
        >
          Overview
        </Link>
        {project.can.read ? (
          <Link to="/projects/$slug/entities/" params={{ slug: project.slug }} activeProps={tab}>
            Entities
          </Link>
        ) : null}
        {project.can.read ? (
          <Link to="/projects/$slug/runs/" params={{ slug: project.slug }} activeProps={tab}>
            Runs
          </Link>
        ) : null}
        <Link to="/projects/$slug/members/" params={{ slug: project.slug }} activeProps={tab}>
          Members
        </Link>
        {project.can.manage_webhooks ? (
          <Link to="/projects/$slug/webhooks/" params={{ slug: project.slug }} activeProps={tab}>
            Webhooks
          </Link>
        ) : null}
        {project.can.manage ? (
          <Link to="/projects/$slug/settings/" params={{ slug: project.slug }} activeProps={tab}>
            Settings
          </Link>
        ) : null}
      </nav>
    </>
  );
}

/** An Org Admin who isn't a project Admin makes themselves one. The server records it. */
export function useJoinAsAdmin(project: Project) {
  const me = useSuspenseQuery(meQuery).data;
  const { say } = useMessages();
  const join = useApiMutation(
    async () =>
      unwrap(
        await iki.PUT("/api/v1/projects/{project_id}/members/users/{user_id}", {
          params: { path: { project_id: project.id, user_id: me.id } },
          body: { role: "admin" },
        }),
      ),
    [["projects"], ["me"]],
  );
  return {
    join: () => {
      join.mutate(undefined, {
        onSuccess: () => {
          say(`You’re now an Admin of ${project.name}.`);
        },
      });
    },
    busy: join.isPending,
    problem: join.error ? problemOf(join.error) : undefined,
  };
}

export function ProjectOverview() {
  const project = useProject();
  usePageTitle(project.name);
  const roles = useQuery(rolesQuery).data ?? [];
  const role = roles.find((r) => r.name === project.my_role);

  return (
    <>
      {project.archived ? (
        <div className="alert alert-warning" role="note">
          <p>
            This project is archived and read-only.
            {project.can.manage ? (
              <>
                {" "}
                You can restore it in{" "}
                <Link to="/projects/$slug/settings/" params={{ slug: project.slug }}>
                  Settings
                </Link>
                .
              </>
            ) : null}
          </p>
        </div>
      ) : null}
      {project.can.read ? (
        <section className="card">
          {project.description ? <p className="preserve-lines">{project.description}</p> : null}
          <dl className="summary">
            <dt>Team</dt>
            <dd>
              <a href={`/teams/${project.team.slug}/`}>{project.team.name}</a>
            </dd>
            {role ? (
              <>
                <dt>Your role</dt>
                <dd>
                  <strong>{role.label}</strong>: {role.description}
                </dd>
              </>
            ) : null}
            <dt>Created</dt>
            <dd>
              <Time value={project.created_at} />
            </dd>
          </dl>
          <p>
            <Link to="/projects/$slug/entities/" params={{ slug: project.slug }}>
              Browse the project’s entities
            </Link>
          </p>
        </section>
      ) : (
        <NotAMember project={project} />
      )}
      {project.can.read ? <ProjectCosts project={project} /> : null}
    </>
  );
}

function NotAMember({ project }: { project: Project }) {
  const members = useQuery(membersQuery(project.id)).data;
  const admins = members?.filter((m) => m.role === "admin").length;
  const { join, busy, problem } = useJoinAsAdmin(project);
  return (
    <section className="card">
      <h2 className="section-title">You’re not a member of this project</h2>
      <p>
        As an Org Admin you can see that it exists and who its members are, but not its data. To
        work in it, add yourself as an Admin. This is recorded.
      </p>
      {admins === 0 ? (
        <p>
          <strong>This project has no Admins.</strong> Add yourself to manage it, then hand it to
          someone on the team.
        </p>
      ) : null}
      <ProblemAlert problem={problem} />
      <Button variant="primary" busy={busy} busyText="Adding…" onClick={join}>
        Add me as Admin
      </Button>
    </section>
  );
}
