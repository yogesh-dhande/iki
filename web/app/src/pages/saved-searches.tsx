/**
 * A grid's saved searches: links to each (yours, and those shared with the
 * project), and saving the grid's search under a name.
 *
 * A saved search keeps what the address does (type, archived or not, text,
 * filters and sort), so opening one is following a link to that address. The
 * sidebar's saved views link to `/saved-searches/<id>/`, which goes there.
 */

import { unwrap } from "@ikisci/sdk";
import { queryOptions, useQuery, useSuspenseQuery, type QueryClient } from "@tanstack/react-query";
import { Link, redirect, useParams } from "@tanstack/react-router";
import { useId, useState } from "react";

import { iki, problemOf, useApiMutation, type Project } from "../api";
import { usePageTitle } from "../layout";
import { TopBar } from "../page-top";
import {
  gridSearch,
  SAVED_SEARCHES,
  savedSearchesQuery,
  validateEntitySearch,
  type EntitySearch,
  type SavedSearch,
} from "../entities";
import { sameJson, searchWords, wordsOf, type Filter } from "../filters";
import { Button, ConfirmDialog, PageTitle, TextField, useMessages } from "../ui";

interface Query {
  type?: string | null;
  archived?: boolean | null;
  exact_type?: boolean | null;
  q?: string | null;
  where?: { all?: unknown[] } | Filter | null;
  sort?: { field: string; descending?: boolean } | null;
}

/**
 * The grid's address for a saved search, or null if it holds what the grid can't show:
 * opening it would run another search. The grid's own searches come back as they were
 * made (see `gridSearch`): the filters', and a group of conditions the search box's words
 * make, as those words again.
 */
export function toAddress(query: Query): EntitySearch | null {
  const out: EntitySearch = {};
  // The grid finds a type's subtypes too.
  if (query.exact_type) return null;
  if (query.type) out.type = query.type;
  if (query.archived === true) out.show = "archived";
  else if (query.archived !== false) out.show = "all";
  // Text the search box would read as conditions isn't text to it.
  if (query.q && searchWords(query.q).conditions.length) return null;
  const words: string[] = [];
  if (query.where) {
    const where = query.where as Record<string, unknown>;
    const all = "all" in where ? (where["all"] as unknown[]) : "field" in where ? [where] : null;
    if (!all) return null;
    const filters: Filter[] = [];
    for (const c of all) {
      if (typeof c !== "object" || c === null) return null;
      if ("field" in c) {
        filters.push(c as Filter);
      } else if ("all" in c && Array.isArray(c.all) && !words.length) {
        const said = wordsOf(c.all);
        if (!said?.length) return null;
        words.push(...said);
      } else {
        return null;
      }
    }
    if (filters.length) out.filters = filters;
  }
  const q = [query.q ?? "", ...words].filter(Boolean).join(" ");
  if (q) out.q = q;
  if (query.sort) {
    out.sort = query.sort.field;
    if (query.sort.descending) out.desc = true;
  }
  // The grid reads only so many filters and so much text from its address. A search that
  // needs more isn't a link: the grid would run it without what it cut.
  return sameJson(validateEntitySearch({ ...out }), out) ? out : null;
}

/** The grid's search, as a saved search keeps it: what the grid runs, without its project. */
export function fromAddress(search: EntitySearch) {
  const show = search.show ?? "current";
  return gridSearch({
    type: search.type,
    archived: show === "all" ? undefined : show === "archived",
    q: search.q,
    filters: search.filters,
    sort: search.sort,
    descending: search.desc,
  });
}

