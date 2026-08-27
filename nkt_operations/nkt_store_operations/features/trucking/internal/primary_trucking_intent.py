from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import now

from nkt_operations.nkt_store_operations.features.trucking.access import (
    is_external_carrier_privileged,
)
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    mark_primary_committed,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import (
    _runtime_role,
    prepare_event_for_primary,
    validate_transport_packet,
)
from nkt_operations.nkt_store_operations.features.trucking.trucking_offline_contract import (
    TRIP_LIFECYCLE_FAMILY,
    normalize_trucking_trip_lifecycle_intent,
)

FOUNDATION_VERSION = "C15C.10L-R4D"
PRIMARY_JOURNAL = "NKT Primary Trucking Trip Intent"
RECEIPT_STATE = "Trucking Trip Lifecycle Intent Preserved"
PRIMARY_ACK_NAMESPACE = uuid.UUID("82da5ff6-1e21-42aa-9a76-8c22e6557848")
MATERIALIZER_PATH = "nkt_operations.nkt_store_operations.features.trucking.trucking_materializer.materialize_preserved_trucking_event"
PH_TZ = ZoneInfo("Asia/Manila")


def _mysql_local_datetime(value):
    """Convert an immutable ISO event time to the DB's Manila-local naive Datetime.

    The canonical payload/envelope continue to preserve the original timezone-aware
    ISO string. This conversion exists only because MariaDB DATETIME has no timezone
    offset storage and rejects values such as 2026-08-16T23:20:10+08:00.
    """
    raw = str(value or "").strip()
    if not raw:
        raise frappe.ValidationError("Trucking physical event datetime is required.")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(raw)
    except Exception as exc:
        raise frappe.ValidationError("Trucking physical event datetime is invalid.") from exc
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(PH_TZ).replace(tzinfo=None)


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Trucking Primary receiver unavailable.")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _expected_ack(event_uuid, payload_hash):
    material = TRIP_LIFECYCLE_FAMILY + "\0" + str(event_uuid) + "\0" + payload_hash.lower()
    return str(uuid.uuid5(PRIMARY_ACK_NAMESPACE, material))


def _claim_name(event_uuid):
    return "nkt-10l-trucking-" + hashlib.sha256(str(event_uuid).encode()).hexdigest()[:28]


def _acquire(event_uuid):
    name = _claim_name(event_uuid)
    rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (name, 30), as_list=True)
    if not rows or int(rows[0][0] or 0) != 1:
        raise frappe.ValidationError("Trucking Primary preservation is busy. Safe retry required.")
    return name


def _release(name):
    try:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
    except Exception:
        pass


def _vehicle_ownership(vehicle):
    if not str(vehicle or "").strip():
        return ""
    return str(frappe.db.get_value("NKT Vehicle", vehicle, "custom_fleet_ownership") or "").strip()


def _validate_primary_visibility(origin_user, payload):
    ownership = _vehicle_ownership(payload.get("vehicle"))
    if ownership == "External Carrier" and not is_external_carrier_privileged(origin_user):
        raise frappe.PermissionError(
            "External carrier trucking intent is restricted to Owner/Admin."
        )
    source_job = str(payload.get("source_c9_trucking_job") or "").strip()
    if source_job:
        row = frappe.db.get_value(
            "NKT Trucking Job",
            source_job,
            ["delivery_vehicle", "carrier_account"],
            as_dict=True,
        )
        if not row:
            raise frappe.ValidationError("Source Trucking Job is unavailable at Primary.")
        external = bool(str(row.carrier_account or "").strip()) or _vehicle_ownership(row.delivery_vehicle) == "External Carrier"
        if external and not is_external_carrier_privileged(origin_user):
            raise frappe.PermissionError(
                "External carrier Supplier Arrival trucking is restricted to Owner/Admin."
            )
    return ownership


def _enqueue_materialization_after_commit(event_uuid: str) -> None:
    try:
        frappe.enqueue(
            MATERIALIZER_PATH,
            event_uuid=event_uuid,
            queue="short",
            enqueue_after_commit=True,
            job_id="nkt-c15c10l-materialize-" + str(event_uuid),
            deduplicate=True,
        )
    except TypeError:
        frappe.enqueue(
            MATERIALIZER_PATH,
            event_uuid=event_uuid,
            queue="short",
            enqueue_after_commit=True,
            job_id="nkt-c15c10l-materialize-" + str(event_uuid),
        )


def prepare_trucking_for_primary(event_uuid):
    return prepare_event_for_primary(event_uuid, expected_family=TRIP_LIFECYCLE_FAMILY)


