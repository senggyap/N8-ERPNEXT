from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import flt, getdate, now

from nkt_operations.nkt_store_operations.features.payments_accounts.card_surcharge import (
    apply_payment_row_card_fields,
    normalize_payment_method,
)
from nkt_operations.nkt_store_operations.features.payments_accounts.cash_ledger import validate_cashier_shift
from nkt_operations.nkt_store_operations.features.cashier.internal.cashier_shift_alias import is_edge_shift_reference, validate_edge_shift_for_money
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

FOUNDATION_VERSION = "C15C.10A-R4"
PH_TZ = ZoneInfo("Asia/Manila")

TENDER_INTENT_FAMILY = "NKT Cashier Tender Intent"
TENDER_INTENT_ACTION = "Capture Cashier Tender Intent"

CASHIER_ROLES = {
    "NKT Cashier",
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
    "Return Credit",
}
REFERENCE_METHODS = {"GCash", "Maya", "Card", "Bank Transfer", "Online"}
TOLERANCE = 0.005

ALLOWED_TOP_LEVEL_KEYS = {
    # NKT_MANAGER_PIN_TENDER_EVIDENCE_MP1
    "company",
    "customer",
    "cashier_shift",
    "settlement_location",
    "default_warehouse",
    "client_observed_at",
    "client_ui_version",
    "items",
    "payments",
    "price_authorization",
    # Derived canonical fields are accepted only when they recalculate exactly,
    # making normalization idempotent for durable transport validation.
    "merchandise_total",
    "payment_settlement_total",
    "card_surcharge_total",
    "actual_collected_total",
}
ALLOWED_ITEM_KEYS = {
    "line_no",
    "item_code",
    "item",
    "qty",
    "quantity",
    "rate",
    "final_rate",
    "warehouse",
    "source_warehouse",
    "amount",
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
    # Derived canonical fields.
    "card_surcharge",
    "collected_amount",
}

ALLOWED_PRICE_AUTHORIZATION_KEYS = {
    "authorized_by",
    "authorized_on",
    "reason",
    "explanation",
    "adjustment_signature",
    "authorization_runtime_role",
    "authorization_device_id",
}


def _runtime_role() -> str:
    return str(frappe.conf.get("nkt_runtime_role") or "Primary").strip()


def _session_user(user: Optional[str] = None) -> str:
    user = user or frappe.session.user
    if not user or user == "Guest":
        raise frappe.PermissionError("Local Cashier tender entry unavailable.")
    return user


def _require_cashier_authority(user: str) -> None:
    if not (set(frappe.get_roles(user) or []) & CASHIER_ROLES):
        raise frappe.PermissionError("Local Cashier tender entry unavailable.")


def _uuid(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(
            "Transaction identity is invalid. Refresh the Cashier Fast Screen and try again."
        ) from exc


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
        raise frappe.ValidationError("Tender-intent text is too long.")
    return out


def _positive(value: Any, label: str) -> float:
    try:
        out = float(value)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} must be numeric.") from exc
    if not math.isfinite(out) or out <= 0:
        raise frappe.ValidationError(f"{label} must be greater than zero.")
    return float(f"{out:.6f}")


def _nonnegative(value: Any, label: str) -> float:
    try:
        out = float(value or 0)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} must be numeric.") from exc
    if not math.isfinite(out) or out < 0:
        raise frappe.ValidationError(f"{label} must be zero or greater.")
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
        raise NKTIdempotencyConflict(f"{label} conflicts with recalculated tender truth.")


