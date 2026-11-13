/**
 * Compute → Volumes (ADR 0075): each environment's volumes, the version of them machines
 * for its runs get, and the setup builds that make versions. Compute admins ask for a
 * build; one whose setup asks for approval waits for another Compute admin's. Each build
 * runs on a machine of its own, and its log, progress and network use show here.
 */

import { unwrap } from "@ikisci/sdk";
import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef, useState, type SubmitEvent } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { iki, meQuery, problemOf, useApiMutation } from "../api";
import { formatSize } from "../entities";
import { useForm } from "../forms";
import { usePageTitle } from "../layout";
import {
  Button,
  ConfirmDialog,
  Loading,
  PageHeader,
  ProblemAlert,
  RelativeTime,
  TextField,
  useMessages,
} from "../ui";
import {
  FINISHED,
  readSetupLog,
  SETUP_STATE,
  setupsQuery,
  setupTone,
  timeoutText,
  useSetupUpdates,
  VOLUMES,
  volumesQuery,
  type EnvironmentVolumes,
  type SetupBuild,
  type SetupState,
} from "../volumes";
import { NotFound } from "./errors";

const INTRO =
  "Some environments read reference data, such as a sequence database, from a volume their " +
  "setup script fills. A setup build runs the script on a machine of its own and keeps what " +
  "it wrote as a new version: a snapshot, or its own directory on a shared file system. Each " +
  "run reads the newest version that isn’t retired, read-only, and records which it read.";

export function ComputeVolumes() {
  usePageTitle("Volumes");
  const me = useSuspenseQuery(meQuery).data;
  const environments = useQuery({ ...volumesQuery, enabled: me.can.manage_compute });
  if (!me.can.manage_compute) return <NotFound />;
  return (
    <>
      <PageHeader title="Volumes" eyebrow="Compute" />
      <p className="muted">{INTRO}</p>
      {environments.isPending ? (
        <Loading label="Loading environments…" />
      ) : environments.isError ? (
        <ProblemAlert problem={problemOf(environments.error)} />
      ) : environments.data.length === 0 ? (
        <section className="card">
          <p className="muted">No installed plugin has an environment with volumes.</p>
        </section>
      ) : (
        environments.data.map((environment) => (
          <EnvironmentCard key={environment.environment} environment={environment} meId={me.id} />
        ))
      )}
    </>
  );
}

