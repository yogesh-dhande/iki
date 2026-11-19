/**
 * A request between projects on its page (ADR 0132): the project that asks and the service
 * it's sent to, who sent it and when, what it sent the service and the results that came
 * of it; and, until it's sent, sending it. A result's page says whose it is until it's
 * released.
 */

import { ApiError, unwrap } from "@ikisci/sdk";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useId, useState } from "react";

import { iki, problemOf, projectQuery, projectsQuery, useApiMutation } from "../api";
import {
  ENTITIES,
  entityQuery,
  hasIdentity,
  inSentence,
  personName,
  stateLabel,
  typesQuery,
  type Entity,
  type EntityType,
  type Ref,
} from "../entities";
import {
  fieldLabels,
  sentToQuery,
  servicesQuery,
  twoSidesQuery,
  type RequestSides,
  type Service,
} from "../requests";
import {
  Button,
  ConfirmDialog,
  Loading,
  ProblemAlert,
  RadioField,
  RelativeTime,
  TextField,
  useMessages,
} from "../ui";

interface ProjectRef {
  id: string;
  name: string;
  slug: string;
}

/** A project by name: a link, if the reader can open it. Each side of a request may not
 * see the other's project. */
function ProjectName({ project }: { project: ProjectRef | null }) {
  const visible = useQuery(projectsQuery).data;
  if (!project) return <span className="muted">A project you can’t see</span>;
  if (!visible?.some((p) => p.id === project.id)) return <>{project.name}</>;
  return (
    <Link to="/projects/$slug/" params={{ slug: project.slug }}>
      {project.name}
    </Link>
  );
}

interface Shown {
  hrid: string;
  title: string;
  type: string;
  archived: boolean;
}

/** A record, linked: its title and HRID. A registry entry's record opens in `project`, the
 * project whose record it is (ADR 0115). */
function RecordLink({ record, project }: { record: Shown; project: string | undefined }) {
  const types = useQuery(typesQuery).data;
  const search = project && hasIdentity(types, record.type) ? { project } : {};
  return (
    <>
      <Link to="/entities/$ref/" params={{ ref: record.hrid }} search={search}>
        {record.title}
      </Link>
      {record.title !== record.hrid ? <span className="muted"> {record.hrid}</span> : null}
      {record.archived ? <span className="muted"> (archived)</span> : null}
    </>
  );
}

/** The records a request's `sends` fields name now, as far as the reader can read them. */
export function namedRecords(entity: Entity, sends: readonly string[]): Ref[] {
  const ids = sends.flatMap((name) => {
    const value = entity.values[name];
    const all: unknown[] = Array.isArray(value) ? value : [value];
    return all.filter((v): v is string => typeof v === "string");
  });
  return [...new Set(ids)].flatMap((id) => {
    const found = entity.refs[id];
    return found ? [found] : [];
  });
}

/** Records, each linked, in the project whose record each is. */
function RecordList({
  records,
  label,
  project,
}: {
  records: readonly (Ref | Entity)[];
  label: string;
  /** For references, the project they're in: the request's. */
  project?: string | undefined;
}) {
  return (
    <ul className="value-list" aria-label={label}>
      {records.map((r) => (
        <li key={r.hrid}>
          <RecordLink record={r} project={"owner_project" in r ? r.owner_project?.slug : project} />
        </li>
      ))}
    </ul>
  );
}

/** What a request is for, on each side: who fills in what. */
function about(type: EntityType, sides: RequestSides): string {
  const asker = sides.asking_project?.name ?? "The project that asks";
  const theirs = sides.service_fields.length ? fieldLabels(type, sides.service_fields) : "";
  if (!sides.service) {
    return theirs
      ? `${asker} asks a service for this. Once it’s sent, the service fills in ${theirs}; until then, no one can.`
      : `${asker} asks a service for this.`;
  }
  return theirs
    ? `${sides.service.name} fills in ${theirs}. ${asker}’s Editors change the rest.`
    : `${sides.service.name} works on it. ${asker}’s Editors change it.`;
}

