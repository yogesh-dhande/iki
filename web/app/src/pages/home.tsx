import { useInfiniteQuery, useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { meQuery, projectsQuery } from "../api";
import { recordSearch, typesQuery } from "../entities";
import { usePageTitle } from "../layout";
import { dueText, workQuery, workStates } from "../people";
import { registriesQuery } from "../registries";
import { buttonClass, Loading, PageHeader } from "../ui";
import { PluginSections } from "./plugin-sections";
import { ProjectRows, sortProjects } from "./project-rows";

/** How many of what's assigned to you the home page lists: the rest are on My work. */
const WORK_SHOWN = 5;

/** The first of what's assigned to you (ADR 0122), soonest due first, if there's any. */
function YourWork() {
  const work = useInfiniteQuery(workQuery({}));
  const types = useQuery(typesQuery).data;
  const items = work.data?.pages[0]?.items ?? [];
  if (!items.length) return null;
  const more = items.length > WORK_SHOWN || Boolean(work.data?.pages[0]?.next_cursor);
  return (
    <section aria-labelledby="work-title">
      <h2 id="work-title">Your work</h2>
      <ul className="rows">
        {items.slice(0, WORK_SHOWN).map(({ entity, due }) => (
          <li className="row" key={entity.id}>
            <div className="row-main">
              <Link
                className="row-title"
                to="/entities/$ref/"
                params={{ ref: entity.hrid }}
                search={recordSearch(entity)}
              >
                {entity.title !== entity.hrid ? `${entity.title} (${entity.hrid})` : entity.hrid}
              </Link>
              <span className="row-meta">
                {[
                  ...workStates(
                    types?.find((t) => t.name === entity.type),
                    entity,
                  ),
                  entity.owner_project?.name,
                  due ? `Due ${dueText(due).text}` : undefined,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
            </div>
          </li>
        ))}
      </ul>
      <p>
        <Link to="/work/">{more ? "See all your work" : "Go to My work"}</Link>
      </p>
    </section>
  );
}

export function Home() {
  usePageTitle("Home");
  const me = useSuspenseQuery(meQuery).data;
  const projects = useQuery(projectsQuery);
  // Registrars curate registries' entries (ADR 0115): theirs are a link away.
  const curated = (useQuery(registriesQuery()).data ?? []).filter((r) => r.can.curate);
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
      <YourWork />
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
      {/* What plugins' components find of theirs, in every project they read (ADR 0117). */}
      <PluginSections kind="person.home" />
      {curated.length ? (
        <section aria-labelledby="curated-title">
          <h2 id="curated-title">Registries you curate</h2>
          <ul className="rows">
            {curated.map((r) => (
              <li className="row" key={r.id}>
                <div className="row-main">
                  <Link
                    className="row-title"
                    to="/registries/$registryId/entries/"
                    params={{ registryId: r.id }}
                  >
                    {r.name}
                  </Link>
                  {r.description ? <span className="row-meta">{r.description}</span> : null}
                </div>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {others > 0 ? (
        <p className="muted">
          As an Org Admin you can also see {others} project{others === 1 ? "" : "s"} you’re not a
          member of. <Link to="/projects/">See all projects</Link>
        </p>
      ) : null}
    </>
  );
}
