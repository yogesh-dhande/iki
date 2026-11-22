/**
 * The file picker (ADR 0177): a project's Files list in a dialog, held to what a field or
 * param takes, with its search, filters and sort. Wherever someone chooses a file already
 * in Iki: file fields, lists of files and file cells in rows, run file params, lists of
 * them, and the spreadsheet importer. Uploading a new one stays beside it.
 */

import { useRef, useState } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { formatSize } from "./entities";
import { FileTable } from "./file-table";
import { UploadStep } from "./file-upload";
import { NO_FILTER, type FileFilter, type FileRow, type Takes } from "./files";
import { media } from "./media";
import { Button } from "./ui";

export interface FilePickerProps {
  isOpen: boolean;
  onClose: () => void;
  /** The project whose files are listed: those added to it and those its records hold. */
  projectId: string;
  /** What it's choosing for, for its heading: "Choose a file for Trace". */
  label: string;
  /** What the field or param takes: only those files are listed. */
  takes: Takes;
  /** Several at once, up to `room` more; or one. */
  multiple?: boolean;
  room?: number | undefined;
  /** What's already chosen, which shows as chosen. */
  chosen?: readonly string[];
  onChoose: (files: FileRow[]) => void;
  /** Files from this computer instead, acquired `acquiredAt` if the person said (a day, or
   * ""): the field uploads them as it does its own. */
  onUpload?: ((files: File[], acquiredAt: string) => void) | undefined;
}

/** What a field takes, as a sentence: "It takes PDF files, up to 10.0 MiB." */
export function takesText(takes: Takes): string {
  const rules = [
    takes.accepts.length ? `${media.either(takes.accepts)} files` : "",
    takes.maxSize != null ? `up to ${formatSize(takes.maxSize)}` : "",
  ]
    .filter(Boolean)
    .join(", ");
  return rules ? `It takes ${rules}.` : "";
}

export function FilePicker(props: FilePickerProps) {
  // Its state starts afresh each time it opens.
  return (
    <ModalOverlay
      className="dialog-overlay"
      isOpen={props.isOpen}
      isDismissable
      onOpenChange={(open) => {
        if (!open) props.onClose();
      }}
    >
      <Modal className="dialog file-picker">
        <Dialog className="dialog-body">{props.isOpen ? <PickerBody {...props} /> : null}</Dialog>
      </Modal>
    </ModalOverlay>
  );
}

function PickerBody({
  onClose,
  projectId,
  label,
  takes,
  multiple = false,
  room,
  chosen: already = [],
  onChoose,
  onUpload,
}: FilePickerProps) {
  const [filter, setFilter] = useState<FileFilter>(NO_FILTER);
  const [chosen, setChosen] = useState<Map<string, FileRow>>(new Map());
  // Files from this computer, before they're sent: the picker asks when they were acquired.
  const [pending, setPending] = useState<File[] | null>(null);
  const held = new Set(already);
  const input = useRef<HTMLInputElement>(null);
  const rules = takesText(takes);
  const choose = (files: FileRow[]) => {
    onChoose(files);
    onClose();
  };
  if (pending && onUpload) {
    return (
      <>
        <Heading slot="title" className="section-title">
          {pending.length === 1 ? `Upload a file for ${label}` : `Upload files for ${label}`}
        </Heading>
        <UploadStep
          files={pending}
          onUpload={(acquiredAt) => {
            onUpload(pending, acquiredAt);
            onClose();
          }}
          onBack={() => {
            setPending(null);
          }}
        />
      </>
    );
  }
  return (
    <>
      <Heading slot="title" className="section-title">
        {multiple ? `Choose files for ${label}` : `Choose a file for ${label}`}
      </Heading>
      <p className="muted file-picker-rules">
        {[
          rules,
          multiple && room !== undefined
            ? room > 0
              ? `Choose up to ${String(room)}.`
              : "It holds as many as it takes: remove some first."
            : "",
        ]
          .filter(Boolean)
          .join(" ")}
      </p>
      <FileTable
        projectId={projectId}
        filter={filter}
        onFilter={setFilter}
        takes={takes}
        caption={`Files you can choose for ${label}`}
        pick={{
          multiple,
          room,
          chosen,
          held,
          onToggle: (file, on) => {
            if (!multiple) {
              if (on) choose([file]);
              return;
            }
            setChosen((current) => {
              const next = new Map(current);
              if (on) next.set(file.id, file);
              else next.delete(file.id);
              return next;
            });
          },
        }}
        actions={
          onUpload ? (
            <>
              <Button
                onClick={() => {
                  input.current?.click();
                }}
              >
                {multiple ? "Upload new ones" : "Upload a new one"}
              </Button>
              <input
                ref={input}
                type="file"
                hidden
                aria-label={multiple ? "Upload new ones" : "Upload a new one"}
                multiple={multiple}
                accept={media.accept(takes.accepts)}
                onChange={(event) => {
                  const files = [...(event.target.files ?? [])];
                  event.target.value = "";
                  if (files.length) setPending(files);
                }}
              />
            </>
          ) : null
        }
      />
      <div className="actions">
        {multiple ? (
          <Button
            variant="primary"
            disabled={chosen.size === 0}
            onClick={() => {
              choose([...chosen.values()]);
            }}
          >
            {chosen.size
              ? `Choose ${String(chosen.size)} ${chosen.size === 1 ? "file" : "files"}`
              : "Choose files"}
          </Button>
        ) : null}
        <Button variant="quiet" onClick={onClose}>
          Cancel
        </Button>
      </div>
    </>
  );
}