def _normalize_item(raw: Dict[str, Any], idx: int, default_warehouse: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise frappe.ValidationError(f"Item row {idx} must be an object.")
    extra = set(raw) - ALLOWED_ITEM_KEYS
    if extra:
        raise frappe.ValidationError(f"Item row {idx} contains unsupported fields.")
    if "line_no" in raw and int(raw.get("line_no") or 0) != idx:
        raise frappe.ValidationError(f"Item row {idx} has a mismatched canonical line number.")

    item_code = _text(raw.get("item_code") or raw.get("item"), f"Item on row {idx}")
    qty = _positive(
        raw.get("qty") if raw.get("qty") is not None else raw.get("quantity"),
        f"Quantity on row {idx}",
    )
    rate = _nonnegative(
        raw.get("rate") if raw.get("rate") is not None else raw.get("final_rate"),
        f"Rate on row {idx}",
    )
    warehouse = _text(
        raw.get("warehouse") or raw.get("source_warehouse") or default_warehouse,
        f"Warehouse on row {idx}",
    )
    amount = float(f"{qty * rate:.6f}")
    if "amount" in raw and not _money_equal(raw.get("amount"), amount):
        raise NKTIdempotencyConflict(
            f"Item amount on row {idx} conflicts with Quantity × Rate."
        )
    return {
        "line_no": idx,
        "item_code": item_code,
        "qty": qty,
        "rate": rate,
        "warehouse": warehouse,
        "amount": amount,
    }


def _normalize_payment(raw: Dict[str, Any], idx: int) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise frappe.ValidationError(f"Payment row {idx} must be an object.")
    extra = set(raw) - ALLOWED_PAYMENT_KEYS
    if extra:
        raise frappe.ValidationError(f"Payment row {idx} contains unsupported fields.")
    if "line_no" in raw and int(raw.get("line_no") or 0) != idx:
        raise frappe.ValidationError(
            f"Payment row {idx} has a mismatched canonical line number."
        )

    method = normalize_payment_method(raw.get("method") or raw.get("payment_method"))
    if method not in PAYMENT_METHODS:
        raise frappe.ValidationError(
            f"Unsupported payment method on row {idx}: {method or '(blank)'}."
        )
    amount = _positive(raw.get("amount"), f"Payment amount on row {idx}")
    reference = str(raw.get("reference") or raw.get("reference_number") or "").strip()
    provider = str(raw.get("provider") or raw.get("bank_or_provider") or "").strip()
    check_number = str(raw.get("check_number") or reference or "").strip()
    check_date = str(raw.get("check_date") or "").strip()
    remarks = _optional_text(raw.get("remarks"), 500)

    row = {"payment_method": method, "amount": amount}
    card = apply_payment_row_card_fields(row)
    surcharge = flt(card["card_surcharge"])
    collected = flt(card["collected_amount"])

    if method == "Cash":
        tendered = _nonnegative(raw.get("cash_tendered"), f"Cash Tendered on row {idx}")
        if tendered + TOLERANCE < amount:
            raise frappe.ValidationError("Cash Tendered is less than Cash Due.")
        change = max(tendered - amount, 0)
        if "change_amount" in raw and not _money_equal(raw.get("change_amount"), change):
            raise NKTIdempotencyConflict(
                "Cash Change conflicts with Cash Tendered minus Cash Due."
            )
        reference = ""
        provider = ""
        check_number = ""
        check_date = ""
    else:
        if abs(flt(raw.get("cash_tendered"))) > TOLERANCE or abs(flt(raw.get("change_amount"))) > TOLERANCE:
            raise frappe.ValidationError(
                f"Only Cash may carry Cash Tendered or Change on row {idx}."
            )
        tendered = 0.0
        change = 0.0

    if method == "Check":
        if not check_number:
            raise frappe.ValidationError(f"Check Number is required on payment row {idx}.")
        if not check_date:
            raise frappe.ValidationError(f"Check Date is required on payment row {idx}.")
        if not provider:
            raise frappe.ValidationError(f"Issuing Bank is required on payment row {idx}.")
        reference = check_number
    elif method in REFERENCE_METHODS and not reference:
        raise frappe.ValidationError(
            f"Reference Number is required for {method} on payment row {idx}."
        )
    elif method in {"Account", "Return Credit"}:
        reference = ""
        provider = ""
        check_number = ""
        check_date = ""

    non_cash_basis = method in {"Account", "Return Credit"}
    _validate_derived(raw, "card_surcharge", surcharge, f"Card surcharge on row {idx}")
    _validate_derived(
        raw,
        "collected_amount",
        0.0 if non_cash_basis else collected,
        f"Collected amount on row {idx}",
    )

    return {
        "line_no": idx,
        "payment_method": method,
        "amount": amount,
        "card_surcharge": surcharge,
        "collected_amount": 0.0 if non_cash_basis else collected,
        "cash_tendered": tendered,
        "change_amount": change,
        "reference_number": reference,
        "bank_or_provider": provider,
        "check_number": check_number,
        "check_date": check_date,
        "remarks": remarks,
    }


def _normalize_price_authorization(raw: Any) -> Optional[Dict[str, Any]]:
    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict):
        raise frappe.ValidationError("Cashier Tender price authorization must be an object.")
    extra = set(raw) - ALLOWED_PRICE_AUTHORIZATION_KEYS
    if extra:
        raise frappe.ValidationError("Cashier Tender price authorization contains unsupported fields.")

    authorized_by = _text(raw.get("authorized_by"), "Price Authorized By", 240)
    authorized_on = _text(raw.get("authorized_on"), "Price Authorized On", 80)
    reason = _text(raw.get("reason"), "Price Authorization Reason", 140)
    explanation = _optional_text(raw.get("explanation"), 1000)
    signature = _text(raw.get("adjustment_signature"), "Price Authorization Signature", 64).lower()
    if len(signature) != 64 or any(ch not in "0123456789abcdef" for ch in signature):
        raise frappe.ValidationError("Cashier Tender price authorization signature is invalid.")
    runtime_role = _text(raw.get("authorization_runtime_role"), "Price Authorization Runtime Role", 40)
    if runtime_role not in {"Primary", "Store Edge"}:
        raise frappe.ValidationError("Cashier Tender price authorization runtime role is invalid.")
    device_id = _optional_text(raw.get("authorization_device_id"), 240)

    return {
        "authorized_by": authorized_by,
        "authorized_on": authorized_on,
        "reason": reason,
        "explanation": explanation,
        "adjustment_signature": signature,
        "authorization_runtime_role": runtime_role,
        "authorization_device_id": device_id,
    }


