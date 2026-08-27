from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from typing import Any, Dict, Iterable
from zoneinfo import ZoneInfo

import frappe

FOUNDATION_VERSION = "C15C.10I-R2"

FAMILY = "NKT Return Exchange Declaration Intent"
ACTION = "record_return_exchange_declaration"

SIDES = {"Cashier", "Encoder"}
SIDE_ROLE = {"Cashier": "NKT Cashier", "Encoder": "NKT Encoder"}
ADMIN_ROLES = {"System Manager", "NKT OWNER", "NKT ADMINISTRATOR"}

TRANSACTION_TYPES = {"Return", "Exchange"}
RETURN_CLASSIFICATIONS = {"Saleable", "Damaged", "Fraction", "Rejected"}
VALUE_TREATMENTS = {"Full Value", "Deduct Missing kg", "Manual Deduction"}

MONEY_METHODS = {"Cash", "Check", "GCash", "Maya", "Card", "Bank Transfer", "Online"}
SETTLEMENT_METHODS = MONEY_METHODS | {"Account"}
REFERENCE_METHODS = {"Check", "GCash", "Maya", "Card", "Bank Transfer", "Online"}
SETTLEMENT_DESTINATIONS = {"None", "Refund Money", "Customer Credit", "Account Adjustment"}

PH_TZ = ZoneInfo("Asia/Manila")
TOLERANCE = 0.000001

# Locked outage rules.
CROSS_MIDNIGHT_TRUE_TIME_PRESERVED = True
EMPLOYEE_MANUAL_BACKDATE_ENABLED = False
CASHIER_SHIFT_PRESERVED = True
CASHIER_ENCODER_OPERATIONALLY_INDEPENDENT = True
MATCHING_IS_POST_OPERATION_RECONCILIATION = True

SALEABLE_RETURN_LOCAL_USABLE_STOCK = True
DAMAGED_FRACTION_REJECTED_NORMAL_USABLE_STOCK = False

ENCODER_CUSTOMER_CREDIT_LOCAL_USABLE = True
ENCODER_ACCOUNT_ADJUSTMENT_LOCAL_USABLE = True

CONTROLLED_REVERSAL_OFFLINE_ENABLED = False
CANONICAL_POSTING_AT_EDGE_ENABLED = False

# The 2% Card rule is not client-authored. The accepted C7/Card helper derives
# Card surcharge/collected amount later. Maya is not Card and carries no +2%.
CARD_SURCHARGE_CLIENT_AUTHORED = False
CARD_SURCHARGE_CANONICAL_DERIVED = True

# Canonical/posting/matching results are server-owned and may never be supplied
# as operator-authored immutable intent.
FORBIDDEN_SERVER_EFFECT_KEYS = {
    "name",
    "docstatus",
    "posting_status",
    "posted_on",
    "reconciliation_status",
    "matched_declaration",
    "exact_candidate_count",
    "match_key",
    "return_stock_entry",
    "new_cashier_sale",
    "new_customer_order",
    "account_adjustment_record",
    "customer_credit_record",
    "refund_movement",
    "stock_entry",
    "sales_invoice",
    "payment_entry",
    "gl_entry",
    "stock_ledger_entry",
    "card_surcharge",
    "collected_amount",
}

# Controlled reversal is deliberately Primary/online-only.
FORBIDDEN_OFFLINE_REVERSAL_KEYS = {
    "controlled_reversal",
    "reversal",
    "reversal_reason",
    "reversal_request",
    "reversal_document",
    "cancel_posted_return",
    "cancel_posted_exchange",
}

RETURN_ROW_ALLOWED_KEYS = {
    "line_no",
    "item",
    "quantity",
    "original_source_warehouse",
    "classification",
    "actual_kg_returned",
    "return_value_treatment",
    "manual_deduction",
}

NEW_ROW_ALLOWED_KEYS = {
    "line_no",
    "item",
    "quantity",
    "rate",
    "source_warehouse",
}

PAYMENT_ROW_ALLOWED_KEYS = {
    "line_no",
    "payment_method",
    "amount",
    "cash_tendered",
    "reference_number",
    "bank_or_provider",
    "check_date",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any, label: str, *, minimum: float = 0.0) -> float:
    try:
        number = float(value or 0)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} must be numeric.") from exc
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


def _reject_unknown_keys(
    value: Dict[str, Any],
    allowed: Iterable[str],
    label: str,
) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise frappe.ValidationError(
            f"{label} contains unsupported/client-owned fields: {', '.join(unknown)}."
        )


