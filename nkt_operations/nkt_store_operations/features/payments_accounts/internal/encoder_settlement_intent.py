from __future__ import annotations

import json
import math
import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import flt, getdate, now

from nkt_operations.nkt_store_operations.features.payments_accounts.card_surcharge import (
    apply_payment_row_card_fields,
    normalize_payment_method,
)
from nkt_operations.nkt_store_operations.features.sales.customer_order_intent import (
    ORDER_INTENT_FAMILY,
    _canonical_order_intent_json,
    _normalize_order_intent_payload,
)
from nkt_operations.nkt_store_operations.features.offline_edge.policy import (
    device_policy_snapshot,
    event_family_policy,
    manila_now,
    validate_business_time,
)
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    begin_event,
    canonical_payload_hash,
    mark_edge_accepted,
)

FOUNDATION_VERSION = "C15C.10D-R2"
PH_TZ = ZoneInfo("Asia/Manila")

ENCODER_SETTLEMENT_FAMILY = "NKT Encoder Settlement Intent"
ENCODER_SETTLEMENT_ACTION = "Capture Encoder Settlement Intent"

ENCODER_ROLES = {
    "NKT Encoder",
    "NKT OWNER",
    "NKT ADMINISTRATOR",
    "System Manager",
}

PAYMENT_METHODS = {
    "Cash",
    "Check",
    "GCash",
    "Maya",
    "Card",
    "Bank Transfer",
    "Online",
    "Account",
}
REFERENCE_METHODS = {"GCash", "Maya", "Card", "Bank Transfer", "Online"}
TOLERANCE = 0.005

ALLOWED_TOP_LEVEL_KEYS = {
    "order_event_uuid",
    "company",
    "customer",
    "client_observed_at",
    "client_ui_version",
    "payments",
    "merchandise_total",
    "declared_payment_total",
    "declared_account",
    "declared_card_surcharge_total",
    "declared_total_collected",
}
ALLOWED_PAYMENT_KEYS = {
    "line_no",
    "method",
    "payment_method",
    "amount",
    "cash_tendered",
    "change_amount",
    "reference",
    "reference_number",
    "provider",
    "bank_or_provider",
    "check_number",
    "check_date",
    "remarks",
    "card_surcharge",
    "collected_amount",
}


def _runtime_role() -> str:
    return str(frappe.conf.get("nkt_runtime_role") or "Primary").strip()


def _session_user(user: Optional[str] = None) -> str:
    user = user or frappe.session.user
    if not user or user == "Guest":
        raise frappe.PermissionError("Local Encoder settlement entry unavailable.")
    return user


def _require_encoder_authority(user: str) -> None:
    if not (set(frappe.get_roles(user) or []) & ENCODER_ROLES):
        raise frappe.PermissionError("Local Encoder settlement entry unavailable.")


def _uuid(value: Any, label: str = "Transaction identity") -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _text(value: Any, label: str, max_len: int = 240) -> str:
    out = str(value or "").strip()
    if not out:
        raise frappe.ValidationError(f"{label} is required.")
    if len(out) > max_len:
        raise frappe.ValidationError(f"{label} is too long.")
    return out


def _optional_text(value: Any, max_len: int = 500) -> str:
    out = str(value or "").strip()
    if len(out) > max_len:
        raise frappe.ValidationError("Encoder settlement text is too long.")
    return out


def _positive(value: Any, label: str) -> float:
    try:
        out = float(value)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} must be numeric.") from exc
    if not math.isfinite(out) or out <= 0:
        raise frappe.ValidationError(f"{label} must be greater than zero.")
    return float(f"{out:.6f}")


def _money_equal(left: Any, right: Any, tolerance: float = 0.01) -> bool:
    return abs(flt(left) - flt(right)) <= tolerance


