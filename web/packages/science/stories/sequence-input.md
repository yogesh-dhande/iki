### Sequence and HELM input

Import `@ikisci/science/sequence-input` and set the `data` property of `<iki-sequence-input>`. Supply `label` and a string `value`. The default `format: "sequence"` provides a one-line editor. Use `format: "helm"` for a multiline editor and live status. Set `reading` to the description produced by your parser. Editing clears the previous reading.

Each edit emits a bubbling `value-change` event with the exact text in `event.detail`. Optional `inputId` links the native input to a visible label's `for` attribute. Optional `describedBy` contains IDs of help or error messages. The optional booleans `disabled`, `invalid` and `required` set the control's state. Reassign `data` to update the element. Import `@ikisci/science/science-inputs.css` for the compact input styles. [Sequence input story](./sequence-input.html)
