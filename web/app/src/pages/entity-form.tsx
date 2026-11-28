/**
 * Creating and editing entities, with forms built from each type's fields.
 *
 * - A new entity's form starts from its fields' defaults. A type with an
 *   identity registers it in the project's registry (ADR 0115): the form
 *   lands on the project's record and says whether that made a new registry
 *   entry, gave the project its own record of one there already, or opened
 *   the record the project had.
 * - An edit can't change a registry entry's values once they're set: its
 *   identity, what's derived from it, and its facts, which only the
 *   registry's registrars change.
 * - An edit sends only the fields that changed, with the revision it
 *   started from (If-Match). If someone else saved in between, nothing is
 *   overwritten: their values come in for the fields this person didn't
 *   touch, the form says what changed, and they save again.
 * - Its plugins' rules (ADR 0127) are checked as it's typed, each field saying
 *   which it breaks. A rule that holds records for review, such as a result's
 *   specification, asks why to save it anyway: it's saved, and held.
 * - Leaving an edit with changes unsaved (a link, Back, closing the tab) asks
 *   first; the save goes to the record without asking.
 */

import { ApiError, unwrap } from "@ikisci/sdk";
import { useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useState, type ReactNode, type SubmitEvent } from "react";

import { iki, useApiMutation } from "../api";
import {
  ENTITIES,
  editReturns,
  entityQuery,
  inSentence,
  recordSearch,
  stateLabel,
  typesQuery,
  type Entity,
  type EntityType,
  type FieldDef,
} from "../entities";
import { FieldInput } from "../entity-inputs";
import {
  blankMessage,
  encode,
  formFields,
  initialDraft,
  isBlank,
  same,
  toDraft,
  type Draft,
} from "../entity-values";
import { useForm } from "../forms";
import { usePageTitle } from "../layout";
import {
  Button,
  buttonClass,
  ConfirmDialog,
  PageHeader,
  SelectField,
  TextField,
  useLeaveGuard,
  useMessages,
} from "../ui";
import { APP_TYPE } from "../apps";
import { createdMessage } from "../registries";
import { fieldLabels, isServiceField, listed, writableFields } from "../requests";

import { breakErrors, useRuleBreaks, type RuleBreak } from "../rules";

import { AppPreview } from "./app-preview";
import { useProject } from "../project";

type Drafts = Record<string, Draft>;

function draftsOf(type: EntityType, entity: Entity): Drafts {
  return Object.fromEntries(
    formFields(type).map((f) => [f.name, toDraft(f, entity.values[f.name], entity)]),
  );
}

/** Each field's value to send, or the checks' messages, by `values/<name>` as the API points. */
function encodeAll(
  type: EntityType,
  fields: FieldDef[],
  drafts: Drafts,
): { values: Record<string, unknown>; errors: Record<string, string> } {
  const values: Record<string, unknown> = {};
  const errors: Record<string, string> = {};
  for (const field of fields) {
    const draft = drafts[field.name] ?? null;
    if (field.required && isBlank(field, draft)) {
      errors[`values/${field.name}`] = blankMessage(field);
      continue;
    }
    const encoded = encode(field, draft, type);
    if (encoded.ok) values[field.name] = encoded.value;
    else errors[`values/${field.name}`] = encoded.error;
  }
  return { values, errors };
}

/** For a request between projects (ADR 0132), the fields the form leaves to the other side,
 * and whose they are. */
function otherSide(type: EntityType, entity?: Entity): string | null {
  const sides = entity?.request;
  const service = sides?.service_fields ?? type.request?.service_fields;
  if (!service) return null;
  const offered = new Set(writableFields(type, entity).map((f) => f.name));
  const left = formFields(type).filter((f) => !offered.has(f.name));
  if (!left.length) return null;
  const labels = fieldLabels(
    type,
    left.map((f) => f.name),
  );
  const asker = sides?.asking_project?.name ?? "The project that asks";
  if (!sides?.service) {
    return `${labels} ${left.length === 1 ? "is" : "are"} for the service it’s sent to: it fills ${left.length === 1 ? "it" : "them"} in once it’s sent.`;
  }
  return sides.yours.includes("service")
    ? `${labels} ${left.length === 1 ? "is" : "are"} ${asker}’s: its Editors change ${left.length === 1 ? "it" : "them"}.`
    : `${labels} ${left.length === 1 ? "is" : "are"} ${sides.service.name}’s: it fills ${left.length === 1 ? "it" : "them"} in.`;
}