export function SavedSearches({ project, search }: { project: Project; search: EntitySearch }) {
  const saved = useQuery(savedSearchesQuery(project.id));
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState<SavedSearch | null>(null);
  const narrowed = Boolean(search.q) || Boolean(search.filters?.length) || Boolean(search.sort);
  const searches = saved.data ?? [];

  if (!searches.length && !narrowed) return null;
  return (
    <div className="saved-searches">
      {searches.length ? (
        <nav aria-label="Saved searches">
          <ul className="saved-list">
            {searches.map((s) => {
              const address = toAddress(s.query);
              return (
                <li key={s.id} className="saved-item">
                  {address ? (
                    <Link
                      to="/projects/$slug/entities/"
                      params={{ slug: project.slug }}
                      search={address}
                      className="saved-link"
                      // The link to where the grid is gets aria-current="page".
                      activeOptions={{ exact: true, includeSearch: true }}
                    >
                      {s.name}
                    </Link>
                  ) : (
                    <span
                      className="saved-link"
                      title="Made with the API: the grid can’t show its conditions."
                    >
                      {s.name}
                    </span>
                  )}
                  {s.shared ? <span className="saved-shared">Shared</span> : null}
                  {s.can.delete ? (
                    <Button
                      variant="quiet"
                      label={`Remove saved search ${s.name}`}
                      onClick={() => {
                        setRemoving(s);
                      }}
                    >
                      ×
                    </Button>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </nav>
      ) : null}
      {narrowed ? (
        <Button
          onClick={() => {
            setSaving(true);
          }}
        >
          Save this search
        </Button>
      ) : null}
      <SaveSearchDialog
        isOpen={saving}
        project={project}
        search={search}
        onClose={() => {
          setSaving(false);
        }}
      />
      <RemoveSearchDialog
        removing={removing}
        project={project}
        onClose={() => {
          setRemoving(null);
        }}
      />
    </div>
  );
}

function savedSearchQuery(id: string) {
  return queryOptions({
    queryKey: [...SAVED_SEARCHES, "one", id],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/saved-searches/{saved_id}", { params: { path: { saved_id: id } } }),
      ),
  });
}

function projectByIdQuery(id: string) {
  return queryOptions({
    queryKey: ["projects", "by-id", id],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/projects/{project_id}", { params: { path: { project_id: id } } }),
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

/** Saving a grid's search under a name: in `project`, which its Editors may share it
 * with, or without one, a search of every project its saver reads, theirs alone. */
export function SaveSearchDialog({
  isOpen,
  project,
  search,
  title = "Save this search",
  onClose,
}: {
  isOpen: boolean;
  project: Project | undefined;
  search: EntitySearch;
  title?: string;
  onClose: (saved?: SavedSearch) => void;
}) {
  const [name, setName] = useState("");
  const [share, setShare] = useState(false);
  const shareId = useId();
  const { say } = useMessages();
  const create = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/saved-searches", {
          body: {
            ...(project ? { project: project.id } : {}),
            name: name.trim(),
            query: fromAddress(search),
            shared: Boolean(project) && share,
          },
        }),
      ),
    [SAVED_SEARCHES],
  );
  const close = (made?: SavedSearch) => {
    setName("");
    setShare(false);
    create.reset();
    onClose(made);
  };
  return (
    <ConfirmDialog
      isOpen={isOpen}
      title={title}
      confirmLabel="Save"
      confirmVariant="primary"
      busy={create.isPending}
      problem={create.error ? problemOf(create.error) : undefined}
      onCancel={() => {
        close();
      }}
      onConfirm={() => {
        create.mutate(undefined, {
          onSuccess: (made) => {
            close(made);
            say(`Saved “${made.name}”.`);
          },
        });
      }}
    >
      <TextField label="Name" value={name} onChange={setName} maxLength={100} />
      {project?.can.edit ? (
        <label className="choice" htmlFor={shareId}>
          <input
            id={shareId}
            type="checkbox"
            checked={share}
            onChange={(event) => {
              setShare(event.target.checked);
            }}
          />
          <span className="choice-text">
            Share with everyone in {project.name}
            <span className="choice-help">Only you can change it.</span>
          </span>
        </label>
      ) : project ? null : (
        <p className="field-help">
          It finds what you can read in every project you’re in. Only you see it.
        </p>
      )}
    </ConfirmDialog>
  );
}

/** Removing a saved search, saying who else it goes for. */
export function RemoveSearchDialog({
  removing,
  project,
  onClose,
}: {
  removing: SavedSearch | null;
  /** Its project, by name, when it's shared there. */
  project: { name: string } | undefined;
  onClose: (removed: boolean) => void;
}) {
  const { say } = useMessages();
  const remove = useApiMutation(
    async (id: string) => {
      unwrap(
        await iki.DELETE("/api/v1/saved-searches/{saved_id}", {
          params: { path: { saved_id: id } },
        }),
      );
    },
    [SAVED_SEARCHES],
  );
  return (
    <ConfirmDialog
      isOpen={removing !== null}
      title={`Remove “${removing?.name ?? ""}”?`}
      confirmLabel="Remove"
      busy={remove.isPending}
      problem={remove.error ? problemOf(remove.error) : undefined}
      onCancel={() => {
        remove.reset();
        onClose(false);
      }}
      onConfirm={() => {
        if (!removing) return;
        remove.mutate(removing.id, {
          onSuccess: () => {
            say(`Removed “${removing.name}”.`);
            onClose(true);
          },
        });
      }}
    >
      <p>
        {removing?.shared
          ? `It’s shared, so it goes for everyone in ${project?.name ?? "its project"}.`
          : "Only the saved search goes: no entities change."}
      </p>
    </ConfirmDialog>
  );
}
