/**
 * A record's electronic signatures under 21 CFR Part 11 (ADR 0126): the states that need
 * them, each signature given or whether this person may give it, and every signature as
 * people read it (§11.50), checked again on request (§11.70).
 *
 * Signing sends the person to their identity provider to sign in again, both components
 * of their sign-in (§11.200); it answers to the server, which sends them back here with
 * `?signing=` and the attempt's ID, whose outcome this page then shows.
 */

import { ApiError, unwrap, type Schemas } from "@ikisci/sdk";
import { keepPreviousData, queryOptions, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useRouterState, useSearch } from "@tanstack/react-router";
import { useEffect, useId, useState } from "react";

import { fieldErrors, iki, meQuery, problemOf, useApiMutation } from "../api";
import { apiRef, ENTITIES, type Entity } from "../entities";
import {
  Badge,
  Button,
  CheckboxField,
  ConfirmDialog,
  ProblemAlert,
  TextField,
  useMessages,
} from "../ui";

type SignCan = Schemas["SignCan"];
type Slot = Schemas["SignatureCan"];
type Signature = Schemas["SignatureOut"];
type Signing = Schemas["SigningOut"];

export const SIGNATURES = [...ENTITIES, "signatures"] as const;

export function signaturesQuery(ref: string, verify: boolean) {
  return queryOptions({
    queryKey: [...SIGNATURES, ref.toUpperCase(), verify],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/entities/{ref}/signatures", {
          params: { path: { ref }, query: { verify } },
          signal,
        }),
      ),
    // Checking them again keeps showing them meanwhile.
    placeholderData: keepPreviousData,
  });
}

export const mySigningQuery = queryOptions({
  queryKey: ["me", "signing"],
  queryFn: async ({ signal }) => unwrap(await iki.GET("/api/v1/me/signing", { signal })),
});

function signingQuery(id: string) {
  return queryOptions({
    queryKey: ["signings", id],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/signings/{signing_id}", {
          params: { path: { signing_id: id } },
          signal,
        }),
      ),
    staleTime: Infinity,
  });
}

/** Leaves the app for the identity provider. A function of its own, for tests to replace. */
export const leave = {
  to(url: string): void {
    window.location.assign(url);
  },
};

const STATUS: Record<Signature["status"], string> = {
  carried: "Carried",
  pending: "Waiting for others",
  withdrawn: "Withdrawn",
  lapsed: "Lapsed",
};

export function EntitySignatures({ entity, signing }: { entity: Entity; signing?: string }) {
  const id = useId();
  const ref = apiRef(entity);
  const [verify, setVerify] = useState(false);
  const listed = useQuery(signaturesQuery(ref, verify));
  const signatures = listed.data?.items ?? [];
  const missing = listed.data?.missing ?? [];
  const wanted = entity.can.sign;
  const version = `${entity.id}:${String(entity.revision)}:${entity.entry?.id ?? ""}:${String(entity.entry?.revision ?? "")}`;
  const [chosen, setChosen] = useState<{
    field: string;
    to: string;
    index: number;
    version: string;
  } | null>(null);
  const [withdrawing, setWithdrawing] = useState<Signature | null>(null);
  const chosenChange = chosen
    ? wanted.find((change) => change.field === chosen.field && change.to === chosen.to)
    : undefined;
  const chosenSlot = chosenChange?.signatures.find((slot) => slot.index === chosen?.index);
  if (chosen && (!chosenChange || !chosenSlot)) setChosen(null);
  if (!signing && !wanted.length && !signatures.length && !missing.length) return null;
  return (
    <section className="card" aria-labelledby={`${id}-title`}>
      <h2 className="section-title" id={`${id}-title`}>
        Signatures
      </h2>
      {signing ? <SigningResult entity={entity} signing={signing} /> : null}
      {wanted.map((change) => (
        <Wanted
          key={`${change.field}:${change.to}`}
          change={change}
          onSign={(slot) => {
            setChosen({ field: change.field, to: change.to, index: slot.index, version });
          }}
        />
      ))}
      {missing.length ? (
        <div className="alert alert-error" role="alert">
          <p>
            {missing.length === 1 ? "A signature" : `${String(missing.length)} signatures`} this
            record was signed with {missing.length === 1 ? "is" : "are"} gone from the database:
            something outside Iki deleted {missing.length === 1 ? "it" : "them"}. Tell your Org
            Admin.
          </p>
        </div>
      ) : null}
      {signatures.length ? (
        <>
          <h3 className="section-title">Given</h3>
          <ul className="rows" aria-label="Signatures given">
            {signatures.map((s) => (
              <Given
                key={s.id}
                signature={s}
                onWithdraw={() => {
                  setWithdrawing(s);
                }}
              />
            ))}
          </ul>
          <div className="actions">
            <Button
              busy={verify && listed.isFetching}
              busyText="Checking…"
              onClick={() => {
                if (verify) void listed.refetch();
                else setVerify(true);
              }}
            >
              Check signatures
            </Button>
          </div>
        </>
      ) : null}
      {chosen && chosenChange && chosenSlot ? (
        <SignDialog
          key={version}
          entity={entity}
          change={chosenChange}
          slot={chosenSlot}
          changed={chosen.version !== version}
          onClose={() => {
            setChosen(null);
          }}
        />
      ) : null}
      {withdrawing ? (
        <WithdrawDialog
          entity={entity}
          signature={withdrawing}
          onClose={() => {
            setWithdrawing(null);
          }}
        />
      ) : null}
    </section>
  );
}

