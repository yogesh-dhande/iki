/**
 * Where a record is (ADR 0180): its path, freezer to box, and the box's map with its own
 * position marked; who has it, if it's checked out; the controls that move it, check it
 * out and in, hand it over and print its label; and its location history.
 */

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useId, useState } from "react";

import { meQuery, problemOf } from "../api";
import { BoxMap } from "../box-map";
import { personName, type Entity, type EntityType } from "../entities";
import { LabelDialog } from "../labels";
import {
  containerQuery,
  EVENT_KINDS,
  locationEventsQuery,
  locationQuery,
  stepName,
  type LocationEvent,
} from "../storage";
import { CustodyDialog, MoveDialog, WherePath, type Chosen, type Custody } from "../storage-ui";
import { Button, Loading, ProblemAlert, Time } from "../ui";

type Doing = "move" | Custody | "label";

export function EntityLocation({ entity, type }: { entity: Entity; type: EntityType | undefined }) {
  const id = useId();
  const storage = type?.storage;
  const where = useQuery({ ...locationQuery(entity.id), enabled: !!storage });
  const me = useQuery(meQuery).data;
  const [doing, setDoing] = useState<Doing | null>(null);
  const location = where.data;
  const box = location?.path.at(-1);
  const map = useQuery({
    ...containerQuery(box?.hrid ?? "-"),
    enabled: !!box && !!location?.position,
  });
  if (!storage) return null;
  const container = storage.holds.length > 0;
  const chosen: Chosen = {
    id: entity.id,
    hrid: entity.hrid,
    title: entity.title,
    type: entity.type,
    code: null,
  };
  const editing = entity.can.edit && !entity.archived;
  const out = location?.custodian;
  const mine = !!out && out.id === me?.id;
  const here = map.data?.contents.find((o) => o.id === entity.id);
  return (
    <section className="card where-card" aria-labelledby={`${id}-title`}>
      <div className="section-head">
        <h2 className="section-title" id={`${id}-title`}>
          Where it is
        </h2>
        {container ? (
          <Link to="/storage/$ref/" params={{ ref: entity.hrid }}>
            Open what’s in it
          </Link>
        ) : null}
      </div>
      {where.error ? <ProblemAlert problem={problemOf(where.error)} /> : null}
      {!location ? <Loading label="Finding where it is…" /> : null}
      {location ? <WherePath location={location} label={`Where ${entity.hrid} is`} /> : null}
      {out ? (
        <div className="alert alert-warning" role="note">
          <p>
            Checked out by {personName(out)}
            {location.custody_since ? (
              <>
                {" "}
                since <Time value={location.custody_since} />
              </>
            ) : null}
            {location.position ? `. Its position, ${location.position}, is kept for it.` : "."}
          </p>
        </div>
      ) : null}
      {map.data?.shape && here?.row && here.column ? (
        <div className="where-map">
          <BoxMap
            label={`${stepName(map.data)}’s positions, with ${entity.hrid}’s marked`}
            shape={map.data.shape}
            contents={map.data.contents}
            marked={{ row: here.row, column: here.column }}
            compact
            onActivate={() => undefined}
          />
          <Link
            className="where-open"
            to="/storage/$ref/"
            params={{ ref: map.data.hrid }}
            search={{ at: here.position ?? "" }}
          >
            Open {stepName(map.data)}
          </Link>
        </div>
      ) : null}
      {editing && location ? (
        <div className="actions">
          <Button
            onClick={() => {
              setDoing("move");
            }}
          >
            {location.stored ? "Move" : "Put away"}
          </Button>
          {location.stored && !out ? (
            <Button
              onClick={() => {
                setDoing("check-out");
              }}
            >
              Check out
            </Button>
          ) : null}
          {out ? (
            <Button
              onClick={() => {
                setDoing("check-in");
              }}
            >
              Check in
            </Button>
          ) : null}
          {out && mine ? (
            <Button
              onClick={() => {
                setDoing("hand-over");
              }}
            >
              Hand over
            </Button>
          ) : null}
          <Button
            variant="quiet"
            onClick={() => {
              setDoing("label");
            }}
          >
            Print label
          </Button>
        </div>
      ) : null}
      <History entity={entity} />
      {doing === "move" ? (
        <MoveDialog
          records={[chosen]}
          from={box?.hrid}
          onClose={() => {
            setDoing(null);
          }}
        />
      ) : null}
      {doing === "check-out" || doing === "check-in" || doing === "hand-over" ? (
        <CustodyDialog
          doing={doing}
          records={[chosen]}
          projectId={entity.owner_project?.id ?? null}
          onClose={() => {
            setDoing(null);
          }}
        />
      ) : null}
      {doing === "label" ? (
        <LabelDialog
          records={[{ id: entity.id, hrid: entity.hrid }]}
          onClose={() => {
            setDoing(null);
          }}
        />
      ) : null}
    </section>
  );
}

function endText(end: LocationEvent["from_place"]): string {
  if (!end) return "";
  const ref = end.ref;
  const name = ref
    ? ref.title && ref.title !== ref.hrid
      ? ref.title
      : ref.hrid
    : "a place you can’t open";
  return end.position ? `${name}, ${end.position}` : name;
}

function eventText(e: LocationEvent): string {
  switch (e.kind) {
    case "checked_out":
      return `to ${personName(e.custodian)}`;
    case "handed_over":
      return `from ${personName(e.from_custodian)} to ${personName(e.custodian)}`;
    case "received":
      return e.handed_over_by ? `from ${e.handed_over_by}` : "";
    case "removed":
      return e.from_place ? `from ${endText(e.from_place)}` : "";
    default: {
      const from = e.from_place ? `from ${endText(e.from_place)}` : "";
      const to = e.to_place ? `to ${endText(e.to_place)}` : "";
      return [from, to].filter(Boolean).join(" ");
    }
  }
}

function History({ entity }: { entity: Entity }) {
  const query = useInfiniteQuery(locationEventsQuery(entity.id));
  const items = query.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <details className="disclosure">
      <summary>Its location history</summary>
      {query.isPending ? <Loading label="Loading its history…" /> : null}
      {query.error ? <ProblemAlert problem={problemOf(query.error)} /> : null}
      {query.data && !items.length ? (
        <p className="muted">It hasn’t been put anywhere yet.</p>
      ) : null}
      {items.length ? (
        <div className="table-scroll">
          <table className="table">
            <caption className="visually-hidden">
              Where {entity.hrid} has been, newest first
            </caption>
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">What</th>
                <th scope="col">By</th>
                <th scope="col">Why</th>
              </tr>
            </thead>
            <tbody>
              {items.map((e) => (
                <tr key={e.id}>
                  <td>
                    <Time value={e.created_at} />
                  </td>
                  <td>
                    {EVENT_KINDS[e.kind]} {eventText(e)}
                    {e.confirmed ? (
                      <span className="cell-meta">Confirmed by scanning {e.confirmed}</span>
                    ) : null}
                  </td>
                  <td>
                    {personName(e.user)}
                    {e.agent ? <span className="cell-meta">through {e.agent.name}</span> : null}
                  </td>
                  <td>{e.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {query.hasNextPage ? (
        <div className="actions">
          <Button
            busy={query.isFetchingNextPage}
            busyText="Loading…"
            onClick={() => void query.fetchNextPage()}
          >
            Show older
          </Button>
        </div>
      ) : null}
    </details>
  );
}
