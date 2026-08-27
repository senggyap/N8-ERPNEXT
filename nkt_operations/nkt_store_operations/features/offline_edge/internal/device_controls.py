from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
from frappe.utils import now

from nkt_operations.nkt_store_operations.features.offline_edge.policy import (
    PRIVILEGED_ROLES,
    TERMINAL_DEVICE_STATUSES,
    _device_row,
    _require_authenticated,
    _roles,
    device_policy_snapshot,
    user_security_snapshot,
)

FOUNDATION_VERSION = "C15C.8A-R1"
RESTRICTABLE_SCOPES = {"User", "Device", "Both"}
TERMINAL_OWNER_STATUSES = {"Revoked", "Lost/Stolen", "Retired"}


def _require_privileged(user: Optional[str] = None) -> str:
    user = _require_authenticated(user)
    if not (_roles(user) & PRIVILEGED_ROLES):
        raise frappe.PermissionError("Security control unavailable.")
    return user


OWNER_ADMIN_SECURITY_ROLES = frozenset({"NKT OWNER", "NKT ADMINISTRATOR"})


def _require_owner_admin(user: Optional[str] = None) -> str:
    user = _require_authenticated(user)
    if not (_roles(user) & OWNER_ADMIN_SECURITY_ROLES):
        raise frappe.PermissionError("Security control unavailable.")
    return user


def _require_reason(reason: Optional[str]) -> str:
    reason = str(reason or "").strip()
    if not reason:
        raise frappe.ValidationError("Reason is required.")
    return reason


def _scope(scope: str) -> str:
    scope = str(scope or "").strip()
    if scope not in RESTRICTABLE_SCOPES:
        raise frappe.ValidationError("Invalid restriction scope.")
    return scope


def _require_user(user: Optional[str]) -> str:
    user = str(user or "").strip()
    if not user or not frappe.db.exists("User", user):
        raise frappe.ValidationError("Valid User is required.")
    return user


def _device_doc(device_id: str):
    _device_row(device_id)
    return frappe.get_doc("NKT Device Registry", device_id)


def _ensure_user_state(user: str):
    if frappe.db.exists("NKT User Security State", user):
        return frappe.get_doc("NKT User Security State", user)
    return frappe.get_doc({
        "doctype": "NKT User Security State",
        "user": user,
        "status": "Active",
        "policy_version": 1,
    })


def _restrict_device(device_id: str, reason: str):
    doc = _device_doc(device_id)
    if doc.status in TERMINAL_DEVICE_STATUSES:
        raise frappe.PermissionError("Security control unavailable.")
    if doc.status != "Restricted":
        doc.status = "Restricted"
    doc.restriction_reason = reason
    doc.save(ignore_permissions=True)
    return doc


def _restore_device(device_id: str, notes: Optional[str] = None):
    doc = _device_doc(device_id)
    if doc.status in TERMINAL_DEVICE_STATUSES:
        # Revoked/Lost/Retired is deliberately not the same as temporary restriction.
        raise frappe.PermissionError("Security control unavailable.")
    if doc.status == "Restricted":
        doc.status = "Active"
        doc.save(ignore_permissions=True)
    return doc


def _restrict_user(user: str, reason: str):
    doc = _ensure_user_state(user)
    doc.status = "Restricted"
    doc.restriction_reason = reason
    doc.save(ignore_permissions=True)
    return doc


def _restore_user(user: str, notes: Optional[str] = None):
    doc = _ensure_user_state(user)
    doc.status = "Active"
    doc.restore_notes = str(notes or "").strip()
    doc.save(ignore_permissions=True)
    return doc


def _assert_pair(user: str, device_id: str) -> None:
    row = _device_row(device_id)
    assigned = row.get("assigned_user")
    if assigned and assigned != user:
        raise frappe.ValidationError("User and Device assignment do not match.")