function Fields({
  type,
  fields,
  types,
  drafts,
  setDraft,
  errors,
  notes,
  locked,
  projectId,
  creating = false,
  others,
}: {
  type: EntityType;
  /** The fields to offer: for a request, only its reader's side's. */
  fields: FieldDef[];
  /** What the form leaves to someone else, and why. */
  others?: string | null;
  types: EntityType[];
  drafts: Drafts;
  setDraft: (name: string, draft: Draft) => void;
  errors: Record<string, string>;
  notes: Record<string, string>;
  locked: Set<string>;
  /** The project a field's people and files are from: for a request's service fields (ADR
   * 0132), the service's. */
  projectId: string | ((field: FieldDef) => string);
  /** A new record's: its state fields start in their initial states. */
  creating?: boolean;
}) {
  const computed = type.fields.filter((f) => f.computed && !f.detached);
  const kept = type.fields.filter((f) => f.detached);
  // State fields (ADR 0125) start where their type says, and are set on the record's page.
  const stateful = type.fields.filter((f) => f.type === "state" && !f.detached);
  // A new request's service states are the service's, set once it's sent (ADR 0132): not
  // said to start in their initial state, whose name may say it's been sent.
  const service = creating ? (type.request?.service_fields ?? []) : [];
  const states = stateful.filter((f) => !service.includes(f.name));
  const served = stateful.filter((f) => service.includes(f.name));
  const keptFrom = [...new Set(kept.map((f) => f.plugin))].join(", ");
  return (
    <>
      {fields.map((field) => (
        <FieldInput
          key={field.name}
          field={field}
          type={type}
          types={types}
          draft={drafts[field.name] ?? null}
          onChange={(draft) => {
            setDraft(field.name, draft);
          }}
          error={errors[`values/${field.name}`]}
          errors={errors}
          note={notes[field.name]}
          disabled={locked.has(field.name)}
          projectId={typeof projectId === "string" ? projectId : projectId(field)}
        />
      ))}
      {computed.length ? (
        <p className="muted">
          Actions set {computed.map((f) => inSentence(f.label)).join(", ")}: they aren’t typed in.
        </p>
      ) : null}
      {states.length ? (
        <p className="muted">
          {creating
            ? `${states.map((f) => `${f.label} is ${stateLabel(f, f.initial ?? "")}`).join(", ")} to begin with: `
            : `${states.map((f) => f.label).join(", ")}: `}
          {states.length === 1 ? "it’s" : "they’re"} set on its page, by the lab roles its states
          name.
        </p>
      ) : null}
      {served.length ? (
        <p className="muted">
          {served.map((f) => f.label).join(", ")} {served.length === 1 ? "is" : "are"} the
          service’s: {served.length === 1 ? "it’s" : "they’re"} set once the request is sent.
        </p>
      ) : null}
      {kept.length ? (
        <p className="muted">
          {kept.map((f) => f.label).join(", ")} {kept.length === 1 ? "is" : "are"} from the{" "}
          {keptFrom} plugin, which is detached:{" "}
          {kept.length === 1 ? "it keeps its value" : "they keep their values"}.
        </p>
      ) : null}
      {others ? <p className="muted">{others}</p> : null}
    </>
  );
}

function Reason({
  value,
  onChange,
  error,
  required = false,
}: {
  value: string;
  onChange: (value: string) => void;
  error: string | undefined;
  required?: boolean;
}) {
  return (
    <TextField
      label="Reason"
      value={value}
      onChange={onChange}
      error={error}
      maxLength={2000}
      optional={!required}
      multiline
      help="Why, for the record. It’s kept in the entity’s history."
    />
  );
}

