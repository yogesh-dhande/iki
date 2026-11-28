/**
 * A record's header (ADR 0176), on every tab of its page: where it is (Entities, its
 * type, its ID), what it's called and the state it's in, what can be done with it, and
 * its tabs. Actions people use often are buttons (Watch, Run action, Edit); the rest,
 * such as exporting and archiving, are in its ⋯ menu.
 */

import { unwrap, isStale } from "@ikisci/sdk";
import { useQuery, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { Link, useParams, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import {
  Button as AriaButton,
  Menu,
  MenuItem,
  MenuTrigger,
  Popover,
  Separator,
} from "react-aria-components";

import { iki, problemOf, useApiMutation } from "../api";
import { useFoundTabs } from "../component-entities";
import { componentsQuery, entityTabs } from "../components";
import {
  apiRef,
  ENTITIES,
  entityQuery,
  recordSearch,
  typesQuery,
  type Entity,
  type EntityType,
} from "../entities";
import { entityAppsQuery } from "../apps";
import { lineageGraphQuery, type LineageEntity, type LineageGraph } from "../lineage";
import { PhoneActions, TopBar, usePhone } from "../page-top";
import { changedProblem, useRevisioned, type IfMatch } from "../revisioned";
import { TabStrip } from "../tab-strip";
import { ExportDialog, exportFormats } from "./entity-export";
import { signaturesQuery } from "./entity-signatures";
import { StateBadges } from "./entity-state";
import { EntityWatch } from "./entity-watch";
import {
  Badge,
  buttonClass,
  ConfirmDialog,
  PageTitle,
  RelativeTime,
  TextField,
  useMessages,
} from "../ui";

/** The entity the page is about, from the `$ref` in its address, and `?project=` for a
 * record of a registry entry. */
export function useEntity(): { entity: Entity; type: EntityType | undefined; types: EntityType[] } {
  const { ref } = useParams({ from: "/entities/$ref" });
  const { project } = useSearch({ from: "/entities/$ref" });
  const entity = useSuspenseQuery(entityQuery(ref, project)).data;
  const types = useSuspenseQuery(typesQuery).data;
  return { entity, type: types.find((t) => t.name === entity.type), types };
}

/** The records it's directly linked to, either way: what its Lineage tab counts, and its
 * page's rail lists. One step each way, as the graph has them (ADR 0123). */
export function neighboursQuery(entity: Entity) {
  return lineageGraphQuery(apiRef(entity), 1, 1, []);
}

/** A record it's directly linked to, as its rail lists it and its Lineage tab counts it. */
export interface Neighbour {
  node: LineageEntity;
  /** The reference fields that link the two, by label: "Sequence". */
  via: string[];
}

/**
 * The records it's directly linked to, each once. A registry entry and a project's record
 * of it are one thing (ADR 0115), and the graph has both, under one HRID: they're one
 * here, as the record of its own project where it has one. Each says which of the
 * reference fields between the two link them.
 */
export function neighboursOf(
  graph: LineageGraph,
  entity: Pick<Entity, "type" | "owner_project">,
  types: EntityType[],
): Neighbour[] {
  const fieldLabel = (type: string, name: string) =>
    types.find((t) => t.name === type)?.fields.find((f) => f.name === name)?.label ?? name;
  const byId = new Map(graph.entities.map((e) => [e.id, e]));
  const groups = new Map<string, LineageEntity[]>();
  for (const e of graph.entities) {
    if (e.at === 0) continue;
    // Upstream and downstream are told apart: a record can be both.
    const key = `${e.hrid}|${String(Math.sign(e.at))}`;
    groups.set(key, [...(groups.get(key) ?? []), e]);
  }
  const slug = entity.owner_project?.slug;
  return [...groups.values()].map((group) => {
    const rank = (e: LineageEntity) =>
      (e.entry ? 2 : 0) + (slug && e.project?.slug === slug ? 0 : 1);
    const node = group.reduce((best, e) => (rank(e) < rank(best) ? e : best));
    const ids = new Set(group.map((e) => e.id));
    const via = new Set<string>();
    for (const edge of graph.edges) {
      if (edge.kind !== "reference" || !edge.field) continue;
      const from = byId.get(edge.node);
      if (edge.node === graph.root && ids.has(edge.on))
        via.add(fieldLabel(entity.type, edge.field));
      else if (from && ids.has(edge.node) && edge.on === graph.root) {
        via.add(fieldLabel(from.type, edge.field));
      }
    }
    return { node, via: [...via] };
  });
}

/** How many revisions its audit trail has: its own, and its registry entry's. */
export function trailCount(entity: Entity): number {
  return entity.revision + (entity.entry?.revision ?? 0);
}

/** The address of its audit trail as CSV, for inspection. */
export function trailCsvUrl(entity: Entity): string {
  return `/api/v1/entities/${encodeURIComponent(entity.id)}/revisions.csv`;
}

export function EntityTabs({ entity, type }: { entity: Entity; type: EntityType | undefined }) {
  const tab = { "aria-current": "page" } as const;
  // What decides which tabs there are and how wide each is. Until each has answered, or
  // failed, the tabs aren't drawn: one pressed as they arrived would move under the finger.
  const read = {
    // The type's viewers, and which plugins' components are about it.
    types: useQuery(typesQuery),
    // Plugins' tabs (ADR 0084). If they can't be listed, the page still works without them.
    components: useQuery(componentsQuery),
    // Apps about it (ADR 0090), likewise.
    apps: useQuery(entityAppsQuery(apiRef(entity))),
    // What its Lineage tab counts.
    graph: useQuery(neighboursQuery(entity)),
  };
  const types = read.types.data ?? [];
  // A tab on every record shows only where its search finds something for this one.
  const found = useFoundTabs(entity.id, entityTabs(entity.type, types, read.components.data ?? []));
  const plugins = found.tabs;
  const settled = Object.values(read).every((q) => !q.isLoading) && found.settled;
  const apps = read.apps.data;
  const graph = read.graph.data;
  const related = graph ? neighboursOf(graph, entity, types).length : undefined;
  // The project's record, on every tab: other projects' records have its HRID too.
  const search = recordSearch(entity);
  const trail = trailCount(entity);
  const phone = usePhone();
  const actions = <RecordActions entity={entity} type={type} />;
  return (
    <>
      {/* Where it is, in the lists of everything the reader can read, across projects, and
          what can be done with it: in the bar across the top of the page (ADR 0176). On a
          phone its title comes first, then its actions, under it. */}
      <TopBar
        crumbs={[
          <Link to="/entities/">Entities</Link>,
          <Link to="/entities/types/$/" params={{ _splat: entity.type }} search={{}}>
            {type?.label_plural ?? type?.label ?? entity.type}
          </Link>,
          entity.hrid,
        ]}
        actions={phone ? undefined : actions}
      />
      <div className="record-heading">
        <div className="record-title">
          <PageTitle>{entity.title}</PageTitle>
          {entity.title !== entity.hrid ? <code className="id-chip">{entity.hrid}</code> : null}
          <StatusPills entity={entity} type={type} />
        </div>
        <p className="record-meta">
          {type ? (
            <Link to="/types/$/" params={{ _splat: type.name }}>
              {type.label}
            </Link>
          ) : (
            <code>{entity.type}</code>
          )}
          {entity.owner_project ? (
            <>
              {" · "}
              <Link to="/projects/$slug/" params={{ slug: entity.owner_project.slug }}>
                {entity.owner_project.name}
              </Link>
            </>
          ) : null}
          {" · changed "}
          <RelativeTime value={entity.updated_at} />
        </p>
      </div>
      {phone ? <PhoneActions>{actions}</PhoneActions> : null}
      <TabStrip
        label="Record"
        className="record-tabs"
        settled={settled}
        sure={["Overview", "Lineage", "Audit trail"]}
        widths={`${String(related)} ${String(trail)}`}
      >
        <Link
          to="/entities/$ref/"
          params={{ ref: entity.hrid }}
          search={search}
          activeOptions={{ exact: true, includeSearch: false }}
          activeProps={tab}
        >
          Overview
        </Link>
        {(type?.viewers ?? []).map((v) => (
          <Link
            key={v.viewer}
            to="/entities/$ref/viewers/$viewer/"
            params={{ ref: entity.hrid, viewer: v.viewer }}
            search={search}
            activeOptions={{ includeSearch: false }}
            activeProps={tab}
          >
            {v.label}
          </Link>
        ))}
        <Link
          to="/entities/$ref/lineage/"
          params={{ ref: entity.hrid }}
          search={search}
          activeOptions={{ includeSearch: false }}
          activeProps={tab}
        >
          Lineage
          {related === undefined ? null : <TabCount n={related} what="linked records" />}
        </Link>
        <Link
          to="/entities/$ref/audit/"
          params={{ ref: entity.hrid }}
          search={search}
          activeOptions={{ includeSearch: false }}
          activeProps={tab}
        >
          Audit trail
          <TabCount
            n={trail}
            what={
              entity.entry
                ? `revisions: ${String(entity.revision)} of this record, ${String(entity.entry.revision)} of its entry`
                : "revisions"
            }
          />
        </Link>
        {plugins.map(({ component, label }) => (
          <Link
            key={component.name}
            to="/entities/$ref/components/$/"
            params={{ ref: entity.hrid, _splat: component.name }}
            search={search}
            activeOptions={{ includeSearch: false }}
            activeProps={tab}
          >
            {label}
          </Link>
        ))}
        {(apps ?? []).map((app) => (
          <Link
            key={app.hrid}
            to="/entities/$ref/apps/$app/"
            params={{ ref: entity.hrid, app: app.hrid }}
            search={search}
            activeOptions={{ includeSearch: false }}
            activeProps={tab}
          >
            {/* Two apps of one name are told apart by their IDs. */}
            {(apps ?? []).filter((a) => a.name === app.name).length > 1
              ? `${app.name} (${app.hrid})`
              : app.name}
          </Link>
        ))}
      </TabStrip>
    </>
  );
}

/** A tab's count, said in words to screen readers, and on hovering it: “Audit trail, 5
 * revisions: 3 of this record, 2 of its entry”. */
function TabCount({ n, what }: { n: number; what: string }) {
  return (
    <span className="tab-count" title={`${n.toLocaleString()} ${what}`}>
      <span className="visually-hidden">, </span>
      {n.toLocaleString()}
      <span className="visually-hidden"> {what}</span>
    </span>
  );
}

/** What state it's in: its states, locked, signed, archived, superseded. */
function StatusPills({ entity, type }: { entity: Entity; type: EntityType | undefined }) {
  const signatures = useQuery(signaturesQuery(apiRef(entity), false)).data?.signatures ?? [];
  const signed = signatures.some(
    (s) =>
      s.status === "carried" &&
      s.carried_by === entity.revision &&
      s.entry_revision === (entity.entry?.revision ?? null),
  );
  return (
    <span className="record-pills">
      <StateBadges entity={entity} type={type} />
      {signed ? <Badge>Signed</Badge> : null}
      {entity.archived ? <Badge muted>Archived</Badge> : null}
      {entity.entry?.superseded ? <Badge muted>Superseded</Badge> : null}
      {type?.detached ? <Badge muted>Detached plugin</Badge> : null}
    </span>
  );
}

/** Watch, Run action and Edit, then the ⋯ menu: export, its audit trail, and archive or
 * restore for Editors in its owner project. */
function RecordActions({ entity, type }: { entity: Entity; type: EntityType | undefined }) {
  const [archiving, setArchiving] = useState(false);
  const [exporting, setExporting] = useState(false);
  const editable = entity.can.edit && type !== undefined;
  // Anyone who reads it may download it (ADR 0171).
  const exports = exportFormats(type, entity.values).length > 0;
  return (
    <div className="actions record-actions">
      <EntityWatch entity={entity} />
      {!entity.archived && entity.owner_project ? (
        <Link
          to="/projects/$slug/runs/new/"
          params={{ slug: entity.owner_project.slug }}
          search={{ entities: entity.hrid }}
          className={buttonClass()}
        >
          Run action
        </Link>
      ) : null}
      {(editable || (type && entity.can.correct)) && !entity.archived ? (
        <Link
          to="/entities/$ref/edit/"
          params={{ ref: entity.hrid }}
          search={recordSearch(entity)}
          className={buttonClass("primary")}
        >
          Edit
        </Link>
      ) : null}
      <MenuTrigger>
        <AriaButton className="button button-icon" aria-label={`More actions for ${entity.hrid}`}>
          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <circle cx="5" cy="12" r="1.6" />
            <circle cx="12" cy="12" r="1.6" />
            <circle cx="19" cy="12" r="1.6" />
          </svg>
        </AriaButton>
        <Popover className="menu-popover" placement="bottom end">
          <Menu
            className="menu"
            aria-label={`More actions for ${entity.hrid}`}
            onAction={(key) => {
              if (key === "export") setExporting(true);
              if (key === "archive") setArchiving(true);
            }}
          >
            {exports ? (
              <MenuItem id="export" className="menu-item">
                Export…
              </MenuItem>
            ) : null}
            <MenuItem id="trail" className="menu-item" href={trailCsvUrl(entity)} download>
              Download audit trail (CSV)
            </MenuItem>
            {editable ? (
              <>
                <Separator className="menu-separator" />
                <MenuItem
                  id="archive"
                  className={entity.archived ? "menu-item" : "menu-item menu-item-danger"}
                >
                  {entity.archived ? "Restore…" : "Archive…"}
                </MenuItem>
              </>
            ) : null}
          </Menu>
        </Popover>
      </MenuTrigger>
      {exports ? (
        <ExportDialog
          entity={entity}
          type={type}
          isOpen={exporting}
          onClose={() => {
            setExporting(false);
          }}
        />
      ) : null}
      {editable ? (
        <ArchiveDialog
          entity={entity}
          isOpen={archiving}
          onClose={() => {
            setArchiving(false);
          }}
        />
      ) : null}
    </div>
  );
}

/** Archiving it, or restoring it, with a reason for its history. */
function ArchiveDialog({
  entity,
  isOpen,
  onClose,
}: {
  entity: Entity;
  isOpen: boolean;
  onClose: () => void;
}) {
  const [reason, setReason] = useState("");
  const { say } = useMessages();
  const client = useQueryClient();
  const change = useApiMutation(
    async ({ header, archived }: { header: IfMatch; archived: boolean }) =>
      unwrap(
        await iki.PATCH("/api/v1/entities/{ref}", {
          params: { path: { ref: entity.id }, header },
          body: { archived, reason: reason.trim() },
        }),
      ),
    [ENTITIES],
  );
  const close = () => {
    onClose();
    setReason("");
    change.reset();
  };
  // From the revision it was opened at, which the person reviewed.
  const rev = useRevisioned(entity, { open: isOpen, error: change.error, refresh: ENTITIES });
  const archiving = !rev.read.archived;
  return (
    <ConfirmDialog
      isOpen={isOpen}
      title={`${archiving ? "Archive" : "Restore"} ${entity.hrid}?`}
      confirmLabel={archiving ? "Archive" : "Restore"}
      confirmVariant={archiving ? "danger" : "primary"}
      busy={change.isPending || rev.rereading}
      problem={
        rev.changed
          ? changedProblem(
              "Someone changed it while this was open. The page now shows their change: check it, then try again.",
            )
          : change.error && !isStale(change.error)
            ? problemOf(change.error)
            : undefined
      }
      onCancel={close}
      onConfirm={() => {
        rev.submit((header) => {
          change.mutate(
            { header, archived: archiving },
            {
              onSuccess: (saved) => {
                client.setQueryData(entityQuery(saved.hrid).queryKey, saved);
                close();
                say(archiving ? `Archived ${saved.hrid}.` : `Restored ${saved.hrid}.`);
              },
            },
          );
        });
      }}
    >
      <p>
        {archiving
          ? "It stays in every project it’s in, with its values and history, but it can’t change and lists hide it. You can restore it later."
          : "It can change again, and lists show it again."}
      </p>
      <TextField
        label="Reason"
        value={reason}
        onChange={setReason}
        maxLength={2000}
        optional
        multiline
        help="Why, for the record. It’s kept in its history."
      />
    </ConfirmDialog>
  );
}
