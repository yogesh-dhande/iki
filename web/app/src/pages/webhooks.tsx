/**
 * Webhooks: a project's (project Admins) and the organization's (Org Admins).
 * The same pages serve both; `scope` says which. Org Admins also see where
 * every project's webhooks send, and can turn any of them off.
 */

import { unwrap } from "@ikisci/sdk";
import { useInfiniteQuery, useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useId, useState, type ReactNode, type SubmitEvent } from "react";

import {
  deliveriesQuery,
  deliveryQuery,
  iki,
  inventoryQuery,
  meQuery,
  problemOf,
  useApiMutation,
  webhookEventsQuery,
  webhookPolicyQuery,
  webhookQuery,
  webhooksQuery,
  WEBHOOKS,
  type DeliverySummary,
  type InventoryWebhook,
  type Project,
  type Webhook,
  type WebhookCreated,
  type WebhookScope,
} from "../api";
import { required, useForm } from "../forms";
import { usePageTitle } from "../layout";
import {
  Badge,
  Button,
  CheckboxesField,
  ConfirmDialog,
  CopyValue,
  Loading,
  PageHeader,
  ProblemAlert,
  RadioField,
  RelativeTime,
  TextField,
  Time,
  useMessages,
} from "../ui";
import { useProject } from "./project-overview";

/** As many as the server allows in one place. */
export const MAX_WEBHOOKS = 20;
/** How often the deliveries refresh while any are still being sent. */
export const PENDING_POLL_MS = 5_000;

/** The host a webhook sends to, as people recognise it. */
export function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function statusOf(webhook: Pick<Webhook, "active" | "disabled_reason">): {
  text: string;
  muted: boolean;
} {
  if (webhook.active) return { text: "On", muted: false };
  if (webhook.disabled_reason === "failing") return { text: "Off: kept failing", muted: true };
  if (webhook.disabled_reason === "org_admin") {
    return { text: "Off: turned off by an Org Admin", muted: true };
  }
  return { text: "Off", muted: true };
}

const DELIVERY_STATUS: Record<DeliverySummary["status"], string> = {
  pending: "Sending",
  succeeded: "Delivered",
  failed: "Failed",
};

function plural(n: number, one: string, many = `${one}s`): string {
  return `${String(n)} ${n === 1 ? one : many}`;
}

/** Links within one scope's webhook pages. */
function useWebhookLinks(scope: WebhookScope, project: Project | undefined) {
  return {
    list: (children: ReactNode) =>
      scope === "project" && project ? (
        <Link to="/projects/$slug/webhooks/" params={{ slug: project.slug }}>
          {children}
        </Link>
      ) : (
        <Link to="/settings/webhooks/">{children}</Link>
      ),
    one: (webhookId: string, children: ReactNode) =>
      scope === "project" && project ? (
        <Link to="/projects/$slug/webhooks/$webhookId/" params={{ slug: project.slug, webhookId }}>
          {children}
        </Link>
      ) : (
        <Link to="/settings/webhooks/$webhookId/" params={{ webhookId }}>
          {children}
        </Link>
      ),
  };
}

// --- Settings tabs ------------------------------------------------------------------

/** The organization settings' tabs, as the server-rendered settings pages show them. */
export function SettingsTabs() {
  return (
    <nav className="tabs" aria-label="Settings">
      <a href="/settings/sign-in/">Sign-in</a>
      <a href="/settings/apps/">Apps</a>
      <a href="/settings/email/">Email</a>
      <Link to="/settings/webhooks/" activeProps={{ "aria-current": "page" }}>
        Webhooks
      </Link>
    </nav>
  );
}

function OrgHeader() {
  return (
    <>
      <PageHeader title="Webhooks" />
      <SettingsTabs />
    </>
  );
}

function OnlyOrgAdmins() {
  return (
    <section className="card">
      <p>Only Org Admins can manage the organization’s webhooks.</p>
    </section>
  );
}

const INTRO =
  "Webhooks tell other systems, such as an ELN or LIMS, when something changes in Iki. " +
  "Iki sends each event as a signed POST request, and keeps trying for a day if the receiver is down.";

// --- Lists --------------------------------------------------------------------------

