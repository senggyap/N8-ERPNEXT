from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List

import frappe
from frappe.utils import flt, now

from nkt_operations.nkt_store_operations.features.payments_accounts.internal.cashier_tender_intent import (
    TENDER_INTENT_FAMILY,
    _canonical_cashier_tender_intent_json,
    _normalize_cashier_tender_intent_payload,
)
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    canonical_payload_hash,
)

FOUNDATION_VERSION = "C15C.10B-R3"
PRIMARY_JOURNAL_DOCTYPE = "NKT Primary Cashier Tender Intent"
PRIMARY_RECEIPT_DOCTYPE = "NKT Sync Primary Receipt"
PAYMENT_RECEIPT_DOCTYPE = "NKT Payment Receipt"
TENDER_PRESERVED_STATE = "Tender Intent Preserved"
PAYMENT_MATERIALIZED_STATE = "Payment Receipt Materialized - Awaiting Cashier Movement"
NO_RECEIPT_STATE = "Payment Receipt Not Required - Awaiting Later Settlement Family"


def _runtime_role() -> str:
    return str(frappe.conf.get("nkt_runtime_role") or "Primary").strip()


def _require_primary() -> None:
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Payment Receipt Tender materialization is unavailable.")


def _require_rpc_materialization_authority() -> None:
    user = str(frappe.session.user or "").strip()
    roles = set(frappe.get_roles(user))
    if user == "Administrator" or "System Manager" in roles:
        return
    raise frappe.PermissionError("Payment Receipt Tender materialization is unavailable.")


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
    lock_name = "nkt-c15c10b-pr-" + event_uuid
    rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (lock_name, 30), as_list=True)
    if not rows or int(rows[0][0] or 0) != 1:
        raise frappe.ValidationError(
            "Payment Receipt materialization is busy for this immutable Cashier Tender."
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
            payment_settlement_total,card_surcharge_total,actual_collected_total,
            payload_sha256,canonical_payload_json,preservation_state,
            downstream_state,payment_receipt,payment_receipt_materialized_at
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

    receipt = frappe.db.get_value(
        PRIMARY_RECEIPT_DOCTYPE,
        event_uuid,
        [
            "name",
            "event_family",
            "payload_sha256",
            "canonical_doctype",
            "canonical_name",
            "materialization_state",
            "result_code",
        ],
        as_dict=True,
    )
    if not receipt:
        raise NKTIdempotencyConflict(
            "Primary Cashier Tender journal exists without its immutable Sync Primary Receipt."
        )

    bad = []
    if str(journal.event_family or "") != TENDER_INTENT_FAMILY:
        bad.append("journal.event_family")
    if str(journal.preservation_state or "") != "Preserved":
        bad.append("journal.preservation_state")
    if str(receipt.event_family or "") != TENDER_INTENT_FAMILY:
        bad.append("receipt.event_family")
    if str(receipt.payload_sha256 or "") != str(journal.payload_sha256 or ""):
        bad.append("receipt.payload_sha256")
    if str(receipt.canonical_doctype or "") != PRIMARY_JOURNAL_DOCTYPE:
        bad.append("receipt.canonical_doctype")
    if str(receipt.canonical_name or "") != event_uuid:
        bad.append("receipt.canonical_name")
    if str(receipt.materialization_state or "") != TENDER_PRESERVED_STATE:
        bad.append("receipt.materialization_state")
    if str(receipt.result_code or "") != "Committed":
        bad.append("receipt.result_code")
    if bad:
        raise NKTIdempotencyConflict(
            "Cashier Tender is not eligible for Payment Receipt materialization: "
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


def _cash_basis_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        dict(row)
        for row in (payload.get("payments") or [])
        if row.get("payment_method") not in {"Account", "Return Credit"}
        and flt(row.get("amount")) > 0.005
    ]


def _build_receipt(journal, payload: Dict[str, Any], rows: List[Dict[str, Any]]):
    receipt = frappe.get_doc(
        {
            "doctype": PAYMENT_RECEIPT_DOCTYPE,
            "company": journal.company,
            "receipt_datetime": journal.settled_at,
            "payment_purpose": "Cashier Sale Payment",
            "customer": journal.customer,
            "received_by": journal.origin_user,
            "encoded_by": journal.origin_user,
            "source_primary_tender_intent": journal.name,
            "source_tender_payload_sha256": journal.payload_sha256,
            "downstream_effects_state": "Deferred - C15C.10C/10D",
            "allocation_status": "Unallocated - Awaiting Encoder",
            "remarks": (
                "Canonical cash-basis Payment Receipt materialized from preserved "
                f"Cashier Tender {journal.name}. Downstream effects remain deferred."
            ),
        }
    )
    for row in rows:
        receipt.append(
            "payments",
            {
                "payment_method": row.get("payment_method"),
                "amount": row.get("amount"),
                "card_surcharge": row.get("card_surcharge"),
                "collected_amount": row.get("collected_amount"),
                "cash_tendered": row.get("cash_tendered"),
                "change_amount": row.get("change_amount"),
                "reference_number": row.get("reference_number"),
                "bank_or_provider": row.get("bank_or_provider"),
                "check_number": row.get("check_number"),
                "check_date": row.get("check_date"),
                "verification_status": "Not Required",
                "affects_cash_drawer": 1 if row.get("payment_method") == "Cash" else 0,
                "remarks": row.get("remarks"),
            },
        )
    return receipt


def _assert_existing_receipt(journal, payload: Dict[str, Any], receipt_name: str) -> None:
    if not frappe.db.exists(PAYMENT_RECEIPT_DOCTYPE, receipt_name):
        raise NKTIdempotencyConflict(
            "Primary Tender points to a missing Payment Receipt."
        )
    doc = frappe.get_doc(PAYMENT_RECEIPT_DOCTYPE, receipt_name)
    if doc.docstatus != 1:
        raise NKTIdempotencyConflict(
            "Tender-derived Payment Receipt is not submitted."
        )
    if str(doc.source_primary_tender_intent or "") != str(journal.name):
        raise NKTIdempotencyConflict(
            "Payment Receipt source Tender identity conflicts."
        )
    if str(doc.source_tender_payload_sha256 or "") != str(journal.payload_sha256 or ""):
        raise NKTIdempotencyConflict(
            "Payment Receipt source Tender hash conflicts."
        )
    # Reuse the controller's immutable source-row validator.
    doc.validate_primary_tender_binding()


def _result(event_uuid: str, receipt_name: str | None, replay: bool, not_required: bool = False):
    return {
        "event_uuid": event_uuid,
        "payment_receipt": receipt_name,
        "payment_receipt_created": bool(receipt_name) and not replay,
        "payment_receipt_not_required": bool(not_required),
        "replay": bool(replay),
        "materialization_state": (
            NO_RECEIPT_STATE if not_required else PAYMENT_MATERIALIZED_STATE
        ),
        "cashier_movement_created": False,
        "customer_advance_created": False,
        "customer_order_updated": False,
        "receivable_created": False,
        "warehouse_release_created": False,
        "stock_entry_created": False,
        "matching_executed": False,
    }


def materialize_payment_receipt_from_tender(event_uuid: str) -> Dict[str, Any]:
    _require_primary()
    event_uuid = _event_uuid(event_uuid)
    _acquire_lock(event_uuid)
    journal, payload = _load_locked_source(event_uuid)

    existing = frappe.db.get_value(
        PAYMENT_RECEIPT_DOCTYPE,
        {
            "source_primary_tender_intent": event_uuid,
            "docstatus": ["!=", 2],
        },
        "name",
    )

    rows = _cash_basis_rows(payload)

    if str(journal.downstream_state or "") == NO_RECEIPT_STATE:
        if journal.payment_receipt or existing:
            raise NKTIdempotencyConflict(
                "Primary Tender is marked Payment Receipt Not Required but a canonical receipt exists."
            )
        if rows:
            raise NKTIdempotencyConflict(
                "Primary Tender is marked Payment Receipt Not Required but contains cash-basis payments."
            )
        if not journal.payment_receipt_materialized_at:
            raise NKTIdempotencyConflict(
                "Primary Tender Payment Receipt Not Required state is missing its materialization timestamp."
            )
        return _result(event_uuid, None, replay=True, not_required=True)

    if (
        str(journal.downstream_state or "") == PAYMENT_MATERIALIZED_STATE
        and not journal.payment_receipt
        and not existing
    ):
        raise NKTIdempotencyConflict(
            "Primary Tender says Payment Receipt was materialized but the canonical receipt binding is missing."
        )

    if journal.payment_receipt:
        if not existing or str(existing) != str(journal.payment_receipt):
            raise NKTIdempotencyConflict(
                "Primary Tender Payment Receipt binding conflicts with canonical receipt state."
            )
        _assert_existing_receipt(journal, payload, existing)
        return _result(event_uuid, existing, replay=True)

    if existing:
        _assert_existing_receipt(journal, payload, existing)
        frappe.db.set_value(
            PRIMARY_JOURNAL_DOCTYPE,
            event_uuid,
            {
                "payment_receipt": existing,
                "payment_receipt_materialized_at": now(),
                "downstream_state": PAYMENT_MATERIALIZED_STATE,
            },
            update_modified=False,
        )
        return _result(event_uuid, existing, replay=True)

    if not rows:
        frappe.db.set_value(
            PRIMARY_JOURNAL_DOCTYPE,
            event_uuid,
            {
                "payment_receipt": None,
                "payment_receipt_materialized_at": now(),
                "downstream_state": NO_RECEIPT_STATE,
            },
            update_modified=False,
        )
        return _result(event_uuid, None, replay=False, not_required=True)

    receipt = _build_receipt(journal, payload, rows)
    receipt.flags.ignore_permissions = True
    receipt.insert(ignore_permissions=True)
    receipt.submit()

    frappe.db.set_value(
        PRIMARY_JOURNAL_DOCTYPE,
        event_uuid,
        {
            "payment_receipt": receipt.name,
            "payment_receipt_materialized_at": now(),
            "downstream_state": PAYMENT_MATERIALIZED_STATE,
        },
        update_modified=False,
    )

    return _result(event_uuid, receipt.name, replay=False)


@frappe.whitelist()
def materialize_payment_receipt(event_uuid):
    _require_rpc_materialization_authority()
    return materialize_payment_receipt_from_tender(event_uuid)