export function EntityRequest({ entity, type }: { entity: Entity; type: EntityType }) {
  const id = useId();
  const sides = entity.request;
  const sent = sides?.submitted ?? false;
  const both = useQuery({ ...twoSidesQuery(entity.id), enabled: sent });
  if (!sides) return null;
  const sending = !sent && sides.yours.includes("asker") && !entity.archived;
  const named = namedRecords(entity, sides.sends);
  const here = entity.owner_project?.slug;
  return (
    <section className="card" aria-labelledby={`${id}-title`}>
      <h2 className="section-title" id={`${id}-title`}>
        Request
      </h2>
      <p className="muted">{about(type, sides)}</p>
      <dl className="summary">
        <dt>Asked by</dt>
        <dd>
          <ProjectName project={sides.asking_project} />
        </dd>
        <dt>Sent to</dt>
        <dd>
          {sides.service ? (
            <ProjectName project={sides.service} />
          ) : (
            <span className="muted">Not sent yet</span>
          )}
        </dd>
        {sent ? (
          <>
            <dt>Sent</dt>
            <dd>
              By {personName(sides.submitted_by)}
              {sides.submitted_at ? (
                <>
                  , <RelativeTime value={sides.submitted_at} />
                </>
              ) : null}
            </dd>
            <dt>What it sent</dt>
            <dd>
              {both.error ? (
                <ProblemAlert problem={problemOf(both.error)} />
              ) : !both.data ? (
                <Loading label="Finding what it sent…" />
              ) : both.data.sent.length ? (
                <RecordList records={both.data.sent} label="What it sent" />
              ) : (
                <span className="muted">No records</span>
              )}
            </dd>
            <dt>Results</dt>
            <dd>
              {!both.data ? null : both.data.results.length ? (
                <RecordList records={both.data.results} label="Results" />
              ) : (
                <span className="muted">
                  {sides.yours.includes("service") ? "None yet" : "None released yet"}
                </span>
              )}
            </dd>
          </>
        ) : sides.sends.length ? (
          <>
            <dt>What it sends</dt>
            <dd>
              {named.length ? (
                <RecordList records={named} label="What it sends" project={here} />
              ) : (
                <span className="muted">
                  Nothing yet: name records in its {fieldLabels(type, sides.sends)}
                </span>
              )}
            </dd>
          </>
        ) : null}
      </dl>
      {sending ? <Sending entity={entity} type={type} sides={sides} named={named} /> : null}
    </section>
  );
}

/** Sending it, for the asking project's Editors: unless no service takes it from them, or
 * their project keeps its records to itself. */
function Sending({
  entity,
  type,
  sides,
  named,
}: {
  entity: Entity;
  type: EntityType;
  sides: RequestSides;
  named: Ref[];
}) {
  const id = useId();
  const owner = sides.asking_project ?? entity.owner_project;
  const project = useQuery({ ...projectQuery(owner?.slug ?? ""), enabled: Boolean(owner) });
  const services = useQuery({
    ...servicesQuery(owner?.id ?? "", type.name),
    enabled: Boolean(owner),
  });
  const [open, setOpen] = useState(false);
  if (!owner) return null;
  if (services.error) return <ProblemAlert problem={problemOf(services.error)} />;
  const why =
    project.data && !project.data.allow_sharing
      ? `${owner.name} doesn’t allow sharing its entities outside it, so it can’t send requests. Its Admins can allow it in its settings.`
      : services.data && !services.data.length
        ? `No service takes ${inSentence(type.label_plural)} from ${owner.name} yet. A service’s Admins choose what it takes, in its settings.`
        : "";
  return (
    <>
      <div className="actions">
        <Button
          variant="primary"
          disabled={Boolean(why) || !services.data}
          describedBy={why ? `${id}-why` : undefined}
          onClick={() => {
            setOpen(true);
          }}
        >
          Send to a service…
        </Button>
      </div>
      {why ? (
        <p className="muted" id={`${id}-why`}>
          {why}
        </p>
      ) : null}
      {open && services.data ? (
        <SendDialog
          entity={entity}
          type={type}
          sides={sides}
          named={named}
          services={services.data}
          onClose={() => {
            setOpen(false);
          }}
        />
      ) : null}
    </>
  );
}