/** The values typed so far that are valid, for checking rules against. */
function typedValues(type: EntityType, drafts: Drafts): Record<string, unknown> {
  return encodeAll(type, formFields(type), drafts).values;
}

/** Whether a save needs a hold reason: the rules it breaks hold records, or the server
 * said so. */
function needsHold(found: RuleBreak[], error: unknown): boolean {
  if (error instanceof ApiError && error.problem.code === "hold_reason_required") return true;
  return found.length > 0 && found.every((b) => b.hold);
}

/** The checks a save makes of the rules it breaks: those that refuse it, by field, and a
 * reason for those that hold it. */
function ruleChecks(found: RuleBreak[], hold: boolean, reason: string): Record<string, string> {
  const errors = breakErrors(found.filter((b) => !b.hold));
  if (hold && !reason.trim()) {
    errors["hold_reason"] = "Say why it’s saved though it breaks these rules.";
  }
  return errors;
}

function HoldReason({
  found,
  value,
  onChange,
  error,
}: {
  found: RuleBreak[];
  value: string;
  onChange: (value: string) => void;
  error: string | undefined;
}) {
  const labels = [...new Set(found.filter((b) => b.hold).map((b) => `“${b.label}”`))];
  return (
    <div className="alert alert-warning" role="note">
      <p>
        {labels.length
          ? `It breaks ${labels.join(" and ")}, which ${labels.length === 1 ? "holds" : "hold"} it for review.`
          : "It breaks rules that hold it for review."}{" "}
        It’s saved as it is, with your reason, and held until someone with the rule’s lab role,
        other than you, reviews it. While it’s held, it can’t be set to a state that locks it.
      </p>
      <TextField
        label="Why save it anyway"
        value={value}
        onChange={onChange}
        error={error}
        maxLength={2000}
        multiline
        help="Such as what was measured, and how. It’s kept with the hold, and its reviewer reads it."
      />
    </div>
  );
}

/** Where the form is: the project, and its registry. */
interface Context {
  project: string;
  registry: string;
}

/** A registry entry's value (ADR 0115) that an edit can't change: its identity, what's
 * derived from it, and a fact, once each is set. */
function entryValue(field: FieldDef): boolean {
  return field.registry !== null && field.registry !== undefined;
}

/** Rules people should know before typing: set once, unique, identity and registry. */
function notesFor(
  type: EntityType,
  editing: boolean,
  drafts: Drafts,
  where: Context,
): Record<string, string> {
  const notes: Record<string, string> = {};
  const label = inSentence(type.label);
  for (const f of formFields(type)) {
    const parts: string[] = [];
    const set = editing && !isBlank(f, drafts[f.name] ?? null);
    if (f.registry === "fact") {
      parts.push(
        set
          ? `A registry fact: only the ${where.registry} registry’s registrars change it.`
          : `A registry fact: every project with this ${label} has the same one. Once it’s set, only the ${where.registry} registry’s registrars change it.`,
      );
    } else if (f.registry === "derived") {
      parts.push(
        set
          ? "Derived from its identity, in its registry entry: it can’t change."
          : `Derived from its identity. If this ${label} is registered already, the entry’s value is kept.`,
      );
    } else if (f.immutable) {
      parts.push(
        set ? "Set once: it can’t change." : "It can be set once, and can’t change after.",
      );
    }
    if (f.unique) {
      parts.push(`No two ${type.label_plural.toLowerCase()} in a project have the same one.`);
    }
    if (!editing && type.identity.includes(f.name)) {
      parts.push(
        `If this ${label} is registered in ${where.registry} already, ${where.project} gets its own record of it, with the same ID.`,
      );
    }
    if (parts.length) notes[f.name] = parts.join(" ");
  }
  return notes;
}

// --- New ---------------------------------------------------------------------------

export function NewEntity() {
  const project = useProject();
  const types = useSuspenseQuery(typesQuery).data;
  const { type: name } = useSearch({ from: "/projects/$slug/entities/new" });
  const type = types.find((t) => t.name === name && !t.abstract && !t.detached);
  usePageTitle(`New ${type ? inSentence(type.label) : "entity"} · ${project.name}`);
  if (!type) return <ChooseType />;
  // A new form for each type: drafts don't carry between types.
  return <NewEntityForm key={type.name} type={type} types={types} />;
}

