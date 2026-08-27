from __future__ import annotations

from typing import Dict, Iterable

import frappe

FOUNDATION_VERSION = "C15C.10L-R3D"

OWNER_ADMIN = ("NKT OWNER", "NKT ADMINISTRATOR")
JOB_DOCTYPE = "NKT Trucking Job"
SENSITIVE_EXTERNAL_DOCTYPES = (
    "NKT Trucker SOA",
    "NKT Trucker Payment",
    "NKT Trucker Adjustment",
)

EXTERNAL_CARRIER_REPORT = "NKT External Carrier Payable Queue"

FULL_MANAGE = {
    "read": 1,
    "write": 1,
    "create": 1,
    "delete": 1,
    "submit": 0,
    "cancel": 0,
    "amend": 0,
    "report": 1,
    "export": 1,
    "import": 0,
    "print": 1,
    "email": 1,
    "share": 1,
    "select": 1,
    "if_owner": 0,
}

JOB_OWNER_ADMIN = dict(FULL_MANAGE)


def _rows(parent: str):
    return frappe.get_all(
        "Custom DocPerm",
        filters={"parent": parent, "permlevel": 0},
        fields=[
            "name", "parent", "role", "permlevel",
            "read", "write", "create", "delete", "submit", "cancel", "amend",
            "report", "export", "import", "print", "email", "share", "select",
            "if_owner",
        ],
        order_by="creation asc, name asc",
    )


def _upsert(parent: str, role: str, values: Dict[str, int]) -> None:
    matches = [row for row in _rows(parent) if row.role == role]
    if matches:
        keep = matches[0]
        frappe.db.set_value(
            "Custom DocPerm",
            keep.name,
            values,
            update_modified=False,
        )
        for duplicate in matches[1:]:
            frappe.delete_doc(
                "Custom DocPerm",
                duplicate.name,
                ignore_permissions=True,
                force=True,
            )
        return

    frappe.get_doc({
        "doctype": "Custom DocPerm",
        "parent": parent,
        "parenttype": "DocType",
        "parentfield": "permissions",
        "role": role,
        "permlevel": 0,
        **values,
    }).insert(ignore_permissions=True)


def _delete_roles_except(parent: str, allowed_roles: Iterable[str]) -> None:
    allowed = set(allowed_roles)
    for row in _rows(parent):
        if row.role not in allowed:
            frappe.delete_doc(
                "Custom DocPerm",
                row.name,
                ignore_permissions=True,
                force=True,
            )


def _repair_external_carrier_report_roles() -> None:
    """Repair persisted Report roles without saving the standard Report document.

    In developer mode, Report.save() can serialize a Standard Report back into the
    application tree and generate report source artifacts. That is forbidden here.
    We therefore mutate only the `Has Role` child rows in the database.
    """
    existing = frappe.get_all(
        "Has Role",
        filters={
            "parent": EXTERNAL_CARRIER_REPORT,
            "parenttype": "Report",
            "parentfield": "roles",
        },
        fields=["name", "role", "idx"],
        order_by="idx asc, creation asc, name asc",
    )
    target = list(OWNER_ADMIN)
    current = [str(row.role or "").strip() for row in existing]
    if current == target and len(existing) == 2:
        return

    for row in existing:
        frappe.delete_doc(
            "Has Role",
            row.name,
            ignore_permissions=True,
            force=True,
        )

    for idx, role in enumerate(target, start=1):
        child = frappe.get_doc({
            "doctype": "Has Role",
            "parent": EXTERNAL_CARRIER_REPORT,
            "parenttype": "Report",
            "parentfield": "roles",
            "idx": idx,
            "role": role,
        })
        child.db_insert()

    frappe.clear_cache(doctype="Report")


def _report_role_contract() -> Dict[str, object]:
    report = frappe.get_doc("Report", EXTERNAL_CARRIER_REPORT)
    roles = [str(row.role or "").strip() for row in report.roles]
    return {
        "report": EXTERNAL_CARRIER_REPORT,
        "effective_roles": roles,
        "owner_admin_only": set(roles) == set(OWNER_ADMIN) and len(roles) == 2,
    }


def repair_external_carrier_custom_docperms() -> Dict[str, object]:
    """Make the effective Custom DocPerm layer match the locked C15C.10L privacy rule.

    Why this exists:
    Frappe Custom DocPerm rows override the static DocType permission table. The live
    NKT Trucking Job had a Custom DocPerm layer containing only NKT Trucking Operations,
    so the static OWNER/ADMIN rows in the DocType JSON were not enough to guarantee the
    user's requested flexibility.

    This repair:
    - preserves existing employee Trucking Job custom permissions;
    - explicitly adds OWNER + ADMIN to effective Trucking Job permissions;
    - makes the external-carrier commercial doctypes OWNER + ADMIN only;
    - does not grant ordinary employees access to the external commercial side.
    """
    for role in OWNER_ADMIN:
        _upsert(JOB_DOCTYPE, role, JOB_OWNER_ADMIN)

    for doctype in SENSITIVE_EXTERNAL_DOCTYPES:
        _delete_roles_except(doctype, OWNER_ADMIN)
        for role in OWNER_ADMIN:
            _upsert(doctype, role, FULL_MANAGE)

    for doctype in (JOB_DOCTYPE, *SENSITIVE_EXTERNAL_DOCTYPES):
        frappe.clear_cache(doctype=doctype)

    _repair_external_carrier_report_roles()
    return contract_status()


def contract_status() -> Dict[str, object]:
    job_roles = {row.role for row in _rows(JOB_DOCTYPE) if int(row.read or 0)}
    sensitive = {
        doctype: sorted(
            row.role for row in _rows(doctype) if int(row.read or 0)
        )
        for doctype in SENSITIVE_EXTERNAL_DOCTYPES
    }
    report_contract = _report_role_contract()
    return {
        "foundation_version": FOUNDATION_VERSION,
        "trucking_job_effective_custom_read_roles": sorted(job_roles),
        "trucking_job_owner_admin_guaranteed": set(OWNER_ADMIN).issubset(job_roles),
        "sensitive_external_effective_custom_read_roles": sensitive,
        "sensitive_external_owner_admin_only": all(
            set(roles) == set(OWNER_ADMIN) for roles in sensitive.values()
        ),
        "ordinary_trucking_job_employee_permission_preserved": (
            "NKT Trucking Operations" in job_roles
        ),
        "external_carrier_report_effective_roles": report_contract["effective_roles"],
        "external_carrier_report_owner_admin_only": report_contract["owner_admin_only"],
    }


def after_migrate():
    repair_external_carrier_custom_docperms()
