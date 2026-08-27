from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import getdate, now

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
from nkt_operations.nkt_store_operations.features.cashier.internal.shift_close_zout_offline_intent import (
    CASHIER_SHIFT_OPEN_FAMILY,
    CASHIER_SHIFT_CLOSE_FAMILY,
    ENCODER_ZOUT_FINALIZE_FAMILY,
    canonical_cashier_shift_open_intent_json,
    canonical_cashier_shift_close_intent_json,
    canonical_encoder_zout_finalization_intent_json,
    normalize_cashier_shift_open_intent,
    normalize_cashier_shift_close_intent,
    normalize_encoder_zout_finalization_intent,
)

FOUNDATION_VERSION = "C15C.10K-R3"
PH_TZ = ZoneInfo("Asia/Manila")
SHIFT_PROJECTION = "NKT Edge Cashier Shift Projection"
ZOUT_PROJECTION = "NKT Edge Encoder Z-Out Projection"


def _runtime_role() -> str:
    return str(frappe.conf.get("nkt_runtime_role") or "Primary").strip()


def _session_user(user=None) -> str:
    user = str(user or frappe.session.user or "").strip()
    if not user or user == "Guest":
        raise frappe.PermissionError("Offline Shift Close / Z-Out unavailable.")
    return user


def _roles(user: str) -> set[str]:
    return set(frappe.get_roles(user) or [])


def _require_cashier(user: str) -> None:
    if not (_roles(user) & {"NKT Cashier", "NKT OWNER", "NKT ADMINISTRATOR", "System Manager"}) and user != "Administrator":
        raise frappe.PermissionError("Offline Cashier Shift lifecycle unavailable.")


def _require_encoder(user: str) -> None:
    if not (_roles(user) & {"NKT Encoder", "NKT OWNER", "NKT ADMINISTRATOR", "System Manager"}) and user != "Administrator":
        raise frappe.PermissionError("Offline Encoder Z-Out unavailable.")


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _mysql_local_datetime(value: Any, label: str) -> datetime:
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


def _stable_client_created_at(event_uuid: str):
    existing = frappe.db.exists("NKT Sync Event", event_uuid)
    if existing:
        return frappe.db.get_value("NKT Sync Event", existing, "client_created_at")
    return now()


def _accept_pending_event(
    *,
    event_uuid: str,
    family: str,
    action: str,
    business_date: str,
    settled_at: Any,
    payload: Dict[str, Any],
    canonical_json: str,
    device_id: str,
    user: str,
) -> Dict[str, Any]:
    digest = canonical_payload_hash(payload)
    if digest != canonical_payload_hash(json.loads(canonical_json)):
        raise NKTIdempotencyConflict("Shift Close / Z-Out canonical payload hash is unstable.")

    device = device_policy_snapshot(
        device_id,
        user=user,
        requested_context="NKT Retail",
    )
    if event_family_policy(family).get("offline_write_allowed") is not True:
        raise frappe.PermissionError("This Shift Close / Z-Out family is not enabled offline.")

    envelope = {
        "event_uuid": event_uuid,
        "event_family": family,
        "event_action": action,
        "operational_context": device.get("operational_context") or "NKT Retail",
        "origin_device": str(device_id),
        "origin_user": user,
        "business_date": business_date,
        "settled_at": _mysql_local_datetime(settled_at, "Event Settled At"),
        "client_created_at": _stable_client_created_at(event_uuid),
        "payload_sha256": digest,
    }
    event, replay = begin_event(envelope)
    pending_name = frappe.db.exists("NKT Sync Pending Payload", event_uuid)

    if pending_name:
        pending = frappe.get_doc("NKT Sync Pending Payload", pending_name)
        if (
            pending.event_family != family
            or str(pending.payload_sha256 or "").lower() != digest
            or str(pending.payload_json or "") != canonical_json
        ):
            raise NKTIdempotencyConflict(
                "Shift Close / Z-Out pending payload conflicts with immutable event content."
            )
    elif event.sync_state in ("Received", "Accepted at Edge", "Awaiting Primary"):
        frappe.get_doc({
            "doctype": "NKT Sync Pending Payload",
            "event_uuid": event_uuid,
            "event_family": family,
            "payload_sha256": digest,
            "queue_state": "Accepted at Edge",
            "payload_json": canonical_json,
            "edge_accepted_at": now(),
            "attempt_count": 0,
        }).insert(ignore_permissions=True)
    elif event.sync_state == "Committed at Primary":
        return {
            "event_uuid": event_uuid,
            "event_family": family,
            "sync_state": event.sync_state,
            "payload_sha256": digest,
            "replay": True,
            "pending_payload_created": False,
        }
    else:
        raise frappe.ValidationError("Shift Close / Z-Out event is not eligible for Edge acceptance.")

    if event.sync_state in ("Received", "Accepted at Edge", "Awaiting Primary"):
        mark_edge_accepted(event_uuid)
        event.reload()

    return {
        "event_uuid": event_uuid,
        "event_family": family,
        "sync_state": event.sync_state,
        "payload_sha256": digest,
        "replay": bool(replay),
        "pending_payload_created": not bool(pending_name),
    }


