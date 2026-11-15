/**
 * A saved view's page (core.View, ADR 0086): the viewer, showing its entity as it was at
 * the revision the view pinned, as the view saved it. A switch shows the latest revision
 * instead. Saving is explicit, and each save is a revision of the view: what the viewer
 * shows now, on the revision it shows.
 */

import { unwrap } from "@ikisci/sdk";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useCallback, useId, useRef, useState } from "react";

import { iki, problemOf, useApiMutation } from "../api";
import { ENTITIES, entityQuery, revisionQuery, type Entity, type EntityType } from "../entities";
import { Button, Loading, ProblemAlert, useMessages } from "../ui";
import { readViewState, type Capture } from "../views";
import { ViewerBody, type Source } from "./entity-viewer";

export function SavedViewPanel({ view, types }: { view: Entity; types: EntityType[] }) {
  const targetId = typeof view.values["target"] === "string" ? view.values["target"] : "";
  const pinned = Number(view.values["target_revision"]);
  const target = view.refs[targetId];
  const type = types.find((t) => t.name === target?.type);
  const viewer = type?.viewers.find((v) => v.viewer === view.values["viewer"]);
  const state = readViewState(view.values["state"]);
  const [latest, setLatest] = useState(false);
  const switchId = useId();
  const capture = useRef<Capture | null>(null);
  const onCapture = useCallback((fn: Capture | null) => {
    capture.current = fn;
  }, []);
  const ref = target?.hrid ?? targetId;
  const revision = useQuery({ ...revisionQuery(ref, pinned), enabled: Boolean(target) });
  const current = useQuery({ ...entityQuery(ref), enabled: Boolean(target) && latest });
  const { say } = useMessages();
  const save = useApiMutation(async () => {
    const now = capture.current ? await capture.current() : state;
    const shownRevision = latest && current.data ? current.data.revision : pinned;
    return unwrap(
      await iki.PATCH("/api/v1/entities/{ref}", {
        params: {
          path: { ref: view.id },
          header: { "If-Match": `"${String(view.revision)}"` },
        },
        body: { values: { state: now, target_revision: shownRevision }, reason: "" },
      }),
    );
  }, [ENTITIES, ["views"]]);

  if (!target) {
    return (
      <section className="card">
        <p className="muted">It shows an entity you can’t read.</p>
      </section>
    );
  }
  if (!type || !viewer) {
    return (
      <section className="card">
        <p className="muted">
          {target.hrid}’s type doesn’t have the {String(view.values["viewer"])} viewer any more.
        </p>
      </section>
    );
  }
  const source: Source | undefined = latest
    ? current.data
    : revision.data
      ? {
          title: target.title,
          values: revision.data.values,
          files: revision.data.files,
          refs: revision.data.refs,
          owner_project: view.owner_project,
        }
      : undefined;
  const behind = current.data && current.data.revision !== pinned;
  return (
    <section className="card" aria-labelledby="view-title">
      <h2 className="section-title" id="view-title">
        {viewer.label} of{" "}
        <Link to="/entities/$ref/" params={{ ref: target.hrid }}>
          {target.title}
        </Link>
      </h2>
      <div className="view-pin">
        <p className="muted">
          {latest && current.data
            ? `The latest revision of ${target.hrid}, ${String(current.data.revision)}. The view saved revision ${String(pinned)}.`
            : `Revision ${String(pinned)} of ${target.hrid}, as the view saved it.`}
        </p>
        <label className="choice" htmlFor={switchId}>
          <input
            id={switchId}
            type="checkbox"
            role="switch"
            checked={latest}
            onChange={(event) => {
              setLatest(event.target.checked);
            }}
          />
          <span className="choice-text">Show the latest revision</span>
        </label>
        {view.can.edit ? (
          <Button
            busy={save.isPending}
            busyText="Saving…"
            onClick={() => {
              save.mutate(undefined, {
                onSuccess: (saved) => {
                  say(`Saved ${saved.hrid}, revision ${String(saved.revision)}.`);
                },
              });
            }}
          >
            {latest && behind ? "Save on the latest revision" : "Save the view"}
          </Button>
        ) : null}
      </div>
      <ProblemAlert problem={save.error ? problemOf(save.error) : undefined} />
      {revision.isError && !latest ? (
        <p className="muted">Revision {pinned} isn’t one you can read.</p>
      ) : source ? (
        <ViewerBody
          key={latest ? "latest" : "pinned"}
          source={source}
          type={type}
          viewer={viewer}
          state={state}
          onCapture={onCapture}
        />
      ) : (
        <Loading label="Opening the view…" />
      )}
    </section>
  );
}
