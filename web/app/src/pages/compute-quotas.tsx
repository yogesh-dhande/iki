/**
 * Compute → Quotas (ADR 0045): limits on how many jobs run at once and how many runs wait,
 * for the organization, a team or a project, on one tier or all. Compute admins set,
 * change and remove them; every change is audited.
 */

import { unwrap } from "@ikisci/sdk";
import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { useDeferredValue, useState, type SubmitEvent } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { iki, meQuery, problemOf, useApiMutation } from "../api";
import {
  COMPUTE,
  quotaScopesQuery,
  quotasQuery,
  TIER_NAME,
  type Quota,
  type QuotaScopeKind,
  type Tier,
} from "../compute";
import { useForm } from "../forms";
import { usePageTitle } from "../layout";
import {
  Button,
  ConfirmDialog,
  Loading,
  PageHeader,
  ProblemAlert,
  RadioField,
  SelectField,
  TextField,
  useMessages,
} from "../ui";
import { ComputeTabs } from "./compute";
import { NotFound } from "./errors";

export function ComputeQuotasPage() {
  usePageTitle("Quotas");
  const me = useSuspenseQuery(meQuery).data;
  if (!me.can.manage_compute) return <NotFound />;
  return (
    <>
      <PageHeader title="Quotas" eyebrow="Compute" />
      <ComputeTabs />
      <Quotas />
    </>
  );
}

/** "The organization", "Structural biology (team)", "PDB-2291 (project)". */
export function scopeName(quota: Quota): string {
  if (quota.project) return quota.project.name;
  if (quota.team) return quota.team.name;
  return "The organization";
}

function scopeKind(quota: Quota): string {
  return quota.project ? "Project" : quota.team ? "Team" : "Organization";
}

/** "3 of 4", "3, no limit", or "Paused" for a limit of 0. */
export function usage(now: number, limit: number | null | undefined): string {
  if (limit === null || limit === undefined) return `${String(now)}, no limit`;
  if (limit === 0) return now ? `Paused (${String(now)} still running)` : "Paused";
  return `${String(now)} of ${String(limit)}`;
}

