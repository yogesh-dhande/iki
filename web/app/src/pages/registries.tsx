/**
 * Registries' pages (ADR 0115).
 *
 * - A registry's entries, for its registrars: each one's ID and type, how
 *   many projects hold it (never which), and whether it was superseded.
 * - An entry's page, for those with a record of it and its registrars: its
 *   values, the records of it the reader can read, and its history.
 *   Registrars change its facts, with a reason, and supersede it for good;
 *   holders add it to another of their projects.
 */

import { unwrap } from "@ikisci/sdk";
import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useState, type SubmitEvent } from "react";

import { iki, isStale, problemOf, projectsQuery, useApiMutation, useOpenedRevision } from "../api";
import {
  ENTITIES,
  inSentence,
  typeLabel,
  typesQuery,
  type EntityType,
  type FieldDef,
} from "../entities";
import { FieldInput, inSentenceLabel, orList } from "../entity-inputs";
import { encode, initialDraft, isBlank, same, toDraft, type Draft } from "../entity-values";
import { required, useForm } from "../forms";
import { optionsQuery } from "../lab-lists";
import { usePageTitle } from "../layout";
import {
  createdMessage,
  entryQuery,
  entryRevisionsQuery,
  registryEntriesQuery,
  registryQuery,
  REGISTRY_SETTINGS,
  type Entry,
} from "../registries";
import {
  Badge,
  Button,
  ConfirmDialog,
  Loading,
  PageHeader,
  ProblemAlert,
  RadioField,
  SelectField,
  TextField,
  Time,
  useMessages,
} from "../ui";
import { registryNote, Value } from "./entity-value";
import { EntryRevisionRow } from "./revision-changes";

// --- A registry's entries ------------------------------------------------------------------

type Showing = "all" | "current" | "superseded";

const SHOWING: Record<Showing, boolean | undefined> = {
  all: undefined,
  current: false,
  superseded: true,
};