export function ProjectWebhooks() {
  const project = useProject();
  usePageTitle(`Webhooks · ${project.name}`);
  if (!project.can.manage_webhooks) {
    return (
      <section className="card">
        <p>Only the project’s Admins can manage its webhooks.</p>
      </section>
    );
  }
  return <WebhookList scope="project" project={project} />;
}

export function OrgWebhooks() {
  usePageTitle("Webhooks");
  const me = useSuspenseQuery(meQuery).data;
  return (
    <>
      <OrgHeader />
      {me.can.manage_webhooks ? (
        <>
          <WebhookList scope="org" project={undefined} />
          <ProjectWebhookGovernance />
        </>
      ) : (
        <OnlyOrgAdmins />
      )}
    </>
  );
}

function WebhookList({ scope, project }: { scope: WebhookScope; project: Project | undefined }) {
  const webhooks = useQuery(webhooksQuery(project?.id));
  const policy = useQuery({ ...webhookPolicyQuery, enabled: scope === "project" });
  const [created, setCreated] = useState<WebhookCreated | undefined>();
  const links = useWebhookLinks(scope, project);
  const items = webhooks.data ?? [];
  const blocked = scope === "project" && policy.data?.project_webhooks === false;

  let adding: ReactNode;
  if (created) {
    adding = (
      <Created
        webhook={created}
        link={links.one}
        onDone={() => {
          setCreated(undefined);
        }}
      />
    );
  } else if (project?.archived) {
    adding = <p className="muted">Restore the project to add webhooks.</p>;
  } else if (blocked) {
    adding = (
      <p>
        Your Org Admins have turned off project webhooks: this project’s webhooks don’t send, and
        only Org Admins can add webhooks.
      </p>
    );
  } else if (items.length >= MAX_WEBHOOKS) {
    adding = (
      <p className="muted">
        There are already {MAX_WEBHOOKS} webhooks here. Delete one to add another.
      </p>
    );
  } else {
    adding = <AddWebhook scope={scope} project={project} onCreated={setCreated} />;
  }

  return (
    <>
      <p className="muted">{INTRO}</p>
      {blocked && items.length > 0 ? (
        <div className="alert alert-warning" role="status">
          <p>
            Your Org Admins have turned off project webhooks, so these don’t send. Ask an Org Admin
            if this project needs them.
          </p>
        </div>
      ) : null}
      <div className="columns">
        <section className="card">
          <h2 className="section-title">
            {scope === "project" ? "This project’s webhooks" : "Organization webhooks"}
          </h2>
          {webhooks.isPending ? (
            <Loading label="Loading webhooks…" />
          ) : webhooks.isError ? (
            <ProblemAlert problem={problemOf(webhooks.error)} />
          ) : items.length === 0 ? (
            <p className="muted">
              {scope === "project"
                ? "No webhooks yet. Add one to tell another system about changes to this project."
                : "No webhooks yet. Add one to tell another system about new projects, teams and people."}
            </p>
          ) : (
            <ul className="rows">
              {items.map((webhook) => (
                <WebhookRow key={webhook.id} webhook={webhook} link={links.one} />
              ))}
            </ul>
          )}
        </section>
        <section className="card">
          <h2 className="section-title">Add a webhook</h2>
          {adding}
        </section>
      </div>
    </>
  );
}

function WebhookRow({
  webhook,
  link,
}: {
  webhook: Webhook;
  link: (id: string, children: ReactNode) => ReactNode;
}) {
  const status = statusOf(webhook);
  const last = webhook.last_delivery;
  return (
    <li className="row">
      <div className="row-main">
        <span className="row-title">{link(webhook.id, hostOf(webhook.url))}</span>
        <span className="row-meta">
          {webhook.description ? `${webhook.description} · ` : ""}
          {plural(webhook.events.length, "event")}
          {last ? (
            <>
              {" · "}
              {last.status === "failed" ? "Last delivery failed " : "Last sent "}
              <RelativeTime value={last.created_at} />
            </>
          ) : null}
        </span>
      </div>
      <div className="row-tags">
        <Badge muted={status.muted}>{status.text}</Badge>
      </div>
    </li>
  );
}

// --- Adding -------------------------------------------------------------------------