def _projection_by_edge_uuid(edge_shift_uuid: str):
    name = frappe.db.get_value(SHIFT_PROJECTION, {"edge_shift_uuid": edge_shift_uuid}, "name")
    return frappe.get_doc(SHIFT_PROJECTION, name) if name else None


def _verify_open_projection(doc, normalized):
    checks = {
        "edge_shift_uuid": normalized["edge_shift_uuid"],
        "company": normalized["company"],
        "settlement_location": normalized["settlement_location"],
        "cashier": normalized["cashier"],
        "shift_business_date": normalized["shift_business_date"],
    }
    for field, expected in checks.items():
        if str(doc.get(field) or "") != str(expected or ""):
            raise NKTIdempotencyConflict(
                f"Store Edge Cashier Shift projection conflicts on {field}."
            )


def accept_cashier_shift_open_at_edge(
    event_uuid: str,
    payload: Dict[str, Any],
    *,
    device_id: str,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Offline Cashier Shift open unavailable.")

    event_uuid = _uuid(event_uuid, "Shift Open Event UUID")
    user = _session_user(user)
    _require_cashier(user)
    normalized = normalize_cashier_shift_open_intent(payload)
    if normalized["cashier"] != user:
        raise frappe.PermissionError("Cashier Shift open user must match authenticated Edge user.")

    existing = _projection_by_edge_uuid(normalized["edge_shift_uuid"])
    shift_reference = "EDGE-SHIFT-" + normalized["edge_shift_uuid"]
    if existing:
        _verify_open_projection(existing, normalized)
        if existing.open_event_uuid and str(existing.open_event_uuid) != event_uuid:
            raise NKTIdempotencyConflict("Edge Shift UUID already belongs to another Open Event UUID.")
    else:
        other = frappe.db.get_value(
            SHIFT_PROJECTION,
            {"cashier": user, "local_status": "Open"},
            "name",
        )
        if other:
            raise frappe.ValidationError(
                f"Cashier already has an unfinished Store Edge shift: {other}."
            )
        if frappe.db.exists("NKT Cashier Shift", {"cashier": user, "docstatus": 0, "status": "Open"}):
            raise frappe.ValidationError(
                "Cashier already has an open canonical Cashier Shift on this Store Edge."
            )

    accepted = _accept_pending_event(
        event_uuid=event_uuid,
        family=CASHIER_SHIFT_OPEN_FAMILY,
        action=normalized.get("action") or "Open Cashier Shift Offline",
        business_date=normalized["shift_business_date"],
        settled_at=normalized["shift_start"],
        payload=normalized,
        canonical_json=canonical_cashier_shift_open_intent_json(normalized),
        device_id=device_id,
        user=user,
    )

    if not existing:
        existing = frappe.get_doc({
            "doctype": SHIFT_PROJECTION,
            "shift_reference": shift_reference,
            "edge_shift_uuid": normalized["edge_shift_uuid"],
            "primary_shift_name": "",
            "source_kind": "Opened Offline at Edge",
            "company": normalized["company"],
            "settlement_location": normalized["settlement_location"],
            "cashier": normalized["cashier"],
            "shift_business_date": normalized["shift_business_date"],
            "shift_start": _mysql_local_datetime(normalized["shift_start"], "Shift Start"),
            "opening_cash": normalized["opening_cash"],
            "local_status": "Open",
            "open_event_uuid": event_uuid,
            "open_sync_state": "Pending Edge",
            "close_sync_state": "Not Closed",
        })
        existing.insert(ignore_permissions=True)

    return {
        **accepted,
        "edge_shift_uuid": normalized["edge_shift_uuid"],
        "cashier_shift_reference": existing.name,
        "local_status": existing.local_status,
        "canonical_cashier_shift_created": False,
    }


def _adopt_primary_shift_for_close(normalized, event_uuid: str):
    primary_name = str(normalized.get("primary_shift_name") or "").strip()
    if not primary_name or not frappe.db.exists("NKT Cashier Shift", primary_name):
        return None
    shift = frappe.get_doc("NKT Cashier Shift", primary_name)
    if int(shift.docstatus or 0) != 0 or str(shift.status or "") != "Open":
        raise frappe.ValidationError("Primary Cashier Shift is not open on this Store Edge.")
    if str(shift.cashier or "") != normalized["cashier"]:
        raise frappe.ValidationError("Primary Cashier Shift cashier conflicts with offline close.")
    if str(shift.company or "") != normalized["company"]:
        raise frappe.ValidationError("Primary Cashier Shift company conflicts with offline close.")
    if str(shift.settlement_location or "") != normalized["settlement_location"]:
        raise frappe.ValidationError("Primary Cashier Shift settlement location conflicts with offline close.")

    doc = frappe.get_doc({
        "doctype": SHIFT_PROJECTION,
        "shift_reference": primary_name,
        "edge_shift_uuid": normalized["edge_shift_uuid"],
        "primary_shift_name": primary_name,
        "source_kind": "Primary Shift Adopted at Edge",
        "company": normalized["company"],
        "settlement_location": normalized["settlement_location"],
        "cashier": normalized["cashier"],
        "shift_business_date": normalized["shift_business_date"],
        "shift_start": shift.shift_start,
        "opening_cash": shift.opening_cash,
        "local_status": "Open",
        "open_event_uuid": "",
        "open_sync_state": "Primary Preserved",
        "close_sync_state": "Not Closed",
    })
    doc.insert(ignore_permissions=True)
    return doc


def accept_cashier_shift_close_at_edge(
    event_uuid: str,
    payload: Dict[str, Any],
    *,
    device_id: str,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Offline Cashier Shift close unavailable.")

    event_uuid = _uuid(event_uuid, "Shift Close Event UUID")
    user = _session_user(user)
    _require_cashier(user)
    normalized = normalize_cashier_shift_close_intent(payload)
    if normalized["cashier"] != user:
        raise frappe.PermissionError("Only the assigned Cashier may close this Store Edge shift.")

    projection = _projection_by_edge_uuid(normalized["edge_shift_uuid"])
    if not projection:
        projection = _adopt_primary_shift_for_close(normalized, event_uuid)
    if not projection:
        raise frappe.DoesNotExistError(
            "Store Edge Cashier Shift projection is unavailable for offline close."
        )

    identity_checks = {
        "company": normalized["company"],
        "settlement_location": normalized["settlement_location"],
        "cashier": normalized["cashier"],
        "shift_business_date": normalized["shift_business_date"],
    }
    for field, expected in identity_checks.items():
        if str(projection.get(field) or "") != str(expected or ""):
            raise NKTIdempotencyConflict(f"Offline close conflicts with Store Edge shift {field}.")

    if projection.local_status == "Closed":
        if str(projection.close_event_uuid or "") != event_uuid:
            raise NKTIdempotencyConflict("Cashier Shift is already physically closed by another event.")
    elif projection.local_status != "Open":
        raise frappe.ValidationError("Store Edge Cashier Shift is not open.")

    accepted = _accept_pending_event(
        event_uuid=event_uuid,
        family=CASHIER_SHIFT_CLOSE_FAMILY,
        action="Close Cashier Shift Offline",
        business_date=normalized["physical_close_date"],
        settled_at=normalized["physical_close_datetime"],
        payload=normalized,
        canonical_json=canonical_cashier_shift_close_intent_json(normalized),
        device_id=device_id,
        user=user,
    )

    if projection.local_status == "Open":
        projection.local_status = "Closed"
        projection.close_event_uuid = event_uuid
        projection.close_sync_state = "Pending Edge"
        projection.physical_close_datetime = _mysql_local_datetime(
            normalized["physical_close_datetime"], "Physical Close Date / Time"
        )
        projection.actual_cash = normalized["actual_cash"]
        projection.provisional_expected_cash = normalized["provisional_expected_cash"]
        projection.provisional_over_short = normalized["provisional_over_short"]
        projection.provisional_movement_count = normalized["provisional_movement_count"]
        projection.denominations_json = json.dumps(
            normalized["denominations"], sort_keys=True, separators=(",", ":")
        )
        projection.provisional_summary_json = json.dumps(
            normalized["provisional_summary"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        projection.provisional_summary_sha256 = normalized["provisional_summary_sha256"]
        projection.count_notes = normalized["count_notes"]
        projection.save(ignore_permissions=True)

    return {
        **accepted,
        "edge_shift_uuid": normalized["edge_shift_uuid"],
        "cashier_shift_reference": projection.name,
        "local_status": "Closed",
        "physical_count_is_immutable_truth": True,
        "edge_expected_cash_is_provisional": True,
        "canonical_cashier_shift_closed_at_primary": False,
    }


def accept_encoder_zout_finalization_at_edge(
    event_uuid: str,
    payload: Dict[str, Any],
    *,
    device_id: str,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Official offline Encoder Z-Out unavailable.")

    event_uuid = _uuid(event_uuid, "Encoder Z-Out Event UUID")
    user = _session_user(user)
    _require_encoder(user)
    normalized = normalize_encoder_zout_finalization_intent(payload)
    if normalized["encoder"] != user:
        raise frappe.PermissionError("Encoder Z-Out user must match authenticated Edge user.")

    accepted = _accept_pending_event(
        event_uuid=event_uuid,
        family=ENCODER_ZOUT_FINALIZE_FAMILY,
        action="Finalize Official Encoder Z-Out Offline",
        business_date=normalized["business_date"],
        settled_at=normalized["finalized_on"],
        payload=normalized,
        canonical_json=canonical_encoder_zout_finalization_intent_json(normalized),
        device_id=device_id,
        user=user,
    )

    existing = frappe.db.get_value(
        ZOUT_PROJECTION,
        {"edge_zout_uuid": normalized["edge_zout_uuid"]},
        "name",
    )
    if existing:
        doc = frappe.get_doc(ZOUT_PROJECTION, existing)
        if (
            str(doc.event_uuid or "") != event_uuid
            or str(doc.snapshot_sha256 or "").lower() != normalized["snapshot_sha256"]
        ):
            raise NKTIdempotencyConflict(
                "Official offline Encoder Z-Out projection conflicts with immutable identity."
            )
    else:
        doc = frappe.get_doc({
            "doctype": ZOUT_PROJECTION,
            "edge_zout_uuid": normalized["edge_zout_uuid"],
            "event_uuid": event_uuid,
            "company": normalized["company"],
            "encoder": normalized["encoder"],
            "business_date": normalized["business_date"],
            "start_datetime": _mysql_local_datetime(normalized["start_datetime"], "Z-Out Start"),
            "effective_end_datetime": _mysql_local_datetime(normalized["effective_end_datetime"], "Z-Out End"),
            "finalized_on": _mysql_local_datetime(normalized["finalized_on"], "Z-Out Finalized On"),
            "snapshot_json": normalized["snapshot_json"],
            "snapshot_sha256": normalized["snapshot_sha256"],
            "sync_state": "Pending Edge",
        })
        doc.insert(ignore_permissions=True)

    return {
        **accepted,
        "edge_zout_uuid": normalized["edge_zout_uuid"],
        "official_finalized_offline": True,
        "official_snapshot_sha256": normalized["snapshot_sha256"],
        "canonical_encoder_zout_created": False,
    }


@frappe.whitelist()
def open_cashier_shift_offline(event_uuid, payload, device_id):
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    return accept_cashier_shift_open_at_edge(event_uuid, payload, device_id=device_id)


@frappe.whitelist()
def close_cashier_shift_offline(event_uuid, payload, device_id):
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    return accept_cashier_shift_close_at_edge(event_uuid, payload, device_id=device_id)


@frappe.whitelist()
def finalize_encoder_zout_offline(event_uuid, payload, device_id):
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    return accept_encoder_zout_finalization_at_edge(event_uuid, payload, device_id=device_id)
