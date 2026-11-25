/**
 * A project's starters (ADR 0198): the recipes installed to set an assay service up. Its
 * Admins choose a review flow, drawn as the flow its results follow (ADR 0199), the project
 * that asks it, and who holds each lab role; preview what that changes; then apply it. Its
 * other members read what's installed and what's applied.
 */

import { ApiError } from "@ikisci/sdk";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useId, useMemo, useState, type SubmitEvent } from "react";

import { membersQuery, problemOf, projectsQuery, useApiMutation, type Project } from "../api";
import { typeFlowQuery } from "../flow";
import { FlowDiagram, useRoleLabels } from "../flow-diagram";
import { usePageTitle } from "../layout";
import { either } from "../flow-model";
import { StaleNote } from "../observations";
import { intakeQuery, listed } from "../requests";
import {
  applyStarter,
  eligible,
  exampleLabel,
  flowSummary,
  independentEnough,
  planChanges,
  previewStarter,
  signingRoles,
  STARTERS,
  startersQuery,
  type Member,
  type Starter,
  type StarterPlan,
  type StarterSetup,
} from "../starters";
import {
  Badge,
  Button,
  CheckboxesField,
  Loading,
  ProblemAlert,
  RadioField,
  SelectField,
  useMessages,
} from "../ui";
import { useProject } from "./project-overview";

export function ProjectStarters() {
  const project = useProject();
  usePageTitle(`Starters · ${project.name}`);
  const starters = useQuery(startersQuery(project.id));
  return (
    <div className="columns">
      <section className="card card-wide starter-intro">
        <h2 className="section-title">Starters</h2>
        <p className="muted">
          A starter sets {project.name} up as a service from what’s installed: the review flow its
          results follow, the project that asks it, who holds each lab role, and an example to
          practise on. Applying one again with another flow changes only what new work follows.
        </p>
        <p>
          <Link to="/projects/$slug/settings/" params={{ slug: project.slug }}>
            Back to settings
          </Link>
        </p>
      </section>
      {starters.isPending ? <Loading label="Loading starters" /> : null}
      {starters.error ? <ProblemAlert problem={problemOf(starters.error)} /> : null}
      {starters.data?.length === 0 ? (
        <section className="card card-wide">
          <p className="empty">No starters are installed. A plugin that offers one adds it here.</p>
        </section>
      ) : null}
      {starters.data?.map((s) =>
        project.can.manage && !project.archived ? (
          <StarterSetupCard key={s.id} starter={s} project={project} />
        ) : (
          <StarterCard key={s.id} starter={s} project={project} />
        ),
      )}
    </div>
  );
}

/** What's applied, in a badge. */
function Applied({ starter }: { starter: Starter }) {
  const applied = starter.applied;
  if (!applied) return <Badge muted>Not applied here</Badge>;
  const flow = starter.flows.find((f) => f.id === applied.flow);
  return (
    <Badge>
      Applied: {flow?.label ?? applied.flow}, version {applied.version}
    </Badge>
  );
}

/** A starter as its members read it: its flows, drawn, and what's applied. */
function StarterCard({ starter, project }: { starter: Starter; project: Project }) {
  const id = useId();
  return (
    <section className="card card-wide starter" aria-labelledby={`${id}-title`}>
      <div className="section-header">
        <h2 className="section-title" id={`${id}-title`}>
          {starter.label}
        </h2>
        <span className="tags">
          <Badge muted>Version {starter.version}</Badge>
          <Applied starter={starter} />
        </span>
      </div>
      <p>{starter.description}</p>
      <p className="muted">
        {project.archived
          ? "Restore the project to set it up."
          : "Only the project’s Admins set it up. These are its review flows."}
      </p>
      {starter.flows.map((flow) => (
        <div className="starter-flow" key={flow.id}>
          <h3 className="starter-flow-title">{flow.label}</h3>
          <p>{flowSummary(flow)}</p>
          <ResultFlow type={flow.result_type} />
        </div>
      ))}
    </section>
  );
}

