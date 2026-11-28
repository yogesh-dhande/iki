/**
 * Admin → Quarantine (ADR 0216, 0217): the files the malware scan holds, across the
 * deployment, for Quarantine Admins. Each shows where its bytes are, who added them, and
 * why they're held; a malware finding the admin has checked is a false positive can be
 * released, with their reason, recorded beside the finding and in the audit log. An
 * inspection that couldn't finish can't be: it found nothing to overrule.
 */

import { unwrap } from "@ikisci/sdk";
import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useEffect, useRef, useState, type SubmitEvent } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { iki, meQuery, problemOf, useApiMutation } from "../api";
import { formatSize } from "../entities";
import { FILES } from "../files";
import { required, useForm } from "../forms";
import { usePageTitle } from "../layout";
import {
  COPY_KINDS,
  heldBecause,
  QUARANTINE,
  quarantinedQuery,
  quarantineFilterOf,
  quarantineQuery,
  quarantineSearchOf,
  type HeldCopy,
  type QuarantineFilter,
  type Quarantined,
  type QuarantineState,
  type QuarantineStatus,
} from "../quarantine";
import {
  Button,
  CopyValue,
  Loading,
  PageHeader,
  ProblemAlert,
  RelativeTime,
  TextField,
  useMessages,
} from "../ui";
import { SearchBox, ToolbarSelect } from "./entity-filters";
import { NotFound } from "./errors";

const STATUSES: { value: QuarantineStatus; label: string }[] = [
  { value: "held", label: "Held now" },
  { value: "released", label: "Released" },
];

const STATES: { value: QuarantineState | ""; label: string }[] = [
  { value: "", label: "Any" },
  { value: "infected", label: "Malware found" },
  { value: "not_scanned", label: "Scan incomplete" },
];

/** The held bytes' name: their first file's, or the start of their SHA-256 if no file
 * has them yet. */
function nameOf(item: Quarantined): string {
  return item.files[0]?.name ?? `SHA-256 ${item.sha256.slice(0, 12)}…`;
}

/** Held, in the colour of what's wrong; released, quiet. */
function toneOf(item: Quarantined): string {
  return item.release ? "badge badge-muted" : "badge badge-bad";
}

function Header() {
  return (
    <PageHeader
      title="Quarantine"
      description="Files the malware scan holds, until a Quarantine Admin reviews them."
    />
  );
}

export function QuarantinePage() {
  usePageTitle("Quarantine");
  const me = useSuspenseQuery(meQuery).data;
  if (!me.can.manage_quarantine) return <NotFound />;
  return (
    <>
      <Header />
      <p className="muted">
        Iki scans every file uploaded or registered before anything can use it. A file the scanner
        flags, or can’t inspect completely, is held: its details stay visible, but nobody can
        download it, attach it to a record or give it to a run. Release a malware finding once
        you’ve confirmed it’s a false positive; the finding stays with the file, beside your reason.
      </p>
      <QuarantineList />
    </>
  );
}

