/**
 * A project's starters (ADRs 0198, 0240 and 0247): the recipes installed to set a service or
 * procedure up, such as an assay, lot release testing or experiment sign-off. Its Admins choose a review flow,
 * drawn as the flow its results follow (ADR 0199), and the members who hold each lab role.
 * They preview the changes before applying them. Its other members read what's installed
 * and what's applied.
 */

import { ApiError } from "@ikisci/sdk";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useId, useMemo, useState, type SubmitEvent } from "react";

import { labRolesQuery, membersQuery, problemOf, useApiMutation, type Project } from "../api";
import { typeFlowQuery } from "../flow";
import { FlowDiagram, useRoleLabels } from "../flow-diagram";
import { usePageTitle } from "../layout";
import { useForm } from "../forms";
import { StaleNote } from "../observations";
import { listed } from "../requests";
import {
  applyStarter,
  canStaff,
  eligible,
  independentEnough,
  planChanges,
  previewStarter,
  STARTERS,
  startersQuery,
  type Member,
  routeOf,
  type Starter,
  type StarterFlow,
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
          A starter chooses the review flow for new work in {project.name}, such as assay results,
          lot release testing or experiments, and assigns the lab roles needed to run it.
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
          s.procedure ? (
            <ProcedureStarterCard key={s.id} starter={s} project={project} />
          ) : (
            <StarterSetupCard key={s.id} starter={s} project={project} />
          )
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
  const flowLabel = (flow: string) => starter.flows.find((f) => f.id === flow)?.label ?? flow;
  if (!applied && starter.chosen) {
    // Chosen another way: by another starter, or through the API.
    return <Badge>Chosen: {flowLabel(starter.chosen.flow)}</Badge>;
  }
  if (!applied && starter.default) {
    return <Badge muted>Deployment default: {flowLabel(starter.default.flow)}</Badge>;
  }
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
          <p>{flow.summary}</p>
          <ResultFlow flow={flow} />
        </div>
      ))}
    </section>
  );
}

/** The flow a starter's result type follows, drawn: only this flow's route, where the type
 * has one per flow (ADR 0240). */