/** The flow a starter's result type follows, drawn. */
function ResultFlow({ type }: { type: string }) {
  const flow = useQuery(typeFlowQuery(type));
  const roles = useRoleLabels();
  const field = flow.data?.fields[0];
  if (flow.isPending) return <Loading label="Loading its flow" />;
  if (!field) return null;
  return (
    <>
      <FlowDiagram flow={field} roles={roles} words="hidden" />
      <p className="starter-flow-more">
        <Link to="/types/$/" params={{ _splat: type }}>
          Every state and check of {flow.data?.label ?? type}
        </Link>
      </p>
    </>
  );
}

function memberName(member: Member): string {
  const user = member.user;
  if (!user) return member.team?.name ?? "A team";
  return user.name || user.email;
}

const PROJECT_ROLE = { viewer: "Viewer", editor: "Editor", runner: "Runner", admin: "Admin" };

/** A starter as a project's Admin sets it up. */
function StarterSetupCard({ starter, project }: { starter: Starter; project: Project }) {
  const id = useId();
  const { say } = useMessages();
  const applied = starter.applied;
  const members = useQuery(membersQuery(project.id));
  const projects = useQuery(projectsQuery);
  const intake = useQuery(intakeQuery(project.id));
  const [flowId, setFlowId] = useState(applied?.flow ?? starter.flows[0]?.id ?? "research");
  const flow = starter.flows.find((f) => f.id === flowId) ?? starter.flows[0];
  const resultFlow = useQuery(typeFlowQuery(flow?.result_type ?? ""));
  const [requester, setRequester] = useState(applied?.requester_project ?? "");
  // Who's chosen for each role: until the Admin chooses, whoever holds it already.
  const [picked, setPicked] = useState<Record<string, string[]> | null>(null);
  const [plan, setPlan] = useState<{ key: string; plan: StarterPlan } | null>(null);
  // After a refused apply: how what applying does changed since it was previewed.
  const [changes, setChanges] = useState<{ key: string; body: string }[] | null>(null);
  const [tried, setTried] = useState(false);

  const roleOf = (role: string) => starter.roles.find((r) => r.id === role);
  const label = (role: string) => roleOf(role)?.label ?? role;
  const held = useMemo(() => {
    const out: Record<string, string[]> = {};
    for (const role of starter.roles) {
      out[role.id] = eligible(members.data ?? [], role.minimum_project_role)
        .filter((m) => m.lab_roles.some((g) => g.role === role.id))
        .map((m) => m.id);
    }
    return out;
  }, [members.data, starter.roles]);
  const chosen = picked ?? held;
  const required = flow?.required_roles ?? [];
  const signers = signingRoles(resultFlow.data?.fields[0]).filter((r) => required.includes(r));
  const independent = independentEnough(signers, chosen);
  const missing = required.filter((r) => !(chosen[r] ?? []).length);
  // The original requester stays (ADR 0198): its example request and results stay together.
  const locked = applied !== null;
  const cannotSwitch = applied !== null && applied.requester_project === null;
  const requesters = (projects.data ?? []).filter(
    (p) => p.id !== project.id && !p.archived && p.can.edit,
  );
  const setup: StarterSetup = {
    project: project.id,
    requester_project: requester,
    version: starter.version,
    flow: flow?.id ?? "research",
    role_assignments: Object.fromEntries(required.map((r) => [r, [...(chosen[r] ?? [])].sort()])),
  };
  const key = JSON.stringify(setup);
  const previewed = plan?.key === key ? plan.plan : null;

  const preview = useApiMutation((s: StarterSetup) => previewStarter(starter.id, s), []);
  // It applies the plan shown, as Iki identified it (ADR 0207).
  const apply = useApiMutation(
    ({ setup: s, plan: reviewed }: { setup: StarterSetup; plan: string }) =>
      applyStarter(starter.id, s, reviewed),
    [STARTERS, ["projects"], ["entities"]],
  );
  const problem =
    (apply.error ?? preview.error) ? problemOf(apply.error ?? preview.error) : undefined;
  const memberById = new Map((members.data ?? []).map((m) => [m.id, m]));
  const nameOf = (member: string) => {
    const found = memberById.get(member);
    return found ? memberName(found) : "someone";
  };

  const blocked = !requester
    ? "Choose the project that asks for the example assay."
    : missing.length
      ? `Choose someone for ${either(missing.map(label))}.`
      : !independent
        ? `Choose ${String(signers.length)} different people for ${listed(signers.map(label))}: each signs a different step, and someone who signed one can’t sign the next. One person may hold several roles, so long as each step can have its own signer.`
        : null;

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setTried(true);
    if (blocked || cannotSwitch) return;
    if (!previewed) {
      apply.reset();
      setChanges(null);
      preview.mutate(setup, {
        onSuccess: (result) => {
          setPlan({ key, plan: result });
        },
      });
      return;
    }
    const shown = previewed;
    setChanges(null);
    apply.mutate(
      { setup, plan: shown.plan },
      {
        onError: (error) => {
          // What applying does changed: nothing was applied. Show it as it is now, and what
          // changed, to review; applying again applies that.
          const now = error instanceof ApiError ? error.problem.plan : undefined;
          if (error instanceof ApiError && error.problem.code === "starter_plan_changed" && now) {
            apply.reset();
            setPlan({ key, plan: now });
            setChanges(planChanges(shown, now, label, nameOf));
          }
        },
        onSuccess: (result) => {
          setPlan({
            key,
            plan: { ...result, changed: false, grants_to_add: {}, examples_to_create: [] },
          });
          say(
            result.changed
              ? `Applied ${starter.label} with the ${flow?.label ?? ""} flow.`
              : `${starter.label} was already set up this way.`,
          );
        },
      },
    );
  }

  const people = (role: string) => {
    const minimum = roleOf(role)?.minimum_project_role ?? "viewer";
    return eligible(members.data ?? [], minimum);
  };
  const intakeEmpty =
    intake.data &&
    !intake.data.types.length &&
    !intake.data.projects.length &&
    !intake.data.from_registry;
  const requesterName =
    projects.data?.find((p) => p.id === requester)?.name ?? "the requesting project";

  return (
    <section className="card card-wide starter" aria-labelledby={`${id}-title`}>
      <div className="section-header">
        <h2 className="section-title" id={`${id}-title`}>
          {starter.label}
        </h2>
        <span className="tags">
          <Badge muted>Version {starter.version}</Badge>
          <Applied starter={starter} />
        </span>
      </div>
      <p>{starter.description}</p>
      <form onSubmit={submit} noValidate>
        <RadioField
          label="Review flow"
          value={flow?.id ?? "research"}
          onChange={(value) => {
            setFlowId(value);
          }}
          options={starter.flows.map((f) => ({
            value: f.id,
            label: f.id === applied?.flow ? `${f.label} (applied now)` : f.label,
            description: flowSummary(f),
          }))}
        />
        {flow ? (
          <div className="starter-flow">
            <h3 className="starter-flow-title">How a {flow.label} result moves</h3>
            <ResultFlow type={flow.result_type} />
          </div>
        ) : null}
        {applied && flow && applied.flow !== flow.id ? (
          <div className="alert alert-warning" role="note">
            <p>
              Switching to {flow.label} changes what new fits create. Results already recorded keep
              the flow they started in, with its signatures: a QC result already submitted still
              needs its independent review and QA release, and a research result waiting for its
              sign-off still needs only that.
            </p>
          </div>
        ) : null}

        <h3 className="starter-step">The project that asks</h3>
        {locked ? (
          cannotSwitch ? (
            <p className="field-help">
              It was set up with a requesting project you can’t edit. Only someone who can edit that
              project can apply it again, as its example request and results stay with it.
            </p>
          ) : (
            <p className="field-help">
              {requesterName}, as when it was first applied. It stays the same, so its example
              request and results stay together.
            </p>
          )
        ) : (
          <SelectField
            label="Requesting project"
            value={requester}
            onChange={setRequester}
            help="It sends the example analysis request, and gets the signed result back. You need to be able to edit it."
            error={
              tried && !requester
                ? "Choose the project that asks for the example assay."
                : undefined
            }
            options={[
              { value: "", label: "Choose a project" },
              ...requesters.map((p) => ({ value: p.id, label: p.name })),
            ]}
          />
        )}

        <h3 className="starter-step">Who does what</h3>
        <p className="field-help starter-help">
          People with their own membership of {project.name}. Each lab role needs a project role at
          least as high as it says; add people in{" "}
          <Link to="/projects/$slug/members/" params={{ slug: project.slug }}>
            Members
          </Link>
          .
        </p>
        {members.isPending ? <Loading label="Loading members" /> : null}
        {required.map((role) => {
          const options = people(role);
          const minimum = roleOf(role)?.minimum_project_role ?? "viewer";
          return options.length ? (
            <CheckboxesField
              key={role}
              label={label(role)}
              literal={false}
              value={chosen[role] ?? []}
              onChange={(value) => {
                setPicked({ ...chosen, [role]: value });
              }}
              help={`Needs ${PROJECT_ROLE[minimum]} or higher.`}
              error={
                tried && !(chosen[role] ?? []).length
                  ? `Choose someone for ${label(role)}.`
                  : undefined
              }
              options={options.map((m) => ({
                value: m.id,
                label: memberName(m),
                description: [
                  PROJECT_ROLE[m.role],
                  m.lab_roles.some((g) => g.role === role) ? `holds ${label(role)} already` : "",
                ]
                  .filter(Boolean)
                  .join(" · "),
              }))}
            />
          ) : (
            <div className="field" key={role}>
              <p className="starter-role">{label(role)}</p>
              <p className="field-error">
                No one here can be {label(role)}: it needs a membership of their own as{" "}
                {PROJECT_ROLE[minimum]} or higher.
              </p>
            </div>
          );
        })}
        {tried && blocked && !missing.length && requester ? (
          <p className="field-error" role="alert">
            {blocked}
          </p>
        ) : null}

        <ProblemAlert problem={problem} />
        {previewed && changes ? (
          <StaleNote
            changes={changes}
            what="What applying does"
            again={
              previewed.changed
                ? "If it’s still what you want, apply it."
                : "Nothing is left to apply."
            }
          />
        ) : null}
        {previewed ? (
          <Plan
            plan={previewed}
            flowLabel={starter.flows.find((f) => f.id === previewed.flow)?.label ?? previewed.flow}
            label={label}
            memberName={nameOf}
            intake={
              intakeEmpty && !applied
                ? `${project.name} takes no requests yet: applying lets it take analysis requests from ${requesterName}, and from no other project.`
                : null
            }
          />
        ) : null}
        <div className="actions">
          <Button
            type="submit"
            variant="primary"
            disabled={cannotSwitch || (previewed !== null && !previewed.changed)}
            busy={preview.isPending || apply.isPending}
            busyText={preview.isPending ? "Previewing…" : "Applying…"}
          >
            {previewed ? "Apply" : "Preview"}
          </Button>
          {previewed ? (
            <Button
              variant="quiet"
              onClick={() => {
                setPlan(null);
                setChanges(null);
              }}
            >
              Change it
            </Button>
          ) : null}
        </div>
      </form>
    </section>
  );
}