function EnvironmentCard({ environment, meId }: { environment: EnvironmentVolumes; meId: string }) {
  const [asking, setAsking] = useState(false);
  const builds = useQuery(setupsQuery(environment.environment));
  const going = (builds.data ?? []).some((b) => !FINISHED.has(b.state));
  const titleId = `environment-${environment.environment}`;
  return (
    <section className="card" aria-labelledby={titleId}>
      <h2 className="section-title" id={titleId}>
        {environment.label} <code className="muted">{environment.environment}</code>
      </h2>
      {environment.waiting ? (
        <div className="alert alert-warning" role="status">
          <p>Its runs are waiting. {environment.waiting}</p>
        </div>
      ) : null}
      <dl className="summary">
        <dt>Volumes</dt>
        <dd>
          <ul className="value-list">
            {environment.volumes.map((volume) => (
              <li key={volume.name}>
                <code>{volume.name}</code> · {formatSize(volume.size)} at{" "}
                <code>{volume.mount}</code>
                {volume.filled_by_setup ? " · filled by its setup" : ""}
                {volume.access === "read_write_once" ? " · one run at a time may change it" : ""}
              </li>
            ))}
          </ul>
        </dd>
        {environment.setup ? (
          <>
            <dt>Setup</dt>
            <dd>
              Runs <code>{environment.setup.script}</code>
              {environment.setup.timeout
                ? ` for up to ${timeoutText(environment.setup.timeout)}`
                : ""}
              {environment.setup.requires_approval
                ? ". Each build waits for another Compute admin’s approval."
                : "."}
              {environment.setup.network.length ? (
                <>
                  {" It may reach "}
                  {environment.setup.network.map((host, index) => (
                    <span key={host}>
                      {index ? ", " : ""}
                      <code>{host}</code>
                    </span>
                  ))}
                  .
                </>
              ) : (
                " It may reach nothing on the internet."
              )}
            </dd>
          </>
        ) : null}
        <dt>Runs get</dt>
        <dd>
          {environment.places.length === 0 ? (
            <span className="muted">
              None of its machines can have its volumes yet: bind it to a template that gives them
              from a snapshot or a shared file system.
            </span>
          ) : (
            <ul className="value-list">
              {environment.places.map((place) => (
                <li key={place.place}>
                  {environment.places.length > 1 ? (
                    <>
                      In <code>{place.place}</code>:{" "}
                    </>
                  ) : null}
                  {place.hrid && place.version !== null ? (
                    <>
                      version {place.version} ({place.hrid}
                      {place.data_version ? `, ${place.data_version}` : ""})
                      {place.ready_at ? (
                        <>
                          {", ready "}
                          <RelativeTime value={place.ready_at} />
                        </>
                      ) : null}
                    </>
                  ) : (
                    <span className="muted">no version yet</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </dd>
      </dl>
      {environment.setup && environment.places.length > 0 ? (
        <div className="actions">
          <Button
            variant="primary"
            disabled={going}
            onClick={() => {
              setAsking(true);
            }}
          >
            Ask for a setup build
          </Button>
          {going ? <span className="muted">A build is under way.</span> : null}
        </div>
      ) : null}
      {asking ? (
        <AskDialog
          environment={environment}
          onClose={() => {
            setAsking(false);
          }}
        />
      ) : null}
      <h3 className="section-title">Setup builds</h3>
      {builds.isPending ? (
        <Loading label="Loading setup builds…" />
      ) : builds.isError ? (
        <ProblemAlert problem={problemOf(builds.error)} />
      ) : builds.data.length === 0 ? (
        <p className="muted">None yet.</p>
      ) : (
        <ul className="rows">
          {builds.data.map((build) => (
            <BuildRow key={build.id} build={build} meId={meId} environment={environment} />
          ))}
        </ul>
      )}
    </section>
  );
}

// --- Asking for one -----------------------------------------------------------------

function AskDialog({
  environment,
  onClose,
}: {
  environment: EnvironmentVolumes;
  onClose: () => void;
}) {
  const { say } = useMessages();
  const [reason, setReason] = useState("");
  const ask = useApiMutation(
    async (body: { environment: string; reason: string }) =>
      unwrap(await iki.POST("/api/v1/compute/setups", { body })),
    [VOLUMES],
  );
  const form = useForm(ask.error);
  const approval = environment.setup?.requires_approval ?? false;

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    ask.mutate(
      { environment: environment.environment, reason: reason.trim() },
      {
        onSuccess: (made) => {
          const names = made.map((b) => b.hrid).join(", ");
          say(
            approval
              ? `Asked for ${names}. It starts once another Compute admin approves it.`
              : `Asked for ${names}. It starts as soon as a machine is ready.`,
          );
          onClose();
        },
      },
    );
  }

  return (
    <ModalOverlay
      className="dialog-overlay"
      isOpen
      isDismissable={!ask.isPending}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <Modal className="dialog">
        <Dialog className="dialog-body">
          <Heading slot="title" className="section-title">
            Ask for a setup build of {environment.label}
          </Heading>
          <form {...form.props} onSubmit={submit}>
            {form.alert}
            <p>
              It runs <code>{environment.setup?.script}</code> on a machine of its own, then keeps
              what it wrote as a new version. Runs carry on with the current version until it’s
              ready.
              {approval ? " Another Compute admin approves it first." : ""}
            </p>
            <TextField
              label="Why"
              value={reason}
              onChange={setReason}
              error={form.errors["reason"]}
              help="Such as a new release of the database. It’s kept with the build."
              maxLength={2000}
              optional
              multiline
            />
            <div className="actions">
              <Button type="submit" variant="primary" busy={ask.isPending} busyText="Asking…">
                Ask for it
              </Button>
              <Button variant="quiet" onClick={onClose} disabled={ask.isPending}>
                Cancel
              </Button>
            </div>
          </form>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}

// --- A build ------------------------------------------------------------------------

export function SetupStateBadge({ state }: { state: SetupState }) {
  return <span className={`badge badge-${setupTone(state)}`}>{SETUP_STATE[state]}</span>;
}

type Decision = "approve" | "cancel" | "retire";

function BuildRow({
  build,
  meId,
  environment,
}: {
  build: SetupBuild;
  meId: string;
  environment: EnvironmentVolumes;
}) {
  const { say } = useMessages();
  const [open, setOpen] = useState(false);
  const [deciding, setDeciding] = useState<Decision>();
  const finished = FINISHED.has(build.state);
  // While its log is open: live, as its script writes.
  useSetupUpdates(build.id, open && !finished);
  const decide = useApiMutation(
    async (decision: Decision) => {
      const params = { params: { path: { ref: build.id } } };
      switch (decision) {
        case "approve":
          return unwrap(await iki.POST("/api/v1/compute/setups/{ref}/approve", params));
        case "cancel":
          return unwrap(await iki.POST("/api/v1/compute/setups/{ref}/cancel", params));
        case "retire":
          return unwrap(await iki.POST("/api/v1/compute/setups/{ref}/retire", params));
      }
    },
    [VOLUMES],
  );
  const mine = build.requested_by.id === meId;
  const going = build.state === "running" || build.state === "snapshotting";
  const label = `${build.hrid}, version ${String(build.version)}`;

  function confirm() {
    if (!deciding) return;
    decide.mutate(deciding, {
      onSuccess: () => {
        say(
          {
            approve: `Approved ${build.hrid}. It starts as soon as a machine is ready.`,
            cancel: going
              ? `Cancelling ${build.hrid}: its script is being stopped.`
              : `Cancelled ${build.hrid}.`,
            retire: `Retired version ${String(build.version)} (${build.hrid}).`,
          }[deciding],
        );
        setDeciding(undefined);
      },
    });
  }

  return (
    <li className="row row-stacked">
      <div className="row-main">
        <span className="row-title">
          {build.hrid} · version {build.version} <SetupStateBadge state={build.state} />
          {build.current ? <span className="badge badge-good">Runs get this</span> : null}
          {build.retired_at ? <span className="badge badge-muted">Retired</span> : null}
        </span>
        <span className="row-meta">
          Asked for by {build.requested_by.name} <RelativeTime value={build.created_at} />
          {build.approved_by && build.approved_at ? (
            <>
              {" · approved by "}
              {build.approved_by.name} <RelativeTime value={build.approved_at} />
            </>
          ) : null}
          {build.started_at && build.finished_at
            ? ` · took ${duration(build.started_at, build.finished_at)}`
            : ""}
          {build.place !== "" && environment.places.length > 1 ? ` · in ${build.place}` : ""}
        </span>
        {build.reason ? <p className="row-meta">“{build.reason}”</p> : null}
        {build.data_version ? (
          <p className="row-meta">
            Data: <strong>{build.data_version}</strong>
          </p>
        ) : null}
        {build.state === "awaiting_approval" ? (
          <p className="row-meta">
            {mine
              ? "Waiting for another Compute admin to approve it."
              : `${build.requested_by.name} asked for it: it waits for your approval, or another Compute admin’s.`}
          </p>
        ) : null}
        {build.state === "running" && build.progress !== null ? (
          <div className="run-progress">
            <progress value={build.progress} max={1} aria-label={`${build.hrid} progress`} />
            <span>
              {Math.round(build.progress * 100)}%
              {build.progress_message ? ` · ${build.progress_message}` : ""}
            </span>
          </div>
        ) : null}
        {build.state === "queued" && build.progress_message ? (
          <p className="row-meta">{build.progress_message}</p>
        ) : null}
        {build.state === "snapshotting" ? (
          <p className="row-meta">
            {build.volumes.every((volume) => volume.kind === "directory")
              ? "Its script succeeded; it’s ready once its machine has stopped."
              : "Its script succeeded; its volumes are being snapshotted."}
            {build.volumes
              .filter((volume) => volume.state === "creating" && volume.message)
              .map((volume) => ` ${volume.name}: ${volume.message}.`)
              .join("")}
          </p>
        ) : null}
        {build.cancel_requested_at && !finished ? (
          <p className="row-meta">Cancelling: its script is being stopped.</p>
        ) : null}
        {build.error ? <p className="row-reason">{build.error.message}</p> : null}
        {build.retired_at && build.retired_by ? (
          <p className="row-meta">
            Retired by {build.retired_by.name} <RelativeTime value={build.retired_at} />. Machines
            that have it keep it; it’s deleted once none does.
          </p>
        ) : null}
        {open ? <BuildDetails build={build} /> : null}
      </div>
      <div className="row-actions">
        {build.state === "awaiting_approval" && !mine ? (
          <Button
            variant="primary"
            label={`Approve ${label}`}
            onClick={() => {
              setDeciding("approve");
            }}
          >
            Approve
          </Button>
        ) : null}
        {!finished && !build.cancel_requested_at ? (
          <Button
            variant="quiet"
            label={`Cancel ${label}`}
            onClick={() => {
              setDeciding("cancel");
            }}
          >
            Cancel
          </Button>
        ) : null}
        {build.state === "succeeded" && !build.retired_at ? (
          <Button
            variant="quiet"
            label={`Retire ${label}`}
            onClick={() => {
              setDeciding("retire");
            }}
          >
            Retire
          </Button>
        ) : null}
        <Button
          variant="quiet"
          label={`${open ? "Hide" : "Show"} log of ${build.hrid}`}
          onClick={() => {
            setOpen(!open);
          }}
        >
          {open ? "Hide log" : "Show log"}
        </Button>
      </div>
      <ConfirmDialog
        isOpen={deciding !== undefined}
        title={
          deciding === "approve"
            ? `Approve ${build.hrid}?`
            : deciding === "retire"
              ? `Retire version ${String(build.version)}?`
              : `Cancel ${build.hrid}?`
        }
        confirmLabel={
          deciding === "approve"
            ? "Approve"
            : deciding === "retire"
              ? "Retire version"
              : "Cancel build"
        }
        cancelLabel={deciding === "cancel" ? "Keep it" : "Cancel"}
        confirmVariant={deciding === "approve" ? "primary" : "danger"}
        busy={decide.isPending}
        problem={decide.error ? problemOf(decide.error) : undefined}
        onCancel={() => {
          setDeciding(undefined);
          decide.reset();
        }}
        onConfirm={confirm}
      >
        {deciding === "approve" ? (
          <p>
            {build.requested_by.name} asked for it
            {build.reason ? `: “${build.reason}”` : ""}. It starts a machine on{" "}
            <code>{build.template}</code> that runs{" "}
            <code>{environment.setup?.script ?? "its setup script"}</code> for up to{" "}
            {timeoutText(build.timeout)}
            {environment.setup?.network.length
              ? `, reaching ${environment.setup.network.join(", ")}`
              : ""}
            .
          </p>
        ) : deciding === "retire" ? (
          <p>
            New machines get the newest version that isn’t retired
            {build.current ? "; if there’s none, the environment’s runs wait for one" : ""}.
            Machines that have this one keep it, and it’s deleted once none does.
          </p>
        ) : (
          <p>
            {going ? "Its script is stopped, and what it wrote is thrown away." : "It won’t start."}
          </p>
        )}
      </ConfirmDialog>
    </li>
  );
}

function duration(from: string, to: string): string {
  const seconds = Math.max(0, Math.round((Date.parse(to) - Date.parse(from)) / 1000));
  if (seconds < 60) return `${String(seconds)} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${String(minutes)} min`;
  const hours = Math.floor(minutes / 60);
  return minutes % 60 ? `${String(hours)} h ${String(minutes % 60)} min` : `${String(hours)} h`;
}

function BuildDetails({ build }: { build: SetupBuild }) {
  return (
    <>
      {build.volumes.length ? (
        <ul className="value-list" aria-label={`Volumes of ${build.hrid}`}>
          {build.volumes.map((volume) => (
            <li key={volume.name}>
              <code>{volume.name}</code> · {formatSize(volume.size)} ·{" "}
              {volume.kind === "directory"
                ? {
                    creating: "being written",
                    ready: "ready",
                    failed: "failed",
                    deleted: "removed",
                  }[volume.state]
                : {
                    creating: "being snapshotted",
                    ready: "snapshot ready",
                    failed: "snapshot failed",
                    deleted: "snapshot deleted",
                  }[volume.state]}
              {volume.kind === "directory" ? (
                <>
                  {" "}
                  · <code>{volume.handle}</code>
                </>
              ) : null}
              {volume.message ? `: ${volume.message}` : ""}
            </li>
          ))}
        </ul>
      ) : null}
      {build.egress.length ? (
        <ul className="value-list" aria-label={`Network use of ${build.hrid}`}>
          {build.egress.map((reached) => (
            <li key={`${reached.host}:${String(reached.port)}:${reached.refused}`}>
              <code>{reached.host}</code>
              {reached.port === 443 ? "" : `:${String(reached.port)}`}
              {reached.refused
                ? ` · refused: ${reached.refused}`
                : ` · ${String(reached.connections)} connection${reached.connections === 1 ? "" : "s"}, ${formatSize(reached.bytes_received)} received`}
            </li>
          ))}
        </ul>
      ) : null}
      <SetupLog build={build} />
    </>
  );
}

/** Its script's log, read as it grows. */
function SetupLog({ build }: { build: SetupBuild }) {
  const size = build.log_size;
  const [text, setText] = useState("");
  const [next, setNext] = useState(0);
  const [read, setRead] = useState(false);
  const [problem, setProblem] = useState<ReturnType<typeof problemOf>>();
  const box = useRef<HTMLPreElement>(null);
  const atBottom = useRef(true);
  const finished = FINISHED.has(build.state);
  const behind = !read || next < size;

  useEffect(() => {
    if (!behind || problem) return;
    const stale = { now: false };
    void (async () => {
      try {
        let offset = next;
        let added = "";
        for (;;) {
          const page = await readSetupLog(build.id, offset);
          const end = page.offset + new TextEncoder().encode(page.text).length;
          added += page.text;
          if (!page.text || end >= page.size) {
            offset = Math.max(offset, end);
            break;
          }
          offset = end;
        }
        if (stale.now) return;
        setText((t) => t + added);
        setNext(offset);
        setRead(true);
      } catch (error) {
        if (!stale.now) setProblem(problemOf(error));
      }
    })();
    return () => {
      stale.now = true;
    };
  }, [behind, next, problem, build.id]);

  useLayoutEffect(() => {
    const el = box.current;
    if (el && atBottom.current) el.scrollTop = el.scrollHeight;
  }, [text]);

  return (
    <div className="log">
      <div className="log-bar">
        <span className="muted">
          {formatSize(size)}
          {finished ? "" : " so far"}
        </span>
      </div>
      <ProblemAlert problem={problem} />
      {!read ? <Loading label="Loading the log…" /> : null}
      {read && !text ? (
        <p className="muted">{finished ? "It printed nothing." : "Nothing printed yet."}</p>
      ) : null}
      {text ? (
        <pre
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- it scrolls
          tabIndex={0}
          ref={box}
          role="log"
          aria-live="off"
          className="log-text"
          aria-label={`Log of ${build.hrid}`}
          onScroll={(event) => {
            const el = event.currentTarget;
            atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
          }}
        >
          {text}
        </pre>
      ) : null}
    </div>
  );
}
