/**
 * Saved views on a viewer's tab (ADR 0086, ADR 0110): what the viewer shows now, saved as a
 * view of this revision, and every view saved before that shows this entity in this viewer,
 * alone or with others. A view is an entity of its own (iki/core.View): its page opens it on the
 * revisions it showed. Saving one can add other entities to it, each at its latest revision:
 * structures are drawn in one scene with this one, anything else beside it.
 */

import { unwrap } from "@ikisci/sdk";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useId, useRef, useState, type RefObject } from "react";

import { iki, problemOf, useApiMutation } from "../api";
import { FieldInput } from "../entity-inputs";
import {
  ENTITIES,
  entityQuery,
  typesQuery,
  type Entity,
  type EntityType,
  type FieldDef,
} from "../entities";
import type { Picked } from "../entity-values";
import { Button, ConfirmDialog, TextField, Time, useMessages } from "../ui";
import {
  listed,
  MAX_TARGETS,
  savedViewsQuery,
  VIEW_TYPE,
  type Capture,
  type SavedView,
  type TargetState,
  type ViewerName,
} from "../views";
import { NO_RULES } from "../entity-values";

type AnyViewer = EntityType["viewers"][number];

export function SavedViews({
  entity,
  viewer,
  capture,
}: {
  entity: Entity;
  viewer: AnyViewer;
  capture: RefObject<Capture | null>;
}) {
  const views = useInfiniteQuery(savedViewsQuery(entity, viewer.viewer));
  const saved = views.data?.pages.flatMap((p) => p.views) ?? [];
  const here = entity.owner_project?.id;
  return (
    <section className="saved-views" aria-labelledby="saved-views-title">
      <div className="section-header">
        <h2 id="saved-views-title" className="section-title">
          Saved views
        </h2>
        {entity.can.edit ? <SaveView entity={entity} viewer={viewer} capture={capture} /> : null}
      </div>
      {saved.length ? (
        <>
          <ul className="saved-view-list">
            {saved.map((one) => (
              <li key={one.view.id}>
                <Link to="/entities/$ref/" params={{ ref: one.view.hrid }}>
                  {one.view.title}
                </Link>{" "}
                <span className="muted">
                  revision {String(one.targets[one.index]?.revision ?? "")}
                  {/* A registry entry's view pins its own project's record of it (ADR 0115). */}
                  {one.view.owner_project && one.view.owner_project.id !== here
                    ? ` in ${one.view.owner_project.name}`
                    : ""}
                  {one.targets.length > 1 ? ` · ${withOthers(one)}` : ""}
                  {one.view.created_by ? ` · ${one.view.created_by.name}` : ""} ·{" "}
                  <Time value={one.view.created_at} />
                </span>
              </li>
            ))}
          </ul>
          {views.hasNextPage ? (
            <p>
              <Button
                busy={views.isFetchingNextPage}
                busyText="Loading…"
                onClick={() => void views.fetchNextPage()}
              >
                Show older
              </Button>
            </p>
          ) : null}
        </>
      ) : views.isSuccess ? (
        <p className="muted">
          None yet. A saved view keeps what the viewer shows, on this revision, for figures and
          reports.
        </p>
      ) : null}
    </section>
  );
}

/** What else a view shows: "with PST-000002" or "with PST-000002 and 3 others". */
function withOthers({ view, targets, index }: SavedView): string {
  const others = targets.filter((_, i) => i !== index);
  const named = others.slice(0, 2).map((t) => view.refs[t.id]?.hrid ?? "an entity");
  const rest = others.length - named.length;
  return `with ${listed(rest ? [...named, `${String(rest)} other${rest === 1 ? "" : "s"}`] : named)}`;
}

/** Another entity a view being saved will show, at its latest revision. */
interface Added {
  entity: Entity;
  viewers: AnyViewer[];
  viewer: ViewerName;
  label: string;
}

