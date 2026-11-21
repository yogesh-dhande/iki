/**
 * Exporting a record as a file other tools read, such as GenBank (ADR 0171): from its
 * page's header, or a revision as it was from its history. Iki writes the file from the
 * record's values at that revision, and audits the download. A file that couldn't hold
 * all of the record says so, and the page says so too.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { useState } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { iki, problemOf, useApiMutation } from "../api";
import type { Entity, EntityType } from "../entities";
import { Button, ProblemAlert, RadioField, useMessages } from "../ui";

type Format = Schemas["FileFormatOut"];
type FormatName = Format["format"];

interface Saved {
  name: string;
  leftOut: number;
}

/** The file's name, as the server gives it in Content-Disposition. */
function fileName(response: Response, fallback: string): string {
  const header = response.headers.get("Content-Disposition") ?? "";
  return /filename="([^"]+)"/.exec(header)?.[1] ?? fallback;
}

/** Hands the browser the file to save, under its name. */
function save(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => {
    URL.revokeObjectURL(url);
  }, 0);
}

function leftOutText(saved: Saved, format: FormatName): string {
  if (!saved.leftOut) return "";
  const things = saved.leftOut === 1 ? "1 thing" : `${String(saved.leftOut)} things`;
  return format === "genbank"
    ? ` It couldn’t hold ${things} of the record: the file’s COMMENT says which.`
    : ` It couldn’t hold ${things} of the record, such as its features, which GenBank holds.`;
}

/**
 * An Export button and its dialog, for a type that exports files: nothing for one that
 * doesn't. With `revision`, it exports that revision as it was.
 */
export function ExportButton({
  entity,
  type,
  revision,
}: {
  entity: Entity;
  type: EntityType | undefined;
  revision?: number;
}) {
  const formats = (type?.exports ?? []).flatMap((e) => e.formats);
  const [open, setOpen] = useState(false);
  const [chosen, setChosen] = useState<FormatName | undefined>(undefined);
  const { say } = useMessages();
  const format = chosen ?? formats[0]?.format;
  const download = useApiMutation(async (wanted: FormatName): Promise<Saved> => {
    const result = await iki.GET("/api/v1/entities/{ref}/export", {
      params: {
        path: { ref: entity.id },
        query: { format: wanted, ...(revision === undefined ? {} : { revision }) },
      },
      parseAs: "blob",
    });
    const blob = unwrap<Blob>(result);
    const extension = formats.find((f) => f.format === wanted)?.extension ?? "txt";
    const name = fileName(result.response, `${entity.hrid}.${extension}`);
    save(blob, name);
    return { name, leftOut: Number(result.response.headers.get("Iki-Left-Out") ?? 0) };
  }, []);
  if (!formats.length || format === undefined) return null;
  const which = revision === undefined ? "" : `, revision ${String(revision)}`;
  const close = () => {
    setOpen(false);
    download.reset();
  };
  return (
    <>
      <Button
        {...(revision === undefined
          ? {}
          : { variant: "quiet" as const, label: `Export revision ${String(revision)}` })}
        onClick={() => {
          setOpen(true);
        }}
      >
        Export
      </Button>
      <ModalOverlay
        className="dialog-overlay"
        isOpen={open}
        isDismissable={!download.isPending}
        onOpenChange={(isOpen) => {
          if (!isOpen) close();
        }}
      >
        <Modal className="dialog">
          <Dialog className="dialog-body">
            <Heading slot="title" className="section-title">
              Export {entity.title}
              {which}
            </Heading>
            <ProblemAlert problem={download.error ? problemOf(download.error) : undefined} />
            <RadioField
              label="Format"
              value={format}
              onChange={setChosen}
              options={formats.map((f) => ({
                value: f.format,
                label: f.label,
                description: f.description,
              }))}
              help={
                revision === undefined
                  ? "Written from its latest revision. Each download is in the audit log."
                  : "Written from this revision, as it was then. Each download is in the audit log."
              }
            />
            <div className="actions">
              <Button
                variant="primary"
                busy={download.isPending}
                busyText="Writing…"
                onClick={() => {
                  download.mutate(format, {
                    onSuccess: (saved) => {
                      close();
                      say(`Downloaded ${saved.name}.${leftOutText(saved, format)}`);
                    },
                  });
                }}
              >
                Download
              </Button>
              <Button variant="quiet" onClick={close} disabled={download.isPending}>
                Cancel
              </Button>
            </div>
          </Dialog>
        </Modal>
      </ModalOverlay>
    </>
  );
}
