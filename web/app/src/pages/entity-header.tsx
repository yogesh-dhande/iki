/**
 * A record's header (ADR 0176), on every tab of its page: where it is (Entities, its
 * type, its ID), what it's called and the state it's in, what can be done with it, and
 * its tabs. Actions people use often are buttons (Watch, Run action, Edit); the rest,
 * such as exporting and archiving, are in its ⋯ menu.
 */

import { ApiError, unwrap } from "@ikisci/sdk";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useRouterState } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import {
  Button as AriaButton,
  Menu,
  MenuItem,
  MenuTrigger,
  Popover,
  Separator,
} from "react-aria-components";

import { iki, problemOf, useApiMutation } from "../api";
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
import { lineageGraphQuery } from "../lineage";
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

/** The records it's directly linked to, either way: what its Lineage tab counts, and its
 * page's rail lists. One step each way, as the graph has them (ADR 0123). */
export function neighboursQuery(entity: Entity) {
  return lineageGraphQuery(apiRef(entity), 1, 1, []);
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
  const types = useQuery(typesQuery).data ?? [];
  // Plugins' tabs (ADR 0084). If they can't be listed, the page still works without them.
  const plugins = entityTabs(entity.type, types, useQuery(componentsQuery).data ?? []);
  // Apps about it (ADR 0090), likewise.
  const apps = useQuery(entityAppsQuery(apiRef(entity))).data;
  // On a phone its tabs scroll across: the one you're on is kept in view.
  const nav = useRef<HTMLElement>(null);
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  useEffect(() => {
    const current = nav.current?.querySelector<HTMLElement>('[aria-current="page"]');
    if (current && nav.current && nav.current.scrollWidth > nav.current.clientWidth) {
      current.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }, [pathname]);
  const neighbours = useQuery(neighboursQuery(entity)).data;
  const related = neighbours?.entities.filter((e) => e.at !== 0).length;
  // The project's record, on every tab: other projects' records have its HRID too.
  const search = recordSearch(entity);
  const trail = trailCount(entity);
  return (
    <>
      <div className="record-top">
        <nav aria-label="Breadcrumb" className="crumbs">
          <ol>
            {/* The lists of everything the reader can read, across projects (ADR 0176). */}
            <li>
              <Link to="/entities/">Entities</Link>
            </li>
            <li>
              <Link to="/entities/types/$/" params={{ _splat: entity.type }} search={{}}>
                {type?.label_plural ?? type?.label ?? entity.type}
              </Link>
            </li>
            <li>
              <span aria-current="page">{entity.hrid}</span>
            </li>
          </ol>
        </nav>
        <RecordActions entity={entity} type={type} />
      </div>
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
      <nav className="tabs record-tabs" aria-label="Record" ref={nav}>
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
          <TabCount n={trail} what="revisions" />
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
      </nav>
    </>
  );
}

/** A tab's count, said in words to screen readers: “Audit trail, 5 revisions”. */
function TabCount({ n, what }: { n: number; what: string }) {
  return (
    <span className="tab-count">
      <span className="visually-hidden">, </span>
      {n.toLocaleString()}
      <span className="visually-hidden"> {what}</span>
    </span>
  );
}

/** What state it's in: its states, locked, signed, archived, superseded. */
function StatusPills({ entity, type }: { entity: Entity; type: EntityType | undefined }) {
  const signatures = useQuery(signaturesQuery(apiRef(entity), false)).data?.items ?? [];
  const signed = signatures.some((s) => s.status === "carried");
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
  const exports = exportFormats(type).length > 0;
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
      {editable && !entity.archived ? (
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
  const archiving = !entity.archived;
  const change = useApiMutation(
    async () =>
      unwrap(
        await iki.PATCH("/api/v1/entities/{ref}", {
          params: {
            path: { ref: entity.id },
            header: { "If-Match": `"${String(entity.revision)}"` },
          },
          body: { archived: archiving, reason: reason.trim() },
        }),
      ),
    [ENTITIES],
  );
  const close = () => {
    onClose();
    setReason("");
    change.reset();
  };
  const stale = change.error instanceof ApiError && change.error.problem.code === "stale";
  return (
    <ConfirmDialog
      isOpen={isOpen}
      title={`${archiving ? "Archive" : "Restore"} ${entity.hrid}?`}
      confirmLabel={archiving ? "Archive" : "Restore"}
      confirmVariant={archiving ? "danger" : "primary"}
      busy={change.isPending}
      problem={
        stale && change.error
          ? {
              ...change.error.problem,
              detail:
                "Someone changed it while this was open. The page now shows their change: check it, then try again.",
            }
          : change.error
            ? problemOf(change.error)
            : undefined
      }
      onCancel={close}
      onConfirm={() => {
        change.mutate(undefined, {
          onSuccess: (saved) => {
            client.setQueryData(entityQuery(saved.hrid).queryKey, saved);
            close();
            say(archiving ? `Archived ${saved.hrid}.` : `Restored ${saved.hrid}.`);
          },
          onError: (error) => {
            // Read it again, so trying again sends the revision it's at now.
            if (error.problem.code === "stale") {
              void client.invalidateQueries({ queryKey: ENTITIES });
            }
          },
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
