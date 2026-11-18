/**
 * Reassigning the records picked in the grid (ADR 0122): one assignment field of each,
 * named for someone else (or emptied), each record as its own revision. Records that can't
 * change are left as they are, and the dialog says why.
 */

import { useState } from "react";

import { problemOf, useApiMutation } from "../api";
import { inSentence, type Entity, type EntityType } from "../entities";
import { PersonPicker } from "../entity-inputs";
import type { Picked } from "../entity-values";
import { AFTER_ASSIGNING, assignMany, type Assigned } from "../people";
import { ConfirmDialog, SelectField, TextField, useMessages } from "../ui";

type Op = "set" | "add" | "remove";

export function ReassignDialog({
  type,
  picked,
  projectId,
  onClose,
}: {
  type: EntityType;
  picked: Entity[];
  projectId: string;
  onClose: (done: boolean) => void;
}) {
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
        entities: picked.map((e) => e.id),
        field: name,
        person: person?.id ?? null,
        op: many ? op : "set",
        reason: reason.trim(),
      }),
    AFTER_ASSIGNING,
  );
  if (!field) return null;
  const records = `${String(picked.length)} ${inSentence(picked.length === 1 ? type.label : type.label_plural)}`;
  if (done) {
    const hrids = new Map(picked.map((e) => [e.id, e.hrid]));
    return (
      <ConfirmDialog
        isOpen
        title={`Reassigned ${String(done.changed.length)} of ${String(picked.length)}`}
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
      busy={assign.isPending}
      problem={assign.error ? problemOf(assign.error) : undefined}
      onCancel={() => {
        onClose(false);
      }}
      onConfirm={() => {
        assign.mutate(undefined, {
          onSuccess: (result) => {
            say(
              `Reassigned ${String(result.changed.length)} of ${String(picked.length)}` +
                (result.skipped.length
                  ? `; ${String(result.skipped.length)} left as they were.`
                  : "."),
            );
            setDone(result);
          },
        });
      }}
    >
      <p>
        Each record changes as a revision of its own, with your name and reason. Records whose state
        locks them are left as they are, their people locked with them.
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
