/**
 * Saved views on a viewer's tab (ADR 0086): what the viewer shows now, saved as a view of
 * this revision, and the views saved before. A view is an entity of its own (core.View):
 * its page opens it on the revision it showed.
 */

import { unwrap } from "@ikisci/sdk";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useState, type RefObject } from "react";

import { iki, problemOf, useApiMutation } from "../api";
import { ENTITIES, type Entity, type EntityType } from "../entities";
import { Button, ConfirmDialog, TextField, Time, useMessages } from "../ui";
import { savedViewsQuery, VIEW_TYPE, type Capture } from "../views";

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
  const views = useQuery(savedViewsQuery(entity, viewer.viewer));
  return (
    <section className="saved-views" aria-labelledby="saved-views-title">
      <div className="section-header">
        <h2 id="saved-views-title" className="section-title">
          Saved views
        </h2>
        {entity.can.edit ? <SaveView entity={entity} viewer={viewer} capture={capture} /> : null}
      </div>
      {views.data?.length ? (
        <ul className="saved-view-list">
          {views.data.map((view) => (
            <li key={view.id}>
              <Link to="/entities/$ref/" params={{ ref: view.hrid }}>
                {view.title}
              </Link>{" "}
              <span className="muted">
                revision {String(view.values["target_revision"])}
                {view.created_by ? ` · ${view.created_by.name}` : ""} ·{" "}
                <Time value={view.created_at} />
              </span>
            </li>
          ))}
        </ul>
      ) : views.isSuccess ? (
        <p className="muted">
          None yet. A saved view keeps what the viewer shows, on this revision, for figures and
          reports.
        </p>
      ) : null}
    </section>
  );
}

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
  const navigate = useNavigate();
  const { say } = useMessages();
  const save = useApiMutation(async () => {
    const state = capture.current ? await capture.current() : { version: 1 };
    return unwrap(
      await iki.POST("/api/v1/entities", {
        body: {
          type: VIEW_TYPE,
          project: entity.owner_project?.id ?? "",
          values: {
            name: name.trim(),
            ...(description.trim() ? { description: description.trim() } : {}),
            target: entity.id,
            target_revision: entity.revision,
            viewer: viewer.viewer,
            state,
          },
          reason: "",
          allow_duplicate: false,
        },
      }),
    );
  }, [ENTITIES, ["views", entity.id]]);
  const close = () => {
    setOpen(false);
    setName("");
    setDescription("");
    save.reset();
  };
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
      </ConfirmDialog>
    </>
  );
}