def set_restriction(
    scope: str,
    *,
    user: Optional[str] = None,
    device_id: Optional[str] = None,
    reason: Optional[str] = None,
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    actor = _require_privileged(actor)
    scope = _scope(scope)
    reason = _require_reason(reason)

    user_value = _require_user(user) if scope in {"User", "Both"} else None
    device_value = str(device_id or "").strip() if scope in {"Device", "Both"} else None
    if scope in {"Device", "Both"}:
        _device_row(device_value)
    if scope == "Both":
        _assert_pair(user_value, device_value)

    if scope in {"User", "Both"}:
        _restrict_user(user_value, reason)
    if scope in {"Device", "Both"}:
        _restrict_device(device_value, reason)

    return {
        "scope": scope,
        "user": user_value,
        "device_id": device_value,
        "status": "Restricted",
        "actor": actor,
    }


def restore_restriction(
    scope: str,
    *,
    user: Optional[str] = None,
    device_id: Optional[str] = None,
    notes: Optional[str] = None,
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    actor = _require_owner_admin(actor)
    scope = _scope(scope)

    user_value = _require_user(user) if scope in {"User", "Both"} else None
    device_value = str(device_id or "").strip() if scope in {"Device", "Both"} else None
    if scope in {"Device", "Both"}:
        _device_row(device_value)
    if scope == "Both":
        _assert_pair(user_value, device_value)

    if scope in {"User", "Both"}:
        _restore_user(user_value, notes)
    if scope in {"Device", "Both"}:
        _restore_device(device_value, notes)

    return {
        "scope": scope,
        "user": user_value,
        "device_id": device_value,
        "status": "Active",
        "actor": actor,
    }


def mark_terminal_device(
    device_id: str,
    status: str,
    *,
    reason: Optional[str] = None,
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    actor = _require_owner_admin(actor)
    status = str(status or "").strip()
    if status not in TERMINAL_OWNER_STATUSES:
        raise frappe.ValidationError("Invalid terminal device status.")
    reason = _require_reason(reason)

    doc = _device_doc(device_id)
    doc.status = status
    doc.revocation_reason = reason
    doc.save(ignore_permissions=True)

    return {
        "device_id": device_id,
        "status": status,
        "actor": actor,
        "client_access": "unavailable",
        "local_action": "crypto_erase_sensitive_state",
        "preserve_device_id": True,
    }


def self_restrict_device(
    device_id: str,
    *,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Employee self-trigger primitive for the future Ctrl+Alt+Shift+F12 shortcut.

    No confirmation belongs in the client flow. The shortcut wiring itself is
    staged for C15C.8B together with the exact limited Fast Screen rendering.
    """
    user = _require_authenticated(user)
    row = _device_row(device_id)

    if row.get("status") in TERMINAL_DEVICE_STATUSES:
        raise frappe.PermissionError("Device access unavailable.")

    assigned = row.get("assigned_user")
    if not assigned or assigned != user:
        raise frappe.PermissionError("Device access unavailable.")

    doc = frappe.get_doc("NKT Device Registry", device_id)
    if doc.status != "Restricted":
        doc.status = "Restricted"
        doc.restriction_reason = "Employee self-restricted from approved client shortcut."
        doc.save(ignore_permissions=True)
    
    return {
        "device_id": device_id,
        "ui_mode": "limited",
        "restore_allowed_for_employee": False,
        "message": "Limited mode enabled.",
    }


def client_security_bootstrap(
    device_id: str,
    *,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Minimal client security bootstrap.

    For terminal devices the user-facing message remains generic while the
    approved client receives an internal wipe instruction. Device UUID is
    preserved so central revocation remains enforceable after local wipe.
    """
    user = _require_authenticated(user)
    row = _device_row(device_id)
    privileged = bool(_roles(user) & PRIVILEGED_ROLES)
    assigned = row.get("assigned_user")

    if assigned and assigned != user and not privileged:
        raise frappe.PermissionError("Device access unavailable.")

    if row.get("status") in TERMINAL_DEVICE_STATUSES:
        return {
            "access": "unavailable",
            "message": "Device access unavailable.",
            "local_action": "crypto_erase_sensitive_state",
            "preserve_device_id": True,
            "critical_offline_mutations_enabled": False,
        }

    policy = device_policy_snapshot(device_id, user=user)
    frappe.db.set_value(
        "NKT Device Registry",
        device_id,
        "last_seen_at",
        now(),
        update_modified=False,
    )
    user_policy = user_security_snapshot(user)

    return {
        "access": "ok",
        "ui_mode": policy["ui_mode"],
        "device_policy_version": policy["policy_version"],
        "user_policy_version": user_policy["policy_version"],
        "critical_offline_mutations_enabled": False,
        "local_action": "none",
    }


@frappe.whitelist()
def owner_set_restriction(
    scope: str,
    user: Optional[str] = None,
    device_id: Optional[str] = None,
    reason: Optional[str] = None,
):
    return set_restriction(
        scope,
        user=user,
        device_id=device_id,
        reason=reason,
    )


@frappe.whitelist()
def owner_restore_restriction(
    scope: str,
    user: Optional[str] = None,
    device_id: Optional[str] = None,
    notes: Optional[str] = None,
):
    return restore_restriction(
        scope,
        user=user,
        device_id=device_id,
        notes=notes,
    )


@frappe.whitelist()
def owner_mark_terminal_device(
    device_id: str,
    status: str,
    reason: Optional[str] = None,
):
    return mark_terminal_device(
        device_id,
        status,
        reason=reason,
    )


@frappe.whitelist()
def self_restrict_current_device(device_id: str):
    return self_restrict_device(device_id)


@frappe.whitelist()
def get_client_security_bootstrap(device_id: str):
    return client_security_bootstrap(device_id)
