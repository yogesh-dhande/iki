/**
 * A saved search at its own address, `/saved-searches/<id>/` (the sidebar's saved views):
 * it opens at the grid's address for it, or says why it can't.
 */

import { unwrap } from "@ikisci/sdk";
import { queryOptions, useSuspenseQuery, type QueryClient } from "@tanstack/react-query";
import { Link, redirect, useParams } from "@tanstack/react-router";

import { iki } from "../api";
import { SAVED_SEARCHES } from "../entities";
import { usePageTitle } from "../layout";
import { TopBar } from "../page-top";
import { toAddress } from "../search-address";
import { PageTitle } from "../ui";

function savedSearchQuery(id: string) {
  return queryOptions({
    queryKey: [...SAVED_SEARCHES, "one", id],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/saved-searches/{saved_id}", {
          params: { path: { saved_id: id } },
          signal,
        }),
      ),
  });
}

function projectByIdQuery(id: string) {
  return queryOptions({
    queryKey: ["projects", "by-id", id],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/projects/{project_id}", {
          params: { path: { project_id: id } },
          signal,
        }),
      ),
  });
}

/** Opening a saved search from its own address (a saved view): its project's grid, or,
 * for a search of every project, All entities, with the search in the address. One the
 * grid can't show stays here, to say so. */
export async function openSavedSearch(client: QueryClient, id: string): Promise<void> {
  const saved = await client.query(savedSearchQuery(id));
  const address = toAddress(saved.query);
  if (!address) return;
  if (!saved.project) {
    // eslint-disable-next-line @typescript-eslint/only-throw-error -- how TanStack redirects
    throw redirect({ to: "/entities/", search: address, replace: true });
  }
  const project = await client.query(projectByIdQuery(saved.project));
  // eslint-disable-next-line @typescript-eslint/only-throw-error -- how TanStack redirects
  throw redirect({
    to: "/projects/$slug/entities/",
    params: { slug: project.slug },
    search: address,
    replace: true,
  });
}

export function SavedSearchPage() {
  const { savedId } = useParams({ from: "/saved-searches/$savedId" });
  const saved = useSuspenseQuery(savedSearchQuery(savedId)).data;
  usePageTitle(saved.name);
  return (
    <section className="card">
      <TopBar crumbs={[<Link to="/entities/">Entities</Link>, saved.name]} />
      <PageTitle>{saved.name}</PageTitle>
      <p>
        This search was saved through Iki’s API, with conditions the grid can’t show, so it can’t
        open there.{" "}
        {saved.project ? (
          <ProjectEntitiesLink id={saved.project} />
        ) : (
          <>
            Everything you can read is in <Link to="/entities/">All entities</Link>.
          </>
        )}
      </p>
    </section>
  );
}

function ProjectEntitiesLink({ id }: { id: string }) {
  const project = useSuspenseQuery(projectByIdQuery(id)).data;
  return (
    <>
      Its project’s entities are{" "}
      <Link to="/projects/$slug/entities/" params={{ slug: project.slug }}>
        in {project.name}
      </Link>
      .
    </>
  );
}
