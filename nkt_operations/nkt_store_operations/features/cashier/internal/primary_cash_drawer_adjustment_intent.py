from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict

import frappe
from frappe.utils import flt, now

from nkt_operations.nkt_store_operations.features.cashier.internal.cash_drawer_adjustment_intent import (
    CASH_DRAWER_ADJUSTMENT_INTENT_ACTION,
    CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY,
    _canonical_cash_drawer_adjustment_intent_json,
    _normalize_cash_drawer_adjustment_intent_payload,
)
from nkt_operations.nkt_store_operations.features.cashier.internal.cashier_shift_alias import is_edge_shift_reference, preserved_edge_shift_identity
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    mark_primary_committed,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import (
    _runtime_role,
    prepare_event_for_primary,
    validate_transport_packet,
)

FOUNDATION_VERSION = "C15C.10F-R3"
PRIMARY_JOURNAL = "NKT Primary Cash Drawer Adjustment Intent"
MATERIALIZATION_STATE = "Cash Drawer Adjustment Intent Preserved"
PRIMARY_ACK_NAMESPACE = uuid.UUID("c6e8a6a8-59ab-4c5b-baf6-7aa59d568a76")
TOLERANCE = 0.000001


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
    payload_hash = str(payload_hash or "").lower()
    if len(payload_hash) != 64 or any(c not in "0123456789abcdef" for c in payload_hash):
        raise frappe.ValidationError("Payload hash is invalid.")
    material = CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY + "\0" + event_uuid + "\0" + payload_hash
    return str(uuid.uuid5(PRIMARY_ACK_NAMESPACE, material))


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Cash Drawer Adjustment Intent Primary receiver unavailable.")


def _claim_name(kind: str, identity: str) -> str:
    return "nkt-10f-cash-" + hashlib.sha256(
        f"{kind}:{identity}".encode("utf-8")
    ).hexdigest()[:36]


def _release_lock(name: str):
    try:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
    except Exception:
        pass


def _acquire_claims(event_uuid: str, cashier_shift: str):
    names = sorted({
        _claim_name("event", event_uuid),
        _claim_name("cashier-shift", cashier_shift),
    })
    acquired = []
    try:
        for name in names:
            rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (name, 30), as_list=True)
            if not rows or int(rows[0][0] or 0) != 1:
                raise frappe.ValidationError(
                    "Cash Drawer Adjustment Intent preservation is busy. Safe retry is required."
                )
            acquired.append(name)
    except Exception:
        for name in reversed(acquired):
            _release_lock(name)
        raise

    state = {"released": False}
    def release_once():
        if state["released"]:
            return
        for name in reversed(acquired):
            _release_lock(name)
        state["released"] = True
    frappe.db.after_commit.add(release_once)
    frappe.db.after_rollback.add(release_once)


def _receipt_for_update(event_uuid: str):
    rows = frappe.db.sql(
        "SELECT name FROM `tabNKT Sync Primary Receipt` WHERE name=%s FOR UPDATE",
        (event_uuid,),
        as_dict=True,
    )
    return frappe.get_doc("NKT Sync Primary Receipt", event_uuid) if rows else None


def _journal_for_update(event_uuid: str):
    rows = frappe.db.sql(
        f"SELECT name FROM `tab{PRIMARY_JOURNAL}` WHERE name=%s FOR UPDATE",
        (event_uuid,),
        as_dict=True,
    )
    return frappe.get_doc(PRIMARY_JOURNAL, event_uuid) if rows else None