function ChooseType() {
  const project = useProject();
  const types = useSuspenseQuery(typesQuery).data;
  const navigate = useNavigate({ from: "/projects/$slug/entities/new" });
  const creatable = types
    .filter((t) => !t.abstract && !t.detached)
    .sort((a, b) => a.label.localeCompare(b.label));
  const [choice, setChoice] = useState(creatable[0]?.name ?? "");
  return (
    <section className="card form-card" aria-labelledby="new-entity-title">
      <h2 className="section-title" id="new-entity-title">
        New entity
      </h2>
      {creatable.length === 0 ? (
        <p className="empty">No plugins with entity types are installed.</p>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void navigate({ search: { type: choice } });
          }}
        >
          <SelectField
            label="Type"
            value={choice}
            onChange={setChoice}
            options={creatable.map((t) => ({ value: t.name, label: t.label }))}
          />
          <div className="actions">
            <Button type="submit" variant="primary">
              Continue
            </Button>
            <Link
              to="/projects/$slug/entities/"
              params={{ slug: project.slug }}
              className={buttonClass("quiet")}
            >
              Cancel
            </Link>
          </div>
        </form>
      )}
    </section>
  );
}

function NewEntityForm({ type, types }: { type: EntityType; types: EntityType[] }) {
  const project = useProject();
  const navigate = useNavigate();
  const { say } = useMessages();
  const [drafts, setDrafts] = useState<Drafts>(() =>
    Object.fromEntries(formFields(type).map((f) => [f.name, initialDraft(f, type)])),
  );
  const [reason, setReason] = useState("");
  const [holdReason, setHoldReason] = useState("");
  const found = useRuleBreaks(type, typedValues(type, drafts));

  const create = useApiMutation(
    async (values: Record<string, unknown>) =>
      unwrap(
        await iki.POST("/api/v1/entities", {
          body: {
            type: type.name,
            project: project.id,
            values,
            reason: reason.trim(),
            ...(hold ? { hold_reason: holdReason.trim() } : {}),
          },
        }),
      ),
    [ENTITIES],
  );
  const hold = needsHold(found, create.error);
  const form = useForm(create.error, create.isPending);
  const label = inSentence(type.label);
  const registers = type.identity.length > 0;
  // A new request is its project's alone: the service's fields wait until it's sent.
  const fields = writableFields(type);

  function send() {
    const { values, errors } = encodeAll(type, fields, drafts);
    if (!form.validate({ ...ruleChecks(found, hold, holdReason), ...errors })) return;
    // Blank fields are left out, so the type's defaults apply.
    const given = Object.fromEntries(
      Object.entries(values).filter(([, v]) => v !== null && !(Array.isArray(v) && !v.length)),
    );
    create.mutate(given, {
      onSuccess: (made) => {
        void navigate({
          to: "/entities/$ref/",
          params: { ref: made.hrid },
          search: recordSearch(made, project.slug),
        }).then(() => {
          say(createdMessage(made, project));
        });
      },
    });
  }

  return (
    <>
      <section className="card form-card" aria-labelledby="new-entity-title">
        {/* Under the project's heading and tabs, as its other pages are. */}
        <h2 className="section-title" id="new-entity-title">
          New {label}
        </h2>
        <p>
          {registers
            ? `Registered in the ${project.registry.name} registry, with one ID wherever it’s used. ${project.name}’s record of it is its own: its members can read it, and its Editors can change it.`
            : `In ${project.name}: its members can read it, and its Editors can change it.`}{" "}
          <Link to="/types/$/" params={{ _splat: type.name }}>
            About {inSentence(type.label_plural)}
          </Link>
        </p>
        {form.alert}
        <form
          {...form.props}
          onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
            event.preventDefault();
            send();
          }}
        >
          <Fields
            type={type}
            fields={fields}
            others={
              type.request
                ? [otherSide(type), "Once it’s made, send it to a service from its page."]
                    .filter(Boolean)
                    .join(" ")
                : null
            }
            types={types}
            drafts={drafts}
            setDraft={(name, draft) => {
              setDrafts((current) => ({ ...current, [name]: draft }));
            }}
            errors={{ ...breakErrors(found), ...form.errors }}
            creating
            notes={notesFor(type, false, drafts, {
              project: project.name,
              registry: project.registry.name,
            })}
            locked={new Set()}
            projectId={project.id}
          />
          {hold ? (
            <HoldReason
              found={found}
              value={holdReason}
              onChange={setHoldReason}
              error={form.errors["hold_reason"]}
            />
          ) : null}
          <Reason value={reason} onChange={setReason} error={form.errors["reason"]} />
          <div className="actions">
            <Button type="submit" variant="primary" busy={create.isPending} busyText="Creating…">
              Create {label}
            </Button>
            <Link
              to="/projects/$slug/entities/"
              params={{ slug: project.slug }}
              search={{ type: type.name }}
              className={buttonClass("quiet")}
            >
              Cancel
            </Link>
          </div>
        </form>
      </section>
      {type.name === APP_TYPE ? (
        <AppPreview type={type} types={types} drafts={drafts} project={project} />
      ) : null}
    </>
  );
}

