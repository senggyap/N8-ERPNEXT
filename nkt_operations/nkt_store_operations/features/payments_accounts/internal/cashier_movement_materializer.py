from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List

import frappe
from frappe.utils import cint, flt, get_datetime, getdate, now

from nkt_operations.nkt_store_operations.features.payments_accounts.card_surcharge import (
    normalize_payment_method,
    row_collected_amount,
)
from nkt_operations.nkt_store_operations.features.payments_accounts.cash_ledger import create_cashier_movement
from nkt_operations.nkt_store_operations.features.cashier.internal.cashier_shift_alias import resolve_primary_cashier_shift
from nkt_operations.nkt_store_operations.features.payments_accounts.internal.cashier_tender_intent import (
    TENDER_INTENT_FAMILY,
    _canonical_cashier_tender_intent_json,
    _normalize_cashier_tender_intent_payload,
)
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    canonical_payload_hash,
)

FOUNDATION_VERSION = "C15C.10C-R2"
PRIMARY_JOURNAL_DOCTYPE = "NKT Primary Cashier Tender Intent"
PRIMARY_RECEIPT_DOCTYPE = "NKT Sync Primary Receipt"
PAYMENT_RECEIPT_DOCTYPE = "NKT Payment Receipt"
MOVEMENT_DOCTYPE = "NKT Cashier Movement"

PAYMENT_MATERIALIZED_STATE = "Payment Receipt Materialized - Awaiting Cashier Movement"
NO_RECEIPT_STATE = "Payment Receipt Not Required - Awaiting Later Settlement Family"
MOVEMENT_MATERIALIZED_STATE = "Cashier Movement Materialized - Awaiting Receivable"
NO_MOVEMENT_STATE = "Cashier Movement Not Required - Awaiting Receivable"
TENDER_PRESERVED_STATE = "Tender Intent Preserved"
TOLERANCE = 0.005


def _runtime_role() -> str:
    return str(frappe.conf.get("nkt_runtime_role") or "Primary").strip()


def _require_primary() -> None:
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Cashier Movement Tender materialization is unavailable.")


def _require_rpc_materialization_authority() -> None:
    user = str(frappe.session.user or "").strip()
    roles = set(frappe.get_roles(user))
    if user == "Administrator" or "System Manager" in roles:
        return
    raise frappe.PermissionError("Cashier Movement Tender materialization is unavailable.")


def _event_uuid(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError("Cashier Tender Event UUID is invalid.") from exc


def _release_lock(lock_name: str) -> None:
    try:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
    except Exception:
        pass


def _acquire_lock(event_uuid: str) -> None:
    lock_name = "nkt-c15c10c-mov-" + event_uuid
    rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (lock_name, 30), as_list=True)
    if not rows or int(rows[0][0] or 0) != 1:
        raise frappe.ValidationError(
            "Cashier Movement materialization is busy for this immutable Cashier Tender."
        )
    state = {"released": False}

    def release_once():
        if state["released"]:
            return
        _release_lock(lock_name)
        state["released"] = True

    frappe.db.after_commit.add(release_once)
    frappe.db.after_rollback.add(release_once)


def _load_locked_source(event_uuid: str):
    rows = frappe.db.sql(
        """
        SELECT
            name,event_uuid,event_family,origin_user,business_date,settled_at,
            company,customer,cashier_shift,settlement_location,
            payload_sha256,canonical_payload_json,preservation_state,
            downstream_state,payment_receipt,payment_receipt_materialized_at,
            cashier_movement_materialized_at,cashier_movement_count
        FROM `tabNKT Primary Cashier Tender Intent`
        WHERE name=%s
        FOR UPDATE
        """,
        (event_uuid,),
        as_dict=True,
    )
    if not rows:
        raise frappe.DoesNotExistError("Preserved Primary Cashier Tender was not found.")
    journal = rows[0]

    primary_receipt = frappe.db.get_value(
        PRIMARY_RECEIPT_DOCTYPE,
        event_uuid,
        [
            "name", "event_family", "payload_sha256", "canonical_doctype",
            "canonical_name", "materialization_state", "result_code",
        ],
        as_dict=True,
    )
    if not primary_receipt:
        raise NKTIdempotencyConflict(
            "Primary Cashier Tender journal exists without its immutable Sync Primary Receipt."
        )

    bad = []
    if str(journal.event_family or "") != TENDER_INTENT_FAMILY:
        bad.append("journal.event_family")
    if str(journal.preservation_state or "") != "Preserved":
        bad.append("journal.preservation_state")
    if str(primary_receipt.event_family or "") != TENDER_INTENT_FAMILY:
        bad.append("receipt.event_family")
    if str(primary_receipt.payload_sha256 or "") != str(journal.payload_sha256 or ""):
        bad.append("receipt.payload_sha256")
    if str(primary_receipt.canonical_doctype or "") != PRIMARY_JOURNAL_DOCTYPE:
        bad.append("receipt.canonical_doctype")
    if str(primary_receipt.canonical_name or "") != event_uuid:
        bad.append("receipt.canonical_name")
    if str(primary_receipt.materialization_state or "") != TENDER_PRESERVED_STATE:
        bad.append("receipt.materialization_state")
    if str(primary_receipt.result_code or "") != "Committed":
        bad.append("receipt.result_code")
    if bad:
        raise NKTIdempotencyConflict(
            "Cashier Tender is not eligible for Cashier Movement materialization: "
            + ", ".join(bad)
        )

    try:
        raw = json.loads(journal.canonical_payload_json or "{}")
    except Exception as exc:
        raise NKTIdempotencyConflict("Primary Cashier Tender payload is unreadable.") from exc

    normalized = _normalize_cashier_tender_intent_payload(raw)
    canonical = _canonical_cashier_tender_intent_json(normalized)
    digest = canonical_payload_hash(normalized)
    if canonical != str(journal.canonical_payload_json or ""):
        raise NKTIdempotencyConflict(
            "Primary Cashier Tender canonical payload changed after preservation."
        )
    if digest != str(journal.payload_sha256 or ""):
        raise NKTIdempotencyConflict(
            "Primary Cashier Tender payload hash changed after preservation."
        )

    return journal, normalized


