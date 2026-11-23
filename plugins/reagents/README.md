# Reagents and consumables

Use **Stock** in a project to see what the lab can draw, where each lot is
stored, and which orders are still due. A catalogue item represents one stock
unit and pack size. Each delivery has its own lot identity, receipt and
amount ledger.

## Order and receive

1. Create a **Stock item** with its supplier, catalogue number, pack size,
   stock unit and low-stock threshold. Choose the pack the lab orders, such
   as a rack of 50. A case of 500 needs its own catalogue item. Count
   consumables in units (`1`).
   A stock item can also be measured by mass (mg) or volume (mL).
2. Create a **Stock location** for each storage destination. Assign the project's
   **Stock manager** role to the people who should receive low-stock alerts.
3. From the item, place a reorder for a whole number of packs. Record the
   supplier order reference. The reorder list shows packs ordered, received
   and outstanding.
4. Receive each delivery against that order. Select its storage destination
   and enter the supplier lot, delivery reference and delivery line. A partial
   receipt leaves the remaining packs open. The receipt opens the delivered
   lot's amount ledger and places the lot in storage. The supplier, delivery
   note and line identify one receipt even if they are entered on another
   order with different capitalization or surrounding whitespace.
5. Use the ordinary lot QA flow to release the material. Withdraw from the
   lot's amount ledger and select the experiment under **Used in** when
   recording consumption.

## Read stock levels

On-hand stock includes the physical balance of lots in the project. Available
stock counts stored material eligible for routine use. Checked-out,
unplaced, expired and quarantined lots can have a physical balance. Inspect an item's lots to
see its locations, remaining balances and eligibility. A low-stock alert
uses the available balance and the item's threshold, including equality.
An item with a threshold and no usable stock can already need reordering.
The hourly maintenance check notifies stock managers. After it observes a
recovery above the threshold, a later drop can alert again.

The dev example uses Corning catalogue number 430790. The manufacturer lists
50 tubes per rack and 500 per case. The example orders two racks, receives
them separately, and draws three tubes from the first rack into a draft
ELN-linked experiment. The experiment records the same movement ID as the
lot ledger. Its order, lots, dates and withdrawals are synthetic. [Corning product catalogue](https://ecatalog.corning.com/life-sciences/b2b/us/en/Liquid-Handling/Tubes%2C-Liquid-Handling/Centrifuge-Tubes/Corning%C2%AE-15mL-Centrifuge-Tubes/p/430790)
