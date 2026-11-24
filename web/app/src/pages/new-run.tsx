/**
 * Starting a run: choose an action, fill in its inputs and params, and say
 * why. It opens from an entity's page (or a selection in the grid) with the
 * entities named in its address, `?entities=PRT-000001,PRT-000002`, and
 * offers only the actions that take them.
 *
 * Several entities for an input that takes one make one run for each, all
 * or none (the server checks every one before queuing any).
 */

import { unwrap } from "@ikisci/sdk";
import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useState, type SubmitEvent } from "react";

import { iki, problemOf, useApiMutation, type Project } from "../api";
import {
  inSentence,
  recordQuery,
  typeLabel,
  typesQuery,
  type Entity,
  type EntityType,
  type FieldDef,
} from "../entities";
import { parseLabels } from "../compute";
import { money } from "../costs";
import { FieldInput } from "../entity-inputs";
import {
  blankMessage,
  encode,
  initialDraft,
  isBlank,
  type Draft,
  type Schemaed,
} from "../entity-values";
import { useForm } from "../forms";
import { usePageTitle } from "../layout";
import { consentFor, SendsOutside, useSending } from "../outside-apis";
import {
  actionsQuery,
  actionsTakingQuery,
  MAX_BATCH,
  RUNS,
  runEntitiesQuery,
  type Action,
} from "../runs";
import {
  Button,
  buttonClass,
  ConfirmDialog,
  Loading,
  SelectField,
  TextField,
  useMessages,
} from "../ui";
import { useProject } from "./project-overview";

const AND = new Intl.ListFormat("en", { type: "conjunction" });

export type Input = Action["inputs"][number];

interface Launch {
  inputs: Record<string, string | string[]>;
  params: Record<string, unknown>;
  /** Link inputs from other projects into this one first (ADR 0066). */
  addToProject: boolean;
}

/** "PRT-000001", "PRT-000001 and PRT-000002", "PRT-000001, PRT-000002 and PRT-000003". */
export function listed(items: string[]): string {
  return items.length > 1
    ? `${items.slice(0, -1).join(", ")} and ${items.at(-1) ?? ""}`
    : (items[0] ?? "");
}

/** Whether entities of `type` can be given to an input taking `wanted`. */
function isA(types: EntityType[], type: string, wanted: string): boolean {
  if (wanted === type || wanted === "iki/core.Entity") return true;
  return types.find((t) => t.name === wanted)?.subtypes.includes(type) ?? false;
}

/** The first input that takes all of `kinds`, as the server picks it. */
function inputTaking(action: Action, kinds: string[], types: EntityType[]): Input | undefined {
  return action.inputs.find((i) => kinds.every((k) => isA(types, k, i.type)));
}

const TIER: Record<Action["tier"], string> = {
  self_hosted: "on a machine its team registered with Iki",
  backend: "on one of Iki’s backend workers",
  container: "in a container on the deployment’s compute",
  vm: "on a virtual machine of its own",
};

function duration(seconds: number): string {
  if (seconds < 120) return `${String(seconds)} seconds`;
  if (seconds < 2 * 3600) return `${String(Math.round(seconds / 60))} minutes`;
  return `${String(Math.round(seconds / 3600))} hours`;
}

/** As a field the entity form's inputs know: a reference, or a list of them. */
/** What a run may cost (ADR 0070): typically, from its recent runs, and at most, for its
 * timeout, on the machine it would run on. */
function Estimate({ estimate, runs }: { estimate: NonNullable<Action["estimate"]>; runs: number }) {
  const { currency } = estimate;
  const each = runs > 1 ? " each" : "";
  const most = `at most ${money(estimate.timeout_cost, currency)}${each} if it runs for its whole timeout`;
  if (estimate.typical === null) {
    return (
      <>
        Its machine costs {money(estimate.price_per_hour, currency)} an hour: {most}.
      </>
    );
  }
  return (
    <>
      A run typically costs about {money(estimate.typical, currency)}
      {runs > 1 ? `, ${money(estimate.typical * runs, currency)} for all ${String(runs)}` : ""}{" "}
      <span className="muted">(the median of its last {estimate.runs_seen} that succeeded)</span>,
      and {most}.
    </>
  );
}

