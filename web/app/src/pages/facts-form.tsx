/**
 * Changing a registry entry's facts (ADR 0115), with a reason, from the revision the
 * person read (If-Match): a registered entity's, on its registry entry's page, and a lab
 * list's option's (ADR 0270), which is an entry too, on its list's page. The server makes
 * both the one change (`entries.curate`); this is the one form for it. It also adds a
 * list's option, which starts from nothing.
 *
 * If someone changes it while the form is open, nothing is saved from the old revision:
 * their values come in where this person hadn't typed, the form says which, keeps what
 * both changed as this person typed it, keeps the reason, and saves when they save again.
 */

import { isStale } from "@ikisci/sdk";
import type { QueryKey } from "@tanstack/react-query";
import { useMemo, useState, type ReactNode, type SubmitEvent } from "react";

import { useApiMutation } from "../api";
import { inSentence, type EntityType, type FieldDef } from "../entities";
import { FieldInput } from "../entity-inputs";
import { encode, isBlank, same, type Draft } from "../entity-values";
import { required, useForm } from "../forms";
import { useRevisioned, type IfMatch } from "../revisioned";
import { Button, TextField } from "../ui";

type Drafts = Record<string, Draft>;

/** What changed under the form, by the fields' labels. */
interface Changed {
  /** Someone else changed them and this person hadn't: they show the new values. */
  updated: string[];
  /** Both changed them: this person's values are kept. */
  both: string[];
}

export interface FactsFormProps<T extends { revision: number }> {
  /** What it changes, as the page has it now; none, to add one. */
  item: T | undefined;
  /** What it's called, in a sentence: "PRT-000001", "Dialysis". */
  name: string;
  /** The fields it changes, in order. */
  fields: FieldDef[];
  type: EntityType;
  types: EntityType[];
  /** Whose lab lists a reference to one picks from (ADR 0270). */
  registryId: string;
  /** Its fields as drafts. */
  draftsOf: (item: T | undefined) => Drafts;
  /** Saves what changed (every field, to add one), from the revision read. */
  save: (change: {
    values: Record<string, unknown>;
    reason: string;
    header?: IfMatch;
  }) => Promise<T>;
  /** What saving makes stale, and what's read again when it finds it changed. */
  invalidate: readonly QueryKey[];
  refresh: QueryKey;
  /** What the reason is for, and what it says when it's missing; it's optional to add. */
  reasonHelp: string;
  reasonMissing: string;
  submitLabel: string;
  onSaved: (saved: T) => void;
  onCancel: () => void;
  /** Its heading, which names the form, where nothing around it does. */
  labelledBy?: string;
  /** What it's shown in, such as a dialog, which mustn't close while it saves. */
  frame?: (form: ReactNode, busy: boolean) => ReactNode;
  children?: ReactNode;
}

export function FactsForm<T extends { revision: number }>({
  item,
  name,
  fields,
  type,
  types,
  registryId,
  draftsOf,
  save: saveIt,
  invalidate,
  refresh,
  reasonHelp,
  reasonMissing,
  submitLabel,
  onSaved,
  onCancel,
  labelledBy,
  frame = (form) => form,
  children,
}: FactsFormProps<T>) {
  const adding = item === undefined;
  const [base, setBase] = useState<Drafts>(() => draftsOf(item));
  const [drafts, setDrafts] = useState<Drafts>(base);
  const [reason, setReason] = useState("");
  const [unchanged, setUnchanged] = useState(false);
  const [changed, setChanged] = useState<Changed | null>(null);
  const save = useApiMutation(saveIt, invalidate);
  // The item as the page has it, with a revision even before there's one to add.
  const live = useMemo(() => ({ revision: item?.revision ?? 0, item }), [item]);
  const rev = useRevisioned(live, {
    error: save.error,
    refresh,
    onCatchUp: (latest) => {
      const theirs = draftsOf(latest.item);
      const updated: string[] = [];
      const both: string[] = [];
      const next: Drafts = { ...drafts };
      for (const f of fields) {
        if (same(theirs[f.name], base[f.name])) continue;
        if (same(drafts[f.name], base[f.name])) {
          next[f.name] = theirs[f.name] ?? null;
          updated.push(f.label);
        } else if (!same(drafts[f.name], theirs[f.name])) {
          both.push(f.label);
        }
      }
      setBase(theirs);
      setDrafts(next);
      setChanged({ updated, both });
    },
  });
  const form = useForm(isStale(save.error) ? null : save.error, save.isPending);
  const reasonError = form.errors["reason"] ?? form.errors["values/reason"];

  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    setUnchanged(false);
    const values: Record<string, unknown> = {};
    const checks: Record<string, string | false> = adding
      ? {}
      : { reason: required(reason, reasonMissing) };
    for (const f of fields) {
      const draft = drafts[f.name] ?? null;
      if (!adding && same(draft, base[f.name])) continue;
      if (isBlank(f, draft)) {
        if (f.required) checks[`values/${f.name}`] = `Enter its ${inSentence(f.label)}.`;
        else if (!adding) values[f.name] = null;
        continue;
      }
      const encoded = encode(f, draft, type);
      if (encoded.ok) values[f.name] = encoded.value;
      else checks[`values/${f.name}`] = encoded.error;
    }
    if (!form.validate(checks)) return;
    if (!adding && !Object.keys(values).length) {
      setUnchanged(true);
      return;
    }
    if (adding) {
      save.mutate({ values, reason: reason.trim() }, { onSuccess: onSaved });
      return;
    }
    rev.submit((header) => {
      setChanged(null);
      save.mutate({ values, reason: reason.trim(), header }, { onSuccess: onSaved });
    });
  };

  return frame(
    <form {...form.props} aria-labelledby={labelledBy} onSubmit={submit}>
      {changed ? (
        <div className="alert alert-warning" role="alert">
          <p>
            Someone changed {name} while you were editing (it’s at revision {rev.read.revision}{" "}
            now), so nothing was saved yet.
          </p>
          {changed.updated.length || changed.both.length ? (
            <ul>
              {changed.updated.length ? (
                <li>
                  {changed.updated.join(", ")} now{" "}
                  {changed.updated.length === 1 ? "has its new value" : "have their new values"}.
                </li>
              ) : null}
              {changed.both.length ? (
                <li>
                  You both changed {changed.both.join(", ")}: yours{" "}
                  {changed.both.length === 1 ? "is" : "are"} kept. Check theirs before you save.
                </li>
              ) : null}
            </ul>
          ) : null}
          <p>Save again to make your change from it.</p>
        </div>
      ) : rev.changed ? (
        <div className="alert alert-warning" role="alert">
          <p>Someone changed {name} while you were editing, so nothing was saved yet.</p>
        </div>
      ) : unchanged ? (
        <div className="alert alert-warning" role="alert">
          <p>Nothing to save: you haven’t changed anything.</p>
        </div>
      ) : (
        form.alert
      )}
      {children}
      {fields.map((f) => (
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
          registryId={registryId}
        />
      ))}
      <TextField
        label="Reason"
        value={reason}
        onChange={setReason}
        error={reasonError}
        maxLength={2000}
        multiline
        optional={adding}
        help={reasonHelp}
      />
      <div className="actions">
        <Button
          type="submit"
          variant="primary"
          busy={save.isPending || rev.rereading}
          busyText="Saving…"
        >
          {submitLabel}
        </Button>
        <Button variant="quiet" onClick={onCancel} disabled={save.isPending}>
          Cancel
        </Button>
      </div>
    </form>,
    save.isPending,
  );
}
