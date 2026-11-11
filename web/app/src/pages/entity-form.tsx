/**
 * Creating and editing entities, with forms built from each type's fields.
 *
 * - A new entity's form starts from its fields' defaults. If the type reuses
 *   duplicates, creating one that exists opens the existing one instead; if
 *   it warns, the form says which entity it would duplicate and asks.
 * - An edit sends only the fields that changed, with the revision it
 *   started from (If-Match). If someone else saved in between, nothing is
 *   overwritten: their values come in for the fields this person didn't
 *   touch, the form says what changed, and they save again.
 */

import { ApiError, unwrap, type Problem } from "@ikisci/sdk";
import { useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useState, type SubmitEvent } from "react";

import { iki, useApiMutation } from "../api";
import {
  ENTITIES,
  entityQuery,
  inSentence,
  typesQuery,
  type Entity,
  type EntityType,
  type FieldDef,
} from "../entities";
import { FieldInput } from "../entity-inputs";
import {
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
import { Button, buttonClass, PageHeader, SelectField, TextField, useMessages } from "../ui";
import { useProject } from "./project-overview";

type Drafts = Record<string, Draft>;

function draftsOf(type: EntityType, entity: Entity): Drafts {
  return Object.fromEntries(
    formFields(type).map((f) => [f.name, toDraft(f, entity.values[f.name], entity)]),
  );
}

function blankMessage(field: FieldDef): string {
  switch (field.type) {
    case "enum":
    case "bool":
      return `Choose ${inSentence(field.label)}.`;
    case "ref":
    case "file":
      return `Choose ${inSentence(field.label)}.`;
    case "list":
      return `Add at least one ${inSentence(field.label)}.`;
    default:
      return `Enter ${inSentence(field.label)}.`;
  }
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

function Fields({
  type,
  types,
  drafts,
  setDraft,
  errors,
  notes,
  locked,
  projectId,
}: {
  type: EntityType;
  types: EntityType[];
  drafts: Drafts;
  setDraft: (name: string, draft: Draft) => void;
  errors: Record<string, string>;
  notes: Record<string, string>;
  locked: Set<string>;
  projectId: string;
}) {
  const computed = type.fields.filter((f) => f.computed);
  return (
    <>
      {formFields(type).map((field) => (
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
          note={notes[field.name]}
          disabled={locked.has(field.name)}
          projectId={projectId}
        />
      ))}
      {computed.length ? (
        <p className="muted">
          Actions set {computed.map((f) => inSentence(f.label)).join(", ")}: they aren’t typed in.
        </p>
      ) : null}
    </>
  );
}

function Reason({
  value,
  onChange,
  error,
}: {
  value: string;
  onChange: (value: string) => void;
  error: string | undefined;
}) {
  return (
    <TextField
      label="Reason"
      value={value}
      onChange={onChange}
      error={error}
      maxLength={2000}
      optional
      multiline
      help="Why, for the record. It’s kept in the entity’s history."
    />
  );
}

/** Rules people should know before typing: set once, and identity. */
function notesFor(type: EntityType, editing: boolean, drafts: Drafts): Record<string, string> {
  const notes: Record<string, string> = {};
  for (const f of formFields(type)) {
    const parts: string[] = [];
    if (f.immutable) {
      parts.push(
        editing && !isBlank(f, drafts[f.name] ?? null)
          ? "Set once: it can’t change."
          : "It can be set once, and can’t change after.",
      );
    }
    if (!editing && type.identity.includes(f.name) && type.on_duplicate === "reuse") {
      parts.push(`If ${inSentence(type.label)} with the same one exists, that one is used.`);
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
  const type = types.find((t) => t.name === name && !t.abstract);
  usePageTitle(`New ${type ? inSentence(type.label) : "entity"} · ${project.name}`);
  if (!type) return <ChooseType />;
  // A new form for each type: drafts don't carry between types.
  return <NewEntityForm key={type.name} type={type} types={types} />;
}

function ChooseType() {
  const project = useProject();
  const types = useSuspenseQuery(typesQuery).data;
  const navigate = useNavigate({ from: "/projects/$slug/entities/new" });
  const creatable = types.filter((t) => !t.abstract).sort((a, b) => a.label.localeCompare(b.label));
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

interface Duplicate {
  id: string;
  hrid: string;
}

function duplicatesOf(problem: Problem): Duplicate[] {
  return (problem.errors ?? []).flatMap((e) => {
    const id = (e as { entity?: string }).entity;
    const hrid = /The same as (\S+)\./.exec(e.detail)?.[1];
    return id && hrid ? [{ id, hrid }] : [];
  });
}

function NewEntityForm({ type, types }: { type: EntityType; types: EntityType[] }) {
  const project = useProject();
  const navigate = useNavigate();
  const { say } = useMessages();
  const [drafts, setDrafts] = useState<Drafts>(() =>
    Object.fromEntries(formFields(type).map((f) => [f.name, initialDraft(f, type)])),
  );
  const [reason, setReason] = useState("");
  const [duplicates, setDuplicates] = useState<Duplicate[]>([]);

  const create = useApiMutation(
    async (body: { values: Record<string, unknown>; allowDuplicate: boolean }) =>
      unwrap(
        await iki.POST("/api/v1/entities", {
          body: {
            type: type.name,
            project: project.id,
            values: body.values,
            reason: reason.trim(),
            allow_duplicate: body.allowDuplicate,
          },
        }),
      ),
    [ENTITIES],
  );
  const duplicate = create.error instanceof ApiError && create.error.problem.code === "duplicate";
  const form = useForm(duplicate ? null : create.error);
  const label = inSentence(type.label);

  function send(allowDuplicate: boolean) {
    const fields = formFields(type);
    const { values, errors } = encodeAll(type, fields, drafts);
    if (!form.validate(errors)) return;
    // Blank fields are left out, so the type's defaults apply.
    const given = Object.fromEntries(
      Object.entries(values).filter(([, v]) => v !== null && !(Array.isArray(v) && !v.length)),
    );
    setDuplicates([]);
    create.mutate(
      { values: given, allowDuplicate },
      {
        onSuccess: (made) => {
          void navigate({ to: "/entities/$ref/", params: { ref: made.hrid } }).then(() => {
            if (made.reused) {
              say(
                made.linked
                  ? `${made.title} was already in Iki, so it’s been added to ${project.name} instead of making a copy.`
                  : `${made.title} was already in ${project.name}, so nothing new was made.`,
              );
            } else {
              say(`Created ${made.hrid}.`);
            }
          });
        },
        onError: (error) => {
          if (error.problem.code === "duplicate") setDuplicates(duplicatesOf(error.problem));
        },
      },
    );
  }

  return (
    <section className="card form-card" aria-labelledby="new-entity-title">
      {/* Under the project's heading and tabs, as its other pages are. */}
      <h2 className="section-title" id="new-entity-title">
        New {label}
      </h2>
      <p>
        In {project.name}: its members can read it, and its Editors can change it.{" "}
        <Link to="/types/$name/" params={{ name: type.name }}>
          About {inSentence(type.label_plural)}
        </Link>
      </p>
      {duplicate ? (
        <div className="alert alert-warning" role="alert">
          <p>
            {duplicates.length === 1 ? "This is" : "These values are"} the same as{" "}
            {duplicates.map((d, index) => (
              <span key={d.id}>
                {index ? ", " : ""}
                <Link to="/entities/$ref/" params={{ ref: d.hrid }}>
                  {d.hrid}
                </Link>
              </span>
            ))}
            . Use {duplicates.length === 1 ? "it" : "one of them"}, or create a duplicate anyway.
          </p>
          <div className="actions">
            <Button
              busy={create.isPending}
              busyText="Creating…"
              onClick={() => {
                send(true);
              }}
            >
              Create a duplicate
            </Button>
          </div>
        </div>
      ) : (
        form.alert
      )}
      <form
        {...form.props}
        onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
          event.preventDefault();
          send(false);
        }}
      >
        <Fields
          type={type}
          types={types}
          drafts={drafts}
          setDraft={(name, draft) => {
            setDrafts((current) => ({ ...current, [name]: draft }));
          }}
          errors={form.errors}
          notes={notesFor(type, false, drafts)}
          locked={new Set()}
          projectId={project.id}
        />
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
  );
}

// --- Edit --------------------------------------------------------------------------

export function EditEntity() {
  const { ref } = useParams({ from: "/entities/$ref/edit" });
  const entity = useSuspenseQuery(entityQuery(ref)).data;
  const types = useSuspenseQuery(typesQuery).data;
  const type = types.find((t) => t.name === entity.type);
  usePageTitle(`Edit ${entity.title} (${entity.hrid})`);
  if (!type || !entity.can.edit || entity.archived) {
    return (
      <section className="card form-card">
        <PageHeader title={`Edit ${entity.title}`} />
        <p>
          {!type
            ? `Its type, ${entity.type}, isn’t installed any more, so it can’t be edited.`
            : entity.archived
              ? "It’s archived, so it can’t change. Restore it first."
              : "Only Editors in its owner project can change it."}
        </p>
        <Link to="/entities/$ref/" params={{ ref: entity.hrid }}>
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
  const fields = formFields(type);
  const locked = new Set(
    fields
      .filter((f) => f.immutable && !isBlank(f, base.drafts[f.name] ?? null))
      .map((f) => f.name),
  );
  const changed = fields.filter(
    (f) => !locked.has(f.name) && !same(drafts[f.name], base.drafts[f.name]),
  );

  const save = useApiMutation(
    async (values: Record<string, unknown>) =>
      unwrap(
        await iki.PATCH("/api/v1/entities/{ref}", {
          params: {
            path: { ref: base.entity.id },
            header: { "If-Match": `"${String(base.entity.revision)}"` },
          },
          body: { values, reason: reason.trim() },
        }),
      ),
    [ENTITIES],
  );
  const stale = save.error instanceof ApiError && save.error.problem.code === "stale";
  const form = useForm(stale ? null : save.error);

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
    setUnchanged(false);
    const { values, errors } = encodeAll(type, changed, drafts);
    if (!form.validate(errors)) return;
    if (changed.length === 0) {
      setUnchanged(true);
      return;
    }
    setConflict(null);
    save.mutate(values, {
      onSuccess: (saved) => {
        client.setQueryData(entityQuery(saved.hrid).queryKey, saved);
        void navigate({ to: "/entities/$ref/", params: { ref: saved.hrid } }).then(() => {
          say(`Saved revision ${String(saved.revision)} of ${saved.hrid}.`);
        });
      },
      onError: (error) => {
        if (error.problem.code === "stale") void catchUp();
      },
    });
  }

  return (
    <section className="card form-card">
      <PageHeader
        eyebrow={
          <Link to="/entities/$ref/" params={{ ref: opened.hrid }}>
            {opened.hrid}
          </Link>
        }
        title={`Edit ${opened.title}`}
      />
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
                  to="/entities/$ref/history/"
                  params={{ ref: base.entity.hrid }}
                  target="_blank"
                >
                  their change
                </Link>{" "}
                before you save.
              </li>
            ) : null}
          </ul>
          <p>Save again to keep your changes.</p>
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
          types={types}
          drafts={drafts}
          setDraft={(name, draft) => {
            setDrafts((current) => ({ ...current, [name]: draft }));
          }}
          errors={form.errors}
          notes={notesFor(type, true, base.drafts)}
          locked={locked}
          projectId={opened.owner_project?.id ?? ""}
        />
        <Reason value={reason} onChange={setReason} error={form.errors["reason"]} />
        <div className="actions">
          <Button type="submit" variant="primary" busy={save.isPending} busyText="Saving…">
            Save changes
          </Button>
          <Link to="/entities/$ref/" params={{ ref: opened.hrid }} className={buttonClass("quiet")}>
            Cancel
          </Link>
        </div>
      </form>
    </section>
  );
}
