/**
 * Settings → Outside APIs (ADR 0130): every outside API installed plugins send data to,
 * and the projects each is approved for. Org Admins approve one for a project here, for
 * the host and purpose it names, and only here: no token can, so no agent can approve
 * where its runs send data.
 */

import { unwrap } from "@ikisci/sdk";
import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { useState, type SubmitEvent } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { iki, problemOf, projectsQuery, useApiMutation } from "../api";
import { money } from "../costs";
import { useForm } from "../forms";
import { usePageTitle } from "../layout";
import { OUTSIDE_APIS, outsideApisQuery, type OutsideApi } from "../outside-apis";
import {
  Badge,
  Button,
  ConfirmDialog,
  PageHeader,
  RelativeTime,
  SelectField,
  TextField,
  useMessages,
} from "../ui";
import { SettingsTabs } from "./webhooks";

type Approval = OutsideApi["approvals"][number];

function price(api: OutsideApi): string {
  const p = api.price;
  if (!p) return "No price set: its calls cost nothing in Iki’s costs and budgets.";
  const parts = [
    ...(p.per_call !== null ? [`${money(p.per_call, p.currency)} a call`] : []),
    ...Object.entries(p.usage).map(([unit, each]) => `${money(each, p.currency)} per ${unit}`),
  ];
  return `${parts.join(", ")}, counted with each run’s cost and against budgets.`;
}

