/**
 * Storage (ADR 0180): the freezers and other places at the top, what's checked out, what
 * was received without a location and awaits put-away, and each container's page, with its
 * map of positions to open, select and move from.
 */

import { unwrap } from "@ikisci/sdk";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useMemo, useState } from "react";

import { iki, meQuery, problemOf, useApiMutation } from "../api";
import { BoxMap, MapLegend, slotKey } from "../box-map";
import { personName, typeLabel, typesQuery } from "../entities";
import { LabelDialog } from "../labels";
import { usePageTitle } from "../layout";
import {
  awaitingPutAwayQuery,
  checkedOutQuery,
  containerQuery,
  freeSlots,
  pathText,
  placesQuery,
  resolveScan,
  scanFor,
  scanRefusal,
  STORAGE,
  stepName,
  type Container,
  type Occupant,
  type ScanCandidate,
  type ScanMatch,
  type ScanMode,
  type ScanResolved,
} from "../storage";
import {
  CustodyDialog,
  IkiIdEntry,
  MoveDialog,
  ScanBar,
  ScanChooser,
  ScanRefusedNote,
  StepLink,
  type Chosen,
  type Custody,
} from "../storage-ui";
import { Button, buttonClass, Loading, PageHeader, ProblemAlert, Time } from "../ui";

/** Where a scan found something: its container's page, or its own. */
function useOpenScanned() {
  const navigate = useNavigate();
  return (match: ScanMatch) => {
    if (match.container) {
      void navigate({ to: "/storage/$ref/", params: { ref: match.hrid } });
      return;
    }
    const box = match.location?.path.at(-1);
    if (box && match.position) {
      void navigate({
        to: "/storage/$ref/",
        params: { ref: box.hrid },
        search: { at: match.position },
      });
      return;
    }
    void navigate({ to: "/entities/$ref/", params: { ref: match.hrid } });
  };
}