function useEventChoices(scope: WebhookScope) {
  const events = useQuery(webhookEventsQuery(scope));
  return {
    query: events,
    options: (events.data ?? []).map((e) => ({
      value: e.type,
      label: e.type,
      description: e.description,
    })),
  };
}

function AddWebhook({
  scope,
  project,
  onCreated,
}: {
  scope: WebhookScope;
  project: Project | undefined;
  onCreated: (webhook: WebhookCreated) => void;
}) {
  const { say } = useMessages();
  const [url, setUrl] = useState("");
  const [description, setDescription] = useState("");
  const [chosen, setChosen] = useState<string[]>([]);
  const events = useEventChoices(scope);
  const create = useApiMutation(
    async (body: { url: string; description: string; events: string[] }) =>
      project
        ? unwrap(
            await iki.POST("/api/v1/projects/{project_id}/webhooks", {
              params: { path: { project_id: project.id } },
              body,
            }),
          )
        : unwrap(await iki.POST("/api/v1/webhooks", { body })),
    [WEBHOOKS],
  );
  const form = useForm(create.error);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const ok = form.validate({
      url: required(url, "Enter the address to send events to."),
      events: chosen.length === 0 && "Choose at least one event.",
    });
    if (!ok) return;
    create.mutate(
      { url: url.trim(), description: description.trim(), events: chosen },
      {
        onSuccess: (webhook) => {
          setUrl("");
          setDescription("");
          setChosen([]);
          onCreated(webhook);
          say(`Added a webhook to ${hostOf(webhook.url)}, and sent it a test message.`);
        },
      },
    );
  }

  return (
    <>
      {form.alert}
      <form {...form.props} onSubmit={submit}>
        <TextField
          label="Address"
          value={url}
          onChange={setUrl}
          error={form.errors["url"]}
          help="An https:// address that accepts POST requests with a JSON body."
          maxLength={2000}
        />
        <TextField
          label="Description"
          value={description}
          onChange={setDescription}
          error={form.errors["description"]}
          help="What receives these, so others know what it’s for."
          maxLength={300}
          optional
        />
        {events.query.isPending ? (
          <Loading label="Loading events…" />
        ) : events.query.isError ? (
          <ProblemAlert problem={problemOf(events.query.error)} />
        ) : (
          <CheckboxesField
            label="Events"
            value={chosen}
            onChange={setChosen}
            options={events.options}
            error={form.errors["events"]}
            help="Iki sends only these. Each message names the event and what changed."
          />
        )}
        <Button type="submit" variant="primary" busy={create.isPending} busyText="Adding…">
          Add webhook
        </Button>
      </form>
    </>
  );
}

/** The secret, shown once, just after the webhook is added. */
function Created({
  webhook,
  link,
  onDone,
}: {
  webhook: WebhookCreated;
  link: (id: string, children: ReactNode) => ReactNode;
  onDone: () => void;
}) {
  return (
    <div className="alert alert-success" role="status">
      <p>
        <strong>Copy the signing secret now.</strong> It won’t be shown again.
      </p>
      <SecretHelp />
      <CopyValue value={webhook.secret} label="signing secret" />
      <div className="actions">
        {link(webhook.id, "See the webhook")}
        <Button variant="quiet" onClick={onDone}>
          Add another
        </Button>
      </div>
    </div>
  );
}

function SecretHelp() {
  return (
    <p>
      Your receiver uses it to check that messages came from Iki: each has <code>webhook-id</code>,{" "}
      <code>webhook-timestamp</code> and <code>webhook-signature</code> headers, as the{" "}
      <a href="https://www.standardwebhooks.com/" rel="noreferrer">
        Standard Webhooks
      </a>{" "}
      libraries expect.
    </p>
  );
}

// --- One webhook --------------------------------------------------------------------

export function ProjectWebhook() {
  const project = useProject();
  const { webhookId } = useParams({ from: "/projects/$slug/webhooks/$webhookId" });
  if (!project.can.manage_webhooks) {
    return (
      <section className="card">
        <p>Only the project’s Admins can manage its webhooks.</p>
      </section>
    );
  }
  return <WebhookDetail scope="project" project={project} webhookId={webhookId} />;
}

