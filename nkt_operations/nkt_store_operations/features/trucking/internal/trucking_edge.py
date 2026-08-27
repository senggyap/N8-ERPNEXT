from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import now

from nkt_operations.nkt_store_operations.features.trucking.access import (
    is_external_carrier_privileged,
)
from nkt_operations.nkt_store_operations.features.offline_edge.policy import (
    device_policy_snapshot,
    event_family_policy,
)
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    begin_event,
    canonical_payload_hash,
    mark_edge_accepted,
)
from nkt_operations.nkt_store_operations.features.trucking.trucking_offline_contract import (
    TRIP_LIFECYCLE_ACTION,
    TRIP_LIFECYCLE_FAMILY,
    canonical_trucking_trip_lifecycle_intent_json,
    normalize_trucking_trip_lifecycle_intent,
)

FOUNDATION_VERSION = "C15C.10L-R4D"
PH_TZ = ZoneInfo("Asia/Manila")
PROJECTION = "NKT Edge Trucking Trip Projection"
NORMAL_OPERATION_ROLES = {"NKT Trucking Operations", "NKT OWNER", "NKT ADMINISTRATOR", "System Manager"}


def _runtime_role():
    return str(frappe.conf.get("nkt_runtime_role") or "Primary").strip()


def _user(user=None):
    user = str(user or frappe.session.user or "").strip()
    if not user or user == "Guest":
        raise frappe.PermissionError("Offline Trucking unavailable.")
    return user


def _require_operator(user):
    roles = set(frappe.get_roles(user) or [])
    if user != "Administrator" and not (roles & NORMAL_OPERATION_ROLES):
        raise frappe.PermissionError("Offline Trucking unavailable.")


def _uuid(value, label):
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _mysql_local(value, label):
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(raw)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PH_TZ)
    else:
        dt = dt.astimezone(PH_TZ)
    return dt.replace(tzinfo=None)


def _stable_created(event_uuid):
    if frappe.db.exists("NKT Sync Event", event_uuid):
        return frappe.db.get_value("NKT Sync Event", event_uuid, "client_created_at")
    return now()


def _vehicle_ownership(vehicle):
    if not str(vehicle or "").strip():
        return ""
    return str(frappe.db.get_value("NKT Vehicle", vehicle, "custom_fleet_ownership") or "").strip()


def _enforce_employee_scope(user, payload):
    if is_external_carrier_privileged(user):
        return
    vehicle = str(payload.get("vehicle") or "").strip()
    if vehicle and _vehicle_ownership(vehicle) == "External Carrier":
        raise frappe.PermissionError("External carrier trucking is restricted to Owner/Admin.")
    source_job = str(payload.get("source_c9_trucking_job") or "").strip()
    if source_job:
        row = frappe.db.get_value(
            "NKT Trucking Job",
            source_job,
            ["delivery_vehicle", "carrier_account"],
            as_dict=True,
        )
        if not row:
            raise frappe.ValidationError("Source Trucking Job is unavailable.")
        if str(row.carrier_account or "").strip():
            raise frappe.PermissionError("External carrier trucking is restricted to Owner/Admin.")
        if _vehicle_ownership(row.delivery_vehicle) != "ENT-Owned":
            raise frappe.PermissionError("External carrier trucking is restricted to Owner/Admin.")


def _accept_event(event_uuid, normalized, device_id, user):
    digest = canonical_payload_hash(normalized)
    canonical_json = canonical_trucking_trip_lifecycle_intent_json(normalized)
    if digest != canonical_payload_hash(json.loads(canonical_json)):
        raise NKTIdempotencyConflict("Trucking lifecycle canonical payload hash is unstable.")

    device = device_policy_snapshot(device_id, user=user)
    if event_family_policy(TRIP_LIFECYCLE_FAMILY).get("offline_write_allowed") is not True:
        raise frappe.PermissionError("Offline Trucking lifecycle family is not enabled.")

    envelope = {
        "event_uuid": event_uuid,
        "event_family": TRIP_LIFECYCLE_FAMILY,
        "event_action": TRIP_LIFECYCLE_ACTION,
        "operational_context": device.get("operational_context") or "NKT Retail",
        "origin_device": str(device_id),
        "origin_user": user,
        "business_date": _mysql_local(
            normalized["event_datetime"], "Trucking Event Time"
        ).date().isoformat(),
        "settled_at": _mysql_local(normalized["event_datetime"], "Trucking Event Time"),
        "client_created_at": _stable_created(event_uuid),
        "payload_sha256": digest,
    }
    event, replay = begin_event(envelope)
    pending_name = frappe.db.exists("NKT Sync Pending Payload", event_uuid)
    if pending_name:
        pending = frappe.get_doc("NKT Sync Pending Payload", pending_name)
        if (
            pending.event_family != TRIP_LIFECYCLE_FAMILY
            or str(pending.payload_sha256 or "").lower() != digest
            or str(pending.payload_json or "") != canonical_json
        ):
            raise NKTIdempotencyConflict("Trucking pending payload conflicts with immutable event.")
    elif event.sync_state in ("Received", "Accepted at Edge", "Awaiting Primary"):
        frappe.get_doc({
            "doctype": "NKT Sync Pending Payload",
            "event_uuid": event_uuid,
            "event_family": TRIP_LIFECYCLE_FAMILY,
            "payload_sha256": digest,
            "queue_state": "Accepted at Edge",
            "payload_json": canonical_json,
            "edge_accepted_at": now(),
            "attempt_count": 0,
        }).insert(ignore_permissions=True)
    elif event.sync_state == "Committed at Primary":
        return {
            "event_uuid": event_uuid,
            "event_family": TRIP_LIFECYCLE_FAMILY,
            "sync_state": event.sync_state,
            "payload_sha256": digest,
            "replay": True,
        }
    else:
        raise frappe.ValidationError("Trucking lifecycle event is not eligible for Edge acceptance.")

    if event.sync_state in ("Received", "Accepted at Edge", "Awaiting Primary"):
        mark_edge_accepted(event_uuid)
        event.reload()
    return {
        "event_uuid": event_uuid,
        "event_family": TRIP_LIFECYCLE_FAMILY,
        "sync_state": event.sync_state,
        "payload_sha256": digest,
        "replay": bool(replay),
    }