export function RegistryEntries() {
  const { registryId } = useParams({ from: "/registries/$registryId/entries" });
  const registry = useQuery(registryQuery(registryId));
  const types = useSuspenseQuery(typesQuery).data;
  const [q, setQ] = useState("");
  const [type, setType] = useState("");
  const [showing, setShowing] = useState<Showing>("all");
  const curates = registry.data?.can.curate ?? false;
  const entries = useInfiniteQuery({
    ...registryEntriesQuery(registryId, { q, type, superseded: SHOWING[showing] }),
    enabled: curates,
  });
  usePageTitle(registry.data ? `Entries in ${registry.data.name}` : "Registry");
  if (registry.isError) return <ProblemAlert problem={problemOf(registry.error)} />;
  if (!registry.data) return <Loading label="Loading the registry…" />;
  const items = entries.data?.pages.flatMap((p) => p.items) ?? [];
  const registered = types.filter((t) => t.identity.length && !t.abstract);
  return (
    <>
      <PageHeader
        crumbs={[
          registry.data.can.manage ? (
            <a href={`${REGISTRY_SETTINGS}${registry.data.id}/`}>{registry.data.name} registry</a>
          ) : (
            `${registry.data.name} registry`
          ),
          "Entries",
        ]}
        title="Entries"
      />
      {!curates ? (
        <section className="card">
          <p>
            Only the {registry.data.name} registry’s registrars see all its entries. You see an
            entry when one of your projects has a record of it.
          </p>
        </section>
      ) : (
        <>
          <p className="muted">
            Every identity registered in {registry.data.name}, newest first. You see how many
            projects have each, but not which, nor what they recorded.
          </p>
          <form
            className="toolbar"
            role="search"
            aria-label="Entries"
            onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
              event.preventDefault();
            }}
          >
            <TextField label="ID" value={q} onChange={setQ} maxLength={32} optional />
            <SelectField
              label="Type"
              value={type}
              onChange={setType}
              options={[
                { value: "", label: "Every type" },
                ...registered.map((t) => ({ value: t.name, label: t.label })),
              ]}
            />
            <SelectField
              label="Show"
              value={showing}
              onChange={setShowing}
              options={[
                { value: "all", label: "All" },
                { value: "current", label: "Current" },
                { value: "superseded", label: "Superseded" },
              ]}
            />
          </form>
          {entries.error ? <ProblemAlert problem={problemOf(entries.error)} /> : null}
          {entries.isPending ? <Loading label="Loading its entries…" /> : null}
          {entries.data && items.length === 0 ? (
            <p className="empty">No entries{q || type || showing !== "all" ? " match" : " yet"}.</p>
          ) : null}
          {items.length ? (
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">ID</th>
                    <th scope="col">Type</th>
                    <th scope="col">Projects with it</th>
                    <th scope="col">Registered</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((e) => (
                    <tr key={e.id}>
                      <th scope="row">
                        <Link to="/entries/$ref/" params={{ ref: e.hrid }}>
                          {e.hrid}
                        </Link>
                        {e.superseded ? (
                          <>
                            {" "}
                            <Badge muted>Superseded</Badge>
                          </>
                        ) : null}
                      </th>
                      <td>{typeLabel(types, e.type)}</td>
                      <td>{e.holders.toLocaleString()}</td>
                      <td>
                        <Time value={e.created_at} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          {entries.hasNextPage ? (
            <div className="actions">
              <Button
                busy={entries.isFetchingNextPage}
                busyText="Loading…"
                onClick={() => void entries.fetchNextPage()}
              >
                Show more
              </Button>
            </div>
          ) : null}
        </>
      )}
    </>
  );
}

// --- An entry ------------------------------------------------------------------------------

export function EntryPage() {
  const { ref } = useParams({ from: "/entries/$ref" });
  const entry = useSuspenseQuery(entryQuery(ref)).data;
  const types = useSuspenseQuery(typesQuery).data;
  const type = types.find((t) => t.name === entry.type);
  if (type?.lab_list) return <OptionPage entry={entry} type={type} />;
  return <RegistryEntryPage entry={entry} type={type} />;
}

function RegistryEntryPage({ entry, type }: { entry: Entry; type: EntityType | undefined }) {
  const types = useSuspenseQuery(typesQuery).data;
  usePageTitle(`${entry.hrid} · ${entry.registry.name} registry`);
  const fields = (type?.fields ?? []).filter((f) => f.registry);
  return (
    <>
      <PageHeader
        description={
          <>
            {type ? (
              <Link to="/types/$/" params={{ _splat: type.name }}>
                {type.label}
              </Link>
            ) : (
              entry.type
            )}{" "}
            · {entry.registry.name} registry
          </>
        }
        title={entry.hrid}
        badge={entry.superseded ? <Badge muted>Superseded</Badge> : null}
      />
      {entry.superseded ? (
        <div className="alert alert-warning" role="note">
          <p>
            Superseded by {entry.superseded.by.hrid} on <Time value={entry.superseded.at} />: “
            {entry.superseded.reason}”. It keeps its ID and values, and can’t be registered again.
            Each project that has it moves its own record to {entry.superseded.by.hrid}.
          </p>
        </div>
      ) : null}
      <section className="card" aria-labelledby="entry-values-title">
        <h2 className="section-title" id="entry-values-title">
          Values
        </h2>
        <p className="muted">
          Every project with {entry.hrid} has these. Each project’s own values are in its record.
        </p>
        <dl className="summary values">
          {fields.map((f) => (
            <div key={f.name} className="value-row">
              <dt>
                {f.label}
                <span className="cell-meta">{registryNote(f)}</span>
              </dt>
              <dd>
                <Value field={f} value={entry.values[f.name]} entity={entry} />
              </dd>
            </div>
          ))}
        </dl>
        {entry.can.curate && type ? <Curate entry={entry} type={type} types={types} /> : null}
      </section>
      <section className="card" aria-labelledby="entry-details-title">
        <h2 className="section-title" id="entry-details-title">
          Details
        </h2>
        <dl className="summary">
          <dt>ID</dt>
          <dd>
            <code>{entry.hrid}</code>
          </dd>
          <dt>Registry</dt>
          <dd>{entry.registry.name}</dd>
          <dt>Registered</dt>
          <dd>
            <Time value={entry.created_at} />
          </dd>
          <dt>Revision</dt>
          <dd>{entry.revision}</dd>
          {entry.holders !== null ? (
            <>
              <dt>Projects with it</dt>
              <dd>
                {entry.holders.toLocaleString()}
                <span className="cell-meta">As a registrar you see how many, not which.</span>
              </dd>
            </>
          ) : null}
        </dl>
      </section>
      <Records entry={entry} />
      <AddToProject entry={entry} />
      {entry.can.curate ? <Supersede entry={entry} types={types} /> : null}
      <EntryHistory entry={entry} type={type} />
    </>
  );
}

/** The records of it the reader can read: their own projects', and those shared with them. */
function Records({ entry }: { entry: Entry }) {
  return (
    <section className="card" aria-labelledby="entry-records-title">
      <h2 className="section-title" id="entry-records-title">
        Records you can read
      </h2>
      {entry.records.length === 0 ? (
        <p className="muted">
          None of your projects has a record of it. As a registrar, you see the entry, not projects’
          records.
        </p>
      ) : (
        <ul className="rows">
          {entry.records.map((r) => (
            <li className="row" key={r.id}>
              <div className="row-main">
                <Link
                  className="row-title"
                  to="/entities/$ref/"
                  params={{ ref: entry.hrid }}
                  search={{ project: r.project.slug }}
                >
                  {r.project.name}’s record
                </Link>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Gives another of the reader's projects a record of its own: the identity, none of any
 * project's data. The API lists those it may go to: projects they edit in its registry
 * without a record of it, which a project they read it in may share with (ADR 0067). */
function AddToProject({ entry }: { entry: Entry }) {
  const navigate = useNavigate();
  const { say } = useMessages();
  const choices = entry.add_to;
  // Projects shown by name: two of one name are told apart by their slugs.
  const label = (p: { name: string; slug: string }) =>
    choices.filter((c) => c.name === p.name).length > 1 ? `${p.name} (${p.slug})` : p.name;
  const [picked, setPicked] = useState("");
  const [reason, setReason] = useState("");
  const target = choices.find((p) => p.id === picked) ?? choices[0];
  const add = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/entries/{ref}/records", {
          params: { path: { ref: entry.id } },
          body: { project: target?.id ?? "", values: {}, reason: reason.trim() },
        }),
      ),
    [ENTITIES],
  );
  if (!target || entry.superseded) return null;
  return (
    <section className="card" aria-labelledby="entry-add-title">
      <h2 className="section-title" id="entry-add-title">
        Add to another project
      </h2>
      <p>
        The project gets a record of its own, with {entry.hrid}’s values and none of any other
        project’s data.
      </p>
      <ProblemAlert problem={add.error ? problemOf(add.error) : undefined} />
      <form
        onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
          event.preventDefault();
          add.mutate(undefined, {
            onSuccess: (made) => {
              void navigate({
                to: "/entities/$ref/",
                params: { ref: made.hrid },
                search: { project: target.slug },
              }).then(() => {
                say(
                  made.reused
                    ? createdMessage(made, target)
                    : `Added ${made.hrid} to ${target.name}. This is its own record of it.`,
                );
              });
            },
          });
        }}
      >
        <SelectField
          label="Project"
          value={target.id}
          onChange={setPicked}
          options={choices.map((p) => ({ value: p.id, label: label(p) }))}
        />
        <TextField
          label="Reason"
          value={reason}
          onChange={setReason}
          maxLength={2000}
          optional
          multiline
          help="Why, for the record. It’s kept in the new record’s history."
        />
        <Button type="submit" variant="primary" busy={add.isPending} busyText="Adding…">
          Add to {label(target)}
        </Button>
      </form>
    </section>
  );
}

/** A registrar changes its facts, with a reason, from the revision they read (If-Match).
 * Fixed facts (ADR 0143) aren't offered: no one changes them, and a wrong one is superseded. */
function Curate({ entry, type, types }: { entry: Entry; type: EntityType; types: EntityType[] }) {
  const facts = type.fields.filter((f) => f.registry === "fact" && !f.detached && !f.immutable);
  const client = useQueryClient();
  const { say } = useMessages();
  const [open, setOpen] = useState(false);
  const start = () =>
    Object.fromEntries(facts.map((f) => [f.name, toDraft(f, entry.values[f.name], entry)]));
  const [base, setBase] = useState<Record<string, Draft>>(start);
  const [drafts, setDrafts] = useState<Record<string, Draft>>(start);
  const [reason, setReason] = useState("");
  const [unchanged, setUnchanged] = useState(false);
  const save = useApiMutation(
    async ({
      values,
      header,
    }: {
      values: Record<string, unknown>;
      header: { "If-Match": string };
    }) =>
      unwrap(
        await iki.PATCH("/api/v1/entries/{ref}", {
          params: { path: { ref: entry.id }, header },
          body: { values, reason: reason.trim() },
        }),
      ),
    [ENTITIES],
  );
  // The facts the form starts from are the revision it opened at, which it saves from: a
  // change someone made meanwhile is refused, and the form says to start again from it.
  const opened = useOpenedRevision(entry.revision, { open });
  const stale = isStale(save.error);
  const form = useForm(stale ? null : save.error, save.isPending);
  if (!facts.length) return null;
  if (!open) {
    return (
      <div className="actions">
        <Button
          onClick={() => {
            const fresh = start();
            setBase(fresh);
            setDrafts(fresh);
            setOpen(true);
          }}
        >
          Change facts
        </Button>
      </div>
    );
  }
  const close = () => {
    setOpen(false);
    setReason("");
    setUnchanged(false);
    save.reset();
  };
  return (
    <form
      {...form.props}
      aria-labelledby="curate-title"
      onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
        event.preventDefault();
        setUnchanged(false);
        const values: Record<string, unknown> = {};
        const checks: Record<string, string | false> = {
          reason: required(reason, "Say why: every project with it sees it in the history."),
        };
        for (const f of facts) {
          const draft = drafts[f.name] ?? null;
          if (same(draft, base[f.name])) continue;
          if (isBlank(f, draft)) {
            values[f.name] = null;
            continue;
          }
          const encoded = encode(f, draft, type);
          if (encoded.ok) values[f.name] = encoded.value;
          else checks[`values/${f.name}`] = encoded.error;
        }
        if (!form.validate(checks)) return;
        if (!Object.keys(values).length) {
          setUnchanged(true);
          return;
        }
        save.mutate(
          { values, header: opened.ifMatch },
          {
            onSuccess: (saved) => {
              client.setQueryData(entryQuery(saved.hrid).queryKey, saved);
              close();
              say(`Saved revision ${String(saved.revision)} of ${saved.hrid}.`);
            },
          },
        );
      }}
    >
      <h3 className="section-title" id="curate-title">
        Change facts
      </h3>
      <p className="muted">
        Every project with {entry.hrid} sees the change, with who made it, when and why.
      </p>
      {stale ? (
        <div className="alert alert-warning" role="alert">
          <p>
            Someone changed {entry.hrid} while you were editing, so nothing was saved. Close this,
            check the new values, and make your change again.
          </p>
        </div>
      ) : unchanged ? (
        <div className="alert alert-warning" role="alert">
          <p>Nothing to save: you haven’t changed anything.</p>
        </div>
      ) : (
        form.alert
      )}
      {facts.map((f) => (
        <FieldInput
          key={f.name}
          field={f}
          type={type}
          types={types}
          draft={drafts[f.name] ?? null}
          onChange={(draft) => {
            setDrafts((current) => ({ ...current, [f.name]: draft }));
          }}
          error={form.errors[`values/${f.name}`]}
          projectId=""
          registryId={entry.registry.id}
        />
      ))}
      <TextField
        label="Reason"
        value={reason}
        onChange={setReason}
        error={form.errors["reason"]}
        maxLength={2000}
        multiline
        help="Why, for the record. Every project with it sees it in the history."
      />
      <div className="actions">
        <Button type="submit" variant="primary" busy={save.isPending} busyText="Saving…">
          Save facts
        </Button>
        <Button
          variant="quiet"
          onClick={() => {
            if (stale) void client.invalidateQueries({ queryKey: entryQuery(entry.hrid).queryKey });
            close();
          }}
        >
          {stale ? "Close" : "Cancel"}
        </Button>
      </div>
    </form>
  );
}

type Replacing = "entry" | "type";

/** The fields a registrar gives a new entry of an entry's identity as another type (ADR
 * 0165), as its registry says: those the entry's values don't fill as they are, such as
 * its parts' roles, which the new type names otherwise, and its own facts. */
function registeredAs(target: EntityType | undefined, names: readonly string[]): FieldDef[] {
  return names.flatMap((n) => target?.fields.find((f) => f.name === n) ?? []);
}

/** A registrar replaces a mistaken entry with another, for good: one registered already,
 * or a new entry of its identity as another type under it (ADR 0165), such as a protein
 * complex's chains as the antibody they are, which it hands its identity to. */
function Supersede({ entry, types }: { entry: Entry; types: EntityType[] }) {
  const others = entry.can.supersede_as ?? [];
  const mine = types.find((t) => t.name === entry.type);
  const [replacing, setReplacing] = useState<Replacing>("entry");
  const [replacement, setReplacement] = useState("");
  const fieldsOf = (name: string) =>
    registeredAs(
      types.find((t) => t.name === name),
      others.find((o) => o.type === name)?.fields ?? [],
    );
  const startFor = (name: string) => {
    const target = types.find((t) => t.name === name);
    return Object.fromEntries(
      fieldsOf(name).map((f) => [
        f.name,
        entry.values[f.name] !== undefined && entry.values[f.name] !== null
          ? toDraft(f, entry.values[f.name], entry)
          : target
            ? initialDraft(f, target)
            : null,
      ]),
    );
  };
  const first = others[0]?.type ?? "";
  const [as, setAs] = useState(() => ({ type: first, drafts: startFor(first) }));
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  const { say } = useMessages();
  const client = useQueryClient();
  const target = types.find((t) => t.name === as.type);
  const fields = fieldsOf(as.type);
  const kind = target ? inSentence(target.label) : as.type;
  const byType = replacing === "type";
  const named = byType ? `a new ${kind}` : replacement.trim().toUpperCase();
  const supersede = useApiMutation(
    async ({
      header,
      values,
    }: {
      header: { "If-Match": string };
      values: Record<string, unknown> | null;
    }) =>
      unwrap(
        await iki.POST("/api/v1/entries/{ref}/supersede", {
          params: { path: { ref: entry.id }, header },
          body: values
            ? { type: as.type, values, reason: reason.trim() }
            : { replacement: replacement.trim().toUpperCase(), reason: reason.trim() },
        }),
      ),
    [ENTITIES],
  );
  // The entry as the registrar confirmed it: one changed meanwhile is refused (412).
  const opened = useOpenedRevision(entry.revision, { open: confirming, error: supersede.error });
  const [values, setValues] = useState<Record<string, unknown> | null>(null);
  const form = useForm(confirming ? null : supersede.error, supersede.isPending);
  return (
    <section className="card" aria-labelledby="supersede-title">
      <h2 className="section-title" id="supersede-title">
        Supersede
      </h2>
      <p>
        When {entry.hrid} was registered by mistake, such as two registrations of one compound, or a
        structure registered wrongly, replace it with the right entry. It keeps its ID and values,
        and can’t be registered again. Each project that has it is told, and moves its own record.
      </p>
      {form.alert}
      <form
        {...form.props}
        onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
          event.preventDefault();
          const checks: Record<string, string | false> = {
            reason: required(reason, "Say why: every project with it is told."),
          };
          const given: Record<string, unknown> = {};
          if (byType) {
            for (const f of fields) {
              const draft = as.drafts[f.name] ?? null;
              if (f.registry !== "identity" && isBlank(f, draft)) continue;
              const encoded = encode(f, draft, target ?? { values_schema: {} });
              if (encoded.ok) given[f.name] = encoded.value;
              else checks[`values/${f.name}`] = encoded.error;
            }
          } else {
            checks["replacement"] =
              required(replacement, "Enter the ID of the entry that replaces it.") ||
              (replacement.trim().toUpperCase() === entry.hrid &&
                "It can’t replace itself: enter another entry’s ID.");
          }
          if (!form.validate(checks)) return;
          supersede.reset();
          setValues(byType ? given : null);
          setConfirming(true);
        }}
      >
        {others.length ? (
          <RadioField<Replacing>
            label="Replace it with"
            value={replacing}
            onChange={setReplacing}
            options={[
              { value: "entry", label: "An entry registered already" },
              {
                value: "type",
                label: `Its identity, registered as another type`,
                description: `the same ${identityOf(mine)}, as ${orList(
                  others.map((o) => inSentenceLabel(typeLabel(types, o.type))),
                )}`,
              },
            ]}
          />
        ) : null}
        {byType ? (
          <>
            <SelectField
              label="Register it as"
              value={as.type}
              onChange={(name) => {
                setAs({ type: name, drafts: startFor(name) });
              }}
              options={others.map((o) => ({ value: o.type, label: typeLabel(types, o.type) }))}
              help={`A new entry, with the next ${target?.hrid_prefix ?? ""} ID. It has ${
                entry.hrid
              }’s values for what the two types share, and these. Its identity stays the same.`}
            />
            {target
              ? fields.map((f) => (
                  <FieldInput
                    key={`${as.type}:${f.name}`}
                    field={f}
                    type={target}
                    types={types}
                    draft={as.drafts[f.name] ?? null}
                    onChange={(draft) => {
                      setAs((current) => ({
                        ...current,
                        drafts: { ...current.drafts, [f.name]: draft },
                      }));
                    }}
                    error={form.errors[`values/${f.name}`]}
                    errors={form.errors}
                    path={`values/${f.name}`}
                    note={
                      f.registry === "identity"
                        ? `${entry.hrid}’s identity, which stays the same: only what ${inSentenceLabel(
                            target.label,
                          )} names otherwise, such as its parts’ roles, can change.`
                        : undefined
                    }
                    projectId=""
                    registryId={entry.registry.id}
                  />
                ))
              : null}
          </>
        ) : (
          <TextField
            label="Replaced by"
            value={replacement}
            onChange={setReplacement}
            error={form.errors["replacement"]}
            maxLength={32}
            help={`The ID of a current entry in the ${entry.registry.name} registry, of its type${
              others.length ? " or another type of its identity" : ""
            }.`}
          />
        )}
        <TextField
          label="Reason"
          value={reason}
          onChange={setReason}
          error={form.errors["reason"]}
          maxLength={2000}
          multiline
          help="Why, for the record. Every project with it is told."
        />
        <Button type="submit" variant="danger">
          Supersede {entry.hrid}…
        </Button>
      </form>
      <ConfirmDialog
        isOpen={confirming}
        title={`Supersede ${entry.hrid} with ${named}?`}
        confirmLabel={`Supersede ${entry.hrid}`}
        busy={supersede.isPending}
        problem={supersede.error ? problemOf(supersede.error) : undefined}
        onCancel={() => {
          setConfirming(false);
        }}
        onConfirm={() => {
          supersede.mutate(
            { header: opened.ifMatch, values },
            {
              onSuccess: (saved) => {
                client.setQueryData(entryQuery(saved.hrid).queryKey, saved);
                setConfirming(false);
                say(
                  values && saved.superseded
                    ? `Superseded ${saved.hrid} with ${saved.superseded.by.hrid}, a new ${kind}. Every project with it has been told.`
                    : `Superseded ${saved.hrid}. Every project with it has been told.`,
                );
              },
            },
          );
        }}
      >
        <p>
          <strong>This can’t be undone.</strong> {entry.hrid} will be marked as replaced by {named},
          for good, and no one will be able to register it again.
          {values ? ` Registering its identity will find the new ${kind} from then on.` : null}
        </p>
        <p>
          Each project that has it is told, and its Editors move their records to {named}{" "}
          themselves.
          {values
            ? ` A record can’t change its type, so each move makes the project’s record of the new ${kind}, with what the two types’ records share, and archives the old one.`
            : null}
        </p>
      </ConfirmDialog>
    </section>
  );
}

