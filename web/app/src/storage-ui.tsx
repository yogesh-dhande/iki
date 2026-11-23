/**
 * What the storage pages share (ADR 0180): the scan box, the camera, which tube a scan
 * identifies (a choice among readable tubes, the refusal when one can't be read, and a typed
 * Iki ID), a record's path, and the dialogs that move records, check them out and in, and
 * hand them over.
 */

import { unwrap } from "@ikisci/sdk";
import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useId, useRef, useState, type SyntheticEvent } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { fieldErrors, iki, meQuery, problemOf, useApiMutation } from "./api";
import { ENTITIES, typesQuery } from "./entities";
import { PersonPicker } from "./entity-inputs";
import { cameraUnavailable, makeDetector, useScanner, type ScanRejection } from "./scan";
import {
  candidatePlace,
  containerQuery,
  ikiId,
  indexOf,
  resolveScan,
  IKI_ID_REFUSED,
  SCAN_REFUSED,
  scanRefusal,
  slotsOf,
  STORAGE,
  stepName,
  type Location,
  type Proof,
  type ScanCandidate,
  type ScanMode,
  type ScanResolved,
  type Step,
} from "./storage";
import { Button, ProblemAlert, RadioField, TextField, useMessages } from "./ui";

// --- Scanning -----------------------------------------------------------------------------------

/**
 * Where a code is scanned or typed. A keyboard-mode scanner's code goes here wherever focus
 * is on the page, unless it's in another text field; a camera reads one where the browser
 * can. `onScan` is given each code exactly as it was read or typed: it's trimmed only to
 * tell that it's empty, so what's recorded is what the scanner sent.
 */
export function ScanBar({
  label = "Scan or type a code",
  help = "A barcode, an ID such as TUBE-000012, or a label’s link.",
  onScan,
  busy = false,
  disabled = false,
}: {
  label?: string;
  help?: string;
  onScan: (code: string) => void;
  busy?: boolean;
  disabled?: boolean;
}) {
  const id = useId();
  const [text, setText] = useState("");
  const [unread, setUnread] = useState<ScanRejection | null>(null);
  const [camera, setCamera] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const take = (code: string) => {
    setUnread(null);
    if (!code.trim()) return;
    onScan(code);
  };
  useScanner((result) => {
    if (result.kind === "rejected") setUnread(result.reason);
    else take(result.code);
  }, !disabled && !camera);
  const submit = (event: SyntheticEvent) => {
    event.preventDefault();
    take(text);
    setText("");
    input.current?.focus();
  };
  return (
    <form className="scan-bar" onSubmit={submit} role="search" aria-label={label}>
      <div className="field">
        <label htmlFor={`${id}-code`}>{label}</label>
        <div className="scan-bar-row">
          <input
            id={`${id}-code`}
            ref={input}
            value={text}
            onChange={(e) => {
              setText(e.target.value);
            }}
            autoComplete="off"
            spellCheck={false}
            enterKeyHint="search"
            aria-describedby={`${id}-help`}
            disabled={disabled}
            maxLength={200}
          />
          <Button type="submit" busy={busy} busyText="Finding…" disabled={disabled}>
            Find
          </Button>
          <Button
            variant="quiet"
            disabled={disabled}
            onClick={() => {
              setCamera(true);
            }}
          >
            Use camera
          </Button>
        </div>
        <p className="field-help" id={`${id}-help`}>
          {help} A scanner works wherever you are on this page.
        </p>
      </div>
      {unread && (
        <p className="field-error" role="alert">
          {unread === "early-key"
            ? "A key arrived before the rest of the scan."
            : "Couldn’t read the scan."}{" "}
          Scan again or type the code.
        </p>
      )}
      {camera ? (
        <CameraDialog
          onCode={(code) => {
            setCamera(false);
            take(code);
          }}
          onClose={() => {
            setCamera(false);
          }}
        />
      ) : null}
    </form>
  );
}

function isLive(run: { live: boolean }): boolean {
  return run.live;
}

