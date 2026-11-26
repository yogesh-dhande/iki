/** What a preview says when it can't show a file: why, and the file's download. */

import type { ReactNode } from "react";

import { downloadHref } from "../files";
import { buttonClass } from "../ui";
import type { PreviewFile } from "./source";

export function Download({ file }: { file: PreviewFile }) {
  return (
    <a className={buttonClass()} href={downloadHref(file.id)} download={file.name}>
      Download {file.name}
    </a>
  );
}

/** When the preview can't show the file: why, and the download. */
export function NoPreview({
  file,
  children,
  download = true,
}: {
  file: PreviewFile;
  children: ReactNode;
  download?: boolean;
}) {
  return (
    <div className="preview-none">
      <p>{children}</p>
      {download ? <Download file={file} /> : null}
    </div>
  );
}
