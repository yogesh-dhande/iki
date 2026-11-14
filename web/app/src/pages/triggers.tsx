/**
 * A project's triggers (ADR 0080): the installed plugins' triggers, which of them are on
 * here and as whom, and what each change fired. Its Admins turn them on and off, as
 * GitHub's repository settings allow Actions and Slack's admins allow workflows.
 */

import { unwrap } from "@ikisci/sdk";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useState } from "react";

import { iki, problemOf, useApiMutation, type Project } from "../api";
import { inSentence, typeLabel, typesQuery } from "../entities";
import { usePageTitle } from "../layout";
import {
  projectTriggersQuery,
  RUNS,
  triggerFiringsQuery,
  type Firing,
  type ProjectTrigger,
} from "../runs";
import {
  Button,
  ConfirmDialog,
  Loading,
  ProblemAlert,
  RelativeTime,
  SelectField,
  useMessages,
} from "../ui";
import { useProject } from "./project-overview";

/** How often the firings are read again while some wait to be fired. */
const FIRINGS_REFRESH_MS = 5_000;

const FIRING_STATE: Record<Firing["state"], string> = {
  pending: "Firing",
  launched: "Launched",
  failed: "Failed",
  skipped: "Skipped",
};

function firingTone(state: Firing["state"]): string {
  switch (state) {
    case "launched":
      return "good";
    case "failed":
      return "bad";
    case "skipped":
      return "quiet";
    default:
      return "busy";
  }
}

/** Tiers whose runs wait for the approval of the Admin a trigger runs as. */
function waits(trigger: ProjectTrigger): boolean {
  return trigger.tier === "container" || trigger.tier === "vm";
}

/** "a new protein", "a protein's organism or sequence changes": what fires it. */
function firedBy(trigger: ProjectTrigger, typeLabel: string): string {
  if (trigger.on === "entity.created") return `Each new ${typeLabel}`;
  if (trigger.fields.length === 0) return `Each change to a ${typeLabel}`;
  const fields = trigger.fields.map((f) => f.replaceAll("_", " "));
  const list =
    fields.length === 1
      ? fields[0]
      : `${fields.slice(0, -1).join(", ")} or ${fields[fields.length - 1] ?? ""}`;
  return `A change to a ${typeLabel}’s ${list ?? ""}`;
}

export function ProjectTriggers() {
  const project = useProject();
  usePageTitle(`Triggers · ${project.name}`);
  const triggers = useQuery(projectTriggersQuery(project.id));
  const types = useQuery(typesQuery).data;
  const label = (name: string) => inSentence(typeLabel(types, name));
  return (
    <>
      <p className="muted">
        A trigger runs one of a plugin’s actions by itself when entities in {project.name} are
        created or change. It’s off until one of the project’s Admins turns it on, and its runs are
        launched as that Admin; those on the container and VM tiers wait for their approval. What a
        triggered run changes never fires a trigger, so triggers can’t set each other off.
      </p>
      {triggers.isPending ? <Loading label="Loading triggers…" /> : null}
      {triggers.error ? <ProblemAlert problem={problemOf(triggers.error)} /> : null}
      {triggers.data?.length === 0 ? (
        <p className="muted empty">No installed plugin has a trigger.</p>
      ) : null}
      {triggers.data?.length ? (
        <ul className="rows" aria-label="Triggers">
          {triggers.data.map((t) => (
            <TriggerRow
              key={t.name}
              project={project}
              trigger={t}
              fires={firedBy(t, label(t.type))}
            />
          ))}
        </ul>
      ) : null}
      {triggers.data?.length ? <Firings project={project} triggers={triggers.data} /> : null}
    </>
  );
}

function stateLine(trigger: ProjectTrigger) {
  if (trigger.enabled && trigger.enabled_by) {
    return (
      <>
        On since <RelativeTime value={trigger.enabled_at ?? ""} />, running as{" "}
        {trigger.enabled_by.name}
        {trigger.last_fired_at ? (
          <>
            {" "}
            · last fired <RelativeTime value={trigger.last_fired_at} />
          </>
        ) : null}
      </>
    );
  }
  if (trigger.disabled_at && trigger.disabled_reason) {
    return (
      <>
        Iki turned it off <RelativeTime value={trigger.disabled_at} />: {trigger.disabled_reason}
      </>
    );
  }
  if (trigger.disabled_at && trigger.disabled_by) {
    return (
      <>
        Turned off by {trigger.disabled_by.name} <RelativeTime value={trigger.disabled_at} />
      </>
    );
  }
  return <>Never turned on here</>;
}

