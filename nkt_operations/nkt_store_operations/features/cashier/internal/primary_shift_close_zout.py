from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import now

from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    mark_primary_committed,
)
from nkt_operations.nkt_store_operations.features.cashier.internal.shift_close_zout_offline_intent import (
    CASHIER_SHIFT_OPEN_FAMILY,
    CASHIER_SHIFT_CLOSE_FAMILY,
    ENCODER_ZOUT_FINALIZE_FAMILY,
    normalize_cashier_shift_open_intent,
    normalize_cashier_shift_close_intent,
    normalize_encoder_zout_finalization_intent,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import (
    _runtime_role,
    prepare_event_for_primary,
    validate_transport_packet,
)

FOUNDATION_VERSION = "C15C.10K-R3"
MATERIALIZER_PATH = "nkt_operations.nkt_store_operations.features.cashier.internal.shift_close_zout_materializer.materialize_preserved_shift_close_zout"
PH_TZ = ZoneInfo("Asia/Manila")
PRIMARY_JOURNAL = "NKT Primary Shift Close Z-Out Intent"
PRIMARY_ACK_NAMESPACE = uuid.UUID("67a1db17-e04f-4c13-a304-f57c468ea68f")

RECEIPT_STATES = {
    CASHIER_SHIFT_OPEN_FAMILY: "Cashier Shift Open Intent Preserved",
    CASHIER_SHIFT_CLOSE_FAMILY: "Cashier Shift Close Intent Preserved",
    ENCODER_ZOUT_FINALIZE_FAMILY: "Encoder Z-Out Finalization Intent Preserved",
}


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Shift Close / Z-Out Primary receiver unavailable.")


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _canonical_json(value: Dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _expected_ack(event_uuid: str, family: str, payload_hash: str) -> str:
    material = family + "\0" + _uuid(event_uuid, "Event UUID") + "\0" + payload_hash.lower()
    return str(uuid.uuid5(PRIMARY_ACK_NAMESPACE, material))


def _claim_name(event_uuid: str) -> str:
    return "nkt-10k-shift-zout-" + hashlib.sha256(str(event_uuid).encode()).hexdigest()[:28]


def _acquire_claim(event_uuid: str) -> str:
    name = _claim_name(event_uuid)
    rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (name, 30), as_list=True)
    if not rows or int(rows[0][0] or 0) != 1:
        raise frappe.ValidationError("Shift Close / Z-Out Primary preservation is busy. Safe retry required.")
    return name


def _release_claim(name: str):
    try:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
    except Exception:
        pass


def _enqueue_materialization_after_commit(event_uuid: str) -> None:
    try:
        frappe.enqueue(
            MATERIALIZER_PATH,
            event_uuid=event_uuid,
            queue="short",
            enqueue_after_commit=True,
            job_id="nkt-c15c10k-materialize-" + str(event_uuid),
            deduplicate=True,
        )
    except TypeError:
        frappe.enqueue(
            MATERIALIZER_PATH,
            event_uuid=event_uuid,
            queue="short",
            enqueue_after_commit=True,
            job_id="nkt-c15c10k-materialize-" + str(event_uuid),
        )


def _normalize(family: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if family == CASHIER_SHIFT_OPEN_FAMILY:
        return normalize_cashier_shift_open_intent(payload)
    if family == CASHIER_SHIFT_CLOSE_FAMILY:
        return normalize_cashier_shift_close_intent(payload)
    if family == ENCODER_ZOUT_FINALIZE_FAMILY:
        return normalize_encoder_zout_finalization_intent(payload)
    raise frappe.ValidationError("Unsupported C15C.10K Primary event family.")


def _identity(family: str, payload: Dict[str, Any]) -> tuple[str, str, str]:
    if family in (CASHIER_SHIFT_OPEN_FAMILY, CASHIER_SHIFT_CLOSE_FAMILY):
        return payload["edge_shift_uuid"], payload["cashier"], str(payload.get("primary_shift_name") or "")
    return payload["edge_zout_uuid"], payload["encoder"], ""


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


def _ack(receipt, journal, replay: bool) -> Dict[str, Any]:
    return {
        "event_uuid": receipt.name,
        "event_family": receipt.event_family,
        "primary_ack_uuid": receipt.primary_ack_uuid,
        "payload_sha256": receipt.payload_sha256,
        "result_code": receipt.result_code,
        "committed": True,
        "replay": bool(replay),
        "canonical_doctype": PRIMARY_JOURNAL,
        "canonical_name": journal.name,
        "materialization_state": receipt.materialization_state,
        "edge_identity": journal.edge_identity,
        "canonical_business_document_created": False,
    }


def prepare_shift_close_zout_for_primary(event_uuid: str, expected_family: str):
    return prepare_event_for_primary(event_uuid, expected_family=expected_family)


def receive_shift_close_zout_at_primary(
    packet: Dict[str, Any],
    *,
    expected_family: str,
) -> Dict[str, Any]:
    _require_primary()
    envelope, raw_payload, envelope_hash, payload_hash = validate_transport_packet(
        packet,
        expected_family=expected_family,
    )
    family = envelope["event_family"]
    if family not in RECEIPT_STATES:
        raise frappe.ValidationError("Unsupported C15C.10K Primary event family.")

    payload = _normalize(family, raw_payload)
    edge_identity, actor, primary_shift_name = _identity(family, payload)
    if str(envelope.get("origin_user") or "") != actor:
        raise frappe.ValidationError("Shift Close / Z-Out actor must match immutable origin user.")

    if family == CASHIER_SHIFT_OPEN_FAMILY:
        event_date = payload["shift_business_date"]
        settled = payload["shift_start"]
    elif family == CASHIER_SHIFT_CLOSE_FAMILY:
        event_date = payload["physical_close_date"]
        settled = payload["physical_close_datetime"]
    else:
        event_date = payload["business_date"]
        settled = payload["finalized_on"]

    if str(envelope.get("business_date") or "") != str(event_date):
        raise frappe.ValidationError("Shift Close / Z-Out event business date conflicts with immutable payload.")

    event_uuid = envelope["event_uuid"]
    expected_ack = _expected_ack(event_uuid, family, payload_hash)
    receipt_state = RECEIPT_STATES[family]

    lock = _acquire_claim(event_uuid)
    try:
        receipt = _receipt_for_update(event_uuid)
        if receipt:
            if (
                receipt.event_family != family
                or receipt.primary_ack_uuid != expected_ack
                or receipt.envelope_sha256 != envelope_hash
                or receipt.payload_sha256 != payload_hash
                or receipt.canonical_doctype != PRIMARY_JOURNAL
                or receipt.canonical_name != event_uuid
                or receipt.materialization_state != receipt_state
                or receipt.result_code != "Committed"
            ):
                raise NKTIdempotencyConflict(
                    "Shift Close / Z-Out Primary receipt conflicts with immutable content."
                )
            journal = _journal_for_update(event_uuid)
            if not journal:
                raise NKTIdempotencyConflict(
                    "Shift Close / Z-Out Primary receipt exists without preserved journal."
                )
            if (
                journal.event_family != family
                or str(journal.payload_sha256 or "").lower() != payload_hash
                or str(journal.envelope_sha256 or "").lower() != envelope_hash
                or str(journal.canonical_payload_json or "") != _canonical_json(payload)
                or str(journal.canonical_envelope_json or "") != _canonical_json(envelope)
            ):
                raise NKTIdempotencyConflict(
                    "Shift Close / Z-Out preserved journal conflicts with immutable content."
                )
            if journal.materialization_state == "Pending Canonical Materialization":
                _enqueue_materialization_after_commit(event_uuid)
            return _ack(receipt, journal, replay=True)

        journal = frappe.get_doc({
            "doctype": PRIMARY_JOURNAL,
            "event_uuid": event_uuid,
            "event_family": family,
            "event_action": envelope["event_action"],
            "edge_identity": edge_identity,
            "origin_device": envelope["origin_device"],
            "origin_user": envelope["origin_user"],
            "company": payload["company"],
            "actor": actor,
            "event_business_date": envelope["business_date"],
            "settled_at": envelope["settled_at"],
            "primary_shift_name": primary_shift_name,
            "envelope_sha256": envelope_hash,
            "payload_sha256": payload_hash,
            "canonical_envelope_json": _canonical_json(envelope),
            "canonical_payload_json": _canonical_json(payload),
            "preservation_state": "Preserved",
            "materialization_state": "Pending Canonical Materialization",
            "canonical_doctype": "",
            "canonical_name": "",
            "primary_ack_uuid": expected_ack,
            "primary_preserved_at": now(),
            "reconciliation_status": "Not Reconciled",
        })
        journal.insert(ignore_permissions=True)

        receipt = frappe.get_doc({
            "doctype": "NKT Sync Primary Receipt",
            "event_uuid": event_uuid,
            "event_family": family,
            "primary_ack_uuid": expected_ack,
            "envelope_sha256": envelope_hash,
            "payload_sha256": payload_hash,
            "primary_received_at": now(),
            "primary_committed_at": now(),
            "result_code": "Committed",
            "canonical_doctype": PRIMARY_JOURNAL,
            "canonical_name": event_uuid,
            "materialization_state": receipt_state,
        })
        receipt.insert(ignore_permissions=True)

        mark_primary_committed(
            event_uuid,
            PRIMARY_JOURNAL,
            event_uuid,
            primary_ack_uuid=expected_ack,
        )
        _enqueue_materialization_after_commit(event_uuid)
        return _ack(receipt, journal, replay=False)
    finally:
        _release_claim(lock)


def apply_shift_close_zout_ack_at_edge(ack: Dict[str, Any]) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Shift Close / Z-Out preservation ACK unavailable.")
    if not isinstance(ack, dict):
        raise frappe.ValidationError("Shift Close / Z-Out preservation ACK is invalid.")

    event_uuid = _uuid(ack.get("event_uuid"), "Event UUID")
    family = str(ack.get("event_family") or "")
    if family not in RECEIPT_STATES:
        raise frappe.ValidationError("Shift Close / Z-Out ACK family is invalid.")
    if (
        ack.get("committed") is not True
        or ack.get("result_code") != "Committed"
        or ack.get("materialization_state") != RECEIPT_STATES[family]
        or ack.get("canonical_doctype") != PRIMARY_JOURNAL
        or ack.get("canonical_name") != event_uuid
        or ack.get("canonical_business_document_created") is not False
    ):
        raise frappe.ValidationError("Shift Close / Z-Out ACK is not preservation-only.")

    event = frappe.get_doc("NKT Sync Event", event_uuid)
    payload_hash = str(ack.get("payload_sha256") or "").lower()
    if event.event_family != family or str(event.payload_sha256 or "").lower() != payload_hash:
        raise NKTIdempotencyConflict("Shift Close / Z-Out ACK conflicts with immutable Edge event.")

    ack_uuid = _uuid(ack.get("primary_ack_uuid"), "Primary ACK UUID")
    if event.sync_state in ("Accepted at Edge", "Awaiting Primary"):
        mark_primary_committed(
            event_uuid,
            PRIMARY_JOURNAL,
            event_uuid,
            primary_ack_uuid=ack_uuid,
        )
    elif event.sync_state == "Committed at Primary":
        if (
            str(event.primary_ack_uuid or "") != ack_uuid
            or str(event.canonical_doctype or "") != PRIMARY_JOURNAL
            or str(event.canonical_name or "") != event_uuid
        ):
            raise NKTIdempotencyConflict("Shift Close / Z-Out ACK binding conflicts with committed Edge event.")
    else:
        raise frappe.ValidationError("Shift Close / Z-Out event is not eligible for ACK.")

    pending = frappe.db.exists("NKT Sync Pending Payload", event_uuid)
    if pending:
        pd = frappe.get_doc("NKT Sync Pending Payload", pending)
        if str(pd.payload_sha256 or "").lower() != payload_hash:
            raise NKTIdempotencyConflict("Shift Close / Z-Out ACK conflicts with pending payload.")
        frappe.delete_doc("NKT Sync Pending Payload", pd.name, ignore_permissions=True, force=True)

    edge_identity = str(ack.get("edge_identity") or "")
    if family in (CASHIER_SHIFT_OPEN_FAMILY, CASHIER_SHIFT_CLOSE_FAMILY):
        name = frappe.db.get_value(
            "NKT Edge Cashier Shift Projection",
            {"edge_shift_uuid": edge_identity},
            "name",
        )
        if name:
            doc = frappe.get_doc("NKT Edge Cashier Shift Projection", name)
            if family == CASHIER_SHIFT_OPEN_FAMILY:
                doc.open_sync_state = "Primary Preserved"
            else:
                doc.close_sync_state = "Primary Preserved"
            doc.save(ignore_permissions=True)
    else:
        name = frappe.db.get_value(
            "NKT Edge Encoder Z-Out Projection",
            {"edge_zout_uuid": edge_identity},
            "name",
        )
        if name:
            doc = frappe.get_doc("NKT Edge Encoder Z-Out Projection", name)
            doc.sync_state = "Primary Preserved"
            doc.save(ignore_permissions=True)

    return {
        "event_uuid": event_uuid,
        "event_family": family,
        "primary_ack_uuid": ack_uuid,
        "sync_state": "Committed at Primary",
        "pending_payload_purged": bool(pending),
        "canonical_business_document_created": False,
    }


def contract_status():
    return {
        "foundation_version": FOUNDATION_VERSION,
        "three_families_preserved_on_primary": True,
        "edge_cashier_shift_projection_enabled": True,
        "edge_official_encoder_zout_projection_enabled": True,
        "full_canonical_payload_preserved_before_edge_purge": True,
        "canonical_cashier_shift_materialization_enabled": False,
        "canonical_encoder_zout_materialization_enabled": False,
        "cashier_tender_edge_shift_alias_bridge_enabled": False,
        "cash_drawer_edge_shift_alias_bridge_enabled": False,
        "official_offline_zout_rewrite_allowed": False,
        "cashier_physical_count_rewrite_allowed": False,
    }


@frappe.whitelist()
def receive_shift_open(packet):
    if isinstance(packet, str):
        packet = frappe.parse_json(packet)
    return receive_shift_close_zout_at_primary(
        packet, expected_family=CASHIER_SHIFT_OPEN_FAMILY
    )


@frappe.whitelist()
def receive_shift_close(packet):
    if isinstance(packet, str):
        packet = frappe.parse_json(packet)
    return receive_shift_close_zout_at_primary(
        packet, expected_family=CASHIER_SHIFT_CLOSE_FAMILY
    )


@frappe.whitelist()
def receive_encoder_zout(packet):
    if isinstance(packet, str):
        packet = frappe.parse_json(packet)
    return receive_shift_close_zout_at_primary(
        packet, expected_family=ENCODER_ZOUT_FINALIZE_FAMILY
    )


@frappe.whitelist()
def apply_preservation_ack(ack):
    if isinstance(ack, str):
        ack = frappe.parse_json(ack)
    return apply_shift_close_zout_ack_at_edge(ack)