/** What an entry's identity is, in a sentence: its type's identity fields, as the type
 * labels them ("parts"). */
function identityOf(mine: EntityType | undefined): string {
  const names = (mine?.identity ?? []).map(
    (n) => mine?.fields.find((f) => f.name === n)?.label.toLowerCase() ?? n,
  );
  return names.length ? names.join(" and ") : "identity";
}

// --- A lab list's option ---------------------------------------------------------------

/**
 * A lab list's option (ADR 0270): an entry no project has a record of, which every
 * project in its registry picks from. Its page says which list it's in, what's become of
 * it, and its history: each change with who, when and why. Its curators change it on the
 * list's page. Once it's superseded, each project's Editors move their records that name
 * it to its replacement here, as the notification about it says.
 */
function OptionPage({ entry, type }: { entry: Entry; type: EntityType }) {
  // An option is shown by its label and named by its code: its HRID is only its page's
  // address, from the sequence every list's options share (ADR 0295).
  const code = typeof entry.values["code"] === "string" ? entry.values["code"] : entry.hrid;
  const label = typeof entry.values["label"] === "string" ? entry.values["label"] : code;
  const fields = type.fields.filter((f) => f.name !== "position");
  // What's become of it, as its list says: archived, or superseded by which option.
  const option = useQuery(
    optionsQuery(type.name, { registry: entry.registry.id }, true),
  ).data?.find((o) => o.id === entry.id);
  const archived = Boolean(option?.archived);
  const by = entry.superseded?.by;
  const replacement = option?.superseded_by?.label ?? (by ? (by.code ? by.title : by.hrid) : "");
  usePageTitle(`${label} · ${type.label_plural}`);
  return (
    <>
      <PageHeader
        crumbs={[
          <Link
            to="/types/$/"
            params={{ _splat: type.name }}
            search={{ registry: entry.registry.id }}
          >
            {type.label_plural}
          </Link>,
          label,
        ]}
        description={
          <>
            An option of{" "}
            <Link
              to="/types/$/"
              params={{ _splat: type.name }}
              search={{ registry: entry.registry.id }}
              hash={`option-${code}`}
            >
              {type.label_plural}
            </Link>{" "}
            · {entry.registry.name} registry · <code>{code}</code>
          </>
        }
        title={label}
        badge={
          entry.superseded ? (
            <Badge muted>Superseded</Badge>
          ) : archived ? (
            <Badge muted>Archived</Badge>
          ) : null
        }
      />
      {entry.superseded ? (
        <div className="alert alert-warning" role="note">
          <p>
            Superseded by {replacement} on <Time value={entry.superseded.at} />: “
            {entry.superseded.reason}”. Dropdowns offer {replacement} instead. Each project whose
            records name {label} moves them to {replacement} itself.
          </p>
        </div>
      ) : archived ? (
        <div className="alert alert-warning" role="note">
          <p>
            Archived: no dropdown offers it. The records that name it keep it, and its curators can
            restore it on the list’s page.
          </p>
        </div>
      ) : null}
      {entry.superseded ? (
        <MoveReferences entry={entry} type={type} label={label} replacement={replacement} />
      ) : null}
      <section className="card" aria-labelledby="entry-values-title">
        <h2 className="section-title" id="entry-values-title">
          Values
        </h2>
        <p className="muted">
          Every project in the {entry.registry.name} registry picks it as this. Its curators change
          it on the{" "}
          <Link
            to="/types/$/"
            params={{ _splat: type.name }}
            search={{ registry: entry.registry.id }}
          >
            list’s page
          </Link>
          .
        </p>
        <dl className="summary values">
          {fields.map((f) => (
            <div key={f.name} className="value-row">
              <dt>{f.label}</dt>
              <dd>
                <Value field={f} value={entry.values[f.name]} entity={entry} />
              </dd>
            </div>
          ))}
        </dl>
      </section>
      <EntryHistory entry={entry} type={type} />
    </>
  );
}