def _load_bound_payment_receipt(journal, payload: Dict[str, Any]):
    receipt_name = str(journal.payment_receipt or "").strip()
    if not receipt_name:
        raise NKTIdempotencyConflict(
            "Cashier Tender reached Payment Receipt materialized state without a canonical receipt binding."
        )

    doc = frappe.get_doc(PAYMENT_RECEIPT_DOCTYPE, receipt_name)
    if cint(doc.docstatus) != 1:
        raise NKTIdempotencyConflict("Tender-derived Payment Receipt is not submitted.")
    if str(doc.source_primary_tender_intent or "") != str(journal.name):
        raise NKTIdempotencyConflict("Payment Receipt source Tender identity conflicts.")
    if str(doc.source_tender_payload_sha256 or "") != str(journal.payload_sha256 or ""):
        raise NKTIdempotencyConflict("Payment Receipt source Tender hash conflicts.")
    doc.validate_primary_tender_binding()

    hard = []
    if str(doc.company or "") != str(journal.company or ""):
        hard.append("company")
    if str(doc.customer or "") != str(journal.customer or ""):
        hard.append("customer")
    if str(doc.cashier_shift or "") != str(journal.cashier_shift or ""):
        hard.append("cashier_shift")
    if str(doc.settlement_location or "") != str(journal.settlement_location or ""):
        hard.append("settlement_location")
    if str(doc.received_by or "") != str(journal.origin_user or ""):
        hard.append("received_by")
    if get_datetime(doc.receipt_datetime) != get_datetime(journal.settled_at):
        hard.append("receipt_datetime")
    if hard:
        raise NKTIdempotencyConflict(
            "Payment Receipt immutable Tender context conflicts: " + ", ".join(hard)
        )
    return doc


def _validate_shift_truth(journal) -> None:
    resolved_shift = resolve_primary_cashier_shift(journal.cashier_shift)
    shift = frappe.db.get_value(
        "NKT Cashier Shift",
        resolved_shift,
        [
            "name", "docstatus", "status", "company", "settlement_location",
            "cashier", "shift_start", "shift_end",
        ],
        as_dict=True,
    )
    if not shift:
        raise NKTIdempotencyConflict("Cashier Tender shift is missing on Primary.")

    hard = []
    if str(shift.company or "") != str(journal.company or ""):
        hard.append("company")
    if str(shift.settlement_location or "") != str(journal.settlement_location or ""):
        hard.append("settlement_location")
    if str(shift.cashier or "") != str(journal.origin_user or ""):
        hard.append("cashier")
    if shift.shift_start and getdate(shift.shift_start) != getdate(journal.business_date):
        hard.append("business_date")
    if shift.shift_end and get_datetime(journal.settled_at) > get_datetime(shift.shift_end):
        hard.append("settled_after_shift_end")
    if hard:
        raise NKTIdempotencyConflict(
            "Cashier Tender shift identity/time conflicts with observed business truth: "
            + ", ".join(hard)
        )


