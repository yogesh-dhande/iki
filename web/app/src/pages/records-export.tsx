/**
 * Exporting many records as one file (ADR 0229): those picked in a table, or every one its
 * search finds, as a FASTA, GenBank or SDF file that each of their types exports. The
 * server pins each record at the revision it has when asked, writes the file (in the
 * background past a hundred), and keeps it a week; each download is in the audit log.
 * What the format couldn't hold of a record is listed here, and in the file where the
 * format has a place for it.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { queryOptions, useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { iki, problemOf, useApiMutation } from "../api";
import {
  formatSize,
  gridSearch,
  inSentence,
  type EntityFilter,
  type EntityType,
} from "../entities";
import {
  Button,
  buttonClass,
  CheckboxesField,
  ProblemAlert,
  RadioField,
  RelativeTime,
} from "../ui";
import { ToolIcon } from "./entity-filters";

type Format = Schemas["FileFormatOut"];
type Made = Schemas["RecordExportOut"];
type ManyFormat = Schemas["RecordExportIn"]["format"];

/** As many as one file holds; the server says so too. */
export const MAX_RECORDS = 10_000;

const EXPORTS = ["entity-exports"];
/** The records whose notes the dialog lists: the rest are counted. */
const NOTES_SHOWN = 50;

/** The formats every one of these types writes many records into one file of. */
export function manyFormats(names: readonly string[], types: readonly EntityType[]): Format[] {
  const each = [...new Set(names)].map((name) =>
    (types.find((t) => t.name === name)?.exports ?? []).flatMap((e) =>
      e.export === "signed_report" ? [] : e.formats.filter((f) => f.many === "file"),
    ),
  );
  const [first, ...rest] = each;
  return (first ?? []).filter((f) =>
    rest.every((formats) => formats.some((g) => g.format === f.format)),
  );
}

/** An SDF's data: the fields every one of these types can carry, in the first's order. */
export function dataFields(names: readonly string[], types: readonly EntityType[]): string[] {
  const each = [...new Set(names)].map((name) =>
    (types.find((t) => t.name === name)?.exports ?? []).flatMap((e) =>
      e.export === "compound" ? e.data : [],
    ),
  );
  const [first, ...rest] = each;
  return (first ?? []).filter((f) => rest.every((fields) => fields.includes(f)));
}

function exportQuery(id: string) {
  return queryOptions({
    queryKey: [...EXPORTS, id],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/entity-exports/{export_id}", {
          params: { path: { export_id: id } },
          signal,
        }),
      ),
    // Until it's written, look again each second: its count of those done goes up.
    refetchInterval: (query) =>
      query.state.data?.state === "queued" || query.state.data?.state === "writing" ? 1000 : false,
  });
}

function recentQuery() {
  return queryOptions({
    queryKey: [...EXPORTS, "mine"],
    queryFn: async ({ signal }) =>
      unwrap(await iki.GET("/api/v1/entity-exports", { params: { query: { limit: 5 } }, signal }))
        .items,
    refetchInterval: (query) =>
      query.state.data?.some((e) => e.state === "queued" || e.state === "writing") ? 2000 : false,
  });
}

function contentUrl(made: Made): string {
  return `/api/v1/entity-exports/${made.id}/content`;
}

/** Starts the download of a ready export: the server redirects to the file. */
function download(made: Made): void {
  const link = document.createElement("a");
  link.href = contentUrl(made);
  link.download = made.file_name;
  document.body.append(link);
  link.click();
  link.remove();
}

function records(count: number): string {
  return `${count.toLocaleString()} ${count === 1 ? "record" : "records"}`;
}

/**
 * An Export button and its dialog, for records of types that export a file: nothing for
 * those that don't. `picked` exports those records, in that order; without it, everything
 * the table's search finds.
 */
