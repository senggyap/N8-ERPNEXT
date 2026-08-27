from __future__ import annotations

from typing import Any

import frappe

FOUNDATION_VERSION = "C15C.10L-R3"

NORMAL_EXTERNAL_CARRIER_ROLES = {"NKT OWNER", "NKT ADMINISTRATOR"}
TECHNICAL_SUPPORT_ROLES = {"System Manager"}


def _user(user=None) -> str:
    user = str(user or frappe.session.user or "").strip()
    if not user or user == "Guest":
        raise frappe.PermissionError("Trucking access unavailable.")
    return user


def _roles(user: str) -> set[str]:
    return set(frappe.get_roles(user) or [])


def roles_authorize_external_carrier(roles, *, user="") -> bool:
    roles = set(roles or [])
    if str(user or "") == "Administrator":
        return True
    # C15F R2B: System Manager remains technical support, but external-carrier
    # commercial browsing is a normal-business privilege of Owner/Admin only.
    return bool(roles & NORMAL_EXTERNAL_CARRIER_ROLES)


def is_external_carrier_privileged(user=None) -> bool:
    user = _user(user)
    return roles_authorize_external_carrier(_roles(user), user=user)


def require_external_carrier_access(user=None) -> None:
    if not is_external_carrier_privileged(user):
        raise frappe.PermissionError("External carrier records are restricted to Owner/Admin.")


def _vehicle_ownership(vehicle: str) -> str:
    if not str(vehicle or "").strip():
        return ""
    return str(
        frappe.db.get_value("NKT Vehicle", vehicle, "custom_fleet_ownership") or ""
    ).strip()


def _job_is_employee_visible(job_name: str) -> bool:
    if not str(job_name or "").strip():
        return True
    row = frappe.db.get_value(
        "NKT Trucking Job",
        job_name,
        ["delivery_vehicle", "carrier_account"],
        as_dict=True,
    )
    if not row:
        return False
    if str(row.carrier_account or "").strip():
        return False
    return _vehicle_ownership(row.delivery_vehicle) == "ENT-Owned"


def validate_employee_trip_scope(doc, method=None):
    """Block ordinary employees from creating/editing external-carrier trips."""
    user = _user()
    if is_external_carrier_privileged(user):
        return

    if str(doc.get("fleet_ownership_snapshot") or "") == "External Carrier":
        raise frappe.PermissionError("External carrier trips are restricted to Owner/Admin.")
    if str(doc.get("carrier_account_snapshot") or "").strip():
        raise frappe.PermissionError("External carrier trips are restricted to Owner/Admin.")

    vehicle = str(doc.get("vehicle") or "").strip()
    if vehicle:
        ownership = _vehicle_ownership(vehicle)
        if ownership == "External Carrier":
            raise frappe.PermissionError("External carrier trucks are restricted to Owner/Admin.")

    source_job = str(doc.get("source_c9_trucking_job") or "").strip()
    if source_job and not _job_is_employee_visible(source_job):
        raise frappe.PermissionError(
            "External carrier Supplier Arrival trucking records are restricted to Owner/Admin."
        )


def get_trip_permission_query_conditions(user=None):
    user = _user(user)
    if is_external_carrier_privileged(user):
        return ""
    return (
        "IFNULL(`tabNKT Trucking Trip`.`fleet_ownership_snapshot`, '') != 'External Carrier' "
        "AND IFNULL(`tabNKT Trucking Trip`.`carrier_account_snapshot`, '') = ''"
    )


def has_trip_permission(doc, user=None, permission_type=None):
    user = _user(user)
    if is_external_carrier_privileged(user):
        return True
    if permission_type == "create" or getattr(doc, "is_new", lambda: False)():
        return True
    return (
        str(doc.get("fleet_ownership_snapshot") or "") != "External Carrier"
        and not str(doc.get("carrier_account_snapshot") or "").strip()
    )


def get_waybill_permission_query_conditions(user=None):
    user = _user(user)
    if is_external_carrier_privileged(user):
        return ""
    return (
        "(IFNULL(`tabNKT Trucking Waybill`.`trip`, '') = '' OR EXISTS ("
        "SELECT 1 FROM `tabNKT Trucking Trip` t "
        "WHERE t.name=`tabNKT Trucking Waybill`.`trip` "
        "AND IFNULL(t.fleet_ownership_snapshot,'') != 'External Carrier' "
        "AND IFNULL(t.carrier_account_snapshot,'') = ''))"
    )


def has_waybill_permission(doc, user=None, permission_type=None):
    user = _user(user)
    if is_external_carrier_privileged(user):
        return True
    trip = str(doc.get("trip") or "").strip()
    if not trip:
        return True
    row = frappe.db.get_value(
        "NKT Trucking Trip",
        trip,
        ["fleet_ownership_snapshot", "carrier_account_snapshot"],
        as_dict=True,
    )
    return bool(
        row
        and str(row.fleet_ownership_snapshot or "") != "External Carrier"
        and not str(row.carrier_account_snapshot or "").strip()
    )


def get_job_permission_query_conditions(user=None):
    user = _user(user)
    if is_external_carrier_privileged(user):
        return ""
    return (
        "IFNULL(`tabNKT Trucking Job`.`carrier_account`, '') = '' "
        "AND EXISTS (SELECT 1 FROM `tabNKT Vehicle` v "
        "WHERE v.name=`tabNKT Trucking Job`.`delivery_vehicle` "
        "AND IFNULL(v.custom_fleet_ownership,'')='ENT-Owned')"
    )


def has_job_permission(doc, user=None, permission_type=None):
    user = _user(user)
    if is_external_carrier_privileged(user):
        return True
    if str(doc.get("carrier_account") or "").strip():
        return False
    return _vehicle_ownership(str(doc.get("delivery_vehicle") or "")) == "ENT-Owned"


def deny_external_commercial_query(user=None):
    user = _user(user)
    return "" if is_external_carrier_privileged(user) else "1=0"


def has_external_commercial_permission(doc, user=None, permission_type=None):
    return bool(is_external_carrier_privileged(user))


def contract_status():
    return {
        "normal_external_carrier_roles": sorted(NORMAL_EXTERNAL_CARRIER_ROLES),
        "ordinary_employee_external_trip_browse": False,
        "ordinary_employee_external_trip_create_or_edit": False,
        "ordinary_employee_trucker_soa_payment_adjustment_access": False,
        "sanitized_supplier_receiving_exception_preserved": True,
        "employee_ent_owned_trip_access": True,
    }
