/**
 * What the storage pages share (ADR 0180): the scan box, the camera, a record's path, and
 * the dialogs that move records, check them out and in, and hand them over.
 */

import { unwrap } from "@ikisci/sdk";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useId, useRef, useState, type SyntheticEvent } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { fieldErrors, iki, meQuery, problemOf, useApiMutation } from "./api";
import { ENTITIES, typesQuery } from "./entities";
import { PersonPicker } from "./entity-inputs";
import { cameraUnavailable, makeDetector, useScanner } from "./scan";
import { STORAGE, stepName, type Location, type Step } from "./storage";
import { Button, ProblemAlert, RadioField, TextField, useMessages } from "./ui";

// --- Scanning -----------------------------------------------------------------------------------

/**
 * Where a code is scanned or typed. A keyboard-mode scanner's code goes here wherever focus
 * is on the page, unless it's in another text field; a camera reads one where the browser
 * can. `onScan` is given each code.
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
  const [camera, setCamera] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const take = (code: string) => {
    const trimmed = code.trim();
    if (!trimmed) return;
    onScan(trimmed);
  };
  useScanner((code) => {
    take(code);
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
  return (
    <nav aria-label={label} className="where-path">
      <ol>
        {location.hidden ? <li className="muted">A place you can’t open</li> : null}
        {location.path.map((s) => (
          <li key={s.id}>
            <StepLink step={s} />
            {s.position ? <span className="where-position">{s.position}</span> : null}
          </li>
        ))}
        {location.position ? (
          <li aria-current="location">
            <span className="where-position where-here">{location.position}</span>
          </li>
        ) : null}
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

/** Scanning each record's label to say it's the one in hand. */
function Confirmations({
  needed,
  confirmed,
  onConfirm,
}: {
  needed: readonly Chosen[];
  confirmed: Record<string, string>;
  onConfirm: (next: Record<string, string>) => void;
}) {
  const [wrong, setWrong] = useState<string | null>(null);
  if (!needed.length) return null;
  const left = needed.filter((r) => !confirmed[r.id]);
  return (
    <div className="confirmations">
      <ScanBar
        label="Scan each label to confirm"
        help="Its type asks for each record’s own label to be scanned before it moves."
        onScan={(code) => {
          const match = needed.find(
            (r) =>
              r.code?.toLowerCase() === code.toLowerCase() ||
              r.hrid.toLowerCase() === code.toLowerCase(),
          );
          if (match) {
            setWrong(null);
            onConfirm({ ...confirmed, [match.id]: code });
          } else {
            setWrong(`${code} isn’t one of these.`);
          }
        }}
      />
      {wrong ? (
        <p className="field-error" role="alert">
          {wrong}
        </p>
      ) : null}
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

async function previewMoves(body: {
  items: { record: string; to: string | null; position: string | null }[];
  reason: string;
  confirmations: Record<string, string>;
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
  const [confirmed, setConfirmed] = useState<Record<string, string>>({});
  const [planned, setPlanned] = useState<Planned | null>(null);
  const needed = useConfirms(records, "move");
  const { say } = useMessages();
  const items = () =>
    records.map((r, i) => ({
      record: r.id,
      to: where === "out" ? null : to.trim(),
      // The first from the position given, and the rest each in the next free one.
      position: where === "out" || !start.trim() || i > 0 ? null : start.trim(),
    }));
  const preview = useApiMutation(
    () => previewMoves({ items: items(), reason: reason.trim(), confirmations: {}, preview: true }),
    [],
  );
  const move = useApiMutation(
    () =>
      previewMoves({
        items: items(),
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
                maxLength={64}
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
          <Confirmations needed={needed} confirmed={confirmed} onConfirm={setConfirmed} />
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
                  preview.mutate(undefined, {
                    onSuccess: (done) => {
                      setPlanned(done);
                    },
                  });
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
  const [confirmed, setConfirmed] = useState<Record<string, string>>({});
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
          <Confirmations needed={needed} confirmed={confirmed} onConfirm={setConfirmed} />
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