function TriggerRow({
  project,
  trigger,
  fires,
}: {
  project: Project;
  trigger: ProjectTrigger;
  fires: string;
}) {
  const [asking, setAsking] = useState<"enable" | "disable" | null>(null);
  const { say } = useMessages();
  const change = useApiMutation(
    async (verb: "enable" | "disable") => {
      const params = { path: { project_id: project.id, name: trigger.name } };
      return unwrap(
        verb === "enable"
          ? await iki.POST("/api/v1/projects/{project_id}/triggers/{name}/enable", { params })
          : await iki.POST("/api/v1/projects/{project_id}/triggers/{name}/disable", { params }),
      );
    },
    [RUNS],
  );
  const close = () => {
    setAsking(null);
    change.reset();
  };
  const onAsSomeoneElse = trigger.enabled && trigger.can.enable;
  return (
    <li className="row">
      <div className="row-main">
        <span className="row-title">{trigger.label}</span>
        <span className="row-meta">
          {fires} in {project.name} starts a run of {trigger.action_label}
          {waits(trigger) ? `, on the ${trigger.tier ?? ""} tier: each waits for approval` : ""}.
          {trigger.description ? ` ${trigger.description}` : ""}
        </span>
        <span className="row-meta">{stateLine(trigger)}</span>
        {trigger.why_not_enable && !trigger.enabled ? (
          <span className="row-meta">You can’t turn it on: {trigger.why_not_enable}</span>
        ) : null}
      </div>
      <div className="row-tags">
        <span className={`badge ${trigger.enabled ? "badge-good" : "badge-muted"}`}>
          {trigger.enabled ? "On" : "Off"}
        </span>
        {trigger.can.enable ? (
          <Button
            variant={trigger.enabled ? "quiet" : "primary"}
            onClick={() => {
              setAsking("enable");
            }}
          >
            {onAsSomeoneElse ? "Run as me" : "Turn on"}
          </Button>
        ) : null}
        {trigger.can.disable ? (
          <Button
            onClick={() => {
              setAsking("disable");
            }}
          >
            Turn off
          </Button>
        ) : null}
      </div>
      <ConfirmDialog
        isOpen={asking === "enable"}
        title={onAsSomeoneElse ? `Run “${trigger.label}” as you?` : `Turn on “${trigger.label}”?`}
        confirmLabel={onAsSomeoneElse ? "Run as me" : "Turn on"}
        confirmVariant="primary"
        busy={change.isPending}
        problem={change.error ? problemOf(change.error) : undefined}
        onCancel={close}
        onConfirm={() => {
          change.mutate("enable", {
            onSuccess: () => {
              close();
              say(`“${trigger.label}” is on, and runs as you.`);
            },
          });
        }}
      >
        <p>
          {fires} in {project.name} will start a run of {trigger.action_label}, launched as you and
          recorded as started by this trigger.
          {onAsSomeoneElse && trigger.enabled_by
            ? ` It runs as ${trigger.enabled_by.name} now.`
            : ""}
        </p>
        {waits(trigger) ? (
          <p>
            Its runs are on the {trigger.tier} tier, so each waits for your approval before it
            starts.
          </p>
        ) : (
          <p>
            Its runs are on the {trigger.tier} tier, and start without asking. The project’s budget
            still applies.
          </p>
        )}
        <p>If you lose the role to run it here, Iki turns it off and tells the project’s Admins.</p>
      </ConfirmDialog>
      <ConfirmDialog
        isOpen={asking === "disable"}
        title={`Turn off “${trigger.label}”?`}
        confirmLabel="Turn off"
        cancelLabel="Keep it on"
        busy={change.isPending}
        problem={change.error ? problemOf(change.error) : undefined}
        onCancel={close}
        onConfirm={() => {
          change.mutate("disable", {
            onSuccess: () => {
              close();
              say(`“${trigger.label}” is off.`);
            },
          });
        }}
      >
        <p>
          Changes from now on start nothing. Runs it already started carry on, and an Admin can turn
          it on again.
        </p>
      </ConfirmDialog>
    </li>
  );
}

function Firings({ project, triggers }: { project: Project; triggers: ProjectTrigger[] }) {
  const { trigger } = useSearch({ from: "/projects/$slug/triggers" });
  const navigate = useNavigate();
  const firings = useInfiniteQuery({
    ...triggerFiringsQuery(project.id, trigger),
    refetchInterval: (query) =>
      query.state.data?.pages.some((p) => p.items.some((f) => f.state === "pending"))
        ? FIRINGS_REFRESH_MS
        : false,
  });
  const items = firings.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <section aria-labelledby="firings-title">
      <h2 className="section-title" id="firings-title">
        What they fired
      </h2>
      <div className="toolbar">
        <SelectField
          label="Trigger"
          compact
          value={trigger ?? ""}
          options={[
            { value: "", label: "Every trigger" },
            ...triggers.map((t) => ({ value: t.name, label: t.label })),
          ]}
          onChange={(value) => {
            void navigate({
              to: "/projects/$slug/triggers/",
              params: { slug: project.slug },
              search: value ? { trigger: value } : {},
            });
          }}
        />
      </div>
      {firings.isPending ? <Loading label="Loading what triggers fired…" /> : null}
      {firings.error ? <ProblemAlert problem={problemOf(firings.error)} /> : null}
      {firings.isSuccess && items.length === 0 ? (
        <p className="muted empty">Nothing yet. Each change a trigger fires on is listed here.</p>
      ) : null}
      {items.length ? (
        <ul className="rows" aria-label="Firings">
          {items.map((f) => (
            <li className="row" key={f.id}>
              <div className="row-main">
                <span className="row-title">
                  <Link to="/entities/$ref/" params={{ ref: f.entity.hrid }}>
                    {f.entity.hrid}
                  </Link>{" "}
                  {f.event === "entity.created" ? "was created" : "changed"}
                </span>
                <span className="row-meta">
                  {f.label} · revision {f.revision} · <RelativeTime value={f.created_at} />
                  {f.run ? (
                    <>
                      {" · "}
                      <Link to="/runs/$ref/" params={{ ref: f.run.hrid }}>
                        {f.run.hrid}
                      </Link>
                    </>
                  ) : null}
                  {f.error ? ` · ${f.error.message}` : ""}
                </span>
              </div>
              <div className="row-tags">
                <span className={`badge badge-${firingTone(f.state)}`}>
                  {FIRING_STATE[f.state]}
                </span>
              </div>
            </li>
          ))}
        </ul>
      ) : null}
      {firings.hasNextPage ? (
        <div className="actions">
          <Button
            busy={firings.isFetchingNextPage}
            busyText="Loading…"
            onClick={() => void firings.fetchNextPage()}
          >
            Show older
          </Button>
        </div>
      ) : null}
    </section>
  );
}