def _validate_shift_identity(payload: Dict[str, Any], envelope: Dict[str, Any]) -> Dict[str, Any]:
    shift_name = payload["cashier_shift"]
    if not frappe.db.exists("NKT Cashier Shift", shift_name):
        preserved = preserved_edge_shift_identity(shift_name) if is_edge_shift_reference(shift_name) else None
        if not preserved:
            raise NKTIdempotencyConflict(
                "Cash Drawer Adjustment Intent references a Cashier Shift missing at Primary. "
                "If this is a Store Edge shift, sync/preserve its Shift Open event first and retry."
            )
        if str(preserved["cashier"] or "") != str(envelope.get("origin_user") or ""):
            roles = set(frappe.get_roles(envelope.get("origin_user")) or [])
            if not (roles & {"NKT OWNER","NKT ADMINISTRATOR","System Manager"}) and str(envelope.get("origin_user")) != "Administrator":
                raise NKTIdempotencyConflict(
                    "Cash Drawer Adjustment Intent origin user conflicts with the preserved Store Edge shift cashier."
                )
        return {
            "company": str(preserved["company"] or ""),
            "settlement_location": str(preserved["settlement_location"] or ""),
            "cashier": str(preserved["cashier"] or ""),
            "current_shift_status": str(preserved["materialization_state"] or ""),
            "current_shift_docstatus": 0,
        }

    shift = frappe.get_doc("NKT Cashier Shift", shift_name)
    if str(shift.cashier or "") != str(envelope.get("origin_user") or ""):
        roles = set(frappe.get_roles(envelope.get("origin_user")) or [])
        if not (roles & {"NKT OWNER","NKT ADMINISTRATOR","System Manager"}):
            raise NKTIdempotencyConflict(
                "Cash Drawer Adjustment Intent origin user conflicts with the Primary shift cashier."
            )
    return {
        "company": str(shift.company or ""),
        "settlement_location": str(shift.settlement_location or ""),
        "cashier": str(shift.cashier or ""),
        "current_shift_status": str(shift.status or ""),
        "current_shift_docstatus": int(shift.docstatus or 0),
    }


def _journal_conflicts(journal, envelope, payload, envelope_hash, payload_hash, shift_identity):
    bad = []
    checks = {
        "event_family": CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY,
        "event_action": CASH_DRAWER_ADJUSTMENT_INTENT_ACTION,
        "origin_device": envelope.get("origin_device"),
        "origin_user": envelope.get("origin_user"),
        "operational_context": envelope.get("operational_context"),
        "cashier_shift": payload.get("cashier_shift"),
        "company": shift_identity.get("company"),
        "settlement_location": shift_identity.get("settlement_location"),
        "cashier": shift_identity.get("cashier"),
        "adjustment_type": payload.get("adjustment_type"),
        "direction": payload.get("direction"),
        "party_name": payload.get("party_name"),
        "purpose": payload.get("purpose"),
        "supporting_document": payload.get("supporting_document"),
        "envelope_sha256": envelope_hash,
        "payload_sha256": payload_hash,
    }
    for field, expected in checks.items():
        if str(journal.get(field) or "") != str(expected or ""):
            bad.append(field)
    if abs(flt(journal.amount) - flt(payload.get("amount"))) > TOLERANCE:
        bad.append("amount")
    if abs(flt(journal.denomination_total) - flt(payload.get("denomination_total"))) > TOLERANCE:
        bad.append("denomination_total")
    if str(journal.denominations_json or "") != _canonical_json(payload.get("denominations") or {}):
        bad.append("denominations_json")
    if str(journal.canonical_envelope_json or "") != _canonical_json(envelope):
        bad.append("canonical_envelope_json")
    if str(journal.canonical_payload_json or "") != _canonical_cash_drawer_adjustment_intent_json(payload):
        bad.append("canonical_payload_json")
    return bad


def prepare_cash_drawer_adjustment_intent_for_primary(event_uuid: str):
    return prepare_event_for_primary(
        event_uuid,
        expected_family=CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY,
    )