/** Reading a code with the camera, or saying why it can't here. */
export function CameraDialog({
  onCode,
  onClose,
}: {
  onCode: (code: string) => void;
  onClose: () => void;
}) {
  const id = useId();
  const video = useRef<HTMLVideoElement>(null);
  const [why, setWhy] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const found = useRef(onCode);
  useEffect(() => {
    found.current = onCode;
  });
  useEffect(() => {
    // Set false when it closes: what's still coming is dropped.
    const run = { live: true };
    let stream: MediaStream | null = null;
    let timer = 0;
    void (async () => {
      const unavailable = await cameraUnavailable();
      if (unavailable) {
        if (run.live) setWhy(unavailable);
        return;
      }
      const detector = await makeDetector();
      if (!detector) {
        if (run.live) setWhy("This browser’s camera reader doesn’t read Iki’s codes.");
        return;
      }
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: "environment" },
          audio: false,
        });
      } catch {
        if (run.live) {
          setWhy("The camera couldn’t be used: allow it for this site, or use a scanner.");
        }
        return;
      }
      if (!run.live || !video.current) {
        stream.getTracks().forEach((t) => {
          t.stop();
        });
        return;
      }
      video.current.srcObject = stream;
      await video.current.play().catch(() => undefined);
      setReady(true);
      const look = async () => {
        if (!run.live || !video.current) return;
        try {
          const codes = await detector.detect(video.current);
          const code = codes.find((c) => c.rawValue)?.rawValue;
          // It may have closed while the frame was read.
          if (code && isLive(run)) {
            found.current(code);
            return;
          }
        } catch {
          // A frame it couldn't read: the next one may do.
        }
        timer = window.setTimeout(() => void look(), 200);
      };
      void look();
    })();
    return () => {
      run.live = false;
      window.clearTimeout(timer);
      stream?.getTracks().forEach((t) => {
        t.stop();
      });
    };
  }, []);
  return (
    <ModalOverlay
      className="dialog-overlay"
      isOpen
      isDismissable
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <Modal className="dialog camera-dialog">
        <Dialog className="dialog-body" aria-labelledby={`${id}-title`}>
          <Heading slot="title" className="section-title" id={`${id}-title`}>
            Scan with the camera
          </Heading>
          {why ? (
            <div className="alert alert-warning" role="alert">
              <p>{why}</p>
            </div>
          ) : (
            <>
              <p className="muted" role="status">
                {ready ? "Hold the code in the frame." : "Starting the camera…"}
              </p>
              <video ref={video} className="camera-video" muted playsInline aria-hidden="true" />
            </>
          )}
          <div className="actions">
            <Button variant="quiet" onClick={onClose}>
              Close
            </Button>
          </div>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}

// --- Which tube a scan identifies ---------------------------------------------------------------

/**
 * Said when a scan can't identify a tube here: a record the person can't read is involved.
 * Nothing about how many matched, or which: the same words whatever's behind it. For a typed
 * Iki ID, words that also cover one that matched nothing, so neither is told apart.
 */
export function ScanRefusedNote({ mode = "label" }: { mode?: ScanMode | undefined }) {
  return (
    <div className="alert alert-warning scan-refused" role="alert">
      <p>{mode === "iki_id" ? IKI_ID_REFUSED : SCAN_REFUSED}</p>
    </div>
  );
}

/** A candidate's name, project and place, as a chooser shows it. */
function CandidateText({ candidate: c }: { candidate: ScanCandidate }) {
  return (
    <>
      <span className="scan-choice-name">
        <strong className="storage-code">{c.hrid}</strong>
        {c.title && c.title !== c.hrid ? <span> {c.title}</span> : null}
      </span>
      <span className="scan-choice-meta">
        <span>{c.owner_project ? c.owner_project.name : "A project you can’t open"}</span>
        <span>{candidatePlace(c)}</span>
      </span>
    </>
  );
}

/**
 * The readable records a scan matched, each with its project and where it is: the person
 * picks the one in hand. Without `onChoose`, only shown: a physical confirmation needs the
 * tube's own Iki label or its printed Iki ID, which `then` asks for.
 */