function ApproveDialog({ api, onClose }: { api: OutsideApi; onClose: () => void }) {
  const { say } = useMessages();
  const projects = useQuery(projectsQuery).data ?? [];
  const approved = new Set(api.approvals.filter((a) => a.current).map((a) => a.project));
  const choices = projects.filter((p) => !p.archived && !approved.has(p.id));
  const [picked, setPicked] = useState("");
  // The projects load after the dialog opens: until one's picked, the first is.
  const project = picked || (choices[0]?.id ?? "");
  const [note, setNote] = useState("");
  const approve = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/outside-apis/{name}/approvals", {
          params: { path: { name: api.api } },
          body: { project, note: note.trim() },
        }),
      ),
    [OUTSIDE_APIS],
  );
  const form = useForm(approve.error);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form.validate({ project: !project && "Choose a project." })) return;
    approve.mutate(undefined, {
      onSuccess: (made) => {
        say(`Approved ${api.label} for ${made.project_name}.`);
        onClose();
      },
    });
  }

  return (
    <ModalOverlay
      className="dialog-overlay"
      isOpen
      isDismissable={!approve.isPending}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <Modal className="dialog">
        <Dialog className="dialog-body">
          <Heading slot="title" className="section-title">
            Approve {api.label} for a project
          </Heading>
          <form {...form.props} onSubmit={submit}>
            {form.alert}
            <p>
              Runs in the project will be able to send data to <code>{api.host}</code>, for this
              purpose: {api.purpose}
            </p>
            {api.actions.length ? (
              <ul>
                {api.actions.map((a) => (
                  <li key={a.action}>
                    {a.label}: {a.statement}
                  </li>
                ))}
              </ul>
            ) : null}
            <p>
              If the plugin later names another host or purpose, it must be approved again. The
              approval is recorded in the audit log.
            </p>
            {choices.length ? (
              <SelectField
                label="Project"
                value={project}
                onChange={setPicked}
                options={choices.map((p) => ({ value: p.id, label: p.name }))}
                error={form.errors["project"]}
              />
            ) : (
              <p className="muted">It’s approved for every project already.</p>
            )}
            <TextField
              label="Note"
              value={note}
              onChange={setNote}
              maxLength={2000}
              optional
              multiline
              help="Why, for the record: such as the agreement with the provider that covers it."
            />
            <div className="actions">
              <Button
                type="submit"
                variant="primary"
                busy={approve.isPending}
                busyText="Approving…"
                disabled={!choices.length}
              >
                Approve
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

function ApprovalRow({ api, approval }: { api: OutsideApi; approval: Approval }) {
  const { say } = useMessages();
  const [asking, setAsking] = useState(false);
  const revoke = useApiMutation(async () => {
    await iki.DELETE("/api/v1/outside-apis/{name}/approvals/{project_id}", {
      params: { path: { name: api.api, project_id: approval.project } },
    });
  }, [OUTSIDE_APIS]);
  return (
    <tr>
      <th scope="row">
        {approval.project_name}
        {approval.current ? null : (
          <>
            {" "}
            <Badge muted>For another host or purpose</Badge>
          </>
        )}
      </th>
      <td>
        {approval.approved_by.name} <RelativeTime value={approval.approved_at} />
        {approval.note ? <div className="muted preserve-lines">{approval.note}</div> : null}
      </td>
      <td>
        <Button
          variant="danger"
          label={`Revoke ${api.label} for ${approval.project_name}`}
          onClick={() => {
            setAsking(true);
          }}
        >
          Revoke
        </Button>
        <ConfirmDialog
          isOpen={asking}
          title={`Revoke ${api.label} for ${approval.project_name}?`}
          confirmLabel="Revoke"
          busy={revoke.isPending}
          problem={revoke.error ? problemOf(revoke.error) : undefined}
          onCancel={() => {
            setAsking(false);
            revoke.reset();
          }}
          onConfirm={() => {
            revoke.mutate(undefined, {
              onSuccess: () => {
                setAsking(false);
                say(`Revoked ${api.label} for ${approval.project_name}.`);
              },
            });
          }}
        >
          <p>
            Runs in {approval.project_name} can no longer send data to {api.label}: launches are
            refused, running ones’ next calls too, and runs waiting on its jobs end, their jobs
            cancelled.
          </p>
        </ConfirmDialog>
      </td>
    </tr>
  );
}

function ApiCard({ api }: { api: OutsideApi }) {
  const [approving, setApproving] = useState(false);
  const id = `api-${api.api.replace(/[^a-z0-9]/gi, "-")}`;
  return (
    <section className="card" aria-labelledby={id}>
      <h2 className="section-title" id={id}>
        {api.label} <Badge muted>{api.api}</Badge>
      </h2>
      <dl className="summary">
        <dt>Host</dt>
        <dd>{api.host ? <code>{api.host}</code> : "Not set: its plugin’s setting is empty."}</dd>
        <dt>Purpose</dt>
        <dd>{api.purpose}</dd>
        <dt>Key</dt>
        <dd>
          {api.credential ? (
            api.credential.set ? (
              <>
                Set, as <code>{api.credential.key}</code> in the run secrets. Iki’s egress proxy
                sends it in <code>{api.credential.header}</code>; runs never see it.
              </>
            ) : (
              <>
                Not set: set <code>{api.credential.key}</code> in the run secrets. Until then, its
                actions can’t run.
              </>
            )
          ) : (
            "It needs none."
          )}
        </dd>
        <dt>Jobs</dt>
        <dd>
          {api.jobs
            ? `Runs wait on its long jobs without holding a machine${api.webhook ? ", and it calls back, signed, when one is done" : ""}.`
            : "Its calls answer at once."}
        </dd>
        <dt>Price</dt>
        <dd>{price(api)}</dd>
        {api.actions.length ? (
          <>
            <dt>Actions</dt>
            <dd>
              <ul>
                {api.actions.map((a) => (
                  <li key={a.action}>
                    {a.label}: {a.statement}
                  </li>
                ))}
              </ul>
            </dd>
          </>
        ) : null}
      </dl>
      {api.approvals.length ? (
        <div className="table-scroll">
          <table className="table">
            <caption>Approved for</caption>
            <thead>
              <tr>
                <th scope="col">Project</th>
                <th scope="col">Approved</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {api.approvals.map((approval) => (
                <ApprovalRow key={approval.project} api={api} approval={approval} />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">Not approved for any project yet: no run can send it data.</p>
      )}
      <div className="actions">
        <Button
          onClick={() => {
            setApproving(true);
          }}
        >
          Approve for a project
        </Button>
      </div>
      {approving ? (
        <ApproveDialog
          api={api}
          onClose={() => {
            setApproving(false);
          }}
        />
      ) : null}
    </section>
  );
}

export function OutsideApisSettings() {
  usePageTitle("Outside APIs");
  const apis = useSuspenseQuery(outsideApisQuery).data;
  return (
    <>
      <PageHeader title="Outside APIs" />
      <SettingsTabs />
      <p>
        Models and services outside Iki that installed plugins’ actions send data to. A run can send
        data to one only once it’s approved for the run’s project, and the person starting it has
        agreed to what it sends. Each call is recorded with the run.
      </p>
      {apis.length ? (
        apis.map((api) => <ApiCard key={api.api} api={api} />)
      ) : (
        <p className="muted">No installed plugin calls an outside API.</p>
      )}
    </>
  );
}
