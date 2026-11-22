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

import { ApiError, unwrap } from "@ikisci/sdk";
import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useState, type SubmitEvent } from "react";

import { iki, problemOf, useApiMutation } from "../api";
import { ENTITIES, typeLabel, typesQuery, type EntityType } from "../entities";
import { FieldInput } from "../entity-inputs";
import { encode, isBlank, same, toDraft, type Draft } from "../entity-values";
import { required, useForm } from "../forms";
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
  SelectField,
  TextField,
  Time,
  useMessages,
} from "../ui";
import { registryNote, Value } from "./entity";
import { EntryRevisionRow } from "./entity-audit";

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
        eyebrow={
          registry.data.can.manage ? (
            <a href={`${REGISTRY_SETTINGS}${registry.data.id}/`}>{registry.data.name} registry</a>
          ) : (
            `${registry.data.name} registry`
          )
        }
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
  usePageTitle(`${entry.hrid} · ${entry.registry.name} registry`);
  const fields = (type?.fields ?? []).filter((f) => f.registry);
  return (
    <>
      <PageHeader
        eyebrow={
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
      {entry.can.curate ? <Supersede entry={entry} /> : null}
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
    async (values: Record<string, unknown>) =>
      unwrap(
        await iki.PATCH("/api/v1/entries/{ref}", {
          params: {
            path: { ref: entry.id },
            header: { "If-Match": `"${String(entry.revision)}"` },
          },
          body: { values, reason: reason.trim() },
        }),
      ),
    [ENTITIES],
  );
  const stale = save.error instanceof ApiError && save.error.problem.code === "stale";
  const form = useForm(stale ? null : save.error);
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
        save.mutate(values, {
          onSuccess: (saved) => {
            client.setQueryData(entryQuery(saved.hrid).queryKey, saved);
            close();
            say(`Saved revision ${String(saved.revision)} of ${saved.hrid}.`);
          },
        });
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

/** A registrar replaces a mistaken entry with another, for good. */
function Supersede({ entry }: { entry: Entry }) {
  const [replacement, setReplacement] = useState("");
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  const { say } = useMessages();
  const client = useQueryClient();
  const supersede = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/entries/{ref}/supersede", {
          params: { path: { ref: entry.id } },
          body: { replacement: replacement.trim().toUpperCase(), reason: reason.trim() },
        }),
      ),
    [ENTITIES],
  );
  const form = useForm(confirming ? null : supersede.error);
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
          if (
            !form.validate({
              replacement:
                required(replacement, "Enter the ID of the entry that replaces it.") ||
                (replacement.trim().toUpperCase() === entry.hrid &&
                  "It can’t replace itself: enter another entry’s ID."),
              reason: required(reason, "Say why: every project with it is told."),
            })
          ) {
            return;
          }
          supersede.reset();
          setConfirming(true);
        }}
      >
        <TextField
          label="Replaced by"
          value={replacement}
          onChange={setReplacement}
          error={form.errors["replacement"]}
          maxLength={32}
          help={`The ID of a current entry of the same type in the ${entry.registry.name} registry.`}
        />
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
        title={`Supersede ${entry.hrid} with ${replacement.trim().toUpperCase()}?`}
        confirmLabel={`Supersede ${entry.hrid}`}
        busy={supersede.isPending}
        problem={supersede.error ? problemOf(supersede.error) : undefined}
        onCancel={() => {
          setConfirming(false);
        }}
        onConfirm={() => {
          supersede.mutate(undefined, {
            onSuccess: (saved) => {
              client.setQueryData(entryQuery(saved.hrid).queryKey, saved);
              setConfirming(false);
              say(`Superseded ${saved.hrid}. Every project with it has been told.`);
            },
          });
        }}
      >
        <p>
          <strong>This can’t be undone.</strong> {entry.hrid} will be marked as replaced by{" "}
          {replacement.trim().toUpperCase()}, for good, and no one will be able to register it
          again.
        </p>
        <p>
          Each project that has it is told, and its Editors move their records to{" "}
          {replacement.trim().toUpperCase()} themselves.
        </p>
      </ConfirmDialog>
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
