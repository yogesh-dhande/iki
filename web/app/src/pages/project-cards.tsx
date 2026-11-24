/**
 * The cards on a project's Overview (ADR 0176): its records by type, what changed lately,
 * what it is and who's in it, and its automation. Each reads only what its reader may.
 */

import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

import {
  meQuery,
  membersQuery,
  problemOf,
  rolesQuery,
  webhooksQuery,
  type Member,
  type Project,
} from "../api";
import { personName, typesQuery } from "../entities";
import { initials, swatchOf } from "../project-color";
import { TypeDot } from "../type-color";
import { changeWords, changedBy, projectChangesQuery, type ProjectChange } from "../project-page";
import { countsQuery } from "../lists";
import { projectTriggersQuery, workflowsQuery } from "../runs";
import { Reserve, useSettled } from "../settling";
import { Loading, ProblemAlert, RelativeTime, Time } from "../ui";

/** A card of the Overview: a heading, maybe a link beside it, and what it holds. */
export function Box({
  id,
  title,
  aside,
  children,
}: {
  id: string;
  title: string;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="box" aria-labelledby={id}>
      <div className="box-head">
        <h2 id={id}>{title}</h2>
        {aside ? <div className="box-aside">{aside}</div> : null}
      </div>
      {children}
    </section>
  );
}

/** How many of each type of record the project has, each opening that type's table. */
export function EntityCounts({ project }: { project: Project }) {
  const counts = useQuery(countsQuery(project.id));
  const late = useSettled(!counts.isLoading);
  // The lists across projects name a project by its slug (ADR 0176).
  const held = { project: project.slug };
  return (
    <Box
      id="project-counts-title"
      title="Entities in this project"
      aside={
        <Link to="/entities/" search={held}>
          Open in Entities, filtered to this project
        </Link>
      }
    >
      <Reserve
        late={late}
        ready={!counts.isLoading}
        size="reserve-counts"
        label="Counting its entities…"
        name="Entities by type"
      >
        {counts.isError ? <ProblemAlert problem={problemOf(counts.error)} /> : null}
        {counts.isPending ? <Loading label="Counting its entities…" /> : null}
        {counts.data?.types.length === 0 ? (
          <p className="box-body muted">
            No entities yet.{" "}
            {project.can.edit && !project.archived ? (
              <Link to="/projects/$slug/entities/new/" params={{ slug: project.slug }}>
                Add one
              </Link>
            ) : null}
          </p>
        ) : null}
        {counts.data?.types.length ? (
          <ul className="type-counts" aria-label="Entities by type">
            {counts.data.types.map((t) => (
              <li key={t.type}>
                <Link to="/entities/types/$/" params={{ _splat: t.type }} search={held}>
                  <span className="type-count-label">
                    <TypeDot type={t.type} />
                    {t.label}
                  </span>{" "}
                  {t.count === null ? (
                    <span className="type-count-value muted" title="Not counted: it would be slow">
                      <span aria-hidden="true">–</span>
                      <span className="visually-hidden">not counted</span>
                    </span>
                  ) : (
                    <span className="type-count-value">{t.count.toLocaleString("en-GB")}</span>
                  )}
                </Link>
              </li>
            ))}
          </ul>
        ) : null}
      </Reserve>
    </Box>
  );
}

/** A person's initials on their swatch, with their name for those who can't see it; only
 * the swatch where their name is beside it. */
export function Avatar({ id, name, named = false }: { id: string; name: string; named?: boolean }) {
  if (named) {
    return (
      <span className="avatar" data-swatch={swatchOf(id)} aria-hidden="true">
        {initials(name)}
      </span>
    );
  }
  return (
    <span className="avatar" data-swatch={swatchOf(id)} title={name}>
      <span aria-hidden="true">{initials(name)}</span>
      <span className="visually-hidden">{name}</span>
    </span>
  );
}

function ChangeRow({ change, project }: { change: ProjectChange; project: Project }) {
  const types = useQuery(typesQuery).data;
  const type = types?.find((t) => t.name === change.entity.type);
  const { verb, after } = changeWords(change, type);
  const who = changedBy(change);
  return (
    <li className="change">
      {change.user ? (
        <Avatar id={change.user.id} name={personName(change.user)} named />
      ) : (
        <span className="avatar avatar-system" aria-hidden="true">
          iki
        </span>
      )}
      <span className="change-what">
        {who} {verb}{" "}
        <Link
          className="hrid"
          to="/entities/$ref/"
          params={{ ref: change.entity.hrid }}
          search={{ project: project.slug }}
        >
          {change.entity.hrid}
        </Link>
        {change.entity.title !== change.entity.hrid ? ` ${change.entity.title}` : ""}
        {after}
        {change.reason ? <span className="muted"> · “{change.reason}”</span> : null}
      </span>
      <span className="change-when muted">
        {change.created_at ? <RelativeTime value={change.created_at} /> : null}
      </span>
    </li>
  );
}

