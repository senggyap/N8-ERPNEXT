from __future__ import annotations

import json
import math
from typing import Any, Dict, Optional

import frappe
from frappe.utils import flt, getdate

from nkt_operations.nkt_store_operations.doctype.nkt_vehicle.nkt_vehicle import normalize_plate
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import NKTIdempotencyConflict

FOUNDATION_VERSION = "C15C.10H-R2"
FAMILY = "NKT Supplier Receiving Physical Intent"
ACTION = "record_physical_supplier_receiving"
MOVEMENT_TYPE = "External Supplier Arrival"
TOLERANCE = 0.000001
MAX_ROWS = 200

CONDITION_CLASSIFICATIONS = {
    "Normal",
    "Damaged",
    "Wet",
    "Broken Packaging",
    "Mixed Damage",
    "Other",
}

ALLOWED_TOP_LEVEL_KEYS = {
    "movement_type",
    "purchase_order",
    "company",
    "supplier",
    "receiving_date",
    "receiving_warehouse",
    "bill_of_lading_no",
    "supplier_dr_no",
    "supplier_delivery_reference",
    "delivery_vehicle",
    "internal_vehicle_no",
    "plate_number",
    "driver_name",
    "receiving_notes",
    "client_observed_at",
    "client_ui_version",
    "items",
    "total_expected_qty",
    "total_delivered_qty",
    "total_accepted_qty",
    "total_damaged_qty",
    "total_other_rejected_qty",
    "total_rejected_qty",
    "total_shortage_qty",
    "total_overdelivery_qty",
}

ALLOWED_ITEM_KEYS = {
    "line_no",
    "purchase_order_item",
    "item_code",
    "item_name",
    "uom",
    "expected_qty",
    "delivered_qty",
    "accepted_qty",
    "damaged_qty",
    "other_rejected_qty",
    "rejected_qty",
    "shortage_qty",
    "overdelivery_qty",
    "rejected_warehouse",
    "condition_classification",
    "condition_reason",
}

FORBIDDEN_MONEY_SIDE_KEYS = {
    "rate",
    "price",
    "amount",
    "base_rate",
    "base_amount",
    "payable",
    "payment",
    "payment_method",
    "check_no",
    "check_date",
    "margin",
    "gross_claim_amount",
    "claimed_amount",
    "agreed_deduction_amount",
    "supplier_claimable_qty",
    "responsibility",
    "claim_status",
    "supplier_soa",
    "purchase_invoice",
    "gl_entry",
    "account",
}


def _text(value: Any, label: str, max_length: int) -> str:
    value = str(value or "").strip()
    if not value:
        raise frappe.ValidationError(f"{label} is required.")
    if len(value) > max_length:
        raise frappe.ValidationError(f"{label} is too long.")
    return value


def _optional_text(value: Any, max_length: int) -> Optional[str]:
    value = str(value or "").strip()
    if not value:
        return None
    if len(value) > max_length:
        raise frappe.ValidationError("Supplier Receiving text value is too long.")
    return value


def _quantity(value: Any, label: str, *, positive: bool = False) -> float:
    try:
        number = float(flt(value))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} must be a number.") from exc
    if not math.isfinite(number):
        raise frappe.ValidationError(f"{label} must be finite.")
    if number < -TOLERANCE:
        raise frappe.ValidationError(f"{label} cannot be negative.")
    if positive and number <= TOLERANCE:
        raise frappe.ValidationError(f"{label} must be greater than zero.")
    if abs(number) <= TOLERANCE:
        number = 0.0
    return float(f"{number:.6f}")


def _assert_if_supplied(raw: Dict[str, Any], field: str, expected: float, label: str) -> None:
    if field not in raw:
        return
    supplied = _quantity(raw.get(field), label)
    if abs(supplied - expected) > TOLERANCE:
        raise NKTIdempotencyConflict(
            f"{label} conflicts with immutable physical quantities."
        )


