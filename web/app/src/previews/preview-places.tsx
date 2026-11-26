/**
 * Where previews open (ADR 0226): on a file's page, under its facts, and from the records
 * and runs that hold a file, in a dialog beside its link. Each opens the same preview,
 * reading the file through the same audited link a download takes.
 */

import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { problemOf } from "../api";
import { formatSize } from "../entities";
import { downloadHref, fileQuery } from "../files";
import { useMedia } from "../media";
import { Button, buttonClass, Loading, ProblemAlert } from "../ui";
import { FilePreview, VIEWERS } from "./file-preview";
import type { PreviewFile } from "./source";

/** A file up to this opens its preview with its page; a larger one waits to be asked, since
 * a preview reads it, as a download does. Viewers that read ranges always open. */
export const OPEN_WITH_PAGE = 25 * 1024 * 1024;

function useViewerName(mediaType: string): string | undefined {
  const name = useMedia().get(mediaType)?.viewer;
  return name && Object.hasOwn(VIEWERS, name) ? name : undefined;
}

/** A file page's preview: shown with the page, or on asking for a large file. */
export function FilePreviewSection({ file }: { file: PreviewFile }) {
  const name = useViewerName(file.media_type);
  const viewer = name ? VIEWERS[name] : undefined;
  const ranged = viewer?.max === null;
  const [asked, setAsked] = useState(false);
  // Past what its viewer opens, the preview says so without reading anything.
  const tooBig = viewer?.max != null && file.size > viewer.max;
  const open = asked || ranged || tooBig || file.size <= OPEN_WITH_PAGE;
  return (
    <section className="preview" aria-labelledby="preview-heading">
      <h2 className="section-title preview-heading" id="preview-heading">
        {viewer ? viewer.shows : "Preview"}
      </h2>
      {open || !viewer ? (
        <FilePreview file={file} />
      ) : (
        <div className="preview-none">
          <p>
            It’s {formatSize(file.size)}. Showing it reads all of it, as a download does, and is
            recorded as one.
          </p>
          <Button
            onClick={() => {
              setAsked(true);
            }}
          >
            Show {viewer.shows.toLowerCase()}
          </Button>
        </div>
      )}
    </section>
  );
}

/** A "Preview" button beside a file's link in a record or run, if Iki can show its format. */
export function PreviewButton({
  id,
  name,
  mediaType,
}: {
  id: string;
  name: string;
  mediaType: string;
}) {
  const viewer = useViewerName(mediaType);
  const [open, setOpen] = useState(false);
  if (!viewer) return null;
  return (
    <>
      <Button
        variant="quiet"
        label={`Preview ${name}`}
        onClick={() => {
          setOpen(true);
        }}
      >
        Preview
      </Button>
      <ModalOverlay className="dialog-overlay" isOpen={open} isDismissable onOpenChange={setOpen}>
        <Modal className="dialog preview-dialog">
          <Dialog className="dialog-body">
            {({ close }) => (
              <>
                <div className="preview-dialog-head">
                  <Heading slot="title" className="section-title">
                    {name}
                  </Heading>
                  <div className="actions">
                    <Link className={buttonClass()} to="/files/$id/" params={{ id }}>
                      Open its page
                    </Link>
                    <a className={buttonClass()} href={downloadHref(id)} download={name}>
                      Download
                    </a>
                    <Button variant="quiet" onClick={close}>
                      Close
                    </Button>
                  </div>
                </div>
                <PreviewOf id={id} />
              </>
            )}
          </Dialog>
        </Modal>
      </ModalOverlay>
    </>
  );
}

/** The file's own record first: its SHA-256 to check, and whether its scan holds it. */
function PreviewOf({ id }: { id: string }) {
  const found = useQuery(fileQuery(id));
  if (found.error) return <ProblemAlert problem={problemOf(found.error)} />;
  if (!found.data) return <Loading label="Loading the file…" />;
  return <FilePreview file={found.data} />;
}