def receive_cash_drawer_adjustment_intent_at_primary(packet: Dict[str, Any]) -> Dict[str, Any]:
    _require_primary()
    envelope, payload, envelope_hash, payload_hash = validate_transport_packet(
        packet,
        expected_family=CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY,
    )
    event_uuid = envelope["event_uuid"]
    payload = _normalize_cash_drawer_adjustment_intent_payload(payload)

    _acquire_claims(event_uuid, payload["cashier_shift"])
    shift_identity = _validate_shift_identity(payload, envelope)
    ack_uuid = _expected_primary_ack_uuid(event_uuid, payload_hash)

    existing = _journal_for_update(event_uuid)
    receipt = _receipt_for_update(event_uuid)
    if existing:
        bad = _journal_conflicts(
            existing, envelope, payload, envelope_hash, payload_hash, shift_identity
        )
        if bad:
            raise NKTIdempotencyConflict(
                "Primary Cash Drawer Adjustment Intent replay conflicts with immutable content: "
                + ", ".join(sorted(set(bad)))
            )
        if not receipt:
            raise NKTIdempotencyConflict(
                "Primary Cash Drawer Adjustment Intent exists without its durable receipt."
            )
        if (
            receipt.event_family != CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY
            or receipt.payload_sha256 != payload_hash
            or receipt.envelope_sha256 != envelope_hash
            or str(receipt.primary_ack_uuid or "") != ack_uuid
            or receipt.materialization_state != MATERIALIZATION_STATE
            or str(receipt.canonical_doctype or "") != PRIMARY_JOURNAL
            or str(receipt.canonical_name or "") != existing.name
        ):
            raise NKTIdempotencyConflict(
                "Primary Cash Drawer Adjustment Intent receipt conflicts with immutable journal."
            )
        return {
            "event_uuid": event_uuid,
            "event_family": CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY,
            "primary_ack_uuid": ack_uuid,
            "payload_sha256": payload_hash,
            "result_code": "Committed",
            "committed": True,
            "replay": True,
            "primary_journal": existing.name,
            "cash_drawer_adjustment_created": False,
            "cashier_movement_created": False,
        }

    if receipt:
        raise NKTIdempotencyConflict(
            "Primary receipt exists without its Cash Drawer Adjustment Intent journal."
        )

    journal = frappe.get_doc({
        "doctype": PRIMARY_JOURNAL,
        "event_uuid": event_uuid,
        "event_family": CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY,
        "event_action": CASH_DRAWER_ADJUSTMENT_INTENT_ACTION,
        "origin_device": envelope["origin_device"],
        "origin_user": envelope["origin_user"],
        "operational_context": envelope["operational_context"],
        "business_date": envelope["business_date"],
        "settled_at": envelope["settled_at"],
        "client_created_at": envelope.get("client_created_at"),
        "cashier_shift": payload["cashier_shift"],
        "company": shift_identity["company"],
        "settlement_location": shift_identity["settlement_location"],
        "cashier": shift_identity["cashier"],
        "adjustment_type": payload["adjustment_type"],
        "direction": payload["direction"],
        "amount": payload["amount"],
        "party_name": payload["party_name"],
        "purpose": payload["purpose"],
        "supporting_document": payload["supporting_document"],
        "denomination_total": payload["denomination_total"],
        "denominations_json": _canonical_json(payload["denominations"]),
        "envelope_sha256": envelope_hash,
        "payload_sha256": payload_hash,
        "canonical_envelope_json": _canonical_json(envelope),
        "canonical_payload_json": _canonical_cash_drawer_adjustment_intent_json(payload),
        "preservation_state": "Preserved",
        "downstream_state": "Awaiting Cash Drawer Materialization",
        "primary_preserved_at": now(),
    })
    journal.insert(ignore_permissions=True)

    receipt = frappe.get_doc({
        "doctype": "NKT Sync Primary Receipt",
        "event_uuid": event_uuid,
        "event_family": CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY,
        "primary_ack_uuid": ack_uuid,
        "envelope_sha256": envelope_hash,
        "payload_sha256": payload_hash,
        "canonical_doctype": PRIMARY_JOURNAL,
        "canonical_name": journal.name,
        "primary_received_at": now(),
        "primary_committed_at": now(),
        "result_code": "Committed",
        "materialization_state": MATERIALIZATION_STATE,
    })
    receipt.insert(ignore_permissions=True)

    return {
        "event_uuid": event_uuid,
        "event_family": CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY,
        "primary_ack_uuid": ack_uuid,
        "payload_sha256": payload_hash,
        "result_code": "Committed",
        "committed": True,
        "replay": False,
        "primary_journal": journal.name,
        "cash_drawer_adjustment_created": False,
        "cashier_movement_created": False,
    }