def _normalize_item(raw: Dict[str, Any], idx: int) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise frappe.ValidationError(f"Supplier Receiving row {idx} must be an object.")

    forbidden = set(raw) & FORBIDDEN_MONEY_SIDE_KEYS
    if forbidden:
        raise frappe.ValidationError(
            f"Supplier Receiving row {idx} contains money-side fields that are not permitted offline."
        )

    extra = set(raw) - ALLOWED_ITEM_KEYS
    if extra:
        raise frappe.ValidationError(
            f"Supplier Receiving row {idx} contains unsupported fields."
        )

    if "line_no" in raw and int(raw.get("line_no") or 0) != idx:
        raise frappe.ValidationError(
            f"Supplier Receiving row {idx} has a mismatched line number."
        )

    expected = _quantity(raw.get("expected_qty"), f"Expected Quantity on row {idx}", positive=True)
    delivered = _quantity(raw.get("delivered_qty"), f"Bags Received on row {idx}")
    damaged = _quantity(raw.get("damaged_qty"), f"Damaged / Problem Bags on row {idx}")
    other_rejected = _quantity(
        raw.get("other_rejected_qty"), f"Other Rejected Quantity on row {idx}"
    )
    rejected = float(f"{damaged + other_rejected:.6f}")

    if rejected - delivered > TOLERANCE:
        raise frappe.ValidationError(
            f"Problem Bags cannot exceed Bags Received on row {idx}."
        )

    accepted = float(f"{max(delivered - rejected, 0.0):.6f}")
    shortage = float(f"{max(expected - delivered, 0.0):.6f}")
    overdelivery = float(f"{max(delivered - expected, 0.0):.6f}")

    _assert_if_supplied(raw, "accepted_qty", accepted, f"Accepted Quantity on row {idx}")
    _assert_if_supplied(raw, "rejected_qty", rejected, f"Rejected Quantity on row {idx}")
    _assert_if_supplied(raw, "shortage_qty", shortage, f"Shortage Quantity on row {idx}")
    _assert_if_supplied(raw, "overdelivery_qty", overdelivery, f"Over-delivery Quantity on row {idx}")

    # The accepted C9 canonical bridge explicitly blocks over-delivery. The offline
    # contract must not accept a physical event that cannot later be materialized.
    if overdelivery > TOLERANCE:
        raise frappe.ValidationError(
            f"Over-delivery is not supported by the accepted Supplier Receiving engine on row {idx}."
        )

    rejected_warehouse = _optional_text(raw.get("rejected_warehouse"), 180)
    classification = str(raw.get("condition_classification") or "").strip()
    reason = _optional_text(raw.get("condition_reason"), 500)

    if rejected > TOLERANCE:
        if not rejected_warehouse:
            raise frappe.ValidationError(
                f"Problem-Bag Holding Warehouse is required on row {idx}."
            )
        if not classification or classification == "Normal":
            raise frappe.ValidationError(
                f"Problem Type is required on row {idx}."
            )
        if classification not in CONDITION_CLASSIFICATIONS:
            raise frappe.ValidationError(
                f"Problem Type is invalid on row {idx}."
            )
    else:
        rejected_warehouse = None
        classification = "Normal"
        reason = None

    return {
        "line_no": idx,
        "purchase_order_item": _text(
            raw.get("purchase_order_item"), f"Purchase Order Item on row {idx}", 140
        ),
        "item_code": _text(raw.get("item_code"), f"Item on row {idx}", 140),
        "item_name": _optional_text(raw.get("item_name"), 240),
        "uom": _text(raw.get("uom"), f"UOM on row {idx}", 80),
        "expected_qty": expected,
        "delivered_qty": delivered,
        "accepted_qty": accepted,
        "damaged_qty": damaged,
        "other_rejected_qty": other_rejected,
        "rejected_qty": rejected,
        "shortage_qty": shortage,
        "overdelivery_qty": overdelivery,
        "rejected_warehouse": rejected_warehouse,
        "condition_classification": classification,
        "condition_reason": reason,
    }


