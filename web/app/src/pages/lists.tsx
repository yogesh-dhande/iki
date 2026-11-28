/**
 * The lists across projects (ADR 0176): All entities, one type's table, and every request.
 * Each is the one table of records (`EntityGrid`), over every project its reader can read;
 * a project's tabs are the same table, held to it.
 */

import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import { Button as AriaButton, Menu, MenuItem, MenuTrigger, Popover } from "react-aria-components";

import type { Project } from "../api";
import { inSentence, typesQuery, type EntityType, type SavedSearch } from "../entities";
import { askedByField, sameJson } from "../filters";
import { usePageTitle } from "../layout";
import { everySavedSearchQuery, useReadableProjects, type ListSearch } from "../lists";
import { requestTypes } from "../requests";
import { TypeDot } from "../type-color";
import { buttonClass, PageHeader } from "../ui";
import { EntityGrid } from "./entity-grid";
import { toAddress } from "../search-address";
import { RemoveSearchDialog, SaveSearchDialog } from "./saved-searches";

/** "New": a record is made in a project, so it asks which, of those its maker edits. With
 * `importing`, the same for importing a spreadsheet's rows as the type's (ADR 0178). */
function NewIn({
  type,
  label,
  importing = false,
}: {
  type?: EntityType | undefined;
  label: string;
  importing?: boolean;
}) {
  const navigate = useNavigate();
  const { projects } = useReadableProjects();
  const editable = projects.filter((p) => p.can.edit && !p.archived);
  if (!editable.length || type?.abstract || type?.detached) return null;
  return (
    <MenuTrigger>
      <AriaButton className={buttonClass(importing ? undefined : "primary")}>
        {label}
        <span aria-hidden="true"> ▾</span>
      </AriaButton>
      <Popover className="menu-popover" placement="bottom end" offset={4}>
        {/* Named by its button, as a menu opened from one is. */}
        <Menu
          className="menu"
          onAction={(slug) => {
            void navigate({
              to: importing ? "/projects/$slug/entities/import/" : "/projects/$slug/entities/new/",
              params: { slug: String(slug) },
              search: type ? { type: type.name } : {},
            });
          }}
        >
          {editable.map((p) => (
            <MenuItem key={p.slug} id={p.slug} className="menu-item">
              {importing ? "Into" : "In"} {p.name}
            </MenuItem>
          ))}
        </Menu>
      </Popover>
    </MenuTrigger>
  );
}

function projectsNote(projects: Project[]): string {
  const n = projects.length;
  if (n === 0) return "Every record you can read. You aren’t in a project yet.";
  return `Every record you can read, of every type, in the ${n === 1 ? "project" : `${String(n)} projects`} you belong to.`;
}

/** Every record its reader can read, of every type, in every project they're in. */
export function AllEntities() {
  usePageTitle("All entities");
  const search = useSearch({ from: "/entities/" });
  const navigate = useNavigate({ from: "/entities/" });
  const types = useSuspenseQuery(typesQuery).data;
  const { projects } = useReadableProjects();
  return (
    <>
      <PageHeader
        title="All entities"
        crumbs={["Entities"]}
        description={projectsNote(projects)}
        actions={<NewIn type={types.find((t) => t.name === search.type)} label="New" />}
      />
      <EntityGrid
        types={types}
        search={search}
        change={(update, replace) => {
          void navigate({ search: update, ...(replace ? { replace } : {}) });
        }}
        pills
      />
    </>
  );
}

/** One type's table: its records across projects, its fields as columns and filters, and
 * the saved searches of it as views. */
export function TypeTable() {
  const { _splat: name = "" } = useParams({ from: "/entities/types/$" });
  const search = useSearch({ from: "/entities/types/$" });
  const navigate = useNavigate({ from: "/entities/types/$" });
  const types = useSuspenseQuery(typesQuery).data;
  const type = types.find((t) => t.name === name);
  usePageTitle(type ? type.label_plural : "Entity type");
  if (!type) {
    return (
      <>
        <PageHeader
          title="No such type"
          crumbs={[<Link to="/entities/">Entities</Link>, "No such type"]}
        />
        <p className="empty">
          There’s no entity type {name}. <Link to="/types/">See the installed types</Link>.
        </p>
      </>
    );
  }
  // The type is the page's: the address's other searches keep to it.
  const held = { ...search, type: undefined };
  return (
    <>
      <PageHeader
        title={type.label_plural}
        marker={<TypeDot type={type.name} large />}
        crumbs={[<Link to="/entities/">Entities</Link>, type.label_plural]}
        description={type.description}
        about={
          <Link to="/types/$/" params={{ _splat: type.name }}>
            About the {type.label} type: its fields and where it comes from
          </Link>
        }
        actions={
          <>
            <NewIn type={type} label="Import" importing />
            <NewIn type={type} label={`New ${inSentence(type.label)}`} />
          </>
        }
      />
      <ViewTabs type={type} search={held} />
      <EntityGrid
        types={types}
        search={held}
        change={(update, replace) => {
          void navigate({
            search: (prev: ListSearch) => ({ ...update(prev), type: undefined }),
            ...(replace ? { replace } : {}),
          });
        }}
        choices={[type]}
      />
    </>
  );
}