export function OrgWebhook() {
  const me = useSuspenseQuery(meQuery).data;
  const { webhookId } = useParams({ from: "/settings/webhooks/$webhookId" });
  return (
    <>
      <OrgHeader />
      {me.can.manage_webhooks ? (
        <WebhookDetail scope="org" project={undefined} webhookId={webhookId} />
      ) : (
        <OnlyOrgAdmins />
      )}
    </>
  );
}

function WebhookDetail({
  scope,
  project,
  webhookId,
}: {
  scope: WebhookScope;
  project: Project | undefined;
  webhookId: string;
}) {
  const webhook = useQuery(webhookQuery(webhookId));
  const host = webhook.data ? hostOf(webhook.data.url) : undefined;
  usePageTitle(
    host
      ? `Webhook to ${host}${project ? ` · ${project.name}` : ""}`
      : project
        ? `Webhooks · ${project.name}`
        : "Webhooks",
  );
  const links = useWebhookLinks(scope, project);

  if (webhook.isPending) return <Loading label="Loading the webhook…" />;
  if (webhook.isError) {
    return (
      <>
        <p className="eyebrow">{links.list("All webhooks")}</p>
        <ProblemAlert problem={problemOf(webhook.error)} />
      </>
    );
  }
  const found = webhook.data;
  const status = statusOf(found);
  return (
    <>
      <p className="eyebrow">{links.list("All webhooks")}</p>
      <h2>
        Webhook to {hostOf(found.url)} <Badge muted={status.muted}>{status.text}</Badge>
      </h2>
      <TurnedOff webhook={found} />
      <div className="columns">
        <Details webhook={found} scope={scope} project={project} />
        <div className="stack">
          <OnOff webhook={found} />
          <Secret webhook={found} />
          <Delete webhook={found} scope={scope} project={project} />
        </div>
      </div>
      <Deliveries webhook={found} />
    </>
  );
}

function TurnedOff({ webhook }: { webhook: Webhook }) {
  if (webhook.active) return null;
  if (webhook.disabled_reason === "failing") {
    return (
      <div className="alert alert-warning" role="status">
        <p>
          Every delivery
          {webhook.failing_since ? (
            <>
              {" "}
              since <Time value={webhook.failing_since} />
            </>
          ) : null}{" "}
          failed, so Iki turned this webhook off. Check the failed deliveries below, fix the
          receiver, then turn it back on.
        </p>
      </div>
    );
  }
  if (webhook.disabled_reason === "org_admin") {
    return (
      <div className="alert alert-warning" role="status">
        <p>An Org Admin turned this webhook off. Only an Org Admin can turn it back on.</p>
      </div>
    );
  }
  return null;
}

function usePatchWebhook(webhook: Webhook) {
  return useApiMutation(
    async (patch: { url?: string; description?: string; events?: string[]; active?: boolean }) =>
      unwrap(
        await iki.PATCH("/api/v1/webhooks/{webhook_id}", {
          params: { path: { webhook_id: webhook.id } },
          body: patch,
        }),
      ),
    [WEBHOOKS],
  );
}