def normalize_supplier_receiving_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise frappe.ValidationError("Supplier Receiving Physical Intent payload must be an object.")

    forbidden = set(payload) & FORBIDDEN_MONEY_SIDE_KEYS
    if forbidden:
        raise frappe.ValidationError(
            "Supplier Receiving Physical Intent contains money-side fields that are not permitted offline."
        )

    extra = set(payload) - ALLOWED_TOP_LEVEL_KEYS
    if extra:
        raise frappe.ValidationError(
            "Supplier Receiving Physical Intent contains unsupported fields."
        )

    movement_type = str(payload.get("movement_type") or MOVEMENT_TYPE).strip()
    if movement_type != MOVEMENT_TYPE:
        raise frappe.ValidationError(
            "Supplier Receiving Physical Intent can only represent External Supplier Arrival."
        )

    bill_of_lading_no = _optional_text(payload.get("bill_of_lading_no"), 180)
    supplier_dr_no = _optional_text(payload.get("supplier_dr_no"), 180)
    supplier_delivery_reference = _optional_text(
        payload.get("supplier_delivery_reference"), 180
    )
    if not (bill_of_lading_no or supplier_dr_no or supplier_delivery_reference):
        raise frappe.ValidationError(
            "At least one supplier delivery reference is required: BL No., Supplier DR No., or Other Supplier Delivery Reference."
        )

    delivery_vehicle = _optional_text(payload.get("delivery_vehicle"), 180)
    internal_vehicle_no = _optional_text(payload.get("internal_vehicle_no"), 120)
    plate_number = normalize_plate(payload.get("plate_number")) or None
    if not (delivery_vehicle or internal_vehicle_no or plate_number):
        raise frappe.ValidationError(
            "Enter at least one vehicle identifier: Vehicle, Plate Number, or Internal Van / Truck No."
        )

    rows = payload.get("items")
    if not isinstance(rows, list) or not rows:
        raise frappe.ValidationError("At least one Supplier Receiving row is required.")
    if len(rows) > MAX_ROWS:
        raise frappe.ValidationError("Supplier Receiving Physical Intent has too many rows.")

    items = [_normalize_item(row, idx) for idx, row in enumerate(rows, start=1)]

    po_rows = [row["purchase_order_item"] for row in items]
    if len(set(po_rows)) != len(po_rows):
        raise frappe.ValidationError(
            "A Purchase Order item cannot appear more than once in one Supplier Receiving intent."
        )

    receiving_warehouse = _text(
        payload.get("receiving_warehouse"), "Receiving Warehouse", 180
    )
    for row in items:
        if (
            row["rejected_qty"] > TOLERANCE
            and row["rejected_warehouse"] == receiving_warehouse
        ):
            raise frappe.ValidationError(
                f"Problem Bags on row {row['line_no']} must use a separate damage/inspection warehouse."
            )

    totals = {
        "total_expected_qty": float(f"{sum(x['expected_qty'] for x in items):.6f}"),
        "total_delivered_qty": float(f"{sum(x['delivered_qty'] for x in items):.6f}"),
        "total_accepted_qty": float(f"{sum(x['accepted_qty'] for x in items):.6f}"),
        "total_damaged_qty": float(f"{sum(x['damaged_qty'] for x in items):.6f}"),
        "total_other_rejected_qty": float(
            f"{sum(x['other_rejected_qty'] for x in items):.6f}"
        ),
        "total_rejected_qty": float(f"{sum(x['rejected_qty'] for x in items):.6f}"),
        "total_shortage_qty": float(f"{sum(x['shortage_qty'] for x in items):.6f}"),
        "total_overdelivery_qty": float(
            f"{sum(x['overdelivery_qty'] for x in items):.6f}"
        ),
    }

    for field, expected in totals.items():
        _assert_if_supplied(payload, field, expected, field.replace("_", " ").title())

    if totals["total_delivered_qty"] <= TOLERANCE:
        raise frappe.ValidationError(
            "Supplier Receiving must contain a positive physically received quantity."
        )

    return {
        "movement_type": MOVEMENT_TYPE,
        "purchase_order": _text(payload.get("purchase_order"), "Purchase Order", 140),
        "company": _text(payload.get("company"), "Company", 180),
        "supplier": _text(payload.get("supplier"), "Supplier", 180),
        "receiving_date": str(
            getdate(_text(payload.get("receiving_date"), "Receiving Date", 20))
        ),
        "receiving_warehouse": receiving_warehouse,
        "bill_of_lading_no": bill_of_lading_no,
        "supplier_dr_no": supplier_dr_no,
        "supplier_delivery_reference": supplier_delivery_reference,
        "delivery_vehicle": delivery_vehicle,
        "internal_vehicle_no": internal_vehicle_no,
        "plate_number": plate_number,
        "driver_name": _optional_text(payload.get("driver_name"), 180),
        "receiving_notes": _optional_text(payload.get("receiving_notes"), 1000),
        "client_observed_at": _text(
            payload.get("client_observed_at"), "Client observed time", 80
        ),
        "client_ui_version": _optional_text(payload.get("client_ui_version"), 120),
        "items": items,
        **totals,
    }


def canonical_supplier_receiving_payload_json(payload: Dict[str, Any]) -> str:
    return json.dumps(
        normalize_supplier_receiving_payload(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def foundation_status() -> Dict[str, Any]:
    return {
        "foundation_version": FOUNDATION_VERSION,
        "event_family": FAMILY,
        "event_action": ACTION,
        "movement_type": MOVEMENT_TYPE,
        "scope": "Physical Supplier Receiving intent contract only",
        "edge_acceptance_enabled": False,
        "transport_registered": False,
        "primary_receipt_contract_enabled": False,
        "primary_materialization_enabled": False,
        "edge_stock_projection_enabled": False,
        "supplier_money_side_offline_enabled": False,
        "supplier_claim_creation_at_edge": False,
        "supplier_soa_creation_at_edge": False,
        "ap_or_payment_creation_at_edge": False,
        "supporting_binary_attachment_transport_enabled": False,
        "bpi_sample_release_included": False,
        "overdelivery_enabled": False,
    }