def apply_cash_drawer_adjustment_intent_ack_at_edge(ack: Dict[str, Any]) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Cash Drawer Adjustment Intent ACK unavailable.")
    if not isinstance(ack, dict):
        raise frappe.ValidationError("Primary ACK is invalid.")

    event_uuid = _uuid(ack.get("event_uuid"), "Event UUID")
    ack_uuid = _uuid(ack.get("primary_ack_uuid"), "Primary ACK UUID")
    payload_hash = str(ack.get("payload_sha256") or "").lower()
    if (
        ack.get("committed") is not True
        or ack.get("result_code") != "Committed"
        or ack.get("event_family") != CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY
    ):
        raise frappe.ValidationError("Primary ACK is not a committed Cash Drawer Adjustment Intent ACK.")

    if not frappe.db.exists("NKT Sync Event", event_uuid):
        raise frappe.DoesNotExistError("Cash Drawer Adjustment event is unavailable.")
    event = frappe.get_doc("NKT Sync Event", event_uuid)
    if event.event_family != CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY:
        raise NKTIdempotencyConflict("Primary ACK family conflicts with immutable event.")
    if event.payload_sha256 != payload_hash:
        raise NKTIdempotencyConflict("Primary ACK payload hash conflicts with immutable event.")

    expected_ack = _expected_primary_ack_uuid(event_uuid, payload_hash)
    if ack_uuid != expected_ack:
        raise NKTIdempotencyConflict("Primary ACK UUID is not the deterministic ACK for this event.")

    projection = frappe.get_doc("NKT Edge Cash Drawer Adjustment Projection", event_uuid)
    bound = str(projection.primary_ack_uuid or "").strip()
    if bound and bound != ack_uuid:
        raise NKTIdempotencyConflict("Edge cash-drawer projection already binds a different Primary ACK.")

    pending = frappe.db.exists("NKT Sync Pending Payload", event_uuid)
    if pending:
        pending_doc = frappe.get_doc("NKT Sync Pending Payload", pending)
        if pending_doc.payload_sha256 != payload_hash:
            raise NKTIdempotencyConflict("Primary ACK conflicts with pending cash-drawer payload.")
        if event.sync_state not in ("Accepted at Edge","Awaiting Primary"):
            raise frappe.ValidationError("Pending cash-drawer event is not eligible for Primary ACK.")

        mark_primary_committed(
            event_uuid,
            "NKT Primary Cash Drawer Adjustment Intent",
            event_uuid,
            primary_ack_uuid=ack_uuid,
        )
        frappe.db.set_value(
            "NKT Edge Cash Drawer Adjustment Projection",
            event_uuid,
            {
                "projection_state": "Primary Preserved",
                "primary_ack_uuid": ack_uuid,
            },
            update_modified=False,
        )
        frappe.delete_doc(
            "NKT Sync Pending Payload",
            pending_doc.name,
            ignore_permissions=True,
            force=True,
        )
        return {
            "event_uuid": event_uuid,
            "primary_ack_uuid": ack_uuid,
            "sync_state": "Committed at Primary",
            "projection_state": "Primary Preserved",
            "pending_payload_purged": True,
            "projection_still_affects_edge_drawer": True,
            "replay": False,
        }

    # Lost-response / repeated ACK path. Once the pending payload has already
    # been purged, the exact same deterministic committed ACK must remain a
    # stable replay rather than falling through with no result.
    event.reload()
    projection.reload()
    if (
        event.sync_state == "Committed at Primary"
        and str(event.canonical_doctype or "") == PRIMARY_JOURNAL
        and str(event.canonical_name or "") == event_uuid
        and str(event.primary_ack_uuid or "") == ack_uuid
        and projection.projection_state == "Primary Preserved"
        and str(projection.primary_ack_uuid or "") == ack_uuid
    ):
        return {
            "event_uuid": event_uuid,
            "primary_ack_uuid": ack_uuid,
            "sync_state": "Committed at Primary",
            "projection_state": "Primary Preserved",
            "pending_payload_purged": False,
            "projection_still_affects_edge_drawer": True,
            "replay": True,
        }

    raise frappe.ValidationError(
        "Primary Cash Drawer Adjustment Intent ACK arrived without a matching pending or committed event."
    )

    event.reload()
    projection.reload()
    if (
        event.sync_state == "Committed at Primary"
        and event.canonical_doctype == "NKT Primary Cash Drawer Adjustment Intent"
        and event.canonical_name == event_uuid
        and str(event.primary_ack_uuid or "") == ack_uuid
        and projection.projection_state == "Primary Preserved"
        and str(projection.primary_ack_uuid or "") == ack_uuid
    ):
        return {
            "event_uuid": event_uuid,
            "primary_ack_uuid": ack_uuid,
            "sync_state": "Committed at Primary",
            "projection_state": "Primary Preserved",
            "pending_payload_purged": False,
            "projection_still_affects_edge_drawer": True,
            "replay": True,
        }

    raise frappe.ValidationError(
        "Primary ACK arrived without matching durable cash-drawer state."
    )