function Wanted({ change, onSign }: { change: SignCan; onSign: (slot: Slot) => void }) {
  const id = useId();
  const count = change.signatures.length;
  return (
    <>
      <h3 className="section-title">
        {change.field_label}: {change.to_label}
      </h3>
      <p className="muted">
        It’s set to {change.to_label} once{" "}
        {count === 1 ? "this signature is" : `these ${String(count)} signatures are`} given, each by
        someone who signs in again, on this revision. A change to the record before then sets them
        aside.
      </p>
      <ul className="rows" aria-label={`Signatures for ${change.to_label}`}>
        {change.signatures.map((slot) => {
          const why = `${id}-${String(slot.index)}-why`;
          return (
            <li key={slot.index} className="row">
              <div className="row-main">
                <span className="row-title">
                  {slot.label} <Badge muted>{slot.meaning_label}</Badge>
                </span>
                <span className="row-meta">
                  “{slot.statement}” · {slot.role_labels.join(" or ")}
                </span>
                {slot.given ? (
                  <span className="row-meta">{slot.given.manifestation}</span>
                ) : (
                  <span className="row-meta" id={why}>
                    {slot.lapsed ? `${slot.lapsed} ` : ""}
                    {slot.why_not ?? "Not given yet."}
                  </span>
                )}
              </div>
              {slot.given ? null : (
                <div className="row-actions">
                  <Button
                    variant="primary"
                    disabled={!slot.allowed}
                    describedBy={why}
                    label={`Sign: ${slot.label}`}
                    onClick={() => {
                      onSign(slot);
                    }}
                  >
                    Sign
                  </Button>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </>
  );
}

function Given({ signature, onWithdraw }: { signature: Signature; onWithdraw: () => void }) {
  const me = useQuery(meQuery).data;
  const checked = signature.verification;
  const mine = me?.id === signature.signer.id;
  return (
    <li className="row">
      <div className="row-main">
        <span className="row-title">{signature.manifestation}</span>
        <span className="row-meta">
          {signature.label}: “{signature.statement}” · revision {signature.revision} ·{" "}
          {signature.change.field_label} {signature.change.from_label} to{" "}
          {signature.change.to_label}
          {signature.reason ? ` · Reason: ${signature.reason}` : ""}
        </span>
        <span className="row-meta">
          <Badge muted={signature.status !== "carried"}>{STATUS[signature.status]}</Badge>{" "}
          {signature.status_detail}
        </span>
        {checked ? (
          checked.valid ? (
            <span className="row-meta">
              <Badge>Verified</Badge> Its key, what it signed and the record’s history all check
              out.
            </span>
          ) : (
            <div className="alert alert-error" role="alert">
              <p>This signature doesn’t verify:</p>
              <ul>
                {checked.checks
                  .filter((c) => !c.ok)
                  .map((c) => (
                    <li key={c.name}>{c.detail}</li>
                  ))}
              </ul>
            </div>
          )
        ) : null}
      </div>
      {mine && signature.status === "pending" ? (
        <div className="row-actions">
          <Button
            variant="quiet"
            label={`Withdraw your signature: ${signature.label}`}
            onClick={onWithdraw}
          >
            Withdraw
          </Button>
        </div>
      ) : null}
    </li>
  );
}

function SignDialog({
  entity,
  change,
  slot,
  changed,
  onClose,
}: {
  entity: Entity;
  change: SignCan;
  slot: Slot;
  changed: boolean;
  onClose: () => void;
}) {
  // Whether it's their first signature, and so needs their agreement, comes after the
  // dialog opens: it can't be signed until it's known, so the agreement is shown first.
  const signing = useQuery(mySigningQuery);
  const mine = signing.data;
  // Back to this page: the server adds `?signing=` and the attempt's ID.
  const path = useRouterState({ select: (s) => s.location.pathname });
  const { project } = useSearch({ from: "/entities/$ref" });
  const here = project ? `${path}?project=${encodeURIComponent(project)}` : path;
  const [reason, setReason] = useState("");
  const [agreed, setAgreed] = useState(false);
  const [missing, setMissing] = useState<"" | "reason" | "agreement">("");
  const begin = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/entities/{ref}/signatures", {
          params: {
            path: { ref: entity.id },
            header: { "If-Match": `"${String(entity.revision)}"` },
          },
          body: {
            field: change.field,
            state: change.to,
            index: slot.index,
            reason: reason.trim(),
            acknowledge: agreed,
            return_to: here,
            ...(entity.entry ? { entry_revision: entity.entry.revision } : {}),
          },
        }),
      ),
    [],
  );
  const client = useQueryClient();
  const problem = begin.error ? problemOf(begin.error) : undefined;
  const errors = fieldErrors(problem);
  const stale = begin.error instanceof ApiError && begin.error.problem.code === "stale";
  const acknowledging = mine ? !mine.acknowledged : false;
  const reasonRequired = change.reason === "required" || (changed && change.reason === "optional");
  const reasonError =
    errors["reason"] ??
    (missing === "reason" ? "Say why: it’s signed with your signature." : undefined);
  return (
    <ConfirmDialog
      isOpen
      title={`Sign ${entity.hrid}: ${slot.label}`}
      confirmLabel="Sign in again to sign"
      confirmVariant="primary"
      confirmDisabled={mine === undefined || stale || !slot.allowed}
      busy={begin.isPending || begin.isSuccess}
      problem={
        stale && problem
          ? {
              ...problem,
              detail:
                "It changed while this was open. Read the current revision before signing again.",
            }
          : problem && !errors["reason"]
            ? problem
            : signing.error
              ? problemOf(signing.error)
              : mine && !mine.can_sign && mine.why_not
                ? {
                    type: "about:blank",
                    title: "Can’t sign",
                    status: 403,
                    code: "cannot_sign",
                    detail: mine.why_not,
                  }
                : undefined
      }
      onCancel={onClose}
      onConfirm={() => {
        if (reasonRequired && !reason.trim()) {
          setMissing("reason");
          return;
        }
        if (acknowledging && !agreed) {
          setMissing("agreement");
          return;
        }
        begin.mutate(undefined, {
          onSuccess: (started) => {
            leave.to(started.sign_in_url);
          },
          onError: (error) => {
            if (error.problem.code === "stale") {
              setReason("");
              setAgreed(false);
              setMissing("");
              void client.invalidateQueries({ queryKey: ENTITIES });
            }
          },
        });
      }}
    >
      {changed ? (
        <p role="status">
          {reasonRequired
            ? "The record changed. Review the current revision and enter your reason again."
            : "The record changed. Review the current revision before signing."}
        </p>
      ) : null}
      {!slot.allowed && slot.why_not ? <p role="alert">{slot.why_not}</p> : null}
      <p>
        You sign revision {entity.revision} of {entity.hrid} as it is now, for setting its{" "}
        {change.field_label.toLowerCase()} to {change.to_label}.
      </p>
      {entity.entry ? <p>Shared registry facts at revision {entity.entry.revision}.</p> : null}
      <blockquote className="statement">
        <p>
          <strong>{slot.meaning_label}.</strong> {slot.statement}
        </p>
      </blockquote>
      {change.reason ? (
        <TextField
          label="Reason"
          value={reason}
          onChange={(value) => {
            setReason(value);
            setMissing("");
          }}
          maxLength={2000}
          optional={!reasonRequired}
          multiline
          error={reasonError}
          help="Why you sign. It’s part of what you sign, and shown with it."
        />
      ) : null}
      {acknowledging && mine ? (
        <CheckboxField
          label="I agree"
          checked={agreed}
          onChange={(value) => {
            setAgreed(value);
            setMissing("");
          }}
          error={missing === "agreement" ? "Agree to this before your first signature." : undefined}
          announce
          help={mine.acknowledgment}
        />
      ) : null}
      <p className="muted">
        Next, {mine?.provider ?? "your identity provider"} asks you to sign in again, with your
        password and second factor: that sign-in is your signature. It’s kept with your name, the
        date and time, and what it means, and can’t be changed or taken off the record.
      </p>
    </ConfirmDialog>
  );
}

function WithdrawDialog({
  entity,
  signature,
  onClose,
}: {
  entity: Entity;
  signature: Signature;
  onClose: () => void;
}) {
  const [reason, setReason] = useState("");
  const { say } = useMessages();
  const withdraw = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/signatures/{signature_id}/withdrawal", {
          params: { path: { signature_id: signature.id } },
          body: { reason: reason.trim() },
        }),
      ),
    [ENTITIES],
  );
  return (
    <ConfirmDialog
      isOpen
      title={`Withdraw your signature on ${entity.hrid}?`}
      confirmLabel="Withdraw"
      confirmDisabled={!reason.trim()}
      busy={withdraw.isPending}
      problem={withdraw.error ? problemOf(withdraw.error) : undefined}
      onCancel={onClose}
      onConfirm={() => {
        withdraw.mutate(undefined, {
          onSuccess: () => {
            onClose();
            say(`You withdrew your signature on ${entity.hrid}.`);
          },
        });
      }}
    >
      <p>
        It no longer counts toward setting it to {signature.change.to_label}. The signature stays on
        the record, marked withdrawn, with your reason.
      </p>
      <TextField
        label="Reason"
        value={reason}
        onChange={setReason}
        maxLength={2000}
        multiline
        help="Why, for the record."
      />
    </ConfirmDialog>
  );
}