/** A project's Editors move its records from a superseded option to its replacement: each
 * record that names it names the replacement, a revision with the reason. The notification
 * about it opens here with its project (`?project=`). */
function MoveReferences({
  entry,
  type,
  label,
  replacement,
}: {
  entry: Entry;
  type: EntityType;
  label: string;
  replacement: string;
}) {
  const { project: asked } = useSearch({ from: "/entries/$ref" });
  const { say } = useMessages();
  const projects = useQuery(projectsQuery).data ?? [];
  const mine = projects.filter(
    (p) => p.registry.id === entry.registry.id && p.can.edit && !p.archived,
  );
  const [picked, setPicked] = useState("");
  const [reason, setReason] = useState("");
  const [moved, setMoved] = useState<{ project: string; hrids: string[] } | null>(null);
  const target = mine.find((p) => p.id === picked) ?? mine.find((p) => p.slug === asked) ?? mine[0];
  const code = typeof entry.values["code"] === "string" ? entry.values["code"] : "";
  const move = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/lists/{name}/options/{code}/move", {
          params: { path: { name: type.name, code } },
          body: { project: target?.id ?? "", reason: reason.trim() },
        }),
      ),
    [ENTITIES],
  );
  const form = useForm(move.error, move.isPending);
  if (!target) return null;
  return (
    <section className="card" aria-labelledby="move-title">
      <h2 className="section-title" id="move-title">
        Move records to {replacement}
      </h2>
      <p>
        Each of the project’s records that names {label} will name {replacement} instead: a revision
        of each, with your reason. Archived and locked records keep {label}.
      </p>
      {form.alert}
      {moved ? (
        <div className="alert alert-success" role="status">
          <p>
            {moved.hrids.length
              ? `Moved ${String(moved.hrids.length)} of ${moved.project}’s records to ${replacement}: ${moved.hrids.join(", ")}.`
              : `None of ${moved.project}’s records that can change name ${label}.`}
          </p>
        </div>
      ) : null}
      <form
        {...form.props}
        onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
          event.preventDefault();
          if (
            !form.validate({ reason: required(reason, "Say why: each record’s history keeps it.") })
          ) {
            return;
          }
          move.mutate(undefined, {
            onSuccess: (done) => {
              setMoved({ project: target.name, hrids: done.moved });
              setReason("");
              say(
                done.moved.length
                  ? `Moved ${String(done.moved.length)} ${done.moved.length === 1 ? "record" : "records"} to ${replacement}.`
                  : `None of ${target.name}’s records needed moving.`,
              );
            },
          });
        }}
      >
        {mine.length > 1 ? (
          <SelectField
            label="Project"
            value={target.id}
            onChange={(id) => {
              setPicked(id);
              setMoved(null);
            }}
            options={mine.map((p) => ({ value: p.id, label: p.name }))}
          />
        ) : null}
        <TextField
          label="Reason"
          value={reason}
          onChange={setReason}
          error={form.errors["reason"] ?? form.errors["values/reason"]}
          maxLength={2000}
          multiline
          help="Why, for the record. Each record that moves keeps it in its history."
        />
        <Button type="submit" variant="primary" busy={move.isPending} busyText="Moving…">
          Move {target.name}’s records
        </Button>
      </form>
    </section>
  );
}

function EntryHistory({ entry, type }: { entry: Entry; type: EntityType | undefined }) {
  const revisions = useInfiniteQuery(entryRevisionsQuery(entry.id));
  const items = revisions.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <section aria-labelledby="entry-history-title">
      <h2 className="section-title" id="entry-history-title">
        History
      </h2>
      <p className="muted">
        Every change, newest first. Changes made through projects you can’t read say only what they
        set.
      </p>
      {revisions.isPending ? <Loading label="Loading the history…" /> : null}
      {revisions.error ? <ProblemAlert problem={problemOf(revisions.error)} /> : null}
      <ul className="rows">
        {items.map((r) => (
          <EntryRevisionRow key={r.number} entry={entry.id} revision={r} type={type} />
        ))}
      </ul>
      {revisions.hasNextPage ? (
        <div className="actions">
          <Button
            busy={revisions.isFetchingNextPage}
            busyText="Loading…"
            onClick={() => void revisions.fetchNextPage()}
          >
            Show older
          </Button>
        </div>
      ) : null}
    </section>
  );
}