def receive_trucking_at_primary(packet: Dict[str, Any]):
    _require_primary()
    envelope, raw_payload, envelope_hash, payload_hash = validate_transport_packet(
        packet,
        expected_family=TRIP_LIFECYCLE_FAMILY,
    )
    payload = normalize_trucking_trip_lifecycle_intent(raw_payload)
    event_business_date = _mysql_local_datetime(payload["event_datetime"]).date().isoformat()
    if str(envelope.get("business_date") or "") != event_business_date:
        raise frappe.ValidationError(
            "Trucking safe-sync Business Date must equal the true physical event date."
        )

    origin_user = str(envelope.get("origin_user") or "")
    ownership = _validate_primary_visibility(origin_user, payload)
    event_uuid = str(envelope["event_uuid"])
    ack_uuid = _expected_ack(event_uuid, payload_hash)

    lock = _acquire(event_uuid)
    try:
        receipt = frappe.db.exists("NKT Sync Primary Receipt", event_uuid)
        journal = frappe.db.exists(PRIMARY_JOURNAL, event_uuid)

        if receipt or journal:
            if not receipt or not journal:
                raise NKTIdempotencyConflict("Trucking preservation replay is incomplete.")
            receipt_doc = frappe.get_doc("NKT Sync Primary Receipt", event_uuid)
            journal_doc = frappe.get_doc(PRIMARY_JOURNAL, event_uuid)
            if (
                receipt_doc.event_family != TRIP_LIFECYCLE_FAMILY
                or receipt_doc.primary_ack_uuid != ack_uuid
                or receipt_doc.envelope_sha256 != envelope_hash
                or receipt_doc.payload_sha256 != payload_hash
                or receipt_doc.materialization_state != RECEIPT_STATE
                or receipt_doc.canonical_doctype != PRIMARY_JOURNAL
                or receipt_doc.canonical_name != event_uuid
                or str(journal_doc.canonical_payload_json or "") != _canonical(payload)
                or str(journal_doc.canonical_envelope_json or "") != _canonical(envelope)
            ):
                raise NKTIdempotencyConflict("Trucking Primary replay conflicts with immutable content.")
            if str(journal_doc.materialization_state or "") == "Pending Canonical Materialization":
                _enqueue_materialization_after_commit(event_uuid)
            return _ack(receipt_doc, journal_doc, True)

        journal_doc = frappe.get_doc({
            "doctype": PRIMARY_JOURNAL,
            "event_uuid": event_uuid,
            "event_family": TRIP_LIFECYCLE_FAMILY,
            "event_action": envelope["event_action"],
            "edge_trip_uuid": payload["edge_trip_uuid"],
            "origin_device": envelope["origin_device"],
            "origin_user": origin_user,
            "trip_date": payload["trip_date"],
            "action": payload["action"],
            "previous_status": payload["previous_status"],
            "new_status": payload["new_status"],
            "event_datetime": _mysql_local_datetime(payload["event_datetime"]),
            "container_no": payload["container_no"],
            "vehicle": payload["vehicle"],
            "primary_vehicle_ownership": ownership,
            "source_c9_trucking_job": payload["source_c9_trucking_job"],
            "envelope_sha256": envelope_hash,
            "payload_sha256": payload_hash,
            "canonical_envelope_json": _canonical(envelope),
            "canonical_payload_json": _canonical(payload),
            "preservation_state": "Preserved",
            "materialization_state": "Pending Canonical Materialization",
            "canonical_doctype": "",
            "canonical_name": "",
            "primary_ack_uuid": ack_uuid,
            "primary_preserved_at": now(),
        })
        journal_doc.insert(ignore_permissions=True)

        receipt_doc = frappe.get_doc({
            "doctype": "NKT Sync Primary Receipt",
            "event_uuid": event_uuid,
            "event_family": TRIP_LIFECYCLE_FAMILY,
            "primary_ack_uuid": ack_uuid,
            "envelope_sha256": envelope_hash,
            "payload_sha256": payload_hash,
            "primary_received_at": now(),
            "primary_committed_at": now(),
            "result_code": "Committed",
            "canonical_doctype": PRIMARY_JOURNAL,
            "canonical_name": event_uuid,
            "materialization_state": RECEIPT_STATE,
        })
        receipt_doc.insert(ignore_permissions=True)

        mark_primary_committed(
            event_uuid,
            PRIMARY_JOURNAL,
            event_uuid,
            primary_ack_uuid=ack_uuid,
        )
        _enqueue_materialization_after_commit(event_uuid)
        return _ack(receipt_doc, journal_doc, False)
    finally:
        _release(lock)


