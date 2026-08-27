from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime

from nkt_operations.nkt_store_operations.features.payments_accounts.card_surcharge import normalize_payment_method
from nkt_operations.nkt_store_operations.features.payments_accounts.cash_ledger import (
    _preserved_cash_drawer_shift_validation_context,
    _preserved_return_exchange_shift_context_matches,
)


VERSION = "1.9.6-MP2-MANAGER-PIN-EOD"
MODULE = "NKT Store Operations"
APP = "nkt_operations"
TOLERANCE = 0.005

SHIFT = "NKT Cashier Shift"
MOVEMENT = "NKT Cashier Movement"
ADJUSTMENT = "NKT Cash Drawer Adjustment"
CONTROL_LOG = "NKT Shift Control Log"
WORKSPACE = "NKT Cashier Operations"

ADMIN_ROLES = {"System Manager", "NKT OWNER", "NKT ADMINISTRATOR"}
LOW_OPERATIONAL_ROLES = {"NKT Cashier", "NKT Encoder"}
CASHIER_ROLE = "NKT Cashier"
ENCODER_ROLE = "NKT Encoder"

OPEN_SHIFT_STATUSES = {"Not Opened", "Open"}
CASHIER_CLOSED_STATUS = "Cashier Closed - Awaiting Review"
COUNTED_STATUSES = {CASHIER_CLOSED_STATUS, "Counted - Awaiting Approval", "Turned Over - Awaiting Review"}
FINAL_SHIFT_STATUSES = {"Closed", "Reviewed / Closed", "Cancelled"}

DENOMINATIONS = {
    "bill_1000_qty": 1000.0,
    "bill_500_qty": 500.0,
    "bill_200_qty": 200.0,
    "bill_100_qty": 100.0,
    "bill_50_qty": 50.0,
    "bill_20_qty": 20.0,
    "coin_20_qty": 20.0,
    "coin_10_qty": 10.0,
    "coin_5_qty": 5.0,
    "coin_1_qty": 1.0,
    "coin_025_qty": 0.25,
}

DEPOSIT_DENOMINATIONS = {f"deposit_{fieldname}": value for fieldname, value in DENOMINATIONS.items()}

SHIFT_STATUS_OPTIONS = "\n".join([
    "Not Opened",
    "Open",
    CASHIER_CLOSED_STATUS,
    "Reviewed / Closed",
    "Closed",
    "Cancelled",
    # Preserved for historical development records.
    "Counted - Awaiting Approval",
    "Turned Over - Awaiting Review",
])

TURNOVER_STATUS_OPTIONS = "\n".join([
    "Not Turned Over",
    CASHIER_CLOSED_STATUS,
    "Reviewed / OK",
    "Reviewed with Difference",
    "Reopened for Correction",
    # Preserved historical values.
    "Turned Over - Awaiting Review",
    "Awaiting Confirmation",
    "Turned Over",
])

MOVEMENT_TYPE_OPTIONS = "\n".join([
    "Customer Order Payment",
    "Customer Account Collection",
    "Customer Return Refund",
    "Exchange Difference Collected",
    "Exchange Difference Refunded",
    "Petty Cash Release",
    "Petty Cash Return",
    "Cash Drop",
    "Advance / Mid-Shift Deposit",
    "Advance Deposit Reversal",
    "Other Cash In",
    "Other Cash Out",
    "Account Collection",
])

ADJUSTMENT_MAP = {
    "Petty Cash Release": {"movement_type": "Petty Cash Release", "direction": "Out"},
    "Petty Cash Return": {"movement_type": "Petty Cash Return", "direction": "In"},
    "Cash Drop": {"movement_type": "Cash Drop", "direction": "Out"},
    "Advance / Mid-Shift Deposit": {"movement_type": "Advance / Mid-Shift Deposit", "direction": "Out"},
    "Other Cash In": {"movement_type": "Other Cash In", "direction": "In"},
    "Other Cash Out": {"movement_type": "Other Cash Out", "direction": "Out"},
}

REVERSAL_MAP = {
    "Petty Cash Release": {"movement_type": "Petty Cash Return", "direction": "In"},
    "Petty Cash Return": {"movement_type": "Petty Cash Release", "direction": "Out"},
    "Cash Drop": {"movement_type": "Other Cash In", "direction": "In"},
    "Advance / Mid-Shift Deposit": {"movement_type": "Advance Deposit Reversal", "direction": "In"},
    "Other Cash In": {"movement_type": "Other Cash Out", "direction": "Out"},
    "Other Cash Out": {"movement_type": "Other Cash In", "direction": "In"},
}


# ---------------------------------------------------------------------------
# Roles and permission helpers
# ---------------------------------------------------------------------------


def _roles(user=None):
    return set(frappe.get_roles(user or frappe.session.user))


def _is_admin(user=None):
    user = user or frappe.session.user
    return user == "Administrator" or bool(_roles(user).intersection(ADMIN_ROLES))


def _is_cashier(user=None):
    return CASHIER_ROLE in _roles(user or frappe.session.user)


def _is_encoder(user=None):
    return ENCODER_ROLE in _roles(user or frappe.session.user)


def _is_restricted_operational_user(user=None):
    user = user or frappe.session.user
    if _is_admin(user):
        return False
    return bool(_roles(user).intersection(LOW_OPERATIONAL_ROLES))


def _require_admin(action):
    if not _is_admin():
        frappe.throw(_("You are not authorized to {0}.").format(action), frappe.PermissionError)


def _require_cashier_or_admin(action):
    if not (_is_admin() or _is_cashier()):
        frappe.throw(_("Only the assigned Cashier or an authorized Owner/Administrator may {0}.").format(action), frappe.PermissionError)


def _user_has_role(user, role):
    return bool(
        frappe.db.exists(
            "Has Role",
            {"parenttype": "User", "parent": user, "role": role},
        )
    )


def _cashier_is_valid_operator(user):
    if not user:
        return False
    return user == "Administrator" or bool(_roles(user).intersection(ADMIN_ROLES)) or _user_has_role(user, CASHIER_ROLE)


def _sql_user(user):
    return frappe.db.escape(user or frappe.session.user)


# ---------------------------------------------------------------------------
# Row-level permissions
# ---------------------------------------------------------------------------


def get_shift_permission_query_conditions(user=None):
    user = user or frappe.session.user
    if _is_admin(user):
        return None
    if _is_cashier(user):
        return f"`tab{SHIFT}`.`cashier` = {_sql_user(user)}"
    return "1=0"


def has_shift_permission(doc, user=None, permission_type=None):
    user = user or frappe.session.user
    permission_type = permission_type or "read"
    if _is_admin(user):
        return True
    if not _is_cashier(user):
        return False
    if permission_type in {"delete", "cancel", "submit", "amend", "share", "email", "export"}:
        return False
    if permission_type == "create" and getattr(doc, "is_new", lambda: False)():
        return True
    return getattr(doc, "cashier", None) == user


def get_movement_permission_query_conditions(user=None):
    user = user or frappe.session.user
    if _is_admin(user):
        return None
    if _is_cashier(user):
        return f"`tab{MOVEMENT}`.`cashier` = {_sql_user(user)}"
    return "1=0"


def has_movement_permission(doc, user=None, permission_type=None):
    user = user or frappe.session.user
    permission_type = permission_type or "read"
    if _is_admin(user):
        return True
    if not _is_cashier(user):
        return False
    if permission_type not in {"read", "print", "select"}:
        return False
    return getattr(doc, "cashier", None) == user


def get_adjustment_permission_query_conditions(user=None):
    user = user or frappe.session.user
    if _is_admin(user):
        return None
    if _is_cashier(user):
        return f"`tab{ADJUSTMENT}`.`cashier` = {_sql_user(user)}"
    return "1=0"


def has_adjustment_permission(doc, user=None, permission_type=None):
    user = user or frappe.session.user
    permission_type = permission_type or "read"
    if _is_admin(user):
        return True
    if not _is_cashier(user):
        return False
    if permission_type in {"delete", "cancel", "amend", "share", "email", "export"}:
        return False
    if permission_type == "create" and getattr(doc, "is_new", lambda: False)():
        return True
    return getattr(doc, "cashier", None) == user


def get_user_permission_query_conditions(user=None):
    user = user or frappe.session.user
    if not _is_restricted_operational_user(user):
        return None
    return f"`tabUser`.`name` = {_sql_user(user)}"


def has_user_permission(doc, user=None, permission_type=None):
    user = user or frappe.session.user
    if not _is_restricted_operational_user(user):
        return None
    permission_type = permission_type or "read"
    if permission_type in {"create", "delete", "cancel", "submit", "amend", "share", "export", "email"}:
        return False
    return getattr(doc, "name", None) == user


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def _field_exists(doctype, fieldname):
    return bool(frappe.db.exists("DocType", doctype) and frappe.get_meta(doctype).has_field(fieldname))


def _append_missing_fields(doctype, fields):
    if not frappe.db.exists("DocType", doctype):
        frappe.throw(_("Required DocType is missing: {0}").format(doctype))
    meta = frappe.get_meta(doctype)
    missing = [field for field in fields if field.get("fieldname") and not meta.has_field(field["fieldname"])]
    if not missing:
        return []

    custom_field_meta = frappe.get_meta("Custom Field")
    installed = []
    for field in missing:
        values = {"doctype": "Custom Field", "dt": doctype}
        for key, value in field.items():
            if key == "fieldname" or custom_field_meta.has_field(key):
                values[key] = value
        doc = frappe.get_doc(values)
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        installed.append(field["fieldname"])
    frappe.clear_cache(doctype=doctype)
    return installed


def _sanitize_permission_rows(permissions, *, is_submittable):
    """Return permission rows valid for the target DocType submission model."""
    sanitized = []
    for permission in permissions:
        row = dict(permission)
        if not is_submittable:
            row["submit"] = 0
            row["cancel"] = 0
            row["amend"] = 0
        sanitized.append(row)
    return sanitized


def _ensure_custom_doctype(name, autoname, fields, permissions, *, is_submittable=False):
    if frappe.db.exists("DocType", name):
        doc = frappe.get_doc("DocType", name)
        existing = {row.fieldname: row for row in (doc.get("fields") or []) if row.fieldname}
        desired_names = []
        changed = False
        for idx, field in enumerate(fields, start=1):
            fieldname = field.get("fieldname")
            if not fieldname:
                continue
            desired_names.append(fieldname)
            row = existing.get(fieldname)
            if not row:
                row = doc.append("fields", field)
                existing[fieldname] = row
                changed = True
            else:
                for key, value in field.items():
                    if row.get(key) != value:
                        row.set(key, value)
                        changed = True
            if cint(row.idx) != idx:
                row.idx = idx
                changed = True

        next_idx = len(desired_names) + 1
        for row in doc.get("fields") or []:
            if row.fieldname not in desired_names:
                if cint(row.idx) != next_idx:
                    row.idx = next_idx
                    changed = True
                next_idx += 1
        doc.fields = sorted(doc.fields, key=lambda row: cint(row.idx))
        if changed:
            doc.flags.ignore_permissions = True
            doc.save(ignore_permissions=True)
        frappe.clear_cache(doctype=name)
        return

    doc = frappe.new_doc("DocType")
    doc.name = name
    doc.module = MODULE
    doc.custom = 1
    doc.track_changes = 1
    doc.allow_rename = 0
    doc.allow_import = 0
    doc.allow_bulk_edit = 0
    doc.autoname = autoname
    doc.is_submittable = 1 if is_submittable else 0
    doc.sort_field = "creation"
    doc.sort_order = "DESC"
    for field in fields:
        doc.append("fields", field)
    for permission in _sanitize_permission_rows(permissions, is_submittable=is_submittable):
        doc.append("permissions", permission)
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    frappe.clear_cache(doctype=name)


def _perm(role, *, read=1, write=0, create=0, delete=0, submit=0, cancel=0, amend=0, report=0, export=0, print_=0, email=0, share=0):
    return {
        "role": role,
        "read": read,
        "write": write,
        "create": create,
        "delete": delete,
        "submit": submit,
        "cancel": cancel,
        "amend": amend,
        "report": report,
        "export": export,
        "print": print_,
        "email": email,
        "share": share,
    }


def _admin_permissions():
    return [
        _perm(role, read=1, write=1, create=1, delete=1, submit=1, cancel=0, amend=0, report=1, export=1, print_=1, email=1, share=1)
        for role in ("System Manager", "NKT OWNER", "NKT ADMINISTRATOR")
    ]


def _replace_permissions(doctype, rows):
    """Replace site-level permissions without saving a standard DocType.

    Frappe v16 rejects saving a standard DocType when a site-level default
    print format is present. Role Permission Manager / Customize Form stores
    site-specific permission overrides as Custom DocPerm rows, which take
    priority over the standard DocPerm rows. Insert those child rows directly,
    matching Frappe's customization sync path, so the existing DocType and
    print-format configuration remain untouched.
    """
    if not frappe.db.exists("DocType", doctype):
        frappe.throw(_("Required DocType is missing: {0}").format(doctype))
    if not frappe.db.exists("DocType", "Custom DocPerm"):
        frappe.throw(_("Frappe Custom DocPerm is unavailable; cannot install controlled permissions."))

    is_submittable = bool(cint(frappe.db.get_value("DocType", doctype, "is_submittable")))
    safe_rows = _sanitize_permission_rows(rows, is_submittable=is_submittable)
    frappe.db.delete("Custom DocPerm", {"parent": doctype})

    allowed_fields = {
        "role", "permlevel", "if_owner", "read", "write", "create", "delete",
        "submit", "cancel", "amend", "report", "export", "import",
        "set_user_permissions", "share", "print", "email", "select",
    }
    for idx, row in enumerate(safe_rows, start=1):
        values = {
            "doctype": "Custom DocPerm",
            "parent": doctype,
            "parenttype": "DocType",
            "parentfield": "permissions",
            "idx": idx,
            "permlevel": cint(row.get("permlevel") or 0),
            "if_owner": cint(row.get("if_owner") or 0),
        }
        for key in allowed_fields:
            if key in row:
                values[key] = row[key]
        custom_perm = frappe.get_doc(values)
        custom_perm.flags.ignore_permissions = True
        custom_perm.db_insert()

    frappe.clear_cache(doctype=doctype)


