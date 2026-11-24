/**
 * Labels (ADR 0180): each record's unique Iki code as a square DataMatrix or a QR code, its
 * full HRID, title and label line, and, only when asked for and where it fits, its vendor
 * barcode as a separate Code 128; laid out at a label printer's size for the browser to
 * print, or as ZPL for a Zebra printer. The server checks what fits, for a preview and a
 * print alike. The codes are drawn here, by bwip-js, loaded only when a label is.
 */

import { unwrap } from "@ikisci/sdk";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useId, useState } from "react";
import { createPortal } from "react-dom";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import type { Problem } from "@ikisci/sdk";

import { iki, problemOf, useApiMutation } from "./api";
import { fetchPageRead } from "./page-reads";
import { labelSizesQuery, type LabelSize, type PreparedLabel } from "./storage";
import { Button, CheckboxField, Loading, RadioField, SelectField, useMessages } from "./ui";

export type Symbology = "datamatrix" | "qr";
type Code = Symbology | "code128";

const BCID: Record<Code, string> = { datamatrix: "datamatrix", qr: "qrcode", code128: "code128" };

/**
 * How each code is drawn, as the server's ZPL draws it: a square 24 × 24 ECC200 DataMatrix,
 * a version 3 QR code at error correction level Q, and a Code 128 without its text.
 */
const OPTIONS: Record<Code, Record<string, string | number | boolean>> = {
  datamatrix: { version: "24x24" },
  qr: { eclevel: "Q", version: 3 },
  code128: { height: 6, includetext: false },
};

/** A code drawn as SVG: modules only, its text shown beside it on the label. */
export async function codeSvg(kind: Code, text: string): Promise<string> {
  const { toSVG } = await import("@bwip-js/browser");
  return toSVG({
    bcid: BCID[kind],
    text,
    scale: 2,
    ...OPTIONS[kind],
    paddingwidth: 0,
    paddingheight: 0,
  });
}