def _manila_sql_datetime(value: Any, label: str) -> str:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except Exception as exc:
            raise frappe.ValidationError(f"{label} is not a valid datetime.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PH_TZ)
    else:
        dt = dt.astimezone(PH_TZ)
    return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_ref(value: Any) -> str:
    return "".join(str(value or "").strip().lower().split())


def _normalize_provider(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _validate_derived(raw: Dict[str, Any], key: str, calculated: float, label: str) -> None:
    if key in raw and not _money_equal(raw.get(key), calculated):
        raise NKTIdempotencyConflict(f"{label} conflicts with recalculated Encoder declaration truth.")


def _normalize_payment(raw: Dict[str, Any], idx: int) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise frappe.ValidationError(f"Encoder payment row {idx} must be an object.")
    extra = set(raw) - ALLOWED_PAYMENT_KEYS
    if extra:
        raise frappe.ValidationError(f"Encoder payment row {idx} contains unsupported fields.")
    if "line_no" in raw and int(raw.get("line_no") or 0) != idx:
        raise frappe.ValidationError(
            f"Encoder payment row {idx} has a mismatched canonical line number."
        )

    method = normalize_payment_method(raw.get("method") or raw.get("payment_method"))
    if method not in PAYMENT_METHODS:
        raise frappe.ValidationError(
            f"Unsupported Encoder payment method on row {idx}: {method or '(blank)'}."
        )
    amount = _positive(raw.get("amount"), f"Encoder payment amount on row {idx}")
    reference = str(raw.get("reference") or raw.get("reference_number") or "").strip()
    provider = str(raw.get("provider") or raw.get("bank_or_provider") or "").strip()
    check_number = str(raw.get("check_number") or reference or "").strip()
    check_date = str(raw.get("check_date") or "").strip()
    remarks = _optional_text(raw.get("remarks"), 500)

    card_row = {"payment_method": method, "amount": amount}
    card = apply_payment_row_card_fields(card_row)
    surcharge = flt(card["card_surcharge"])
    collected = flt(card["collected_amount"])

    if method == "Cash":
        raw_tendered = flt(raw.get("cash_tendered"))
        raw_change = flt(raw.get("change_amount"))
        if abs(raw_change) > TOLERANCE:
            raise frappe.ValidationError(
                "Encoder Cash declaration cannot carry Cashier change."
            )
        if raw_tendered > TOLERANCE and not _money_equal(raw_tendered, amount):
            raise frappe.ValidationError(
                "Encoder Cash declaration cannot claim a Cashier tendered amount."
            )
        # Encoder declares settlement classification only. It never owns drawer truth.
        cash_tendered = amount
        change_amount = 0.0
        reference = ""
        provider = ""
        check_number = ""
        check_date = ""
    else:
        if abs(flt(raw.get("cash_tendered"))) > TOLERANCE or abs(flt(raw.get("change_amount"))) > TOLERANCE:
            raise frappe.ValidationError(
                f"Only Cash may carry Encoder cash placeholders on row {idx}."
            )
        cash_tendered = 0.0
        change_amount = 0.0

    if method == "Check":
        if not check_number:
            raise frappe.ValidationError(f"Check Number is required on Encoder payment row {idx}.")
        if not check_date:
            raise frappe.ValidationError(f"Check Date is required on Encoder payment row {idx}.")
        if not provider:
            raise frappe.ValidationError(f"Issuing Bank is required on Encoder payment row {idx}.")
        reference = check_number
    elif method in REFERENCE_METHODS and not reference:
        raise frappe.ValidationError(
            f"Reference Number is required for {method} on Encoder payment row {idx}."
        )
    elif method == "Account":
        reference = ""
        provider = ""
        check_number = ""
        check_date = ""

    canonical_collected = 0.0 if method == "Account" else collected
    _validate_derived(raw, "card_surcharge", surcharge, f"Card surcharge on Encoder row {idx}")
    _validate_derived(raw, "collected_amount", canonical_collected, f"Collected amount on Encoder row {idx}")

    return {
        "line_no": idx,
        "payment_method": method,
        "amount": amount,
        "card_surcharge": surcharge,
        "collected_amount": canonical_collected,
        "cash_tendered": cash_tendered,
        "change_amount": change_amount,
        "reference_number": reference,
        "bank_or_provider": provider,
        "check_number": check_number,
        "check_date": check_date,
        "remarks": remarks,
    }


def _normalize_encoder_settlement_intent_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise frappe.ValidationError("Encoder Settlement Intent payload must be an object.")
    extra = set(payload) - ALLOWED_TOP_LEVEL_KEYS
    if extra:
        raise frappe.ValidationError("Encoder Settlement Intent contains unsupported fields.")

    order_event_uuid = _uuid(payload.get("order_event_uuid"), "Customer Order Event UUID")
    company = _text(payload.get("company"), "Company")
    customer = _text(payload.get("customer"), "Customer")
    observed = _text(payload.get("client_observed_at"), "Client observed time", 80)
    ui_version = _optional_text(payload.get("client_ui_version"), 120)
    merchandise_total = _positive(payload.get("merchandise_total"), "Merchandise total")

    raw_payments = payload.get("payments")
    if not isinstance(raw_payments, list) or not raw_payments:
        raise frappe.ValidationError("At least one Encoder declared-payment row is required.")
    if len(raw_payments) > 30:
        raise frappe.ValidationError("Encoder Settlement Intent has too many payment rows.")
    payments = [_normalize_payment(raw, idx) for idx, raw in enumerate(raw_payments, start=1)]

    if sum(1 for x in payments if x["payment_method"] == "Cash") > 1:
        raise frappe.ValidationError("Only one Encoder Cash declaration row is allowed.")

    seen_checks = set()
    for row in payments:
        if row["payment_method"] != "Check":
            continue
        key = (
            _normalize_provider(row["bank_or_provider"]),
            _normalize_ref(row["check_number"]),
        )
        if key in seen_checks:
            raise frappe.ValidationError(
                "The same physical Check cannot appear twice in one Encoder declaration."
            )
        seen_checks.add(key)

    declared_total = sum(flt(x["amount"]) for x in payments)
    declared_account = sum(
        flt(x["amount"]) for x in payments if x["payment_method"] == "Account"
    )
    surcharge_total = sum(flt(x["card_surcharge"]) for x in payments)
    collected_total = sum(flt(x["collected_amount"]) for x in payments)

    if abs(declared_total - merchandise_total) > 0.01:
        raise frappe.ValidationError(
            "Encoder declared-payment total must equal the merchandise total."
        )

    _validate_derived(payload, "declared_payment_total", declared_total, "Declared payment total")
    _validate_derived(payload, "declared_account", declared_account, "Declared Account principal")
    _validate_derived(
        payload,
        "declared_card_surcharge_total",
        surcharge_total,
        "Declared Card surcharge total",
    )
    _validate_derived(
        payload,
        "declared_total_collected",
        collected_total,
        "Declared collected total",
    )

    return {
        "order_event_uuid": order_event_uuid,
        "company": company,
        "customer": customer,
        "client_observed_at": observed,
        "client_ui_version": ui_version,
        "payments": payments,
        "merchandise_total": float(f"{merchandise_total:.6f}"),
        "declared_payment_total": float(f"{declared_total:.6f}"),
        "declared_account": float(f"{declared_account:.6f}"),
        "declared_card_surcharge_total": float(f"{surcharge_total:.6f}"),
        "declared_total_collected": float(f"{collected_total:.6f}"),
    }


def _canonical_encoder_settlement_intent_json(payload: Dict[str, Any]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _edge_order_context(order_event_uuid: str, *, device_id: str, user: str) -> Dict[str, Any]:
    if not frappe.db.exists("NKT Sync Event", order_event_uuid):
        raise frappe.DoesNotExistError(
            "The local Customer Order Intent is unavailable for this Encoder settlement."
        )
    event = frappe.get_doc("NKT Sync Event", order_event_uuid)
    if event.event_family != ORDER_INTENT_FAMILY:
        raise frappe.ValidationError(
            "Encoder settlement must reference a Customer Order Intent."
        )
    if str(event.origin_device or "") != str(device_id or ""):
        raise frappe.PermissionError(
            "This Encoder settlement belongs to another registered workstation."
        )
    if str(event.origin_user or "") != str(user or ""):
        raise frappe.PermissionError(
            "This Encoder settlement belongs to another Encoder identity."
        )
    if event.sync_state not in (
        "Accepted at Edge",
        "Awaiting Primary",
        "Committed at Primary",
    ):
        raise frappe.ValidationError(
            "The referenced Customer Order Intent is not eligible for settlement declaration."
        )

    pending = frappe.db.exists("NKT Sync Pending Payload", order_event_uuid)
    order_payload = None
    if pending:
        row = frappe.get_doc("NKT Sync Pending Payload", pending)
        if row.event_family != ORDER_INTENT_FAMILY:
            raise NKTIdempotencyConflict(
                "Customer Order pending payload family conflicts with immutable order identity."
            )
        try:
            raw = json.loads(row.payload_json or "{}")
        except Exception as exc:
            raise NKTIdempotencyConflict(
                "Customer Order pending payload is not valid JSON."
            ) from exc
        order_payload = _normalize_order_intent_payload(raw)
        if _canonical_order_intent_json(order_payload) != row.payload_json:
            raise NKTIdempotencyConflict(
                "Customer Order pending payload canonical form changed."
            )
        if canonical_payload_hash(order_payload) != event.payload_sha256:
            raise NKTIdempotencyConflict(
                "Customer Order pending payload no longer matches immutable order identity."
            )
    elif event.sync_state != "Committed at Primary":
        raise frappe.ValidationError(
            "Customer Order Intent exists without its durable pending payload."
        )

    return {"event": event, "order_payload": order_payload}


def accept_encoder_settlement_intent_at_edge(
    event_uuid: str,
    device_id: str,
    business_date: Any,
    settled_at: Any,
    payload: Dict[str, Any],
    *,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    """
    C15C.10D R2 foundation.

    This records only the Encoder's independent settlement/account declaration.
    It does NOT submit Customer Order, match Cashier/Encoder, create Payment
    Receipt/Cashier Movement, create Receivable/Advance, release stock, or move stock.
    """
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Local Encoder settlement entry unavailable.")

    user = _session_user(user)
    _require_encoder_authority(user)
    device = device_policy_snapshot(
        device_id,
        user=user,
        requested_context="NKT Retail",
    )
    if device.get("ui_mode") != "normal":
        raise frappe.PermissionError("Local Encoder settlement entry unavailable.")

    family = event_family_policy(ENCODER_SETTLEMENT_FAMILY)
    if family.get("offline_write_allowed") is not True:
        raise frappe.PermissionError("Local Encoder settlement entry unavailable.")

    business = validate_business_time(business_date, settled_at)
    normalized = _normalize_encoder_settlement_intent_payload(payload)
    order_ctx = _edge_order_context(
        normalized["order_event_uuid"],
        device_id=device_id,
        user=user,
    )
    order_event = order_ctx["event"]

    today = manila_now().date()
    if getdate(order_event.business_date) != today or getdate(business["business_date"]) != today:
        raise frappe.ValidationError(
            "Encoder Settlement Intent requires today's Customer Order Intent. "
            "NKT does not allow antedated or backdated frontline settlement."
        )
    if getdate(order_event.business_date) != getdate(business["business_date"]):
        raise frappe.ValidationError(
            "Encoder settlement Business Date must equal the Customer Order Intent Business Date."
        )

    order_payload = order_ctx["order_payload"]
    if order_payload is not None:
        if order_payload["company"] != normalized["company"]:
            raise frappe.ValidationError("Encoder settlement Company does not match the saved order.")
        if order_payload["customer"] != normalized["customer"]:
            raise frappe.ValidationError("Encoder settlement Customer does not match the saved order.")
        order_total = sum(flt(x["amount"]) for x in order_payload["items"])
        if not _money_equal(order_total, normalized["merchandise_total"]):
            raise NKTIdempotencyConflict(
                "Encoder settlement merchandise total conflicts with the immutable saved order."
            )

    event_uuid = _uuid(event_uuid, "Encoder Settlement Event UUID")
    digest = canonical_payload_hash(normalized)
    envelope = {
        "event_uuid": event_uuid,
        "event_family": ENCODER_SETTLEMENT_FAMILY,
        "event_action": ENCODER_SETTLEMENT_ACTION,
        "operational_context": device.get("operational_context") or "NKT Retail",
        "origin_device": device_id,
        "origin_user": user,
        "business_date": business["business_date"],
        "settled_at": _manila_sql_datetime(
            business["settled_at_manila"], "Business / Settled Time"
        ),
        "client_created_at": _manila_sql_datetime(
            normalized["client_observed_at"], "Client observed time"
        ),
        "payload_sha256": digest,
        "legacy_request_id": normalized["order_event_uuid"],
    }

    event, replay = begin_event(envelope)
    canonical = _canonical_encoder_settlement_intent_json(normalized)
    pending_name = frappe.db.exists("NKT Sync Pending Payload", event.event_uuid)

    if pending_name:
        pending = frappe.get_doc("NKT Sync Pending Payload", pending_name)
        if pending.event_family != ENCODER_SETTLEMENT_FAMILY:
            raise NKTIdempotencyConflict(
                "Pending payload family conflicts with immutable Encoder Settlement Intent."
            )
        if pending.payload_sha256 != digest or pending.payload_json != canonical:
            raise NKTIdempotencyConflict(
                "Pending payload conflicts with immutable Encoder Settlement Intent."
            )
        frappe.db.set_value(
            "NKT Sync Pending Payload",
            pending.name,
            {
                "attempt_count": int(pending.attempt_count or 0) + 1,
                "last_attempt_at": now(),
            },
            update_modified=False,
        )
    else:
        if replay and event.sync_state not in ("Committed at Primary", "Conflict", "Failed"):
            raise frappe.ValidationError(
                "Encoder Settlement Intent exists but its durable pending payload is unavailable."
            )
        if event.sync_state not in ("Committed at Primary", "Conflict", "Failed"):
            frappe.get_doc(
                {
                    "doctype": "NKT Sync Pending Payload",
                    "event_uuid": event.event_uuid,
                    "event_family": ENCODER_SETTLEMENT_FAMILY,
                    "payload_sha256": digest,
                    "payload_json": canonical,
                    "queue_state": "Accepted at Edge",
                    "edge_accepted_at": now(),
                    "attempt_count": 1,
                    "last_attempt_at": now(),
                }
            ).insert(ignore_permissions=True)

    if event.sync_state not in ("Committed at Primary", "Conflict", "Failed"):
        mark_edge_accepted(event.event_uuid)
        event.reload()

    return {
        "event_uuid": event.event_uuid,
        "event_family": ENCODER_SETTLEMENT_FAMILY,
        "sync_state": event.sync_state,
        "durable_ack": True,
        "replay": bool(replay),
        "payload_sha256": digest,
        "order_event_uuid": normalized["order_event_uuid"],
        "merchandise_total": normalized["merchandise_total"],
        "declared_payment_total": normalized["declared_payment_total"],
        "declared_account": normalized["declared_account"],
        "declared_card_surcharge_total": normalized["declared_card_surcharge_total"],
        "declared_total_collected": normalized["declared_total_collected"],
        "customer_order_submitted": False,
        "matching_executed": False,
        "payment_receipt_created": False,
        "cashier_movement_created": False,
        "receivable_created": False,
        "advance_applied": False,
        "warehouse_release_created": False,
        "stock_entry_created": False,
        "primary_settlement_materializer_required": True,
    }


@frappe.whitelist()
def submit_encoder_settlement_intent(
    event_uuid: str,
    device_id: str,
    business_date: str,
    settled_at: str,
    payload: Any,
):
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    return accept_encoder_settlement_intent_at_edge(
        event_uuid,
        device_id,
        business_date,
        settled_at,
        payload,
        user=_session_user(),
    )
