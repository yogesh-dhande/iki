/**
 * A grid's saved searches: links to each (yours, and those shared with the
 * project), and saving the grid's search under a name.
 *
 * A saved search keeps what the address does (type, archived or not, text,
 * filters and sort), so opening one is following a link to that address.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useId, useState } from "react";

import { iki, problemOf, useApiMutation, type Project } from "../api";
import {
  SAVED_SEARCHES,
  savedSearchesQuery,
  type EntitySearch,
  type SavedSearch,
} from "../entities";
import type { Filter } from "../filters";
import { Button, ConfirmDialog, TextField, useMessages } from "../ui";

interface Query {
  type?: string | null;
  archived?: boolean | null;
  q?: string | null;
  where?: { all?: Filter[] } | Filter | null;
  sort?: { field: string; descending?: boolean } | null;
}

/** The grid's address for a saved search, or null if it holds what the grid can't show. */
export function toAddress(query: Query): EntitySearch | null {
  const out: EntitySearch = {};
  if (query.type) out.type = query.type;
  if (query.archived === true) out.show = "archived";
  else if (query.archived !== false) out.show = "all";
  if (query.q) out.q = query.q;
  if (query.where) {
    const where = query.where as Record<string, unknown>;
    const all = "all" in where ? (where["all"] as unknown[]) : "field" in where ? [where] : null;
    // Conditions the grid can show: every one of some fields' conditions.
    if (!all?.every((c) => typeof c === "object" && c !== null && "field" in c)) return null;
    out.filters = all as Filter[];
  }
  if (query.sort) {
    out.sort = query.sort.field;
    if (query.sort.descending) out.desc = true;
  }
  return out;
}

/** The grid's search, as a saved search keeps it. */
export function fromAddress(search: EntitySearch) {
  const show = search.show ?? "current";
  return {
    exact_type: false,
    ...(search.type ? { type: search.type } : {}),
    ...(show === "all" ? {} : { archived: show === "archived" }),
    ...(search.q ? { q: search.q } : {}),
    ...(search.filters?.length ? { where: { all: search.filters as Schemas["Condition"][] } } : {}),
    ...(search.sort ? { sort: { field: search.sort, descending: search.desc ?? false } } : {}),
  };
}

export function SavedSearches({ project, search }: { project: Project; search: EntitySearch }) {
  const saved = useQuery(savedSearchesQuery(project.id));
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState<SavedSearch | null>(null);
  const [name, setName] = useState("");
  const [share, setShare] = useState(false);
  const shareId = useId();
  const { say } = useMessages();
  const narrowed = Boolean(search.q) || Boolean(search.filters?.length) || Boolean(search.sort);
  const create = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/saved-searches", {
          body: {
            project: project.id,
            name: name.trim(),
            query: fromAddress(search),
            shared: share,
          },
        }),
      ),
    [SAVED_SEARCHES],
  );
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
  const searches = saved.data ?? [];
  const closeSave = () => {
    setSaving(false);
    setName("");
    setShare(false);
    create.reset();
  };

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
      <ConfirmDialog
        isOpen={saving}
        title="Save this search"
        confirmLabel="Save"
        confirmVariant="primary"
        busy={create.isPending}
        problem={create.error ? problemOf(create.error) : undefined}
        onCancel={closeSave}
        onConfirm={() => {
          create.mutate(undefined, {
            onSuccess: (made) => {
              closeSave();
              say(`Saved “${made.name}”.`);
            },
          });
        }}
      >
        <TextField label="Name" value={name} onChange={setName} maxLength={100} />
        {project.can.edit ? (
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
        ) : null}
      </ConfirmDialog>
      <ConfirmDialog
        isOpen={removing !== null}
        title={`Remove “${removing?.name ?? ""}”?`}
        confirmLabel="Remove"
        busy={remove.isPending}
        problem={remove.error ? problemOf(remove.error) : undefined}
        onCancel={() => {
          setRemoving(null);
          remove.reset();
        }}
        onConfirm={() => {
          if (!removing) return;
          remove.mutate(removing.id, {
            onSuccess: () => {
              say(`Removed “${removing.name}”.`);
              setRemoving(null);
            },
          });
        }}
      >
        <p>
          {removing?.shared
            ? `It’s shared, so it goes for everyone in ${project.name}.`
            : "Only the saved search goes: no entities change."}
        </p>
      </ConfirmDialog>
    </div>
  );
}
