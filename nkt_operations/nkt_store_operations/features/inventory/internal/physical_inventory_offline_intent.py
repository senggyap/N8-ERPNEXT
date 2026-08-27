from __future__ import annotations

import json
import math
from datetime import date, datetime
from typing import Any, Dict, Iterable
from zoneinfo import ZoneInfo

import frappe

FOUNDATION_VERSION = "C15C.10J-R2"

FAMILY = "NKT Physical Inventory Count Intent"
ACTION = "record_physical_inventory_count"

OPERATIONAL_ROLES = {
    "NKT Encoder",
    "NKT Warehouse",
    "NKT ADMINISTRATOR",
    "NKT OWNER",
}

COUNT_REASONS = {
    "Cycle Count",
    "Spot Count",
    "Warehouse Recount",
    "Store Recount",
    "Damage / Handling Check",
    "Other",
}

PH_TZ = ZoneInfo("Asia/Manila")
TOLERANCE = 0.000001

# C8 remains the authoritative online Physical Inventory / Stock Reconciliation
# workflow. C15C.10J R2 only preserves immutable operator-observed count facts.
PRIMARY_STOCK_ADJUSTMENT_AUTHORITATIVE = True
CANONICAL_POSTING_AT_EDGE_ENABLED = False
EDGE_STOCK_LEDGER_ENABLED = False
EDGE_SYSTEM_QTY_IS_CANONICAL = False
PHYSICAL_OBSERVATION_TIME_PRESERVED = True
EMPLOYEE_MANUAL_BACKDATE_ENABLED = False

# Discovery proved C8 refreshes book/system quantity again immediately before
# authoritative Stock Reconciliation. Therefore system/book quantity, variance,
# valuation and posting results are server-owned, not immutable Edge facts.
FORBIDDEN_SERVER_EFFECT_KEYS = {
    "name",
    "docstatus",
    "snapshot_datetime",
    "system_qty_snapshot",
    "system_qty",
    "book_qty",
    "actual_qty",
    "available_qty",
    "variance_qty",
    "variance_direction",
    "variance_line_count",
    "valuation_rate_snapshot",
    "valuation_rate",
    "valuation_rate_source",
    "posted_qty",
    "adjustment_status",
    "review_status",
    "blockers",
    "stock_reconciliation",
    "stock_reconciliation_name",
    "stock_ledger_entry",
    "stock_ledger_entries",
    "sle",
    "posting_date",
    "posting_time",
    "set_posting_time",
    "posted_by",
    "posted_on",
    "accountability_classification",
    "accountability_notes",
    "reviewed_by",
    "reviewed_on",
    "review_notes",
    "review_lock",
    "canonical_qty",
    "canonical_variance",
    "canonical_posting_result",
}

# Generic quantity-only C8 posting already blocks serialized/batched inventory.
# R2 does not invent a new offline serial/batch workflow.
FORBIDDEN_SERIAL_BATCH_KEYS = {
    "batch_no",
    "batch",
    "serial_no",
    "serial_numbers",
    "serial_and_batch_bundle",
    "serial_and_batch_entry",
}

TOP_LEVEL_ALLOWED_KEYS = {
    "family",
    "action",
    "submit_request_id",
    "company",
    "warehouse",
    "business_date",
    "count_datetime",
    "counted_by",
    "entry_role",
    "count_reason",
    "physical_count_reference",
    "operator_notes",
    "physical_count_confirmed",
    "items",
}