/** What a scan found, said where it's heard. */
function ScanResults({ code, matches }: { code: string; matches: ScanMatch[] }) {
  if (!matches.length) {
    return (
      <div className="alert alert-warning" role="alert">
        <p>Nothing you can read has the code “{code}”.</p>
      </div>
    );
  }
  return (
    <section className="card" aria-labelledby="scan-results-title">
      <h2 className="section-title" id="scan-results-title">
        {matches.length} records have the code “{code}”
      </h2>
      <ul className="scan-results">
        {matches.map((m) => (
          <li key={m.id}>
            <Link to="/entities/$ref/" params={{ ref: m.hrid }}>
              {m.hrid}
            </Link>{" "}
            {m.title !== m.hrid ? m.title : null}
            <span className="cell-meta">
              {m.location ? pathText(m.location) : "Not in storage"}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function StoragePage() {
  usePageTitle("Storage");
  const places = useQuery(placesQuery);
  const me = useQuery(meQuery).data;
  const out = useQuery({ ...checkedOutQuery(me?.id), enabled: !!me });
  const awaiting = useQuery(awaitingPutAwayQuery);
  const types = useQuery(typesQuery).data ?? [];
  const open = useOpenScanned();
  const [scanned, setScanned] = useState<{ code: string; matches: ScanMatch[] } | null>(null);
  const [checkingIn, setCheckingIn] = useState<Chosen[] | null>(null);
  const find = useApiMutation(scanFor, []);
  return (
    <>
      <PageHeader
        title="Storage"
        description="Freezers, racks and boxes: what’s in each position, and who has what out."
        actions={
          <Link className={buttonClass()} to="/requests/">
            Receive a request’s tubes
          </Link>
        }
      />
      <ScanBar
        label="Find by scanning"
        busy={find.isPending}
        onScan={(code) => {
          find.mutate(code, {
            onSuccess: (matches) => {
              const only = matches.length === 1 ? matches[0] : undefined;
              if (only) {
                open(only);
              } else {
                setScanned({ code, matches });
              }
            },
          });
        }}
      />
      <ProblemAlert problem={find.error ? problemOf(find.error) : undefined} />
      {scanned ? <ScanResults code={scanned.code} matches={scanned.matches} /> : null}
      <section className="card" aria-labelledby="places-title">
        <h2 className="section-title" id="places-title">
          Places
        </h2>
        {places.error ? <ProblemAlert problem={problemOf(places.error)} /> : null}
        {places.isPending ? <Loading label="Loading places…" /> : null}
        {places.data && !places.data.length ? (
          <p className="muted">
            Nothing is kept anywhere yet. Add a freezer, a shelf or a box as a record of its type,
            then put things in it.
          </p>
        ) : null}
        {places.data?.length ? (
          <div className="table-scroll">
            <table className="table">
              <caption className="visually-hidden">Places at the top of storage</caption>
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Type</th>
                  <th scope="col" className="numeric">
                    Holds
                  </th>
                  <th scope="col" className="numeric">
                    Free positions
                  </th>
                </tr>
              </thead>
              <tbody>
                {places.data.map((p) => (
                  <tr key={p.id}>
                    <th scope="row">
                      <Link to="/storage/$ref/" params={{ ref: p.hrid }}>
                        {stepName(p)}
                      </Link>
                      {p.title !== p.hrid ? <span className="cell-meta">{p.hrid}</span> : null}
                    </th>
                    <td>{typeLabel(types, p.type)}</td>
                    <td className="numeric">{p.count}</td>
                    <td className="numeric">
                      {p.shape ? `${String(p.free ?? 0)} of ${String(p.shape.size)}` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
      <section className="card" aria-labelledby="out-title">
        <h2 className="section-title" id="out-title">
          Checked out to you
        </h2>
        {out.isPending ? <Loading label="Loading what you have…" /> : null}
        {out.data && !out.data.length ? <p className="muted">Nothing.</p> : null}
        {out.data?.length ? (
          <>
            <div className="table-scroll">
              <table className="table">
                <caption className="visually-hidden">What’s checked out to you</caption>
                <thead>
                  <tr>
                    <th scope="col">Record</th>
                    <th scope="col">Its position</th>
                    <th scope="col">Since</th>
                  </tr>
                </thead>
                <tbody>
                  {out.data.map((x) => (
                    <tr key={x.id}>
                      <th scope="row">
                        <Link to="/entities/$ref/" params={{ ref: x.hrid }}>
                          {x.hrid}
                        </Link>
                        {x.title !== x.hrid ? <span className="cell-meta">{x.title}</span> : null}
                      </th>
                      <td>{pathText(x.location)}</td>
                      <td>
                        <Time value={x.custody_since} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="actions">
              <Button
                onClick={() => {
                  setCheckingIn(
                    out.data.map((x) => ({
                      id: x.id,
                      hrid: x.hrid,
                      title: x.title,
                      type: x.type,
                      code: null,
                    })),
                  );
                }}
              >
                Check them all in
              </Button>
            </div>
          </>
        ) : null}
      </section>
      <section className="card" aria-labelledby="awaiting-title">
        <h2 className="section-title" id="awaiting-title">
          Received, not yet put away
        </h2>
        {awaiting.error ? <ProblemAlert problem={problemOf(awaiting.error)} /> : null}
        {awaiting.isPending ? <Loading label="Loading what’s waiting…" /> : null}
        {awaiting.data && !awaiting.data.length ? <p className="muted">Nothing.</p> : null}
        {awaiting.data?.length ? (
          <>
            <p className="muted">
              Kept in someone’s custody when it was received. Each leaves this list once it’s put
              away.
            </p>
            <div className="table-scroll">
              <table className="table">
                <caption className="visually-hidden">
                  Received records held without a location
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Record</th>
                    <th scope="col">Held by</th>
                    <th scope="col">Since</th>
                  </tr>
                </thead>
                <tbody>
                  {awaiting.data.map((x) => (
                    <tr key={x.id}>
                      <th scope="row">
                        <Link to="/entities/$ref/" params={{ ref: x.hrid }}>
                          {x.hrid}
                        </Link>
                        {x.title !== x.hrid ? <span className="cell-meta">{x.title}</span> : null}
                      </th>
                      <td>{x.custodian.id === me?.id ? "You" : personName(x.custodian)}</td>
                      <td>
                        <Time value={x.custody_since} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : null}
      </section>
      {checkingIn ? (
        <CustodyDialog
          doing="check-in"
          records={checkingIn}
          projectId={null}
          onClose={() => {
            setCheckingIn(null);
          }}
        />
      ) : null}
    </>
  );
}

// --- A container --------------------------------------------------------------------------------

function chosenOf(o: Occupant): Chosen | null {
  if (!o.id || !o.ref) return null;
  return { id: o.id, hrid: o.ref.hrid, title: o.ref.title, type: o.ref.type, code: o.code };
}

type Dialog =
  | { kind: "move"; records: Chosen[] }
  | { kind: "custody"; doing: Custody; records: Chosen[] }
  | { kind: "labels"; records: { id: string; hrid: string }[] };

export function ContainerPage() {
  const { ref } = useParams({ from: "/storage/$ref" });
  const { at } = useSearch({ from: "/storage/$ref" });
  const found = useQuery(containerQuery(ref));
  const c = found.data;
  usePageTitle(c ? `${stepName(c)} (${c.hrid})` : "Storage");
  if (found.error) {
    return (
      <>
        <PageHeader crumbs={[<Link to="/storage/">Storage</Link>, ref]} title={ref} />
        <ProblemAlert problem={problemOf(found.error)} />
      </>
    );
  }
  if (!c) return <Loading label="Loading…" />;
  return <ContainerView key={c.id} c={c} at={at} />;
}

function ContainerView({ c, at }: { c: Container; at: string | undefined }) {
  const types = useQuery(typesQuery).data ?? [];
  const navigate = useNavigate();
  const client = useQueryClient();
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [showFree, setShowFree] = useState(false);
  const [focusKey, setFocusKey] = useState<string | undefined>();
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [heard, setHeard] = useState("");
  // A scan that needs more: a choice among readable tubes, or another way to identify one.
  const [scanned, setScanned] = useState<
    | { kind: "ambiguous"; code: string; candidates: ScanCandidate[] }
    | { kind: "refused"; mode: ScanMode }
    | null
  >(null);
  const shape = c.shape;
  const byKey = useMemo(() => {
    const m = new Map<string, Occupant>();
    for (const o of c.contents) if (o.row && o.column) m.set(slotKey(o.row, o.column), o);
    return m;
  }, [c.contents]);
  const marked = useMemo(() => {
    if (!at) return undefined;
    const o = c.contents.find((x) => x.position?.toUpperCase() === at.toUpperCase());
    return o?.row && o.column ? { row: o.row, column: o.column } : undefined;
  }, [at, c.contents]);
  const free = shape ? freeSlots(shape, c.contents) : [];
  const chosen = [...selected]
    .map((k) => byKey.get(k))
    .filter((o): o is Occupant => !!o)
    .map(chosenOf)
    .filter((x): x is Chosen => !!x);
  const reconcile = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/storage/containers/{ref}/reconciliations", {
          params: { path: { ref: c.id } },
        }),
      ),
    [STORAGE],
  );
  const toggle = (key: string) => {
    const next = new Set(selected);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    setSelected(next);
  };
  /** The tube a scan identified in this box: focused on its map, and selected if selecting. */
  const show = (found: ScanCandidate, code: string) => {
    const here = c.contents.find((o) => o.id === found.id);
    if (!here?.row || !here.column) {
      setHeard(`${found.hrid} isn’t in a position here.`);
      return;
    }
    const key = slotKey(here.row, here.column);
    // Each scan moves focus there, even to the position it's on.
    setFocusKey(`${key}#${String(Date.now())}`);
    if (selecting) {
      toggle(key);
      setHeard(
        `${here.ref?.hrid ?? code} at ${here.position ?? ""}: ${selected.has(key) ? "unselected" : "selected"}.`,
      );
    } else {
      setHeard(`${here.ref?.hrid ?? code} is at ${here.position ?? ""}.`);
    }
  };
  /**
   * A scan, or a typed Iki ID: identified among what's in this box first. A code several
   * readable tubes here share asks which; one a tube you can't read is part of says only
   * that it can't be used. A code no tube here has is looked for everywhere.
   */
  const onScan = async (code: string, mode: ScanMode = "label", choice?: ScanCandidate) => {
    setScanned(null);
    let found: ScanResolved;
    try {
      found = await resolveScan({
        scope: "box",
        container: c.id,
        code,
        mode,
        ...(choice ? { choice: choice.id } : {}),
      });
    } catch (error) {
      const refusal = scanRefusal(problemOf(error));
      if (refusal?.kind === "ambiguous") {
        setScanned({ kind: "ambiguous", code, candidates: refusal.candidates });
        setHeard("");
      } else if (refusal) {
        setScanned({ kind: "refused", mode });
        setHeard("");
      } else setHeard(problemOf(error).detail);
      return;
    }
    const identified = found.status === "one" ? found.candidates[0] : undefined;
    if (identified) {
      show(identified, code);
      return;
    }
    if (found.status === "ambiguous") {
      setScanned({ kind: "ambiguous", code, candidates: found.candidates });
      setHeard("");
      return;
    }
    if (mode === "iki_id") {
      setHeard(`Nothing in ${c.hrid} has the Iki ID ${code.trim()}.`);
      return;
    }
    const matches = await scanFor(code).catch(() => []);
    const only = matches.length === 1 ? matches[0] : undefined;
    if (!only) {
      setHeard(
        matches.length
          ? `Several records have the code ${code}.`
          : `Nothing you can read has the code ${code}.`,
      );
      return;
    }
    if (only.container) {
      void navigate({ to: "/storage/$ref/", params: { ref: only.hrid } });
      return;
    }
    setHeard(
      `${only.hrid} isn’t in ${c.hrid}: ${only.location ? pathText(only.location) : "it’s not in storage"}.`,
    );
  };
  const location = c.location;
  const typeName = typeLabel(types, c.type);
  const facts = [
    typeName,
    shape
      ? `${String(shape.rows)} × ${String(shape.columns)}, ${String(shape.size)} positions, ${String(c.free ?? free.length)} free`
      : `holds ${String(c.contents.length)}`,
  ].join(" · ");
  return (
    <>
      <PageHeader
        crumbs={[
          <Link to="/storage/">Storage</Link>,
          ...location.path.map((s) => <StepLink step={s} key={s.id} />),
          stepName(c),
        ]}
        title={stepName(c)}
        description={`${c.hrid} · ${facts}${location.path.length ? ` · in ${pathText(location)}` : ""}`}
        actions={
          <>
            <Link className={buttonClass()} to="/entities/$ref/" params={{ ref: c.hrid }}>
              Its record
            </Link>
            {c.can.label ? (
              <Button
                onClick={() => {
                  setDialog({ kind: "labels", records: [{ id: c.id, hrid: c.hrid }] });
                }}
              >
                Print its label
              </Button>
            ) : null}
            {shape && c.can.keep ? (
              c.reconciliation ? (
                <Link
                  className={buttonClass("primary")}
                  to="/storage/reconciliations/$id/"
                  params={{ id: c.reconciliation }}
                >
                  Continue reconciling
                </Link>
              ) : (
                <Button
                  variant="primary"
                  busy={reconcile.isPending}
                  busyText="Starting…"
                  onClick={() => {
                    reconcile.mutate(undefined, {
                      onSuccess: (r) => {
                        void navigate({
                          to: "/storage/reconciliations/$id/",
                          params: { id: r.id },
                        });
                      },
                    });
                  }}
                >
                  Reconcile by scanning
                </Button>
              )
            ) : null}
          </>
        }
      />
      <ProblemAlert problem={reconcile.error ? problemOf(reconcile.error) : undefined} />
      {location.custodian ? (
        <div className="alert alert-warning" role="note">
          <p>
            Checked out by {personName(location.custodian)}
            {location.custody_since ? (
              <>
                {" "}
                since <Time value={location.custody_since} />
              </>
            ) : null}
            .
          </p>
        </div>
      ) : null}
      <ScanBar
        label={selecting ? "Scan to select" : "Scan to find"}
        help={
          selecting
            ? "Each one scanned is selected, or unselected if it was."
            : "A tube in this box is shown on its map; a container is opened."
        }
        onScan={(code) => void onScan(code)}
      />
      {scanned?.kind === "refused" ? <ScanRefusedNote mode={scanned.mode} /> : null}
      {scanned?.kind === "ambiguous" ? (
        <ScanChooser
          code={scanned.code}
          candidates={scanned.candidates}
          onChoose={(choice) => {
            void onScan(scanned.code, "label", choice);
          }}
          onCancel={() => {
            setScanned(null);
          }}
        />
      ) : null}
      <IkiIdEntry open={scanned !== null} onEnter={(hrid) => void onScan(hrid, "iki_id")} />
      <p className="scan-heard" role="status" aria-live="polite">
        {heard}
      </p>
      {shape ? (
        <section className="card box-card" aria-labelledby="map-title">
          <div className="section-head box-toolbar">
            <h2 className="section-title" id="map-title">
              Positions
            </h2>
            <div className="box-toolbar-actions" role="toolbar" aria-label="The map’s tools">
              {c.can.keep ? (
                <Button
                  label={selecting ? "Done selecting" : "Select several"}
                  onClick={() => {
                    setSelecting(!selecting);
                    if (selecting) setSelected(new Set());
                  }}
                >
                  {selecting ? "Done selecting" : "Select several"}
                </Button>
              ) : null}
              <button
                type="button"
                className={buttonClass()}
                aria-pressed={showFree}
                onClick={() => {
                  setShowFree(!showFree);
                }}
              >
                Show free positions
              </button>
            </div>
          </div>
          <p className="muted box-free" role="status">
            {free.length
              ? `${String(free.length)} free: the next is ${free[0]?.label ?? ""}.`
              : "No free positions."}
          </p>
          {selecting ? (
            <div className="selection-bar" role="region" aria-label="Selected">
              <p>
                <strong>{chosen.length}</strong> selected
              </p>
              <div className="actions">
                <Button
                  variant="primary"
                  disabled={!chosen.length}
                  onClick={() => {
                    setDialog({ kind: "move", records: chosen });
                  }}
                >
                  Move…
                </Button>
                <Button
                  disabled={!chosen.length || chosen.some((x) => byKeyOut(byKey, x))}
                  onClick={() => {
                    setDialog({ kind: "custody", doing: "check-out", records: chosen });
                  }}
                >
                  Check out
                </Button>
                <Button
                  disabled={!chosen.length || !chosen.every((x) => byKeyOut(byKey, x))}
                  onClick={() => {
                    setDialog({ kind: "custody", doing: "check-in", records: chosen });
                  }}
                >
                  Check in
                </Button>
                <Button
                  disabled={!chosen.length}
                  onClick={() => {
                    setDialog({ kind: "labels", records: chosen });
                  }}
                >
                  Print labels
                </Button>
                <Button
                  variant="quiet"
                  disabled={!selected.size}
                  onClick={() => {
                    setSelected(new Set());
                  }}
                >
                  Clear
                </Button>
              </div>
            </div>
          ) : null}
          <BoxMap
            label={`${stepName(c)}’s positions`}
            shape={shape}
            contents={c.contents}
            mode={selecting ? "select" : "open"}
            selected={selected}
            marked={marked}
            showFree={showFree}
            focusKey={focusKey}
            onActivate={(slot) => {
              const o = slot.occupant;
              if (selecting) {
                if (o?.ref) toggle(slotKey(slot.row, slot.column));
                return;
              }
              if (!o?.ref) return;
              if (o.container) {
                void navigate({ to: "/storage/$ref/", params: { ref: o.ref.hrid } });
              } else {
                void navigate({ to: "/entities/$ref/", params: { ref: o.ref.hrid } });
              }
            }}
          />
          <MapLegend />
          <p className="field-help">
            {selecting
              ? "Arrow keys move between positions; Space or Enter selects one."
              : "Arrow keys move between positions; Enter opens what’s in one."}
          </p>
        </section>
      ) : null}
      <Contents c={c} />
      {dialog?.kind === "move" ? (
        <MoveDialog
          records={dialog.records}
          from={c.hrid}
          onClose={() => {
            setDialog(null);
          }}
          onMoved={() => {
            setSelected(new Set());
            void client.invalidateQueries({ queryKey: STORAGE });
          }}
        />
      ) : null}
      {dialog?.kind === "custody" ? (
        <CustodyDialog
          doing={dialog.doing}
          records={dialog.records}
          projectId={c.project}
          onClose={() => {
            setDialog(null);
            setSelected(new Set());
          }}
        />
      ) : null}
      {dialog?.kind === "labels" ? (
        <LabelDialog
          records={dialog.records}
          onClose={() => {
            setDialog(null);
          }}
        />
      ) : null}
    </>
  );
}

function byKeyOut(byKey: Map<string, Occupant>, x: Chosen): boolean {
  for (const o of byKey.values()) if (o.id === x.id) return !!o.custodian;
  return false;
}

/** What's in it, as a list: a container with no grid's only view, and a map's text twin. */
function Contents({ c }: { c: Container }) {
  const types = useQuery(typesQuery).data ?? [];
  return (
    <section className="card" aria-labelledby="contents-title">
      <h2 className="section-title" id="contents-title">
        {c.shape ? "What’s in it" : "In it"}
      </h2>
      {!c.contents.length ? <p className="muted">Nothing yet.</p> : null}
      {c.contents.length ? (
        <div className="table-scroll">
          <table className="table">
            <caption className="visually-hidden">What’s in {c.hrid}</caption>
            <thead>
              <tr>
                {c.shape ? <th scope="col">Position</th> : null}
                <th scope="col">Record</th>
                <th scope="col">Type</th>
                <th scope="col">Checked out</th>
              </tr>
            </thead>
            <tbody>
              {c.contents.map((o, i) => (
                <tr key={o.id ?? `hidden-${String(i)}`}>
                  {c.shape ? <td className="storage-code">{o.position}</td> : null}
                  <th scope="row">
                    {o.ref ? (
                      <>
                        <Link
                          to={o.container ? "/storage/$ref/" : "/entities/$ref/"}
                          params={{ ref: o.ref.hrid }}
                        >
                          {o.ref.hrid}
                        </Link>
                        {o.ref.title !== o.ref.hrid ? (
                          <span className="cell-meta">{o.ref.title}</span>
                        ) : null}
                        {o.container ? <span className="cell-meta">holds {o.count}</span> : null}
                      </>
                    ) : (
                      <span className="muted">A record you can’t read</span>
                    )}
                  </th>
                  <td>{o.ref ? typeLabel(types, o.ref.type) : ""}</td>
                  <td>
                    {o.custodian ? (
                      <>
                        {personName(o.custodian)}
                        {o.custody_since ? (
                          <span className="cell-meta">
                            since <Time value={o.custody_since} />
                          </span>
                        ) : null}
                      </>
                    ) : (
                      <span className="muted">No</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}