function Details({
  webhook,
  scope,
  project,
}: {
  webhook: Webhook;
  scope: WebhookScope;
  project: Project | undefined;
}) {
  const { say } = useMessages();
  const [url, setUrl] = useState(webhook.url);
  const [description, setDescription] = useState(webhook.description);
  const [chosen, setChosen] = useState<string[]>(webhook.events);
  const events = useEventChoices(scope);
  const save = usePatchWebhook(webhook);
  const form = useForm(save.error);
  const readOnly = Boolean(project?.archived);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const ok = form.validate({
      url: required(url, "Enter the address to send events to."),
      events: chosen.length === 0 && "Choose at least one event.",
    });
    if (!ok) return;
    save.mutate(
      { url: url.trim(), description: description.trim(), events: chosen },
      {
        onSuccess: () => {
          say("Saved.");
        },
      },
    );
  }

  return (
    <section className="card">
      <h2 className="section-title">Details</h2>
      {readOnly ? (
        <>
          <p className="muted">Restore the project to change its webhooks.</p>
          <dl className="summary">
            <dt>Address</dt>
            <dd>
              <code>{webhook.url}</code>
            </dd>
            <dt>Events</dt>
            <dd>{webhook.events.join(", ")}</dd>
          </dl>
        </>
      ) : (
        <>
          {form.alert}
          <form {...form.props} onSubmit={submit}>
            <TextField
              label="Address"
              value={url}
              onChange={setUrl}
              error={form.errors["url"]}
              maxLength={2000}
            />
            <TextField
              label="Description"
              value={description}
              onChange={setDescription}
              error={form.errors["description"]}
              maxLength={300}
              optional
            />
            {events.query.isPending ? (
              <Loading label="Loading events…" />
            ) : (
              <CheckboxesField
                label="Events"
                value={chosen}
                onChange={setChosen}
                options={events.options}
                error={form.errors["events"]}
              />
            )}
            <Button type="submit" variant="primary" busy={save.isPending} busyText="Saving…">
              Save
            </Button>
          </form>
        </>
      )}
      <p className="field-help">
        Added
        {webhook.created_by ? ` by ${webhook.created_by.name}` : ""}{" "}
        <Time value={webhook.created_at} />.
      </p>
    </section>
  );
}

function OnOff({ webhook }: { webhook: Webhook }) {
  const me = useSuspenseQuery(meQuery).data;
  const { say } = useMessages();
  const toggle = usePatchWebhook(webhook);
  const ping = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/webhooks/{webhook_id}/ping", {
          params: { path: { webhook_id: webhook.id } },
        }),
      ),
    [[...WEBHOOKS, "deliveries", webhook.id]],
  );
  const lockedByOrg = webhook.disabled_reason === "org_admin" && !me.can.manage_webhooks;
  const problem = toggle.error ?? ping.error;
  return (
    <section className="card">
      <h2 className="section-title">Sending</h2>
      <p>
        {webhook.active
          ? "Iki sends the chosen events to this webhook as they happen."
          : "Iki isn’t sending anything to this webhook. Events that happen while it’s off aren’t sent later."}
      </p>
      <ProblemAlert problem={problem ? problemOf(problem) : undefined} />
      <div className="actions">
        {webhook.active ? (
          <>
            <Button
              busy={ping.isPending}
              busyText="Sending…"
              onClick={() => {
                toggle.reset();
                ping.mutate(undefined, {
                  onSuccess: () => {
                    say("Sent a test message. It’s in the deliveries below.");
                  },
                });
              }}
            >
              Send a test message
            </Button>
            <Button
              variant="quiet"
              busy={toggle.isPending}
              busyText="Turning off…"
              onClick={() => {
                ping.reset();
                toggle.mutate(
                  { active: false },
                  {
                    onSuccess: () => {
                      say("Turned the webhook off.");
                    },
                  },
                );
              }}
            >
              Turn off
            </Button>
          </>
        ) : lockedByOrg ? null : (
          <Button
            variant="primary"
            busy={toggle.isPending}
            busyText="Turning on…"
            onClick={() => {
              toggle.mutate(
                { active: true },
                {
                  onSuccess: () => {
                    say("Turned the webhook on.");
                  },
                },
              );
            }}
          >
            Turn on
          </Button>
        )}
      </div>
    </section>
  );
}

