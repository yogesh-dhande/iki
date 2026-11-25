/** The alignment's stored file, references, saved views and run actions on its selected records. */
import type { Schemas } from "@ikisci/sdk";
import {
  MAX_COLUMNS,
  type AlignmentData,
  type AlignmentOpen,
  type AlignmentPosition,
  type AlignmentViewerElement,
} from "@ikisci/science/alignment-viewer";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { createElement, useEffect, useMemo, useRef, useState } from "react";
import { idsOf, shownRows } from "../alignment";
import { problemOf } from "../api";
import { formatsQuery } from "../media";
import { formatSize, type EntityType } from "../entities";
import {
  declaredAlphabet,
  MAX_READ_BYTES,
  readInWorker,
  type Alphabet,
  type Reading,
} from "../reading";
import { MAX_BATCH } from "../runs";
import { readStoredFile } from "../stored";
import { buttonClass, Loading, ProblemAlert } from "../ui";
import type { Capture, Position } from "../views";
import type { Source } from "./entity-viewer";

type Viewer = Schemas["AlignmentViewerOut"];
type Aligned = Extract<Reading, { kind: "alignment" }>;
const number = new Intl.NumberFormat();

export function AlignmentViewer({
  source,
  type,
  viewer,
  position,
  onCapture,
}: {
  source: Source;
  type: EntityType;
  viewer: Viewer;
  /** Where a saved view's cursor was. */
  position?: Position | undefined;
  onCapture?: ((capture: Capture | null) => void) | undefined;
}) {
  const formats = useQuery(formatsQuery);
  const value = source.values[viewer.alignment];
  const fileId = typeof value === "string" ? value : null;
  const file = fileId ? source.files[fileId] : undefined;
  const field = type.fields.find((f) => f.name === viewer.alignment);
  if (!fileId) {
    return <p className="muted">It has no {field?.label.toLowerCase() ?? "alignment"} yet.</p>;
  }
  if (!file)
    return <p className="muted">Its {field?.label.toLowerCase()} is a file you can’t read.</p>;
  if (formats.error) return <ProblemAlert problem={problemOf(formats.error)} />;
  if (!formats.data) return <Loading label="Loading file formats…" />;
  const format = formats.data.get(file.media_type);
  return (
    <section className="alignment" aria-labelledby="alignment-file">
      <p id="alignment-file" className="structure-file">
        <span>{file.name}</span> <span className="muted">{formatSize(file.size)}</span> ·{" "}
        <a href={`/api/v1/files/${fileId}/content`} download={file.name}>
          Download {file.name}
        </a>
      </p>
      {format && format.viewer !== "alignment" ? (
        <p>The alignment viewer doesn’t read {format.label} files.</p>
      ) : file.size > MAX_READ_BYTES ? (
        <p>
          It’s bigger than the alignment viewer opens ({formatSize(MAX_READ_BYTES)}). Download it to
          open it in a desktop viewer, such as Jalview.
        </p>
      ) : (
        <AlignmentFile
          key={fileId}
          fileId={fileId}
          name={file.name}
          alphabet={declaredAlphabet(format?.id)}
          source={source}
          viewer={viewer}
          position={position}
          onCapture={onCapture}
        />
      )}
    </section>
  );
}

type State =
  { phase: "reading" } | { phase: "read"; reading: Reading } | { phase: "failed"; message: string };

function AlignmentFile({
  fileId,
  name,
  alphabet,
  source,
  viewer,
  position,
  onCapture,
}: {
  fileId: string;
  name: string;
  /** What the file says it holds, which its letters are read as. */
  alphabet: Alphabet | null;
  source: Source;
  viewer: Viewer;
  position?: Position | undefined;
  onCapture?: ((capture: Capture | null) => void) | undefined;
}) {
  const [state, setState] = useState<State>({ phase: "reading" });
  useEffect(() => {
    const abort = new AbortController();
    readStoredFile(fileId, abort.signal)
      .then((bytes) => readInWorker(bytes, alphabet))
      .then(
        (reading) => {
          if (!abort.signal.aborted) setState({ phase: "read", reading });
        },
        (error: unknown) => {
          if (abort.signal.aborted) return;
          setState({
            phase: "failed",
            message: `Iki couldn’t read ${name}. ${error instanceof Error ? error.message : ""}`,
          });
        },
      );
    return () => {
      abort.abort();
    };
  }, [fileId, name, alphabet]);

  if (state.phase === "reading") return <Loading label={`Reading ${name}…`} />;
  if (state.phase === "failed") {
    return (
      <div className="alert alert-error" role="alert">
        <p>{state.message}</p>
      </div>
    );
  }
  const { reading } = state;
  if (reading.kind === "error") {
    return (
      <div className="alert alert-error" role="alert">
        <p>
          {name} isn’t FASTA the viewer can read: {reading.message}
          {reading.line ? ` (line ${number.format(reading.line)})` : ""}.
        </p>
      </div>
    );
  }
  if (reading.kind === "sequences") {
    return (
      <>
        <p>
          {name}’s {number.format(reading.records.length)} sequences aren’t aligned: an alignment’s
          rows, gaps and all, are all one length.
        </p>
        <ul className="alignment-records">
          {reading.records.slice(0, 100).map((record, i) => (
            <li key={i}>
              <span className="mono">{record.id}</span>{" "}
              <span className="muted">{number.format(record.length)} long</span>
            </li>
          ))}
        </ul>
      </>
    );
  }
  if (reading.length > MAX_COLUMNS) {
    return (
      <p>
        It’s {number.format(reading.length)} columns long, more than the alignment viewer draws (
        {number.format(MAX_COLUMNS)}). Download it to open it in a desktop viewer, such as Jalview.
      </p>
    );
  }
  return (
    <AlignmentGrid
      alignment={reading}
      source={source}
      viewer={viewer}
      title={name}
      position={position}
      onCapture={onCapture}
    />
  );
}

