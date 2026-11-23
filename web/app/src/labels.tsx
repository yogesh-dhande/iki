/**
 * Labels (ADR 0180): each record's code as a DataMatrix or QR code and as Code 128, its
 * HRID, title and label line, laid out at a label printer's size for the browser to print,
 * or as ZPL for a Zebra printer. The codes are drawn here, by bwip-js, loaded only when a
 * label is.
 */

import { unwrap } from "@ikisci/sdk";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useId, useState } from "react";
import { createPortal } from "react-dom";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { iki, problemOf, useApiMutation } from "./api";
import { labelSizesQuery, type LabelSize, type PreparedLabel } from "./storage";
import { Button, Loading, ProblemAlert, RadioField, SelectField, useMessages } from "./ui";

export type Symbology = "datamatrix" | "qr";
type Code = Symbology | "code128";

const BCID: Record<Code, string> = { datamatrix: "datamatrix", qr: "qrcode", code128: "code128" };

/** A code drawn as SVG: modules only, its text shown beside it on the label. */
export async function codeSvg(kind: Code, text: string): Promise<string> {
  const { toSVG } = await import("@bwip-js/browser");
  return toSVG({
    bcid: BCID[kind],
    text,
    scale: 2,
    ...(kind === "code128" ? { height: 6, includetext: false } : {}),
    paddingwidth: 0,
    paddingheight: 0,
  });
}

/** An SVG as an image's source: the page's policy allows data: images, not inline markup. */
export function svgSource(svg: string): string {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

/** The two codes of a label, drawn once each. */
function useCodes(label: PreparedLabel, symbology: Symbology, withBar: boolean) {
  const [codes, setCodes] = useState<{ square: string; bar: string | null } | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    Promise.all([
      codeSvg(symbology, label.code),
      withBar ? codeSvg("code128", label.code) : Promise.resolve(null),
    ]).then(
      ([square, bar]) => {
        if (live) setCodes({ square, bar });
      },
      () => {
        if (live) setFailed(true);
      },
    );
    return () => {
      live = false;
    };
  }, [label.code, symbology, withBar]);
  return { codes, failed };
}

/** One label, at its size in millimetres, as it prints. */
export function LabelFace({
  label,
  size,
  symbology,
}: {
  label: PreparedLabel;
  size: LabelSize;
  symbology: Symbology;
}) {
  const { codes, failed } = useCodes(label, symbology, size.code128);
  return (
    <div className={`label label-${size.key}`} aria-label={`Label for ${label.hrid}`} role="img">
      <div className="label-square">
        {codes ? (
          <img src={svgSource(codes.square)} alt="" />
        ) : failed ? (
          <span className="label-failed">?</span>
        ) : null}
      </div>
      <div className="label-text">
        <strong className="label-hrid">{label.hrid}</strong>
        {label.title ? <span className="label-title">{label.title}</span> : null}
        {label.line ? <span className="label-line">{label.line}</span> : null}
        {label.code !== label.hrid ? <span className="label-code">{label.code}</span> : null}
      </div>
      {size.code128 && codes?.bar ? (
        <div className="label-bar">
          <img src={svgSource(codes.bar)} alt="" />
        </div>
      ) : null}
    </div>
  );
}

