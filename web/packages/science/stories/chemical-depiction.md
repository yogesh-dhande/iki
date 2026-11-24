### Chemical depiction

Import `@ikisci/science/chemical-depiction` to register `<iki-chemical-depiction>`.
Set its `data` property to `{ smiles: string, name?: string, label?: string }`.
The SMILES supplies atom connectivity and stereochemistry. The name labels the drawing
for screen readers and the label identifies the compound in errors. Assign a new object
to update the drawing, or `null` to clear it.

The element emits bubbling, composed `depiction-ready` with `{ smiles }` when it
creates the image and `depiction-error` with a message string on failure. The entry
includes the RDKit engine. A restricted document needs permission to compile
WebAssembly and display blob images. [Open the aspirin example](chemical-depiction.html).