function AlignmentGrid({
  alignment,
  source,
  viewer,
  title,
  position,
  onCapture,
}: {
  alignment: Aligned;
  source: Source;
  viewer: Viewer;
  title: string;
  position?: Position | undefined;
  onCapture?: ((capture: Capture | null) => void) | undefined;
}) {
  const referenceId = viewer.reference ? idsOf(source.values[viewer.reference])[0] : undefined;
  const members = viewer.rows ? source.values[viewer.rows] : undefined;
  const listed = useMemo(
    () => [...(referenceId ? [referenceId] : []), ...idsOf(members)],
    [referenceId, members],
  );
  const all = useMemo(
    () => shownRows(alignment.rows, listed, source.refs),
    [alignment.rows, listed, source.refs],
  );
  const referenceHrid = referenceId ? source.refs[referenceId]?.hrid : undefined;
  const reference = referenceHrid
    ? (all.find((r) => r.entity?.hrid === referenceHrid) ?? null)
    : null;
  const data = useMemo<AlignmentData>(
    () => ({
      title,
      alignment,
      labels: all.map(
        ({ entity }) =>
          entity && {
            text: entity.hrid,
            description: entity.title,
            href: `/entities/${encodeURIComponent(entity.hrid)}/`,
            selectionKey: entity.hrid,
          },
      ),
      ...(reference ? { referenceRow: reference.index + 1 } : {}),
      ...(position ? { position } : {}),
    }),
    [alignment, title, all, reference, position],
  );
  const linked = useMemo(() => {
    const ordered = reference ? [reference, ...all.filter((r) => r !== reference)] : all;
    return [...new Set(ordered.flatMap((r) => (r.entity ? [r.entity.hrid] : [])))];
  }, [all, reference]);
  const [selection, setSelection] = useState<{ data: AlignmentData; keys: string[] } | null>(null);
  const picked = selection?.data === data ? selection.keys : [];
  const setPicked = (keys: string[]) => {
    setSelection({ data, keys });
  };
  const element = useRef<AlignmentViewerElement | null>(null);
  const navigate = useNavigate();
  useEffect(() => {
    return () => {
      onCapture?.(null);
    };
  }, [onCapture]);
  return (
    <>
      {referenceHrid && !reference ? (
        <p className="muted">
          Its reference, {referenceHrid}, isn’t one of its rows: no row is headed by its HRID.
        </p>
      ) : null}
      <SelectionBar
        picked={picked}
        linked={linked}
        project={source.owner_project?.slug}
        onPick={(keys) => {
          if (element.current) element.current.selection = keys;
          setPicked(keys);
        }}
      />
      {createElement("iki-alignment-viewer", {
        ref: element,
        data,
        "onselection-change": (event: CustomEvent<string[]>) => {
          setPicked(event.detail);
        },
        "onposition-change": (event: CustomEvent<AlignmentPosition>) => {
          const state = event.detail;
          onCapture?.(() => Promise.resolve({ targets: [{ position: state }] }));
        },
        "onrow-open": (event: CustomEvent<AlignmentOpen>) => {
          const entity = all[event.detail.row - 1]?.entity;
          if (entity) {
            event.preventDefault();
            void navigate({ to: "/entities/$ref/", params: { ref: entity.hrid } });
          }
        },
      })}
    </>
  );
}

/** What a person picked: sequences in Iki the alignment lists, ready to run an action on. */
function SelectionBar({
  picked,
  linked,
  project,
  onPick,
}: {
  picked: string[];
  linked: string[];
  project: string | undefined;
  onPick: (hrids: string[]) => void;
}) {
  if (!linked.length) return null;
  const tooMany = picked.length > MAX_BATCH;
  let status = "";
  if (tooMany) {
    status = `${String(picked.length)} selected: an action runs on at most ${String(MAX_BATCH)} at once`;
  } else if (picked.length) {
    status = `${String(picked.length)} ${picked.length === 1 ? "sequence" : "sequences"} selected`;
  }
  return (
    <div className={picked.length ? "selection-bar" : "alignment-select"}>
      <p role="status" className={picked.length ? undefined : "visually-hidden"}>
        {status}
      </p>
      {picked.length && !tooMany && project ? (
        <Link
          to="/projects/$slug/runs/new/"
          params={{ slug: project }}
          search={{ entities: picked.join(",") }}
          className={buttonClass("primary")}
        >
          Run action on {picked.length === 1 ? "it" : "them"}
        </Link>
      ) : null}
      {picked.length < linked.length ? (
        <button
          type="button"
          className={buttonClass("quiet")}
          onClick={() => {
            onPick(linked);
          }}
        >
          Select every sequence in Iki ({number.format(linked.length)})
        </button>
      ) : null}
      {picked.length ? (
        <button
          type="button"
          className={buttonClass("quiet")}
          onClick={() => {
            onPick([]);
          }}
        >
          Clear selection
        </button>
      ) : null}
    </div>
  );
}
