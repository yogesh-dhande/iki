/**
 * A grid's saved searches: links to each (yours, and those shared with the
 * project), and saving the grid's search under a name.
 *
 * A saved search keeps what the address does (type, archived or not, text,
 * filters and sort), so opening one is following a link to that address. The
 * sidebar's saved views link to `/saved-searches/<id>/`, which goes there
 * (pages/saved-search.tsx).
 */

import { unwrap } from "@ikisci/sdk";
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
import { fromAddress, toAddress } from "../search-address";
import { Button, ConfirmDialog, TextField, useMessages } from "../ui";

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