export function inputField(input: Input): FieldDef {
  return {
    role: null,
    role_label: null,
    assignment: false,
    assignment_field: null,
    assignment_states: [],
    name: input.name,
    label: input.label,
    description: input.description,
    type: input.cardinality === "many" ? "list" : "ref",
    items: input.cardinality === "many" ? "ref" : null,
    to: [input.type],
    plugin: "",
    required: !input.optional,
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
}

export const NO_RULES: Schemaed = { values_schema: { type: "object", properties: {} } };

export function NewRun() {
  const project = useProject();
  const search = useSearch({ from: "/projects/$slug/runs/new" });
  const types = useSuspenseQuery(typesQuery).data;
  const hrids = search.entities ? [...new Set(search.entities.split(","))] : [];
  usePageTitle(`Run an action · ${project.name}`);

  if (project.can.run.length === 0) {
    return (
      <section className="card form-card" aria-labelledby="new-run-title">
        <h2 className="section-title" id="new-run-title">
          Run an action
        </h2>
        <p>
          {project.archived
            ? `${project.name} is archived, so nothing new can run in it.`
            : `You can’t run actions in ${project.name}. Its Editors run actions on backend workers and their own computers, and its Runners on containers and virtual machines too. A project Admin can change your role.`}
        </p>
      </section>
    );
  }
  if (hrids.length > MAX_BATCH) {
    return (
      <section className="card form-card" aria-labelledby="new-run-title">
        <h2 className="section-title" id="new-run-title">
          Run an action
        </h2>
        <p>
          That’s {hrids.length} entities: an action runs on at most {MAX_BATCH} at once. Choose
          fewer.
        </p>
      </section>
    );
  }
  return <Chooser project={project} hrids={hrids} types={types} chosen={search.action} />;
}

function Chooser({
  project,
  hrids,
  types,
  chosen,
}: {
  project: Project;
  hrids: string[];
  types: EntityType[];
  chosen: string | undefined;
}) {
  const navigate = useNavigate({ from: "/projects/$slug/runs/new" });
  const given = useQuery({ ...runEntitiesQuery(hrids, project.id), enabled: hrids.length > 0 });
  const entities = hrids.length ? given.data?.entities : [];
  const ambiguous = given.data?.ambiguous ?? [];
  const kinds = [...new Set((entities ?? []).map((e) => e.type))];
  const all = useQuery({ ...actionsQuery, enabled: hrids.length === 0 });
  const taking = useQuery({ ...actionsTakingQuery(kinds), enabled: kinds.length > 0 });
  const actions = hrids.length ? taking.data : all.data;

  if (hrids.length && given.isPending) return <Loading label="Loading the entities…" />;
  const missing = hrids.filter(
    (h) => !ambiguous.includes(h) && !(entities ?? []).some((e) => e.hrid === h),
  );
  if (!entities || !actions) return <Loading label="Loading actions…" />;

  const runnable = actions.filter((a) => a.available && project.can.run.includes(a.tier));
  const notHere = actions.filter((a) => !a.available);
  const notYours = actions.filter((a) => a.available && !project.can.run.includes(a.tier));
  const action = runnable.find((a) => a.name === chosen);
  // Opened for one that can't run, from a link: why, before anything is filled in.
  const blocked = [...notHere, ...notYours].find((a) => a.name === chosen);
  const why = !blocked
    ? null
    : blocked.available
      ? `${blocked.label || blocked.name} needs the ${blocked.needs} role in ${project.name}.`
      : `${blocked.label || blocked.name} can’t run here. ${blocked.unavailable_reason ?? "It can’t run on this deployment."}`;
  const about =
    kinds.length === 1
      ? inSentence(
          entities.length === 1
            ? typeLabel(types, kinds[0] ?? "")
            : (types.find((t) => t.name === kinds[0])?.label_plural ?? kinds[0] ?? ""),
        )
      : "these entities";

  return (
    <section className="card form-card" aria-labelledby="new-run-title">
      <h2 className="section-title" id="new-run-title">
        Run an action
      </h2>
      <p>
        In {project.name}: entities it makes belong to {project.name}, and the run is recorded with
        who started it, and why.
      </p>
      {missing.length ? (
        <div className="alert alert-warning" role="alert">
          <p>
            {missing.join(", ")} {missing.length === 1 ? "isn’t" : "aren’t"} an entity you can read,
            so {missing.length === 1 ? "it’s" : "they’re"} left out.
          </p>
        </div>
      ) : null}
      {ambiguous.length ? (
        <div className="alert alert-warning" role="alert">
          <p>
            {listed(ambiguous)} {ambiguous.length === 1 ? "is" : "are"} in more than one of your
            other projects, so {ambiguous.length === 1 ? "it’s" : "they’re"} left out: add the
            record you mean to {project.name}, then start the run here.
          </p>
        </div>
      ) : null}
      {why ? (
        <div className="alert alert-warning" role="alert">
          <p>{why}</p>
        </div>
      ) : null}
      {runnable.length === 0 ? (
        <p className="empty">
          {hrids.length
            ? `No action you can run here takes ${about}.`
            : "No action you can run here is installed."}
        </p>
      ) : (
        <SelectField
          label="Action"
          value={action?.name ?? ""}
          options={[
            { value: "", label: "Choose an action" },
            ...runnable.map((a) => ({ value: a.name, label: a.label || a.name })),
          ]}
          onChange={(value) => {
            void navigate({
              search: (prev) => ({ ...prev, action: value || undefined }),
              replace: true,
            });
          }}
        />
      )}
      {notHere.length || notYours.length ? (
        <details className="disclosure">
          <summary>
            {notHere.length + notYours.length} more{" "}
            {notHere.length + notYours.length === 1 ? "action" : "actions"} you can’t run here
          </summary>
          <ul>
            {notYours.map((a) => (
              <li key={a.name}>
                {a.label || a.name}: needs the {a.needs} role in {project.name}.
              </li>
            ))}
            {notHere.map((a) => (
              <li key={a.name}>
                {a.label || a.name}: {a.unavailable_reason ?? "it can’t run on this deployment."}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {action ? (
        <RunForm
          key={action.name}
          project={project}
          action={action}
          entities={entities}
          each={entities.length ? inputTaking(action, kinds, types) : undefined}
          types={types}
        />
      ) : (
        <div className="actions">
          <Link
            to="/projects/$slug/runs/"
            params={{ slug: project.slug }}
            className={buttonClass("quiet")}
          >
            Cancel
          </Link>
        </div>
      )}
    </section>
  );
}

function Given({ entities, error }: { entities: Entity[]; error: string | undefined }) {
  const shown = entities.slice(0, 8);
  return (
    <>
      {error ? <p className="field-error">{error}</p> : null}
      <ul className="value-list">
        {shown.map((e) => (
          <li key={e.id}>
            <Link to="/entities/$ref/" params={{ ref: e.hrid }}>
              {e.hrid}
            </Link>
            {e.title !== e.hrid ? <span className="muted"> {e.title}</span> : null}
          </li>
        ))}
      </ul>
      {entities.length > shown.length ? (
        <p className="muted">and {entities.length - shown.length} more</p>
      ) : null}
    </>
  );
}

function RunForm({
  project,
  action,
  entities,
  each,
  types,
}: {
  project: Project;
  action: Action;
  entities: Entity[];
  each: Input | undefined;
  types: EntityType[];
}) {
  const navigate = useNavigate();
  const { say } = useMessages();
  const params: Schemaed = { values_schema: action.params_schema };
  const others = action.inputs.filter((i) => i.name !== each?.name);
  const [drafts, setDrafts] = useState<Record<string, Draft>>(() => ({
    ...Object.fromEntries(
      others.map((i) => [`inputs/${i.name}`, i.cardinality === "many" ? [] : null]),
    ),
    ...Object.fromEntries(action.params.map((p) => [`params/${p.name}`, initialDraft(p, params)])),
  }));
  const reviewsPrep = action.name === "iki/lots.record_plasmid_verification";
  const alignment = drafts["inputs/alignment"];
  const selectedAlignment =
    reviewsPrep && alignment && typeof alignment === "object" && "id" in alignment
      ? alignment.id
      : "";
  const alignmentRecord = useQuery({
    ...recordQuery(selectedAlignment, project.id),
    enabled: Boolean(selectedAlignment),
  });
  const evidence =
    reviewsPrep && each?.name === "alignment" && entities.length === 1
      ? entities[0]
      : alignmentRecord.data;
  const verdict = evidence?.values["verdict"];
  const overridesVerdict =
    drafts["params/outcome"] === "match" && (verdict === "differs" || verdict === "incomplete");
  let verdictNote: string | undefined;
  if (evidence) {
    verdictNote =
      typeof verdict === "string"
        ? `The alignment reports “${verdict}”.`
        : "The alignment has no reported verdict.";
  } else if (selectedAlignment) {
    verdictNote = alignmentRecord.isError
      ? "The alignment’s verdict could not be loaded."
      : "Loading the alignment’s verdict…";
  }
  const [reason, setReason] = useState("");
  const [labels, setLabels] = useState("");
  const [agreed, setAgreed] = useState(false);
  const sending = useSending(project.id, action.apis);
  const selfHosted = action.tier === "self_hosted";
  const batch = each?.cardinality === "one" && entities.length > 1;
  const tooMany = each?.cardinality === "many" && each.max !== null && entities.length > each.max;

  const launch = useApiMutation(
    async (body: Launch) => {
      const common = {
        project: project.id,
        params: body.params,
        reason: reason.trim(),
        reuse: true,
        add_to_project: body.addToProject,
        labels: selfHosted ? parseLabels(labels) : [],
        consent: agreed ? consentFor(action.apis) : [],
      };
      if (batch) {
        const made = unwrap(
          await iki.POST("/api/v1/actions/{name}/runs/batch", {
            params: { path: { name: action.name } },
            body: {
              ...common,
              each: each.name,
              entities: entities.map((e) => e.id),
              inputs: body.inputs,
            },
          }),
        );
        return { runs: made.runs, batch: made.batch };
      }
      const inputs = each
        ? {
            ...body.inputs,
            [each.name]:
              each.cardinality === "many" ? entities.map((e) => e.id) : (entities[0]?.id ?? ""),
          }
        : body.inputs;
      return {
        runs: [
          unwrap(
            await iki.POST("/api/v1/actions/{name}/runs", {
              params: { path: { name: action.name } },
              body: { ...common, inputs },
            }),
          ),
        ],
        batch: undefined,
      };
    },
    [RUNS],
  );
  const form = useForm(launch.error, launch.isPending);
  // A launch refused for inputs from other projects, to send again adding them.
  const [outside, setOutside] = useState<{ body: Launch; hrids: string[] } | null>(null);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const errors: Record<string, string> = {};
    const inputs: Record<string, string | string[]> = {};
    for (const input of others) {
      const key = `inputs/${input.name}`;
      const field = inputField(input);
      const draft = drafts[key] ?? null;
      if (isBlank(field, draft)) {
        if (!input.optional) errors[key] = `Choose ${inSentence(input.label)}.`;
        continue;
      }
      const encoded = encode(field, draft, NO_RULES);
      if (encoded.ok) inputs[input.name] = encoded.value as string | string[];
      else errors[key] = encoded.error;
    }
    const values: Record<string, unknown> = {};
    for (const param of action.params) {
      const key = `params/${param.name}`;
      const draft = drafts[key] ?? null;
      if (isBlank(param, draft)) {
        if (param.required && param.default === null) errors[key] = blankMessage(param);
        continue;
      }
      const encoded = encode(param, draft, params);
      if (encoded.ok) values[param.name] = encoded.value;
      else errors[key] = encoded.error;
    }
    const overrideReason = values["override_reason"];
    if (
      overridesVerdict &&
      !errors["params/override_reason"] &&
      (typeof overrideReason !== "string" || !overrideReason.trim())
    ) {
      errors["params/override_reason"] =
        "Give a reason to record a match over differing or incomplete evidence.";
    }
    if (tooMany) errors[`inputs/${each.name}`] = `This input takes at most ${String(each.max)}.`;
    if (action.apis.length && !agreed) {
      errors["consent"] = "Agree to send this data, or don’t start the run.";
    }
    if (!form.validate(errors)) return;
    start({ inputs, params: values, addToProject: false });
  }

  function start(body: Launch) {
    launch.mutate(body, {
      onError: (error) => {
        const problem = problemOf(error);
        if (problem.code === "inputs_outside_project") {
          setOutside({ body, hrids: (problem.entities ?? []).map((e) => e.hrid) });
        }
      },
      onSuccess: ({ runs, batch: made }) => {
        setOutside(null);
        const first = runs[0];
        if (made) {
          const reused = runs.filter((r) => r.reused).length;
          void navigate({ to: "/batches/$ref/", params: { ref: made.hrid } }).then(() => {
            say(
              `Started ${String(runs.length)} runs of ${action.label || action.name}, as ${made.hrid}` +
                (reused
                  ? `. ${String(reused)} reused the results of identical runs, so ran nothing.`
                  : "."),
            );
          });
        } else if (first) {
          void navigate({ to: "/runs/$ref/", params: { ref: first.hrid } }).then(() => {
            say(
              first.reused
                ? `${first.hrid} reused the results of ${first.reused_from?.hrid ?? "an identical run"}.`
                : `Started ${first.hrid}.`,
            );
          });
        }
      },
    });
  }

  const changes = action.updates.map((u) => {
    const input = action.inputs.find((i) => i.name === u.input);
    const fields = types.find((t) => t.name === input?.type)?.fields ?? [];
    const labels = u.fields.map((f) => inSentence(fields.find((x) => x.name === f)?.label ?? f));
    return `its ${labels.join(", ")} on the ${inSentence(input?.label ?? u.input)}`;
  });
  const makes = action.creates.map((c) =>
    c.cardinality === "many"
      ? inSentence(types.find((t) => t.name === c.type)?.label_plural ?? c.type)
      : `one ${inSentence(typeLabel(types, c.type))}`,
  );

  return (
    <form {...form.props} onSubmit={submit}>
      {form.alert}
      {action.description ? <p>{action.description}</p> : null}
      <ul className="run-facts">
        <li>
          It runs {TIER[action.tier]}
          {action.labels.length ? (
            <>
              {" "}
              labelled{" "}
              {action.labels.map((label, index) => (
                <span key={label}>
                  {index ? ", " : null}
                  <code>{label}</code>
                </span>
              ))}
            </>
          ) : null}
          {action.environment ? (
            <>
              , in the <code>{action.environment}</code> environment
            </>
          ) : null}
          , for up to {duration(action.timeout)} each time.
        </li>
        {action.estimate ? (
          <li>
            <Estimate estimate={action.estimate} runs={batch ? entities.length : 1} />
          </li>
        ) : null}
        {changes.length ? <li>It changes {changes.join("; ")}.</li> : null}
        {makes.length ? <li>It makes new {AND.format(makes)}.</li> : null}
        {action.network.length ? (
          <li>It can reach {action.network.join(", ")} on the internet.</li>
        ) : action.apis.length ? null : (
          <li>It can’t reach the internet.</li>
        )}
        {action.apis.some((a) => a.priced) ? (
          <li>Its calls to outside APIs cost money, counted with the run’s cost.</li>
        ) : null}
        {action.apis.some((a) => a.parks) ? (
          <li>While an outside API works on it, it waits without holding a machine.</li>
        ) : null}
        {action.secrets.some((secret) => secret.set) ? (
          <li>
            It gets{" "}
            {action.secrets
              .filter((secret) => secret.set)
              .map((secret) => secret.name)
              .join(", ")}{" "}
            from this deployment’s secrets, which its log never shows.
          </li>
        ) : null}
      </ul>
      {each ? (
        <fieldset className="field">
          <legend>{each.label}</legend>
          {batch ? (
            <p className="field-help">
              One run for each of these {entities.length}: if any can’t run, none is started.
            </p>
          ) : null}
          <Given
            entities={entities}
            error={form.errors[`inputs/${each.name}`] ?? form.errors["entities"]}
          />
        </fieldset>
      ) : null}
      {others.map((input) => (
        <FieldInput
          key={input.name}
          field={inputField(input)}
          type={NO_RULES}
          types={types}
          draft={drafts[`inputs/${input.name}`] ?? null}
          onChange={(draft) => {
            setDrafts((d) => ({ ...d, [`inputs/${input.name}`]: draft }));
          }}
          error={form.errors[`inputs/${input.name}`]}
          projectId={project.id}
        />
      ))}
      {action.params.map((param) => (
        <FieldInput
          key={param.name}
          field={param}
          type={params}
          types={types}
          draft={drafts[`params/${param.name}`] ?? null}
          onChange={(draft) => {
            setDrafts((d) => ({ ...d, [`params/${param.name}`]: draft }));
          }}
          error={form.errors[`params/${param.name}`]}
          note={param.name === "outcome" ? verdictNote : undefined}
          projectId={project.id}
        />
      ))}
      {selfHosted ? (
        <TextField
          label="Machine labels"
          value={labels}
          onChange={setLabels}
          error={form.errors["labels"]}
          optional
          help="Only a machine with all of these takes it, such as lab-3 for one lab’s instrument, separated by commas."
        />
      ) : null}
      <TextField
        label="Reason"
        value={reason}
        onChange={setReason}
        error={form.errors["reason"]}
        maxLength={2000}
        optional
        multiline
        help="Why, for the record. It’s kept with the run, and with what it changes."
      />
      <SendsOutside
        projectName={project.name}
        statements={action.apis}
        missing={sending.unapproved}
        agreed={agreed}
        onAgree={setAgreed}
        error={form.errors["consent"]}
      />
      <div className="actions">
        <Button
          type="submit"
          variant="primary"
          busy={launch.isPending}
          busyText="Starting…"
          disabled={sending.blocked}
        >
          {batch ? `Start ${String(entities.length)} runs` : "Start run"}
        </Button>
        <Link
          to="/projects/$slug/runs/"
          params={{ slug: project.slug }}
          className={buttonClass("quiet")}
        >
          Cancel
        </Link>
      </div>
      <ConfirmDialog
        isOpen={outside !== null}
        title={`Add to ${project.name}?`}
        confirmLabel={batch ? `Add and start ${String(entities.length)} runs` : "Add and start run"}
        confirmVariant="primary"
        busy={launch.isPending}
        problem={
          launch.error && problemOf(launch.error).code !== "inputs_outside_project"
            ? problemOf(launch.error)
            : undefined
        }
        onCancel={() => {
          setOutside(null);
        }}
        onConfirm={() => {
          if (outside) start({ ...outside.body, addToProject: true });
        }}
      >
        <p>
          {listed(outside?.hrids ?? [])} {(outside?.hrids.length ?? 0) > 1 ? "are" : "is"} in other
          projects. A run reads only what’s in its project, so{" "}
          {(outside?.hrids.length ?? 0) > 1 ? "they’ll" : "it’ll"} be added to {project.name} first,
          and its members will be able to read {(outside?.hrids.length ?? 0) > 1 ? "them" : "it"}.
        </p>
        <p>Adding is recorded in each project’s audit log.</p>
      </ConfirmDialog>
    </form>
  );
}
