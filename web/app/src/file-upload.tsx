/**
 * Adding files from this computer, saying when their data was acquired (ADR 0177): in the
 * file picker, and in a project's Files tab. Where a file's format records when, Iki reads
 * it from the file and that wins; otherwise it's the day given here, or unknown. Never the
 * upload time.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { formatSize } from "./entities";
import { FILES } from "./files";
import { media } from "./media";
import { Button, useMessages } from "./ui";
import { upload, uploadError, type Progress } from "./uploads";

/** Formats whose files say when their data was acquired, which Iki reads (ADR 0177). */
export const SELF_DATED = "AB1, FCS, mzML, JCAMP-DX, EXIF images, OME-TIFF, SAM and BAM";

/** Today, in the person's own calendar: no day after it can be when data was acquired. */
function today(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${String(now.getFullYear())}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** How far an upload has got, as one line. */
export function UploadStatus({ name, progress }: { name: string; progress: Progress }) {
  if (progress.phase === "checking") return <>Iki is checking {name}’s bytes…</>;
  const percent = progress.total ? Math.floor((progress.done / progress.total) * 100) : 100;
  return (
    <>
      {progress.phase === "hashing" ? "Reading" : "Uploading"} {name}: {percent}% (
      {formatSize(progress.done)} of {formatSize(progress.total)})
    </>
  );
}

/** When the files' data was acquired, as the person adding them says: optional. */
export function AcquiredField({
  value,
  onChange,
  several,
}: {
  value: string;
  onChange: (value: string) => void;
  several: boolean;
}) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>
        {several ? "When their data was acquired" : "When its data was acquired"}
        <span className="optional"> (optional)</span>
      </label>
      <input
        id={id}
        type="date"
        className="file-acquired-input"
        value={value}
        max={today()}
        aria-describedby={`${id}-help`}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
      <p className="field-help" id={`${id}-help`}>
        {SELF_DATED} files record it themselves, and Iki reads it from them instead. For others,
        give the day here, or it stays unknown. It’s never taken from when it’s added.
      </p>
    </div>
  );
}

/** The files chosen from this computer, before they're sent: what they are, and when. */
export function UploadStep({
  files,
  onUpload,
  onBack,
  backLabel = "Back",
}: {
  files: readonly File[];
  onUpload: (acquiredAt: string) => void;
  onBack: () => void;
  backLabel?: string;
}) {
  const [acquiredAt, setAcquiredAt] = useState("");
  const late = acquiredAt > today();
  return (
    <div className="file-upload-step">
      <ul className="file-upload-list">
        {files.map((file, i) => (
          <li key={`${String(i)} ${file.name}`}>
            <span className="file-upload-name">{file.name}</span>{" "}
            <span className="muted">{formatSize(file.size)}</span>
          </li>
        ))}
      </ul>
      <AcquiredField value={acquiredAt} onChange={setAcquiredAt} several={files.length > 1} />
      <div className="actions">
        <Button
          variant="primary"
          disabled={late}
          onClick={() => {
            onUpload(acquiredAt);
          }}
        >
          {files.length === 1 ? "Upload it" : `Upload ${String(files.length)} files`}
        </Button>
        <Button variant="quiet" onClick={onBack}>
          {backLabel}
        </Button>
      </div>
    </div>
  );
}

type Sending =
  | { state: "waiting" }
  | { state: "sending"; progress: Progress | null }
  | { state: "added" }
  | { state: "failed"; problem: string };

/** At most this many files send at once; each sends its parts a few at a time too. */
const AT_ONCE = 3;

/** A project's Files tab's Upload files: chosen here, then sent, each with its progress. */
export function UploadFiles({
  projectId,
  projectName,
}: {
  projectId: string;
  projectName: string;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [sending, setSending] = useState<Sending[] | null>(null);
  const transfer = useRef<AbortController | null>(null);
  const client = useQueryClient();
  const { say } = useMessages();
  const busy = sending?.some((s) => s.state === "waiting" || s.state === "sending") ?? false;

  // Leaving the page stops what's still sending.
  useEffect(
    () => () => {
      transfer.current?.abort();
    },
    [],
  );

  const close = () => {
    transfer.current?.abort();
    transfer.current = null;
    setFiles([]);
    setSending(null);
  };

  async function send(acquiredAt: string) {
    const controller = new AbortController();
    transfer.current = controller;
    const states: Sending[] = files.map(() => ({ state: "waiting" }));
    const show = (i: number, next: Sending) => {
      states[i] = next;
      if (!controller.signal.aborted) setSending([...states]);
    };
    setSending([...states]);
    let next = 0;
    const worker = async () => {
      while (next < files.length && !controller.signal.aborted) {
        const i = next++;
        const file = files[i];
        if (!file) continue;
        show(i, { state: "sending", progress: null });
        try {
          await upload(
            file,
            projectId,
            (progress) => {
              show(i, { state: "sending", progress });
            },
            controller.signal,
            media.of(file),
            acquiredAt,
          );
          show(i, { state: "added" });
        } catch (failure) {
          show(i, { state: "failed", problem: uploadError(failure) });
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(AT_ONCE, files.length) }, worker));
    if (controller.signal.aborted) return;
    transfer.current = null;
    void client.invalidateQueries({ queryKey: FILES });
    const added = states.filter((s) => s.state === "added").length;
    if (added === files.length) {
      say(added === 1 ? `Added ${files[0]?.name ?? "the file"}.` : `Added ${String(added)} files.`);
      close();
    }
  }

  return (
    <>
      <Button
        onClick={() => {
          input.current?.click();
        }}
      >
        Upload files
      </Button>
      <input
        ref={input}
        type="file"
        hidden
        multiple
        aria-label="Upload files"
        onChange={(event) => {
          const chosen = [...(event.target.files ?? [])];
          event.target.value = "";
          if (chosen.length) setFiles(chosen);
        }}
      />
      <ModalOverlay
        className="dialog-overlay"
        isOpen={files.length > 0}
        isDismissable={!busy}
        onOpenChange={(open) => {
          if (!open) close();
        }}
      >
        <Modal className="dialog">
          <Dialog className="dialog-body">
            <Heading slot="title" className="section-title">
              {files.length === 1
                ? `Upload a file to ${projectName}`
                : `Upload files to ${projectName}`}
            </Heading>
            {sending ? (
              <>
                <ul className="file-upload-list" aria-live="polite">
                  {files.map((file, i) => {
                    const s = sending[i] ?? { state: "waiting" };
                    return (
                      <li key={`${String(i)} ${file.name}`}>
                        {s.state === "sending" && s.progress ? (
                          <UploadStatus name={file.name} progress={s.progress} />
                        ) : (
                          <>
                            <span className="file-upload-name">{file.name}</span>{" "}
                            <span className={s.state === "failed" ? "field-error" : "muted"}>
                              {s.state === "waiting"
                                ? "Waiting"
                                : s.state === "sending"
                                  ? "Starting…"
                                  : s.state === "added"
                                    ? "Added"
                                    : s.problem}
                            </span>
                          </>
                        )}
                      </li>
                    );
                  })}
                </ul>
                <div className="actions">
                  <Button variant="quiet" onClick={close}>
                    {busy ? "Cancel" : "Close"}
                  </Button>
                </div>
              </>
            ) : (
              <UploadStep
                files={files}
                backLabel="Cancel"
                onBack={close}
                onUpload={(acquiredAt) => void send(acquiredAt)}
              />
            )}
          </Dialog>
        </Modal>
      </ModalOverlay>
    </>
  );
}