ROW_ALLOWED_KEYS = {
    "line_no",
    "item_code",
    "physical_qty",
    "physical_qty_confirmed",
    "row_notes",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _strict_bool(value: Any, label: str) -> bool:
    if value is True or value == 1:
        return True
    if value is False or value == 0 or value in (None, ""):
        return False
    raise frappe.ValidationError(f"{label} must be a true/false confirmation.")


def _number(value: Any, label: str, *, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} must be numeric.") from exc
    if not math.isfinite(number):
        raise frappe.ValidationError(f"{label} must be a finite number.")
    if number < minimum - TOLERANCE:
        raise frappe.ValidationError(f"{label} cannot be less than {minimum}.")
    return round(number, 6)


def _date(value: Any, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = _text(value)
    try:
        return date.fromisoformat(raw)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _manila_datetime(value: Any, label: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = _text(value)
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except Exception as exc:
            raise frappe.ValidationError(f"{label} is invalid.") from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=PH_TZ)
    return dt.astimezone(PH_TZ)


def _request_id(value: Any) -> str:
    raw = _text(value)
    if not raw:
        raise frappe.ValidationError("Submit Request ID is required.")
    if len(raw) > 140:
        raise frappe.ValidationError("Submit Request ID is invalid.")
    return raw


def _reject_unknown_keys(value: Dict[str, Any], allowed: Iterable[str], label: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise frappe.ValidationError(
            f"{label} contains unsupported/client-owned fields: {', '.join(unknown)}."
        )


def _reject_server_owned_keys(payload: Dict[str, Any]) -> None:
    bad = sorted(set(payload) & FORBIDDEN_SERVER_EFFECT_KEYS)
    if bad:
        raise frappe.ValidationError(
            "Physical Inventory count intent cannot author canonical stock/posting fields: "
            + ", ".join(bad)
            + "."
        )
    serial_batch = sorted(set(payload) & FORBIDDEN_SERIAL_BATCH_KEYS)
    if serial_batch:
        raise frappe.ValidationError(
            "Generic offline Physical Inventory count intent does not accept serial/batch posting fields: "
            + ", ".join(serial_batch)
            + "."
        )


def _normalize_item(raw: Dict[str, Any], idx: int) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise frappe.ValidationError(f"Counted Item row {idx} is invalid.")

    _reject_server_owned_keys(raw)
    _reject_unknown_keys(raw, ROW_ALLOWED_KEYS, f"Counted Item row {idx}")

    supplied_line = raw.get("line_no")
    if supplied_line not in (None, "", idx):
        try:
            supplied_line = int(supplied_line)
        except Exception as exc:
            raise frappe.ValidationError(
                f"Counted Item row {idx} Line No is invalid."
            ) from exc
        if supplied_line != idx:
            raise frappe.ValidationError(
                f"Counted Item row {idx} Line No does not match its canonical position."
            )

    item_code = _text(raw.get("item_code"))
    if not item_code:
        raise frappe.ValidationError(f"Counted Item row {idx} requires Item.")

    physical_qty = _number(
        raw.get("physical_qty"),
        f"Counted Item row {idx} Physical Qty",
        minimum=0,
    )

    if not _strict_bool(
        raw.get("physical_qty_confirmed"),
        f"Counted Item row {idx} Physical Quantity Counted",
    ):
        raise frappe.ValidationError(
            f"Counted Item row {idx} must be confirmed as physically counted."
        )

    return {
        "line_no": idx,
        "item_code": item_code,
        "physical_qty": physical_qty,
        "physical_qty_confirmed": True,
        "row_notes": _text(raw.get("row_notes")),
    }


def normalize_physical_inventory_count_intent(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise frappe.ValidationError("Physical Inventory count intent payload is invalid.")

    _reject_server_owned_keys(payload)
    _reject_unknown_keys(payload, TOP_LEVEL_ALLOWED_KEYS, "Physical Inventory count intent")

    payload_family = _text(payload.get("family"))
    if payload_family and payload_family != FAMILY:
        raise frappe.ValidationError(
            "Physical Inventory Family does not match the locked offline event family."
        )

    payload_action = _text(payload.get("action"))
    if payload_action and payload_action != ACTION:
        raise frappe.ValidationError(
            "Physical Inventory Action does not match the locked offline event action."
        )

    company = _text(payload.get("company"))
    warehouse = _text(payload.get("warehouse"))
    counted_by = _text(payload.get("counted_by"))
    entry_role = _text(payload.get("entry_role"))

    for label, value in (
        ("Company", company),
        ("Warehouse", warehouse),
        ("Counted / Entered By", counted_by),
        ("Entry Role", entry_role),
    ):
        if not value:
            raise frappe.ValidationError(f"{label} is required.")

    if entry_role not in OPERATIONAL_ROLES:
        raise frappe.ValidationError(
            "Physical Inventory offline count may only be authored by the accepted "
            "NKT Encoder, NKT Warehouse, NKT ADMINISTRATOR, or NKT OWNER roles."
        )

    business_date = _date(payload.get("business_date"), "Business Date")
    count_dt = _manila_datetime(payload.get("count_datetime"), "Physical Count Time")

    # The Edge business date is an automatically captured physical-observation
    # fact. It is not a user backdate selector. Delayed transport is allowed to
    # preserve this fact, but R2 does NOT authorize a historical Stock
    # Reconciliation; Primary materialization policy is intentionally later.
    if count_dt.date() != business_date:
        raise frappe.ValidationError(
            "Physical Inventory Business Date must equal the true Store-Edge physical count date."
        )

    count_reason = _text(payload.get("count_reason"))
    if count_reason not in COUNT_REASONS:
        raise frappe.ValidationError("A valid Physical Inventory Count Reason is required.")

    operator_notes = _text(payload.get("operator_notes"))
    if count_reason == "Other" and not operator_notes:
        raise frappe.ValidationError(
            "Operator Notes are required when Count Reason is Other."
        )

    if not _strict_bool(
        payload.get("physical_count_confirmed"),
        "Physical Count Reflects Actual Stock",
    ):
        raise frappe.ValidationError(
            "Confirm that the physical count reflects actual observed stock."
        )

    items_raw = payload.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        raise frappe.ValidationError("At least one physically counted item is required.")

    items = []
    seen = set()
    for idx, raw in enumerate(items_raw, start=1):
        row = _normalize_item(raw, idx)
        if row["item_code"] in seen:
            raise frappe.ValidationError(
                f"Item {row['item_code']} appears more than once in this physical count."
            )
        seen.add(row["item_code"])
        items.append(row)

    return {
        "family": FAMILY,
        "action": ACTION,
        "submit_request_id": _request_id(payload.get("submit_request_id")),
        "company": company,
        "warehouse": warehouse,
        "business_date": business_date.isoformat(),
        "count_datetime": count_dt.isoformat(),
        "counted_by": counted_by,
        "entry_role": entry_role,
        "count_reason": count_reason,
        "physical_count_reference": _text(payload.get("physical_count_reference")),
        "operator_notes": operator_notes,
        "physical_count_confirmed": True,
        "items": items,
    }


def canonical_physical_inventory_count_intent_json(payload: Dict[str, Any]) -> str:
    return json.dumps(
        normalize_physical_inventory_count_intent(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def foundation_status() -> Dict[str, Any]:
    return {
        "foundation_version": FOUNDATION_VERSION,
        "event_family": FAMILY,
        "event_action": ACTION,
        "accepted_operational_roles": sorted(OPERATIONAL_ROLES),
        "primary_stock_adjustment_authoritative": PRIMARY_STOCK_ADJUSTMENT_AUTHORITATIVE,
        "canonical_posting_at_edge_enabled": CANONICAL_POSTING_AT_EDGE_ENABLED,
        "edge_stock_ledger_enabled": EDGE_STOCK_LEDGER_ENABLED,
        "edge_system_qty_is_canonical": EDGE_SYSTEM_QTY_IS_CANONICAL,
        "physical_observation_time_preserved": PHYSICAL_OBSERVATION_TIME_PRESERVED,
        "employee_manual_backdate_enabled": EMPLOYEE_MANUAL_BACKDATE_ENABLED,
        "zero_physical_quantity_supported": True,
        "serialized_batched_generic_offline_posting_supported": False,
        "count_sheet_attachment_transport_decided": False,
        "print_export_outage_requirement_decided": False,
        "delayed_or_stale_count_auto_adjustment_decided": False,
        "delayed_or_stale_count_policy": (
            "Deferred to the Primary contract. R2 preserves the physical observation "
            "but does not authorize a historical/current Stock Reconciliation from it."
        ),
        "intervening_stock_movement_policy_decided": False,
        "canonical_system_snapshot_policy": (
            "Primary-owned. C8 refreshes current system quantity immediately before posting."
        ),
    }