def _ack(receipt, journal, replay):
    return {
        "event_uuid": receipt.name,
        "event_family": TRIP_LIFECYCLE_FAMILY,
        "primary_ack_uuid": receipt.primary_ack_uuid,
        "payload_sha256": receipt.payload_sha256,
        "result_code": receipt.result_code,
        "committed": True,
        "replay": bool(replay),
        "canonical_doctype": PRIMARY_JOURNAL,
        "canonical_name": journal.name,
        "materialization_state": RECEIPT_STATE,
        "edge_trip_uuid": journal.edge_trip_uuid,
        "canonical_trucking_trip_created": False,
    }


def apply_trucking_ack_at_edge(ack):
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Trucking preservation ACK unavailable.")
    if not isinstance(ack, dict):
        raise frappe.ValidationError("Trucking preservation ACK is invalid.")
    if (
        ack.get("committed") is not True
        or ack.get("result_code") != "Committed"
        or ack.get("event_family") != TRIP_LIFECYCLE_FAMILY
        or ack.get("materialization_state") != RECEIPT_STATE
        or ack.get("canonical_doctype") != PRIMARY_JOURNAL
        or ack.get("canonical_trucking_trip_created") is not False
    ):
        raise frappe.ValidationError("Trucking ACK is not preservation-only.")

    event_uuid = str(ack.get("event_uuid") or "")
    event = frappe.get_doc("NKT Sync Event", event_uuid)
    payload_hash = str(ack.get("payload_sha256") or "").lower()
    ack_uuid = str(ack.get("primary_ack_uuid") or "")
    if event.event_family != TRIP_LIFECYCLE_FAMILY or str(event.payload_sha256 or "").lower() != payload_hash:
        raise NKTIdempotencyConflict("Trucking ACK conflicts with immutable Edge event.")

    if event.sync_state in ("Accepted at Edge", "Awaiting Primary"):
        mark_primary_committed(
            event_uuid,
            PRIMARY_JOURNAL,
            event_uuid,
            primary_ack_uuid=ack_uuid,
        )
    elif event.sync_state == "Committed at Primary":
        if str(event.primary_ack_uuid or "") != ack_uuid:
            raise NKTIdempotencyConflict("Trucking ACK UUID conflicts with committed Edge event.")
    else:
        raise frappe.ValidationError("Trucking event is not eligible for ACK.")

    pending = frappe.db.exists("NKT Sync Pending Payload", event_uuid)
    if pending:
        pd = frappe.get_doc("NKT Sync Pending Payload", pending)
        if str(pd.payload_sha256 or "").lower() != payload_hash:
            raise NKTIdempotencyConflict("Trucking ACK conflicts with pending payload.")
        frappe.delete_doc("NKT Sync Pending Payload", pd.name, ignore_permissions=True, force=True)

    edge_trip_uuid = str(ack.get("edge_trip_uuid") or "")
    projection_name = frappe.db.get_value(
        "NKT Edge Trucking Trip Projection",
        {"edge_trip_uuid": edge_trip_uuid},
        "name",
    )
    if projection_name:
        frappe.db.set_value(
            "NKT Edge Trucking Trip Projection",
            projection_name,
            "sync_state",
            "Primary Preserved",
            update_modified=False,
        )

    return {
        "event_uuid": event_uuid,
        "event_family": TRIP_LIFECYCLE_FAMILY,
        "sync_state": "Committed at Primary",
        "pending_payload_purged": bool(pending),
        "canonical_trucking_trip_created": False,
    }


def contract_status():
    return {
        "trucking_family_preserved_on_primary": True,
        "edge_trip_projection_enabled": True,
        "full_payload_preserved_before_edge_purge": True,
        "canonical_trucking_trip_materialization_enabled": True,
        "customer_soa_write_enabled": False,
        "external_trucker_soa_payment_write_enabled": False,
        "driver_incentive_payment_write_enabled": False,
        "external_supplier_arrival_duplicate_receiving_enabled": False,
    }


@frappe.whitelist()
def receive_trucking(packet):
    if isinstance(packet, str):
        packet = frappe.parse_json(packet)
    return receive_trucking_at_primary(packet)


@frappe.whitelist()
def apply_preservation_ack(ack):
    if isinstance(ack, str):
        ack = frappe.parse_json(ack)
    return apply_trucking_ack_at_edge(ack)