export function ExportRecords({
  filter,
  typeNames,
  types,
  picked,
  label = "Export",
}: {
  filter: EntityFilter;
  /** The types of the records it would export. */
  typeNames: readonly string[];
  types: readonly EntityType[];
  picked?: readonly string[];
  label?: string;
}) {
  const [open, setOpen] = useState(false);
  const formats = manyFormats(typeNames, types);
  if (!formats.length) return null;
  return (
    <>
      <button
        type="button"
        className={buttonClass()}
        onClick={() => {
          setOpen(true);
        }}
      >
        {picked ? null : <ToolIcon name="download" />}
        {label}
      </button>
      {open ? (
        <ExportRecordsDialog
          filter={filter}
          typeNames={typeNames}
          types={types}
          formats={formats}
          {...(picked ? { picked } : {})}
          onClose={() => {
            setOpen(false);
          }}
        />
      ) : null}
    </>
  );
}

function ExportRecordsDialog({
  filter,
  typeNames,
  types,
  formats,
  picked,
  onClose,
}: {
  filter: EntityFilter;
  typeNames: readonly string[];
  types: readonly EntityType[];
  formats: Format[];
  picked?: readonly string[];
  onClose: () => void;
}) {
  const [chosen, setChosen] = useState<string>(formats[0]?.format ?? "");
  const offered = dataFields(typeNames, types);
  const [fields, setFields] = useState<string[]>(offered);
  const [madeId, setMadeId] = useState<string | undefined>(undefined);
  // Written at once, it downloads straight away; in the background, from its link.
  const [downloaded, setDownloaded] = useState(false);
  const start = useApiMutation(
    async (format: ManyFormat): Promise<Made> => {
      const searched = gridSearch(filter);
      const body: Schemas["RecordExportIn"] = picked
        ? { format, entities: [...picked] }
        : { format, search: searched };
      if (format === "sdf") body.fields = fields;
      return unwrap(await iki.POST("/api/v1/entity-exports", { body }));
    },
    [EXPORTS],
  );
  const shown = useQuery({ ...exportQuery(madeId ?? ""), enabled: Boolean(madeId) });
  const made = shown.data ?? start.data;
  const one = typeNames.length === 1 ? types.find((t) => t.name === typeNames[0]) : undefined;
  const what = one ? inSentence(one.label_plural) : "records";
  const title = picked
    ? `Export ${picked.length.toLocaleString()} selected`
    : `Export all ${what} the table shows`;
  const fieldLabel = (name: string) => one?.fields.find((f) => f.name === name)?.label ?? name;
  return (
    <ModalOverlay
      className="dialog-overlay"
      isOpen
      isDismissable={!start.isPending}
      onOpenChange={(isOpen) => {
        if (!isOpen) onClose();
      }}
    >
      <Modal className="dialog">
        <Dialog className="dialog-body">
          <Heading slot="title" className="section-title">
            {title}
          </Heading>
          {made ? (
            <Progress made={made} downloaded={downloaded} />
          ) : (
            <>
              {/* What's picked or searched isn't a field here: the alert says why itself. */}
              <ProblemAlert
                problem={start.error ? { ...problemOf(start.error), errors: [] } : undefined}
              />
              <RadioField
                label="Format"
                value={chosen}
                onChange={setChosen}
                options={formats.map((f) => ({
                  value: f.format,
                  label: f.label,
                  description: f.description,
                }))}
                help={`One file of up to ${MAX_RECORDS.toLocaleString()} records, each as it is now: editing one afterwards doesn’t change the file. Each download is in the audit log.`}
              />
              {chosen === "sdf" && offered.length ? (
                <CheckboxesField
                  label="Data fields"
                  value={fields}
                  onChange={setFields}
                  literal={false}
                  options={offered.map((name) => ({ value: name, label: fieldLabel(name) }))}
                  help="Each record’s HRID, ID and revision are always in it."
                />
              ) : null}
            </>
          )}
          <div className="actions">
            {made ? null : (
              <Button
                variant="primary"
                busy={start.isPending}
                busyText="Pinning records…"
                onClick={() => {
                  start.mutate(chosen as ManyFormat, {
                    onSuccess: (started) => {
                      setMadeId(started.id);
                      if (started.state === "ready") {
                        download(started);
                        setDownloaded(true);
                      }
                    },
                  });
                }}
              >
                Export
              </Button>
            )}
            <Button
              variant={made ? "primary" : "quiet"}
              onClick={onClose}
              disabled={start.isPending}
            >
              {made ? "Done" : "Cancel"}
            </Button>
          </div>
          <Recent shown={made?.id} />
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}

/** How far it's got, then its file, what it couldn't hold, or why it failed. */
function Progress({ made, downloaded }: { made: Made; downloaded: boolean }) {
  const id = useId();
  const noted = Object.entries(made.notes);
  const refused = Object.entries(made.refused);
  if (made.state === "queued" || made.state === "writing") {
    return (
      <div className="export-progress">
        <label htmlFor={id}>
          {made.state === "queued"
            ? `Waiting to write ${records(made.count)}…`
            : `Writing ${made.done.toLocaleString()} of ${records(made.count)}…`}
        </label>
        <progress id={id} max={made.count} value={made.done} />
        <p className="muted">
          You can close this: it’s written in the background, and listed under Recent exports.
        </p>
      </div>
    );
  }
  if (made.state === "failed") {
    return (
      <div role="alert" className="export-result">
        <p>{made.error}</p>
        {refused.length ? (
          <ul>
            {refused.map(([hrid, why]) => (
              <li key={hrid}>
                <code>{hrid}</code>: {why}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    );
  }
  if (made.state === "expired") {
    return <p className="export-result">Its file was deleted a week after it was written.</p>;
  }
  return (
    <div className="export-result">
      <p role="status">
        {downloaded ? "Downloading" : "Ready to download:"}{" "}
        <a href={contentUrl(made)} download={made.file_name}>
          {made.file_name}
        </a>
        : {records(made.count)}
        {made.size === null ? "" : `, ${formatSize(made.size)}`}.
      </p>
      {noted.length ? (
        <>
          <p>
            It couldn’t hold everything of {records(noted.length)}
            {made.format === "genbank"
              ? ", as each one’s COMMENT says"
              : made.format === "sdf"
                ? ", as each one’s IKI_NOTES says"
                : ""}
            :
          </p>
          <ul className="export-notes">
            {noted.slice(0, NOTES_SHOWN).map(([hrid, notes]) => (
              <li key={hrid}>
                <code>{hrid}</code>: {notes.join(" ")}
              </li>
            ))}
          </ul>
          {noted.length > NOTES_SHOWN ? (
            <p className="muted">
              And {records(noted.length - NOTES_SHOWN)} more, which the audit log lists.
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

/** The requester's last few exports, to download again until each expires. */
function Recent({ shown }: { shown: string | undefined }) {
  const id = useId();
  const recent = useQuery(recentQuery());
  // The one just made is shown above it.
  const others = recent.data?.filter((made) => made.id !== shown) ?? [];
  if (!others.length) return null;
  return (
    <section className="export-recent" aria-labelledby={id}>
      <h3 id={id} className="field-label">
        Recent exports
      </h3>
      <ul>
        {others.map((made) => (
          <li key={made.id}>
            {made.state === "ready" ? (
              <a href={contentUrl(made)} download={made.file_name}>
                {made.file_name}
              </a>
            ) : (
              <span>{made.file_name}</span>
            )}{" "}
            <span className="muted">
              {made.state === "ready" && made.expires_at ? (
                <>
                  · deleted <RelativeTime value={made.expires_at} />
                </>
              ) : made.state === "failed" ? (
                "· failed"
              ) : made.state === "expired" ? (
                "· expired"
              ) : (
                `· writing ${made.done.toLocaleString()} of ${made.count.toLocaleString()}`
              )}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