/** An SVG as an image's source: the page's policy allows data: images, not inline markup. */
export function svgSource(svg: string): string {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

/** A label's codes, drawn once each: its unique Iki code, and its vendor barcode if asked for. */
function useCodes(label: PreparedLabel, symbology: Symbology) {
  const [codes, setCodes] = useState<{ square: string; bar: string | null } | null>(null);
  const [failed, setFailed] = useState(false);
  const vendor = label.vendor_code;
  useEffect(() => {
    let live = true;
    Promise.all([
      codeSvg(symbology, label.code),
      vendor !== null ? codeSvg("code128", vendor) : Promise.resolve(null),
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
  }, [label.code, symbology, vendor]);
  return { codes, failed };
}

/**
 * One label, at its size in millimetres, as it prints: the unique Iki code that identifies
 * the record and its full HRID, always; the vendor's Code 128 only when it was asked for,
 * apart from them and named as the vendor's.
 */
export function LabelFace({
  label,
  size,
  symbology,
}: {
  label: PreparedLabel;
  size: LabelSize;
  symbology: Symbology;
}) {
  const { codes, failed } = useCodes(label, symbology);
  const vendor = label.vendor_code;
  return (
    <div
      className={`label label-${size.key}${vendor !== null ? " label-with-vendor" : ""}`}
      aria-label={`Label for ${label.hrid}${vendor !== null ? `, with vendor barcode ${vendor}` : ""}`}
      role="img"
    >
      <div className={`label-square label-square-${symbology}`}>
        {codes ? (
          <img src={svgSource(codes.square)} alt="" data-symbol="iki" />
        ) : failed ? (
          <span className="label-failed">?</span>
        ) : null}
      </div>
      <div className="label-text">
        <strong className="label-hrid">{label.hrid}</strong>
        {label.title ? <span className="label-title">{label.title}</span> : null}
        {label.line ? <span className="label-line">{label.line}</span> : null}
      </div>
      {vendor !== null && codes?.bar ? (
        <div className="label-bar">
          <img src={svgSource(codes.bar)} alt="" data-symbol="vendor" />
          <span className="label-vendor">Vendor {vendor}</span>
        </div>
      ) : null}
    </div>
  );
}

/** Why labels can't be prepared as asked: each record's reason, or the request's. */
function LabelProblem({ problem }: { problem: Problem | undefined }) {
  if (!problem) return null;
  const reasons = [...new Set((problem.errors ?? []).map((e) => e.detail))];
  return (
    <div className="alert alert-error" role="alert">
      {reasons.length ? (
        <>
          <p>These labels can’t be printed as chosen:</p>
          <ul>
            {reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </>
      ) : (
        <p>{problem.detail}</p>
      )}
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
  const [vendorAsked, setVendorAsked] = useState(false);
  const [printing, setPrinting] = useState<PreparedLabel[] | null>(null);
  const { say } = useMessages();
  const size = sizes.data?.sizes.find((s) => s.key === sizeKey);
  // Only a stock with room for one can add the vendor's Code 128; the server checks it fits.
  const vendor = vendorAsked && !!size?.code128;
  const ids = records.map((r) => r.id);
  const options: Options = { size: sizeKey, symbology, dpi: Number(dpi), vendor };
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
            ...(vendor ? { include_vendor_code: true } : {}),
            preview: false,
          },
        }),
      ),
    [],
  );
  const preview = usePreviewLabels(ids, options);
  const shown = preview.data ?? [];
  const previewProblem = preview.error ? problemOf(preview.error) : undefined;
  const changed = () => {
    prepare.reset();
  };
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
          <LabelProblem problem={prepare.error ? problemOf(prepare.error) : undefined} />
          {!sizes.data ? (
            <Loading label="Loading label sizes…" />
          ) : (
            <div className="label-options">
              <SelectField
                label="Size"
                options={sizes.data.sizes.map((s) => ({ value: s.key, label: s.label }))}
                value={sizeKey}
                onChange={(v) => {
                  setSizeKey(v);
                  changed();
                }}
              />
              <RadioField
                label="2D code"
                options={[
                  { value: "datamatrix", label: "DataMatrix" },
                  { value: "qr", label: "QR code" },
                ]}
                value={symbology}
                onChange={(v) => {
                  setSymbology(v);
                  changed();
                }}
                help="Each label’s unique Iki code, printed beside its Iki ID."
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
                onChange={(v) => {
                  setDpi(v);
                  changed();
                }}
                help="For ZPL, and to check a vendor barcode fits."
              />
              <CheckboxField
                label="Also print the vendor barcode (Code 128)"
                checked={vendor}
                disabled={!size?.code128}
                onChange={(v) => {
                  setVendorAsked(v);
                  changed();
                }}
                help={
                  size && !size.code128
                    ? "This size has no room for one: its labels carry the Iki code and ID alone."
                    : "For an instrument that reads it. It’s printed apart from the Iki code, and only if it fits."
                }
              />
            </div>
          )}
          <h3 className="label-preview-title" id={`${id}-preview`}>
            Preview{n > shown.length && shown.length ? ` of the first ${String(shown.length)}` : ""}
          </h3>
          <LabelProblem problem={previewProblem} />
          <div className="label-preview" aria-labelledby={`${id}-preview`} role="group">
            {previewProblem ? (
              <p className="muted">Nothing to preview until they can be printed as chosen.</p>
            ) : size && shown.length ? (
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
              disabled={!size || !!previewProblem}
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
              disabled={!size || !!previewProblem}
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

/** What a preview is asked for: the same as a print, so the server checks the same. */
interface Options {
  size: string;
  symbology: Symbology;
  dpi: number;
  vendor: boolean;
}

function usePreviewLabels(ids: string[], options: Options) {
  const first = ids.slice(0, PREVIEWED);
  return useQuery({
    queryKey: ["storage", "label-preview", first.join(","), options],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.POST("/api/v1/storage/labels", {
          fetch: fetchPageRead,
          body: {
            records: first,
            size: options.size,
            format: "print",
            symbology: options.symbology,
            dpi: options.dpi,
            copies: 1,
            ...(options.vendor ? { include_vendor_code: true } : {}),
            preview: true,
          },
          signal,
        }),
      ).labels,
    // A refusal is the answer: it isn't asked again.
    retry: false,
    placeholderData: (previous) => previous,
  });
}
