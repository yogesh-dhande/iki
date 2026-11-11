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

import { iki, useApiMutation, type Project } from "../api";
import {
  inSentence,
  typeLabel,
  typesQuery,
  type Entity,
  type EntityType,
  type FieldDef,
} from "../entities";
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
import {
  actionsQuery,
  actionsTakingQuery,
  entitiesByHridQuery,
  MAX_BATCH,
  RUNS,
  type Action,
} from "../runs";
import { Button, buttonClass, Loading, SelectField, TextField, useMessages } from "../ui";
import { useProject } from "./project-overview";

type Input = Action["inputs"][number];

/** Whether entities of `type` can be given to an input taking `wanted`. */
function isA(types: EntityType[], type: string, wanted: string): boolean {
  if (wanted === type || wanted === "core.Entity") return true;
  return types.find((t) => t.name === wanted)?.subtypes.includes(type) ?? false;
}

/** The first input that takes all of `kinds`, as the server picks it. */
function inputTaking(action: Action, kinds: string[], types: EntityType[]): Input | undefined {
  return action.inputs.find((i) => kinds.every((k) => isA(types, k, i.type)));
}

const TIER: Record<Action["tier"], string> = {
  client: "on the computer of whoever starts it, with Iki’s worker",
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
function inputField(input: Input): FieldDef {
  return {
    name: input.name,
    label: input.label,
    description: input.description,
    type: input.cardinality === "many" ? "list" : "ref",
    items: input.cardinality === "many" ? "ref" : null,
    to: input.type,
    plugin: "",
    required: !input.optional,
    immutable: false,
    computed: false,
    unit: null,
    unit_symbol: null,
    display_unit: null,
    display_unit_symbol: null,
    display_scale: null,
    display_offset: null,
    input_units: [],
    values: [],
  };
}

const NO_RULES: Schemaed = { values_schema: { type: "object", properties: {} } };

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
  const given = useQuery({ ...entitiesByHridQuery(hrids), enabled: hrids.length > 0 });
  const entities = hrids.length ? given.data : [];
  const kinds = [...new Set((entities ?? []).map((e) => e.type))];
  const all = useQuery({ ...actionsQuery, enabled: hrids.length === 0 });
  const taking = useQuery({ ...actionsTakingQuery(kinds), enabled: kinds.length > 0 });
  const actions = hrids.length ? taking.data : all.data;

  if (hrids.length && given.isPending) return <Loading label="Loading the entities…" />;
  const missing = hrids.filter((h) => !(entities ?? []).some((e) => e.hrid === h));
  if (!entities || !actions) return <Loading label="Loading actions…" />;

  const runnable = actions.filter((a) => a.available && project.can.run.includes(a.tier));
  const notHere = actions.filter((a) => !a.available);
  const notYours = actions.filter((a) => a.available && !project.can.run.includes(a.tier));
  const action = runnable.find((a) => a.name === chosen);
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
  const [reason, setReason] = useState("");
  const batch = each?.cardinality === "one" && entities.length > 1;
  const tooMany = each?.cardinality === "many" && each.max !== null && entities.length > each.max;

  const launch = useApiMutation(
    async (body: {
      inputs: Record<string, string | string[]>;
      params: Record<string, unknown>;
    }) => {
      const common = { project: project.id, params: body.params, reason: reason.trim() };
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
        return made.runs;
      }
      const inputs = each
        ? {
            ...body.inputs,
            [each.name]:
              each.cardinality === "many" ? entities.map((e) => e.id) : (entities[0]?.id ?? ""),
          }
        : body.inputs;
      return [
        unwrap(
          await iki.POST("/api/v1/actions/{name}/runs", {
            params: { path: { name: action.name } },
            body: { ...common, inputs },
          }),
        ),
      ];
    },
    [RUNS],
  );
  const form = useForm(launch.error);

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
    if (tooMany) errors[`inputs/${each.name}`] = `This input takes at most ${String(each.max)}.`;
    if (!form.validate(errors)) return;
    launch.mutate(
      { inputs, params: values },
      {
        onSuccess: (runs) => {
          const first = runs[0];
          if (runs.length === 1 && first) {
            void navigate({ to: "/runs/$ref/", params: { ref: first.hrid } }).then(() => {
              say(`Started ${first.hrid}.`);
            });
          } else {
            void navigate({ to: "/projects/$slug/runs/", params: { slug: project.slug } }).then(
              () => {
                say(`Started ${String(runs.length)} runs of ${action.label || action.name}.`);
              },
            );
          }
        },
      },
    );
  }

  const changes = action.updates.map((u) => {
    const input = action.inputs.find((i) => i.name === u.input);
    const fields = types.find((t) => t.name === input?.type)?.fields ?? [];
    const labels = u.fields.map((f) => inSentence(fields.find((x) => x.name === f)?.label ?? f));
    return `its ${labels.join(", ")} on the ${inSentence(input?.label ?? u.input)}`;
  });
  const makes = action.creates.map((c) =>
    inSentence(
      c.cardinality === "many"
        ? (types.find((t) => t.name === c.type)?.label_plural ?? c.type)
        : typeLabel(types, c.type),
    ),
  );

  return (
    <form {...form.props} onSubmit={submit}>
      {form.alert}
      {action.description ? <p>{action.description}</p> : null}
      <ul className="run-facts">
        <li>
          It runs {TIER[action.tier]}
          {action.environment ? (
            <>
              , in the <code>{action.environment}</code> environment
            </>
          ) : null}
          , for up to {duration(action.timeout)} each time.
        </li>
        {changes.length ? <li>It changes {changes.join("; ")}.</li> : null}
        {makes.length ? <li>It makes new {makes.join(", ")}.</li> : null}
        {action.network.length ? (
          <li>It can reach {action.network.join(", ")} on the internet.</li>
        ) : (
          <li>It can’t reach the internet.</li>
        )}
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
          projectId={project.id}
        />
      ))}
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
      <div className="actions">
        <Button type="submit" variant="primary" busy={launch.isPending} busyText="Starting…">
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
    </form>
  );
}