def _reject_server_effect_keys(payload: Dict[str, Any]) -> None:
    bad = sorted((set(payload) & FORBIDDEN_SERVER_EFFECT_KEYS))
    if bad:
        raise frappe.ValidationError(
            "Return/Exchange immutable intent cannot author canonical/posting results: "
            + ", ".join(bad)
            + "."
        )
    reversal = sorted((set(payload) & FORBIDDEN_OFFLINE_REVERSAL_KEYS))
    if reversal:
        raise frappe.ValidationError(
            "Controlled Return/Exchange reversal is Primary/online-only and cannot be "
            "authored as an offline intent: "
            + ", ".join(reversal)
            + "."
        )


def _normalize_return_row(side: str, raw: Dict[str, Any], idx: int) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise frappe.ValidationError(f"Returned Item row {idx} is invalid.")
    _reject_unknown_keys(raw, RETURN_ROW_ALLOWED_KEYS, f"Returned Item row {idx}")

    if raw.get("line_no") not in (None, "", idx):
        try:
            supplied_line_no = int(raw.get("line_no"))
        except Exception as exc:
            raise frappe.ValidationError(
                f"Returned Item row {idx} Line No is invalid."
            ) from exc
        if supplied_line_no != idx:
            raise frappe.ValidationError(
                f"Returned Item row {idx} Line No does not match its canonical position."
            )

    item = _text(raw.get("item"))
    if not item:
        raise frappe.ValidationError(f"Returned Item row {idx} requires Item.")

    quantity = _number(raw.get("quantity"), f"Returned Item row {idx} Quantity", minimum=0)
    if quantity <= TOLERANCE:
        raise frappe.ValidationError(f"Returned Item row {idx} Quantity must be greater than zero.")

    source_warehouse = _text(raw.get("original_source_warehouse"))
    if not source_warehouse:
        raise frappe.ValidationError(
            f"Returned Item row {idx} requires the OLD source warehouse lineage."
        )

    treatment = _text(raw.get("return_value_treatment")) or "Full Value"
    if treatment not in VALUE_TREATMENTS:
        raise frappe.ValidationError(
            f"Returned Item row {idx} has invalid return-value treatment."
        )

    actual_kg = _number(
        raw.get("actual_kg_returned"),
        f"Returned Item row {idx} Actual kg Returned",
        minimum=0,
    )
    manual_deduction = _number(
        raw.get("manual_deduction"),
        f"Returned Item row {idx} Manual Deduction",
        minimum=0,
    )

    classification = _text(raw.get("classification"))
    if side == "Cashier":
        if classification:
            raise frappe.ValidationError(
                "Cashier does not control returned-stock classification."
            )
        classification = ""
    else:
        if classification not in RETURN_CLASSIFICATIONS:
            raise frappe.ValidationError(
                f"Encoder must select Saleable, Damaged, Fraction, or Rejected "
                f"for Returned Item row {idx}."
            )
        if classification == "Fraction":
            if abs(quantity - 1.0) > TOLERANCE:
                raise frappe.ValidationError(
                    f"Fraction Returned Item row {idx} must be entered one sack at a time."
                )
            if actual_kg <= TOLERANCE:
                raise frappe.ValidationError(
                    f"Fraction Returned Item row {idx} requires Actual kg Returned."
                )

    if treatment == "Deduct Missing kg" and actual_kg <= TOLERANCE:
        raise frappe.ValidationError(
            f"Returned Item row {idx} using Deduct Missing kg requires Actual kg Returned."
        )
    if treatment != "Manual Deduction" and manual_deduction > TOLERANCE:
        raise frappe.ValidationError(
            f"Returned Item row {idx} can carry Manual Deduction only when "
            "Return Value Treatment is Manual Deduction."
        )

    return {
        "line_no": idx,
        "item": item,
        "quantity": quantity,
        "original_source_warehouse": source_warehouse,
        "classification": classification,
        "actual_kg_returned": actual_kg,
        "return_value_treatment": treatment,
        "manual_deduction": manual_deduction,
    }