function ResultFlow({ flow: chosen }: { flow: StarterFlow }) {
  const type = chosen.result_type;
  const flow = useQuery(typeFlowQuery(type));
  const roles = useRoleLabels();
  const field = routeOf(flow.data?.fields[0], chosen.states);
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

/** A plugin starter's flow, drawn as the server compiles it for the governed type in this
 * project (ADR 0306): its route, who sets each state and who must be someone else. */
function ProcedureFlow({
  starter,
  flow: id,
  project,
}: {
  starter: Starter;
  flow: string;
  project: Project;
}) {
  const type = starter.types[0] ?? "";
  const drawn = useQuery(typeFlowQuery(type, { project: project.id }));
  const roles = useRoleLabels();
  if (drawn.isPending) return <Loading label="Loading its flow" />;
  const found = drawn.data?.review?.flows.find(
    (f) => f.procedure === starter.procedure && f.flow === id,
  );
  if (!found?.field) return null;
  return (
    <>
      <FlowDiagram flow={found.field} roles={roles} words="hidden" />
      <p className="starter-flow-more">
        <Link to="/types/$/" params={{ _splat: type }} search={{ project: project.slug }}>
          Every state and check of {drawn.data?.label ?? type}
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
  const [flowId, setFlowId] = useState(
    applied?.flow ?? starter.suggested ?? starter.flows[0]?.id ?? "research",
  );
  const flow = starter.flows.find((f) => f.id === flowId) ?? starter.flows[0];
  // Who's chosen for each role: until the Admin chooses, whoever holds it already.
  const [picked, setPicked] = useState<Record<string, string[]> | null>(null);
  const [plan, setPlan] = useState<{ key: string; plan: StarterPlan } | null>(null);
  // After a refused apply: how what applying does changed since it was previewed.
  const [changes, setChanges] = useState<{ key: string; body: string }[] | null>(null);

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
  // The roles whose holders sign independently of each other, as the server checks them.
  const signers = flow?.independent_roles ?? [];
  const independent = independentEnough(signers, chosen);
  const missing = required.filter((r) => !(chosen[r] ?? []).length);
  const setup: StarterSetup = {
    project: project.id,
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
    [STARTERS, membersQuery(project.id).queryKey],
  );
  const problem =
    (apply.error ?? preview.error) ? problemOf(apply.error ?? preview.error) : undefined;
  const memberById = new Map((members.data ?? []).map((m) => [m.id, m]));
  const nameOf = (member: string) => {
    const found = memberById.get(member);
    return found ? memberName(found) : "someone";
  };

  // What's missing is said when it's previewed, and stays till it's previewed again, as a
  // form's errors do: a choice made meanwhile doesn't move the choices or Preview.
  const form = useForm(null, preview.isPending || apply.isPending);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const checks: Record<string, string | false> = {
      independent:
        !missing.length &&
        !independent &&
        `Choose ${String(signers.length)} different people for ${listed(signers.map(label))}: each signs a different step, and someone who signed one can’t sign the next. One person may hold several roles, so long as each step can have its own signer.`,
    };
    for (const role of required) {
      checks[`role:${role}`] = !(chosen[role] ?? []).length && `Choose someone for ${label(role)}.`;
    }
    if (!form.validate(checks)) return;
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
            plan: { ...result, changed: false, grants_to_add: {} },
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
      <form {...form.props} onSubmit={submit}>
        <RadioField
          label="Review flow"
          value={flow?.id ?? "research"}
          onChange={(value) => {
            setFlowId(value);
          }}
          options={starter.flows.map((f) => ({
            value: f.id,
            label: f.id === applied?.flow ? `${f.label} (applied now)` : f.label,
            description: f.summary,
          }))}
        />
        {flow ? (
          <div className="starter-flow">
            <h3 className="starter-flow-title">How a {flow.label} record moves</h3>
            <ResultFlow flow={flow} />
          </div>
        ) : null}
        {applied && flow && applied.flow !== flow.id ? (
          <div className="alert alert-warning" role="note">
            <p>
              New {starter.governs} will use the {flow.label} flow. Existing ones keep their current
              review and signature requirements.
            </p>
          </div>
        ) : null}

        <h3 className="starter-step">Who does what</h3>
        {required.length ? (
          <p className="field-help starter-help">
            Choose members of {project.name} for each lab role. To add someone, open{" "}
            <Link to="/projects/$slug/members/" params={{ slug: project.slug }}>
              Members
            </Link>
            .
          </p>
        ) : (
          <p className="field-help starter-help">
            No one signs in {flow?.label ?? "this flow"}, so it grants no lab roles. Anyone who can
            run the import records results.
          </p>
        )}
        {members.isPending && required.length ? <Loading label="Loading members" /> : null}
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
              error={form.errors[`role:${role}`]}
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
        {form.errors["independent"] ? (
          <p className="field-error" role="alert">
            {form.errors["independent"]}
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
          />
        ) : null}
        <div className="actions">
          <Button
            type="submit"
            variant="primary"
            disabled={previewed !== null && !previewed.changed}
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

/**
 * A plugin's starter as a project's Admin applies it (ADRs 0302 to 0304): the review flow
 * for new records of its type, who plays each of the procedure's parts, and the members
 * who hold those roles. It starts from the project's choice, else the deployment's
 * default, else the flow the plugin suggests.
 */
function ProcedureStarterCard({ starter, project }: { starter: Starter; project: Project }) {
  const id = useId();
  const { say } = useMessages();
  const applied = starter.applied;
  const members = useQuery(membersQuery(project.id));
  const labRoles = useQuery(labRolesQuery);
  const [flowId, setFlowId] = useState(
    applied?.flow ?? starter.default?.flow ?? starter.suggested ?? starter.flows[0]?.id ?? "",
  );
  const flow = starter.flows.find((f) => f.id === flowId) ?? starter.flows[0];
  const slotsOf = useMemo(() => new Map(starter.slots.map((s) => [s.id, s])), [starter.slots]);
  // Who plays each part: as the project's choice (or the default) binds it, until changed.
  const [roles, setRoles] = useState<Record<string, string>>(() =>
    Object.fromEntries(starter.slots.map((s) => [s.id, s.role])),
  );
  const [picked, setPicked] = useState<Record<string, string[]> | null>(null);
  const [plan, setPlan] = useState<{ key: string; plan: StarterPlan } | null>(null);
  const [changes, setChanges] = useState<{ key: string; body: string }[] | null>(null);

  const roleLabels = useMemo(
    () => new Map((labRoles.data ?? []).map((r) => [r.name, r.label])),
    [labRoles.data],
  );
  const label = (role: string) =>
    roleLabels.get(role) ?? starter.roles.find((r) => r.id === role)?.label ?? role;
  const slots = (flow?.steps ?? [])
    .flatMap((step) => step.slots)
    .filter((slot, i, all) => all.indexOf(slot) === i)
    .flatMap((slot) => {
      const found = slotsOf.get(slot);
      return found ? [found] : [];
    });
  const minimumOf = (role: string) =>
    slots.some((s) => roles[s.id] === role && s.minimum_project_role === "editor")
      ? "editor"
      : "viewer";
  const bound = [...new Set(slots.map((s) => roles[s.id] ?? s.role))];
  const held = useMemo(() => {
    const out: Record<string, string[]> = {};
    for (const role of new Set(Object.values(roles))) {
      out[role] = (members.data ?? [])
        .filter((m) => m.lab_roles.some((g) => g.role === role))
        .map((m) => m.id);
    }
    return out;
  }, [members.data, roles]);
  const chosen = picked ?? held;
  const setup: StarterSetup = {
    project: project.id,
    version: starter.version,
    flow: flow?.id ?? "",
    role_assignments: Object.fromEntries(bound.map((r) => [r, [...(chosen[r] ?? [])].sort()])),
    slots: Object.fromEntries(
      starter.slots
        .filter((s) => roles[s.id] !== s.starter_role)
        .map((s) => [s.id, roles[s.id] ?? s.role]),
    ),
  };
  const key = JSON.stringify(setup);
  const previewed = plan?.key === key ? plan.plan : null;
  const preview = useApiMutation((s: StarterSetup) => previewStarter(starter.id, s), []);
  const apply = useApiMutation(
    ({ setup: s, plan: reviewed }: { setup: StarterSetup; plan: string }) =>
      applyStarter(starter.id, s, reviewed),
    [STARTERS, membersQuery(project.id).queryKey],
  );
  const problem =
    (apply.error ?? preview.error) ? problemOf(apply.error ?? preview.error) : undefined;
  const memberById = new Map((members.data ?? []).map((m) => [m.id, m]));
  const nameOf = (member: string) => {
    const found = memberById.get(member);
    return found ? memberName(found) : "someone";
  };
  const form = useForm(null, preview.isPending || apply.isPending);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const checks: Record<string, string | false> = {};
    const unstaffed = (flow?.steps ?? []).filter(
      (step) => !step.slots.some((slot) => (chosen[roles[slot] ?? ""] ?? []).length),
    );
    for (const step of unstaffed) {
      checks[`step:${step.state}`] = `Choose someone who can move records to ${step.label}.`;
    }
    checks["independent"] =
      !unstaffed.length &&
      flow !== undefined &&
      !canStaff(flow, roles, chosen) &&
      "Choose enough different people that each step’s signer can be someone other than the signers it must be independent of. One person may play several parts, so long as that holds.";
    if (!form.validate(checks)) return;
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
          const now = error instanceof ApiError ? error.problem.plan : undefined;
          if (error instanceof ApiError && error.problem.code === "starter_plan_changed" && now) {
            apply.reset();
            setPlan({ key, plan: now });
            setChanges(planChanges(shown, now, label, nameOf));
          }
        },
        onSuccess: (result) => {
          setPlan({ key, plan: { ...result, changed: false, grants_to_add: {} } });
          say(
            result.changed
              ? `Applied ${starter.label} with the ${flow?.label ?? ""} flow.`
              : `${starter.label} was already set up this way.`,
          );
        },
      },
    );
  }

  const roleOptions = (labRoles.data ?? []).map((r) => ({ value: r.name, label: r.label }));
  const unstaffedErrors = Object.entries(form.errors).filter(([k]) => k.startsWith("step:"));
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
      {!applied && starter.chosen ? (
        <p className="muted">
          New {starter.governs} in {project.name} follow {starter.chosen.procedure}, chosen by{" "}
          {starter.chosen.chosen_by}. Applying this starter replaces that choice.
        </p>
      ) : null}
      {!applied && !starter.chosen && starter.default ? (
        <p className="muted">
          Until {project.name} chooses, new {starter.governs} follow the deployment’s default, set
          by {starter.default.chosen_by}: “{starter.default.reason}”
        </p>
      ) : null}
      <form {...form.props} onSubmit={submit}>
        <RadioField
          label="Review flow"
          value={flow?.id ?? ""}
          onChange={(value) => {
            setFlowId(value);
          }}
          options={starter.flows.map((f) => ({
            value: f.id,
            label:
              f.id === applied?.flow
                ? `${f.label} (applied now)`
                : f.id === starter.suggested && !applied
                  ? `${f.label} (suggested)`
                  : f.label,
            description: f.summary,
          }))}
        />
        {flow ? (
          <div className="starter-flow">
            <h3 className="starter-flow-title">How a {flow.label} record moves</h3>
            <ProcedureFlow starter={starter} flow={flow.id} project={project} />
          </div>
        ) : null}
        {applied && flow && applied.flow !== flow.id ? (
          <div className="alert alert-warning" role="note">
            <p>
              New {starter.governs} will use the {flow.label} flow. Existing ones keep the procedure
              they were made under.
            </p>
          </div>
        ) : null}

        <h3 className="starter-step">Who does what</h3>
        {slots.length ? (
          <p className="field-help starter-help">
            Choose the lab role that plays each part, and members of {project.name} who hold it. To
            add someone, open{" "}
            <Link to="/projects/$slug/members/" params={{ slug: project.slug }}>
              Members
            </Link>
            .
          </p>
        ) : (
          <p className="field-help starter-help">
            No one signs in {flow?.label ?? "this flow"}, so it grants no lab roles.
          </p>
        )}
        {members.isPending && slots.length ? <Loading label="Loading members" /> : null}
        {slots.map((slot) => {
          const role = roles[slot.id] ?? slot.role;
          const minimum = minimumOf(role);
          const options = eligible(members.data ?? [], minimum);
          return (
            <fieldset className="starter-slot" key={slot.id}>
              <legend className="starter-role">{slot.label}</legend>
              {slot.description ? <p className="field-help">{slot.description}</p> : null}
              {roleOptions.length > 1 ? (
                <SelectField
                  label={`Lab role that plays ${slot.label}`}
                  value={role}
                  onChange={(value) => {
                    setRoles({ ...roles, [slot.id]: value });
                  }}
                  options={roleOptions}
                />
              ) : null}
              {options.length ? (
                <CheckboxesField
                  label={`Members who hold ${label(role)}`}
                  literal={false}
                  value={chosen[role] ?? []}
                  onChange={(value) => {
                    setPicked({ ...chosen, [role]: value });
                  }}
                  help={`Needs ${PROJECT_ROLE[minimum]} or higher.`}
                  options={options.map((m) => ({
                    value: m.id,
                    label: memberName(m),
                    description: [
                      PROJECT_ROLE[m.role],
                      m.lab_roles.some((g) => g.role === role)
                        ? `holds ${label(role)} already`
                        : "",
                    ]
                      .filter(Boolean)
                      .join(" · "),
                  }))}
                />
              ) : (
                <p className="field-error">
                  No one here can be {label(role)}: it needs a membership of their own as{" "}
                  {PROJECT_ROLE[minimum]} or higher.
                </p>
              )}
            </fieldset>
          );
        })}
        {unstaffedErrors.map(([k, message]) => (
          <p className="field-error" role="alert" key={k}>
            {message}
          </p>
        ))}
        {form.errors["independent"] ? (
          <p className="field-error" role="alert">
            {form.errors["independent"]}
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
            governs={starter.governs}
          />
        ) : null}
        <div className="actions">
          <Button
            type="submit"
            variant="primary"
            disabled={previewed !== null && !previewed.changed}
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

/** The flow and role grants the admin is about to apply. */
function Plan({
  plan,
  flowLabel,
  label,
  memberName,
  governs = "results",
}: {
  plan: StarterPlan;
  flowLabel: string;
  label: (role: string) => string;
  memberName: (membership: string) => string;
  governs?: string;
}) {
  const grants = Object.entries(plan.grants_to_add).flatMap(([role, ids]) =>
    ids.map((member) => `${memberName(member)} becomes ${label(role)}`),
  );
  return (
    <div className="starter-plan" role="status" aria-label="What applying will do">
      <h3 className="starter-step">What applying will do</h3>
      {!plan.changed ? (
        <p>This starter is already configured with these choices.</p>
      ) : (
        <ul>
          <li>
            {`New ${governs} follow the ${flowLabel} flow${plan.replaces ? ", replacing the project’s current choice" : ""}.`}
          </li>
          {grants.map((g) => (
            <li key={g}>{g}.</li>
          ))}
        </ul>
      )}
    </div>
  );
}
