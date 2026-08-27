from __future__ import annotations

import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional

import frappe
from frappe.utils import now

from nkt_operations.nkt_store_operations.features.offline_edge.policy import (
    device_policy_snapshot,
    event_family_policy,
    validate_business_time,
)
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    begin_event,
    canonical_payload_hash,
    mark_edge_accepted,
)

FOUNDATION_VERSION = "C15C.9A-R2"
PH_TZ = ZoneInfo("Asia/Manila")
PROBE_FAMILY = "NKT Safe Sync Probe"
PROBE_ACTION = "Probe"
ALLOWED_PAYLOAD_KEYS = {"probe_uuid","client_sequence","client_observed_at"}


def _session_user(user: Optional[str]=None) -> str:
    user=user or frappe.session.user
    if not user or user=="Guest":
        raise frappe.PermissionError("Safe-sync probe unavailable.")
    return user


def _runtime_role() -> str:
    return str(frappe.conf.get("nkt_runtime_role") or "Primary").strip()


def _normalize_probe_payload(payload: Dict[str,Any]) -> Dict[str,Any]:
    if not isinstance(payload,dict):
        raise frappe.ValidationError("Probe payload must be an object.")
    extra=set(payload)-ALLOWED_PAYLOAD_KEYS
    if extra:
        raise frappe.ValidationError("Probe payload contains unsupported fields.")

    try:
        probe_uuid=str(uuid.UUID(str(payload.get("probe_uuid") or "")))
    except Exception as exc:
        raise frappe.ValidationError("Probe UUID must be a valid UUID.") from exc

    try:
        seq=int(payload.get("client_sequence"))
    except Exception as exc:
        raise frappe.ValidationError("Client sequence must be an integer.") from exc
    if seq < 0:
        raise frappe.ValidationError("Client sequence must be zero or greater.")

    observed=str(payload.get("client_observed_at") or "").strip()
    if not observed:
        raise frappe.ValidationError("Client observed time is required.")

    return {
        "probe_uuid":probe_uuid,
        "client_sequence":seq,
        "client_observed_at":observed,
    }


def _canonical_payload_json(payload: Dict[str,Any]) -> str:
    return json.dumps(payload,sort_keys=True,separators=(",",":"),ensure_ascii=False,default=str)


def _manila_sql_datetime(value: Any, *, field_label: str) -> str:
    """
    Convert an already-validated/accepted business timestamp to the naive Manila
    wall-clock representation required by MariaDB/Frappe Datetime fields.

    Canonical payload hashing is NOT changed by this storage conversion.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except Exception as exc:
            raise frappe.ValidationError(f"{field_label} is not a valid datetime.") from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PH_TZ)
    else:
        dt = dt.astimezone(PH_TZ)

    return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def accept_probe_at_edge(
    event_uuid: str,
    device_id: str,
    business_date: Any,
    settled_at: Any,
    payload: Dict[str,Any],
    *,
    user: Optional[str]=None,
) -> Dict[str,Any]:
    """
    First real offline-capable adapter, deliberately non-money/non-stock.

    Caller owns transaction. No commit occurs here.
    """
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Safe-sync probe unavailable.")

    user=_session_user(user)
    policy=device_policy_snapshot(device_id,user=user)
    if policy.get("ui_mode") != "normal":
        raise frappe.PermissionError("Safe-sync probe unavailable.")

    family_policy=event_family_policy(PROBE_FAMILY)
    if family_policy.get("offline_write_allowed") is not True:
        raise frappe.PermissionError("Safe-sync probe unavailable.")

    business=validate_business_time(business_date,settled_at)
    normalized=_normalize_probe_payload(payload)
    digest=canonical_payload_hash(normalized)

    envelope={
        "event_uuid":event_uuid,
        "event_family":PROBE_FAMILY,
        "event_action":PROBE_ACTION,
        "operational_context":policy.get("operational_context") or "Other",
        "origin_device":device_id,
        "origin_user":user,
        "business_date":business["business_date"],
        "settled_at":_manila_sql_datetime(
            business["settled_at_manila"],
            field_label="Business / Settled Time",
        ),
        "client_created_at":_manila_sql_datetime(
            normalized["client_observed_at"],
            field_label="Client observed time",
        ),
        "payload_sha256":digest,
    }

    event,replay=begin_event(envelope)

    pending_name=frappe.db.exists("NKT Sync Pending Payload",event.event_uuid)
    if pending_name:
        pending=frappe.get_doc("NKT Sync Pending Payload",pending_name)
        if pending.payload_sha256 != digest:
            raise NKTIdempotencyConflict("Pending payload hash conflicts with immutable Event UUID.")
        if pending.payload_json != _canonical_payload_json(normalized):
            raise NKTIdempotencyConflict("Pending payload content conflicts with immutable Event UUID.")
        frappe.db.set_value(
            "NKT Sync Pending Payload",
            pending.name,
            {"attempt_count":int(pending.attempt_count or 0)+1,"last_attempt_at":now()},
            update_modified=False,
        )
    else:
        if replay and event.sync_state not in ("Committed at Primary","Conflict","Failed"):
            raise frappe.ValidationError("Safe-sync event exists but durable pending payload is unavailable.")

        if event.sync_state not in ("Committed at Primary","Conflict","Failed"):
            frappe.get_doc({
                "doctype":"NKT Sync Pending Payload",
                "event_uuid":event.event_uuid,
                "event_family":PROBE_FAMILY,
                "payload_sha256":digest,
                "payload_json":_canonical_payload_json(normalized),
                "queue_state":"Accepted at Edge",
                "edge_accepted_at":now(),
                "attempt_count":1,
                "last_attempt_at":now(),
            }).insert(ignore_permissions=True)

    if event.sync_state not in ("Committed at Primary","Conflict","Failed"):
        mark_edge_accepted(event.event_uuid)
        event.reload()

    return {
        "event_uuid":event.event_uuid,
        "sync_state":event.sync_state,
        "durable_ack":True,
        "replay":bool(replay),
        "payload_sha256":digest,
        "critical_business_record_created":False,
        "money_or_stock_effect":False,
        "offline_family":PROBE_FAMILY,
    }


@frappe.whitelist()
def submit_safe_sync_probe(
    event_uuid: str,
    device_id: str,
    business_date: str,
    settled_at: str,
    probe_uuid: str,
    client_sequence: int,
    client_observed_at: str,
):
    payload={
        "probe_uuid":probe_uuid,
        "client_sequence":client_sequence,
        "client_observed_at":client_observed_at,
    }
    return accept_probe_at_edge(
        event_uuid,
        device_id,
        business_date,
        settled_at,
        payload,
        user=_session_user(),
    )
