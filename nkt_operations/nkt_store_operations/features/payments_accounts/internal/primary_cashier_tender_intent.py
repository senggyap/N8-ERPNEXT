from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import frappe
from frappe.utils import getdate, now

from nkt_operations.nkt_store_operations.features.payments_accounts.internal.cashier_tender_intent import (
    TENDER_INTENT_FAMILY,
    _canonical_cashier_tender_intent_json,
    _normalize_cashier_tender_intent_payload,
    _normalize_provider,
    _normalize_ref,
)
from nkt_operations.nkt_store_operations.features.cashier.internal.cashier_shift_alias import is_edge_shift_reference, preserved_edge_shift_identity
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    canonical_payload_hash,
    mark_primary_committed,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import (
    validate_transport_packet,
)

FOUNDATION_VERSION = "C15C.10A-R14"
PRIMARY_JOURNAL_DOCTYPE = "NKT Primary Cashier Tender Intent"
PRIMARY_CHECK_DOCTYPE = "NKT Primary Tender Check Identity"
PRIMARY_RECEIPT_DOCTYPE = "NKT Sync Primary Receipt"
MATERIALIZATION_STATE = "Tender Intent Preserved"
PRIMARY_ACK_NAMESPACE = uuid.UUID("f4dfb0aa-11f2-4f66-b6cb-7dfce0ce7a10")


def _runtime_role() -> str:
    return str(frappe.conf.get("nkt_runtime_role") or "Primary").strip()