/** The picker the app uses for references, for any entity. */
const ADD_FIELD: FieldDef = {
  role: null,
  role_label: null,
  assignment: false,
  assignment_field: null,
  assignment_states: [],
  name: "add",
  label: "Add another entity",
  description:
    "It’s shown at its latest revision beside this one. Structures are drawn in one scene, " +
    "where they can be superposed.",
  type: "ref",
  items: null,
  to: ["iki/core.Entity"],
  plugin: "",
  required: false,
  immutable: false,
  computed: false,
  unique: false,
  detached: false,
  link: null,
  unit: null,
  unit_symbol: null,
  display_unit: null,
  display_unit_symbol: null,
  display_scale: null,
  display_offset: null,
  input_units: [],
  values: [],
  initial: null,
  states: [],
};

function SaveView({
  entity,
  viewer,
  capture,
}: {
  entity: Entity;
  viewer: AnyViewer;
  capture: RefObject<Capture | null>;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [label, setLabel] = useState("");
  const [added, setAdded] = useState<Added[]>([]);
  const [picking, setPicking] = useState<Picked | null>(null);
  const [pickError, setPickError] = useState<string>();
  const [justAdded, setJustAdded] = useState<number | null>(null);
  const list = useRef<HTMLOListElement>(null);
  // The one just added has a label to type, next: focus moves to it, not back to the top.
  useEffect(() => {
    if (justAdded === null) return;
    list.current?.children[justAdded + 1]?.querySelector("input")?.focus();
  }, [justAdded]);
  const types = useQuery(typesQuery);
  const client = useQueryClient();
  const navigate = useNavigate();
  const { say } = useMessages();
  const ids = useId();
  const save = useApiMutation(async () => {
    const now = capture.current ? await capture.current() : { targets: [{}] };
    const mine: TargetState = {
      ...now.targets[0],
      ...(label.trim() ? { label: label.trim() } : {}),
    };
    const others = added.map((a): TargetState => (a.label.trim() ? { label: a.label.trim() } : {}));
    return unwrap(
      await iki.POST("/api/v1/entities", {
        body: {
          type: VIEW_TYPE,
          project: entity.owner_project?.id ?? "",
          values: {
            name: name.trim(),
            ...(description.trim() ? { description: description.trim() } : {}),
            targets: [entity.id, ...added.map((a) => a.entity.id)],
            target_revisions: [entity.revision, ...added.map((a) => a.entity.revision)],
            viewers: [viewer.viewer, ...added.map((a) => a.viewer)],
            state: {
              version: 2,
              ...(now.camera ? { camera: now.camera } : {}),
              targets: [mine, ...others],
            },
          },
          reason: "",
        },
      }),
    );
  }, [ENTITIES, ["views"]]);
  const close = () => {
    setOpen(false);
    setName("");
    setDescription("");
    setLabel("");
    setAdded([]);
    setPicking(null);
    setPickError(undefined);
    setJustAdded(null);
    save.reset();
  };
  async function add(picked: Picked | null) {
    setPicking(picked);
    setPickError(undefined);
    if (!picked) return;
    try {
      // By its ID: other projects' records of a registry entry have its HRID too.
      const found = await client.query(entityQuery(picked.id));
      const type = (types.data ?? []).find((t) => t.name === found.type);
      const viewers = type?.viewers ?? [];
      const choice = viewers.find((v) => v.viewer === viewer.viewer) ?? viewers[0];
      const taken = (v: ViewerName) =>
        (found.id === entity.id && v === viewer.viewer) ||
        added.some((a) => a.entity.id === found.id && a.viewer === v);
      const free = viewers.find((v) => !taken(v.viewer));
      if (!choice) {
        setPickError(`${found.hrid}’s type has no viewer, so a view can’t show it.`);
        return;
      }
      if (!free) {
        setPickError(`The view shows ${found.hrid} already.`);
        return;
      }
      setAdded((now) => [
        ...now,
        {
          entity: found,
          viewers,
          viewer: taken(choice.viewer) ? free.viewer : choice.viewer,
          label: "",
        },
      ]);
      // A new picker for the next one, and the one just added's label to type in.
      setPicking(null);
      setJustAdded(added.length);
    } catch (error) {
      setPickError(problemOf(error).detail);
    }
  }
  const full = added.length + 1 >= MAX_TARGETS;
  return (
    <>
      <Button
        onClick={() => {
          setName(`${viewer.label} of ${entity.title}`);
          setOpen(true);
        }}
      >
        Save view
      </Button>
      <ConfirmDialog
        isOpen={open}
        title="Save this view"
        confirmLabel="Save view"
        confirmVariant="primary"
        busy={save.isPending}
        problem={save.error ? problemOf(save.error) : undefined}
        onCancel={close}
        onConfirm={() => {
          save.mutate(undefined, {
            onSuccess: (made) => {
              close();
              void navigate({ to: "/entities/$ref/", params: { ref: made.hrid } }).then(() => {
                say(`Saved the view ${made.hrid}.`);
              });
            },
          });
        }}
      >
        <p>
          It keeps what the {viewer.label.toLowerCase()} viewer shows now, on revision{" "}
          {entity.revision} of {entity.hrid}, and opens on that revision whatever changes later.
        </p>
        <TextField label="Name" value={name} onChange={setName} maxLength={200} />
        <TextField
          label="Description"
          value={description}
          onChange={setDescription}
          maxLength={10000}
          optional
          multiline
        />
        <fieldset className="field view-targets">
          <legend>What it shows</legend>
          <ol className="view-target-list" ref={list}>
            <li>
              <span>
                <strong>{entity.title}</strong> <span className="muted">{entity.hrid}</span> in the{" "}
                {viewer.label.toLowerCase()} viewer, revision {entity.revision}
              </span>
              <TextField
                label={`Label for ${entity.hrid}`}
                value={label}
                onChange={setLabel}
                maxLength={100}
                optional
              />
            </li>
            {added.map((a, index) => (
              <li key={`${a.entity.id} ${a.viewer}`}>
                <span>
                  <strong>{a.entity.title}</strong> <span className="muted">{a.entity.hrid}</span>{" "}
                  at its latest revision, {a.entity.revision}
                </span>
                {a.viewers.length > 1 ? (
                  <div className="field">
                    <label htmlFor={`${ids}-viewer-${String(index)}`}>
                      Viewer for {a.entity.hrid}
                    </label>
                    <select
                      id={`${ids}-viewer-${String(index)}`}
                      value={a.viewer}
                      onChange={(event) => {
                        const chosen = event.target.value as ViewerName;
                        setAdded((now) =>
                          now.map((x, i) => (i === index ? { ...x, viewer: chosen } : x)),
                        );
                      }}
                    >
                      {a.viewers.map((v) => (
                        <option key={v.viewer} value={v.viewer}>
                          {v.label}
                        </option>
                      ))}
                    </select>
                  </div>
                ) : (
                  <span className="muted"> in the {a.viewers[0]?.label.toLowerCase()} viewer</span>
                )}
                <TextField
                  label={`Label for ${a.entity.hrid}`}
                  value={a.label}
                  onChange={(value) => {
                    setAdded((now) =>
                      now.map((x, i) => (i === index ? { ...x, label: value } : x)),
                    );
                  }}
                  maxLength={100}
                  optional
                />
                <Button
                  variant="quiet"
                  onClick={() => {
                    setAdded((now) => now.filter((_, i) => i !== index));
                    setJustAdded(null);
                  }}
                >
                  Remove {a.entity.hrid}
                </Button>
              </li>
            ))}
          </ol>
          {full ? (
            <p className="field-help">A view shows at most {MAX_TARGETS} entities.</p>
          ) : (
            <FieldInput
              key={added.length}
              field={ADD_FIELD}
              type={NO_RULES}
              types={types.data ?? []}
              draft={picking}
              onChange={(draft) => {
                void add(draft as Picked | null);
              }}
              error={pickError}
              projectId={entity.owner_project?.id ?? ""}
            />
          )}
        </fieldset>
      </ConfirmDialog>
    </>
  );
}