function Secret({ webhook }: { webhook: Webhook }) {
  const [confirming, setConfirming] = useState(false);
  const rotate = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/webhooks/{webhook_id}/rotate-secret", {
          params: { path: { webhook_id: webhook.id } },
        }),
      ),
    [WEBHOOKS],
  );
  const fresh = rotate.data;
  return (
    <section className="card">
      <h2 className="section-title">Signing secret</h2>
      {fresh ? (
        <div className="alert alert-success" role="status">
          <p>
            <strong>Copy the new secret now.</strong> It won’t be shown again. Until{" "}
            <Time value={fresh.previous_secret_expires_at} />, messages are signed with both the new
            and the previous secret, so update your receiver before then.
          </p>
          <CopyValue value={fresh.secret} label="signing secret" />
        </div>
      ) : (
        <>
          <SecretHelp />
          {webhook.rotation_ends_at ? (
            <p>
              Messages are signed with both the new and the previous secret until{" "}
              <RelativeTime value={webhook.rotation_ends_at} />.
            </p>
          ) : null}
        </>
      )}
      <Button
        onClick={() => {
          rotate.reset();
          setConfirming(true);
        }}
      >
        Rotate secret
      </Button>
      <ConfirmDialog
        isOpen={confirming}
        title="Rotate the signing secret?"
        confirmLabel="Rotate secret"
        busy={rotate.isPending}
        problem={rotate.error ? problemOf(rotate.error) : undefined}
        onCancel={() => {
          setConfirming(false);
        }}
        onConfirm={() => {
          rotate.mutate(undefined, {
            onSuccess: () => {
              setConfirming(false);
            },
          });
        }}
      >
        <p>
          Iki makes a new secret and shows it once. For 24 hours, messages carry signatures from
          both secrets, so your receiver keeps working while you update it. The previous secret then
          stops working.
        </p>
      </ConfirmDialog>
    </section>
  );
}

function Delete({
  webhook,
  scope,
  project,
}: {
  webhook: Webhook;
  scope: WebhookScope;
  project: Project | undefined;
}) {
  const navigate = useNavigate();
  const { say } = useMessages();
  const [confirming, setConfirming] = useState(false);
  const remove = useApiMutation(async () => {
    unwrap(
      await iki.DELETE("/api/v1/webhooks/{webhook_id}", {
        params: { path: { webhook_id: webhook.id } },
      }),
    );
  }, [WEBHOOKS]);
  const host = hostOf(webhook.url);
  return (
    <section className="card">
      <h2 className="section-title">Delete</h2>
      <p>Iki stops sending to it and deletes its delivery history. The audit log keeps a record.</p>
      <Button
        variant="danger"
        onClick={() => {
          remove.reset();
          setConfirming(true);
        }}
      >
        Delete webhook
      </Button>
      <ConfirmDialog
        isOpen={confirming}
        title={`Delete the webhook to ${host}?`}
        confirmLabel="Delete webhook"
        busy={remove.isPending}
        problem={remove.error ? problemOf(remove.error) : undefined}
        onCancel={() => {
          setConfirming(false);
        }}
        onConfirm={() => {
          remove.mutate(undefined, {
            onSuccess: () => {
              setConfirming(false);
              const done = () => {
                say(`Deleted the webhook to ${host}.`);
              };
              if (scope === "project" && project) {
                void navigate({
                  to: "/projects/$slug/webhooks/",
                  params: { slug: project.slug },
                }).then(done);
              } else {
                void navigate({ to: "/settings/webhooks/" }).then(done);
              }
            },
          });
        }}
      >
        <p>This can’t be undone. To stop it for now, turn it off instead.</p>
      </ConfirmDialog>
    </section>
  );
}

// --- Deliveries ---------------------------------------------------------------------

function Deliveries({ webhook }: { webhook: Webhook }) {
  const sent = useInfiniteQuery({
    ...deliveriesQuery(webhook.id),
    // While any are still being sent, so people see how they went.
    refetchInterval: (query) =>
      query.state.data?.pages.some((p) => p.items.some((d) => d.status === "pending"))
        ? PENDING_POLL_MS
        : false,
  });
  const items = sent.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <section className="card">
      <h2 className="section-title">Deliveries</h2>
      <p className="muted">
        What Iki sent in the last 30 days, newest first. A failed delivery is tried again 7 times
        over a day; after 5 days of nothing but failures, Iki turns the webhook off.
      </p>
      {sent.isPending ? (
        <Loading label="Loading deliveries…" />
      ) : sent.isError ? (
        <ProblemAlert problem={problemOf(sent.error)} />
      ) : items.length === 0 ? (
        <p className="muted">Nothing sent yet.</p>
      ) : (
        <>
          <ul className="rows">
            {items.map((delivery) => (
              <DeliveryRow key={delivery.id} webhook={webhook} delivery={delivery} />
            ))}
          </ul>
          {sent.hasNextPage ? (
            <p>
              <Button
                busy={sent.isFetchingNextPage}
                busyText="Loading…"
                onClick={() => void sent.fetchNextPage()}
              >
                Show older
              </Button>
            </p>
          ) : null}
        </>
      )}
    </section>
  );
}