// --- Edit --------------------------------------------------------------------------

/** Where its edit form is: Entities, its type's table, the record, then Edit. */
function editCrumbs(entity: Entity, type: EntityType | undefined): ReactNode[] {
  return [
    <Link to="/entities/">Entities</Link>,
    <Link to="/entities/types/$/" params={{ _splat: entity.type }} search={{}}>
      {type?.label_plural ?? entity.type}
    </Link>,
    <Link to="/entities/$ref/" params={{ ref: entity.hrid }} search={recordSearch(entity)}>
      {entity.hrid}
    </Link>,
    "Edit",
  ];
}

export function EditEntity() {
  const { ref } = useParams({ from: "/entities/$ref/edit" });
  const { project } = useSearch({ from: "/entities/$ref" });
  const entity = useSuspenseQuery(entityQuery(ref, project)).data;
  const types = useSuspenseQuery(typesQuery).data;
  const type = types.find((t) => t.name === entity.type);
  usePageTitle(`Edit ${entity.title} (${entity.hrid})`);
  if (!type || !(entity.can.edit || entity.can.correct) || entity.archived) {
    return (
      <section className="card form-card">
        <PageHeader crumbs={editCrumbs(entity, type)} title={`Edit ${entity.title}`} />
        <p>
          {!type
            ? `Its type, ${entity.type}, isn’t installed any more, so it can’t be edited.`
            : entity.archived
              ? "It’s archived, so it can’t change. Restore it first."
              : entity.locked
                ? "Its current state prevents you from editing its values."
                : "Only Editors in its owner project can change it."}
        </p>
        <Link to="/entities/$ref/" params={{ ref: entity.hrid }} search={recordSearch(entity)}>
          Back to {entity.hrid}
        </Link>
      </section>
    );
  }
  return <EditEntityForm entity={entity} type={type} types={types} />;
}

interface Conflict {
  /** Fields someone else changed that this person hadn't: they now show the new values. */
  updated: string[];
  /** Fields both changed: this person's values are kept. */
  both: string[];
}