def _expected_rows(receipt) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen = set()
    for row in receipt.get("payments") or []:
        method = normalize_payment_method(row.payment_method)
        if method in {"Account", "Return Credit"}:
            raise NKTIdempotencyConflict(
                "Tender-derived Payment Receipt contains a non-cash-basis row that must not become a Cashier Movement."
            )
        if flt(row.amount) <= TOLERANCE:
            raise NKTIdempotencyConflict(
                "Tender-derived Payment Receipt contains a non-positive movement row."
            )
        source_row = str(row.name or "").strip()
        if not source_row or source_row in seen:
            raise NKTIdempotencyConflict("Payment Receipt movement source-row identity is invalid.")
        seen.add(source_row)
        rows.append(
            {
                "source_row": source_row,
                "payment_method": method,
                "amount": flt(row_collected_amount(row)),
                "settlement_amount": flt(row.amount),
                "card_surcharge": flt(row.get("card_surcharge")),
                "affects_cash_drawer": 1 if method == "Cash" else 0,
                "reference_number": str(row.reference_number or row.check_number or "").strip(),
            }
        )
    return rows


def _movement_rows_for_receipt(receipt_name: str):
    return frappe.get_all(
        MOVEMENT_DOCTYPE,
        filters={
            "source_doctype": PAYMENT_RECEIPT_DOCTYPE,
            "source_name": receipt_name,
            "docstatus": ["!=", 2],
        },
        fields=[
            "name", "docstatus", "status", "company", "posting_datetime",
            "cashier_shift", "settlement_location", "cashier", "movement_type",
            "direction", "payment_method", "amount", "settlement_amount",
            "card_surcharge", "affects_cash_drawer", "customer",
            "source_doctype", "source_name", "source_row", "reference_number",
        ],
        limit_page_length=0,
    )


def _assert_movement(row, expected: Dict[str, Any], journal, receipt_name: str) -> None:
    bad = []
    exact = {
        "company": journal.company,
        "cashier_shift": journal.cashier_shift,
        "settlement_location": journal.settlement_location,
        "cashier": journal.origin_user,
        "movement_type": "Customer Order Payment",
        "direction": "In",
        "payment_method": expected["payment_method"],
        "customer": journal.customer,
        "source_doctype": PAYMENT_RECEIPT_DOCTYPE,
        "source_name": receipt_name,
        "source_row": expected["source_row"],
        "reference_number": expected["reference_number"],
        "status": "Posted",
    }
    for fieldname, value in exact.items():
        if str(row.get(fieldname) or "") != str(value or ""):
            bad.append(fieldname)

    if cint(row.docstatus) != 1:
        bad.append("docstatus")
    if get_datetime(row.posting_datetime) != get_datetime(journal.settled_at):
        bad.append("posting_datetime")
    for fieldname in ("amount", "settlement_amount", "card_surcharge"):
        if abs(flt(row.get(fieldname)) - flt(expected[fieldname])) > TOLERANCE:
            bad.append(fieldname)
    if cint(row.affects_cash_drawer) != cint(expected["affects_cash_drawer"]):
        bad.append("affects_cash_drawer")

    if bad:
        raise NKTIdempotencyConflict(
            "Existing Cashier Movement conflicts with immutable Payment Receipt row "
            f"{expected['source_row']}: " + ", ".join(sorted(set(bad)))
        )


def _verify_exact_set(journal, receipt, expected_rows: List[Dict[str, Any]]) -> List[str]:
    existing = _movement_rows_for_receipt(receipt.name)
    expected_map = {row["source_row"]: row for row in expected_rows}
    existing_map: Dict[str, Any] = {}
    for row in existing:
        key = str(row.source_row or "").strip()
        if not key or key not in expected_map or key in existing_map:
            raise NKTIdempotencyConflict(
                "Cashier Movement source set contains an extra, blank, or duplicate Payment Receipt row identity."
            )
        existing_map[key] = row

    missing = sorted(set(expected_map) - set(existing_map))
    if missing:
        raise NKTIdempotencyConflict(
            "Cashier Movement materialization is incomplete for Payment Receipt rows: "
            + ", ".join(missing)
        )
    for key, expected in expected_map.items():
        _assert_movement(existing_map[key], expected, journal, receipt.name)
    return [existing_map[key].name for key in expected_map]


def _result(event_uuid: str, receipt_name: str | None, movement_names: List[str], replay: bool, not_required=False):
    return {
        "event_uuid": event_uuid,
        "payment_receipt": receipt_name,
        "cashier_movements": movement_names,
        "cashier_movement_count": len(movement_names),
        "cashier_movement_created": bool(movement_names) and not replay,
        "cashier_movement_not_required": bool(not_required),
        "replay": bool(replay),
        "materialization_state": NO_MOVEMENT_STATE if not_required else MOVEMENT_MATERIALIZED_STATE,
        "customer_advance_created": False,
        "customer_order_updated": False,
        "receivable_created": False,
        "warehouse_release_created": False,
        "stock_entry_created": False,
        "matching_executed": False,
    }


