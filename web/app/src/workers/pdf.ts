/**
 * pdf.js's worker, for PDF previews (ADR 0226): it parses the PDF the page hands it and
 * draws its pages' operations for the page to paint. Like Iki's other workers it runs under
 * a policy of its own, from its script's response: no network beyond Iki's origin, and
 * WebAssembly (pdf.js's image decoders) but no `eval`. Importing pdf.js's worker module is
 * what starts it listening to the page.
 */

import "pdfjs-dist/build/pdf.worker.min.mjs";