def _normalize_new_row(side: str, raw: Dict[str, Any], idx: int) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise frappe.ValidationError(f"NEW ORDER row {idx} is invalid.")
    _reject_unknown_keys(raw, NEW_ROW_ALLOWED_KEYS, f"NEW ORDER row {idx}")

    if raw.get("line_no") not in (None, "", idx):
        try:
            supplied_line_no = int(raw.get("line_no"))
        except Exception as exc:
            raise frappe.ValidationError(
                f"NEW ORDER row {idx} Line No is invalid."
            ) from exc
        if supplied_line_no != idx:
            raise frappe.ValidationError(
                f"NEW ORDER row {idx} Line No does not match its canonical position."
            )

    item = _text(raw.get("item"))
    if not item:
        raise frappe.ValidationError(f"NEW ORDER row {idx} requires Item.")

    quantity = _number(raw.get("quantity"), f"NEW ORDER row {idx} Quantity", minimum=0)
    if quantity <= TOLERANCE:
        raise frappe.ValidationError(f"NEW ORDER row {idx} Quantity must be greater than zero.")

    rate = _number(raw.get("rate"), f"NEW ORDER row {idx} Rate", minimum=0)

    source_warehouse = _text(raw.get("source_warehouse"))
    if side == "Cashier":
        if source_warehouse:
            raise frappe.ValidationError(
                "Cashier does not control the NEW ORDER source warehouse."
            )
        source_warehouse = ""
    else:
        if not source_warehouse:
            raise frappe.ValidationError(
                f"Encoder must select Source Warehouse for NEW ORDER row {idx}."
            )

    return {
        "line_no": idx,
        "item": item,
        "quantity": quantity,
        "rate": round(rate, 2),
        "source_warehouse": source_warehouse,
    }


def _normalize_payment_row(side: str, raw: Dict[str, Any], idx: int) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise frappe.ValidationError(f"Settlement row {idx} is invalid.")
    _reject_unknown_keys(raw, PAYMENT_ROW_ALLOWED_KEYS, f"Settlement row {idx}")

    if raw.get("line_no") not in (None, "", idx):
        try:
            supplied_line_no = int(raw.get("line_no"))
        except Exception as exc:
            raise frappe.ValidationError(
                f"Settlement row {idx} Line No is invalid."
            ) from exc
        if supplied_line_no != idx:
            raise frappe.ValidationError(
                f"Settlement row {idx} Line No does not match its canonical position."
            )

    method = _text(raw.get("payment_method"))
    if method not in SETTLEMENT_METHODS:
        raise frappe.ValidationError(
            f"Settlement row {idx} uses an unsupported payment method."
        )

    amount = _number(raw.get("amount"), f"Settlement row {idx} Amount", minimum=0)
    if amount <= TOLERANCE:
        raise frappe.ValidationError(f"Settlement row {idx} Amount must be greater than zero.")

    reference = _text(raw.get("reference_number"))
    provider = _text(raw.get("bank_or_provider"))
    check_date = _text(raw.get("check_date"))
    cash_tendered = _number(
        raw.get("cash_tendered"),
        f"Settlement row {idx} Cash Tendered",
        minimum=0,
    )

    if method == "Cash":
        reference = ""
        provider = ""
        check_date = ""
        if side == "Cashier":
            if cash_tendered + TOLERANCE < amount:
                raise frappe.ValidationError(
                    f"Settlement row {idx} Cash Tendered is less than the Cash amount."
                )
        elif cash_tendered > TOLERANCE:
            raise frappe.ValidationError(
                "Encoder does not author Cash Tendered/Change; that is Cashier money-side evidence."
            )
        else:
            cash_tendered = 0.0
    else:
        if cash_tendered > TOLERANCE:
            raise frappe.ValidationError(
                f"Settlement row {idx} Cash Tendered is allowed only for Cash."
            )
        cash_tendered = 0.0

    if method in REFERENCE_METHODS and not reference:
        raise frappe.ValidationError(
            f"Settlement row {idx} requires a reference for {method}."
        )
    if method == "Check":
        if not provider:
            raise frappe.ValidationError(
                f"Settlement row {idx} Check requires Issuing Bank."
            )
        if not check_date:
            raise frappe.ValidationError(
                f"Settlement row {idx} Check requires Check Date."
            )

    return {
        "line_no": idx,
        "payment_method": method,
        "amount": round(amount, 2),
        "cash_tendered": round(cash_tendered, 2),
        "reference_number": reference,
        "bank_or_provider": provider,
        "check_date": check_date,
    }