/** A saved search as the type table's address: its own, held to its project if it has
 * one. Null if the table can't show it. */
function viewAddress(saved: SavedSearch, projects: Project[]): ListSearch | null {
  const address = toAddress(saved.query);
  if (!address) return null;
  const rest: ListSearch = { ...address, type: undefined };
  delete rest.type;
  if (!saved.project) return rest;
  const project = projects.find((p) => p.id === saved.project);
  return project ? { ...rest, project: project.slug } : null;
}

/** The type table's views: all its records, and the saved searches of it its reader sees,
 * each a link to the table as it was saved. The one showing is current. */
function ViewTabs({ type, search }: { type: EntityType; search: ListSearch }) {
  const { projects } = useReadableProjects();
  const saved = useQuery(everySavedSearchQuery).data ?? [];
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState<SavedSearch | null>(null);
  const views = saved
    .filter((s) => s.query["type"] === type.name)
    .flatMap((s) => {
      const address = viewAddress(s, projects);
      return address ? [{ saved: s, address }] : [];
    });
  const here = Object.fromEntries(Object.entries(search).filter(([, v]) => v !== undefined));
  const current = views.find((v) => sameJson(v.address, here));
  const narrowed =
    Boolean(search.q) ||
    Boolean(search.filters?.length) ||
    Boolean(search.sort) ||
    Boolean(search.project);
  const chosen = projects.find((p) => p.slug === search.project);
  return (
    <nav className="view-tabs" aria-label="Views">
      <Link
        to="/entities/types/$/"
        params={{ _splat: type.name }}
        search={{}}
        className="view-tab"
        activeOptions={{ exact: true, includeSearch: true }}
      >
        All {inSentence(type.label_plural)}
      </Link>
      {views.map(({ saved: s, address }) => (
        <span key={s.id} className="view-tab-item">
          <Link
            to="/entities/types/$/"
            params={{ _splat: type.name }}
            search={address}
            className="view-tab"
            activeOptions={{ exact: true, includeSearch: true }}
          >
            {s.name}
            {s.shared ? <span className="view-tab-meta"> · shared</span> : null}
          </Link>
          {s.can.delete ? (
            <button
              type="button"
              className="view-tab-remove"
              aria-label={`Remove view ${s.name}`}
              onClick={() => {
                setRemoving(s);
              }}
            >
              <span aria-hidden="true">×</span>
            </button>
          ) : null}
        </span>
      ))}
      {narrowed && !current ? (
        <button
          type="button"
          className="view-tab view-tab-add"
          onClick={() => {
            setSaving(true);
          }}
        >
          <span aria-hidden="true">+</span>Save view
        </button>
      ) : null}
      <SaveSearchDialog
        isOpen={saving}
        title="Save this view"
        project={chosen}
        search={{ ...search, type: type.name }}
        onClose={() => {
          setSaving(false);
        }}
      />
      <RemoveSearchDialog
        removing={removing}
        project={projects.find((p) => p.id === removing?.project)}
        onClose={() => {
          setRemoving(null);
        }}
      />
    </nav>
  );
}

const REQUESTS = { one: "request", many: "requests" };

/** Every request its reader can read (ADR 0132): those their projects sent, and those sent
 * to their projects as services. */
export function AllRequests() {
  usePageTitle("Requests");
  const search = useSearch({ from: "/requests" });
  const navigate = useNavigate({ from: "/requests" });
  const types = useSuspenseQuery(typesQuery).data;
  const { projects } = useReadableProjects();
  const kinds = requestTypes(types);
  const chosen = kinds.find((t) => t.name === search.type);
  return (
    <>
      <PageHeader
        title="Requests"
        description="Every request you can read: those your projects sent to services, and those sent to your projects as a service."
      />
      {kinds.length === 0 ? (
        <p className="empty">
          No plugin here makes requests between projects: a request is a record of a request type,
          such as a core facility’s assay request.
        </p>
      ) : (
        <EntityGrid
          types={types}
          search={search}
          change={(update, replace) => {
            void navigate({ search: update, ...(replace ? { replace } : {}) });
          }}
          // Without a type picked, the request types', and none of the rest.
          fixed={
            chosen
              ? []
              : [{ field: "type", op: "in", value: kinds.flatMap((t) => [t.name, ...t.subtypes]) }]
          }
          choices={kinds}
          noun={REQUESTS}
          extra={[askedByField(projects)]}
          sides={{ always: true }}
          empty={(narrowed) =>
            narrowed ? "No requests you can read match." : "No requests you can read yet."
          }
        />
      )}
    </>
  );
}