function EditEntityForm({
  entity: opened,
  type,
  types,
}: {
  entity: Entity;
  type: EntityType;
  types: EntityType[];
}) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const { say } = useMessages();
  // What the form started from: the entity as read, and its fields as drafts.
  const [base, setBase] = useState(() => ({ entity: opened, drafts: draftsOf(type, opened) }));
  const [drafts, setDrafts] = useState<Drafts>(base.drafts);
  const [reason, setReason] = useState("");
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [unchanged, setUnchanged] = useState(false);
  // For a request between projects (ADR 0132), only the reader's side's fields.
  const fields = writableFields(type, opened);
  // Set once, or a registry entry's value: only its registrars change a fact (ADR 0115).
  const locked = new Set(
    fields
      .filter(
        (f) =>
          (f.immutable || (opened.entry && entryValue(f))) &&
          !isBlank(f, base.drafts[f.name] ?? null),
      )
      .map((f) => f.name),
  );
  const changed = fields.filter((f) => {
    if (locked.has(f.name) || same(drafts[f.name], base.drafts[f.name])) return false;
    // Reformatting a number or trimming a label can leave the submitted value unchanged.
    const next = encode(f, drafts[f.name] ?? null, type);
    const previous = encode(f, base.drafts[f.name] ?? null, type);
    return !next.ok || !previous.ok || !same(next.value, previous.value);
  });
  const canSave = !base.entity.archived && (base.entity.can.edit || base.entity.can.correct);
  const returns = changed.length && canSave ? editReturns(base.entity, type) : [];
  const [holdReason, setHoldReason] = useState("");
  // Only rules reading what it changes, as the server checks an edit.
  const proposed = { ...base.entity.values, ...typedValues(type, drafts) };
  const clearing = changed.some(
    (field) =>
      field.reason_on_clear &&
      base.entity.values[field.name] === true &&
      proposed[field.name] !== true,
  );
  const changedNames = new Set(changed.map((f) => f.name));
  for (const r of returns) {
    proposed[r.field] = r.to;
    changedNames.add(r.field);
  }
  const found = useRuleBreaks(type, proposed, changedNames);
  // Leaving with changes unsaved asks first; the save's own navigation doesn't.
  const leave = useLeaveGuard(changed.length > 0);

  const save = useApiMutation(
    async (values: Record<string, unknown>) =>
      unwrap(
        await iki.PATCH("/api/v1/entities/{ref}", {
          params: {
            path: { ref: base.entity.id },
            header: { "If-Match": `"${String(base.entity.revision)}"` },
          },
          body: {
            values,
            reason: reason.trim(),
            ...(hold ? { hold_reason: holdReason.trim() } : {}),
          },
        }),
      ),
    [ENTITIES],
  );
  const hold = needsHold(found, save.error);
  const stale = save.error instanceof ApiError && save.error.problem.code === "stale";
  const form = useForm(stale ? null : save.error, save.isPending);

  /** Someone saved first: bring their values in where this person hadn't typed. */
  async function catchUp() {
    const latest = await client.query({ ...entityQuery(base.entity.id), staleTime: 0 });
    const theirs = draftsOf(type, latest);
    const updated: string[] = [];
    const both: string[] = [];
    const next: Drafts = { ...drafts };
    for (const f of fields) {
      if (same(theirs[f.name], base.drafts[f.name])) continue;
      if (same(drafts[f.name], base.drafts[f.name])) {
        next[f.name] = theirs[f.name] ?? null;
        updated.push(f.label);
      } else if (!same(drafts[f.name], theirs[f.name])) {
        both.push(f.label);
      }
    }
    setBase({ entity: latest, drafts: theirs });
    setDrafts(next);
    setConflict({ updated, both });
  }

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSave) return;
    setUnchanged(false);
    const { values, errors } = encodeAll(type, changed, drafts);
    if (!reason.trim()) {
      if (clearing) errors["reason"] = "Give a reason for clearing this flag.";
      else if (returns.length || (changed.length > 0 && base.entity.can.edit_reason_required))
        errors["reason"] = "Give a reason for this correction.";
    }
    if (!form.validate({ ...ruleChecks(found, hold, holdReason), ...errors })) return;
    if (changed.length === 0) {
      setUnchanged(true);
      return;
    }
    setConflict(null);
    save.mutate(values, {
      onSuccess: (saved) => {
        const search = recordSearch(saved);
        client.setQueryData(entityQuery(saved.hrid, search.project).queryKey, saved);
        void navigate({
          to: "/entities/$ref/",
          params: { ref: saved.hrid },
          search,
          ignoreBlocker: true,
        }).then(() => {
          say(`Saved revision ${String(saved.revision)} of ${saved.hrid}.`);
        });
      },
      onError: (error) => {
        if (error.problem.code === "stale") void catchUp();
      },
    });
  }

  return (
    <>
      <section className="card form-card">
        <PageHeader crumbs={editCrumbs(opened, type)} title={`Edit ${opened.title}`} />
        {conflict ? (
          <div className="alert alert-warning" role="alert">
            <p>
              Someone else saved {base.entity.hrid} while you were editing (it’s at revision{" "}
              {base.entity.revision} now), so nothing was saved yet.
            </p>
            <ul>
              {conflict.updated.length ? (
                <li>
                  {conflict.updated.join(", ")} now{" "}
                  {conflict.updated.length === 1 ? "has its" : "have their"} new{" "}
                  {conflict.updated.length === 1 ? "value" : "values"}.
                </li>
              ) : null}
              {conflict.both.length ? (
                <li>
                  You both changed {conflict.both.join(", ")}: yours{" "}
                  {conflict.both.length === 1 ? "is" : "are"} kept. Check{" "}
                  <Link
                    to="/entities/$ref/audit/"
                    params={{ ref: base.entity.hrid }}
                    search={recordSearch(base.entity)}
                    target="_blank"
                  >
                    their change
                  </Link>{" "}
                  before you save.
                </li>
              ) : null}
            </ul>
            <p>
              {canSave
                ? "Save again to keep your changes."
                : "You can no longer edit this record. Your unsaved values are shown below."}
            </p>
          </div>
        ) : unchanged ? (
          <div className="alert alert-warning" role="alert">
            <p>Nothing to save: you haven’t changed anything.</p>
          </div>
        ) : (
          form.alert
        )}
        <form {...form.props} onSubmit={submit}>
          <Fields
            type={type}
            fields={fields}
            others={otherSide(type, opened)}
            types={types}
            drafts={drafts}
            setDraft={(name, draft) => {
              setDrafts((current) => ({ ...current, [name]: draft }));
            }}
            errors={{ ...breakErrors(found), ...form.errors }}
            notes={notesFor(type, true, base.drafts, {
              project: opened.owner_project?.name ?? "the project",
              registry: opened.entry?.registry.name ?? "",
            })}

            locked={locked}
            projectId={(field) => {
              const sides = opened.request;
              return sides?.service && isServiceField(sides, field.name)
                ? sides.service.id
                : (opened.owner_project?.id ?? "");
            }}
          />
          {hold ? (
            <HoldReason
              found={found}
              value={holdReason}
              onChange={setHoldReason}
              error={form.errors["hold_reason"]}
            />
          ) : null}
          {returns.length ? (
            <div className="alert alert-warning" role="alert">
              <p>Saving these changes will return {listed(returns.map((r) => r.description))}.</p>
              <p>Earlier signatures apply only to the revisions they signed.</p>
            </div>
          ) : null}
          <Reason
            value={reason}
            onChange={setReason}
            error={form.errors["reason"]}
            required={
              clearing || returns.length > 0 || base.entity.can.edit_reason_required === true
            }
          />
          <div className="actions">
            <Button
              type="submit"
              variant="primary"
              disabled={!canSave}
              busy={save.isPending}
              busyText="Saving…"
            >
              Save changes
            </Button>
            <Link
              to="/entities/$ref/"
              params={{ ref: opened.hrid }}
              search={recordSearch(opened)}
              className={buttonClass("quiet")}
            >
              Cancel
            </Link>
          </div>
        </form>
      </section>
      <ConfirmDialog
        isOpen={leave.asking}
        title="Discard your changes?"
        confirmLabel="Discard changes"
        cancelLabel="Keep editing"
        busy={false}
        onCancel={leave.stay}
        onConfirm={leave.leave}
      >
        <p>
          {listed(changed.map((f) => f.label))} {changed.length === 1 ? "has" : "have"} changes that
          aren’t saved. Discarding them leaves {opened.hrid} as it is.
        </p>
      </ConfirmDialog>
      {type.name === APP_TYPE && opened.owner_project ? (
        <AppPreview type={type} types={types} drafts={drafts} project={opened.owner_project} />
      ) : null}
    </>
  );
}
