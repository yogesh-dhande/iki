import { useInfiniteQuery, useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { meQuery, othersProjectsQuery, problemOf, projectPagesQuery } from "../api";
import { recordSearch, typesQuery } from "../entities";
import { provisionedOverScim, usePageTitle } from "../layout";
import { dueText, workQuery, workStates } from "../people";
import { registriesQuery } from "../registries";
import { Button, buttonClass, Loading, PageHeader, ProblemAlert } from "../ui";
import { PluginSections } from "./plugin-sections";
import { ProjectRows } from "./project-rows";

/** How many of what's assigned to you the home page lists: the rest are on My work. */
const WORK_SHOWN = 5;

/** The first of what's assigned to you (ADR 0122), soonest due first. */
function YourWork() {
  const work = useInfiniteQuery(workQuery({}));
  const types = useQuery(typesQuery);
  const items = work.data?.pages[0]?.items ?? [];
  return (
    <section aria-labelledby="work-title">
      <div className="section-header">
        <h2 className="section-title" id="work-title">
          Your work
        </h2>
        <Link to="/work/">Go to My work</Link>
      </div>
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a scrollable region */}
      <div className="home-work-items" role="group" aria-label="Assigned records" tabIndex={0}>
        {work.isPending || (items.length > 0 && types.isPending) ? (
          <Loading label="Loading your work…" />
        ) : work.isLoadingError ? (
          <ProblemAlert problem={problemOf(work.error)} />
        ) : items.length ? (
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
                    {entity.title !== entity.hrid
                      ? `${entity.title} (${entity.hrid})`
                      : entity.hrid}
                  </Link>
                  <span className="row-meta">
                    {[
                      ...workStates(
                        types.data?.find((t) => t.name === entity.type),
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
        ) : (
          <p className="muted">Nothing’s assigned to you.</p>
        )}
      </div>
    </section>
  );
}

export function Home() {
  usePageTitle("Home");
  const me = useSuspenseQuery(meQuery).data;
  // Your active projects, a page at a time, in the server's order.
  const projects = useInfiniteQuery(projectPagesQuery({ member: true, archived: false }));
  // Org Admins see every project's name: whether there are others is one row away.
  const others = useQuery({ ...othersProjectsQuery, enabled: me.can.manage_org }).data;
  // Registrars curate registries' entries (ADR 0115): theirs are a link away.
  const curated = (useQuery(registriesQuery()).data ?? []).filter((r) => r.can.curate);
  const fullName = me.name.trim();
  const firstName = fullName === "" ? me.email : (fullName.split(/\s+/)[0] ?? fullName);
  const mine = projects.data?.pages.flatMap((p) => p.items) ?? [];
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
          {projects.hasNextPage ? (
            <div className="actions">
              <Button
                busy={projects.isFetchingNextPage}
                busyText="Loading…"
                onClick={() => void projects.fetchNextPage()}
              >
                Show more
              </Button>
            </div>
          ) : null}
        </>
      ) : (
        <section className="card empty">
          {me.can.create_projects ? (
            <>
              <h2>No projects yet</h2>
              <p>
                Projects hold your entities, runs and views. Create one for your team, then add
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
                {`Every project belongs to a team. Create a team, add people ${
                  provisionedOverScim()
                    ? "once your identity provider provisions them"
                    : "once they’ve signed in"
                }, and its members can create projects.`}
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
      {others ? (
        <p className="muted">
          As an Org Admin you can also see projects you’re not a member of.{" "}
          <Link to="/projects/">See all projects</Link>
        </p>
      ) : null}
    </>
  );
}