export function ScanChooser({
  code,
  candidates,
  noun = "tube",
  onChoose,
  onCancel,
  then,
  busy = false,
}: {
  code: string;
  candidates: readonly ScanCandidate[];
  /** What the candidates are: “tube”, or “container” for where to put them. */
  noun?: string;
  onChoose?: (candidate: ScanCandidate) => void;
  onCancel?: () => void;
  /** What to do instead of choosing, when there's no choice. */
  then?: string;
  busy?: boolean;
}) {
  const id = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    // Said, and where the keyboard goes next: the scanner still works from here.
    heading.current?.focus();
  }, [code, candidates]);
  return (
    <section className="scan-chooser" aria-labelledby={`${id}-title`}>
      <h3 className="scan-chooser-title" id={`${id}-title`} ref={heading} tabIndex={-1}>
        “{code.trim()}” matches more than one {noun}
      </h3>
      <p className="scan-chooser-help">
        {onChoose
          ? `Choose the ${noun} in your hand. Each is shown with its project and where it’s kept.`
          : then}
      </p>
      {onChoose ? (
        <ul className="scan-choices" aria-label={`${noun}s with this code`}>
          {candidates.map((c) => (
            <li key={c.id}>
              <button
                type="button"
                className="scan-choice"
                disabled={busy}
                onClick={() => {
                  onChoose(c);
                }}
              >
                <CandidateText candidate={c} />
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <ul className="scan-choices" aria-label={`${noun}s with this code`}>
          {candidates.map((c) => (
            <li key={c.id} className="scan-choice scan-choice-shown">
              <CandidateText candidate={c} />
            </li>
          ))}
        </ul>
      )}
      {onCancel ? (
        <div className="actions">
          <Button variant="quiet" onClick={onCancel} disabled={busy}>
            {onChoose ? "None of these" : "Dismiss"}
          </Button>
        </div>
      ) : null}
    </section>
  );
}

/**
 * Typing the Iki ID printed on a tube's label, such as TUBE-000012: matched only against
 * Iki IDs, never against vendor barcodes, for a label that can't identify it alone. Shown
 * when asked for, or at once after a scan couldn't identify one.
 */
export function IkiIdEntry({
  onEnter,
  open: opened = false,
  busy = false,
  disabled = false,
}: {
  onEnter: (hrid: string) => void;
  /** Open from the start: a scan was just refused, and this is the way on. */
  open?: boolean;
  busy?: boolean;
  disabled?: boolean;
}) {
  const id = useId();
  const [asked, setAsked] = useState(false);
  const [text, setText] = useState("");
  const [missing, setMissing] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const open = opened || asked;
  const shown = missing ? "Type the Iki ID printed on the label." : undefined;
  if (!open) {
    return (
      <div className="iki-id-ask">
        <Button
          variant="quiet"
          disabled={disabled}
          onClick={() => {
            setAsked(true);
            window.setTimeout(() => input.current?.focus(), 0);
          }}
        >
          Type its printed Iki ID instead
        </Button>
      </div>
    );
  }
  return (
    <form
      className="iki-id-entry"
      noValidate
      aria-label="Enter a printed Iki ID"
      onSubmit={(event) => {
        event.preventDefault();
        if (!text.trim()) {
          setMissing(true);
          input.current?.focus();
          return;
        }
        onEnter(text);
        setText("");
      }}
    >
      <div className={shown ? "field field-invalid" : "field"}>
        <label htmlFor={`${id}-hrid`}>Iki ID</label>
        {shown ? (
          <p className="field-error" id={`${id}-error`} role="alert">
            {shown}
          </p>
        ) : null}
        <div className="scan-bar-row">
          <input
            id={`${id}-hrid`}
            ref={input}
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              if (e.target.value.trim()) setMissing(false);
            }}
            autoComplete="off"
            spellCheck={false}
            maxLength={200}
            disabled={disabled}
            aria-invalid={shown ? "true" : undefined}
            aria-describedby={[shown ? `${id}-error` : "", `${id}-help`].filter(Boolean).join(" ")}
          />
          <Button type="submit" busy={busy} busyText="Checking…" disabled={disabled}>
            Use Iki ID
          </Button>
        </div>
        <p className="field-help" id={`${id}-help`}>
          As printed on its label, such as TUBE-000012. Only Iki IDs are matched, not vendor
          barcodes.
        </p>
      </div>
    </form>
  );
}

// --- Where something is -------------------------------------------------------------------------

/** A step's link: a container's map. */
export function StepLink({ step }: { step: Step }) {
  return (
    <Link to="/storage/$ref/" params={{ ref: step.hrid }}>
      {stepName(step)}
    </Link>
  );
}

/** Where a record is, as a breadcrumb: freezer › rack › box, and its position. */
export function WherePath({ location, label }: { location: Location; label: string }) {
  if (!location.path.length) {
    return (
      <p className="where-none">
        {location.stored ? "In a place you can’t open." : "Not in storage."}
      </p>
    );
  }
  // Each step's position is where the path goes on inside it: the last, the record's own.
  const last = location.path.length - 1;
  return (
    <nav aria-label={label} className="where-path">
      <ol>
        {location.hidden ? <li className="muted">A place you can’t open</li> : null}
        {location.path.map((s, i) => (
          <li key={s.id} aria-current={i === last ? "location" : undefined}>
            <StepLink step={s} />
            {s.position ? (
              <span className={i === last ? "where-position where-here" : "where-position"}>
                {s.position}
              </span>
            ) : null}
          </li>
        ))}
      </ol>
    </nav>
  );
}