function attemptText(attempt: DeliverySummary["attempts"][number]): string {
  if (attempt.status_code !== null) {
    return attempt.error
      ? `${String(attempt.status_code)}: ${attempt.error}`
      : String(attempt.status_code);
  }
  return attempt.error || "No response";
}

function DeliveryRow({ webhook, delivery }: { webhook: Webhook; delivery: DeliverySummary }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const last = delivery.attempts[delivery.attempts.length - 1];
  const failedBadge = delivery.status === "failed";
  return (
    <li className="row delivery">
      <div className="row-main">
        <span className="row-title">
          <code>{delivery.event}</code>
        </span>
        <span className="row-meta">
          <RelativeTime value={delivery.created_at} />
          {" · "}
          {plural(delivery.attempts.length, "attempt")}
          {last ? ` · ${attemptText(last)}` : ""}
          {delivery.redelivered_by ? ` · Sent again by ${delivery.redelivered_by.name}` : ""}
        </span>
        {open ? (
          <div id={id}>
            <DeliveryDetails webhook={webhook} deliveryId={delivery.id} />
          </div>
        ) : null}
      </div>
      <div className="row-tags">
        <Badge muted={failedBadge || delivery.status === "pending"}>
          {DELIVERY_STATUS[delivery.status]}
        </Badge>
        <button
          className="button button-quiet"
          type="button"
          aria-label={`Details of ${delivery.event}`}
          aria-expanded={open}
          aria-controls={open ? id : undefined}
          onClick={() => {
            setOpen(!open);
          }}
        >
          Details
        </button>
      </div>
    </li>
  );
}

function DeliveryDetails({ webhook, deliveryId }: { webhook: Webhook; deliveryId: string }) {
  const { say } = useMessages();
  const delivery = useQuery(deliveryQuery(webhook.id, deliveryId));
  const again = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver", {
          params: { path: { webhook_id: webhook.id, delivery_id: deliveryId } },
        }),
      ),
    [
      [...WEBHOOKS, "deliveries", webhook.id],
      [...WEBHOOKS, "delivery", deliveryId],
    ],
  );
  if (delivery.isPending) return <Loading label="Loading the delivery…" />;
  if (delivery.isError) return <ProblemAlert problem={problemOf(delivery.error)} />;
  const found = delivery.data;
  return (
    <div className="delivery-details">
      <dl className="summary">
        <dt>Message ID</dt>
        <dd>
          <code>{found.message_id}</code>
        </dd>
        {found.attempts.map((attempt, index) => (
          <AttemptRow key={attempt.at} index={index} attempt={attempt} />
        ))}
      </dl>
      <h3 className="section-title">Body</h3>
      {/* Focusable, so keyboard users can scroll a long body (axe: scrollable-region-focusable). */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a scrollable region */}
      <pre className="code" tabIndex={0} role="region" aria-label={`Body of ${found.event}`}>
        {JSON.stringify(found.payload, null, 2)}
      </pre>
      <ProblemAlert problem={again.error ? problemOf(again.error) : undefined} />
      {webhook.active && found.status !== "pending" ? (
        <Button
          busy={again.isPending}
          busyText="Sending…"
          onClick={() => {
            again.mutate(undefined, {
              onSuccess: () => {
                say(`Sent the ${found.event} message again.`);
              },
            });
          }}
        >
          Send again
        </Button>
      ) : null}
    </div>
  );
}

function AttemptRow({
  index,
  attempt,
}: {
  index: number;
  attempt: DeliverySummary["attempts"][number];
}) {
  return (
    <>
      <dt>Attempt {index + 1}</dt>
      <dd>
        <Time value={attempt.at} /> · {attemptText(attempt)} · {String(attempt.duration_ms)} ms
      </dd>
    </>
  );
}

// --- Governing project webhooks -----------------------------------------------------