def _normalize_cashier_tender_intent_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise frappe.ValidationError("Cashier Tender Intent payload must be an object.")
    extra = set(payload) - ALLOWED_TOP_LEVEL_KEYS
    if extra:
        raise frappe.ValidationError("Cashier Tender Intent contains unsupported fields.")

    company = _text(payload.get("company"), "Company")
    customer = _text(payload.get("customer"), "Customer")
    cashier_shift = _text(payload.get("cashier_shift"), "Cashier Shift")
    settlement_location = _text(payload.get("settlement_location"), "Settlement Location")
    default_warehouse = _text(
        payload.get("default_warehouse") or settlement_location,
        "Default Warehouse",
    )
    observed = _text(payload.get("client_observed_at"), "Client observed time", 80)
    ui_version = _optional_text(payload.get("client_ui_version"), 120)
    price_authorization = _normalize_price_authorization(payload.get("price_authorization"))

    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise frappe.ValidationError("At least one Cashier basket line is required.")
    if len(raw_items) > 200:
        raise frappe.ValidationError("Cashier Tender Intent has too many item lines.")
    items = [
        _normalize_item(raw, idx, default_warehouse)
        for idx, raw in enumerate(raw_items, start=1)
    ]
    merchandise_total = sum(flt(x["amount"]) for x in items)

    raw_payments = payload.get("payments")
    if not isinstance(raw_payments, list) or not raw_payments:
        raise frappe.ValidationError("At least one payment row is required.")
    if len(raw_payments) > 30:
        raise frappe.ValidationError("Cashier Tender Intent has too many payment rows.")
    payments = [
        _normalize_payment(raw, idx)
        for idx, raw in enumerate(raw_payments, start=1)
    ]

    if sum(1 for x in payments if x["payment_method"] == "Cash") > 1:
        raise frappe.ValidationError("Only one Cash payment row is allowed.")

    seen_checks = set()
    for idx, row in enumerate(payments, start=1):
        if row["payment_method"] != "Check":
            continue
        key = (
            _normalize_provider(row["bank_or_provider"]),
            _normalize_ref(row["check_number"]),
        )
        if key in seen_checks:
            raise frappe.ValidationError(
                "The same physical Check cannot appear twice in one Cashier tender."
            )
        seen_checks.add(key)

    settlement_total = sum(flt(x["amount"]) for x in payments)
    surcharge_total = sum(flt(x["card_surcharge"]) for x in payments)
    actual_collected = sum(flt(x["collected_amount"]) for x in payments)

    if abs(settlement_total - merchandise_total) > 0.01:
        raise frappe.ValidationError(
            "Payment settlement total must equal the merchandise total."
        )

    _validate_derived(payload, "merchandise_total", merchandise_total, "Merchandise total")
    _validate_derived(payload, "payment_settlement_total", settlement_total, "Payment settlement total")
    _validate_derived(payload, "card_surcharge_total", surcharge_total, "Card surcharge total")
    _validate_derived(payload, "actual_collected_total", actual_collected, "Actual collected total")

    normalized = {
        "company": company,
        "customer": customer,
        "cashier_shift": cashier_shift,
        "settlement_location": settlement_location,
        "default_warehouse": default_warehouse,
        "client_observed_at": observed,
        "client_ui_version": ui_version,
        "items": items,
        "payments": payments,
        "merchandise_total": float(f"{merchandise_total:.6f}"),
        "payment_settlement_total": float(f"{settlement_total:.6f}"),
        "card_surcharge_total": float(f"{surcharge_total:.6f}"),
        "actual_collected_total": float(f"{actual_collected:.6f}"),
    }
    # Backward compatibility: do not add a null key to pre-MP1 tenders,
    # because their canonical payload hash must remain byte-for-byte stable.
    if price_authorization:
        normalized["price_authorization"] = price_authorization
    return normalized