function QuarantineList() {
  const search = useSearch({ from: "/admin/quarantine/" });
  const navigate = useNavigate({ from: "/admin/quarantine/" });
  const filter = quarantineFilterOf(search);
  const set = (change: Partial<QuarantineFilter>) => {
    void navigate({ search: quarantineSearchOf({ ...filter, ...change }), replace: true });
  };
  const found = useInfiniteQuery(quarantineQuery(filter));
  const items = found.data?.pages.flatMap((p) => p.items) ?? [];
  const released = filter.status === "released";
  const narrowed = Boolean(filter.q || filter.state);

  let empty: string;
  if (narrowed) {
    empty = released
      ? "No released files match. Try another part of the name, or clear the filters."
      : "No held files match. Try another part of the name, or clear the filters.";
  } else {
    empty = released
      ? "No malware finding has been released."
      : "Nothing is held. Files the scanner flags, or can’t inspect completely, wait here for review.";
  }

  return (
    <div className="quarantine">
      <div className="list-toolbar">
        <SearchBox
          value={filter.q}
          placeholder="Search by file name or SHA-256"
          onSearch={(q) => {
            set({ q });
          }}
        />
        <ToolbarSelect
          label="Status"
          value={filter.status}
          options={STATUSES}
          onChange={(status) => {
            set({ status });
          }}
        />
        <ToolbarSelect
          label="Why"
          value={filter.state ?? ""}
          options={STATES}
          onChange={(state) => {
            set({ state: state || undefined });
          }}
        />
        {narrowed ? (
          <Button
            variant="quiet"
            onClick={() => {
              set({ q: "", state: undefined });
            }}
          >
            Clear filters
          </Button>
        ) : null}
      </div>
      {found.isPending ? (
        <Loading label="Loading quarantined files…" />
      ) : found.isError ? (
        <ProblemAlert problem={problemOf(found.error)} />
      ) : items.length === 0 ? (
        <p className="muted empty">{empty}</p>
      ) : (
        <div className="list-table-box">
          <table className="list-table quarantine-rows">
            <caption className="visually-hidden">
              {released ? "Files released from quarantine" : "Files held in quarantine"}
            </caption>
            <thead>
              <tr>
                <th scope="col" className="list-fill">
                  File
                </th>
                <th scope="col">Project</th>
                <th scope="col">Why</th>
                <th scope="col">Added</th>
                <th scope="col">{released ? "Released" : "Scanned"}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <Row key={item.id} item={item} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {found.hasNextPage ? (
        <div className="actions">
          <Button
            busy={found.isFetchingNextPage}
            busyText="Loading…"
            onClick={() => void found.fetchNextPage()}
          >
            Show more
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function Row({ item }: { item: Quarantined }) {
  const first = item.files[0];
  const more = item.file_count - 1;
  const why = heldBecause(item);
  return (
    <tr>
      <td className="list-fill" title={nameOf(item)}>
        <span className="list-fill-text">
          <Link
            className="list-name file-name"
            to="/admin/quarantine/$decisionId/"
            params={{ decisionId: item.id }}
          >
            {nameOf(item)}
          </Link>
        </span>
        <span className="cell-meta">
          {formatSize(item.size)}
          {more > 0 ? ` · and ${more.toLocaleString()} more ${more === 1 ? "file" : "files"}` : ""}
        </span>
      </td>
      <td className="muted">{first?.project.name ?? "None yet"}</td>
      <td>
        <span className={toneOf(item)}>{why.label}</span>
        {why.finding ? (
          <span className="cell-meta">
            <code>{why.finding}</code>
          </span>
        ) : null}
      </td>
      <td className="muted">
        {first ? (
          <>
            <RelativeTime value={first.created_at} />
            {first.created_by ? (
              <span className="cell-meta">by {first.created_by.name}</span>
            ) : null}
          </>
        ) : null}
      </td>
      <td className="muted">
        {item.release ? (
          <>
            <RelativeTime value={item.release.at} />
            <span className="cell-meta">by {item.release.by.name}</span>
          </>
        ) : (
          <RelativeTime value={item.scanned_at} />
        )}
      </td>
    </tr>
  );
}

// --- One decision ----------------------------------------------------------------------

export function QuarantinedPage() {
  const me = useSuspenseQuery(meQuery).data;
  const { decisionId } = useParams({ from: "/admin/quarantine/$decisionId" });
  if (!me.can.manage_quarantine) return <NotFound />;
  return <QuarantinedDetail id={decisionId} readAudit={me.can.read_audit} />;
}

function BackLink() {
  return (
    <p className="eyebrow">
      <Link to="/admin/quarantine/">All quarantined files</Link>
    </p>
  );
}

function QuarantinedDetail({ id, readAudit }: { id: string; readAudit: boolean }) {
  const found = useQuery(quarantinedQuery(id));
  const item = found.data;
  usePageTitle(item ? `${nameOf(item)} · Quarantine` : "Quarantine");
  if (found.isPending) {
    return (
      <>
        <Header />
        <Loading label="Loading the quarantined file…" />
      </>
    );
  }
  if (found.isError || !item) {
    return (
      <>
        <Header />
        <BackLink />
        <ProblemAlert problem={problemOf(found.error)} />
      </>
    );
  }
  return (
    <>
      <Header />
      <BackLink />
      <h2>
        {nameOf(item)} <span className={toneOf(item)}>{item.release ? "Released" : "Held"}</span>
      </h2>
      <div className="columns">
        <Finding item={item} />
        <div className="stack">
          <Release item={item} />
          <History item={item} readAudit={readAudit} />
        </div>
        <Copies item={item} />
      </div>
    </>
  );
}

// Each section is named directly, not through aria-labelledby: Playwright's Firefox can
// crash working a name out that way while axe checks the page.

function Finding({ item }: { item: Quarantined }) {
  const why = heldBecause(item);
  return (
    <section className="card" aria-label="Finding">
      <h3 className="section-title">Finding</h3>
      <dl className="summary">
        <dt>Why it’s held</dt>
        <dd>
          {why.label}
          <div className="muted">{why.detail}</div>
        </dd>
        {why.finding ? (
          <>
            <dt>{item.state === "infected" ? "Signature" : "Scanner’s reason"}</dt>
            <dd>
              <code>{why.finding}</code>
            </dd>
          </>
        ) : null}
        <dt>Scanner</dt>
        <dd>
          {item.scanner}
          {item.signature_version ? (
            <span className="muted">, signatures {item.signature_version}</span>
          ) : null}
        </dd>
        <dt>Scanned</dt>
        <dd>
          <Exact value={item.scanned_at} />
        </dd>
        <dt>Size</dt>
        <dd>
          {formatSize(item.size)}{" "}
          <span className="muted">({item.size.toLocaleString()} bytes)</span>
        </dd>
        <dt>SHA-256</dt>
        <dd>
          <CopyValue value={item.sha256} label="the SHA-256" />
        </dd>
      </dl>
    </section>
  );
}

/** A moment to the minute, as an audit trail gives it. */
function Exact({ value }: { value: string }) {
  return (
    <time dateTime={value}>
      {new Date(value).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" })}
    </time>
  );
}

function Release({ item }: { item: Quarantined }) {
  const [asking, setAsking] = useState(false);
  const [justReleased, setJustReleased] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const released = item.release !== null;
  useEffect(() => {
    if (!justReleased || !released) return;
    // The button that opened the dialog is gone: the section, which says it's released,
    // has the focus, once the dialog has given it back.
    const frame = requestAnimationFrame(() => heading.current?.focus());
    return () => {
      cancelAnimationFrame(frame);
    };
  }, [justReleased, released]);
  let body;
  if (item.release) {
    body = (
      <>
        <p>
          Released by {item.release.by.name} ({item.release.by.email}),{" "}
          <Exact value={item.release.at} />. Everyone who can read its{" "}
          {item.file_count === 1 ? "project" : "projects"} can use it; the finding stays with it.
        </p>
        <blockquote className="statement preserve-lines">{item.release.reason}</blockquote>
      </>
    );
  } else if (item.releasable) {
    body = (
      <>
        <p>
          If you’ve confirmed the file is safe, such as by checking it against its instrument’s or
          vendor’s original, release it so it can be used. The finding stays with the file, beside
          your release and reason.
        </p>
        <div className="actions">
          <Button
            variant="danger"
            onClick={() => {
              setAsking(true);
            }}
          >
            Release…
          </Button>
        </div>
      </>
    );
  } else {
    body = (
      <p>
        It can’t be released. The scanner didn’t finish inspecting it, so it found nothing that
        could be a false positive to overrule. The decision stays with these bytes: the file can be
        added again in a form the scanner can read in full, such as unencrypted or split into
        smaller parts.
      </p>
    );
  }
  return (
    <section className="card" aria-label="Release">
      <h3 className="section-title" ref={heading} tabIndex={-1}>
        Release
      </h3>
      {body}
      {asking ? (
        <ReleaseDialog
          item={item}
          onClose={(done) => {
            setAsking(false);
            setJustReleased(done);
          }}
        />
      ) : null}
    </section>
  );
}

function ReleaseDialog({
  item,
  onClose,
}: {
  item: Quarantined;
  onClose: (released: boolean) => void;
}) {
  const { say } = useMessages();
  const client = useQueryClient();
  const [reason, setReason] = useState("");
  const name = nameOf(item);
  const release = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/quarantine-decisions/{decision_id}/release", {
          params: { path: { decision_id: item.id } },
          body: { reason: reason.trim() },
        }),
      ),
    [QUARANTINE, FILES],
  );
  const form = useForm(release.error, release.isPending);
  const others = item.file_count - 1;

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form.validate({ reason: required(reason, "Say why it’s a false positive.") })) return;
    release.mutate(undefined, {
      onSuccess: (made) => {
        client.setQueryData(quarantinedQuery(item.id).queryKey, made);
        // Lists that held it load afresh, rather than show it held until they're asked again.
        client.removeQueries({ queryKey: [...QUARANTINE, "list"] });
        say(`Released ${name} from quarantine.`);
        onClose(true);
      },
    });
  }

  return (
    <ModalOverlay
      className="dialog-overlay"
      isOpen
      isDismissable={!release.isPending}
      onOpenChange={(open) => {
        if (!open) onClose(false);
      }}
    >
      <Modal className="dialog">
        <Dialog className="dialog-body">
          <Heading slot="title" className="section-title">
            Release {name}?
          </Heading>
          <form {...form.props} onSubmit={submit}>
            {form.alert}
            <p>
              The scanner found <code>{item.reason}</code>. Once released, everyone who can read{" "}
              {others > 0
                ? `the projects of its ${item.file_count.toLocaleString()} files`
                : "its project"}{" "}
              can download it, attach it to records and give it to runs.
            </p>
            <p>
              The finding stays with the file, beside your name and reason, and the release is
              recorded in the audit log. It can’t be undone.
            </p>
            <TextField
              label="Why is it a false positive?"
              value={reason}
              onChange={setReason}
              maxLength={2000}
              multiline
              error={form.errors["reason"]}
              help="Such as how you checked it: compared with the instrument’s original, or the vendor confirmed the signature misfires."
            />
            <div className="actions">
              <Button type="submit" variant="danger" busy={release.isPending} busyText="Releasing…">
                Release
              </Button>
              <Button
                variant="quiet"
                disabled={release.isPending}
                onClick={() => {
                  onClose(false);
                }}
              >
                Cancel
              </Button>
            </div>
          </form>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}

const AUDIT_ACTION: Record<string, string> = {
  infected: "file.quarantined",
  not_scanned: "file.not_scanned",
};

function History({ item, readAudit }: { item: Quarantined; readAudit: boolean }) {
  const why = heldBecause(item);
  return (
    <section className="card" aria-label="History">
      <h3 className="section-title">History</h3>
      <ol className="quarantine-history">
        <li>
          <strong>{item.state === "infected" ? "Quarantined" : "Held"}</strong>{" "}
          <span className="muted">
            <Exact value={item.scanned_at} />
          </span>
          <div>
            {item.scanner}{" "}
            {item.state === "infected" ? (
              <>
                found <code>{item.reason}</code>
              </>
            ) : (
              <>couldn’t finish: {why.label.toLowerCase()}</>
            )}
            .
          </div>
        </li>
        {item.release ? (
          <li>
            <strong>Released</strong>{" "}
            <span className="muted">
              <Exact value={item.release.at} />
            </span>
            <div>
              By {item.release.by.name}:{" "}
              <span className="preserve-lines">{item.release.reason}</span>
            </div>
          </li>
        ) : null}
      </ol>
      {readAudit ? (
        <p className="muted">
          In the audit log:{" "}
          <a href={`/admin/audit/?action=${AUDIT_ACTION[item.state] ?? ""}`}>
            {item.state === "infected" ? "every quarantine" : "every incomplete scan"}
          </a>
          {item.release ? (
            <>
              {" "}
              and <a href="/admin/audit/?action=file.quarantine_released">every release</a>
            </>
          ) : null}
          .
        </p>
      ) : null}
    </section>
  );
}

function Copies({ item }: { item: Quarantined }) {
  return (
    <section className="card card-wide" aria-label="Where it is">
      <h3 className="section-title">Where it is</h3>
      <p className="muted">
        {item.file_count === 1
          ? "These bytes are in one place."
          : item.file_count === 0
            ? "No file has these bytes yet: they were held as they arrived."
            : `These bytes are in ${item.file_count.toLocaleString()} places, which share this scan: releasing it releases each.`}
      </p>
      {item.files.some((copy) => copy.kind === "file" && !copy.file) ? (
        <p className="muted">
          Being a Quarantine Admin doesn’t make you a member of these projects: a file’s name opens
          its page only in a project you can read.
        </p>
      ) : null}
      {item.files.length ? (
        <div className="table-scroll">
          <table className="table">
            <caption className="visually-hidden">Where its bytes are</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">What</th>
                <th scope="col">Project</th>
                <th scope="col">Added</th>
              </tr>
            </thead>
            <tbody>
              {item.files.map((copy, n) => (
                <CopyRow key={copy.file ?? `${copy.kind}-${String(n)}`} copy={copy} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

function CopyRow({ copy }: { copy: HeldCopy }) {
  return (
    <tr>
      <th scope="row">
        {copy.file ? (
          <Link to="/files/$id/" params={{ id: copy.file }}>
            {copy.name}
          </Link>
        ) : (
          copy.name
        )}
        <span className="cell-meta">{copy.media_type}</span>
      </th>
      <td>
        {COPY_KINDS[copy.kind]}
        {copy.run ? (
          <span className="cell-meta">
            of <code>{copy.run}</code>
          </span>
        ) : null}
      </td>
      <td>{copy.project.name}</td>
      <td>
        <Exact value={copy.created_at} />
        {copy.created_by ? (
          <span className="cell-meta">
            by {copy.created_by.name} ({copy.created_by.email})
            {copy.agent ? <> through {copy.agent.name}</> : null}
          </span>
        ) : null}
      </td>
    </tr>
  );
}
