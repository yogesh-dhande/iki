### Quantity input

Import `@ikisci/science/quantity-input` and set the `data` property of `<iki-quantity-input>`. Supply `label`, a `referenceUnit` string, an array `units` of objects with a UCUM `code` and display `symbol`, and a `value` object with three strings.

| Value property | Meaning                                                               |
| -------------- | --------------------------------------------------------------------- |
| `number`       | The amount exactly as typed, including a partial decimal or exponent. |
| `unit`         | One of the offered unit codes, or the exported `OTHER_UNIT` constant. |
| `other`        | The custom UCUM code entered for `OTHER_UNIT`.                        |

Each edit emits a bubbling `value-change` event with the updated value object in `event.detail`. The host validates and converts the amount as appropriate for its unit system. Optional `inputId` links a label to the amount, `describedBy` names help or error elements by ID, and `disabled`, `invalid` and `required` set the control's state. Reassign `data` to update defaults or units. Import `@ikisci/science/science-inputs.css` for compact input styles. [Quantity input story](./quantity-input.html)