// --- Dialogs ------------------------------------------------------------------------------------

/** A record a dialog acts on: its ID, HRID, type, and the code its label carries. */
export interface Chosen {
  id: string;
  hrid: string;
  title: string;
  type: string;
  code: string | null;
}

/** The types of `records` that ask for a scan before they're `what`. */
function useConfirms(records: readonly Chosen[], what: "move" | "draw") {
  const types = useQuery(typesQuery).data ?? [];
  return records.filter((r) =>
    types.find((t) => t.name === r.type)?.storage?.confirm.includes(what),
  );
}

/** What a confirmation said last: who's confirmed, or why the scan wasn't taken. */
type Heard =
  | { kind: "said"; text: string; problem?: boolean }
  | { kind: "refused"; mode: ScanMode }
  | { kind: "ambiguous"; code: string; candidates: ScanCandidate[] };

/** The 32 hex digits an Iki label or a record link names, if it names one. */
function namedId(code: string): string | null {
  const text = code.trim();
  const compact = /^IKI:([0-9A-F]{32})$/i.exec(text);
  if (compact?.[1]) return compact[1].toUpperCase();
  const link = /\/entities\/([0-9a-f-]{32,36})\/?(?:[?#].*)?$/i.exec(text);
  return link?.[1] ? link[1].replaceAll("-", "").toUpperCase() : null;
}

function hexOf(id: string): string {
  return id.replaceAll("-", "").toUpperCase();
}

/** Whether a proof already given is this one again. */
function sameProof(proof: Proof | undefined, code: string, mode: ScanMode): boolean {
  if (proof === undefined) return false;
  const given = typeof proof === "string" ? proof : proof.code;
  const givenMode: ScanMode = typeof proof === "string" ? "label" : proof.mode;
  return givenMode === mode && given.trim().toUpperCase() === code.trim().toUpperCase();
}

/**
 * Which records a scan is most likely the label of, to ask the server about first: those
 * it names, by Iki label, link, Iki ID or the code their label had. The server decides.
 */
function likelyFor(left: readonly Chosen[], code: string, mode: ScanMode): readonly Chosen[] {
  const text = code.trim().toUpperCase();
  const named = mode === "label" ? namedId(code) : null;
  const likely = left.filter((r) =>
    named
      ? hexOf(r.id) === named
      : r.hrid.toUpperCase() === text || (mode === "label" && r.code?.toUpperCase() === text),
  );
  return likely.length ? likely : left;
}

type Answer = { record: Chosen; found: ScanResolved } | { record: Chosen; error: unknown };

/**
 * Scanning each record's label to say it's the one in hand. Each scan is checked by the
 * server against the whole selection and where each one is kept, before it's ticked: a
 * label several tubes share confirms none of them, so the tube's own Iki label or its
 * printed Iki ID is asked for. A record already confirmed is never ticked again.
 */
function Confirmations({
  selected,
  needed,
  confirmed,
  onConfirm,
}: {
  /** Every record in the change, the ones that need no scan too. */
  selected: readonly Chosen[];
  needed: readonly Chosen[];
  confirmed: Record<string, Proof>;
  onConfirm: (next: Record<string, Proof>) => void;
}) {
  const [heard, setHeard] = useState<Heard | null>(null);
  const [checking, setChecking] = useState(false);
  // The newest, for answers that come back after another scan was ticked.
  const latest = useRef(confirmed);
  useEffect(() => {
    latest.current = confirmed;
  }, [confirmed]);
  if (!needed.length) return null;
  const left = needed.filter((r) => !confirmed[r.id]);
  const check = async (code: string, mode: ScanMode) => {
    const now = latest.current;
    const again = needed.find((r) => sameProof(now[r.id], code, mode));
    if (again) {
      setHeard({ kind: "said", text: `${again.hrid} is already confirmed. Nothing changed.` });
      return;
    }
    const open = needed.filter((r) => !now[r.id]);
    if (!open.length) {
      setHeard({ kind: "said", text: "All are confirmed. Nothing changed." });
      return;
    }
    setChecking(true);
    const records = selected.map((r) => r.id);
    const answers: Answer[] = await Promise.all(
      likelyFor(open, code, mode).map((record) =>
        resolveScan({ scope: "confirmation", code, mode, record: record.id, records }).then(
          (found): Answer => ({ record, found }),
          (error: unknown): Answer => ({ record, error }),
        ),
      ),
    );
    setChecking(false);
    // Only the record the server confirmed is ticked, and only if it's still open.
    const hit = answers.find(
      (a) =>
        "found" in a &&
        a.found.status === "one" &&
        a.found.candidates[0]?.id === a.record.id &&
        !latest.current[a.record.id],
    );
    if (hit) {
      const next = { ...latest.current, [hit.record.id]: mode === "iki_id" ? ikiId(code) : code };
      latest.current = next;
      onConfirm(next);
      setHeard({ kind: "said", text: `${hit.record.hrid} confirmed.` });
      return;
    }
    const failed = answers.flatMap((a) => ("error" in a ? [a.error] : []));
    if (failed.some((e) => scanRefusal(problemOf(e))?.kind === "refused")) {
      setHeard({ kind: "refused", mode });
      return;
    }
    const several = answers.find((a) => "found" in a && a.found.status === "ambiguous");
    if (several && "found" in several) {
      setHeard({ kind: "ambiguous", code, candidates: several.found.candidates });
      return;
    }
    const other = failed[0];
    if (other) {
      setHeard({ kind: "said", text: problemOf(other).detail, problem: true });
      return;
    }
    setHeard({
      kind: "said",
      text:
        mode === "iki_id"
          ? `${code.trim()} isn’t the Iki ID of one still to confirm.`
          : `${code.trim()} isn’t one of these.`,
      problem: true,
    });
  };
  const blocked = heard?.kind === "refused" || heard?.kind === "ambiguous";
  return (
    <div className="confirmations">
      <ScanBar
        label="Scan each label to confirm"
        help="Its type asks for each record’s own label to be scanned before it moves."
        busy={checking}
        onScan={(code) => {
          void check(code, "label");
        }}
      />
      {heard?.kind === "refused" ? <ScanRefusedNote mode={heard.mode} /> : null}
      {heard?.kind === "ambiguous" ? (
        <ScanChooser
          code={heard.code}
          candidates={heard.candidates}
          then="A label several tubes share can’t confirm which is in your hand. Scan the tube’s Iki label, or type the Iki ID printed on it."
          onCancel={() => {
            setHeard(null);
          }}
        />
      ) : null}
      <IkiIdEntry
        open={blocked}
        busy={checking}
        onEnter={(hrid) => {
          void check(hrid, "iki_id");
        }}
      />
      {heard?.kind === "said" && heard.problem ? (
        <p className="field-error" role="alert">
          {heard.text}
        </p>
      ) : null}
      <p className="scan-heard" role="status" aria-live="polite">
        {heard?.kind === "said" && !heard.problem ? heard.text : ""}
      </p>
      <ul className="confirm-list" aria-label="Confirmed">
        {needed.map((r) => (
          <li key={r.id} className={confirmed[r.id] ? "confirm-done" : undefined}>
            {r.hrid} {confirmed[r.id] ? "confirmed" : "not scanned yet"}
          </li>
        ))}
      </ul>
      <p className="muted" role="status">
        {left.length ? `${String(left.length)} still to scan.` : "All confirmed."}
      </p>
    </div>
  );
}

type Planned = Awaited<ReturnType<typeof previewMoves>>;

/**
 * Positions in a container from `start` on, one each for `records`, in its grid's order:
 * free ones, or those the records themselves leave. The first is `start` as given, so a
 * position that isn't one is said to be by the server; past the grid's last position the
 * rest are left to the server's next free ones.
 */
async function fromPosition(
  client: QueryClient,
  container: string,
  start: string,
  records: readonly Chosen[],
): Promise<(string | null)[]> {
  if (records.length < 2) return [start];
  const found = await client.query(containerQuery(container)).catch(() => null);
  const shape = found?.shape;
  if (!found || !shape) return [start];
  const leaving = new Set(records.map((r) => r.id));
  const open = slotsOf(
    shape,
    found.contents.filter((o) => !o.id || !leaving.has(o.id)),
  )
    .flat()
    .filter((s) => !s.occupant)
    .sort((a, b) => indexOf(shape, a.row, a.column) - indexOf(shape, b.row, b.column));
  const first = open.findIndex((s) => s.label.toUpperCase() === start.toUpperCase());
  if (first < 0) return [start];
  return records.map((_, i) => open[first + i]?.label ?? null);
}

async function previewMoves(body: {
  items: { record: string; to: string | null; to_choice?: string; position: string | null }[];
  reason: string;
  confirmations: Record<string, Proof>;
  preview: boolean;
}) {
  return unwrap(await iki.POST("/api/v1/storage/moves", { body }));
}

/**
 * Moving records: to one container, each to the next free position from where it starts,
 * or out of storage. What each would do is shown first; then they all move, or none do.
 */
export function MoveDialog({
  records,
  from,
  onClose,
  onMoved,
}: {
  records: readonly Chosen[];
  /** The container they're moved from, which suggests itself as where to. */
  from?: string | undefined;
  onClose: () => void;
  onMoved?: () => void;
}) {
  const id = useId();
  const [where, setWhere] = useState<"container" | "out">("container");
  const [to, setTo] = useState(from ?? "");
  const [start, setStart] = useState("");
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState<Record<string, Proof>>({});
  const [planned, setPlanned] = useState<Planned | null>(null);
  // The container chosen for a code several share, and those it was chosen from.
  const [toChoice, setToChoice] = useState<ScanCandidate | null>(null);
  const [toOptions, setToOptions] = useState<ScanCandidate[] | null>(null);
  const needed = useConfirms(records, "move");
  const { say } = useMessages();
  const client = useQueryClient();
  // Where each goes, as the preview worked it out: the move sends the same.
  const [positions, setPositions] = useState<(string | null)[]>([]);
  const items = (at: readonly (string | null)[], choice: ScanCandidate | null) =>
    records.map((r, i) => ({
      record: r.id,
      to: where === "out" ? null : to.trim(),
      // The code as typed or scanned, and the one chosen for it: the server checks the pair.
      ...(where !== "out" && choice ? { to_choice: choice.id } : {}),
      position: where === "out" ? null : (at[i] ?? null),
    }));
  /** What moving them would do; or, for a code several containers share, those to choose from. */
  const preview = useApiMutation(
    async (choice: ScanCandidate | null): Promise<Planned | { choose: ScanCandidate[] }> => {
      let box: string | null = null;
      if (where === "container" && to.trim()) {
        if (choice) box = choice.id;
        else {
          const found = await resolveScan({ scope: "destination", code: to });
          if (found.status === "ambiguous") return { choose: found.candidates };
          box = found.candidates[0]?.id ?? null;
        }
      }
      const at =
        where === "out" || !start.trim() || !box
          ? []
          : await fromPosition(client, box, start.trim(), records);
      setPositions(at);
      return previewMoves({
        items: items(at, choice),
        reason: reason.trim(),
        confirmations: {},
        preview: true,
      });
    },
    [],
  );
  const check = (choice: ScanCandidate | null) => {
    preview.mutate(choice, {
      onSuccess: (done) => {
        if ("choose" in done) {
          setToOptions(done.choose);
          return;
        }
        setToChoice(choice);
        setToOptions(null);
        setPlanned(done);
      },
    });
  };
  const move = useApiMutation(
    () =>
      previewMoves({
        items: items(positions, toChoice),
        reason: reason.trim(),
        confirmations: confirmed,
        preview: false,
      }),
    [STORAGE, ENTITIES],
  );
  const problem = move.error
    ? problemOf(move.error)
    : preview.error
      ? problemOf(preview.error)
      : undefined;
  const errors = fieldErrors(problem);
  const blocked = planned?.items.some((x) => x.problems.length) ?? false;
  const unconfirmed = needed.some((r) => !confirmed[r.id]);
  const n = records.length;
  const named = n === 1 ? (records[0]?.hrid ?? "") : `${String(n)} records`;
  const changed = () => {
    setPlanned(null);
    setToChoice(null);
    setToOptions(null);
    preview.reset();
    move.reset();
  };
  return (
    <ModalOverlay
      className="dialog-overlay"
      isOpen
      isDismissable={!move.isPending}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <Modal className="dialog storage-dialog">
        <Dialog className="dialog-body" aria-labelledby={`${id}-title`}>
          <Heading slot="title" className="section-title" id={`${id}-title`}>
            Move {named}
          </Heading>
          <MoveProblems problem={problem} records={records} />
          <RadioField
            label="Where to"
            value={where}
            onChange={(v) => {
              setWhere(v);
              changed();
            }}
            options={[
              { value: "container", label: "Into a container" },
              {
                value: "out",
                label: "Out of storage",
                description: "Discarded, used up or sent away.",
              },
            ]}
          />
          {where === "container" ? (
            <div className="move-to">
              <TextField
                label="Container"
                value={to}
                onChange={(v) => {
                  setTo(v);
                  changed();
                }}
                maxLength={200}
                error={errors["items/0/to"]}
                help="Its ID or barcode, such as BOX-000014: scan its label."
              />
              <TextField
                label="Starting at"
                value={start}
                onChange={(v) => {
                  setStart(v);
                  changed();
                }}
                optional
                maxLength={8}
                error={errors["items/0/position"]}
                help={
                  n > 1
                    ? "A position, such as A1: the rest go in the next free ones after it. Leave it empty for the first free one."
                    : "A position, such as A1. Leave it empty for the first free one."
                }
              />
            </div>
          ) : null}
          <TextField
            label="Reason"
            value={reason}
            onChange={(v) => {
              setReason(v);
              move.reset();
            }}
            maxLength={2000}
            multiline
            error={errors["reason"]}
            help="Why they’re moved. It’s kept in each one’s location history."
          />
          {toOptions ? (
            <ScanChooser
              code={to}
              candidates={toOptions}
              noun="container"
              busy={preview.isPending}
              onChoose={(c) => {
                check(c);
              }}
              onCancel={() => {
                setToOptions(null);
              }}
            />
          ) : null}
          <Confirmations
            selected={records}
            needed={needed}
            confirmed={confirmed}
            onConfirm={setConfirmed}
          />
          {planned ? (
            <div className="table-scroll">
              <table className="table move-plan">
                <caption>What moving them does</caption>
                <thead>
                  <tr>
                    <th scope="col">Record</th>
                    <th scope="col">From</th>
                    <th scope="col">To</th>
                    <th scope="col">Problems</th>
                  </tr>
                </thead>
                <tbody>
                  {planned.items.map((x) => (
                    <tr key={x.record.id} className={x.problems.length ? "row-problem" : undefined}>
                      <th scope="row">{x.record.hrid}</th>
                      <td>
                        {x.from_place
                          ? `${x.from_place.ref?.hrid ?? "A place you can’t open"}${
                              x.from_place.position ? `, ${x.from_place.position}` : ""
                            }`
                          : "Nowhere"}
                      </td>
                      <td>
                        {x.to
                          ? `${x.to.hrid}${x.to.position ? `, ${x.to.position}` : ""}`
                          : "Out of storage"}
                      </td>
                      <td>{x.problems.length ? x.problems.join(" ") : "None"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          <div className="actions">
            {planned && !blocked ? (
              <Button
                variant="primary"
                busy={move.isPending}
                busyText="Moving…"
                disabled={unconfirmed}
                onClick={() => {
                  move.mutate(undefined, {
                    onSuccess: () => {
                      say(`Moved ${named}.`);
                      onMoved?.();
                      onClose();
                    },
                  });
                }}
              >
                Move {n === 1 ? "it" : `all ${String(n)}`}
              </Button>
            ) : (
              <Button
                variant="primary"
                busy={preview.isPending}
                busyText="Checking…"
                onClick={() => {
                  check(null);
                }}
              >
                Check where they go
              </Button>
            )}
            <Button variant="quiet" onClick={onClose} disabled={move.isPending}>
              Cancel
            </Button>
          </div>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}

/** What was wrong with a move: each record's problem by its HRID, beside the fields'. */
function MoveProblems({
  problem,
  records,
}: {
  problem: ReturnType<typeof problemOf> | undefined;
  records: readonly Chosen[];
}) {
  if (!problem) return null;
  const items = (problem.errors ?? []).flatMap((e) => {
    const at = /^#\/items\/(\d+)/.exec(e.pointer ?? "");
    if (!at) return [];
    const record = records[Number(at[1])];
    return [`${record?.hrid ?? "One of them"}: ${e.detail}`];
  });
  if (!items.length) return <ProblemAlert problem={problem} />;
  return (
    <div className="alert alert-error" role="alert">
      <p>Nothing was moved:</p>
      <ul>
        {items.map((m) => (
          <li key={m}>{m}</li>
        ))}
      </ul>
    </div>
  );
}

export type Custody = "check-out" | "check-in" | "hand-over";

/**
 * Taking records out to work with (each keeps its position for when it's back), putting
 * them back, or handing one to someone else. Who has what, and since when, is kept.
 */
export function CustodyDialog({
  doing,
  records,
  projectId,
  onClose,
}: {
  doing: Custody;
  records: readonly Chosen[];
  /** Whose people one can be handed to. */
  projectId: string | null;
  onClose: () => void;
}) {
  const id = useId();
  const me = useQuery(meQuery).data;
  const [reason, setReason] = useState("");
  const [person, setPerson] = useState<{ id: string; hrid: string; title: string } | null>(null);
  const [confirmed, setConfirmed] = useState<Record<string, Proof>>({});
  const needed = useConfirms(doing === "check-in" ? records : [], "move");
  const { say } = useMessages();
  const run = useApiMutation(async () => {
    const r = reason.trim();
    switch (doing) {
      case "check-out":
        return unwrap(
          await iki.POST("/api/v1/storage/check-out", {
            body: { records: records.map((x) => x.id), reason: r },
          }),
        );
      case "check-in":
        return unwrap(
          await iki.POST("/api/v1/storage/check-in", {
            body: {
              items: records.map((x) => ({ record: x.id, to: null, position: null })),
              reason: r,
              confirmations: confirmed,
            },
          }),
        );
      case "hand-over":
        return unwrap(
          await iki.POST("/api/v1/storage/hand-over", {
            body: { record: records[0]?.id ?? "", to: person?.id ?? "", reason: r },
          }),
        );
    }
  }, [STORAGE, ENTITIES]);
  const problem = run.error ? problemOf(run.error) : undefined;
  const errors = fieldErrors(problem);
  const n = records.length;
  const named = n === 1 ? (records[0]?.hrid ?? "") : `${String(n)} records`;
  const titles: Record<Custody, string> = {
    "check-out": `Check out ${named}`,
    "check-in": `Check in ${named}`,
    "hand-over": `Hand ${named} over`,
  };
  const done: Record<Custody, string> = {
    "check-out": `Checked out ${named} to you.`,
    "check-in": `Checked in ${named}.`,
    "hand-over": `Handed ${named} to ${person ? person.title || person.hrid : "them"}.`,
  };
  return (
    <ModalOverlay
      className="dialog-overlay"
      isOpen
      isDismissable={!run.isPending}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <Modal className="dialog storage-dialog">
        <Dialog className="dialog-body" aria-labelledby={`${id}-title`}>
          <Heading slot="title" className="section-title" id={`${id}-title`}>
            {titles[doing]}
          </Heading>
          <ProblemAlert
            problem={problem && !errors["reason"] && !errors["to"] ? problem : undefined}
          />
          <p>
            {doing === "check-out"
              ? `To you${me ? `, ${me.name || me.email}` : ""}. Each keeps its position for when it’s back.`
              : doing === "check-in"
                ? "Each goes back to the position it was checked out from."
                : "They have it from now: who had it, and when it changed hands, is kept."}
          </p>
          {doing === "hand-over" && projectId ? (
            <PersonPicker
              label="To"
              projectId={projectId}
              value={person}
              onChange={(p) => {
                setPerson(p);
                run.reset();
              }}
              error={errors["to"]}
              required
            />
          ) : null}
          <TextField
            label="Reason"
            value={reason}
            onChange={setReason}
            maxLength={2000}
            multiline
            optional={doing !== "hand-over"}
            error={errors["reason"]}
            help="It’s kept in each one’s location history."
          />
          <Confirmations
            selected={records}
            needed={needed}
            confirmed={confirmed}
            onConfirm={setConfirmed}
          />
          <div className="actions">
            <Button
              variant="primary"
              busy={run.isPending}
              busyText="Working…"
              disabled={(doing === "hand-over" && !person) || needed.some((r) => !confirmed[r.id])}
              onClick={() => {
                run.mutate(undefined, {
                  onSuccess: () => {
                    say(done[doing]);
                    onClose();
                  },
                });
              }}
            >
              {doing === "check-out"
                ? "Check out"
                : doing === "check-in"
                  ? "Check in"
                  : "Hand over"}
            </Button>
            <Button variant="quiet" onClick={onClose} disabled={run.isPending}>
              Cancel
            </Button>
          </div>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