function SendDialog({
  entity,
  type,
  sides,
  named,
  services,
  onClose,
}: {
  entity: Entity;
  type: EntityType;
  sides: RequestSides;
  named: Ref[];
  services: Service[];
  onClose: () => void;
}) {
  const { say } = useMessages();
  const client = useQueryClient();
  const [chosen, setChosen] = useState(
    services.length === 1 ? (services[0]?.project.id ?? "") : "",
  );
  const [reason, setReason] = useState("");
  const [missing, setMissing] = useState(false);
  const send = useApiMutation(
    async (service: string) =>
      unwrap(
        await iki.POST("/api/v1/entities/{ref}/submit", {
          params: {
            path: { ref: entity.id },
            header: { "If-Match": `"${String(entity.revision)}"` },
          },
          body: { service, reason: reason.trim() },
        }),
      ),
    // Its project has sent to one more service, maybe.
    [ENTITIES, sentToQuery(sides.asking_project?.id ?? "").queryKey],
  );
  const service = services.find((s) => s.project.id === chosen);
  const stale = send.error instanceof ApiError && send.error.problem.code === "stale";
  return (
    <ConfirmDialog
      isOpen
      title={`Send ${entity.hrid} to a service`}
      confirmLabel={service ? `Send to ${service.project.name}` : "Send"}
      confirmVariant="primary"
      busy={send.isPending}
      problem={
        stale && send.error
          ? {
              ...send.error.problem,
              detail:
                "Someone changed it while this was open. The page now shows their change: check it, then send it again.",
            }
          : send.error
            ? problemOf(send.error)
            : undefined
      }
      onCancel={onClose}
      onConfirm={() => {
        if (!service) {
          setMissing(true);
          return;
        }
        send.mutate(service.project.id, {
          onSuccess: (saved) => {
            client.setQueryData(entityQuery(saved.hrid).queryKey, saved);
            onClose();
            say(`Sent ${saved.hrid} to ${service.project.name}.`);
          },
          onError: (error) => {
            // Read it again, so sending again sends the revision it's at now.
            if (error.problem.code === "stale") {
              void client.invalidateQueries({ queryKey: ENTITIES });
            }
          },
        });
      }}
    >
      <RadioField
        label="Service"
        value={chosen}
        onChange={(value) => {
          setChosen(value);
          setMissing(false);
        }}
        options={services.map((s) => ({
          value: s.project.id,
          label: s.project.name,
          ...(s.description ? { description: s.description } : {}),
        }))}
        error={missing ? "Choose the service to send it to." : undefined}
      />
      <p>
        {service?.project.name ?? "The service"} reads this {inSentence(type.label)}
        {sides.sends.length
          ? ` and the records its ${fieldLabels(type, sides.sends)} name, now or later`
          : ""}
        : nothing else of {sides.asking_project?.name ?? "this project"}’s.
      </p>
      {named.length ? (
        <>
          <p>It sends these with it now:</p>
          <RecordList records={named} label="Sent with it" project={entity.owner_project?.slug} />
        </>
      ) : null}
      <p>
        It can’t be taken back: once it’s sent, it stays with{" "}
        {service?.project.name ?? "the service"}, and can’t go to another.
      </p>
      <TextField
        label="Reason"
        value={reason}
        onChange={setReason}
        maxLength={2000}
        optional
        multiline
        help="Why, for the record."
      />
    </ConfirmDialog>
  );
}

/** A request's result (ADR 0132): the service's until it releases it to the project that
 * asked, where it then stays. */
export function ResultNote({ entity, type }: { entity: Entity; type: EntityType }) {
  const spec = type.result;
  if (!spec) return null;
  const of = entity.values[spec.of];
  const request = typeof of === "string" ? entity.refs[of] : undefined;
  const field = type.fields.find((f) => f.name === spec.released_field);
  const state = entity.values[spec.released_field];
  const released = typeof state === "string" && spec.released_states.includes(state);
  const states = spec.released_states.map((s) => (field ? stateLabel(field, s) : s));
  const service = entity.owner_project?.name ?? "the service";
  return (
    <div className="alert alert-info" role="note">
      <p>
        A result of{" "}
        {request ? <RecordLink record={request} project={undefined} /> : "a request you can’t read"}
        .{" "}
        {released
          ? "It’s released to the project that asked for it, where it stays."
          : `It’s ${service}’s until its ${field?.label ?? spec.released_field} is ${states.join(" or ")}: then the project that asked for it reads it too.`}
      </p>
    </div>
  );
}