function ProjectWebhookGovernance() {
  const policy = useQuery(webhookPolicyQuery);
  const inventory = useQuery(inventoryQuery);
  const items = inventory.data ?? [];
  return (
    <section className="card">
      <h2 className="section-title">Project webhooks</h2>
      <p className="muted">
        Where each project’s webhooks send, so you can see where data goes. You can turn any of them
        off; only you and other Org Admins can turn those back on.
      </p>
      {policy.data ? <Policy allowed={policy.data.project_webhooks} /> : null}
      {inventory.isPending ? (
        <Loading label="Loading project webhooks…" />
      ) : inventory.isError ? (
        <ProblemAlert problem={problemOf(inventory.error)} />
      ) : items.length === 0 ? (
        <p className="muted">No project has a webhook.</p>
      ) : (
        <ul className="rows">
          {items.map((webhook) => (
            <InventoryRow key={webhook.id} webhook={webhook} />
          ))}
        </ul>
      )}
    </section>
  );
}

type PolicyChoice = "allowed" | "org_only";

function Policy({ allowed }: { allowed: boolean }) {
  const { say } = useMessages();
  const [choice, setChoice] = useState<PolicyChoice>(allowed ? "allowed" : "org_only");
  const save = useApiMutation(
    async (projectWebhooks: boolean) =>
      unwrap(
        await iki.PATCH("/api/v1/webhook-policy", { body: { project_webhooks: projectWebhooks } }),
      ),
    [WEBHOOKS],
  );
  const changed = (choice === "allowed") !== allowed;
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate(choice === "allowed", {
          onSuccess: () => {
            say("Saved.");
          },
        });
      }}
    >
      <ProblemAlert problem={save.error ? problemOf(save.error) : undefined} />
      <RadioField<PolicyChoice>
        label="Who may add project webhooks"
        value={choice}
        onChange={setChoice}
        options={[
          { value: "allowed", label: "Project Admins, for their projects" },
          {
            value: "org_only",
            label: "No one",
            description: "project webhooks stop sending, and only organization webhooks remain",
          },
        ]}
      />
      {changed ? (
        <Button type="submit" variant="primary" busy={save.isPending} busyText="Saving…">
          Save
        </Button>
      ) : null}
    </form>
  );
}

function InventoryRow({ webhook }: { webhook: InventoryWebhook }) {
  const { say } = useMessages();
  const toggle = useApiMutation(
    async (active: boolean) =>
      unwrap(
        await iki.PATCH("/api/v1/webhooks/{webhook_id}", {
          params: { path: { webhook_id: webhook.id } },
          body: { active },
        }),
      ),
    [[...WEBHOOKS, "inventory"]],
  );
  const status = statusOf(webhook);
  const canTurnOn = !webhook.active && webhook.disabled_reason === "org_admin";
  return (
    <li className="row">
      <div className="row-main">
        <span className="row-title">
          <code>{webhook.host}</code>
        </span>
        <span className="row-meta">
          <Link to="/projects/$slug/" params={{ slug: webhook.project.slug }}>
            {webhook.project.name}
          </Link>
          {" · "}
          {plural(webhook.events.length, "event")}
          {webhook.created_by ? ` · Added by ${webhook.created_by.name}` : ""}
        </span>
        {toggle.error ? (
          <div className="row-problem">
            <ProblemAlert problem={problemOf(toggle.error)} />
          </div>
        ) : null}
      </div>
      <div className="row-actions">
        <Badge muted={status.muted}>{status.text}</Badge>
        {webhook.active ? (
          <Button
            variant="quiet"
            busy={toggle.isPending}
            busyText="Turning off…"
            label={`Turn off the webhook to ${webhook.host} in ${webhook.project.name}`}
            onClick={() => {
              toggle.mutate(false, {
                onSuccess: () => {
                  say(`Turned off the webhook to ${webhook.host} in ${webhook.project.name}.`);
                },
              });
            }}
          >
            Turn off
          </Button>
        ) : canTurnOn ? (
          <Button
            variant="quiet"
            busy={toggle.isPending}
            busyText="Turning on…"
            label={`Turn on the webhook to ${webhook.host} in ${webhook.project.name}`}
            onClick={() => {
              toggle.mutate(true, {
                onSuccess: () => {
                  say(`Turned on the webhook to ${webhook.host} in ${webhook.project.name}.`);
                },
              });
            }}
          >
            Turn on
          </Button>
        ) : null}
      </div>
    </li>
  );
}
