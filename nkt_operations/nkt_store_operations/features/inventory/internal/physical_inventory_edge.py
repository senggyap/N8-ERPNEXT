from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import now

from nkt_operations.nkt_store_operations.features.inventory.internal.physical_inventory_offline_intent import (
    ACTION,
    FAMILY,
    canonical_physical_inventory_count_intent_json,
    normalize_physical_inventory_count_intent,
)
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
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role

FOUNDATION_VERSION = "C15C.10J-R6D"
PH_TZ = ZoneInfo("Asia/Manila")


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _session_user(user=None) -> str:
    user = str(user or frappe.session.user or "").strip()
    if not user or user == "Guest":
        raise frappe.PermissionError("Offline Physical Inventory unavailable.")
    return user


def _mysql_local_datetime(value: Any, label: str) -> datetime:
    """Convert an immutable Manila observation time to Frappe/MySQL Datetime form.

    Canonical payload JSON keeps the original timezone-bearing ISO timestamp.
    Only the technical DocType Datetime value is made naive in Asia/Manila.
    """
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


def accept_physical_inventory_count_at_edge(
    event_uuid: str,
    payload: Dict[str, Any],
    *,
    device_id: str,
    user: str | None = None,
) -> Dict[str, Any]:
    """Durably accept one immutable physical-count observation at Store Edge.

    This creates only the technical NKT Sync Event + Pending Payload journal.
    It does not create NKT Physical Inventory Adjustment, Stock Reconciliation,
    Stock Ledger Entry, Bin mutation, or any canonical stock adjustment.
    """
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Offline Physical Inventory Edge acceptance unavailable.")

    event_uuid = _uuid(event_uuid, "Physical Inventory Event UUID")
    user = _session_user(user)

    if event_family_policy(FAMILY).get("offline_write_allowed") is not True:
        raise frappe.PermissionError("Offline Physical Inventory unavailable.")

    device = device_policy_snapshot(device_id, user=user)
    normalized = normalize_physical_inventory_count_intent(payload)

    if normalized["counted_by"] != user:
        raise frappe.PermissionError(
            "Physical Inventory Counted / Entered By must match the authenticated Edge user."
        )
    roles = set(frappe.get_roles(user) or [])
    if normalized["entry_role"] not in roles:
        raise frappe.PermissionError(
            "Physical Inventory Entry Role is not assigned to the authenticated Edge user."
        )

    validate_business_time(
        normalized["business_date"],
        normalized["count_datetime"],
    )

    canonical_json = canonical_physical_inventory_count_intent_json(normalized)
    digest = canonical_payload_hash(normalized)
    if digest != canonical_payload_hash(json.loads(canonical_json)):
        raise NKTIdempotencyConflict("Physical Inventory canonical payload hash is unstable.")

    # client_created_at is part of the shared safe-sync immutable identity.
    # A retry/replay of the same Event UUID must therefore reuse the timestamp
    # captured by the original NKT Sync Event rather than generating a new now().
    existing_event_name = frappe.db.exists("NKT Sync Event", event_uuid)
    if existing_event_name:
        existing_client_created_at = frappe.db.get_value(
            "NKT Sync Event",
            existing_event_name,
            "client_created_at",
        )
        client_created_at = existing_client_created_at
    else:
        client_created_at = now()

    envelope = {
        "event_uuid": event_uuid,
        "event_family": FAMILY,
        "event_action": ACTION,
        "operational_context": device.get("operational_context") or "NKT Retail",
        "origin_device": str(device_id),
        "origin_user": user,
        "business_date": normalized["business_date"],
        "settled_at": _mysql_local_datetime(normalized["count_datetime"], "Physical Count Time"),
        "client_created_at": client_created_at,
        "payload_sha256": digest,
    }

    event, replay = begin_event(envelope)
    pending_name = frappe.db.exists("NKT Sync Pending Payload", event_uuid)

    if pending_name:
        pending = frappe.get_doc("NKT Sync Pending Payload", pending_name)
        if (
            pending.event_family != FAMILY
            or str(pending.payload_sha256 or "").lower() != digest
            or str(pending.payload_json or "") != canonical_json
        ):
            raise NKTIdempotencyConflict(
                "Physical Inventory pending payload conflicts with immutable event content."
            )
    elif event.sync_state in ("Received", "Accepted at Edge", "Awaiting Primary"):
        frappe.get_doc(
            {
                "doctype": "NKT Sync Pending Payload",
                "event_uuid": event_uuid,
                "event_family": FAMILY,
                "payload_sha256": digest,
                "queue_state": "Accepted at Edge",
                "payload_json": canonical_json,
                "edge_accepted_at": now(),
                "attempt_count": 0,
            }
        ).insert(ignore_permissions=True)
    elif event.sync_state == "Committed at Primary":
        return {
            "event_uuid": event_uuid,
            "event_family": FAMILY,
            "sync_state": event.sync_state,
            "payload_sha256": digest,
            "replay": True,
            "pending_payload_created": False,
            "canonical_stock_adjustment_created": False,
        }
    else:
        raise frappe.ValidationError(
            "Physical Inventory event is not eligible for Edge acceptance."
        )

    if event.sync_state in ("Received", "Accepted at Edge", "Awaiting Primary"):
        mark_edge_accepted(event_uuid)
        event.reload()

    return {
        "event_uuid": event_uuid,
        "event_family": FAMILY,
        "sync_state": event.sync_state,
        "payload_sha256": digest,
        "replay": bool(replay),
        "pending_payload_created": not bool(pending_name),
        "canonical_stock_adjustment_created": False,
    }


@frappe.whitelist()
def accept_physical_inventory_count(event_uuid, payload, device_id):
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    return accept_physical_inventory_count_at_edge(
        event_uuid,
        payload,
        device_id=device_id,
    )