def _ensure_shift_fields():
    return _append_missing_fields(
        SHIFT,
        [
            {"fieldname": "custom_nkt_v19_breakdown_section", "label": "Cashier Shift Breakdown", "fieldtype": "Section Break", "insert_after": "expected_cash"},
            {"fieldname": "custom_nkt_cash_sales", "label": "Cash Sales", "fieldtype": "Currency", "read_only": 1, "insert_after": "custom_nkt_v19_breakdown_section"},
            {"fieldname": "custom_nkt_cash_account_collections", "label": "Cash Account Collections", "fieldtype": "Currency", "read_only": 1, "insert_after": "custom_nkt_cash_sales"},
            {"fieldname": "custom_nkt_cash_other_in", "label": "Other Cash In", "fieldtype": "Currency", "read_only": 1, "insert_after": "custom_nkt_cash_account_collections"},
            {"fieldname": "custom_nkt_cash_refunds", "label": "Cash Refunds", "fieldtype": "Currency", "read_only": 1, "insert_after": "custom_nkt_cash_other_in"},
            {"fieldname": "custom_nkt_petty_cash_releases", "label": "Petty Cash Releases", "fieldtype": "Currency", "read_only": 1, "insert_after": "custom_nkt_cash_refunds"},
            {"fieldname": "custom_nkt_petty_cash_returns", "label": "Petty Cash Returns", "fieldtype": "Currency", "read_only": 1, "insert_after": "custom_nkt_petty_cash_releases"},
            {"fieldname": "custom_nkt_cash_drops", "label": "Cash Drops", "fieldtype": "Currency", "read_only": 1, "insert_after": "custom_nkt_petty_cash_returns"},
            {"fieldname": "custom_nkt_advance_deposits", "label": "Advance / Mid-Shift Deposits", "fieldtype": "Currency", "read_only": 1, "insert_after": "custom_nkt_cash_drops"},
            {"fieldname": "custom_nkt_cash_other_out", "label": "Other Cash Out", "fieldtype": "Currency", "read_only": 1, "insert_after": "custom_nkt_advance_deposits"},
            {"fieldname": "custom_nkt_non_cash_column", "label": "Non-Cash Breakdown", "fieldtype": "Column Break", "insert_after": "custom_nkt_cash_other_out"},
            {"fieldname": "custom_nkt_check_in", "label": "Checks", "fieldtype": "Currency", "read_only": 1, "insert_after": "custom_nkt_non_cash_column"},
            {"fieldname": "custom_nkt_gcash_in", "label": "GCash", "fieldtype": "Currency", "read_only": 1, "insert_after": "custom_nkt_check_in"},
            {"fieldname": "custom_nkt_maya_in", "label": "Maya", "fieldtype": "Currency", "read_only": 1, "insert_after": "custom_nkt_gcash_in"},
            {"fieldname": "custom_nkt_bank_transfer_in", "label": "Bank Transfer", "fieldtype": "Currency", "read_only": 1, "insert_after": "custom_nkt_maya_in"},
            {"fieldname": "custom_nkt_credit_card_in", "label": "Card", "fieldtype": "Currency", "read_only": 1, "insert_after": "custom_nkt_bank_transfer_in"},
            {"fieldname": "custom_nkt_card_surcharge_in", "label": "Card Surcharge (Included)", "fieldtype": "Currency", "read_only": 1, "insert_after": "custom_nkt_credit_card_in"},
            {"fieldname": "custom_nkt_online_in", "label": "Online / Other Online", "fieldtype": "Currency", "read_only": 1, "insert_after": "custom_nkt_credit_card_in"},
            {"fieldname": "custom_nkt_non_cash_out", "label": "Non-Cash Out / Refunds", "fieldtype": "Currency", "read_only": 1, "insert_after": "custom_nkt_online_in"},
            {"fieldname": "custom_nkt_v193_count_draft_section", "label": "Closing Count Draft", "fieldtype": "Section Break", "insert_after": "count_notes"},
            {"fieldname": "custom_nkt_count_draft_total", "label": "Saved Draft Denomination Total", "fieldtype": "Currency", "read_only": 1, "no_copy": 1, "insert_after": "custom_nkt_v193_count_draft_section"},
            {"fieldname": "custom_nkt_count_draft_saved_by", "label": "Draft Saved By", "fieldtype": "Link", "options": "User", "read_only": 1, "no_copy": 1, "insert_after": "custom_nkt_count_draft_total"},
            {"fieldname": "custom_nkt_count_draft_saved_on", "label": "Draft Saved On", "fieldtype": "Datetime", "read_only": 1, "no_copy": 1, "insert_after": "custom_nkt_count_draft_saved_by"},
            {"fieldname": "custom_nkt_v19_audit_section", "label": "V1.9 Shift Lock Audit", "fieldtype": "Section Break", "insert_after": "closed_on"},
            {"fieldname": "custom_nkt_cashier_closed_by", "label": "Cashier Closed By", "fieldtype": "Link", "options": "User", "read_only": 1, "no_copy": 1, "insert_after": "custom_nkt_v19_audit_section"},
            {"fieldname": "custom_nkt_cashier_closed_on", "label": "Cashier Closed On", "fieldtype": "Datetime", "read_only": 1, "no_copy": 1, "insert_after": "custom_nkt_cashier_closed_by"},
            {"fieldname": "custom_nkt_expected_cash_at_count", "label": "Expected Cash at Cashier Close", "fieldtype": "Currency", "read_only": 1, "no_copy": 1, "insert_after": "custom_nkt_cashier_closed_on"},
            {"fieldname": "custom_nkt_movement_count", "label": "Posted Movement Count", "fieldtype": "Int", "read_only": 1, "no_copy": 1, "insert_after": "custom_nkt_expected_cash_at_count"},
            {"fieldname": "custom_nkt_movement_count_at_count", "label": "Movement Count at Cashier Close", "fieldtype": "Int", "read_only": 1, "no_copy": 1, "insert_after": "custom_nkt_movement_count"},
            {"fieldname": "custom_nkt_breakdown_snapshot_json", "label": "Shift Breakdown Snapshot", "fieldtype": "Long Text", "read_only": 1, "hidden": 1, "no_copy": 1, "insert_after": "custom_nkt_movement_count_at_count"},
            {"fieldname": "custom_nkt_reopened_by", "label": "Last Reopened By", "fieldtype": "Link", "options": "User", "read_only": 1, "no_copy": 1, "insert_after": "custom_nkt_breakdown_snapshot_json"},
            {"fieldname": "custom_nkt_reopened_on", "label": "Last Reopened On", "fieldtype": "Datetime", "read_only": 1, "no_copy": 1, "insert_after": "custom_nkt_reopened_by"},
            {"fieldname": "custom_nkt_reopen_reason", "label": "Last Reopen Reason", "fieldtype": "Small Text", "read_only": 1, "no_copy": 1, "insert_after": "custom_nkt_reopened_on"},
            {"fieldname": "custom_nkt_v19_invalid_role_shift", "label": "Invalid Role Shift", "fieldtype": "Check", "read_only": 1, "default": "0", "no_copy": 1, "insert_after": "custom_nkt_reopen_reason"},
            {"fieldname": "custom_nkt_v19_control_note", "label": "V1.9 Control Note", "fieldtype": "Small Text", "read_only": 1, "no_copy": 1, "insert_after": "custom_nkt_v19_invalid_role_shift"},
            {"fieldname": "custom_nkt_price_adjustment_count", "label": "Adjusted Selling-Rate Rows", "fieldtype": "Int", "read_only": 1, "no_copy": 1, "insert_after": "custom_nkt_v19_control_note"},
            {"fieldname": "custom_nkt_price_adjustment_total_effect", "label": "Price Adjustment Net Effect", "fieldtype": "Currency", "read_only": 1, "no_copy": 1, "insert_after": "custom_nkt_price_adjustment_count"},
            {"fieldname": "custom_nkt_price_adjustment_audit_json", "label": "Manager Price Authorization Audit", "fieldtype": "Long Text", "read_only": 1, "hidden": 1, "no_copy": 1, "insert_after": "custom_nkt_price_adjustment_total_effect"},
            {"fieldname": "custom_nkt_v19_version", "label": "Shift Control Version", "fieldtype": "Data", "read_only": 1, "default": VERSION, "no_copy": 1, "insert_after": "custom_nkt_price_adjustment_audit_json"},
        ],
    )


def _ensure_property_setter(doctype, fieldname, property_name, value, property_type="Data"):
    name = f"{doctype}-{fieldname}-{property_name}"
    values = {
        "doctype": "Property Setter",
        "name": name,
        "doc_type": doctype,
        "field_name": fieldname,
        "property": property_name,
        "value": str(value),
        "property_type": property_type,
        "doctype_or_field": "DocField",
    }
    if frappe.db.exists("Property Setter", name):
        doc = frappe.get_doc("Property Setter", name)
        for key, val in values.items():
            if key != "doctype":
                doc.set(key, val)
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc(values)
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)