def materialize_cashier_movements_from_tender(event_uuid: str) -> Dict[str, Any]:
    _require_primary()
    event_uuid = _event_uuid(event_uuid)
    _acquire_lock(event_uuid)
    journal, payload = _load_locked_source(event_uuid)

    if str(journal.downstream_state or "") == NO_MOVEMENT_STATE:
        if journal.payment_receipt:
            raise NKTIdempotencyConflict(
                "Cashier Tender is marked movement-not-required but still points to a Payment Receipt."
            )
        if cint(journal.cashier_movement_count or 0) != 0 or not journal.cashier_movement_materialized_at:
            raise NKTIdempotencyConflict("Cashier Tender movement-not-required state is incomplete.")
        return _result(event_uuid, None, [], replay=True, not_required=True)

    if str(journal.downstream_state or "") == MOVEMENT_MATERIALIZED_STATE:
        if not journal.payment_receipt or not journal.cashier_movement_materialized_at:
            raise NKTIdempotencyConflict(
                "Cashier Tender movement-materialized state is missing its canonical binding/timestamp."
            )
        receipt = _load_bound_payment_receipt(journal, payload)
        _validate_shift_truth(journal)
        expected = _expected_rows(receipt)
        names = _verify_exact_set(journal, receipt, expected)
        if cint(journal.cashier_movement_count or 0) != len(names):
            raise NKTIdempotencyConflict("Cashier Tender stored movement count conflicts with canonical movements.")
        return _result(event_uuid, receipt.name, names, replay=True)

    state = str(journal.downstream_state or "")
    if state == NO_RECEIPT_STATE:
        if journal.payment_receipt:
            raise NKTIdempotencyConflict(
                "Payment Receipt Not Required state conflicts with a canonical Payment Receipt binding."
            )
        frappe.db.set_value(
            PRIMARY_JOURNAL_DOCTYPE,
            event_uuid,
            {
                "downstream_state": NO_MOVEMENT_STATE,
                "cashier_movement_materialized_at": now(),
                "cashier_movement_count": 0,
            },
            update_modified=False,
        )
        return _result(event_uuid, None, [], replay=False, not_required=True)

    if state != PAYMENT_MATERIALIZED_STATE:
        raise NKTIdempotencyConflict(
            "Cashier Tender is not at the C15C.10C movement materialization boundary."
        )

    receipt = _load_bound_payment_receipt(journal, payload)
    _validate_shift_truth(journal)
    expected = _expected_rows(receipt)
    if not expected:
        raise NKTIdempotencyConflict(
            "Payment Receipt exists but has no cash-basis rows for Cashier Movement materialization."
        )

    existing = _movement_rows_for_receipt(receipt.name)
    if existing:
        raise NKTIdempotencyConflict(
            "Cashier Movement rows already exist before the Tender reached the 10C materialized state."
        )

    for row in expected:
        movement = create_cashier_movement(
            company=journal.company,
            posting_datetime=journal.settled_at,
            cashier_shift=resolve_primary_cashier_shift(journal.cashier_shift),
            settlement_location=journal.settlement_location,
            cashier=journal.origin_user,
            movement_type="Customer Order Payment",
            direction="In",
            payment_method=row["payment_method"],
            amount=row["amount"],
            settlement_amount=row["settlement_amount"],
            card_surcharge=row["card_surcharge"],
            source_doctype=PAYMENT_RECEIPT_DOCTYPE,
            source_name=receipt.name,
            source_row=row["source_row"],
            customer=journal.customer,
            reference_number=row["reference_number"],
            remarks=(
                "Canonical Cashier Movement materialized from Tender-derived "
                f"Payment Receipt {receipt.name} / row {row['source_row']}."
            ),
            allow_closed_observed_shift=True,
            force_posted_status=True,
        )
        if not movement:
            raise NKTIdempotencyConflict("Expected Cashier Movement was not created.")

    names = _verify_exact_set(journal, receipt, expected)
    frappe.db.set_value(
        PRIMARY_JOURNAL_DOCTYPE,
        event_uuid,
        {
            "downstream_state": MOVEMENT_MATERIALIZED_STATE,
            "cashier_movement_materialized_at": now(),
            "cashier_movement_count": len(names),
        },
        update_modified=False,
    )
    return _result(event_uuid, receipt.name, names, replay=False)


@frappe.whitelist()
def materialize_cashier_movements(event_uuid):
    _require_rpc_materialization_authority()
    return materialize_cashier_movements_from_tender(event_uuid)
