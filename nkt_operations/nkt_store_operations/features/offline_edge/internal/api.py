from __future__ import annotations

from typing import Any, Dict, Optional

import frappe

from nkt_operations.nkt_store_operations.features.offline_edge.policy import (
    BUSINESS_TIMEZONE,
    device_policy_snapshot,
    event_family_policy,
    is_privileged_user,
    manila_now,
    submission_contract,
    touch_last_seen,
)
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import foundation_status


def _require_session_user() -> str:
    user = frappe.session.user
    if not user or user == "Guest":
        raise frappe.PermissionError("Device access unavailable.")
    return user


def sync_event_status_for_user(
    event_uuid: str,
    device_id: str,
    *,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    user = user or _require_session_user()
    caller_policy = device_policy_snapshot(device_id, user=user)

    row = frappe.db.get_value(
        "NKT Sync Event",
        event_uuid,
        [
            "event_uuid",
            "origin_device",
            "origin_user",
            "operational_context",
            "sync_state",
            "canonical_doctype",
            "canonical_name",
        ],
        as_dict=True,
    )
    if not row:
        raise frappe.PermissionError("Event status unavailable.")

    privileged = is_privileged_user(user)
    if not privileged and (row.origin_user != user or row.origin_device != device_id):
        raise frappe.PermissionError("Event status unavailable.")

    # No payload hash, customer data, technical error text, or broad journal
    # enumeration is returned to frontline clients.
    return {
        "event_uuid": row.event_uuid,
        "sync_state": row.sync_state,
        "canonical_doctype": row.canonical_doctype or None,
        "canonical_name": row.canonical_name or None,
        "operational_context": row.operational_context,
        "caller_ui_mode": caller_policy["ui_mode"],
    }


@frappe.whitelist()
def get_frontline_foundation_status(device_id: str, operational_context: Optional[str] = None) -> Dict[str, Any]:
    user = _require_session_user()
    policy = device_policy_snapshot(
        device_id,
        user=user,
        requested_context=operational_context,
    )
    touch_last_seen(device_id, user=user)
    return {
        "device": policy,
        "server_business_time": manila_now().isoformat(),
        "business_timezone": BUSINESS_TIMEZONE,
        "safe_sync": foundation_status(),
        "submission": submission_contract(),
    }


@frappe.whitelist()
def get_sync_event_status(event_uuid: str, device_id: str) -> Dict[str, Any]:
    return sync_event_status_for_user(event_uuid, device_id, user=_require_session_user())


@frappe.whitelist()
def get_family_sync_policy(event_family: str, device_id: str) -> Dict[str, Any]:
    user = _require_session_user()
    device_policy_snapshot(device_id, user=user)
    return event_family_policy(event_family)