function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** Choosing a size and how, previewing, then printing or downloading ZPL. */
export function LabelDialog({
  records,
  onClose,
}: {
  /** The records to label, by ID, with their HRIDs to name them. */
  records: { id: string; hrid: string }[];
  onClose: () => void;
}) {
  const id = useId();
  const sizes = useQuery(labelSizesQuery);
  const [sizeKey, setSizeKey] = useState("standard");
  const [symbology, setSymbology] = useState<Symbology>("datamatrix");
  const [dpi, setDpi] = useState("203");
  const [copies, setCopies] = useState("1");
  const [printing, setPrinting] = useState<PreparedLabel[] | null>(null);
  const { say } = useMessages();
  const size = sizes.data?.sizes.find((s) => s.key === sizeKey);
  const ids = records.map((r) => r.id);
  const prepare = useApiMutation(
    async (format: "print" | "zpl") =>
      unwrap(
        await iki.POST("/api/v1/storage/labels", {
          body: {
            records: ids,
            size: sizeKey,
            format,
            symbology,
            dpi: Number(dpi),
            copies: Number(copies) || 1,
            preview: false,
          },
        }),
      ),
    [],
  );
  const shown = usePreviewLabels(ids);
  useEffect(() => {
    if (!printing) return;
    const after = () => {
      setPrinting(null);
    };
    window.addEventListener("afterprint", after);
    // Once the codes are drawn: images load from data: sources at once, but bwip-js is
    // loaded on first use.
    const timer = window.setTimeout(() => {
      window.print();
    }, 300);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("afterprint", after);
    };
  }, [printing]);
  const n = records.length;
  const named = n === 1 ? (records[0]?.hrid ?? "1 record") : `${String(n)} records`;
  return (
    <ModalOverlay
      className="dialog-overlay"
      isOpen
      isDismissable={!prepare.isPending}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <Modal className="dialog label-dialog">
        <Dialog className="dialog-body" aria-labelledby={`${id}-title`}>
          <Heading slot="title" className="section-title" id={`${id}-title`}>
            Print labels for {named}
          </Heading>
          <ProblemAlert problem={prepare.error ? problemOf(prepare.error) : undefined} />
          {!sizes.data ? (
            <Loading label="Loading label sizes…" />
          ) : (
            <div className="label-options">
              <SelectField
                label="Size"
                options={sizes.data.sizes.map((s) => ({ value: s.key, label: s.label }))}
                value={sizeKey}
                onChange={setSizeKey}
                {...(size && !size.code128
                  ? { help: "Too narrow for a Code 128: the 2D code alone." }
                  : {})}
              />
              <RadioField
                label="2D code"
                options={[
                  { value: "datamatrix", label: "DataMatrix" },
                  { value: "qr", label: "QR code" },
                ]}
                value={symbology}
                onChange={setSymbology}
              />
              <SelectField
                label="Copies of each"
                options={["1", "2", "3", "4", "5"].map((v) => ({ value: v, label: v }))}
                value={copies}
                onChange={setCopies}
              />
              <SelectField
                label="Zebra printer’s resolution"
                options={sizes.data.dpis.map((d) => ({
                  value: String(d),
                  label: `${String(d)} dpi`,
                }))}
                value={dpi}
                onChange={setDpi}
                help="For ZPL only."
              />
            </div>
          )}
          <h3 className="label-preview-title" id={`${id}-preview`}>
            Preview{n > shown.length && shown.length ? ` of the first ${String(shown.length)}` : ""}
          </h3>
          <div className="label-preview" aria-labelledby={`${id}-preview`} role="group">
            {size && shown.length ? (
              shown.map((l) => <LabelFace key={l.id} label={l} size={size} symbology={symbology} />)
            ) : (
              <Loading label="Drawing labels…" />
            )}
          </div>
          <div className="actions">
            <Button
              variant="primary"
              busy={prepare.isPending && prepare.variables === "print"}
              busyText="Preparing…"
              disabled={!size}
              onClick={() => {
                prepare.mutate("print", {
                  onSuccess: (done) => {
                    setPrinting(done.labels);
                  },
                });
              }}
            >
              Print
            </Button>
            <Button
              busy={prepare.isPending && prepare.variables === "zpl"}
              busyText="Preparing…"
              disabled={!size}
              onClick={() => {
                prepare.mutate("zpl", {
                  onSuccess: (done) => {
                    download(`labels-${sizeKey}-${dpi}dpi.zpl`, done.zpl ?? "");
                    say(`Downloaded ZPL for ${named}.`);
                  },
                });
              }}
            >
              Download ZPL
            </Button>
            <Button variant="quiet" onClick={onClose} disabled={prepare.isPending}>
              Close
            </Button>
          </div>
          {printing && size
            ? createPortal(
                <div className={`label-sheet label-sheet-${size.key}`}>
                  {printing.flatMap((l) =>
                    Array.from({ length: Number(copies) || 1 }, (_, i) => (
                      <LabelFace
                        key={`${l.id}-${String(i)}`}
                        label={l}
                        size={size}
                        symbology={symbology}
                      />
                    )),
                  )}
                </div>,
                document.body,
              )
            : null}
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}

/** The labels a preview shows: the first few, as they'd print, without recording them. */
export const PREVIEWED = 6;

function usePreviewLabels(ids: string[]): PreparedLabel[] {
  const first = ids.slice(0, PREVIEWED);
  const found = useQuery({
    queryKey: ["storage", "label-preview", first.join(",")],
    queryFn: async () =>
      unwrap(
        await iki.POST("/api/v1/storage/labels", {
          body: {
            records: first,
            size: "standard",
            format: "print",
            symbology: "datamatrix",
            dpi: 203,
            copies: 1,
            preview: true,
          },
        }),
      ).labels,
  });
  return found.data ?? [];
}