def _require_primary() -> None:
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Primary Cashier Tender receiver unavailable.")


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _canonical_json(value: Dict[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _expected_primary_ack_uuid(event_uuid: str, payload_hash: str) -> str:
    event_uuid = _uuid(event_uuid, "Event UUID")
    payload_hash = str(payload_hash or "").strip().lower()
    if len(payload_hash) != 64 or any(ch not in "0123456789abcdef" for ch in payload_hash):
        raise frappe.ValidationError("Cashier Tender payload hash is invalid for ACK binding.")
    material = TENDER_INTENT_FAMILY + "\0" + event_uuid + "\0" + payload_hash
    return str(uuid.uuid5(PRIMARY_ACK_NAMESPACE, material))


def _check_identity_key(customer: str, bank_or_provider: str, check_number: str) -> str:
    raw = (
        str(customer or "").strip()
        + "\0"
        + _normalize_provider(bank_or_provider)
        + "\0"
        + _normalize_ref(check_number)
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _check_rows(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    rows = []
    for payment in payload.get("payments") or []:
        if payment.get("payment_method") != "Check":
            continue
        rows.append(
            {
                "identity_key": _check_identity_key(
                    payload.get("customer"),
                    payment.get("bank_or_provider"),
                    payment.get("check_number") or payment.get("reference_number"),
                ),
                "customer": str(payload.get("customer") or "").strip(),
                "bank_or_provider": str(payment.get("bank_or_provider") or "").strip(),
                "check_number": str(
                    payment.get("check_number") or payment.get("reference_number") or ""
                ).strip(),
            }
        )
    return rows


def _validate_against_posted_checks(payload: Dict[str, Any]) -> None:
    customer = str(payload.get("customer") or "").strip()
    for row in _check_rows(payload):
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
              AND REPLACE(
                    LOWER(TRIM(COALESCE(NULLIF(pd.check_number, ''), pd.reference_number, ''))),
                    ' ',
                    ''
                  ) = %s
            LIMIT 1
            """,
            (
                customer,
                "".join(_normalize_provider(row["bank_or_provider"]).split()),
                _normalize_ref(row["check_number"]),
            ),
        )
        if posted:
            raise frappe.ValidationError(
                "This physical Check is already recorded for the selected Customer."
            )



def _validate_against_preserved_checks(payload: Dict[str, Any]) -> None:
    for row in _check_rows(payload):
        existing = frappe.db.get_value(
            PRIMARY_CHECK_DOCTYPE,
            {"identity_key": row["identity_key"]},
            ["parent", "identity_key"],
            as_dict=True,
        )
        if existing:
            raise NKTIdempotencyConflict(
                "A physical Check in this Cashier Tender is already preserved on Primary."
            )


def _context_snapshot(envelope: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, str]:
    """
    Primary preservation must not discard physically observed money merely because
    a master was disabled/revoked/closed after the tender happened. Missing/current
    drift is therefore recorded for downstream controlled materialization.

    A present Cashier Shift whose immutable operator/company/location contradicts
    the tender is different: that is an identity conflict and must not be silently
    preserved as if it matched.
    """
    notes = []
    shift_name = str(payload.get("cashier_shift") or "").strip()
    if frappe.db.exists("NKT Cashier Shift", shift_name):
        shift = frappe.db.get_value(
            "NKT Cashier Shift",
            shift_name,
            ["cashier", "company", "settlement_location", "shift_start", "status", "docstatus"],
            as_dict=True,
        )
        hard = []
        if str(shift.cashier or "") != str(envelope.get("origin_user") or ""):
            hard.append("cashier")
        if str(shift.company or "") != str(payload.get("company") or ""):
            hard.append("company")
        if str(shift.settlement_location or "") != str(payload.get("settlement_location") or ""):
            hard.append("settlement_location")
        if shift.shift_start and getdate(shift.shift_start) != getdate(envelope.get("business_date")):
            hard.append("shift_business_date")
        if hard:
            raise NKTIdempotencyConflict(
                "Primary Cashier Shift identity conflicts with immutable tender: "
                + ", ".join(hard)
            )
        if str(shift.status or "") != "Open":
            notes.append(f"Cashier Shift is now {shift.status or 'unknown'}; observed tender is preserved.")
    else:
        preserved = preserved_edge_shift_identity(shift_name) if is_edge_shift_reference(shift_name) else None
        if preserved:
            hard = []
            if str(preserved["cashier"] or "") != str(envelope.get("origin_user") or ""):
                hard.append("cashier")
            if str(preserved["company"] or "") != str(payload.get("company") or ""):
                hard.append("company")
            if str(preserved["settlement_location"] or "") != str(payload.get("settlement_location") or ""):
                hard.append("settlement_location")
            if getdate(preserved["shift_start"]) != getdate(envelope.get("business_date")):
                hard.append("shift_business_date")
            if hard:
                raise NKTIdempotencyConflict(
                    "Primary preserved Store Edge Cashier Shift identity conflicts with immutable tender: "
                    + ", ".join(hard)
                )
            notes.append(
                "Cashier Shift is an offline Store Edge shift; canonical Primary shift "
                f"state is {preserved['materialization_state']}."
            )
        else:
            notes.append("Cashier Shift is not currently present on Primary.")

    for doctype, value, label in [
        ("User", envelope.get("origin_user"), "Origin User"),
        ("NKT Device Registry", envelope.get("origin_device"), "Origin Device"),
        ("Customer", payload.get("customer"), "Customer"),
        ("Company", payload.get("company"), "Company"),
        ("Warehouse", payload.get("settlement_location"), "Settlement Location"),
    ]:
        value = str(value or "").strip()
        if value and not frappe.db.exists(doctype, value):
            notes.append(f"{label} is not currently present on Primary.")

    return {
        "state": "Matched Current Masters" if not notes else "Master Drift / Missing",
        "note": " ".join(notes)[:1000],
    }


def _receipt_row_for_update(event_uuid: str):
    rows = frappe.db.sql(
        """
        SELECT
            name,event_uuid,event_family,primary_ack_uuid,envelope_sha256,payload_sha256,
            canonical_doctype,canonical_name,materialization_state,
            primary_received_at,primary_committed_at,result_code
        FROM `tabNKT Sync Primary Receipt`
        WHERE name=%s
        FOR UPDATE
        """,
        (event_uuid,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _journal_row_for_update(event_uuid: str):
    rows = frappe.db.sql(
        """
        SELECT
            name,event_uuid,event_family,event_action,envelope_sha256,payload_sha256,
            canonical_envelope_json,canonical_payload_json,preservation_state,
            downstream_state,primary_preserved_at
        FROM `tabNKT Primary Cashier Tender Intent`
        WHERE name=%s
        FOR UPDATE
        """,
        (event_uuid,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _claim_lock_name(event_uuid: str) -> str:
    return f"nkt-c15c10a-ti-{event_uuid}"


def _release_lock(lock_name: str) -> None:
    try:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
    except Exception:
        pass


def _register_lock_release(lock_name: str) -> None:
    state = {"released": False}

    def release_once():
        if state["released"]:
            return
        _release_lock(lock_name)
        state["released"] = True

    frappe.db.after_commit.add(release_once)
    frappe.db.after_rollback.add(release_once)


def _acquire_named_lock(lock_name: str, busy_message: str, timeout_seconds: int = 30) -> None:
    rows = frappe.db.sql(
        "SELECT GET_LOCK(%s,%s)",
        (lock_name, int(timeout_seconds)),
        as_list=True,
    )
    if not rows or int(rows[0][0] or 0) != 1:
        raise frappe.ValidationError(busy_message)
    _register_lock_release(lock_name)


def _check_claim_lock_name(identity_key: str) -> str:
    identity_key = str(identity_key or "").strip().lower()
    # MariaDB named lock identifiers are kept compact; 46 hex chars still preserve
    # 184 bits of collision resistance while fitting the 64-char lock-name budget.
    return "nkt-c15c10a-check-" + identity_key[:46]


def _acquire_check_claim_locks(payload: Dict[str, Any]) -> None:
    lock_names = sorted(
        {
            _check_claim_lock_name(row["identity_key"])
            for row in _check_rows(payload)
            if row.get("identity_key")
        }
    )
    acquired = []
    try:
        for lock_name in lock_names:
            _acquire_named_lock(
                lock_name,
                "Cashier Tender physical Check claim is busy. Safe retry is required.",
            )
            acquired.append(lock_name)
    except Exception:
        # Explicitly release any earlier named locks if a later one could not be acquired.
        # Registered commit/rollback callbacks may call RELEASE_LOCK again harmlessly.
        for lock_name in reversed(acquired):
            _release_lock(lock_name)
        raise


def _rollback_write_scope(write_savepoint: str, original_error: Exception) -> None:
    # MariaDB deadlock victims are rolled back automatically, which destroys all
    # savepoints. Never mask the original contention with "SAVEPOINT does not exist".
    if original_error.__class__.__name__ == "QueryDeadlockError":
        try:
            frappe.db.rollback()
        except Exception:
            pass
        return
    try:
        frappe.db.rollback(save_point=write_savepoint)
    except Exception as rollback_error:
        if "SAVEPOINT" in str(rollback_error).upper():
            try:
                frappe.db.rollback()
            except Exception:
                pass
            return
        raise


def _acquire_claim_lock(event_uuid: str) -> None:
    _acquire_named_lock(
        _claim_lock_name(event_uuid),
        "Cashier Tender sync is busy for this immutable Event UUID. Safe retry is required.",
    )


def _ack_from_preserved_rows(
    receipt,
    journal,
    envelope_hash: str,
    payload_hash: str,
    canonical_envelope: str,
    canonical_payload: str,
    *,
    replay: bool,
) -> Dict[str, Any]:
    bad = []
    expected_ack_uuid = _expected_primary_ack_uuid(str(receipt.name), payload_hash)
    if str(receipt.primary_ack_uuid or "") != expected_ack_uuid:
        bad.append("receipt.primary_ack_uuid")
    if str(receipt.event_family or "") != TENDER_INTENT_FAMILY:
        bad.append("receipt.event_family")
    if str(receipt.envelope_sha256 or "") != envelope_hash:
        bad.append("receipt.envelope_sha256")
    if str(receipt.payload_sha256 or "") != payload_hash:
        bad.append("receipt.payload_sha256")
    if str(receipt.canonical_doctype or "") != PRIMARY_JOURNAL_DOCTYPE:
        bad.append("receipt.canonical_doctype")
    if str(receipt.canonical_name or "") != str(receipt.name):
        bad.append("receipt.canonical_name")
    if str(receipt.materialization_state or "") != MATERIALIZATION_STATE:
        bad.append("receipt.materialization_state")

    if not journal:
        bad.append("primary_journal_missing")
    else:
        if str(journal.event_family or "") != TENDER_INTENT_FAMILY:
            bad.append("journal.event_family")
        if str(journal.envelope_sha256 or "") != envelope_hash:
            bad.append("journal.envelope_sha256")
        if str(journal.payload_sha256 or "") != payload_hash:
            bad.append("journal.payload_sha256")
        if str(journal.canonical_envelope_json or "") != canonical_envelope:
            bad.append("journal.canonical_envelope_json")
        if str(journal.canonical_payload_json or "") != canonical_payload:
            bad.append("journal.canonical_payload_json")
        if str(journal.preservation_state or "") != "Preserved":
            bad.append("journal.preservation_state")

    if bad:
        raise NKTIdempotencyConflict(
            "Primary Cashier Tender replay conflicts with immutable preserved content: "
            + ", ".join(bad)
        )

    return {
        "event_uuid": str(receipt.name),
        "event_family": TENDER_INTENT_FAMILY,
        "primary_ack_uuid": str(receipt.primary_ack_uuid),
        "payload_sha256": str(receipt.payload_sha256),
        "result_code": str(receipt.result_code),
        "committed": True,
        "replay": bool(replay),
        "canonical_doctype": PRIMARY_JOURNAL_DOCTYPE,
        "canonical_name": str(receipt.name),
        "materialization_state": MATERIALIZATION_STATE,
        "canonical_cashier_sale_created": False,
        "payment_receipt_created": False,
        "cashier_movement_created": False,
        "receivable_created": False,
        "warehouse_release_created": False,
        "stock_entry_created": False,
        "matching_executed": False,
    }


def receive_cashier_tender_intent_at_primary(packet: Dict[str, Any]) -> Dict[str, Any]:
    _require_primary()
    if isinstance(packet, str):
        packet = frappe.parse_json(packet)

    envelope, payload, envelope_hash, payload_hash = validate_transport_packet(
        packet,
        expected_family=TENDER_INTENT_FAMILY,
    )
    event_uuid = _uuid(envelope["event_uuid"], "Event UUID")
    canonical_envelope = _canonical_json(envelope)
    canonical_payload = _canonical_cashier_tender_intent_json(payload)

    _acquire_claim_lock(event_uuid)

    receipt = _receipt_row_for_update(event_uuid)
    if receipt:
        journal = _journal_row_for_update(event_uuid)
        return _ack_from_preserved_rows(
            receipt,
            journal,
            envelope_hash,
            payload_hash,
            canonical_envelope,
            canonical_payload,
            replay=True,
        )

    orphan = _journal_row_for_update(event_uuid)
    if orphan:
        raise NKTIdempotencyConflict(
            "Primary Cashier Tender journal exists without its immutable Primary receipt."
        )

    _validate_against_posted_checks(payload)
    _acquire_check_claim_locks(payload)
    _validate_against_preserved_checks(payload)
    context = _context_snapshot(envelope, payload)

    write_savepoint = "c15c10a_tender_write_" + event_uuid.replace("-", "")[:24]
    frappe.db.savepoint(write_savepoint)

    journal_doc = frappe.get_doc(
        {
            "doctype": PRIMARY_JOURNAL_DOCTYPE,
            "event_uuid": event_uuid,
            "event_family": TENDER_INTENT_FAMILY,
            "event_action": envelope["event_action"],
            "origin_device": envelope["origin_device"],
            "origin_user": envelope["origin_user"],
            "operational_context": envelope["operational_context"],
            "business_date": envelope["business_date"],
            "settled_at": envelope["settled_at"],
            "client_created_at": envelope.get("client_created_at"),
            "company": payload["company"],
            "customer": payload["customer"],
            "cashier_shift": payload["cashier_shift"],
            "settlement_location": payload["settlement_location"],
            "default_warehouse": payload["default_warehouse"],
            "merchandise_total": payload["merchandise_total"],
            "payment_settlement_total": payload["payment_settlement_total"],
            "card_surcharge_total": payload["card_surcharge_total"],
            "actual_collected_total": payload["actual_collected_total"],
            "envelope_sha256": envelope_hash,
            "payload_sha256": payload_hash,
            "canonical_envelope_json": canonical_envelope,
            "canonical_payload_json": canonical_payload,
            "preservation_state": "Preserved",
            "downstream_state": "Awaiting Controlled Cashier Sale Materialization",
            "context_validation_state": context["state"],
            "context_validation_note": context["note"],
            "primary_preserved_at": now(),
            "check_identities": _check_rows(payload),
        }
    )
    try:
        journal_doc.insert(ignore_permissions=True)
    except Exception as exc:
        is_unique = isinstance(exc, frappe.DuplicateEntryError) or (
            exc.__class__.__name__ == "UniqueValidationError"
        )
        is_check_identity = "identity_key" in str(exc)
        _rollback_write_scope(write_savepoint, exc)
        if is_unique and is_check_identity:
            raise NKTIdempotencyConflict(
                "A physical Check in this Cashier Tender is already preserved on Primary."
            ) from exc
        raise

    receipt_doc = frappe.get_doc(
        {
            "doctype": PRIMARY_RECEIPT_DOCTYPE,
            "event_uuid": event_uuid,
            "event_family": TENDER_INTENT_FAMILY,
            "primary_ack_uuid": _expected_primary_ack_uuid(event_uuid, payload_hash),
            "envelope_sha256": envelope_hash,
            "payload_sha256": payload_hash,
            "canonical_doctype": PRIMARY_JOURNAL_DOCTYPE,
            "canonical_name": event_uuid,
            "materialization_state": MATERIALIZATION_STATE,
            "primary_received_at": now(),
            "primary_committed_at": now(),
            "result_code": "Committed",
        }
    )
    try:
        receipt_doc.insert(ignore_permissions=True)
    except Exception as exc:
        _rollback_write_scope(write_savepoint, exc)
        raise

    receipt = _receipt_row_for_update(event_uuid)
    journal = _journal_row_for_update(event_uuid)
    return _ack_from_preserved_rows(
        receipt,
        journal,
        envelope_hash,
        payload_hash,
        canonical_envelope,
        canonical_payload,
        replay=False,
    )


@frappe.whitelist()
def receive_cashier_tender_intent(packet):
    return receive_cashier_tender_intent_at_primary(packet)


def apply_cashier_tender_intent_ack_at_edge(ack: Dict[str, Any]) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Cashier Tender Intent ACK unavailable.")
    if not isinstance(ack, dict):
        raise frappe.ValidationError("Cashier Tender Intent ACK is invalid.")

    event_uuid = _uuid(ack.get("event_uuid"), "Event UUID")
    ack_uuid = _uuid(ack.get("primary_ack_uuid"), "Primary ACK UUID")
    payload_hash = str(ack.get("payload_sha256") or "").lower()
    expected_ack_uuid = _expected_primary_ack_uuid(event_uuid, payload_hash)
    if ack_uuid != expected_ack_uuid:
        raise NKTIdempotencyConflict(
            "Cashier Tender Intent ACK UUID is not the deterministic Primary ACK for this immutable tender."
        )

    if ack.get("committed") is not True or ack.get("result_code") != "Committed":
        raise frappe.ValidationError("Cashier Tender Intent ACK is not committed.")
    if ack.get("event_family") != TENDER_INTENT_FAMILY:
        raise frappe.ValidationError("Cashier Tender Intent ACK family is invalid.")
    if ack.get("canonical_doctype") != PRIMARY_JOURNAL_DOCTYPE:
        raise frappe.ValidationError("Cashier Tender Intent ACK journal identity is invalid.")
    if str(ack.get("canonical_name") or "") != event_uuid:
        raise frappe.ValidationError("Cashier Tender Intent ACK journal name is invalid.")
    if ack.get("materialization_state") != MATERIALIZATION_STATE:
        raise frappe.ValidationError("Cashier Tender Intent ACK preservation state is invalid.")

    if not frappe.db.exists("NKT Sync Event", event_uuid):
        raise frappe.DoesNotExistError("Cashier Tender Intent event is unavailable.")
    event = frappe.get_doc("NKT Sync Event", event_uuid)
    if event.event_family != TENDER_INTENT_FAMILY:
        raise frappe.ValidationError("Cashier Tender Intent event family mismatch.")
    if event.payload_sha256 != payload_hash:
        raise NKTIdempotencyConflict(
            "Cashier Tender Intent ACK payload hash conflicts with immutable tender."
        )

    bound = str(event.primary_ack_uuid or "").strip()
    if event.sync_state == "Committed at Primary":
        if (
            bound == ack_uuid
            and event.canonical_doctype == PRIMARY_JOURNAL_DOCTYPE
            and event.canonical_name == event_uuid
        ):
            return {
                "event_uuid": event_uuid,
                "primary_ack_uuid": ack_uuid,
                "canonical_doctype": PRIMARY_JOURNAL_DOCTYPE,
                "canonical_name": event_uuid,
                "sync_state": "Committed at Primary",
                "pending_payload_purged": False,
                "replay": True,
            }
        raise NKTIdempotencyConflict(
            "Committed Cashier Tender Intent conflicts with the supplied Primary ACK."
        )

    if bound and bound != ack_uuid:
        raise NKTIdempotencyConflict(
            "Cashier Tender Intent ACK UUID conflicts with the ACK already bound to this event."
        )

    pending_name = frappe.db.exists("NKT Sync Pending Payload", event_uuid)
    if not pending_name:
        raise frappe.ValidationError(
            "Cashier Tender Intent ACK arrived without its pending payload."
        )
    pending = frappe.get_doc("NKT Sync Pending Payload", pending_name)
    if (
        pending.event_family != TENDER_INTENT_FAMILY
        or pending.payload_sha256 != payload_hash
    ):
        raise NKTIdempotencyConflict(
            "Cashier Tender Intent ACK conflicts with pending immutable tender."
        )

    mark_primary_committed(
        event_uuid,
        PRIMARY_JOURNAL_DOCTYPE,
        event_uuid,
        primary_ack_uuid=ack_uuid,
    )
    frappe.delete_doc(
        "NKT Sync Pending Payload",
        pending.name,
        ignore_permissions=True,
        force=True,
    )

    return {
        "event_uuid": event_uuid,
        "primary_ack_uuid": ack_uuid,
        "canonical_doctype": PRIMARY_JOURNAL_DOCTYPE,
        "canonical_name": event_uuid,
        "sync_state": "Committed at Primary",
        "pending_payload_purged": True,
        "replay": False,
    }