def _adjustment_fields():
    # Fast cashier layout: for an Advance Deposit, the operator sees the shift,
    # adjustment type, denomination grid, calculated total, and optional remarks.
    # System-derived audit fields remain stored but are hidden from the operator.
    return [
        {"fieldname": "details_section", "label": "Cash Drawer Adjustment", "fieldtype": "Section Break"},
        {"fieldname": "cashier_shift", "label": "Cashier Shift", "fieldtype": "Link", "options": SHIFT, "reqd": 1, "in_list_view": 1},
        {"fieldname": "adjustment_type", "label": "Adjustment Type", "fieldtype": "Select", "options": "Petty Cash Release\nPetty Cash Return\nCash Drop\nAdvance / Mid-Shift Deposit\nOther Cash In\nOther Cash Out", "reqd": 1, "in_list_view": 1},
        {"fieldname": "deposit_denomination_section", "label": "Advance Deposit Denominations", "fieldtype": "Section Break", "depends_on": "eval:doc.adjustment_type==\"Advance / Mid-Shift Deposit\""},
        {"fieldname": "deposit_bill_1000_qty", "label": "₱1,000 Bills", "fieldtype": "Int", "default": "0", "depends_on": "eval:doc.adjustment_type==\"Advance / Mid-Shift Deposit\""},
        {"fieldname": "deposit_bill_500_qty", "label": "₱500 Bills", "fieldtype": "Int", "default": "0", "depends_on": "eval:doc.adjustment_type==\"Advance / Mid-Shift Deposit\""},
        {"fieldname": "deposit_bill_200_qty", "label": "₱200 Bills", "fieldtype": "Int", "default": "0", "depends_on": "eval:doc.adjustment_type==\"Advance / Mid-Shift Deposit\""},
        {"fieldname": "deposit_bill_100_qty", "label": "₱100 Bills", "fieldtype": "Int", "default": "0", "depends_on": "eval:doc.adjustment_type==\"Advance / Mid-Shift Deposit\""},
        {"fieldname": "deposit_bill_50_qty", "label": "₱50 Bills", "fieldtype": "Int", "default": "0", "depends_on": "eval:doc.adjustment_type==\"Advance / Mid-Shift Deposit\""},
        {"fieldname": "deposit_bill_20_qty", "label": "₱20 Bills", "fieldtype": "Int", "default": "0", "depends_on": "eval:doc.adjustment_type==\"Advance / Mid-Shift Deposit\""},
        {"fieldname": "deposit_denomination_column", "label": "Coins", "fieldtype": "Column Break", "depends_on": "eval:doc.adjustment_type==\"Advance / Mid-Shift Deposit\""},
        {"fieldname": "deposit_coin_20_qty", "label": "₱20 Coins", "fieldtype": "Int", "default": "0", "depends_on": "eval:doc.adjustment_type==\"Advance / Mid-Shift Deposit\""},
        {"fieldname": "deposit_coin_10_qty", "label": "₱10 Coins", "fieldtype": "Int", "default": "0", "depends_on": "eval:doc.adjustment_type==\"Advance / Mid-Shift Deposit\""},
        {"fieldname": "deposit_coin_5_qty", "label": "₱5 Coins", "fieldtype": "Int", "default": "0", "depends_on": "eval:doc.adjustment_type==\"Advance / Mid-Shift Deposit\""},
        {"fieldname": "deposit_coin_1_qty", "label": "₱1 Coins", "fieldtype": "Int", "default": "0", "depends_on": "eval:doc.adjustment_type==\"Advance / Mid-Shift Deposit\""},
        {"fieldname": "deposit_coin_025_qty", "label": "₱0.25 Coins", "fieldtype": "Int", "default": "0", "depends_on": "eval:doc.adjustment_type==\"Advance / Mid-Shift Deposit\""},
        {"fieldname": "deposit_denomination_total", "label": "Advance Deposit Total", "fieldtype": "Currency", "read_only": 1, "depends_on": "eval:doc.adjustment_type==\"Advance / Mid-Shift Deposit\""},
        {"fieldname": "amount", "label": "Amount", "fieldtype": "Currency", "reqd": 1, "in_list_view": 1, "depends_on": "eval:doc.adjustment_type!=\"Advance / Mid-Shift Deposit\""},
        {"fieldname": "party_name", "label": "Paid To / Received From", "fieldtype": "Data", "depends_on": "eval:doc.adjustment_type!=\"Advance / Mid-Shift Deposit\""},
        {"fieldname": "purpose", "label": "Remarks / Explanation", "fieldtype": "Small Text"},
        {"fieldname": "supporting_document", "label": "Optional Supporting Attachment", "fieldtype": "Attach", "depends_on": "eval:doc.adjustment_type!=\"Advance / Mid-Shift Deposit\""},
        {"fieldname": "deposit_section", "label": "Legacy Advance Deposit Details", "fieldtype": "Section Break", "hidden": 1},
        {"fieldname": "deposit_destination", "label": "Legacy Deposit Destination", "fieldtype": "Data", "hidden": 1},
        {"fieldname": "deposit_reference_number", "label": "Legacy Deposit Reference", "fieldtype": "Data", "hidden": 1},
        {"fieldname": "company", "label": "Company", "fieldtype": "Link", "options": "Company", "reqd": 1, "in_list_view": 1, "hidden": 1},
        {"fieldname": "settlement_location", "label": "Settlement Location", "fieldtype": "Link", "options": "Warehouse", "reqd": 1, "read_only": 1, "hidden": 1},
        {"fieldname": "cashier", "label": "Cashier", "fieldtype": "Link", "options": "User", "reqd": 1, "read_only": 1, "in_list_view": 1, "hidden": 1},
        {"fieldname": "posting_datetime", "label": "Posting Date and Time", "fieldtype": "Datetime", "reqd": 1, "read_only": 1, "default": "Now", "hidden": 1},
        {"fieldname": "direction", "label": "Direction", "fieldtype": "Select", "options": "In\nOut", "reqd": 1, "read_only": 1, "hidden": 1},
        {"fieldname": "posting_section", "label": "Posted Movement", "fieldtype": "Section Break"},
        {"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "Draft\nPosted\nReversed", "default": "Draft", "read_only": 1, "in_list_view": 1},
        {"fieldname": "cashier_movement", "label": "Cashier Movement", "fieldtype": "Link", "options": MOVEMENT, "read_only": 1},
        {"fieldname": "posted_by", "label": "Posted By", "fieldtype": "Link", "options": "User", "read_only": 1},
        {"fieldname": "posted_on", "label": "Posted On", "fieldtype": "Datetime", "read_only": 1},
        {"fieldname": "reversal_section", "label": "Controlled Reversal", "fieldtype": "Section Break"},
        {"fieldname": "reversal_movement", "label": "Reversal Movement", "fieldtype": "Link", "options": MOVEMENT, "read_only": 1},
        {"fieldname": "reversal_reason", "label": "Reversal Reason", "fieldtype": "Small Text", "read_only": 1},
        {"fieldname": "reversed_by", "label": "Reversed By", "fieldtype": "Link", "options": "User", "read_only": 1},
        {"fieldname": "reversed_on", "label": "Reversed On", "fieldtype": "Datetime", "read_only": 1},
    ]

def _control_log_fields():
    return [
        {"fieldname": "log_section", "label": "Shift Control Audit", "fieldtype": "Section Break"},
        {"fieldname": "action", "label": "Action", "fieldtype": "Data", "reqd": 1, "in_list_view": 1},
        {"fieldname": "cashier_shift", "label": "Cashier Shift", "fieldtype": "Link", "options": SHIFT, "in_list_view": 1},
        {"fieldname": "adjustment", "label": "Cash Drawer Adjustment", "fieldtype": "Link", "options": ADJUSTMENT},
        {"fieldname": "performed_by", "label": "Performed By", "fieldtype": "Link", "options": "User", "reqd": 1},
        {"fieldname": "performed_on", "label": "Performed On", "fieldtype": "Datetime", "reqd": 1},
        {"fieldname": "before_status", "label": "Before Status", "fieldtype": "Data"},
        {"fieldname": "after_status", "label": "After Status", "fieldtype": "Data"},
        {"fieldname": "reason", "label": "Reason", "fieldtype": "Small Text"},
        {"fieldname": "details_json", "label": "Details", "fieldtype": "Long Text", "read_only": 1},
    ]


# ---------------------------------------------------------------------------
# Shift calculations and controls
# ---------------------------------------------------------------------------


def _movement_rows(shift_name):
    return frappe.get_all(
        MOVEMENT,
        filters={"cashier_shift": shift_name, "docstatus": 1, "status": "Posted"},
        fields=["name", "movement_type", "direction", "payment_method", "amount", "card_surcharge", "affects_cash_drawer"],
        order_by="posting_datetime asc, creation asc",
        limit_page_length=0,
    )



def _price_adjustment_rows(shift_name):
    """Derived Manager-PIN selling-price audit rows for one Cashier Shift."""
    rows = frappe.db.sql(
        """
        SELECT
            s.name AS sale_name, s.sale_datetime, s.customer, s.customer_name, s.cashier,
            s.matched_customer_order, s.reconciliation_status,
            s.custom_nkt_price_authorized_by AS authorized_by,
            s.custom_nkt_price_authorized_on AS authorized_on,
            s.custom_nkt_price_authorization_reason AS authorization_reason,
            s.custom_nkt_price_authorization_explanation AS authorization_explanation,
            s.custom_nkt_price_authorization_source AS authorization_source,
            s.custom_nkt_price_authorization_device_id AS authorization_device_id,
            i.idx AS line_no, i.item, i.item_name, i.quantity, i.uom, i.source_warehouse,
            i.standard_rate, i.final_rate, o.encoder, o.cashier_reconciled_on
        FROM `tabNKT Cashier Sale` s
        INNER JOIN `tabNKT Cashier Sale Item` i ON i.parent = s.name
        LEFT JOIN `tabNKT Customer Order` o ON o.name = s.matched_customer_order
        WHERE s.docstatus = 1
          AND s.cashier_shift = %s
          AND ABS(COALESCE(i.final_rate, 0) - COALESCE(i.standard_rate, 0)) > 0.000001
        ORDER BY s.sale_datetime, s.name, i.idx
        """,
        (shift_name,),
        as_dict=True,
    )
    out = []
    for row in rows:
        standard_rate = flt(row.standard_rate)
        actual_rate = flt(row.final_rate)
        qty = flt(row.quantity)
        difference = actual_rate - standard_rate
        out.append({
            "sale": row.sale_name, "sale_datetime": str(row.sale_datetime or ""),
            "customer": row.customer_name or row.customer or "", "cashier": row.cashier or "",
            "line_no": cint(row.line_no), "item": row.item_name or row.item or "", "item_code": row.item or "",
            "qty": qty, "uom": row.uom or "", "warehouse": row.source_warehouse or "",
            "standard_rate": float(f"{standard_rate:.2f}"), "actual_rate": float(f"{actual_rate:.2f}"),
            "difference_per_unit": float(f"{difference:.2f}"), "total_rate_effect": float(f"{qty * difference:.2f}"),
            "authorized_by": row.authorized_by or "", "authorized_on": str(row.authorized_on or ""),
            "reason": row.authorization_reason or "", "explanation": row.authorization_explanation or "",
            "authorization_source": row.authorization_source or "", "authorization_device_id": row.authorization_device_id or "",
            "matched_customer_order": row.matched_customer_order or "", "encoder": row.encoder or "",
            "reconciliation_status": row.reconciliation_status or "", "cashier_reconciled_on": str(row.cashier_reconciled_on or ""),
        })
    return out

def calculate_shift_summary(shift_name):
    if not frappe.db.exists(SHIFT, shift_name):
        frappe.throw(_("Cashier Shift does not exist: {0}").format(shift_name))
    shift = frappe.get_doc(SHIFT, shift_name)
    rows = _movement_rows(shift_name)
    price_adjustment_rows = _price_adjustment_rows(shift_name)

    totals = defaultdict(float)
    method_in = defaultdict(float)
    method_out = defaultdict(float)
    movement_in = defaultdict(float)
    movement_out = defaultdict(float)
    cash_movement_in = defaultdict(float)
    cash_movement_out = defaultdict(float)

    for row in rows:
        amount = flt(row.amount)
        direction = row.direction or "In"
        method = normalize_payment_method(row.payment_method or "Other Online")
        movement_type = row.movement_type or "Other Cash In"
        is_cash = method == "Cash"
        bucket = "cash" if is_cash else "non_cash"
        totals[f"{bucket}_{direction.lower()}"] += amount
        if direction == "In":
            method_in[method] += amount
            if method == "Card":
                totals["card_surcharge_in"] += flt(row.get("card_surcharge"))
            movement_in[movement_type] += amount
            if is_cash:
                cash_movement_in[movement_type] += amount
        else:
            method_out[method] += amount
            movement_out[movement_type] += amount
            if is_cash:
                cash_movement_out[movement_type] += amount

    opening_cash = flt(shift.opening_cash)
    expected_cash = opening_cash + totals["cash_in"] - totals["cash_out"]

    cash_sales = cash_movement_in["Customer Order Payment"] + cash_movement_in["Exchange Difference Collected"]
    cash_account_collections = cash_movement_in["Customer Account Collection"] + cash_movement_in["Account Collection"]
    petty_returns = cash_movement_in["Petty Cash Return"]
    cash_other_in = cash_movement_in["Other Cash In"]
    cash_refunds = cash_movement_out["Customer Return Refund"] + cash_movement_out["Exchange Difference Refunded"]
    petty_releases = cash_movement_out["Petty Cash Release"]
    cash_drops = cash_movement_out["Cash Drop"]
    advance_deposits = cash_movement_out["Advance / Mid-Shift Deposit"]
    cash_other_out = cash_movement_out["Other Cash Out"]
    cash_other_in += cash_movement_in["Advance Deposit Reversal"]

    summary = {
        "shift": shift_name,
        "opening_cash": opening_cash,
        "movement_count": len(rows),
        "total_cash_in": totals["cash_in"],
        "total_cash_out": totals["cash_out"],
        "total_non_cash_in": totals["non_cash_in"],
        "total_non_cash_out": totals["non_cash_out"],
        "expected_cash": expected_cash,
        "cash_sales": cash_sales,
        "cash_account_collections": cash_account_collections,
        "cash_other_in": cash_other_in,
        "cash_refunds": cash_refunds,
        "petty_cash_releases": petty_releases,
        "petty_cash_returns": petty_returns,
        "cash_drops": cash_drops,
        "advance_deposits": advance_deposits,
        "cash_other_out": cash_other_out,
        "check_in": method_in["Check"],
        "gcash_in": method_in["GCash"],
        "maya_in": method_in["Maya"],
        "bank_transfer_in": method_in["Bank Transfer"],
        "credit_card_in": method_in["Card"],
        "card_surcharge_in": totals["card_surcharge_in"],
        "online_in": method_in["Online"] + method_in["Other Online"],
        "non_cash_out": totals["non_cash_out"],
        "methods_in": dict(method_in),
        "methods_out": dict(method_out),
        "movements_in": dict(movement_in),
        "movements_out": dict(movement_out),
        "price_adjustment_rows": price_adjustment_rows,
        "price_adjustment_count": len(price_adjustment_rows),
        "price_adjustment_total_effect": sum(flt(row.get("total_rate_effect")) for row in price_adjustment_rows),
    }
    return summary


def _summary_field_values(summary):
    return {
        "total_cash_in": summary["total_cash_in"],
        "total_cash_out": summary["total_cash_out"],
        "total_non_cash_in": summary["total_non_cash_in"],
        "total_non_cash_out": summary["total_non_cash_out"],
        "expected_cash": summary["expected_cash"],
        "custom_nkt_cash_sales": summary["cash_sales"],
        "custom_nkt_cash_account_collections": summary["cash_account_collections"],
        "custom_nkt_cash_other_in": summary["cash_other_in"],
        "custom_nkt_cash_refunds": summary["cash_refunds"],
        "custom_nkt_petty_cash_releases": summary["petty_cash_releases"],
        "custom_nkt_petty_cash_returns": summary["petty_cash_returns"],
        "custom_nkt_cash_drops": summary["cash_drops"],
        "custom_nkt_advance_deposits": summary["advance_deposits"],
        "custom_nkt_cash_other_out": summary["cash_other_out"],
        "custom_nkt_check_in": summary["check_in"],
        "custom_nkt_gcash_in": summary["gcash_in"],
        "custom_nkt_maya_in": summary["maya_in"],
        "custom_nkt_bank_transfer_in": summary["bank_transfer_in"],
        "custom_nkt_credit_card_in": summary["credit_card_in"],
        "custom_nkt_card_surcharge_in": summary["card_surcharge_in"],
        "custom_nkt_online_in": summary["online_in"],
        "custom_nkt_non_cash_out": summary["non_cash_out"],
        "custom_nkt_movement_count": summary["movement_count"],
        "custom_nkt_price_adjustment_count": summary["price_adjustment_count"],
        "custom_nkt_price_adjustment_total_effect": summary["price_adjustment_total_effect"],
        "custom_nkt_price_adjustment_audit_json": json.dumps(summary["price_adjustment_rows"], sort_keys=True, separators=(",", ":"), default=str),
        "custom_nkt_v19_version": VERSION,
    }


def _apply_summary(shift_name, summary=None):
    summary = summary or calculate_shift_summary(shift_name)
    frappe.db.set_value(SHIFT, shift_name, _summary_field_values(summary), update_modified=False)
    return summary


def _assert_shift_access(shift, *, allow_admin=True):
    if allow_admin and _is_admin():
        return
    if not _is_cashier() or shift.cashier != frappe.session.user:
        frappe.throw(_("You may access only your own cashier shift."), frappe.PermissionError)


def _assert_open_shift(shift):
    if cint(shift.docstatus) != 0 or shift.status != "Open":
        frappe.throw(_("Cashier Shift {0} is not open. New drawer movements are locked.").format(shift.name))


def validate_shift_before_insert(doc, method=None):
    _require_cashier_or_admin("open a cashier shift")
    if _is_admin():
        doc.cashier = doc.cashier or frappe.session.user
    else:
        doc.cashier = frappe.session.user

    if not _cashier_is_valid_operator(doc.cashier):
        frappe.throw(_("The selected Shift Operator is not an NKT Cashier or authorized Owner/Administrator."))

    existing = frappe.get_all(
        SHIFT,
        filters={"cashier": doc.cashier, "docstatus": 0, "status": ["in", sorted(OPEN_SHIFT_STATUSES)]},
        pluck="name",
        limit=1,
    )
    if existing:
        frappe.throw(_("Shift Operator {0} already has an unfinished shift: {1}").format(doc.cashier, existing[0]))

    doc.shift_start = doc.shift_start or now_datetime()
    doc.status = "Open"
    doc.turnover_status = "Not Turned Over"
    doc.custom_nkt_v19_version = VERSION


def validate_shift(doc, method=None):
    if doc.is_new():
        return
    before = doc.get_doc_before_save()
    if not before:
        return

    if not _is_admin() and not getattr(doc.flags, "nkt_v19_internal", False):
        _assert_shift_access(doc, allow_admin=False)
        immutable = ("company", "settlement_location", "cashier", "opening_cash", "shift_start")
        changed = [field for field in immutable if doc.get(field) != before.get(field)]
        if changed:
            frappe.throw(_("Cashier cannot change locked shift fields: {0}").format(", ".join(changed)))
        protected = (
            "status", "shift_end", "total_cash_in", "total_cash_out", "total_non_cash_in",
            "total_non_cash_out", "expected_cash", "actual_cash_count", "over_short",
            "count_locked_by", "count_locked_on", "turnover_status", "turnover_amount",
            "approved_by", "approved_on", "closed_by", "closed_on",
            "custom_nkt_count_draft_total", "custom_nkt_count_draft_saved_by", "custom_nkt_count_draft_saved_on",
            "custom_nkt_cashier_closed_by", "custom_nkt_cashier_closed_on",
            *DENOMINATIONS.keys(),
        )
        changed_protected = [field for field in protected if doc.get(field) != before.get(field)]
        if changed_protected:
            frappe.throw(_("Use the controlled V1.9 shift actions instead of editing calculated or locked fields."))


def validate_shift_before_submit(doc, method=None):
    if not getattr(doc.flags, "nkt_v19_internal", False):
        _require_admin("approve and close a cashier shift")
    if doc.status not in {"Reviewed / Closed", "Closed"}:
        frappe.throw(_("A cashier shift may be submitted only after Owner/Administrator review."))


def prevent_shift_cancel(doc, method=None):
    frappe.throw(_("Closed cashier shifts cannot be cancelled. Use a future controlled correction process so the audit trail remains intact."))


def _c15c10i_preserved_return_exchange_refund(doc, shift):
    """
    Narrow Primary-materialization bridge for a Cashier Return/Exchange refund
    that was physically accepted at Store Edge while its original Cashier Shift
    was valid, then canonically materialized after that historical shift closed.

    This is NOT a general closed-shift bypass. It is available only while the
    process-local C15C.10I preserved Return/Exchange context is active and the
    Cashier Movement is the exact `refund` effect of an immutable offline
    Return/Exchange Declaration whose event/shift/company/cashier/time truth
    matches the historical shift.
    """
    company = doc.company or shift.company
    settlement_location = doc.settlement_location or shift.settlement_location
    cashier = doc.cashier or shift.cashier

    if not _preserved_return_exchange_shift_context_matches(
        cashier_shift=shift.name,
        company=company,
        settlement_location=settlement_location,
        cashier=cashier,
    ):
        return False

    if (
        doc.source_doctype != "NKT Return Exchange Declaration"
        or not doc.source_name
        or str(doc.source_row or "") != "refund"
    ):
        frappe.throw(
            _("Preserved Return/Exchange closed-shift compatibility is limited to its exact refund movement.")
        )

    declaration = frappe.db.get_value(
        "NKT Return Exchange Declaration",
        doc.source_name,
        [
            "name", "docstatus", "side", "company", "business_date",
            "entry_datetime", "entry_user", "posting_status",
            "custom_nkt_offline_event_uuid",
            "custom_nkt_offline_cashier_shift",
            "custom_nkt_offline_physical_settled_at",
        ],
        as_dict=True,
    )
    if not declaration or cint(declaration.docstatus) == 2:
        frappe.throw(
            _("Preserved Return/Exchange refund requires a live canonical Declaration.")
        )
    if declaration.side != "Cashier":
        frappe.throw(
            _("Preserved Return/Exchange refund must originate from the Cashier declaration side.")
        )
    if not str(declaration.custom_nkt_offline_event_uuid or "").strip():
        frappe.throw(
            _("Preserved Return/Exchange refund is missing immutable offline Event identity.")
        )

    hard = []
    if declaration.custom_nkt_offline_cashier_shift != shift.name:
        hard.append("cashier_shift")
    if declaration.company != shift.company or company != shift.company:
        hard.append("company")
    if settlement_location != shift.settlement_location:
        hard.append("settlement_location")
    if declaration.entry_user != shift.cashier or cashier != shift.cashier:
        hard.append("cashier")

    try:
        physical = get_datetime(declaration.entry_datetime)
        settled = get_datetime(declaration.custom_nkt_offline_physical_settled_at)
        if physical != settled:
            hard.append("physical_settled_at")
        if get_datetime(doc.posting_datetime) != physical:
            hard.append("movement_posting_datetime")
        if get_datetime(physical).date() != frappe.utils.getdate(declaration.business_date):
            hard.append("business_date")
        if shift.shift_start and physical < get_datetime(shift.shift_start):
            hard.append("before_shift_start")
        if shift.shift_end and physical > get_datetime(shift.shift_end):
            hard.append("after_shift_end")
    except Exception:
        hard.append("shift_time_integrity")

    if doc.direction != "Out":
        hard.append("direction")
    if doc.movement_type not in {
        "Customer Return Refund",
        "Exchange Difference Refunded",
    }:
        hard.append("movement_type")

    if hard:
        frappe.throw(
            _(
                "Preserved Return/Exchange refund conflicts with immutable historical shift truth: {0}."
            ).format(", ".join(hard))
        )

    return True


def validate_movement_shift_open(doc, method=None):
    if not doc.cashier_shift:
        frappe.throw(_("Cashier Movement requires a Cashier Shift."))

    shift = frappe.get_doc(SHIFT, doc.cashier_shift)
    preserved_cash_drawer = _c15c10f_preserved_movement_journal(doc)
    if preserved_cash_drawer:
        return

    preserved_return_exchange = _c15c10i_preserved_return_exchange_refund(doc, shift)
    observed_tender = bool(getattr(doc.flags, "nkt_c15c10c_observed_tender", False))

    if preserved_return_exchange:
        # Exact immutable offline Return/Exchange refund already validated above.
        pass
    elif observed_tender:
        # C15C.10C narrow compatibility bridge:
        # money physically accepted on Store Edge remains authoritative even if
        # its original shift closed before Primary completed movement materialization.
        # This bypass is server-flag-only and requires an immutable Tender-derived
        # submitted Payment Receipt row. Ordinary online movements still require Open.
        if doc.source_doctype != "NKT Payment Receipt" or not doc.source_name or not doc.source_row:
            frappe.throw(_("Observed-tender movement must identify its Payment Receipt row."))

        receipt = frappe.db.get_value(
            "NKT Payment Receipt",
            doc.source_name,
            [
                "docstatus", "source_primary_tender_intent", "cashier_shift",
                "settlement_location", "received_by", "receipt_datetime",
            ],
            as_dict=True,
        )
        if not receipt or cint(receipt.docstatus) != 1 or not receipt.source_primary_tender_intent:
            frappe.throw(_("Observed-tender movement requires a submitted Tender-derived Payment Receipt."))
        if not frappe.db.exists(
            "NKT Payment Detail",
            {"name": doc.source_row, "parent": doc.source_name, "parenttype": "NKT Payment Receipt"},
        ):
            frappe.throw(_("Observed-tender movement source row is not part of its Payment Receipt."))

        journal = frappe.db.get_value(
            "NKT Primary Cashier Tender Intent",
            receipt.source_primary_tender_intent,
            [
                "name", "origin_user", "business_date", "settled_at", "company",
                "cashier_shift", "settlement_location", "preservation_state",
            ],
            as_dict=True,
        )
        if not journal or journal.preservation_state != "Preserved":
            frappe.throw(_("Observed-tender movement source Tender is not preserved."))

        hard = []
        if journal.cashier_shift != shift.name or receipt.cashier_shift != shift.name:
            hard.append("cashier_shift")
        if journal.company != shift.company or doc.company != shift.company:
            hard.append("company")
        if journal.settlement_location != shift.settlement_location or receipt.settlement_location != shift.settlement_location:
            hard.append("settlement_location")
        if journal.origin_user != shift.cashier or receipt.received_by != shift.cashier:
            hard.append("cashier")
        try:
            if frappe.utils.get_datetime(receipt.receipt_datetime) != frappe.utils.get_datetime(journal.settled_at):
                hard.append("settled_at")
            if shift.shift_start and frappe.utils.getdate(shift.shift_start) != frappe.utils.getdate(journal.business_date):
                hard.append("business_date")
            if shift.shift_end and frappe.utils.get_datetime(journal.settled_at) > frappe.utils.get_datetime(shift.shift_end):
                hard.append("settled_after_shift_end")
        except Exception:
            hard.append("shift_time_integrity")
        if hard:
            frappe.throw(
                _("Observed-tender movement conflicts with immutable shift/source truth: {0}.").format(", ".join(hard))
            )
    else:
        _assert_open_shift(shift)

    if doc.company and doc.company != shift.company:
        frappe.throw(_("Cashier Movement company does not match the Cashier Shift."))
    if doc.settlement_location and doc.settlement_location != shift.settlement_location:
        frappe.throw(_("Cashier Movement settlement location does not match the Cashier Shift."))
    if doc.cashier and doc.cashier != shift.cashier:
        frappe.throw(_("Cashier Movement cashier does not match the Cashier Shift operator."))

    doc.company = shift.company
    doc.settlement_location = shift.settlement_location
    doc.cashier = shift.cashier


def _denomination_total(values):
    total = 0.0
    clean = {}
    for fieldname, denomination in DENOMINATIONS.items():
        qty = cint((values or {}).get(fieldname) or 0)
        if qty < 0:
            frappe.throw(_("Denomination quantities cannot be negative."))
        clean[fieldname] = qty
        total += qty * denomination
    return total, clean


def _pending_adjustments(shift_name):
    if not frappe.db.exists("DocType", ADJUSTMENT):
        return []
    return frappe.get_all(ADJUSTMENT, filters={"cashier_shift": shift_name, "docstatus": 0}, pluck="name", limit_page_length=0)


def _log(action, *, shift=None, adjustment=None, before_status=None, after_status=None, reason=None, details=None):
    if not frappe.db.exists("DocType", CONTROL_LOG):
        return None
    doc = frappe.new_doc(CONTROL_LOG)
    doc.action = action
    doc.cashier_shift = shift
    doc.adjustment = adjustment
    doc.performed_by = frappe.session.user
    doc.performed_on = now_datetime()
    doc.before_status = before_status
    doc.after_status = after_status
    doc.reason = reason
    doc.details_json = json.dumps(details or {}, default=str, sort_keys=True)
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return doc.name


@frappe.whitelist()
def refresh_shift_totals(shift_name):
    shift = frappe.get_doc(SHIFT, shift_name)
    _assert_shift_access(shift)
    summary = _apply_summary(shift.name)
    frappe.db.commit()
    return summary


@frappe.whitelist()
def get_cash_count_draft(shift_name):
    shift = frappe.get_doc(SHIFT, shift_name)
    _assert_shift_access(shift)
    if cint(shift.docstatus) != 0:
        frappe.throw(_("Submitted shifts cannot be edited."))
    summary = calculate_shift_summary(shift.name)
    denominations = {fieldname: cint(shift.get(fieldname) or 0) for fieldname in DENOMINATIONS}
    actual_cash, _ = _denomination_total(denominations)
    return {
        "shift": shift.name,
        "status": shift.status,
        "denominations": denominations,
        "notes": shift.count_notes or "",
        "expected_cash": summary["expected_cash"],
        "actual_cash": actual_cash,
        "over_short": actual_cash - flt(summary["expected_cash"]),
        "draft_saved_by": shift.custom_nkt_count_draft_saved_by,
        "draft_saved_on": shift.custom_nkt_count_draft_saved_on,
    }


def _require_assigned_cashier(shift, action):
    if shift.cashier != frappe.session.user:
        frappe.throw(_("Only the assigned Cashier may {0}. The Owner/Administrator reviews after the Cashier closes the shift.").format(action), frappe.PermissionError)
    if not (_is_cashier() or _is_admin()):
        frappe.throw(_("You are not authorized to {0}.").format(action), frappe.PermissionError)


def _save_denomination_values(shift, clean, actual_cash, over_short, notes):
    shift.flags.nkt_v19_internal = True
    for fieldname, qty in clean.items():
        shift.set(fieldname, qty)
    shift.actual_cash_count = actual_cash
    shift.over_short = over_short
    shift.count_notes = notes
    shift.custom_nkt_count_draft_total = actual_cash
    shift.custom_nkt_count_draft_saved_by = frappe.session.user
    shift.custom_nkt_count_draft_saved_on = now_datetime()
    shift.custom_nkt_v19_version = VERSION


@frappe.whitelist()
def save_cash_count_draft(shift_name, denominations=None, notes=None):
    shift = frappe.get_doc(SHIFT, shift_name)
    _require_assigned_cashier(shift, "save the closing count draft")
    _assert_open_shift(shift)
    if isinstance(denominations, str):
        denominations = json.loads(denominations)
    actual_cash, clean = _denomination_total(denominations or {})
    summary = calculate_shift_summary(shift.name)
    over_short = actual_cash - flt(summary["expected_cash"])
    notes = (notes or "").strip()
    _save_denomination_values(shift, clean, actual_cash, over_short, notes)
    shift.blind_count_confirmed = 0
    shift.save(ignore_permissions=True)
    _log(
        "Closing Count Draft Saved",
        shift=shift.name,
        before_status=shift.status,
        after_status=shift.status,
        reason=notes,
        details={"actual_cash": actual_cash, "expected_cash": summary["expected_cash"], "over_short": over_short, "denominations": clean},
    )
    frappe.db.commit()
    return {
        "shift": shift.name,
        "status": shift.status,
        "expected_cash": summary["expected_cash"],
        "actual_cash": actual_cash,
        "over_short": over_short,
        "draft_saved_on": shift.custom_nkt_count_draft_saved_on,
    }


@frappe.whitelist()
def finalize_and_close_shift(shift_name, denominations=None, notes=None):
    shift = frappe.get_doc(SHIFT, shift_name)
    _require_assigned_cashier(shift, "finalize and close the cashier shift")
    _assert_open_shift(shift)

    pending = _pending_adjustments(shift.name)
    if pending:
        frappe.throw(_("Submit or remove draft Cash Drawer Adjustments before closing the shift: {0}").format(", ".join(pending)))

    if isinstance(denominations, str):
        denominations = json.loads(denominations)
    actual_cash, clean = _denomination_total(denominations or {})
    summary = calculate_shift_summary(shift.name)
    over_short = actual_cash - flt(summary["expected_cash"])
    notes = (notes or "").strip()
    if abs(over_short) > TOLERANCE and not notes:
        frappe.throw(_("A cash difference explanation is required when the drawer is over or short."))

    before_status = shift.status
    _save_denomination_values(shift, clean, actual_cash, over_short, notes)
    for fieldname, value in _summary_field_values(summary).items():
        shift.set(fieldname, value)
    shift.blind_count_confirmed = 1
    shift.count_locked_by = frappe.session.user
    shift.count_locked_on = now_datetime()
    shift.custom_nkt_cashier_closed_by = frappe.session.user
    shift.custom_nkt_cashier_closed_on = now_datetime()
    shift.custom_nkt_expected_cash_at_count = summary["expected_cash"]
    shift.custom_nkt_movement_count_at_count = summary["movement_count"]
    shift.custom_nkt_breakdown_snapshot_json = json.dumps(summary, default=str, sort_keys=True)
    shift.turnover_status = CASHIER_CLOSED_STATUS
    shift.turnover_amount = actual_cash
    shift.shift_end = now_datetime()
    shift.status = CASHIER_CLOSED_STATUS
    shift.save(ignore_permissions=True)

    _log(
        "Cashier Finalized and Closed Shift",
        shift=shift.name,
        before_status=before_status,
        after_status=shift.status,
        reason=notes,
        details={"actual_cash": actual_cash, "expected_cash": summary["expected_cash"], "over_short": over_short, "denominations": clean},
    )
    frappe.db.commit()
    return {
        "shift": shift.name,
        "status": shift.status,
        "expected_cash": summary["expected_cash"],
        "actual_cash": actual_cash,
        "over_short": over_short,
        "movement_count": summary["movement_count"],
    }


@frappe.whitelist()
def record_cash_count(shift_name, denominations=None, notes=None):
    """Backward-compatible alias for the V1.9.2 action."""
    return finalize_and_close_shift(shift_name, denominations=denominations, notes=notes)


@frappe.whitelist()
def reopen_cashier_closed_shift(shift_name, reason):
    _require_admin("reopen a cashier-closed shift")
    reason = (reason or "").strip()
    if not reason:
        frappe.throw(_("A reopen reason is required."))
    shift = frappe.get_doc(SHIFT, shift_name)
    if cint(shift.docstatus) != 0 or shift.status not in COUNTED_STATUSES:
        frappe.throw(_("Only a cashier-closed shift awaiting review may be reopened."))

    before_status = shift.status
    previous_locked_by = shift.count_locked_by
    shift.flags.nkt_v19_internal = True
    shift.status = "Open"
    shift.turnover_status = "Reopened for Correction"
    shift.shift_end = None
    shift.blind_count_confirmed = 0
    shift.count_locked_by = None
    shift.count_locked_on = None
    shift.custom_nkt_count_draft_total = flt(shift.actual_cash_count)
    shift.custom_nkt_count_draft_saved_by = previous_locked_by or shift.cashier
    shift.custom_nkt_count_draft_saved_on = now_datetime()
    shift.custom_nkt_expected_cash_at_count = 0
    shift.custom_nkt_movement_count_at_count = 0
    shift.custom_nkt_breakdown_snapshot_json = None
    shift.custom_nkt_reopened_by = frappe.session.user
    shift.custom_nkt_reopened_on = now_datetime()
    shift.custom_nkt_reopen_reason = reason
    shift.custom_nkt_cashier_closed_by = None
    shift.custom_nkt_cashier_closed_on = None
    shift.save(ignore_permissions=True)
    _log("Cashier-Closed Shift Reopened", shift=shift.name, before_status=before_status, after_status="Open", reason=reason)
    frappe.db.commit()
    return {"shift": shift.name, "status": shift.status, "reason": reason}


@frappe.whitelist()
def approve_and_close_shift(shift_name, approval_reason=None):
    _require_admin("review and finalize a cashier shift")
    shift = frappe.get_doc(SHIFT, shift_name)
    if shift.status in {"Counted - Awaiting Approval", "Turned Over - Awaiting Review"} and cint(shift.blind_count_confirmed):
        _upgrade_legacy_counted_shift(shift.name)
        shift = frappe.get_doc(SHIFT, shift.name)
    if cint(shift.docstatus) != 0 or shift.status not in COUNTED_STATUSES:
        frappe.throw(_("The Cashier must finalize and close the shift before Owner/Administrator review."))

    summary = calculate_shift_summary(shift.name)
    if abs(flt(summary["expected_cash"]) - flt(shift.custom_nkt_expected_cash_at_count)) > TOLERANCE:
        frappe.throw(_("Expected cash changed after the Cashier closed the shift. Reopen and investigate the late movement before review."))
    if cint(summary["movement_count"]) != cint(shift.custom_nkt_movement_count_at_count):
        frappe.throw(_("Cashier movement count changed after the Cashier closed the shift. Reopen and investigate before review."))

    over_short = flt(shift.actual_cash_count) - flt(summary["expected_cash"])
    approval_reason = (approval_reason or "").strip()
    if abs(over_short) > TOLERANCE and not approval_reason:
        frappe.throw(_("Owner/Administrator review notes are required for an over or short shift."))

    before_status = shift.status
    reviewed_on = now_datetime()
    final_turnover_status = "Reviewed / OK" if abs(over_short) <= TOLERANCE else "Reviewed with Difference"

    # V1.9.5 compatibility bridge:
    # The original NKT Cashier Shift controller still contains the legacy
    # "turn over denomination count before Reviewed / OK" validation.  The
    # controlled action above has already revalidated the frozen expected-cash
    # snapshot and movement count, so submit the reviewed document while
    # explicitly bypassing that obsolete controller validation. Frappe still
    # performs its structural/link checks and runs on_update/on_submit hooks.
    shift.flags.nkt_v19_internal = True
    shift.flags.ignore_validate = True
    for fieldname, value in _summary_field_values(summary).items():
        shift.set(fieldname, value)
    shift.over_short = over_short
    shift.turnover_status = final_turnover_status
    shift.turnover_confirmed_by = frappe.session.user
    shift.turnover_confirmed_on = reviewed_on
    shift.approval_reason = approval_reason
    shift.approved_by = frappe.session.user
    shift.approved_on = reviewed_on
    shift.closed_by = frappe.session.user
    shift.closed_on = reviewed_on
    shift.status = "Reviewed / Closed"
    shift.submit()

    _log(
        "Shift Reviewed and Finalized",
        shift=shift.name,
        before_status=before_status,
        after_status=shift.status,
        reason=approval_reason,
        details={"expected_cash": summary["expected_cash"], "actual_cash": shift.actual_cash_count, "over_short": over_short},
    )
    frappe.db.commit()
    return {"shift": shift.name, "status": shift.status, "docstatus": shift.docstatus, "over_short": over_short}


# ---------------------------------------------------------------------------
# Cash drawer adjustments
# ---------------------------------------------------------------------------



def _deposit_denomination_total_from_doc(doc):
    values = {fieldname: doc.get(fieldname) for fieldname in DEPOSIT_DENOMINATIONS}
    return _denomination_total_with_map(values, DEPOSIT_DENOMINATIONS)


def _denomination_total_with_map(values, mapping):
    total = 0.0
    clean = {}
    for fieldname, denomination in mapping.items():
        qty = cint((values or {}).get(fieldname) or 0)
        if qty < 0:
            frappe.throw(_("Denomination quantities cannot be negative."))
        clean[fieldname] = qty
        total += qty * denomination
    return total, clean


C15C10F_PRESERVED_FLAG = "nkt_c15c10f_preserved_cash_drawer_intent"


def _c15c10f_preserved_intent_journal(doc):
    event_uuid = str(
        getattr(doc.flags, C15C10F_PRESERVED_FLAG, "") or ""
    ).strip()
    if not event_uuid:
        return None

    if str(frappe.conf.get("nkt_runtime_role") or "") != "Primary":
        frappe.throw(
            _("Preserved offline cash-drawer materialization is Primary-only."),
            frappe.PermissionError,
        )

    journal_dt = "NKT Primary Cash Drawer Adjustment Intent"
    if not frappe.db.exists(journal_dt, event_uuid):
        frappe.throw(_("Preserved cash-drawer intent journal is unavailable."))

    journal = frappe.get_doc(journal_dt, event_uuid)
    if (
        journal.preservation_state != "Preserved"
        or journal.downstream_state
        not in ("Awaiting Cash Drawer Materialization", "Cash Drawer Materialized")
    ):
        frappe.throw(_("Preserved cash-drawer intent is not materialization-eligible."))

    if str(doc.cashier_shift or "") != str(journal.cashier_shift or ""):
        frappe.throw(_("Cash Drawer Adjustment shift conflicts with preserved intent."))
    if str(doc.adjustment_type or "") != str(journal.adjustment_type or ""):
        frappe.throw(_("Cash Drawer Adjustment type conflicts with preserved intent."))
    if abs(flt(doc.amount) - flt(journal.amount)) > TOLERANCE:
        # Advance deposits receive amount from denominations later; zero is allowed
        # before validate() derives the amount.
        if not (
            doc.adjustment_type == "Advance / Mid-Shift Deposit"
            and abs(flt(doc.amount)) <= TOLERANCE
        ):
            frappe.throw(_("Cash Drawer Adjustment amount conflicts with preserved intent."))

    return journal


def _c15c10f_verify_adjustment_against_journal(doc, journal):
    checks = {
        "cashier_shift": doc.cashier_shift,
        "company": doc.company,
        "settlement_location": doc.settlement_location,
        "cashier": doc.cashier,
        "adjustment_type": doc.adjustment_type,
        "direction": doc.direction,
    }
    for field, actual in checks.items():
        if str(actual or "") != str(journal.get(field) or ""):
            frappe.throw(
                _("Cash Drawer Adjustment {0} conflicts with preserved intent.").format(field)
            )
    if abs(flt(doc.amount) - flt(journal.amount)) > TOLERANCE:
        frappe.throw(_("Cash Drawer Adjustment amount conflicts with preserved intent."))


def _c15c10f_preserved_movement_journal(doc):
    event_uuid = str(
        getattr(doc.flags, C15C10F_PRESERVED_FLAG, "") or ""
    ).strip()
    if not event_uuid:
        return None
    if str(frappe.conf.get("nkt_runtime_role") or "") != "Primary":
        frappe.throw(
            _("Preserved offline cash movement materialization is Primary-only."),
            frappe.PermissionError,
        )
    if doc.source_doctype != ADJUSTMENT or not doc.source_name or doc.source_row:
        frappe.throw(_("Preserved offline cash movement must derive from one adjustment."))

    journal_dt = "NKT Primary Cash Drawer Adjustment Intent"
    if not frappe.db.exists(journal_dt, event_uuid):
        frappe.throw(_("Preserved cash-drawer intent journal is unavailable."))
    journal = frappe.get_doc(journal_dt, event_uuid)
    adjustment = frappe.get_doc(ADJUSTMENT, doc.source_name)
    mapping = ADJUSTMENT_MAP.get(adjustment.adjustment_type)
    if not mapping:
        frappe.throw(_("Preserved Cash Drawer Adjustment type is invalid."))

    checks = {
        "cashier_shift": doc.cashier_shift,
        "settlement_location": doc.settlement_location,
        "cashier": doc.cashier,
        "movement_type": doc.movement_type,
        "direction": doc.direction,
    }
    expected = {
        "cashier_shift": journal.cashier_shift,
        "settlement_location": journal.settlement_location,
        "cashier": journal.cashier,
        "movement_type": mapping["movement_type"],
        "direction": mapping["direction"],
    }
    for field, actual in checks.items():
        if str(actual or "") != str(expected[field] or ""):
            frappe.throw(
                _("Preserved Cashier Movement {0} conflicts with intent.").format(field)
            )
    if (
        abs(flt(doc.amount) - flt(journal.amount)) > TOLERANCE
        or str(adjustment.cashier_shift or "") != str(journal.cashier_shift or "")
        or str(adjustment.adjustment_type or "") != str(journal.adjustment_type or "")
        or abs(flt(adjustment.amount) - flt(journal.amount)) > TOLERANCE
    ):
        frappe.throw(_("Preserved Cashier Movement amount/source conflicts with intent."))
    return journal



def _c15c10f_guard_direct_canonical_adjustment_write(preserved=None):
    """
    Store Edge must record operator cash through the dedicated 10F intent family.
    Canonical Cash Drawer Adjustment creation/submission is Primary-only except
    for the already-verified preserved-intent materializer path.
    """
    if preserved:
        return
    runtime = str(frappe.conf.get("nkt_runtime_role") or "Primary").strip()
    if runtime == "Store Edge":
        frappe.throw(
            _(
                "Use F12 Record Adjustment. Direct canonical Cash Drawer Adjustment "
                "writes are unavailable on this Store Edge."
            ),
            frappe.PermissionError,
        )



def validate_adjustment_before_insert(doc, method=None):
    _require_cashier_or_admin("create a cash drawer adjustment")
    shift = frappe.get_doc(SHIFT, doc.cashier_shift)
    _assert_shift_access(shift)
    preserved = _c15c10f_preserved_intent_journal(doc)
    _c15c10f_guard_direct_canonical_adjustment_write(preserved)
    if not preserved:
        _assert_open_shift(shift)
    doc.company = shift.company
    doc.settlement_location = shift.settlement_location
    doc.cashier = shift.cashier
    doc.posting_datetime = (
        get_datetime(preserved.settled_at)
        if preserved
        else (doc.posting_datetime or now_datetime())
    )
    doc.status = "Draft"


def validate_adjustment(doc, method=None):
    mapping = ADJUSTMENT_MAP.get(doc.adjustment_type)
    if not mapping:
        frappe.throw(_("Unsupported Cash Drawer Adjustment type."))
    shift = frappe.get_doc(SHIFT, doc.cashier_shift)
    _assert_shift_access(shift)
    preserved = _c15c10f_preserved_intent_journal(doc)
    _c15c10f_guard_direct_canonical_adjustment_write(preserved)
    if not preserved:
        _assert_open_shift(shift)
    doc.company = shift.company
    doc.settlement_location = shift.settlement_location
    doc.cashier = shift.cashier
    doc.direction = mapping["direction"]

    if doc.adjustment_type == "Advance / Mid-Shift Deposit":
        deposit_total, clean_denominations = _deposit_denomination_total_from_doc(doc)
        doc.deposit_denomination_total = deposit_total
        doc.amount = deposit_total
    if preserved:
        _c15c10f_verify_adjustment_against_journal(doc, preserved)
    if doc.docstatus == 0:
        doc.status = "Draft"


def before_submit_adjustment(doc, method=None):
    validate_adjustment(doc, method)
    amount = flt(doc.amount)
    if amount <= 0:
        frappe.throw(_("Enter at least one denomination. The advance deposit total must be greater than zero."))

    preserved = _c15c10f_preserved_intent_journal(doc)
    if doc.adjustment_type == "Advance / Mid-Shift Deposit":
        if abs(amount - flt(doc.deposit_denomination_total)) > TOLERANCE:
            frappe.throw(_("Advance deposit amount must equal its denomination breakdown."))
        if not preserved:
            summary = calculate_shift_summary(doc.cashier_shift)
            available_cash = flt(summary["expected_cash"])
            if available_cash < -TOLERANCE:
                frappe.throw(
                    _("This shift already has negative expected cash ({0}). Reverse the incorrect cash-out adjustment before posting another deposit.").format(
                        frappe.utils.fmt_money(available_cash)
                    )
                )
            if amount - available_cash > TOLERANCE:
                frappe.throw(
                    _("Advance deposit {0} exceeds the current expected drawer cash of {1}. Reduce the denominations before submitting.").format(
                        frappe.utils.fmt_money(amount), frappe.utils.fmt_money(available_cash)
                    )
                )
    elif not (doc.purpose or "").strip():
        frappe.throw(_("Remarks / Explanation is required for this adjustment type."))

    doc.status = "Posted"
    doc.posted_by = preserved.origin_user if preserved else frappe.session.user
    doc.posted_on = get_datetime(preserved.settled_at) if preserved else now_datetime()


def _create_movement(*, adjustment, mapping, source_row=None, remarks=None):
    movement = frappe.new_doc(MOVEMENT)
    movement.company = adjustment.company
    preserved_event_uuid = str(
        getattr(adjustment.flags, C15C10F_PRESERVED_FLAG, "") or ""
    ).strip()
    movement.posting_datetime = (
        adjustment.posting_datetime if preserved_event_uuid else now_datetime()
    )
    movement.cashier_shift = adjustment.cashier_shift
    movement.settlement_location = adjustment.settlement_location
    movement.cashier = adjustment.cashier
    movement.movement_type = mapping["movement_type"]
    movement.direction = mapping["direction"]
    movement.payment_method = "Cash"
    movement.amount = flt(adjustment.amount)
    movement.affects_cash_drawer = 1
    movement.source_doctype = ADJUSTMENT
    movement.source_name = adjustment.name
    movement.source_row = source_row
    movement.reference_number = adjustment.name
    movement.remarks = remarks or adjustment.purpose
    movement.status = "Posted"
    if preserved_event_uuid:
        setattr(
            movement.flags,
            C15C10F_PRESERVED_FLAG,
            preserved_event_uuid,
        )

        # Validate the immutable preserved event/journal/source binding before
        # granting the process-local closed-shift validation context. This is
        # intentionally stronger than trusting the document flag alone.
        _c15c10f_preserved_movement_journal(movement)

        with _preserved_cash_drawer_shift_validation_context(
            cashier_shift=movement.cashier_shift,
            company=movement.company,
            settlement_location=movement.settlement_location,
            cashier=movement.cashier,
        ):
            # Both insert() and submit() perform lifecycle validation. Keep the
            # already-verified preserved-event context alive through BOTH
            # operations; otherwise before_submit() rechecks require_open=True
            # after the context has already been reset.
            movement.flags.ignore_permissions = True
            movement.insert(ignore_permissions=True)
            movement.flags.ignore_permissions = True
            movement.submit()
    else:
        movement.flags.ignore_permissions = True
        movement.insert(ignore_permissions=True)
        movement.flags.ignore_permissions = True
        movement.submit()

    return movement


def post_adjustment_movement(doc, method=None):
    duplicates = frappe.get_all(
        MOVEMENT,
        filters={"source_doctype": ADJUSTMENT, "source_name": doc.name, "docstatus": 1},
        fields=["name", "source_row"],
        limit_page_length=0,
    )
    duplicate = next((row.name for row in duplicates if not row.source_row), None)
    if duplicate:
        frappe.throw(_("This adjustment already has a posted Cashier Movement: {0}").format(duplicate))
    movement = _create_movement(adjustment=doc, mapping=ADJUSTMENT_MAP[doc.adjustment_type])
    frappe.db.set_value(ADJUSTMENT, doc.name, {"cashier_movement": movement.name, "status": "Posted"}, update_modified=False)
    _apply_summary(doc.cashier_shift)
    _log("Cash Drawer Adjustment Posted", shift=doc.cashier_shift, adjustment=doc.name, before_status="Draft", after_status="Posted", reason=doc.purpose, details={"movement": movement.name, "type": doc.adjustment_type, "amount": doc.amount})


def prevent_adjustment_cancel(doc, method=None):
    frappe.throw(_("Posted cash drawer adjustments cannot be cancelled. An authorized reversal must create an opposite cashier movement."))


@frappe.whitelist()
def reverse_cash_adjustment(adjustment_name, reason):
    _require_admin("reverse a cash drawer adjustment")
    reason = (reason or "").strip()
    if not reason:
        frappe.throw(_("A reversal reason is required."))
    adjustment = frappe.get_doc(ADJUSTMENT, adjustment_name)
    if adjustment.docstatus != 1 or adjustment.status != "Posted":
        frappe.throw(_("Only a posted, unreversed adjustment may be reversed."))
    shift = frappe.get_doc(SHIFT, adjustment.cashier_shift)
    _assert_open_shift(shift)
    mapping = REVERSAL_MAP[adjustment.adjustment_type]
    movement = _create_movement(
        adjustment=adjustment,
        mapping=mapping,
        source_row="REVERSAL",
        remarks=f"Controlled reversal of {adjustment.name}: {reason}",
    )
    frappe.db.set_value(
        ADJUSTMENT,
        adjustment.name,
        {
            "status": "Reversed",
            "reversal_movement": movement.name,
            "reversal_reason": reason,
            "reversed_by": frappe.session.user,
            "reversed_on": now_datetime(),
        },
        update_modified=True,
    )
    _apply_summary(adjustment.cashier_shift)
    _log("Cash Drawer Adjustment Reversed", shift=adjustment.cashier_shift, adjustment=adjustment.name, before_status="Posted", after_status="Reversed", reason=reason, details={"reversal_movement": movement.name})
    frappe.db.commit()
    return {"adjustment": adjustment.name, "status": "Reversed", "reversal_movement": movement.name}


# ---------------------------------------------------------------------------
# UI installation
# ---------------------------------------------------------------------------


SHIFT_CLIENT_SCRIPT = r'''
frappe.ui.form.on('NKT Cashier Shift', {
  refresh(frm) {
    frm.clear_custom_buttons();
    const roles = frappe.user_roles || [];
    const isAdmin = frappe.session.user === 'Administrator' || ['System Manager', 'NKT OWNER', 'NKT ADMINISTRATOR'].some(r => roles.includes(r));
    const isCashier = roles.includes('NKT Cashier');
    const ownShift = frm.doc.cashier === frappe.session.user;
    const v195Marker = 'NKT Shift Close V1.9.5';

    const removeLegacyShiftButtons = () => {
      const legacyLabels = [
        'Record Denomination Count', 'Record and Turn Over Count', 'Turn Over Shift',
        'Mark Reviewed / OK', 'Reviewed / OK', 'Review / Close Shift', 'Close Shift'
      ];
      legacyLabels.forEach(label => {
        frm.remove_custom_button(__(label));
        ['Shift Actions', 'Review Actions', 'Actions'].forEach(group => frm.remove_custom_button(__(label), __(group)));
      });
      if (frm.page && frm.page.wrapper) {
        frm.page.wrapper.find('.custom-actions button, .inner-group-button button').each(function() {
          const text = ($(this).text() || '').trim();
          if (legacyLabels.some(label => text === __(label) || text === label)) $(this).remove();
        });
      }
    };
    removeLegacyShiftButtons();
    setTimeout(removeLegacyShiftButtons, 250);
    setTimeout(removeLegacyShiftButtons, 1000);

    frm.set_df_property('cashier', 'read_only', 1);
    if (!frm.is_new()) {
      frm.set_df_property('opening_cash', 'read_only', 1);
      // Existing shifts must close/review through controlled V1.9 actions,
      // never through Frappe's generic Submit button.
      setTimeout(() => {
        if (frm.page && frm.page.clear_primary_action) frm.page.clear_primary_action();
      }, 0);
    }

    const formatMoney = value => format_currency(value || 0);
    const denominationFields = [
      ['bill_1000_qty', '₱1,000 Bills', 1000], ['bill_500_qty', '₱500 Bills', 500],
      ['bill_200_qty', '₱200 Bills', 200], ['bill_100_qty', '₱100 Bills', 100],
      ['bill_50_qty', '₱50 Bills', 50], ['bill_20_qty', '₱20 Bills', 20],
      ['coin_20_qty', '₱20 Coins', 20], ['coin_10_qty', '₱10 Coins', 10],
      ['coin_5_qty', '₱5 Coins', 5], ['coin_1_qty', '₱1 Coins', 1],
      ['coin_025_qty', '₱0.25 Coins', 0.25]
    ];

    function openClosingCountDialog() {
      frappe.call({
        method: 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.get_cash_count_draft',
        args: {shift_name: frm.doc.name},
        freeze: true,
        callback(r) {
          if (r.exc) return;
          const existing = r.message || {};
          const fields = [];
          denominationFields.forEach((entry, index) => {
            if (index === 6) fields.push({fieldtype: 'Column Break'});
            fields.push({fieldname: entry[0], label: entry[1], fieldtype: 'Int', default: (existing.denominations || {})[entry[0]] || 0});
          });
          fields.push({fieldtype:'Section Break'});
          fields.push({fieldname:'calculated_total', label:'Current Denomination Total', fieldtype:'Currency', read_only:1, default:existing.actual_cash || 0});
          fields.push({fieldname:'expected_cash_display', label:'Current Expected Cash', fieldtype:'Currency', read_only:1, default:existing.expected_cash || 0});
          fields.push({fieldname:'difference_display', label:'Current Over / (Short)', fieldtype:'Currency', read_only:1, default:existing.over_short || 0});
          fields.push({fieldname:'notes', label:'Cash Difference Explanation / Closing Notes', fieldtype:'Small Text', default:existing.notes || ''});
          fields.push({fieldname:'save_draft_button', label:'Save Count Draft', fieldtype:'Button'});

          const dialog = new frappe.ui.Dialog({
            title: __('Closing Count — Draft or Finalize'),
            fields,
            primary_action_label: __('Finalize and Close My Shift'),
            primary_action(values) {
              const denominations = {};
              denominationFields.forEach(entry => denominations[entry[0]] = values[entry[0]] || 0);
              frappe.confirm(
                __('Finalize this count and close your shift? No more drawer movements can be posted until an Owner/Administrator reopens it.'),
                () => frappe.call({
                  method: 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.finalize_and_close_shift',
                  args: {shift_name: frm.doc.name, denominations, notes: values.notes || ''},
                  freeze: true,
                  callback(res) {
                    if (!res.exc) {
                      dialog.hide();
                      const m = res.message || {};
                      frappe.msgprint(__('Cashier shift closed.<br>Expected Cash: {0}<br>Actual Cash: {1}<br>Over / (Short): {2}<br>Status: {3}', [
                        formatMoney(m.expected_cash), formatMoney(m.actual_cash), formatMoney(m.over_short), m.status || ''
                      ]));
                      frm.reload_doc();
                    }
                  }
                })
              );
            }
          });

          const recalc = () => {
            let total = 0;
            denominationFields.forEach(entry => total += (flt(dialog.get_value(entry[0])) || 0) * entry[2]);
            dialog.set_value('calculated_total', total);
            dialog.set_value('difference_display', total - flt(existing.expected_cash || 0));
          };
          dialog.show();
          denominationFields.forEach(entry => dialog.fields_dict[entry[0]].$input.on('input change', recalc));
          dialog.fields_dict.save_draft_button.$input.off('click').on('click', () => {
            const values = dialog.get_values(true) || {};
            const denominations = {};
            denominationFields.forEach(entry => denominations[entry[0]] = values[entry[0]] || 0);
            frappe.call({
              method: 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.save_cash_count_draft',
              args: {shift_name: frm.doc.name, denominations, notes: values.notes || ''},
              freeze: true,
              callback(res) {
                if (!res.exc) {
                  const m = res.message || {};
                  frappe.show_alert({message: __('Count draft saved. Total {0}; difference {1}', [formatMoney(m.actual_cash), formatMoney(m.over_short)]), indicator:'green'});
                }
              }
            });
          });
          recalc();
        }
      });
    }

    if (!frm.is_new() && (isAdmin || (isCashier && ownShift))) {
      frm.add_custom_button(__('Refresh Shift Totals'), () => {
        frappe.call({
          method: 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.refresh_shift_totals',
          args: {shift_name: frm.doc.name}, freeze: true, callback: () => frm.reload_doc()
        });
      }, __('V1.9.5 Shift Actions'));
      frm.add_custom_button(__('View My Shift Movements'), () => frappe.set_route('List', 'NKT Cashier Movement', {cashier_shift: frm.doc.name}), __('Shift Actions'));
    }

    if (!frm.is_new() && frm.doc.status === 'Open' && ownShift && (isCashier || isAdmin)) {
      frm.add_custom_button(__('Closing Count'), openClosingCountDialog, __('Shift Actions'));
    }

    const awaitingReview = ['Cashier Closed - Awaiting Review', 'Counted - Awaiting Approval', 'Turned Over - Awaiting Review'].includes(frm.doc.status);
    if (!frm.is_new() && awaitingReview && isAdmin) {
      frm.add_custom_button(__('Review and Finalize'), () => {
        const dialog = new frappe.ui.Dialog({
          title: __('Owner / Administrator Reconciliation Review'),
          fields: [{fieldname:'approval_reason', label:'Review Note', fieldtype:'Small Text'}],
          primary_action_label: __('Mark Reviewed and Finalize'),
          primary_action(values) {
            frappe.call({
              method: 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.approve_and_close_shift',
              args: {shift_name: frm.doc.name, approval_reason: values.approval_reason || ''},
              freeze: true,
              callback(r) { if (!r.exc) { dialog.hide(); frm.reload_doc(); } }
            });
          }
        });
        dialog.show();
      }, __('V1.9.5 Review Actions'));
      frm.add_custom_button(__('Reopen for Cashier Correction'), () => {
        frappe.prompt(
          [{fieldname:'reason', label:'Reason for Reopening', fieldtype:'Small Text', reqd:1}],
          values => frappe.call({
            method:'nkt_operations.nkt_store_operations.features.cashier.shift_engine.reopen_cashier_closed_shift',
            args:{shift_name:frm.doc.name, reason:values.reason}, freeze:true, callback:() => frm.reload_doc()
          }),
          __('Controlled Reopen'), __('Reopen Shift')
        );
      }, __('V1.9.5 Review Actions'));
    }
  }
});
'''


ADJUSTMENT_CLIENT_SCRIPT = r'''
// NKT Shift Close V1.9.6 — C15C.10F FINAL CASH DRAWER FRONT DOOR
// One browser business action. Server runtime owns authority/routing.
frappe.ui.form.on('NKT Cash Drawer Adjustment', {
  setup(frm) {
    const roles = frappe.user_roles || [];
    const isAdmin = frappe.session.user === 'Administrator' || ['System Manager', 'NKT OWNER', 'NKT ADMINISTRATOR'].some(r => roles.includes(r));
    frm.set_query('cashier_shift', () => {
      const filters = {status: 'Open', docstatus: 0};
      if (!isAdmin) filters.cashier = frappe.session.user;
      return {filters};
    });
  },
  onload(frm) {
    nkt_apply_fast_adjustment_layout(frm);
    nkt_load_available_cash(frm);
    nkt_install_cash_drawer_keys(frm);
  },
  refresh(frm) {
    nkt_apply_fast_adjustment_layout(frm);
    nkt_recalculate_deposit(frm);
    nkt_load_available_cash(frm);
    nkt_install_cash_drawer_keys(frm);

    if (frm.is_new() && frm.doc.docstatus === 0) {
      frm.disable_save();
      frm.page.clear_primary_action();
      frm.page.set_primary_action(__('F12 Record Adjustment'), () => nkt_record_cash_drawer_frontdoor(frm, false));
      frm.add_custom_button(__('F10 Record & Open Voucher'), () => nkt_record_cash_drawer_frontdoor(frm, true));
    }

    const roles = frappe.user_roles || [];
    const isAdmin = frappe.session.user === 'Administrator' || ['System Manager', 'NKT OWNER', 'NKT ADMINISTRATOR'].some(r => roles.includes(r));
    if (frm.doc.docstatus === 1 && frm.doc.status === 'Posted' && isAdmin) {
      frm.add_custom_button(__('Reverse Adjustment'), () => {
        frappe.prompt(
          [{fieldname:'reason', label:'Reversal Reason', fieldtype:'Small Text', reqd:1}],
          values => frappe.call({
            method:'nkt_operations.nkt_store_operations.features.cashier.shift_engine.reverse_cash_adjustment',
            args:{adjustment_name:frm.doc.name, reason:values.reason}, freeze:true, callback:() => frm.reload_doc()
          }),
          __('Controlled Reversal'), __('Reverse')
        );
      });
    }
  },
  cashier_shift(frm) {
    if (!frm.doc.cashier_shift) { frm.__nkt_available_cash = null; return; }
    frappe.db.get_value('NKT Cashier Shift', frm.doc.cashier_shift, ['company', 'settlement_location', 'cashier']).then(r => {
      const v = r.message || {};
      frm.set_value('company', v.company);
      frm.set_value('settlement_location', v.settlement_location);
      frm.set_value('cashier', v.cashier);
    });
    nkt_load_available_cash(frm);
  },
  adjustment_type(frm) {
    nkt_apply_fast_adjustment_layout(frm);
    nkt_recalculate_deposit(frm);
    nkt_load_available_cash(frm);
  },
  validate(frm) {
    nkt_validate_cash_drawer_frontdoor(frm);
  }
});

const nkt_deposit_denominations = {
  deposit_bill_1000_qty:1000, deposit_bill_500_qty:500, deposit_bill_200_qty:200,
  deposit_bill_100_qty:100, deposit_bill_50_qty:50, deposit_bill_20_qty:20,
  deposit_coin_20_qty:20, deposit_coin_10_qty:10, deposit_coin_5_qty:5,
  deposit_coin_1_qty:1, deposit_coin_025_qty:0.25
};

function nkt_bound_device_id() {
  try { return String(window.localStorage.getItem('nkt_device_id') || '').trim(); }
  catch (_) { return ''; }
}

function nkt_cash_drawer_uuid() {
  if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

function nkt_install_cash_drawer_keys(frm) {
  const ns = '.nktCashDrawerC15C10F';
  $(document).off(`keydown${ns}`).on(`keydown${ns}`, e => {
    if ($('.modal.show').length || !cur_frm || cur_frm.doctype !== 'NKT Cash Drawer Adjustment') return;
    if (cur_frm.doc.docstatus !== 0 || !cur_frm.is_new()) return;
    if (e.key === 'F10') {
      e.preventDefault();
      e.stopImmediatePropagation();
      nkt_record_cash_drawer_frontdoor(cur_frm, true);
    }
    if (e.key === 'F12') {
      e.preventDefault();
      e.stopImmediatePropagation();
      nkt_record_cash_drawer_frontdoor(cur_frm, false);
    }
  });
}

function nkt_apply_fast_adjustment_layout(frm) {
  const isDeposit = frm.doc.adjustment_type === 'Advance / Mid-Shift Deposit';
  ['company','settlement_location','cashier','posting_datetime','direction','deposit_section','deposit_destination','deposit_reference_number'].forEach(field => frm.toggle_display(field, false));
  frm.toggle_display('amount', !isDeposit);
  frm.toggle_display('party_name', !isDeposit);
  frm.toggle_display('supporting_document', !isDeposit);
  frm.set_df_property('purpose', 'label', isDeposit ? __('Remarks (Optional)') : __('Remarks / Explanation'));
  frm.set_df_property('purpose', 'reqd', isDeposit ? 0 : 1);
  frm.set_df_property('amount', 'read_only', isDeposit ? 1 : 0);
}

function nkt_recalculate_deposit(frm) {
  if (frm.doc.adjustment_type !== 'Advance / Mid-Shift Deposit' || frm.doc.docstatus !== 0) return;
  let total = 0;
  Object.keys(nkt_deposit_denominations).forEach(field => total += (flt(frm.doc[field]) || 0) * nkt_deposit_denominations[field]);
  frm.set_value('deposit_denomination_total', total);
  frm.set_value('amount', total);
  nkt_show_available_cash(frm, total);
}

function nkt_load_available_cash(frm) {
  if (!frm.doc.cashier_shift || frm.doc.adjustment_type !== 'Advance / Mid-Shift Deposit') return;
  frappe.call({
    method:'nkt_operations.nkt_store_operations.features.cashier.internal.cash_drawer_adjustment_fast_sync.get_cash_drawer_frontdoor_context',
    args:{shift_name:frm.doc.cashier_shift, device_id:nkt_bound_device_id()},
    callback:r => {
      const m = r.message || {};
      frm.__nkt_available_cash = flt(m.expected_cash || 0);
      nkt_show_available_cash(frm, flt(frm.doc.deposit_denomination_total || 0));
    }
  });
}

function nkt_show_available_cash(frm, depositTotal) {
  if (frm.doc.adjustment_type !== 'Advance / Mid-Shift Deposit' || frm.__nkt_available_cash == null) return;
  const available = flt(frm.__nkt_available_cash || 0);
  const remaining = available - flt(depositTotal || 0);
  frm.set_intro(__('Expected drawer cash before deposit: {0}. Remaining after this draft: {1}.', [
    format_currency(available), format_currency(remaining)
  ]), remaining < -0.005 ? 'red' : 'blue');
}

function nkt_validate_cash_drawer_frontdoor(frm) {
  if (!frm.doc.cashier_shift) frappe.throw(__('Cashier Shift is required.'));
  if (!frm.doc.adjustment_type) frappe.throw(__('Adjustment Type is required.'));

  if (frm.doc.adjustment_type === 'Advance / Mid-Shift Deposit') {
    nkt_recalculate_deposit(frm);
    const total = flt(frm.doc.deposit_denomination_total || 0);
    if (total <= 0) {
      frappe.throw(__('Enter at least one denomination. The advance deposit total must be greater than zero.'));
    }
    if (frm.__nkt_available_cash != null && total > flt(frm.__nkt_available_cash) + 0.005) {
      frappe.throw(__('Advance deposit {0} exceeds current expected drawer cash {1}. Reduce the denominations before recording.', [
        format_currency(total), format_currency(frm.__nkt_available_cash)
      ]));
    }
  } else {
    if (flt(frm.doc.amount || 0) <= 0) frappe.throw(__('Amount must be greater than zero.'));
    if (!String(frm.doc.purpose || '').trim()) frappe.throw(__('Remarks / Explanation is required.'));
  }
}

function nkt_cash_drawer_payload(frm) {
  const out = {
    cashier_shift:frm.doc.cashier_shift,
    adjustment_type:frm.doc.adjustment_type,
    amount:flt(frm.doc.amount || 0),
    party_name:String(frm.doc.party_name || '').trim(),
    purpose:String(frm.doc.purpose || '').trim(),
    supporting_document:String(frm.doc.supporting_document || '').trim(),
    client_observed_at:new Date().toISOString()
  };
  Object.keys(nkt_deposit_denominations).forEach(field => out[field] = cint(frm.doc[field] || 0));
  return out;
}

function nkt_record_cash_drawer_frontdoor(frm, openVoucher) {
  if (frm.__nkt_recording) return;
  nkt_validate_cash_drawer_frontdoor(frm);
  if (!frm.__nkt_request_id) frm.__nkt_request_id = nkt_cash_drawer_uuid();

  frm.__nkt_recording = true;
  const requestId = frm.__nkt_request_id;
  const method = 'nkt_operations.nkt_store_operations.features.cashier.internal.cash_drawer_adjustment_fast_sync.record_cash_drawer_adjustment_frontdoor';

  frappe.call({
    method,
    args:{
      payload:JSON.stringify(nkt_cash_drawer_payload(frm)),
      request_id:requestId,
      device_id:nkt_bound_device_id()
    },
    freeze:true,
    freeze_message:__('Recording cash drawer adjustment…')
  }).then(r => {
    nkt_finish_cash_drawer_frontdoor(frm, r.message || {}, openVoucher);
  }).catch(err => {
    frm.__nkt_recording = false;
    frappe.msgprint({
      title:__('Cash Drawer Adjustment Not Recorded'),
      indicator:'red',
      message:(err && err.message) ? err.message : __('The request did not complete. Use F12 again; the same request ID is safe to retry.')
    });
  });
}

function nkt_finish_cash_drawer_frontdoor(frm, result, openVoucher) {
  frm.__nkt_recording = false;
  frappe.show_alert({message:__('Cash drawer adjustment recorded.'), indicator:'green'}, 5);

  if (result.official_print_available && result.cash_drawer_adjustment && openVoucher) {
    frappe.set_route('Form', 'NKT Cash Drawer Adjustment', result.cash_drawer_adjustment);
    return;
  }

  if (!result.official_print_available && openVoucher) {
    frappe.msgprint({
      title:__('Cash Recorded'),
      indicator:'blue',
      message:__('The cash movement is safely recorded. The official voucher will be available after synchronization with the main system.')
    });
  }

  frm.__nkt_request_id = null;
  setTimeout(() => frappe.new_doc('NKT Cash Drawer Adjustment'), 250);
}

frappe.ui.form.on('NKT Cash Drawer Adjustment', Object.fromEntries(
  Object.keys(nkt_deposit_denominations).map(field => [field, nkt_recalculate_deposit])
));
'''


def _ensure_client_script(name, dt, script):
    existing = frappe.db.exists("Client Script", name)
    if existing:
        doc = frappe.get_doc("Client Script", existing)
    else:
        doc = frappe.new_doc("Client Script")
        doc.name = name
    doc.dt = dt
    doc.view = "Form"
    doc.enabled = 1
    doc.script = script
    doc.flags.ignore_permissions = True
    if doc.is_new():
        doc.insert(ignore_permissions=True)
    else:
        doc.save(ignore_permissions=True)


SHIFT_PRINT_FORMAT = r'''
<div style="font-family:Arial,sans-serif;font-size:11px;">
  <h2 style="text-align:center;margin-bottom:2px;">NKT Cashier Shift Reconciliation</h2>
  <div style="text-align:center;margin-bottom:12px;">{{ doc.name }}</div>
  <table style="width:100%;border-collapse:collapse;" border="1" cellpadding="5">
    <tr><td><b>Company</b></td><td>{{ doc.company }}</td><td><b>Settlement Location</b></td><td>{{ doc.settlement_location }}</td></tr>
    <tr><td><b>Cashier</b></td><td>{{ doc.cashier }}</td><td><b>Status</b></td><td>{{ doc.status }}</td></tr>
    <tr><td><b>Opened</b></td><td>{{ doc.shift_start }}</td><td><b>Turned Over</b></td><td>{{ doc.shift_end or '' }}</td></tr>
  </table>
  <h4>Drawer Reconciliation</h4>
  <table style="width:100%;border-collapse:collapse;" border="1" cellpadding="5">
    <tr><td>Opening Cash</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.opening_cash or 0) }}</td></tr>
    <tr><td>Total Cash In</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.total_cash_in or 0) }}</td></tr>
    <tr><td>Total Cash Out</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.total_cash_out or 0) }}</td></tr>
    <tr><td><b>Expected Cash</b></td><td style="text-align:right;"><b>{{ frappe.utils.fmt_money(doc.expected_cash or 0) }}</b></td></tr>
    <tr><td>Actual Cash from Denominations</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.actual_cash_count or 0) }}</td></tr>
    <tr><td><b>Over / (Short)</b></td><td style="text-align:right;"><b>{{ frappe.utils.fmt_money(doc.over_short or 0) }}</b></td></tr>
  </table>
  <h4>Cash Breakdown</h4>
  <table style="width:100%;border-collapse:collapse;" border="1" cellpadding="5">
    <tr><td>Cash Sales</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.custom_nkt_cash_sales or 0) }}</td><td>Cash Account Collections</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.custom_nkt_cash_account_collections or 0) }}</td></tr>
    <tr><td>Petty Cash Releases</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.custom_nkt_petty_cash_releases or 0) }}</td><td>Petty Cash Returns</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.custom_nkt_petty_cash_returns or 0) }}</td></tr>
    <tr><td>Cash Drops</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.custom_nkt_cash_drops or 0) }}</td><td>Cash Refunds</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.custom_nkt_cash_refunds or 0) }}</td></tr>
    <tr><td>Advance / Mid-Shift Deposits</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.custom_nkt_advance_deposits or 0) }}</td><td>Other Cash Out</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.custom_nkt_cash_other_out or 0) }}</td></tr>
  </table>
  <h4>Non-Cash In</h4>
  <table style="width:100%;border-collapse:collapse;" border="1" cellpadding="5">
    <tr><td>Checks</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.custom_nkt_check_in or 0) }}</td><td>GCash</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.custom_nkt_gcash_in or 0) }}</td></tr>
    <tr><td>Maya</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.custom_nkt_maya_in or 0) }}</td><td>Bank Transfer</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.custom_nkt_bank_transfer_in or 0) }}</td></tr>
    <tr><td>Card</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.custom_nkt_credit_card_in or 0) }}</td><td>Online / Other Online</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.custom_nkt_online_in or 0) }}</td></tr>
    <tr><td>Card Surcharge (included)</td><td style="text-align:right;">{{ frappe.utils.fmt_money(doc.custom_nkt_card_surcharge_in or 0) }}</td><td></td><td></td></tr>
  </table>
  {% set price_rows = frappe.parse_json(doc.custom_nkt_price_adjustment_audit_json or '[]') %}
  <h4>Selling Price Adjustments / Manager Authorization</h4>
  <table style="width:100%;border-collapse:collapse;" border="1" cellpadding="4">
    <tr><th>Sale / Item</th><th>Qty</th><th>Normal</th><th>Actual</th><th>Diff / Unit</th><th>Manager Authorization</th><th>Encoder / Match</th></tr>
    {% for row in price_rows %}
    <tr>
      <td><b>{{ row.get('sale','') }}</b><br>{{ row.get('item','') }}<br><small>{{ row.get('warehouse','') }}</small></td>
      <td style="text-align:right;">{{ row.get('qty',0) }} {{ row.get('uom','') }}</td>
      <td style="text-align:right;">{{ frappe.utils.fmt_money(row.get('standard_rate',0)) }}</td>
      <td style="text-align:right;">{{ frappe.utils.fmt_money(row.get('actual_rate',0)) }}</td>
      <td style="text-align:right;">{{ frappe.utils.fmt_money(row.get('difference_per_unit',0)) }}</td>
      <td>{% if row.get('authorized_by') %}<b>{{ row.get('authorized_by') }}</b><br>{{ row.get('reason','') or '—' }}{% if row.get('explanation') %}<br><small>{{ row.get('explanation') }}</small>{% endif %}<br><small>{{ row.get('authorized_on','') }} · {{ row.get('authorization_source','') }}</small>{% else %}<b>NO LINKED MANAGER AUTHORIZATION</b>{% endif %}</td>
      <td>{{ row.get('encoder','') or '—' }}<br><small>{{ row.get('reconciliation_status','') or 'No Encoder Match' }}</small>{% if row.get('matched_customer_order') %}<br><small>{{ row.get('matched_customer_order') }}</small>{% endif %}</td>
    </tr>
    {% else %}
    <tr><td colspan="7" style="text-align:center;">No adjusted selling-rate rows in this shift.</td></tr>
    {% endfor %}
    {% if price_rows %}<tr><td colspan="4"><b>Net effect vs normal selling rates</b></td><td style="text-align:right;"><b>{{ frappe.utils.fmt_money(doc.custom_nkt_price_adjustment_total_effect or 0) }}</b></td><td colspan="2">{{ doc.custom_nkt_price_adjustment_count or 0 }} adjusted row(s)</td></tr>{% endif %}
  </table>

  <h4>Denomination Count</h4>
  <table style="width:100%;border-collapse:collapse;" border="1" cellpadding="4">
    <tr><th>Denomination</th><th>Qty</th><th>Denomination</th><th>Qty</th></tr>
    <tr><td>₱1,000</td><td>{{ doc.bill_1000_qty or 0 }}</td><td>₱20 Coin</td><td>{{ doc.coin_20_qty or 0 }}</td></tr>
    <tr><td>₱500</td><td>{{ doc.bill_500_qty or 0 }}</td><td>₱10 Coin</td><td>{{ doc.coin_10_qty or 0 }}</td></tr>
    <tr><td>₱200</td><td>{{ doc.bill_200_qty or 0 }}</td><td>₱5 Coin</td><td>{{ doc.coin_5_qty or 0 }}</td></tr>
    <tr><td>₱100</td><td>{{ doc.bill_100_qty or 0 }}</td><td>₱1 Coin</td><td>{{ doc.coin_1_qty or 0 }}</td></tr>
    <tr><td>₱50</td><td>{{ doc.bill_50_qty or 0 }}</td><td>₱0.25 Coin</td><td>{{ doc.coin_025_qty or 0 }}</td></tr>
    <tr><td>₱20</td><td>{{ doc.bill_20_qty or 0 }}</td><td></td><td></td></tr>
  </table>
  <p><b>Cashier Closed By / On:</b> {{ doc.custom_nkt_cashier_closed_by or '' }} / {{ doc.custom_nkt_cashier_closed_on or '' }}</p>
  <p><b>Cashier Notes:</b> {{ doc.count_notes or '' }}</p>
  <p><b>Owner/Administrator Review:</b> {{ doc.approval_reason or '' }}</p>
  <table style="width:100%;margin-top:35px;"><tr><td style="width:50%;text-align:center;border-top:1px solid #000;">Cashier Signature</td><td style="width:50%;text-align:center;border-top:1px solid #000;">Reviewed / Approved By</td></tr></table>
</div>
'''

ADJUSTMENT_PRINT_FORMAT = r'''
<div style="font-family:Arial,sans-serif;font-size:12px;">
  <h2 style="text-align:center;margin-bottom:2px;">{% if doc.adjustment_type == 'Advance / Mid-Shift Deposit' %}Advance / Mid-Shift Deposit Slip{% elif doc.direction == 'Out' %}Cash Disbursement Voucher{% else %}Cash Drawer Receipt / Adjustment{% endif %}</h2>
  <div style="text-align:center;margin-bottom:16px;"><b>{{ doc.name }}</b></div>
  <table style="width:100%;border-collapse:collapse;" border="1" cellpadding="7">
    <tr><td><b>Date/Time</b></td><td>{{ doc.posting_datetime }}</td><td><b>Shift</b></td><td>{{ doc.cashier_shift }}</td></tr>
    <tr><td><b>Cashier</b></td><td>{{ doc.cashier }}</td><td><b>Location</b></td><td>{{ doc.settlement_location }}</td></tr>
    <tr><td><b>Type</b></td><td>{{ doc.adjustment_type }}</td><td><b>Amount</b></td><td>{{ frappe.utils.fmt_money(doc.amount or 0) }}</td></tr>
    <tr><td><b>Paid To / Received From</b></td><td colspan="3">{{ doc.party_name or '' }}</td></tr>
    <tr><td><b>Purpose</b></td><td colspan="3">{{ doc.purpose or '' }}</td></tr>
    <tr><td><b>Status</b></td><td>{{ doc.status }}</td><td><b>Movement</b></td><td>{{ doc.cashier_movement or '' }}</td></tr>
    {% if doc.adjustment_type == 'Advance / Mid-Shift Deposit' %}
    <tr><td><b>Deposit Destination</b></td><td>{{ doc.deposit_destination or '' }}</td><td><b>Deposit Reference</b></td><td>{{ doc.deposit_reference_number or '' }}</td></tr>
    {% endif %}
  </table>
  {% if doc.adjustment_type == 'Advance / Mid-Shift Deposit' %}
  <h4>Deposit Denomination Breakdown — {{ frappe.utils.fmt_money(doc.deposit_denomination_total or 0) }}</h4>
  <table style="width:100%;border-collapse:collapse;" border="1" cellpadding="5">
    <tr><td>₱1,000</td><td>{{ doc.deposit_bill_1000_qty or 0 }}</td><td>₱20 Coin</td><td>{{ doc.deposit_coin_20_qty or 0 }}</td></tr>
    <tr><td>₱500</td><td>{{ doc.deposit_bill_500_qty or 0 }}</td><td>₱10 Coin</td><td>{{ doc.deposit_coin_10_qty or 0 }}</td></tr>
    <tr><td>₱200</td><td>{{ doc.deposit_bill_200_qty or 0 }}</td><td>₱5 Coin</td><td>{{ doc.deposit_coin_5_qty or 0 }}</td></tr>
    <tr><td>₱100</td><td>{{ doc.deposit_bill_100_qty or 0 }}</td><td>₱1 Coin</td><td>{{ doc.deposit_coin_1_qty or 0 }}</td></tr>
    <tr><td>₱50</td><td>{{ doc.deposit_bill_50_qty or 0 }}</td><td>₱0.25 Coin</td><td>{{ doc.deposit_coin_025_qty or 0 }}</td></tr>
    <tr><td>₱20</td><td>{{ doc.deposit_bill_20_qty or 0 }}</td><td></td><td></td></tr>
  </table>
  {% endif %}
  <table style="width:100%;margin-top:55px;"><tr><td style="width:33%;text-align:center;border-top:1px solid #000;">Prepared by</td><td style="width:33%;text-align:center;border-top:1px solid #000;">Received by</td><td style="width:33%;text-align:center;border-top:1px solid #000;">Approved by</td></tr></table>
</div>
'''


def _ensure_print_format(name, doc_type, html):
    existing = frappe.db.exists("Print Format", name)
    if existing:
        doc = frappe.get_doc("Print Format", existing)
    else:
        doc = frappe.new_doc("Print Format")
        doc.name = name
    doc.doc_type = doc_type
    doc.print_format_type = "Jinja"
    doc.custom_format = 1
    doc.disabled = 0
    doc.html = html
    doc.flags.ignore_permissions = True
    if doc.is_new():
        doc.insert(ignore_permissions=True)
    else:
        doc.save(ignore_permissions=True)


def _workspace_content():
    return json.dumps(
        [
            {"id": "nkt-cashier-header", "type": "header", "data": {"text": '<span class="h4"><b>Cashier and Shift Operations</b></span>', "col": 12}},
            {"id": "nkt-cashier-card", "type": "card", "data": {"card_name": "Cashier Operations", "col": 6}},
        ],
        separators=(",", ":"),
    )


def _ensure_workspace():
    nav_links = [
        {"label": "Open Cashier Shift", "doctype": SHIFT, "doc_view": "New"},
        {"label": "My Cashier Shifts", "doctype": SHIFT, "doc_view": "List"},
        {"label": "New Cash Drawer Adjustment", "doctype": ADJUSTMENT, "doc_view": "New"},
        {"label": "Cash Drawer Adjustments", "doctype": ADJUSTMENT, "doc_view": "List"},
        {"label": "Cashier Sales", "doctype": "NKT Cashier Sale", "doc_view": "List"},
        {"label": "Receive Account Payment", "doctype": "NKT Cashier Account Collection", "doc_view": "New"},
        {"label": "Cashier Movements", "doctype": MOVEMENT, "doc_view": "List"},
    ]
    old_patch_flag = getattr(frappe.flags, 'in_patch', False)
    frappe.flags.in_patch = True
    try:
        if frappe.db.exists("Workspace", WORKSPACE):
            doc = frappe.get_doc("Workspace", WORKSPACE)
        else:
            doc = frappe.new_doc("Workspace")
        doc.title = WORKSPACE
        doc.label = WORKSPACE
        doc.module = MODULE
        doc.app = APP
        doc.type = "Workspace"
        doc.icon = "expense"
        doc.indicator_color = "orange"
        doc.public = 1
        doc.is_hidden = 0
        doc.hide_custom = 0
        doc.content = _workspace_content()
        doc.set("roles", [])
        for role in ("System Manager", "NKT OWNER", "NKT ADMINISTRATOR", CASHIER_ROLE):
            doc.append("roles", {"role": role})
        doc.set("shortcuts", [])
        doc.set("links", [])
        for nav in nav_links:
            doc.append("shortcuts", {"type": "DocType", "link_to": nav["doctype"], "doc_view": nav["doc_view"], "label": nav["label"]})
        doc.append("links", {"type": "Card Break", "label": "Cashier Operations", "icon": "expense", "link_count": len(nav_links), "description": "Open and close cashier shifts, record controlled drawer adjustments, and review cashier movements."})
        for nav in nav_links:
            doc.append("links", {"type": "Link", "label": nav["label"], "link_type": "DocType", "link_to": nav["doctype"], "onboard": 0})
        doc.flags.ignore_permissions = True
        if doc.is_new():
            doc.insert(ignore_permissions=True)
        else:
            doc.save(ignore_permissions=True)
    finally:
        frappe.flags.in_patch = old_patch_flag

    from frappe.desk.doctype.desktop_icon.desktop_icon import add_workspace_to_desktop
    original = frappe.session.user
    try:
        users = {"Administrator"}
        for row in frappe.get_all("Has Role", filters={"parenttype": "User", "role": ["in", ["NKT OWNER", "NKT ADMINISTRATOR", CASHIER_ROLE]]}, fields=["parent"]):
            users.add(row.parent)
        for user in sorted(users):
            if not frappe.db.exists("User", user):
                continue
            frappe.set_user(user)
            add_workspace_to_desktop(WORKSPACE)
            frappe.clear_cache(user=user)
    finally:
        frappe.set_user(original)
    return WORKSPACE


# ---------------------------------------------------------------------------
# Installation, cleanup, and verification
# ---------------------------------------------------------------------------


def _quarantine_invalid_open_shifts():
    quarantined = []
    rows = frappe.get_all(SHIFT, filters={"docstatus": 0, "status": "Open"}, fields=["name", "cashier", "status"])
    for row in rows:
        if _cashier_is_valid_operator(row.cashier):
            continue
        shift = frappe.get_doc(SHIFT, row.name)
        before_status = shift.status
        shift.flags.nkt_v19_internal = True
        shift.status = "Cancelled"
        shift.shift_end = now_datetime()
        shift.custom_nkt_v19_invalid_role_shift = 1
        shift.custom_nkt_v19_control_note = f"Automatically quarantined during V1.9 installation because {row.cashier} is not an NKT Cashier or authorized Owner/Administrator."
        shift.custom_nkt_v19_version = VERSION
        shift.save(ignore_permissions=True)
        _log("Invalid Role Shift Quarantined", shift=shift.name, before_status=before_status, after_status="Cancelled", reason=shift.custom_nkt_v19_control_note, details={"cashier": row.cashier})
        quarantined.append(shift.name)
    return quarantined


def _refresh_open_shifts():
    refreshed = []
    for name in frappe.get_all(SHIFT, filters={"docstatus": 0, "status": "Open"}, pluck="name", limit_page_length=0):
        _apply_summary(name)
        refreshed.append(name)
    return refreshed


def _upgrade_legacy_counted_shift(shift_name):
    """Convert a V1.9.2 counted shift into the V1.9.3+ cashier-closed state.

    Uses direct site-level field updates so the legacy standard DocType controller cannot
    re-run its obsolete Review/OK transition while the compatibility record is upgraded.
    No amount, denomination, or movement is changed.
    """
    shift = frappe.get_doc(SHIFT, shift_name)
    if cint(shift.docstatus) != 0 or shift.status not in {"Counted - Awaiting Approval", "Turned Over - Awaiting Review"}:
        return False
    if not cint(shift.blind_count_confirmed):
        return False

    summary = calculate_shift_summary(shift.name)
    closed_by = shift.custom_nkt_cashier_closed_by or shift.count_locked_by or shift.cashier
    closed_on = shift.custom_nkt_cashier_closed_on or shift.count_locked_on or shift.shift_end or shift.modified or now_datetime()
    expected_snapshot = flt(shift.custom_nkt_expected_cash_at_count)
    movement_snapshot = cint(shift.custom_nkt_movement_count_at_count)
    snapshot_json = shift.custom_nkt_breakdown_snapshot_json
    if not snapshot_json:
        snapshot_json = json.dumps(summary, default=str, sort_keys=True)
    if not movement_snapshot:
        movement_snapshot = cint(summary["movement_count"])
    # Expected cash may legitimately be zero. Use the missing JSON/close fields as the
    # compatibility signal rather than treating zero as automatically absent.
    if not shift.custom_nkt_cashier_closed_by and not shift.custom_nkt_breakdown_snapshot_json:
        expected_snapshot = flt(summary["expected_cash"])

    values = {
        "status": CASHIER_CLOSED_STATUS,
        "turnover_status": CASHIER_CLOSED_STATUS,
        "custom_nkt_cashier_closed_by": closed_by,
        "custom_nkt_cashier_closed_on": closed_on,
        "custom_nkt_expected_cash_at_count": expected_snapshot,
        "custom_nkt_movement_count_at_count": movement_snapshot,
        "custom_nkt_breakdown_snapshot_json": snapshot_json,
        "custom_nkt_v19_version": VERSION,
    }
    if not shift.shift_end:
        values["shift_end"] = closed_on
    frappe.db.set_value(SHIFT, shift.name, values, update_modified=False)
    if not frappe.db.exists(CONTROL_LOG, {"cashier_shift": shift.name, "action": "Legacy Count Upgraded to Cashier Close"}):
        _log(
            "Legacy Count Upgraded to Cashier Close",
            shift=shift.name,
            before_status=shift.status,
            after_status=CASHIER_CLOSED_STATUS,
            reason="V1.9.4 compatibility upgrade; amounts, denominations, and movements were preserved.",
            details={"expected_cash": summary["expected_cash"], "actual_cash": shift.actual_cash_count, "movement_count": summary["movement_count"]},
        )
    return True


def _upgrade_legacy_counted_shifts():
    upgraded = []
    rows = frappe.get_all(
        SHIFT,
        filters={"docstatus": 0, "status": ["in", ["Counted - Awaiting Approval", "Turned Over - Awaiting Review"]], "blind_count_confirmed": 1},
        pluck="name",
        limit_page_length=0,
    )
    for name in rows:
        if _upgrade_legacy_counted_shift(name):
            upgraded.append(name)
    return upgraded


@frappe.whitelist()
def preflight_guard():
    required = {
        SHIFT: ["company", "settlement_location", "cashier", "opening_cash", "total_cash_in", "total_cash_out", "total_non_cash_in", "total_non_cash_out", "expected_cash", "actual_cash_count", "over_short"],
        MOVEMENT: ["cashier_shift", "cashier", "movement_type", "direction", "payment_method", "amount", "status"],
    }
    errors = []
    for doctype, fields in required.items():
        if not frappe.db.exists("DocType", doctype):
            errors.append(f"Missing DocType: {doctype}")
            continue
        meta = frappe.get_meta(doctype)
        missing = [field for field in fields if not meta.has_field(field)]
        if missing:
            errors.append(f"{doctype} missing fields: {missing}")
    try:
        from nkt_operations.nkt_store_operations.features.payments_accounts.receivables import verify_v1_8
        baseline = verify_v1_8()
        if not baseline.get("passed"):
            errors.append("V1.8 verification did not pass")
    except Exception as exc:
        errors.append(f"V1.8 baseline verification failed: {type(exc).__name__}: {exc}")
    if errors:
        frappe.throw(_("V1.9 preflight failed: {0}").format(json.dumps(errors, indent=2)))
    return {"passed": True, "version": VERSION, "required_doctypes": list(required)}


@frappe.whitelist()
def get_available_drawer_cash(shift_name):
    shift = frappe.get_doc(SHIFT, shift_name)
    _assert_shift_access(shift)
    _assert_open_shift(shift)
    summary = calculate_shift_summary(shift.name)
    return {"shift": shift.name, "expected_cash": flt(summary["expected_cash"]), "movement_count": cint(summary["movement_count"])}


def _advance_deposit_diagnostics():
    negative_open_shifts = []
    for row in frappe.get_all(SHIFT, filters={"docstatus": 0, "status": "Open"}, fields=["name", "cashier"], limit_page_length=0):
        summary = calculate_shift_summary(row.name)
        if flt(summary["expected_cash"]) < -TOLERANCE:
            negative_open_shifts.append({"shift": row.name, "cashier": row.cashier, "expected_cash": flt(summary["expected_cash"]), "movement_count": cint(summary["movement_count"])})
    posted = frappe.get_all(
        ADJUSTMENT,
        filters={"docstatus": 1, "status": "Posted", "adjustment_type": "Advance / Mid-Shift Deposit"},
        fields=["name", "cashier_shift", "cashier", "amount", "cashier_movement", "creation"],
        order_by="creation desc", limit_page_length=20,
    ) if frappe.db.exists("DocType", ADJUSTMENT) else []
    return {"negative_open_shifts": negative_open_shifts, "recent_posted_advance_deposits": posted}


@frappe.whitelist()
def install_v1_9_6():
    frappe.set_user("Administrator")
    if not frappe.db.exists("DocType", ADJUSTMENT):
        frappe.throw(_("NKT Cash Drawer Adjustment is missing. Install V1.9 first."))
    desired = _adjustment_fields()
    desired_order = [row["fieldname"] for row in desired]
    desired_props = {row["fieldname"]: row for row in desired}
    existing_rows = frappe.get_all("DocField", filters={"parent": ADJUSTMENT, "parenttype": "DocType"}, fields=["name", "fieldname"], order_by="idx asc", limit_page_length=0)
    row_by_field = {row.fieldname: row.name for row in existing_rows}
    changed = []
    for idx, fieldname in enumerate(desired_order, start=1):
        rowname = row_by_field.get(fieldname)
        if not rowname:
            continue
        spec = desired_props[fieldname]
        updates = {"idx": idx}
        for key in ("label", "reqd", "hidden", "read_only", "depends_on"):
            updates[key] = spec.get(key, 0 if key in {"reqd", "hidden", "read_only"} else None)
        frappe.db.set_value("DocField", rowname, updates, update_modified=False)
        changed.append(fieldname)
    _ensure_client_script("NKT Cash Drawer Adjustment V1.9", ADJUSTMENT, ADJUSTMENT_CLIENT_SCRIPT)
    frappe.clear_cache(doctype=ADJUSTMENT)
    frappe.clear_cache()
    diagnostics = _advance_deposit_diagnostics()
    frappe.db.commit()
    return {
        "installed": True, "version": VERSION, "changed_adjustment_fields": changed,
        "advance_deposit_form": "Shift, type, denominations, calculated total, optional remarks",
        "zero_amount_policy": "Clean validation message; no server TypeError",
        "negative_cash_policy": "Deposit cannot exceed current expected drawer cash",
        "existing_data_diagnostics": diagnostics, "posted_records_changed": False,
    }


@frappe.whitelist()
def verify_v1_9_6():
    import inspect
    errors = []
    validate_source = inspect.getsource(validate_adjustment)
    submit_source = inspect.getsource(before_submit_adjustment)
    if "deposit_total, _" in validate_source:
        errors.append("Translation helper is still shadowed by a denomination dictionary")
    if "exceeds the current expected drawer cash" not in submit_source:
        errors.append("Server-side deposit-versus-available-cash guard is missing")
    fields = {row.fieldname: row for row in frappe.get_all("DocField", filters={"parent": ADJUSTMENT, "parenttype": "DocType"}, fields=["fieldname", "label", "reqd", "hidden", "idx"], limit_page_length=0)}
    for fieldname in ("deposit_destination", "deposit_reference_number"):
        if not fields.get(fieldname) or not cint(fields[fieldname].hidden):
            errors.append(f"Legacy field should be hidden: {fieldname}")
    purpose = fields.get("purpose")
    if not purpose or cint(purpose.reqd):
        errors.append("Remarks must be optional at schema level for fast advance deposits")
    script = frappe.db.get_value("Client Script", "NKT Cash Drawer Adjustment V1.9", "script") or ""
    if "NKT Shift Close V1.9.6" not in script:
        errors.append("V1.9.6 adjustment client script is not active")
    if "Expected drawer cash before deposit" not in script:
        errors.append("Available-cash feedback is missing from the adjustment client script")
    diagnostics = _advance_deposit_diagnostics()
    report = {"version": VERSION, "errors": errors, "passed": not errors, "diagnostics": diagnostics, "posted_records_changed": False}
    if errors:
        frappe.throw(_("V1.9.6 verification failed: {0}").format(json.dumps(report, indent=2, default=str)))
    return report


@frappe.whitelist()
def install():
    frappe.set_user("Administrator")
    preflight_guard()

    installed_fields = _ensure_shift_fields()
    _ensure_property_setter(SHIFT, "blind_count_confirmed", "label", "Closing Count Finalized")
    _ensure_property_setter(SHIFT, "count_section", "label", "Closing Count Draft and Cashier Close")
    _ensure_property_setter(SHIFT, "approval_section", "label", "Owner / Administrator Reconciliation Review")
    _ensure_property_setter(SHIFT, "status", "options", SHIFT_STATUS_OPTIONS, "Text")
    _ensure_property_setter(SHIFT, "turnover_status", "options", TURNOVER_STATUS_OPTIONS, "Text")
    _ensure_property_setter(MOVEMENT, "movement_type", "options", MOVEMENT_TYPE_OPTIONS, "Text")

    admin_rows = _admin_permissions()
    _ensure_custom_doctype(
        ADJUSTMENT,
        "NKT-CDA-.#####",
        _adjustment_fields(),
        admin_rows + [_perm(CASHIER_ROLE, read=1, write=1, create=1, submit=1, print_=1)],
        is_submittable=True,
    )
    _ensure_custom_doctype(
        CONTROL_LOG,
        "NKT-SHIFT-LOG-.#####",
        _control_log_fields(),
        admin_rows,
        is_submittable=False,
    )

    _replace_permissions(
        SHIFT,
        admin_rows + [_perm(CASHIER_ROLE, read=1, write=1, create=1, print_=1)],
    )
    _replace_permissions(
        MOVEMENT,
        admin_rows + [_perm(CASHIER_ROLE, read=1, print_=1)],
    )
    _replace_permissions(
        ADJUSTMENT,
        admin_rows + [_perm(CASHIER_ROLE, read=1, write=1, create=1, submit=1, print_=1)],
    )
    _replace_permissions(CONTROL_LOG, admin_rows)

    _ensure_client_script("NKT Cashier Shift Controls V1.9", SHIFT, SHIFT_CLIENT_SCRIPT)
    _ensure_client_script("NKT Cash Drawer Adjustment V1.9", ADJUSTMENT, ADJUSTMENT_CLIENT_SCRIPT)
    _ensure_print_format("NKT Cashier Shift Reconciliation", SHIFT, SHIFT_PRINT_FORMAT)
    _ensure_print_format("NKT Cash Disbursement Voucher", ADJUSTMENT, ADJUSTMENT_PRINT_FORMAT)
    workspace = _ensure_workspace()
    quarantined = _quarantine_invalid_open_shifts()
    upgraded_legacy_counted_shifts = _upgrade_legacy_counted_shifts()
    refreshed = _refresh_open_shifts()

    frappe.clear_cache()
    frappe.db.commit()
    return {
        "installed": True,
        "version": VERSION,
        "installed_shift_fields": installed_fields,
        "workspace": workspace,
        "quarantined_invalid_role_shifts": quarantined,
        "upgraded_legacy_counted_shifts": upgraded_legacy_counted_shifts,
        "refreshed_open_shifts": refreshed,
        "cashier_visibility": "Own shifts, own movements, own User record; Cashier saves count drafts and closes own shift",
        "encoder_visibility": "No cashier shifts, no cashier movements, own User record only",
        "review_workflow": "Cashier Closed - Awaiting Review, then Owner/Administrator reconciliation review",
        "advance_deposit_control": "Separate denomination-based Advance / Mid-Shift Deposit cash-out movement",
        "gl_posting_enabled": False,
        "workflow_changes": True,
    }


def _effective_access(doctype, role):
    result = {key: 0 for key in ("read", "write", "create", "delete", "submit", "cancel", "amend", "report", "export", "print", "email", "share")}
    for row in frappe.get_meta(doctype).permissions:
        if row.role != role:
            continue
        for key in result:
            result[key] = max(result[key], cint(row.get(key)))
    return result


@frappe.whitelist()
def verify_v1_9():
    errors = []
    missing_doctypes = [doctype for doctype in (ADJUSTMENT, CONTROL_LOG) if not frappe.db.exists("DocType", doctype)]
    required_shift_fields = [
        "custom_nkt_cash_sales", "custom_nkt_cash_account_collections", "custom_nkt_cash_drops", "custom_nkt_advance_deposits",
        "custom_nkt_count_draft_total", "custom_nkt_count_draft_saved_on", "custom_nkt_cashier_closed_by",
        "custom_nkt_expected_cash_at_count", "custom_nkt_movement_count_at_count",
        "custom_nkt_v19_invalid_role_shift", "custom_nkt_v19_version",
    ]
    missing_fields = [field for field in required_shift_fields if not _field_exists(SHIFT, field)]
    if missing_doctypes:
        errors.append(f"Missing DocTypes: {missing_doctypes}")
    if missing_fields:
        errors.append(f"Missing Shift fields: {missing_fields}")

    status_options = (frappe.get_meta(SHIFT).get_field("status").options or "") if frappe.get_meta(SHIFT).get_field("status") else ""
    movement_options = (frappe.get_meta(MOVEMENT).get_field("movement_type").options or "") if frappe.get_meta(MOVEMENT).get_field("movement_type") else ""
    if CASHIER_CLOSED_STATUS not in status_options:
        errors.append(f"Cashier-close status missing: {status_options}")
    if "Advance / Mid-Shift Deposit" not in movement_options:
        errors.append(f"Advance-deposit movement type missing: {movement_options}")
    adjustment_meta = frappe.get_meta(ADJUSTMENT) if frappe.db.exists("DocType", ADJUSTMENT) else None
    required_adjustment_fields = ["deposit_destination", "deposit_reference_number", "deposit_denomination_total", "deposit_bill_1000_qty", "deposit_coin_025_qty"]
    missing_adjustment_fields = [field for field in required_adjustment_fields if not adjustment_meta or not adjustment_meta.has_field(field)]
    if missing_adjustment_fields:
        errors.append(f"Advance-deposit fields missing: {missing_adjustment_fields}")

    cashier_shift = _effective_access(SHIFT, CASHIER_ROLE)
    encoder_shift = _effective_access(SHIFT, ENCODER_ROLE)
    cashier_movement = _effective_access(MOVEMENT, CASHIER_ROLE)
    encoder_movement = _effective_access(MOVEMENT, ENCODER_ROLE)
    cashier_adjustment = _effective_access(ADJUSTMENT, CASHIER_ROLE) if not missing_doctypes else {}

    if cashier_shift.get("read") != 1 or cashier_shift.get("write") != 1 or cashier_shift.get("create") != 1:
        errors.append(f"Cashier shift permissions incomplete: {cashier_shift}")
    if any(cashier_shift.get(key) for key in ("delete", "submit", "cancel", "share", "export", "email")):
        errors.append(f"Cashier shift permissions too broad: {cashier_shift}")
    if any(encoder_shift.get(key) for key in encoder_shift):
        errors.append(f"Encoder must have no Cashier Shift access: {encoder_shift}")
    if cashier_movement.get("read") != 1 or cashier_movement.get("print") != 1 or any(cashier_movement.get(key) for key in ("write", "create", "delete", "submit", "cancel", "share")):
        errors.append(f"Cashier movement permissions unsafe: {cashier_movement}")
    if any(encoder_movement.get(key) for key in encoder_movement):
        errors.append(f"Encoder must have no Cashier Movement access: {encoder_movement}")
    if cashier_adjustment and not (cashier_adjustment.get("read") and cashier_adjustment.get("write") and cashier_adjustment.get("create") and cashier_adjustment.get("submit")):
        errors.append(f"Cashier adjustment permissions incomplete: {cashier_adjustment}")
    if cashier_adjustment and any(cashier_adjustment.get(key) for key in ("delete", "cancel", "share", "export", "email")):
        errors.append(f"Cashier adjustment permissions too broad: {cashier_adjustment}")

    invalid_open = []
    for row in frappe.get_all(SHIFT, filters={"docstatus": 0, "status": "Open"}, fields=["name", "cashier"]):
        if not _cashier_is_valid_operator(row.cashier):
            invalid_open.append(row)
    if invalid_open:
        errors.append(f"Invalid-role open shifts remain: {invalid_open}")

    workspace = frappe.db.get_value("Workspace", WORKSPACE, ["public", "is_hidden", "module"], as_dict=True)
    workspace_roles = set(frappe.get_all("Has Role", filters={"parenttype": "Workspace", "parent": WORKSPACE}, pluck="role")) if workspace else set()
    if not workspace or not cint(workspace.public) or cint(workspace.is_hidden) or workspace.module != MODULE:
        errors.append(f"Cashier workspace missing or hidden: {workspace}")
    if ENCODER_ROLE in workspace_roles or CASHIER_ROLE not in workspace_roles:
        errors.append(f"Cashier workspace roles incorrect: {sorted(workspace_roles)}")

    scripts = frappe.get_all("Client Script", filters={"name": ["in", ["NKT Cashier Shift Controls V1.9", "NKT Cash Drawer Adjustment V1.9"]], "enabled": 1}, pluck="name")
    formats = frappe.get_all("Print Format", filters={"name": ["in", ["NKT Cashier Shift Reconciliation", "NKT Cash Disbursement Voucher"]], "disabled": 0}, pluck="name")
    if len(scripts) != 2:
        errors.append(f"V1.9 Client Scripts incomplete: {scripts}")
    if len(formats) != 2:
        errors.append(f"V1.9 Print Formats incomplete: {formats}")

    legacy_incomplete = frappe.get_all(
        SHIFT,
        filters={"docstatus": 0, "status": ["in", ["Counted - Awaiting Approval", "Turned Over - Awaiting Review"]], "blind_count_confirmed": 1},
        fields=["name", "status", "cashier", "count_locked_by", "count_locked_on"],
        limit_page_length=0,
    )
    if legacy_incomplete:
        errors.append(f"Legacy counted shifts were not upgraded: {legacy_incomplete}")

    script_source = frappe.db.get_value("Client Script", "NKT Cashier Shift Controls V1.9", "script") or ""
    if "NKT Shift Close V1.9.5" not in script_source:
        errors.append("V1.9.5 Cashier Shift client-script marker is missing")
    import inspect
    review_source = inspect.getsource(approve_and_close_shift)
    if "shift.flags.ignore_validate = True" not in review_source:
        errors.append("V1.9.5 legacy-controller review compatibility bridge is missing")

    report = {
        "version": VERSION,
        "missing_doctypes": missing_doctypes,
        "missing_fields": missing_fields,
        "missing_advance_deposit_fields": missing_adjustment_fields,
        "permissions": {
            "cashier_shift": cashier_shift,
            "encoder_shift": encoder_shift,
            "cashier_movement": cashier_movement,
            "encoder_movement": encoder_movement,
            "cashier_adjustment": cashier_adjustment,
        },
        "invalid_role_open_shifts": invalid_open,
        "legacy_counted_shifts_remaining": legacy_incomplete,
        "workspace_roles": sorted(workspace_roles),
        "client_scripts": scripts,
        "print_formats": formats,
        "user_directory_policy": "NKT Cashier and NKT Encoder may read only their own User record",
        "cash_count_policy": "Cashier saves denomination drafts, sees totals, then finalizes and closes own shift; Encoder has no shift access",
        "review_policy": "Owner/Administrator reviews after Cashier Closed - Awaiting Review and may reopen only with a reason",
        "advance_deposit_policy": "Advance / Mid-Shift Deposit is denomination-based, draft-saveable, and separate from drawings/petty cash",
        "gl_posting_enabled": False,
        "errors": errors,
        "passed": not errors,
    }
    if errors:
        frappe.throw(_("V1.9 verification failed: {0}").format(json.dumps(report, indent=2, default=str)))
    return report