function Quotas() {
  const quotas = useQuery(quotasQuery);
  const [setting, setSetting] = useState<Quota | "new" | undefined>();
  const [removing, setRemoving] = useState<Quota | undefined>();
  return (
    <section className="card card-wide" aria-labelledby="quotas">
      <h2 className="section-title" id="quotas">
        Run quotas
      </h2>
      <p className="muted">
        A quota limits the jobs running at once in its scope, or the runs waiting, or both. Every
        quota that covers a run applies: a project’s and its team’s both hold. Changing one affects
        only what starts next. A limit of 0 running jobs pauses the scope.
      </p>
      {quotas.isPending ? (
        <Loading label="Loading quotas…" />
      ) : quotas.isError ? (
        <ProblemAlert problem={problemOf(quotas.error)} />
      ) : quotas.data.length ? (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">For</th>
                <th scope="col">Tier</th>
                <th scope="col">Running</th>
                <th scope="col">Waiting</th>
                <th scope="col">
                  <span className="visually-hidden">Change</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {quotas.data.map((quota) => {
                const name = scopeName(quota);
                return (
                  <tr key={quota.id}>
                    <th scope="row">
                      {name}
                      <span className="cell-meta">{scopeKind(quota)}</span>
                    </th>
                    <td>{quota.tier ? (TIER_NAME[quota.tier] ?? quota.tier) : "Every tier"}</td>
                    <td>{usage(quota.running, quota.max_running)}</td>
                    <td>{usage(quota.queued, quota.max_queued)}</td>
                    <td className="row-actions">
                      <Button
                        variant="quiet"
                        label={`Change ${name}’s quota`}
                        onClick={() => {
                          setSetting(quota);
                        }}
                      >
                        Change
                      </Button>
                      <Button
                        variant="quiet"
                        label={`Remove ${name}’s quota`}
                        onClick={() => {
                          setRemoving(quota);
                        }}
                      >
                        Remove
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">
          No quotas: fair share alone decides which runs start first, and anyone may queue as many
          as they like.
        </p>
      )}
      <p>
        <Button
          onClick={() => {
            setSetting("new");
          }}
        >
          Set a quota
        </Button>
      </p>
      {setting ? (
        <QuotaDialog
          quota={setting === "new" ? undefined : setting}
          onClose={() => {
            setSetting(undefined);
          }}
        />
      ) : null}
      {removing ? (
        <RemoveQuota
          quota={removing}
          onClose={() => {
            setRemoving(undefined);
          }}
        />
      ) : null}
    </section>
  );
}

const SCOPE_KINDS = [
  { value: "organization", label: "The organization", description: "Every run." },
  { value: "team", label: "A team", description: "The runs in all its projects." },
  { value: "project", label: "A project" },
] as const;

const TIERS = [
  { value: "all", label: "Every tier together" },
  { value: "client", label: "Client" },
  { value: "backend", label: "Backend" },
  { value: "container", label: "Container" },
  { value: "vm", label: "VM" },
] as const;

/** A limit as typed: blank for none, else a whole number in range. */
function limitError(text: string, max: number): string | false {
  if (text.trim() === "") return false;
  const n = Number(text);
  return (
    (!Number.isInteger(n) || n < 0 || n > max) &&
    `Enter a whole number from 0 to ${max.toLocaleString()}, or leave it blank.`
  );
}

function limitOf(text: string): number | null {
  return text.trim() === "" ? null : Number(text);
}

function QuotaDialog({ quota, onClose }: { quota: Quota | undefined; onClose: () => void }) {
  const { say } = useMessages();
  const [kind, setKind] = useState<"organization" | QuotaScopeKind>("organization");
  const [search, setSearch] = useState("");
  const [picked, setPicked] = useState("");
  const [tier, setTier] = useState<"all" | Tier>("all");
  const [running, setRunning] = useState(quota?.max_running?.toString() ?? "");
  const [queued, setQueued] = useState(quota?.max_queued?.toString() ?? "");
  const term = useDeferredValue(search);
  const scopes = useQuery({
    ...quotaScopesQuery(kind === "organization" ? "team" : kind, term),
    enabled: quota === undefined && kind !== "organization",
  });

  const save = useApiMutation(async () => {
    const limits = { max_running: limitOf(running), max_queued: limitOf(queued) };
    if (quota) {
      return unwrap(
        await iki.PATCH("/api/v1/compute/quotas/{quota_id}", {
          params: { path: { quota_id: quota.id } },
          body: limits,
        }),
      );
    }
    return unwrap(
      await iki.POST("/api/v1/compute/quotas", {
        body: {
          ...limits,
          ...(kind === "team" ? { team: picked } : {}),
          ...(kind === "project" ? { project: picked } : {}),
          ...(tier === "all" ? {} : { tier }),
        },
      }),
    );
  }, [COMPUTE]);
  const form = useForm(save.error);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const ok = form.validate({
      scope:
        quota === undefined &&
        kind !== "organization" &&
        picked === "" &&
        `Choose the ${kind} it limits.`,
      max_running:
        limitError(running, 100_000) ||
        (running.trim() === "" && queued.trim() === "" && "Set at least one limit."),
      max_queued: limitError(queued, 1_000_000),
    });
    if (!ok) return;
    save.mutate(undefined, {
      onSuccess: (saved) => {
        say(
          quota
            ? `Changed ${scopeName(saved)}’s quota. It holds back what starts next.`
            : `Set a quota for ${scopeName(saved)}.`,
        );
        onClose();
      },
    });
  }

  const title = quota ? `Change ${scopeName(quota)}’s quota` : "Set a quota";
  return (
    <ModalOverlay
      className="dialog-overlay"
      isOpen
      isDismissable={!save.isPending}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <Modal className="dialog">
        <Dialog className="dialog-body">
          <Heading slot="title" className="section-title">
            {title}
          </Heading>
          {form.alert}
          <form {...form.props} onSubmit={submit}>
            {quota ? (
              <p>
                On {quota.tier ? `the ${TIER_NAME[quota.tier] ?? quota.tier} tier` : "every tier"}.
                Leave a limit blank to remove it.
              </p>
            ) : (
              <>
                <RadioField
                  label="For"
                  value={kind}
                  onChange={(value) => {
                    setKind(value);
                    setPicked("");
                    setSearch("");
                  }}
                  options={SCOPE_KINDS}
                />
                {kind === "organization" ? null : (
                  <>
                    <TextField
                      label={kind === "team" ? "Find a team" : "Find a project"}
                      value={search}
                      onChange={setSearch}
                      maxLength={100}
                      help="Any of them, whether you’re a member or not. Archived ones aren’t listed."
                      optional
                    />
                    {scopes.isPending ? (
                      <Loading label={`Loading ${kind}s…`} />
                    ) : scopes.isError ? (
                      <ProblemAlert problem={problemOf(scopes.error)} />
                    ) : scopes.data.length ? (
                      <RadioField
                        label={kind === "team" ? "Team" : "Project"}
                        value={picked}
                        onChange={setPicked}
                        error={
                          form.errors["scope"] ?? form.errors["team"] ?? form.errors["project"]
                        }
                        options={scopes.data.map((s) => ({
                          value: s.id,
                          label: s.name,
                          ...(s.team ? { description: `${s.team} team` } : {}),
                        }))}
                      />
                    ) : (
                      <p className="muted" role="status">
                        No {kind}s{term.trim() ? ` with “${term.trim()}” in the name` : ""}.
                      </p>
                    )}
                  </>
                )}
                <SelectField label="Tier" value={tier} onChange={setTier} options={TIERS} />
              </>
            )}
            <TextField
              label="Jobs running at once"
              value={running}
              onChange={setRunning}
              error={form.errors["max_running"]}
              help="More wait, even with a worker free, and each says why. 0 pauses the scope."
              optional
            />
            <TextField
              label="Runs waiting"
              value={queued}
              onChange={setQueued}
              error={form.errors["max_queued"]}
              help="Launching past it is refused, saying so."
              optional
            />
            <div className="actions">
              <Button type="submit" variant="primary" busy={save.isPending} busyText="Saving…">
                {quota ? "Save" : "Set quota"}
              </Button>
              <Button variant="quiet" onClick={onClose}>
                Cancel
              </Button>
            </div>
          </form>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}

function RemoveQuota({ quota, onClose }: { quota: Quota; onClose: () => void }) {
  const { say } = useMessages();
  const name = scopeName(quota);
  const remove = useApiMutation(async () => {
    unwrap(
      await iki.DELETE("/api/v1/compute/quotas/{quota_id}", {
        params: { path: { quota_id: quota.id } },
      }),
    );
  }, [COMPUTE]);
  return (
    <ConfirmDialog
      isOpen
      title={`Remove ${name}’s quota?`}
      confirmLabel="Remove quota"
      busy={remove.isPending}
      problem={remove.error ? problemOf(remove.error) : undefined}
      onCancel={onClose}
      onConfirm={() => {
        remove.mutate(undefined, {
          onSuccess: () => {
            say(`Removed ${name}’s quota.`);
            onClose();
          },
        });
      }}
    >
      <p>
        Its runs are no longer held back by it
        {quota.max_running === 0 ? ": the paused scope starts again" : ""}. Other quotas that cover
        them still apply.
      </p>
    </ConfirmDialog>
  );
}