/** What changed in the project lately, newest first; Auditors open the audit log from it. */
export function RecentChanges({ project }: { project: Project }) {
  const changes = useQuery(projectChangesQuery(project.id));
  const me = useQuery(meQuery);
  // Its audit log link is beside its heading, by what the reader may read.
  const late = useSettled(!changes.isLoading && !me.isLoading);
  const audit = `/audit/?${new URLSearchParams({ project: project.id }).toString()}`;
  return (
    <Box
      id="project-changes-title"
      title="Recent changes"
      aside={me.data?.can.read_audit ? <a href={audit}>Project audit log</a> : undefined}
    >
      <Reserve
        late={late}
        ready={!changes.isLoading}
        size="reserve-changes"
        label="Finding what changed…"
        name="Recent changes"
      >
        {changes.isError ? <ProblemAlert problem={problemOf(changes.error)} /> : null}
        {changes.isPending ? <Loading label="Finding what changed…" /> : null}
        {changes.data?.length === 0 ? (
          <p className="box-body muted">Nothing has changed in {project.name} yet.</p>
        ) : null}
        {changes.data?.length ? (
          <ul className="changes" aria-label="Recent changes">
            {changes.data.map((change) => (
              <ChangeRow
                key={`${change.entity.id}-${String(change.number)}`}
                change={change}
                project={project}
              />
            ))}
          </ul>
        ) : null}
      </Reserve>
    </Box>
  );
}

/** What the project is for, its team, your role in it, and when it began. */
export function About({ project }: { project: Project }) {
  const roles = useQuery(rolesQuery);
  const late = useSettled(!roles.isLoading);
  const role = roles.data?.find((r) => r.name === project.my_role);
  // Whether there's a role is known with the project; what it's called comes with the roles.
  const roleRow = late ? Boolean(project.my_role) : Boolean(role);
  return (
    <Box id="project-about-title" title="About">
      <div className="box-body">
        {project.description ? <p className="preserve-lines">{project.description}</p> : null}
        <dl className="summary">
          <dt>Team</dt>
          <dd>
            <a href={`/teams/${project.team.slug}/`}>{project.team.name}</a>
          </dd>
          {roleRow ? (
            <>
              <dt>Your role</dt>
              <dd>
                <Reserve
                  late={late}
                  ready={!roles.isLoading}
                  size="reserve-role"
                  label="Finding your role…"
                  name="Your role"
                >
                  {role ? (
                    <>
                      <strong>{role.label}</strong>: {role.description}
                    </>
                  ) : (
                    project.my_role
                  )}
                </Reserve>
              </dd>
            </>
          ) : null}
          <dt>Created</dt>
          <dd>
            <Time value={project.created_at} />
          </dd>
        </dl>
      </div>
    </Box>
  );
}

/** The most members the card shows; the Members tab has them all. */
const MEMBERS_SHOWN = 12;

function memberName(member: Member): string {
  return member.user ? personName(member.user) : (member.team?.name ?? "");
}

/** Who's in the project: people, and teams as one each. */
export function Members({ project }: { project: Project }) {
  const members = useQuery(membersQuery(project.id));
  const late = useSettled(!members.isLoading);
  const found = members.data ?? [];
  const more = found.length - MEMBERS_SHOWN;
  return (
    <Box
      id="project-members-title"
      title="Members"
      aside={
        <Link to="/projects/$slug/members/" params={{ slug: project.slug }}>
          {project.can.manage_members ? "Manage" : "All members"}
        </Link>
      }
    >
      <Reserve
        late={late}
        ready={!members.isLoading}
        size="reserve-members"
        label="Finding its members…"
        name="Members"
      >
        {members.isError ? <ProblemAlert problem={problemOf(members.error)} /> : null}
        {members.isPending ? <Loading label="Finding its members…" /> : null}
        {found.length ? (
          <ul className="avatars" aria-label="Members">
            {found.slice(0, MEMBERS_SHOWN).map((m) => (
              <li key={m.id}>
                <Avatar id={m.user?.id ?? m.team?.id ?? m.id} name={memberName(m)} />
              </li>
            ))}
            {more > 0 ? <li className="avatars-more muted">+{more}</li> : null}
          </ul>
        ) : null}
      </Reserve>
    </Box>
  );
}

function counted(n: number, one: string, many: string): string {
  return `${String(n)} ${n === 1 ? one : many}`;
}

/** How much runs by itself: the workflows it may run, its triggers on, its webhooks. */
export function Automation({ project }: { project: Project }) {
  const read = {
    workflows: useQuery(workflowsQuery),
    triggers: useQuery(projectTriggersQuery(project.id)),
    // A query that isn't asked, for one who can't manage them, isn't loading.
    webhooks: useQuery({ ...webhooksQuery(project.id), enabled: project.can.manage_webhooks }),
  };
  const ready = Object.values(read).every((q) => !q.isLoading);
  const late = useSettled(ready);
  const workflows = read.workflows.data;
  const triggers = read.triggers.data;
  const webhooks = read.webhooks.data;
  const parts = [
    workflows ? counted(workflows.length, "workflow", "workflows") : "",
    triggers
      ? `${String(triggers.filter((t) => t.enabled).length)} of ${counted(triggers.length, "trigger", "triggers")} on`
      : "",
    webhooks ? counted(webhooks.length, "webhook", "webhooks") : "",
  ].filter(Boolean);
  return (
    <Box id="project-automation-title" title="Automation">
      <div className="box-body">
        <Reserve
          late={late}
          ready={ready}
          size="reserve-line"
          label="Counting its automation…"
          name="Automation"
        >
          {parts.length ? <p className="muted">{parts.join(" · ")}</p> : null}
        </Reserve>
        <p>
          <Link to="/projects/$slug/automation/" params={{ slug: project.slug }}>
            {project.can.manage_webhooks
              ? "Workflows, triggers and webhooks"
              : "Workflows and triggers"}
          </Link>
        </p>
      </div>
    </Box>
  );
}
