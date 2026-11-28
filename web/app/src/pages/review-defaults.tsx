/**
 * Admin → Review procedures (ADR 0302): the deployment's default review for each reviewed
 * type, set by an Org Admin from a plugin's starter. Every project that hasn't chosen its
 * own pins new records of the type to the default's flow; without one, they can't be made
 * until a project's Admin chooses. Setting, changing or clearing a default is a controlled
 * procedure change, so each needs a reason, kept with it in the audit log. Records already
 * made keep their procedure.
 */

import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { useId, useState, type SubmitEvent } from "react";

import { labRolesQuery, problemOf, useApiMutation } from "../api";
import { useForm } from "../forms";
import { usePageTitle } from "../layout";
import {
  clearStarterDefault,
  deploymentStartersQuery,
  setStarterDefault,
  STARTERS,
  type Starter,
} from "../starters";
import {
  Badge,
  Button,
  ConfirmDialog,
  PageHeader,
  RadioField,
  RelativeTime,
  SelectField,
  TextField,
  useMessages,
} from "../ui";

export function ReviewDefaults() {
  usePageTitle("Review procedures");
  const starters = useSuspenseQuery(deploymentStartersQuery).data.filter((s) => s.procedure);
  return (
    <>
      <PageHeader title="Review procedures" />
      <section className="card card-wide">
        <p>
          A project’s Admin chooses how each kind of record is reviewed in the project, from a
          starter in Project settings → Starters. A default here applies in every project that
          hasn’t chosen. Without either, Iki refuses to create the record, so nothing is made under
          a procedure no one chose.
        </p>
        <p className="muted">
          A default needs a reason, kept with it in the audit log. Changing or clearing one changes
          only the records made from then on.
        </p>
      </section>
      {starters.length === 0 ? (
        <section className="card card-wide">
          <p className="empty">
            No installed plugin offers a starter for a review procedure. A plugin that does adds it
            here.
          </p>
        </section>
      ) : null}
      {starters.map((s) => (
        <DefaultCard key={s.id} starter={s} />
      ))}
    </>
  );
}

function DefaultCard({ starter }: { starter: Starter }) {
  const id = useId();
  const { say } = useMessages();
  const labRoles = useQuery(labRolesQuery);
  const current = starter.default;
  const [flowId, setFlowId] = useState(
    current?.flow ?? starter.suggested ?? starter.flows[0]?.id ?? "",
  );
  const [roles, setRoles] = useState<Record<string, string>>(() =>
    Object.fromEntries(starter.slots.map((s) => [s.id, s.role])),
  );
  const [reason, setReason] = useState("");
  const [clearing, setClearing] = useState(false);
  const [clearReason, setClearReason] = useState("");
  const flow = starter.flows.find((f) => f.id === flowId) ?? starter.flows[0];
  const flowLabel = (f: string) => starter.flows.find((x) => x.id === f)?.label ?? f;
  const used = new Set((flow?.steps ?? []).flatMap((s) => s.slots));
  const slots = starter.slots.filter((s) => used.has(s.id));
  const roleOptions = (labRoles.data ?? []).map((r) => ({ value: r.name, label: r.label }));

  const set = useApiMutation(
    () =>
      setStarterDefault(starter.id, {
        version: starter.version,
        flow: flow?.id ?? "",
        slots: Object.fromEntries(
          starter.slots
            .filter((s) => (roles[s.id] ?? s.role) !== s.starter_role)
            .map((s) => [s.id, roles[s.id] ?? s.role]),
        ),
        reason: reason.trim(),
      }),
    [STARTERS],
  );
  const clear = useApiMutation(
    () => clearStarterDefault(starter.id, clearReason.trim()),
    [STARTERS],
  );
  const form = useForm(set.error, set.isPending);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      !form.validate({
        reason:
          !reason.trim() &&
          "Say why: a deployment’s default is a controlled procedure change, kept with its reason.",
      })
    )
      return;
    set.mutate(undefined, {
      onSuccess: () => {
        setReason("");
        say(
          `New ${starter.governs} default to ${flow?.label ?? ""} where a project hasn’t chosen.`,
        );
      },
    });
  }

  return (
    <section className="card card-wide starter" aria-labelledby={`${id}-title`}>
      <div className="section-header">
        <h2 className="section-title" id={`${id}-title`}>
          {starter.label}
        </h2>
        <span className="tags">
          <Badge muted>Version {starter.version}</Badge>
          {current ? (
            <Badge>Default: {flowLabel(current.flow)}</Badge>
          ) : (
            <Badge muted>No default</Badge>
          )}
        </span>
      </div>
      <p>{starter.description}</p>
      {current ? (
        <p className="muted">
          Set by {current.chosen_by} <RelativeTime value={current.chosen_at} />: “{current.reason}”
        </p>
      ) : (
        <p className="muted">
          Projects that haven’t chosen can’t create {starter.governs} until they do.
        </p>
      )}
      <form {...form.props} onSubmit={submit}>
        {form.alert}
        <RadioField
          label="Review flow"
          value={flow?.id ?? ""}
          onChange={setFlowId}
          options={starter.flows.map((f) => ({
            value: f.id,
            label: f.id === starter.suggested ? `${f.label} (suggested)` : f.label,
            description: f.summary,
          }))}
        />
        {roleOptions.length > 1
          ? slots.map((slot) => (
              <SelectField
                key={slot.id}
                label={`Lab role that plays ${slot.label}`}
                value={roles[slot.id] ?? slot.role}
                onChange={(value) => {
                  setRoles({ ...roles, [slot.id]: value });
                }}
                options={roleOptions}
              />
            ))
          : null}
        <TextField
          label="Reason"
          value={reason}
          onChange={setReason}
          maxLength={2000}
          multiline
          error={form.errors["reason"]}
          help="Such as the SOP that governs this work, and its version."
        />
        <div className="actions">
          <Button type="submit" variant="primary" busy={set.isPending} busyText="Setting…">
            {current ? "Change the default" : "Set as the default"}
          </Button>
          {current ? (
            <Button
              variant="quiet"
              onClick={() => {
                setClearing(true);
              }}
            >
              Clear the default
            </Button>
          ) : null}
        </div>
      </form>
      <ConfirmDialog
        isOpen={clearing}
        title={`Clear the default for ${starter.governs}?`}
        confirmLabel="Clear"
        confirmDisabled={!clearReason.trim()}
        busy={clear.isPending}
        problem={clear.error ? problemOf(clear.error) : undefined}
        onCancel={() => {
          setClearing(false);
          clear.reset();
        }}
        onConfirm={() => {
          clear.mutate(undefined, {
            onSuccess: () => {
              setClearing(false);
              setClearReason("");
              say(`Cleared the default for ${starter.governs}.`);
            },
          });
        }}
      >
        <p>
          Projects that haven’t chosen their own can’t create {starter.governs} until their Admin
          chooses. Records already made keep their procedure.
        </p>
        <TextField
          label="Reason"
          value={clearReason}
          onChange={setClearReason}
          maxLength={2000}
          multiline
        />
      </ConfirmDialog>
    </section>
  );
}