def accept_trucking_trip_lifecycle_at_edge(
    event_uuid: str,
    payload: Dict[str, Any],
    *,
    device_id: str,
    user: Optional[str] = None,
):
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Offline Trucking unavailable.")

    event_uuid = _uuid(event_uuid, "Trucking Event UUID")
    user = _user(user)
    _require_operator(user)
    normalized = normalize_trucking_trip_lifecycle_intent(payload)
    _enforce_employee_scope(user, normalized)

    edge_trip_uuid = normalized["edge_trip_uuid"]
    existing_name = frappe.db.get_value(PROJECTION, {"edge_trip_uuid": edge_trip_uuid}, "name")
    existing = frappe.get_doc(PROJECTION, existing_name) if existing_name else None

    if normalized["action"] == "Create":
        if existing:
            if (
                str(existing.latest_event_uuid or "") != event_uuid
                or str(existing.last_payload_sha256 or "").lower()
                != canonical_payload_hash(normalized)
            ):
                raise NKTIdempotencyConflict("Edge Trip UUID already belongs to another immutable event.")
    else:
        if not existing:
            raise frappe.DoesNotExistError("Store Edge Trucking Trip projection is unavailable.")
        if str(existing.trip_date or "") != str(normalized["trip_date"] or ""):
            raise NKTIdempotencyConflict(
                "Offline trucking Trip Date is immutable after Create. "
                "Cross-midnight physical events must keep the original Trip Date "
                "and preserve their true later event datetime."
            )
        if str(existing.local_status or "") != str(normalized["previous_status"] or ""):
            raise NKTIdempotencyConflict(
                "Offline trucking previous status conflicts with Store Edge physical history."
            )

    accepted = _accept_event(event_uuid, normalized, device_id, user)

    values = {
        "latest_event_uuid": event_uuid,
        "company": normalized["company"],
        "trip_date": normalized["trip_date"],
        "job_type": normalized["job_type"],
        "customer": normalized["customer"],
        "source_c9_trucking_job": normalized["source_c9_trucking_job"],
        "origin": normalized["origin"],
        "destination": normalized["destination"],
        "vehicle": normalized["vehicle"],
        "driver_name": normalized["driver_name"],
        "local_status": normalized["new_status"],
        "last_event_datetime": _mysql_local(normalized["event_datetime"], "Trucking Event Time"),
        "container_return_required": normalized["container_return_required"],
        "container_returned": normalized["container_returned"],
        "container_no": normalized["container_no"],
        "eir_required": normalized["eir_required"],
        "eir_no": normalized["eir_no"],
        "paperwork_complete": normalized["paperwork_complete"],
        "pod_attachment": normalized["pod_attachment"],
        "print_snapshot_json": canonical_trucking_trip_lifecycle_intent_json(normalized),
        "print_snapshot_sha256": canonical_payload_hash(normalized),
        "last_payload_sha256": canonical_payload_hash(normalized),
        "sync_state": "Pending Edge",
    }

    if not existing:
        existing = frappe.get_doc({
            "doctype": PROJECTION,
            "edge_trip_uuid": edge_trip_uuid,
            **values,
        })
        existing.insert(ignore_permissions=True)
    elif str(existing.latest_event_uuid or "") != event_uuid:
        for key, value in values.items():
            existing.set(key, value)
        existing.save(ignore_permissions=True)

    return {
        **accepted,
        "edge_trip_uuid": edge_trip_uuid,
        "local_status": existing.local_status,
        "canonical_trucking_trip_created": False,
        "offline_money_effect_created": False,
        "operational_printing_from_cached_snapshot": True,
    }


@frappe.whitelist()
def record_trucking_trip_lifecycle_offline(event_uuid, payload, device_id):
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    return accept_trucking_trip_lifecycle_at_edge(
        event_uuid,
        payload,
        device_id=device_id,
    )