def normalize_return_exchange_intent(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise frappe.ValidationError("Return/Exchange immutable intent payload is invalid.")

    _reject_server_effect_keys(payload)

    allowed = {
        "family",
        "action",
        "side",
        "submit_request_id",
        "company",
        "business_date",
        "entry_datetime",
        "entry_user",
        "cashier_shift",
        "cashier_shift_business_date",
        "customer",
        "old_cashier_sale",
        "old_customer_order",
        "source_generation",
        "transaction_type",
        "returned_items",
        "new_items",
        "settlement_destination",
        "settlement_method",
        "settlement_reference",
        "settlement_payments",
        "return_warehouse",
        "notes",
    }
    _reject_unknown_keys(payload, allowed, "Return/Exchange intent")

    payload_family = _text(payload.get("family"))
    if payload_family and payload_family != FAMILY:
        raise frappe.ValidationError(
            "Return/Exchange intent Family does not match the locked offline event family."
        )

    payload_action = _text(payload.get("action"))
    if payload_action and payload_action != ACTION:
        raise frappe.ValidationError(
            "Return/Exchange intent Action does not match the locked offline event action."
        )

    side = _text(payload.get("side"))
    if side not in SIDES:
        raise frappe.ValidationError("Return/Exchange side must be Cashier or Encoder.")

    company = _text(payload.get("company"))
    customer = _text(payload.get("customer"))
    old_cashier_sale = _text(payload.get("old_cashier_sale"))
    old_customer_order = _text(payload.get("old_customer_order"))
    entry_user = _text(payload.get("entry_user"))

    for label, value in (
        ("Company", company),
        ("Customer", customer),
        ("OLD Cashier Sale", old_cashier_sale),
        ("OLD Customer Order", old_customer_order),
        ("Entered By", entry_user),
    ):
        if not value:
            raise frappe.ValidationError(f"{label} is required.")

    business_date = _date(payload.get("business_date"), "Business Date")
    entry_dt = _manila_datetime(payload.get("entry_datetime"), "Entry Time")
    if entry_dt.date() != business_date:
        raise frappe.ValidationError(
            "Return/Exchange Business Date must equal the true Store-Edge physical entry date."
        )

    source_generation = int(payload.get("source_generation") or 0)
    if source_generation < 0:
        raise frappe.ValidationError("Exchange Generation cannot be negative.")

    transaction_type = _text(payload.get("transaction_type")) or "Return"
    if transaction_type not in TRANSACTION_TYPES:
        raise frappe.ValidationError("Transaction Type must be Return or Exchange.")

    returned_raw = payload.get("returned_items") or []
    if not isinstance(returned_raw, list) or not returned_raw:
        raise frappe.ValidationError("Enter at least one returned item.")
    returned = [
        _normalize_return_row(side, row, idx)
        for idx, row in enumerate(returned_raw, start=1)
    ]

    new_raw = payload.get("new_items") or []
    if not isinstance(new_raw, list):
        raise frappe.ValidationError("NEW ORDER rows are invalid.")
    new_items = [
        _normalize_new_row(side, row, idx)
        for idx, row in enumerate(new_raw, start=1)
    ]

    if transaction_type == "Exchange" and not new_items:
        raise frappe.ValidationError("An Exchange requires at least one NEW ORDER item.")
    if transaction_type == "Return" and new_items:
        raise frappe.ValidationError("A pure Return must not contain NEW ORDER items.")

    return_warehouse = _text(payload.get("return_warehouse"))
    if side == "Encoder":
        if not return_warehouse:
            raise frappe.ValidationError(
                "Encoder must select the Return Receiving Warehouse."
            )
    elif return_warehouse:
        raise frappe.ValidationError(
            "Cashier does not control the Return Receiving Warehouse."
        )

    cashier_shift = _text(payload.get("cashier_shift"))
    shift_date_raw = _text(payload.get("cashier_shift_business_date"))
    if side == "Cashier":
        if not cashier_shift:
            raise frappe.ValidationError(
                "Cashier offline Return/Exchange intent must preserve the original Cashier Shift."
            )
        if not shift_date_raw:
            raise frappe.ValidationError(
                "Cashier Shift Business Date is required."
            )
        shift_date = _date(shift_date_raw, "Cashier Shift Business Date")
        if shift_date != business_date:
            raise frappe.ValidationError(
                "Cashier Shift Business Date must equal the true physical Return/Exchange Business Date."
            )
    else:
        if cashier_shift or shift_date_raw:
            raise frappe.ValidationError(
                "Encoder Return/Exchange intent cannot author Cashier Shift evidence."
            )
        shift_date = None

    destination = _text(payload.get("settlement_destination")) or "None"
    if destination not in SETTLEMENT_DESTINATIONS:
        raise frappe.ValidationError("Invalid Refund / Credit Destination.")

    settlement_method = _text(payload.get("settlement_method"))
    settlement_reference = _text(payload.get("settlement_reference"))

    if destination == "Refund Money":
        if settlement_method and settlement_method not in MONEY_METHODS:
            raise frappe.ValidationError("Invalid Refund Money Method.")
        if settlement_method in REFERENCE_METHODS and not settlement_reference:
            raise frappe.ValidationError(
                "Refund Reference is required for the selected Refund Money Method."
            )
    else:
        if settlement_method or settlement_reference:
            raise frappe.ValidationError(
                "Refund Method/Reference are allowed only for Refund Money."
            )

    payment_rows_raw = payload.get("settlement_payments") or []
    if not isinstance(payment_rows_raw, list):
        raise frappe.ValidationError("Settlement Payment rows are invalid.")
    payments = [
        _normalize_payment_row(side, row, idx)
        for idx, row in enumerate(payment_rows_raw, start=1)
    ]

    # Exact amount/destination tally is intentionally deferred to the accepted
    # C7 preview/financial engine because it depends on OLD-order money/account
    # basis, prior returns, Card derivation, and current receivable state.
    # R2 preserves immutable operator inputs only.

    notes = _text(payload.get("notes"))

    normalized = {
        "family": FAMILY,
        "action": ACTION,
        "side": side,
        "submit_request_id": _request_id(payload.get("submit_request_id")),
        "company": company,
        "business_date": business_date.isoformat(),
        "entry_datetime": entry_dt.isoformat(),
        "entry_user": entry_user,
        "cashier_shift": cashier_shift or None,
        "cashier_shift_business_date": shift_date.isoformat() if shift_date else None,
        "customer": customer,
        "old_cashier_sale": old_cashier_sale,
        "old_customer_order": old_customer_order,
        "source_generation": source_generation,
        "transaction_type": transaction_type,
        "returned_items": returned,
        "new_items": new_items,
        "settlement_destination": destination,
        "settlement_method": settlement_method,
        "settlement_reference": settlement_reference,
        "settlement_payments": payments,
        "return_warehouse": return_warehouse or None,
        "notes": notes,
    }
    return normalized


def canonical_return_exchange_intent_json(payload: Dict[str, Any]) -> str:
    return json.dumps(
        normalize_return_exchange_intent(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def foundation_status() -> Dict[str, Any]:
    return {
        "foundation_version": FOUNDATION_VERSION,
        "event_family": FAMILY,
        "event_action": ACTION,
        "cashier_encoder_operationally_independent": CASHIER_ENCODER_OPERATIONALLY_INDEPENDENT,
        "matching_is_post_operation_reconciliation": MATCHING_IS_POST_OPERATION_RECONCILIATION,
        "cross_midnight_true_time_preserved": CROSS_MIDNIGHT_TRUE_TIME_PRESERVED,
        "employee_manual_backdate_enabled": EMPLOYEE_MANUAL_BACKDATE_ENABLED,
        "cashier_shift_preserved": CASHIER_SHIFT_PRESERVED,
        "saleable_return_local_usable_stock_locked_for_next_stage": SALEABLE_RETURN_LOCAL_USABLE_STOCK,
        "damaged_fraction_rejected_normal_usable_stock": DAMAGED_FRACTION_REJECTED_NORMAL_USABLE_STOCK,
        "encoder_customer_credit_local_usable_locked_for_next_stage": ENCODER_CUSTOMER_CREDIT_LOCAL_USABLE,
        "encoder_account_adjustment_local_usable_locked_for_next_stage": ENCODER_ACCOUNT_ADJUSTMENT_LOCAL_USABLE,
        "controlled_reversal_offline_enabled": CONTROLLED_REVERSAL_OFFLINE_ENABLED,
        "canonical_posting_at_edge_enabled": CANONICAL_POSTING_AT_EDGE_ENABLED,
        "card_surcharge_client_authored": CARD_SURCHARGE_CLIENT_AUTHORED,
        "card_surcharge_canonical_derived": CARD_SURCHARGE_CANONICAL_DERIVED,
        "maya_card_surcharge_rule": "Maya is not Card; +2% applies only to Card.",
    }