def _canonical_cashier_tender_intent_json(payload: Dict[str, Any]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _validate_shift_business_truth(
    normalized: Dict[str, Any],
    business_date: str,
    user: str,
) -> None:
    if is_edge_shift_reference(normalized["cashier_shift"]):
        validate_edge_shift_for_money(
            normalized["cashier_shift"],
            company=normalized["company"],
            settlement_location=normalized["settlement_location"],
            cashier=user,
            require_open=True,
            business_date=business_date,
        )
        today = manila_now().date()
        if getdate(business_date) != today:
            raise frappe.ValidationError(
                "Cashier Tender Intent requires today's live Store Edge shift. "
                "NKT POS does not allow antedated or backdated Cashier settlement."
            )
        return

    shift = validate_cashier_shift(
        cashier_shift=normalized["cashier_shift"],
        company=normalized["company"],
        settlement_location=normalized["settlement_location"],
        cashier=user,
        require_open=True,
    )
    shift_start = frappe.db.get_value(
        "NKT Cashier Shift", normalized["cashier_shift"], "shift_start"
    )
    if not shift_start:
        raise frappe.ValidationError("Cashier Shift has no opening time.")
    shift_date = getdate(shift_start)
    today = manila_now().date()
    if shift_date != today or getdate(business_date) != today:
        raise frappe.ValidationError(
            "Cashier Tender Intent requires today's open Cashier Shift. "
            "NKT POS does not allow antedated or backdated Cashier settlement."
        )


def _validate_known_check_duplicates(normalized: Dict[str, Any], event_uuid: str) -> None:
    customer = normalized["customer"]
    for row in normalized["payments"]:
        if row["payment_method"] != "Check":
            continue
        provider_norm = _normalize_provider(row["bank_or_provider"])
        check_norm = _normalize_ref(row["check_number"])

        # Preserve the accepted online hard-identity rule for a physical Check.
        posted = frappe.db.sql(
            """
            SELECT pd.parent
            FROM `tabNKT Payment Detail` pd
            INNER JOIN `tabNKT Payment Receipt` pr ON pr.name = pd.parent
            WHERE pd.parenttype = 'NKT Payment Receipt'
              AND pr.docstatus = 1
              AND pr.customer = %s
              AND pd.payment_method = 'Check'
              AND REPLACE(LOWER(TRIM(COALESCE(pd.bank_or_provider, ''))), ' ', '') = %s
              AND REPLACE(LOWER(TRIM(COALESCE(NULLIF(pd.check_number, ''), pd.reference_number, ''))), ' ', '') = %s
            LIMIT 1
            """,
            (customer, "".join(provider_norm.split()), check_norm),
        )
        if posted:
            raise frappe.ValidationError(
                "This physical Check is already recorded for the selected Customer."
            )

        # Also prevent a second local pending tender from claiming the same physical Check.
        pendings = frappe.get_all(
            "NKT Sync Pending Payload",
            filters={"event_family": TENDER_INTENT_FAMILY, "name": ["!=", event_uuid]},
            fields=["name", "payload_json"],
            limit_page_length=500,
        )
        for pending in pendings:
            try:
                other = json.loads(pending.payload_json or "{}")
            except Exception:
                continue
            if str(other.get("customer") or "") != customer:
                continue
            for other_row in other.get("payments") or []:
                if other_row.get("payment_method") != "Check":
                    continue
                other_key = (
                    _normalize_provider(other_row.get("bank_or_provider")),
                    _normalize_ref(other_row.get("check_number") or other_row.get("reference_number")),
                )
                if other_key == (provider_norm, check_norm):
                    raise frappe.ValidationError(
                        "This physical Check is already captured in another pending local Cashier tender."
                    )


def _return_credit_consumption_key(event_uuid: str) -> str:
    return hashlib.sha256(
        f"{event_uuid}|return-credit-consumption".encode("utf-8")
    ).hexdigest()


def _return_credit_amount(normalized: Dict[str, Any]) -> float:
    return flt(
        sum(
            flt(row.get("amount"))
            for row in (normalized.get("payments") or [])
            if row.get("payment_method") == "Return Credit"
        )
    )


def _lock_customer_for_return_credit(customer: str) -> None:
    rows = frappe.db.sql(
        "SELECT name FROM `tabCustomer` WHERE name=%s FOR UPDATE",
        (customer,),
    )
    if not rows:
        raise frappe.ValidationError("Customer is unavailable for Return Credit.")


def _verify_return_credit_available(
    event_uuid: str,
    normalized: Dict[str, Any],
) -> Dict[str, float]:
    requested = _return_credit_amount(normalized)
    if requested <= TOLERANCE:
        return {
            "requested_return_credit": 0.0,
            "effective_return_credit_before_this_tender": 0.0,
        }

    _lock_customer_for_return_credit(normalized["customer"])

    from nkt_operations.nkt_store_operations.features.returns.internal.return_exchange_edge_credit import (
        effective_return_credit_available,
    )

    effective = effective_return_credit_available(
        normalized["customer"],
        exclude_consumption_event_uuid=event_uuid,
        locking_current_read=True,
    )
    if requested > effective + TOLERANCE:
        raise frappe.ValidationError(
            "Return Credit payment exceeds the customer's effective available "
            f"Return Credit ({effective:,.2f})."
        )
    return {
        "requested_return_credit": requested,
        "effective_return_credit_before_this_tender": effective,
    }


def _verify_or_insert_return_credit_consumption(
    event_uuid: str,
    normalized: Dict[str, Any],
    credit_truth: Dict[str, float],
) -> Optional[str]:
    amount = flt(credit_truth.get("requested_return_credit"))
    if amount <= TOLERANCE:
        return None

    key = _return_credit_consumption_key(event_uuid)
    existing = frappe.db.exists(
        "NKT Edge Return Credit Consumption Projection",
        key,
    )
    expected = {
        "projection_key": key,
        "event_uuid": event_uuid,
        "customer": normalized["customer"],
        "cashier_shift": normalized["cashier_shift"],
        "reserved_amount": amount,
        "projection_state": "Pending Edge",
    }
    if existing:
        doc = frappe.get_doc("NKT Edge Return Credit Consumption Projection", existing)
        if (
            str(doc.event_uuid or "") != event_uuid
            or str(doc.customer or "") != normalized["customer"]
            or str(doc.cashier_shift or "") != normalized["cashier_shift"]
            or abs(flt(doc.reserved_amount) - amount) > TOLERANCE
        ):
            raise NKTIdempotencyConflict(
                "Return Credit consumption projection conflicts with immutable Cashier Tender Event UUID."
            )
        return doc.name

    doc = frappe.get_doc(
        {"doctype": "NKT Edge Return Credit Consumption Projection", **expected}
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def accept_cashier_tender_intent_at_edge(
    event_uuid: str,
    device_id: str,
    business_date: Any,
    settled_at: Any,
    payload: Dict[str, Any],
    *,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    """
    C15C.10A first money-family promotion.

    Authority is intentionally limited to immutable observed tender intent +
    durable local queue. It does NOT create or submit canonical financial,
    receivable, matching, warehouse, or stock records.
    """
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Local Cashier tender entry unavailable.")

    user = _session_user(user)
    _require_cashier_authority(user)

    device = device_policy_snapshot(
        device_id,
        user=user,
        requested_context="NKT Retail",
    )
    if device.get("ui_mode") != "normal":
        raise frappe.PermissionError("Local Cashier tender entry unavailable.")

    family = event_family_policy(TENDER_INTENT_FAMILY)
    if family.get("offline_write_allowed") is not True:
        raise frappe.PermissionError("Local Cashier tender entry unavailable.")

    business = validate_business_time(business_date, settled_at)
    normalized = _normalize_cashier_tender_intent_payload(payload)
    _validate_shift_business_truth(normalized, business["business_date"], user)

    event_uuid = _uuid(event_uuid)
    _validate_known_check_duplicates(normalized, event_uuid)
    credit_truth = _verify_return_credit_available(event_uuid, normalized)

    digest = canonical_payload_hash(normalized)
    envelope = {
        "event_uuid": event_uuid,
        "event_family": TENDER_INTENT_FAMILY,
        "event_action": TENDER_INTENT_ACTION,
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
    }

    event, replay = begin_event(envelope)
    canonical = _canonical_cashier_tender_intent_json(normalized)
    pending_name = frappe.db.exists("NKT Sync Pending Payload", event.event_uuid)

    if pending_name:
        pending = frappe.get_doc("NKT Sync Pending Payload", pending_name)
        if pending.event_family != TENDER_INTENT_FAMILY:
            raise NKTIdempotencyConflict(
                "Pending payload family conflicts with immutable Cashier Tender Intent."
            )
        if pending.payload_sha256 != digest or pending.payload_json != canonical:
            raise NKTIdempotencyConflict(
                "Pending payload conflicts with immutable Cashier Tender Intent."
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
                "Cashier Tender Intent exists but its durable pending payload is unavailable."
            )
        if event.sync_state not in ("Committed at Primary", "Conflict", "Failed"):
            frappe.get_doc({
                "doctype": "NKT Sync Pending Payload",
                "event_uuid": event.event_uuid,
                "event_family": TENDER_INTENT_FAMILY,
                "payload_sha256": digest,
                "payload_json": canonical,
                "queue_state": "Accepted at Edge",
                "edge_accepted_at": now(),
                "attempt_count": 1,
                "last_attempt_at": now(),
            }).insert(ignore_permissions=True)

    credit_projection = _verify_or_insert_return_credit_consumption(
        event.event_uuid,
        normalized,
        credit_truth,
    )

    if event.sync_state not in ("Committed at Primary", "Conflict", "Failed"):
        mark_edge_accepted(event.event_uuid)
        event.reload()

    return {
        "event_uuid": event.event_uuid,
        "event_family": TENDER_INTENT_FAMILY,
        "sync_state": event.sync_state,
        "durable_ack": True,
        "replay": bool(replay),
        "payload_sha256": digest,
        "merchandise_total": normalized["merchandise_total"],
        "payment_settlement_total": normalized["payment_settlement_total"],
        "card_surcharge_total": normalized["card_surcharge_total"],
        "actual_collected_total": normalized["actual_collected_total"],
        "return_credit_reserved": credit_truth["requested_return_credit"],
        "effective_return_credit_before_tender": credit_truth["effective_return_credit_before_this_tender"],
        "return_credit_consumption_projection": credit_projection,
        "canonical_cashier_sale_created": False,
        "payment_receipt_created": False,
        "cashier_movement_created": False,
        "receivable_created": False,
        "warehouse_release_created": False,
        "stock_entry_created": False,
        "matching_executed": False,
        "primary_tender_materializer_required": True,
    }


@frappe.whitelist()
def submit_cashier_tender_intent(
    event_uuid: str,
    device_id: str,
    business_date: str,
    settled_at: str,
    payload: Any,
):
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    return accept_cashier_tender_intent_at_edge(
        event_uuid,
        device_id,
        business_date,
        settled_at,
        payload,
        user=_session_user(),
    )