/** What came of signing, once the identity provider sent the person back. */
function SigningResult({ entity, signing }: { entity: Entity; signing: string }) {
  const found = useQuery(signingQuery(signing));
  const navigate = useNavigate();
  const { project } = useSearch({ from: "/entities/$ref" });
  const client = useQueryClient();
  const done: Signing | undefined = found.data;
  useEffect(() => {
    if (done?.status === "signed") void client.invalidateQueries({ queryKey: ENTITIES });
  }, [done?.status, client]);
  if (found.error) return <ProblemAlert problem={problemOf(found.error)} />;
  if (done?.record !== entity.id) return null;
  const dismiss = (
    <Button
      variant="quiet"
      onClick={() => {
        void navigate({
          to: "/entities/$ref/",
          params: { ref: entity.hrid },
          search: project ? { project } : {},
          replace: true,
        });
      }}
    >
      Dismiss
    </Button>
  );
  if (done.status === "signed") {
    return (
      <div className="alert alert-success" role="status">
        <p>
          You signed {done.hrid}.{" "}
          {done.set
            ? `Its ${done.field_label} is ${done.to_label} now.`
            : "Setting it waits for the other signatures."}
        </p>
        {dismiss}
      </div>
    );
  }
  return (
    <div className="alert alert-error" role="alert">
      <p>
        {done.status === "started"
          ? "Your signature wasn’t given: signing in again wasn’t finished."
          : `Your signature wasn’t given. ${done.message}`}
      </p>
      {dismiss}
    </div>
  );
}