/** What applying will do: the grants it adds, the examples it makes, the intake it sets. */
function Plan({
  plan,
  flowLabel,
  label,
  memberName,
  intake,
}: {
  plan: StarterPlan;
  flowLabel: string;
  label: (role: string) => string;
  memberName: (membership: string) => string;
  intake: string | null;
}) {
  const grants = Object.entries(plan.grants_to_add).flatMap(([role, ids]) =>
    ids.map((member) => `${memberName(member)} becomes ${label(role)}`),
  );
  return (
    <div className="starter-plan" role="status" aria-label="What applying will do">
      <h3 className="starter-step">What applying will do</h3>
      {!plan.changed ? (
        <p>Nothing: it’s set up this way already.</p>
      ) : (
        <ul>
          <li>New results follow the {flowLabel} flow.</li>
          {grants.map((g) => (
            <li key={g}>{g}.</li>
          ))}
          {plan.examples_to_create.length ? (
            <li>
              It makes an example to practise on:{" "}
              {listed(plan.examples_to_create.map(exampleLabel))}.
            </li>
          ) : null}
          {intake ? <li>{intake}</li> : null}
        </ul>
      )}
      {Object.keys(plan.examples).length ? (
        <p className="muted">
          Its example records are there already, and stay:{" "}
          {listed(Object.keys(plan.examples).map(exampleLabel))}.
        </p>
      ) : null}
    </div>
  );
}
