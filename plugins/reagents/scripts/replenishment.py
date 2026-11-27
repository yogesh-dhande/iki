"""Place an order for catalogue packs, or receive one delivery, transfer or count into stock."""

import hashlib
import json
import os
import sys
import unicodedata
from decimal import Decimal
from pathlib import Path
from typing import Any

PREFIX = "iki/reagents."
LOT_TYPES = {"mg": "MassStockLot", "mL": "VolumeStockLot", "1": "CountStockLot"}


def required_text(params: dict[str, Any], name: str) -> str:
    given = params[name]
    value = given.strip() if isinstance(given, str) else ""
    if not value:
        raise ValueError(f"Enter the {name.replace('_', ' ')}.")
    return value


def place_order(run: dict[str, Any]) -> dict[str, Any]:
    item = run["inputs"]["item"][0]
    params = run["params"]
    line = {
        "type": PREFIX + "ReorderLine",
        "derived_from": [item["id"]],
        "values": {
            "item": item["id"],
            "order_reference": required_text(params, "order_reference"),
            "ordered_packs": params["ordered_packs"],
        },
    }
    return {"creates": [line]}


def receipt_key(issuer: str, delivery_note: str, delivery_line: str) -> str:
    """A document line names one receipt even if someone enters it on a second order.

    The issuer is the supplier for a delivery, ordered or not, so a supplier's line can't be
    received once against an order and again without one. A transfer's issuer is its sender.
    """
    parts = [
        unicodedata.normalize("NFKC", part).strip().casefold()
        for part in (issuer, delivery_note, delivery_line)
    ]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def stock_lot(
    item: dict[str, Any], params: dict[str, Any], packs: int, issuer: str, reason: str
) -> dict[str, Any]:
    """The lot's values and its opening receipt, from the catalogue pack and the document.

    ``reason`` names how it arrived; the receipt's reason ends with the document and line.
    """
    catalogue = item["values"]
    unit = catalogue["stock_unit"]
    size = Decimal(str(catalogue["pack_size"]))
    if unit not in LOT_TYPES or not size.is_finite() or size <= 0:
        raise ValueError("The catalogue pack needs a positive size and a supported stock unit.")
    if unit == "1" and size != size.to_integral_value():
        raise ValueError("A consumable pack contains a whole number of individual units.")
    note = required_text(params, "delivery_note")
    line = required_text(params, "delivery_line")
    values = {
        "item": item["id"],
        "source": "bought",
        "supplier": catalogue["supplier"],
        "catalogue_number": catalogue["catalogue_number"],
        "lot_number": required_text(params, "lot_number"),
        "received_on": params["received_on"],
        "delivery_note": note,
        "delivery_line": line,
        "receipt_key": receipt_key(issuer, note, line),
        "packs": packs,
        # A lot of a critical reagent needs Lot QA's release, as its item says when it's
        # received. Changing the item later leaves this lot as it was received.
        "requires_qa_release": catalogue.get("release_requirement") == "required",
    }
    if params.get("expires_on"):
        values["expires_on"] = params["expires_on"]
    if params.get("certificate"):
        values["certificates"] = [params["certificate"]]
    # Each pack contributes its catalogue size. Keep the number in its stock unit as
    # an exact decimal all the way to the ledger. A tube's capacity is not its stock unit.
    receipt: dict[str, Any] = {
        "amount": {"unit": unit, "value": format(size * packs, "f")},
        "reason": f"{reason} {note}, line {line}.",
    }
    if position := (params.get("position") or "").strip():
        receipt["position"] = position
    return {"type": PREFIX + LOT_TYPES[unit], "values": values, "receipt": receipt}


def receive_delivery(run: dict[str, Any]) -> dict[str, Any]:
    order = run["inputs"]["order"][0]
    values = order["values"]
    item = order["included"]["item"][0]
    params = run["params"]
    packs = params["packs"]
    received = values.get("received_packs") or 0
    outstanding = values["ordered_packs"] - received
    if packs <= 0 or packs > outstanding:
        raise ValueError(f"Receive a positive number of packs up to the {outstanding} outstanding.")
    supplier = item["values"]["supplier"]
    reason = f"Received for {values['order_reference']}, delivery"
    lot = stock_lot(item, params, packs, supplier, reason)
    lot["values"].update(order_line=order["id"], received_as="order")
    lot["derived_from"] = [order["id"]]
    return {
        "updates": {
            "order": [
                {
                    "entity": order["id"],
                    "require_unchanged": True,
                    "values": {"received_packs": received + packs},
                }
            ]
        },
        "creates": [lot],
    }


RECEIVED_AS = ("delivery", "transfer", "opening_balance")


def receive_stock(run: dict[str, Any]) -> dict[str, Any]:
    """Receive stock that no reorder line in Iki answers, if the lab's procedure allows it."""
    if run.get("settings", {}).get("receipts_without_order", "allowed") != "allowed":
        raise ValueError(
            "This lab receives stock only against a reorder line. Add one, then receive the "
            "delivery against it."
        )
    item = run["inputs"]["item"][0]
    params = run["params"]
    kind = params.get("received_as") or "delivery"
    if kind not in RECEIVED_AS:
        raise ValueError("Choose a delivery, a transfer or opening stock.")
    sender = (params.get("received_from") or "").strip()
    if kind == "transfer" and not sender:
        raise ValueError("Enter the lab, site or organization the transfer came from.")
    if kind != "transfer" and sender:
        raise ValueError("Only a transfer names who it was received from.")
    # A supplier's delivery note is the supplier's whether or not it was ordered. A transfer
    # form is its sender's; a count sheet is the lab's own.
    issuer = {"delivery": item["values"]["supplier"], "transfer": sender}.get(kind, "")
    reason = {
        "delivery": "Received without an order, delivery",
        "transfer": f"Transferred from {sender}, document",
        "opening_balance": "Opening stock, count",
    }[kind]
    lot = stock_lot(item, params, params["packs"], issuer, reason)
    lot["values"]["received_as"] = kind
    if sender:
        lot["values"]["received_from"] = sender
    lot["derived_from"] = [item["id"]]
    return {"creates": [lot]}


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    action = {"order": place_order, "receive": receive_delivery, "receive_stock": receive_stock}[
        sys.argv[1]
    ]
    try:
        output = action(run)
    except ValueError as error:
        print(f"::error {error}", flush=True)
        raise SystemExit(1) from error
    document = {"apiVersion": "iki/v1", "kind": "RunOutput", **output}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(document), encoding="utf-8")


if __name__ == "__main__":
    main()
