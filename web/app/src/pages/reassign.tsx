/**
 * Reassigning the records picked in the grid (ADR 0122): one assignment field of each,
 * named for someone else (or emptied), each record as its own revision. Records that can't
 * change are left as they are, and the dialog says why.
 *
 * It sends each record's revision as the grid showed it when the dialog opened. If any
 * changed since, nothing is reassigned: the dialog names them, and the person refreshes
 * the selection and checks them before reassigning.
 */

import type { StaleRecord } from "@ikisci/sdk";
import { Link } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

import { problemOf, useApiMutation } from "../api";
import { inSentence, recordSearch, type Entity, type EntityType } from "../entities";
import { PersonPicker } from "../entity-inputs";
import type { Picked } from "../entity-values";
import { AFTER_ASSIGNING, assignMany, type Assigned } from "../people";
import { Button, ConfirmDialog, ProblemAlert, SelectField, TextField, useMessages } from "../ui";

type Op = "set" | "add" | "remove";
type Stale = StaleRecord[];

export function ReassignDialog({
  type,
  picked,
  projectId,
  refresh,
  onClose,
}: {
  type: EntityType;
  picked: Entity[];
  projectId: string;
  /** Reads the grid again, and gives the picked records as they are now. */
  refresh: () => Promise<Entity[]>;
  onClose: (done: boolean) => void;
}) {
  // The records as reviewed: as the grid showed them when the dialog opened, or when the
  // selection was last refreshed. A refetch behind the dialog doesn't change them.
  const [reviewed, setReviewed] = useState(picked);
  // After a refusal, the records that had changed, and those the refreshed list left out.
  const [refreshed, setRefreshed] = useState<{ changed: Stale; gone: Stale } | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshProblem, setRefreshProblem] = useState<unknown>(null);
  const note = useRef<HTMLDivElement>(null);
  // Set by a refresh: the note that says what changed takes focus once it shows.
  const focusNote = useRef(false);
  const fields = type.fields.filter((f) => f.assignment);
  const [name, setName] = useState(fields[0]?.name ?? "");
  const [person, setPerson] = useState<Picked | null>(null);
  const [op, setOp] = useState<Op>("set");
  const [reason, setReason] = useState("");
  const [done, setDone] = useState<Assigned | null>(null);
  const { say } = useMessages();
  const field = fields.find((f) => f.name === name);
  const many = field?.type === "list";
  const assign = useApiMutation(
    () =>
      assignMany({
        entities: reviewed.map((e) => e.id),
        expected_revisions: Object.fromEntries(reviewed.map(({ id, revision }) => [id, revision])),
        field: name,
        person: person?.id ?? null,
        op: many ? op : "set",
        reason: reason.trim(),
      }),
    AFTER_ASSIGNING,
  );
  const problem = assign.error ? problemOf(assign.error) : undefined;
  const stale =
    problem?.code === "stale"
      ? (problem.stale ?? []).filter((s): s is StaleRecord => "hrid" in s)
      : null;
  // What changed takes focus, to be read before reassigning again: once the note has
  // rendered, which in a busy browser can be after the next frame.
  useEffect(() => {
    if (!focusNote.current || !note.current) return;
    focusNote.current = false;
    note.current.focus();
  });
  if (!field) return null;
  const records = `${String(reviewed.length)} ${inSentence(reviewed.length === 1 ? type.label : type.label_plural)}`;
  const linkTo = (s: { id: string; hrid: string }) => {
    const found = reviewed.find((e) => e.id === s.id);
    return (
      <Link
        to="/entities/$ref/audit/"
        params={{ ref: s.hrid }}
        search={found ? recordSearch(found) : {}}
        target="_blank"
      >
        {s.hrid}
      </Link>
    );
  };
  const refreshSelection = () => {
    if (!stale || refreshing) return;
    setRefreshing(true);
    setRefreshProblem(null);
    refresh().then(
      (now) => {
        const ids = new Set(now.map((e) => e.id));
        setReviewed(now);
        setRefreshed({
          changed: stale.filter((s) => ids.has(s.id)),
          gone: stale.filter((s) => !ids.has(s.id)),
        });
        setRefreshing(false);
        focusNote.current = true;
        assign.reset();
      },
      (error: unknown) => {
        setRefreshProblem(error);
        setRefreshing(false);
      },
    );
  };
  if (done) {
    const hrids = new Map(reviewed.map((e) => [e.id, e.hrid]));
    return (
      <ConfirmDialog
        isOpen
        title={`Reassigned ${String(done.changed.length)} of ${String(reviewed.length)}`}
        confirmLabel="Done"
        confirmVariant="primary"
        cancelLabel="Close"
        busy={false}
        onCancel={() => {
          onClose(true);
        }}
        onConfirm={() => {
          onClose(true);
        }}
      >
        {done.skipped.length ? (
          <>
            <p>These were left as they were:</p>
            <ul>
              {done.skipped.map((s) => (
                <li key={s.entity}>
                  {s.hrid !== "" ? s.hrid : (hrids.get(s.entity) ?? s.entity)}: {s.why}
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p>Each changed as a revision of its own, and whoever it names was told.</p>
        )}
      </ConfirmDialog>
    );
  }
  return (
    <ConfirmDialog
      isOpen
      title={`Reassign ${records}`}
      confirmLabel="Reassign"
      confirmVariant="primary"
      confirmDisabled={stale !== null || reviewed.length === 0}
      busy={assign.isPending}
      problem={stale ? undefined : problem}
      onCancel={() => {
        onClose(false);
      }}
      onConfirm={() => {
        assign.mutate(undefined, {
          onSuccess: (result) => {
            say(
              `Reassigned ${String(result.changed.length)} of ${String(reviewed.length)}` +
                (result.skipped.length
                  ? `; ${String(result.skipped.length)} left as they were.`
                  : "."),
            );
            setDone(result);
          },
        });
      }}
    >
      {stale ? (
        <div className="alert alert-warning" role="alert">
          <p>
            <strong>Nothing was reassigned:</strong>{" "}
            {stale.length === 1
              ? "one of these records"
              : `${String(stale.length)} of these records`}{" "}
            changed after you selected {stale.length === 1 ? "it" : "them"}.
          </p>
          <ul aria-label="Changed since you selected them">
            {stale.map((s) => (
              <li key={s.id}>
                {linkTo(s)}: revision {s.expected_revision} when you selected it, revision{" "}
                {s.current_revision} now.
              </li>
            ))}
          </ul>
          <p>
            Refresh the selection to see them as they are now, then reassign. Your choices here are
            kept.
          </p>
          <ProblemAlert problem={refreshProblem ? problemOf(refreshProblem) : undefined} />
          <div className="actions">
            <Button busy={refreshing} busyText="Refreshing…" onClick={refreshSelection}>
              Refresh the selection
            </Button>
          </div>
        </div>
      ) : refreshed ? (
        <div className="alert alert-success" role="status" tabIndex={-1} ref={note}>
          <p>
            The selection is up to date.
            {refreshed.changed.length ? (
              <>
                {" "}
                Check {refreshed.changed.length === 1 ? "the record" : "the records"} that changed
                before you reassign:{" "}
                {refreshed.changed.map((s, i) => (
                  <span key={s.id}>
                    {i ? ", " : null}
                    {linkTo(s)}
                  </span>
                ))}
                .
              </>
            ) : null}
            {refreshed.gone.length ? (
              <>
                {" "}
                {refreshed.gone.map((s) => s.hrid).join(", ")}{" "}
                {refreshed.gone.length === 1 ? "is" : "are"} no longer in this list, so{" "}
                {refreshed.gone.length === 1 ? "it’s" : "they’re"} left out.
              </>
            ) : null}
          </p>
        </div>
      ) : null}
      <p>
        Each record changes as a revision of its own, with your name and reason. Records whose state
        locks them are left as they are, their people locked with them. If any changed since you
        selected them, none is reassigned.
      </p>
      {fields.length > 1 ? (
        <SelectField
          label="Field"
          value={name}
          options={fields.map((f) => ({ value: f.name, label: f.label }))}
          onChange={(value) => {
            setName(value);
            setPerson(null);
          }}
        />
      ) : null}
      {many ? (
        <SelectField<Op>
          label="Change"
          value={op}
          options={[
            { value: "set", label: `Name only them as ${field.label}` },
            { value: "add", label: "Add them" },
            { value: "remove", label: "Take them off" },
          ]}
          onChange={setOp}
        />
      ) : null}
      <PersonPicker
        label={many ? "Person" : field.label}
        projectId={projectId}
        role={field.role}
        roleLabel={field.role_label}
        value={person}
        onChange={setPerson}
        help={
          many && op !== "set"
            ? undefined
            : `Leave it empty to name nobody.${field.role_label ? ` Someone with the ${field.role_label} role.` : ""}`
        }
      />
      <TextField
        label="Reason"
        value={reason}
        onChange={setReason}
        maxLength={2000}
        optional
        multiline
        help="Why, for the record. It’s kept in each one’s history."
      />
    </ConfirmDialog>
  );
}
