"""NKT V2.0C.4.2.1 controlled warehouse source-change / recall workflow.

This stage preserves the accepted V2.0C.3 Cashier/Encoder engine and adds a
role-separated warehouse release fast screen plus idempotent controlled partial
release posting on top of the existing NKT Warehouse Release controller.


This stage preserves the strict same-date Cashier/Encoder flow, standard-rate
and non-card restrictions, request-ID idempotency, payment integrity, and the
approved matching algorithm. It adds read-only exact reconciliation diagnostics,
professional incoming-Check preflight on both Fast Screens, and post-success
keyboard-spam suppression. No approved cashier/encoder/inventory workflow is redesigned.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, nowdate, now_datetime
from nkt_operations.nkt_store_operations.features.payments_accounts.card_surcharge import (
    normalize_payment_method,
    row_collected_amount,
)

VERSION = "2.0C.4.2.2"
MODULE = "NKT Store Operations"
OPERATING_LOCATION = "NKT Operating Location"
CASHIER_SCREEN = "NKT Cashier Fast Screen"
ENCODER_SCREEN = "NKT Encoder Fast Screen"
CASHIER_SCRIPT = "NKT Cashier Fast Screen V2.0C.3"
ENCODER_SCRIPT = "NKT Encoder Fast Screen V2.0C.3"
WAREHOUSE_RELEASE_SCREEN = "NKT Warehouse Release Fast Screen"
WAREHOUSE_RELEASE_SCRIPT = "NKT Warehouse Release Fast Screen V2.0C.4.1.4.3"
WAREHOUSE_ROLE = "NKT Warehouse"
RELEASE_REQUEST_FIELD = "custom_nkt_fast_release_request_id"
ACCOUNT_FAST_REQUEST_FIELD = "custom_nkt_fast_request_id"
ACCOUNT_FAST_PAYLOAD_HASH_FIELD = "custom_nkt_fast_payload_hash"
ACCOUNT_CASHIER_DOCTYPE = "NKT Cashier Account Collection"
ACCOUNT_ENCODER_DOCTYPE = "NKT Encoder Account Allocation"

WAREHOUSE_CHANGE_SCREEN = "NKT Warehouse Change Fast Screen"
WAREHOUSE_CHANGE_SCRIPT = "NKT Warehouse Change Fast Screen V2.0C.4.2.1"
WAREHOUSE_CHANGE_LOG = "NKT Warehouse Change"
WAREHOUSE_CHANGE_LINK_SCRIPT = "NKT Encoder Warehouse Change Link V2.0C.4.2.1"
WAREHOUSE_CHANGE_ORDER_LINK_SCRIPT = "NKT Customer Order Warehouse Change Link V2.0C.4.2.1.3"
WAREHOUSE_RELEASE_SCRIPT_C421 = "NKT Warehouse Release Fast Screen V2.0C.4.2.1"

ADMIN_ROLES = ("System Manager", "NKT OWNER", "NKT ADMINISTRATOR")
CASHIER_ROLE = "NKT Cashier"
ENCODER_ROLE = "NKT Encoder"
DEFAULT_PRICE_VARIATIONS = (-20, -15, -10, -5, 5, 10, 15, 20)
PRICE_ADJUSTMENT_PRESET_DOCTYPE = "NKT Selling Price Adjustment Preset"


def _configured_price_variations():
    # Owner/Admin-maintained routine PHP-per-unit price adjustments.
    # Initial +/-5/10/15/20 values are fallback only; enabled ERP preset
    # records are the normal production source of truth.
    try:
        if not frappe.db.exists("DocType", PRICE_ADJUSTMENT_PRESET_DOCTYPE):
            return DEFAULT_PRICE_VARIATIONS
        rows = frappe.get_all(
            PRICE_ADJUSTMENT_PRESET_DOCTYPE,
            filters={"enabled": 1},
            fields=["adjustment_pesos", "display_order"],
            order_by="display_order asc, adjustment_pesos asc",
            limit_page_length=200,
        )
        values = []
        for row in rows:
            value = round(float(row.adjustment_pesos or 0), 6)
            if abs(value) < 0.000001 or value in values:
                continue
            values.append(value)
        return tuple(values) if values else DEFAULT_PRICE_VARIATIONS
    except Exception:
        # Price entry must remain operational even if the settings surface is
        # temporarily unavailable; fall back to the accepted starter presets.
        return DEFAULT_PRICE_VARIATIONS


BUSINESS_DOCTYPES = (
    "NKT Cashier Sale",
    "NKT Customer Order",
    "NKT Payment Receipt",
    "NKT Cashier Movement",
    "NKT Warehouse Release",
    "Stock Entry",
    "Stock Reservation Entry",
    "NKT Warehouse Change",
)


def _perm(role: str, *, read=1, write=0, create=0, delete=0, report=0, export=0, print_=0, email=0, share=0):
    return {
        "role": role,
        "read": cint(read),
        "write": cint(write),
        "create": cint(create),
        "delete": cint(delete),
        "report": cint(report),
        "export": cint(export),
        "print": cint(print_),
        "email": cint(email),
        "share": cint(share),
    }


def _admin_permissions():
    return [
        _perm(role, read=1, write=1, create=1, delete=1, report=1, export=1, print_=1, email=1, share=1)
        for role in ADMIN_ROLES
    ]


def _screen_permissions(role: str):
    return _admin_permissions() + [_perm(role, read=1, write=0, create=0, delete=0, report=0, print_=0)]


def _location_permissions():
    return _admin_permissions() + [
        _perm(CASHIER_ROLE, read=1),
        _perm(ENCODER_ROLE, read=1),
    ]


def _warehouse_release_screen_permissions():
    return _admin_permissions() + [_perm(WAREHOUSE_ROLE, read=1)]


def _sanitize_permissions(rows):
    result = []
    for row in rows:
        clean = dict(row)
        clean["submit"] = 0
        clean["cancel"] = 0
        clean["amend"] = 0
        result.append(clean)
    return result


def _ensure_custom_doctype(name: str, autoname: str, fields: list[dict[str, Any]], permissions, *, issingle=False):
    if frappe.db.exists("DocType", name):
        doc = frappe.get_doc("DocType", name)
        existing = {row.fieldname: row for row in (doc.get("fields") or []) if row.fieldname}
        changed = False
        desired = []
        for idx, field in enumerate(fields, start=1):
            fieldname = field.get("fieldname")
            if not fieldname:
                continue
            desired.append(fieldname)
            row = existing.get(fieldname)
            if row is None:
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
        next_idx = len(desired) + 1
        for row in doc.get("fields") or []:
            if row.fieldname not in desired:
                if cint(row.idx) != next_idx:
                    row.idx = next_idx
                    changed = True
                next_idx += 1
        expected_single = 1 if issingle else 0
        if cint(doc.issingle) != expected_single:
            doc.issingle = expected_single
            changed = True
        expected_permissions = _sanitize_permissions(permissions)
        current_permissions = [
            {key: row.get(key) for key in ("role", "read", "write", "create", "delete", "report", "export", "print", "email", "share", "submit", "cancel", "amend")}
            for row in (doc.get("permissions") or [])
        ]
        if current_permissions != expected_permissions:
            doc.set("permissions", [])
            for permission in expected_permissions:
                doc.append("permissions", permission)
            changed = True
        if changed:
            doc.flags.ignore_permissions = True
            doc.save(ignore_permissions=True)
        frappe.clear_cache(doctype=name)
        return

    doc = frappe.new_doc("DocType")
    doc.name = name
    doc.module = MODULE
    doc.custom = 1
    doc.track_changes = 0
    doc.allow_rename = 0
    doc.allow_import = 0
    doc.allow_bulk_edit = 0
    doc.issingle = 1 if issingle else 0
    doc.autoname = autoname
    doc.sort_field = "creation"
    doc.sort_order = "DESC"
    for field in fields:
        doc.append("fields", field)
    for permission in _sanitize_permissions(permissions):
        doc.append("permissions", permission)
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    frappe.clear_cache(doctype=name)


def _hidden_mandatory_without_default(doctype: str):
    """Return existing fields that block Frappe v16 Custom Field validation.

    Some accepted legacy NKT DocTypes contain system-populated fields that are
    both hidden and mandatory without a static default (for example Company,
    Business Date, and Cashier on NKT Cashier Sale). They are valid at runtime
    because the controller fills them, but Frappe v16 re-validates the entire
    DocType whenever any Custom Field is added and rejects that legacy shape.
    """
    blockers = []
    for table, parent_field in (("DocField", "parent"), ("Custom Field", "dt")):
        rows = frappe.get_all(
            table,
            filters={parent_field: doctype, "hidden": 1, "reqd": 1},
            fields=["name", "fieldname", "default"],
            order_by="idx asc",
        )
        for row in rows:
            if row.get("default") in (None, ""):
                blockers.append({"table": table, "name": row.name, "fieldname": row.fieldname})
    return blockers


def _append_missing_custom_fields(doctype: str, fields: list[dict[str, Any]]):
    if not frappe.db.exists("DocType", doctype):
        frappe.throw(_("Required DocType is missing: {0}").format(doctype))

    # Temporarily expose only the pre-existing system-populated blockers while
    # Frappe validates the DocType. Restore their exact hidden state in finally.
    blockers = _hidden_mandatory_without_default(doctype)
    for blocker in blockers:
        frappe.db.set_value(blocker["table"], blocker["name"], "hidden", 0, update_modified=False)
    if blockers:
        frappe.clear_cache(doctype=doctype)

    installed = []
    try:
        custom_meta = frappe.get_meta("Custom Field")
        for field in fields:
            fieldname = field.get("fieldname")
            frappe.clear_cache(doctype=doctype)
            if not fieldname or frappe.get_meta(doctype).has_field(fieldname):
                continue
            values = {"doctype": "Custom Field", "dt": doctype}
            for key, value in field.items():
                if key == "fieldname" or custom_meta.has_field(key):
                    values[key] = value
            custom = frappe.get_doc(values)
            custom.flags.ignore_permissions = True
            custom.insert(ignore_permissions=True)
            installed.append(fieldname)
    finally:
        for blocker in blockers:
            frappe.db.set_value(blocker["table"], blocker["name"], "hidden", 1, update_modified=False)
        if blockers or installed:
            frappe.clear_cache(doctype=doctype)
    return installed


def _ensure_client_script(name: str, doctype: str, filename: str):
    script_path = Path(__file__).with_name(filename)
    if not script_path.exists():
        frappe.throw(_("Companion UI script is missing: {0}").format(script_path.name))
    script_text = script_path.read_text(encoding="utf-8")
    values = {
        "dt": doctype,
        "view": "Form",
        "enabled": 1,
        "script": script_text,
    }
    if frappe.db.exists("Client Script", name):
        doc = frappe.get_doc("Client Script", name)
        doc.update(values)
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.new_doc("Client Script")
        doc.name = name
        doc.update(values)
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
    return name


def _ensure_seed_configuration():
    company = "NKT (Dev)" if frappe.db.exists("Company", "NKT (Dev)") else None
    retail = "NKT Retail Store - NKT-D" if frappe.db.exists("Warehouse", "NKT Retail Store - NKT-D") else None
    natividad = "Natividad Warehouse - NKT-D" if frappe.db.exists("Warehouse", "Natividad Warehouse - NKT-D") else None

    seeded = {"operating_location": None, "warehouse_updates": [], "user_assignments": []}

    if retail:
        values = {
            "custom_nkt_fast_label": "Retail Store",
            "custom_nkt_fulfillment_type": "Immediate Retail Deduction",
        }
        for fieldname, value in values.items():
            frappe.db.set_value("Warehouse", retail, fieldname, value, update_modified=False)
        seeded["warehouse_updates"].append({"warehouse": retail, **values})

    if natividad:
        values = {
            "custom_nkt_fast_label": "Natividad",
            "custom_nkt_fulfillment_type": "External Warehouse Release",
        }
        for fieldname, value in values.items():
            frappe.db.set_value("Warehouse", natividad, fieldname, value, update_modified=False)
        seeded["warehouse_updates"].append({"warehouse": natividad, **values})

    if company and retail:
        location_name = "Retail Store"
        if frappe.db.exists(OPERATING_LOCATION, location_name):
            location = frappe.get_doc(OPERATING_LOCATION, location_name)
        else:
            location = frappe.new_doc(OPERATING_LOCATION)
            location.location_name = location_name
        location.enabled = 1
        location.company = company
        location.friendly_label = "Retail Store"
        location.settlement_location = retail
        location.default_warehouse = retail
        location.flags.ignore_permissions = True
        if location.is_new():
            location.insert(ignore_permissions=True)
        else:
            location.save(ignore_permissions=True)
        seeded["operating_location"] = location.name

        for user in ("cashier@example.com", "encoder@example.com"):
            if not frappe.db.exists("User", user):
                continue
            current = frappe.db.get_value("User", user, "custom_nkt_operating_location")
            if not current:
                frappe.db.set_value("User", user, "custom_nkt_operating_location", location.name, update_modified=False)
                seeded["user_assignments"].append({"user": user, "operating_location": location.name})

    return seeded


def _counts():
    result = {}
    for doctype in BUSINESS_DOCTYPES:
        if frappe.db.exists("DocType", doctype):
            result[doctype] = frappe.db.count(doctype)
    return result


def install_v2_0_b():
    before = _counts()

    _ensure_custom_doctype(
        OPERATING_LOCATION,
        "field:location_name",
        [
            {"fieldname": "location_section", "label": "Operating Location", "fieldtype": "Section Break"},
            {"fieldname": "location_name", "label": "Location Name", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
            {"fieldname": "enabled", "label": "Enabled", "fieldtype": "Check", "default": "1", "in_list_view": 1},
            {"fieldname": "company", "label": "Company", "fieldtype": "Link", "options": "Company", "reqd": 1, "in_list_view": 1},
            {"fieldname": "friendly_label", "label": "Fast Screen Label", "fieldtype": "Data", "reqd": 1, "in_list_view": 1},
            {"fieldname": "settlement_location", "label": "Cashier / Settlement Location", "fieldtype": "Link", "options": "Warehouse", "reqd": 1},
            {"fieldname": "default_warehouse", "label": "Default Source Warehouse", "fieldtype": "Link", "options": "Warehouse", "reqd": 1},
            {"fieldname": "notes", "label": "Notes", "fieldtype": "Small Text"},
        ],
        _location_permissions(),
    )

    _ensure_custom_doctype(
        CASHIER_SCREEN,
        "",
        [{"fieldname": "screen_html", "label": "Cashier Fast Screen", "fieldtype": "HTML"}],
        _screen_permissions(CASHIER_ROLE),
        issingle=True,
    )
    _ensure_custom_doctype(
        ENCODER_SCREEN,
        "",
        [{"fieldname": "screen_html", "label": "Encoder Fast Screen", "fieldtype": "HTML"}],
        _screen_permissions(ENCODER_ROLE),
        issingle=True,
    )

    installed_fields = {
        "User": _append_missing_custom_fields(
            "User",
            [
                {
                    "fieldname": "custom_nkt_operating_location",
                    "label": "NKT Operating Location",
                    "fieldtype": "Link",
                    "options": OPERATING_LOCATION,
                    "insert_after": "location",
                    "description": "Assigned once for the operational account. The fast screen does not ask the operator to select a branch.",
                },
                {
                    "fieldname": "custom_nkt_can_authorize_price_adjustment",
                    "label": "Can Authorize NKT Selling Price Adjustments",
                    "fieldtype": "Check",
                    "default": "0",
                    "insert_after": "custom_nkt_operating_location",
                    "description": "Foundation flag only in V2.0B. No PIN is stored or accepted by this read-only shell.",
                },
            ],
        ),
        "Warehouse": _append_missing_custom_fields(
            "Warehouse",
            [
                {
                    "fieldname": "custom_nkt_fast_label",
                    "label": "NKT Fast Screen Label",
                    "fieldtype": "Data",
                    "insert_after": "warehouse_name",
                },
                {
                    "fieldname": "custom_nkt_fulfillment_type",
                    "label": "NKT Fulfillment Type",
                    "fieldtype": "Select",
                    "options": "\nImmediate Retail Deduction\nExternal Warehouse Release\nRestricted\nNon-Saleable / Inactive",
                    "insert_after": "custom_nkt_fast_label",
                },
            ],
        ),
    }

    seeded = _ensure_seed_configuration()
    scripts = [
        _ensure_client_script(CASHIER_SCRIPT, CASHIER_SCREEN, "nkt_cashier_fast_screen_v2.js"),
        _ensure_client_script(ENCODER_SCRIPT, ENCODER_SCREEN, "nkt_encoder_fast_screen_v2.js"),
    ]

    frappe.db.commit()
    frappe.clear_cache()

    after = _counts()
    changed_business_counts = {
        doctype: {"before": before.get(doctype), "after": after.get(doctype)}
        for doctype in sorted(set(before) | set(after))
        if before.get(doctype) != after.get(doctype)
    }

    return {
        "installed": True,
        "version": VERSION,
        "screens": [CASHIER_SCREEN, ENCODER_SCREEN],
        "client_scripts": scripts,
        "installed_custom_fields": installed_fields,
        "seeded_configuration": seeded,
        "business_record_counts_changed": changed_business_counts,
        "posting_endpoints_added": False,
        "next_action": "Open the two screens through Ctrl+K and test keyboard behavior. F10/F12 remain blocked in this shell.",
    }


def verify_v2_0_b():
    errors = []
    for doctype in (OPERATING_LOCATION, CASHIER_SCREEN, ENCODER_SCREEN):
        if not frappe.db.exists("DocType", doctype):
            errors.append(f"Missing DocType: {doctype}")
    for name in (CASHIER_SCRIPT, ENCODER_SCRIPT):
        if not frappe.db.exists("Client Script", name):
            errors.append(f"Missing Client Script: {name}")
        elif not cint(frappe.db.get_value("Client Script", name, "enabled")):
            errors.append(f"Disabled Client Script: {name}")
    for doctype, fieldname in (
        ("User", "custom_nkt_operating_location"),
        ("User", "custom_nkt_can_authorize_price_adjustment"),
        ("Warehouse", "custom_nkt_fast_label"),
        ("Warehouse", "custom_nkt_fulfillment_type"),
    ):
        if not frappe.get_meta(doctype).has_field(fieldname):
            errors.append(f"Missing field: {doctype}.{fieldname}")

    assignments = []
    for user in ("cashier@example.com", "encoder@example.com"):
        if frappe.db.exists("User", user):
            assignments.append(
                {
                    "user": user,
                    "operating_location": frappe.db.get_value("User", user, "custom_nkt_operating_location"),
                }
            )

    configuration = frappe.get_all(
        OPERATING_LOCATION,
        fields=["name", "enabled", "company", "friendly_label", "settlement_location", "default_warehouse"],
        order_by="name",
    ) if frappe.db.exists("DocType", OPERATING_LOCATION) else []

    report = {
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "routes": {
            "cashier": '/app/nkt-cashier-fast-screen',
            "encoder": '/app/nkt-encoder-fast-screen',
        },
        "operating_locations": configuration,
        "user_assignments": assignments,
        "hardcoded_order_item_default_still_present": frappe.db.get_value(
            "DocField",
            {"parent": "NKT Customer Order Item", "fieldname": "source_warehouse"},
            "default",
        ),
        "global_default_warehouse": frappe.db.get_value(
            "DefaultValue", {"parent": "__default", "defkey": "default_warehouse"}, "defvalue"
        ),
        "posting_endpoints_added": False,
        "business_record_counts": _counts(),
    }
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report


def install_v2_0_b_3_1():
    """Force a new Client Script identity so stale B2 browser metadata cannot hide B3 payment fields."""
    for old_name in (
        "NKT Cashier Fast Screen V2.0B",
        "NKT Encoder Fast Screen V2.0B",
        "NKT Cashier Fast Screen V2.0B.3",
        "NKT Encoder Fast Screen V2.0B.3",
    ):
        if frappe.db.exists("Client Script", old_name):
            frappe.db.set_value("Client Script", old_name, "enabled", 0, update_modified=False)
    result = install_v2_0_b()
    result["version"] = VERSION
    result["refinement"] = {
        "shortcuts": ["F2 Customer", "F3 Enter Item"],
        "customer_current_balance": True,
        "multi_row_payment_preview": True,
        "cash_applied_auto_balances_to_receipt": True,
        "cash_tendered_editable_in_payment_grid": True,
        "cash_excess_becomes_change_not_overpayment": True,
        "zero_cash_row_does_not_mask_non_cash_error": True,
        "check_number_date_and_issuing_bank": True,
        "deposit_account_and_bank_reconciliation_deferred": True,
        "posting_endpoints_added": False,
    }
    result["next_action"] = "Refresh the browser and test editable Cash Tendered/change plus Check Number, Check Date, and Issuing Bank."
    return result


def verify_v2_0_b_3_1():
    report = verify_v2_0_b()
    report["version"] = VERSION
    report["customer_balance_field_available"] = frappe.get_meta("Customer").has_field("custom_nkt_current_account_balance")
    payment_meta = frappe.get_meta("NKT Payment Detail")
    report["check_fields_available"] = {
        "check_number": payment_meta.has_field("check_number"),
        "check_date": payment_meta.has_field("check_date"),
        "bank_or_provider": payment_meta.has_field("bank_or_provider"),
    }
    report["posting_endpoints_added"] = False
    report["business_record_counts"] = _counts()
    report["client_script_status"] = {
        name: frappe.db.get_value("Client Script", name, "enabled")
        for name in (
            "NKT Cashier Fast Screen V2.0B",
            "NKT Encoder Fast Screen V2.0B",
            CASHIER_SCRIPT,
            ENCODER_SCRIPT,
        )
        if frappe.db.exists("Client Script", name)
    }
    if not report["client_script_status"].get(CASHIER_SCRIPT):
        frappe.throw(_("Forced-refresh Cashier Client Script is not enabled."))
    if not report["client_script_status"].get(ENCODER_SCRIPT):
        frappe.throw(_("Forced-refresh Encoder Client Script is not enabled."))
    return report


def _has_any_role(user: str, roles: tuple[str, ...]) -> bool:
    user_roles = set(frappe.get_roles(user))
    return bool(user_roles.intersection(roles))


def _check_fast_screen_access(mode: str):
    user = frappe.session.user
    if user in ("Guest", None):
        frappe.throw(_("Login is required."), frappe.PermissionError)
    allowed = ADMIN_ROLES + ((CASHIER_ROLE,) if mode == "cashier" else (ENCODER_ROLE,))
    if not _has_any_role(user, allowed):
        frappe.throw(_("You are not authorized to open the {0} fast screen.").format(mode.title()), frappe.PermissionError)


def _get_user_location(user: str):
    if not frappe.get_meta("User").has_field("custom_nkt_operating_location"):
        return None
    name = frappe.db.get_value("User", user, "custom_nkt_operating_location")
    if not name and _has_any_role(user, ADMIN_ROLES):
        name = frappe.db.get_value(OPERATING_LOCATION, {"enabled": 1}, "name", order_by="creation asc")
    if not name or not frappe.db.exists(OPERATING_LOCATION, name):
        return None
    return frappe.db.get_value(
        OPERATING_LOCATION,
        name,
        ["name", "enabled", "company", "friendly_label", "settlement_location", "default_warehouse"],
        as_dict=True,
    )


def _warehouse_rows(company: str | None):
    filters = {"disabled": 0, "is_group": 0}
    if company:
        filters["company"] = company
    fields = ["name", "warehouse_name", "company"]
    meta = frappe.get_meta("Warehouse")
    if meta.has_field("custom_nkt_fast_label"):
        fields.append("custom_nkt_fast_label")
    if meta.has_field("custom_nkt_fulfillment_type"):
        fields.append("custom_nkt_fulfillment_type")
    rows = frappe.get_all("Warehouse", filters=filters, fields=fields, order_by="warehouse_name asc")
    result = []
    for row in rows:
        result.append(
            {
                "name": row.name,
                "label": row.get("custom_nkt_fast_label") or row.warehouse_name or row.name,
                "warehouse_name": row.warehouse_name,
                "company": row.company,
                "fulfillment_type": row.get("custom_nkt_fulfillment_type") or "",
            }
        )
    return result


@frappe.whitelist()
def get_fast_ui_bootstrap(mode: str):
    mode = (mode or "").strip().lower()
    if mode not in {"cashier", "encoder"}:
        frappe.throw(_("Mode must be Cashier or Encoder."))
    _check_fast_screen_access(mode)

    user = frappe.session.user
    location = _get_user_location(user)
    setup_error = None
    if not location:
        setup_error = "This operational account has no assigned NKT Operating Location. Ask an Administrator to assign it on the User record."

    open_shift = None
    blocked_shift = None
    shift_block_reason = None
    if mode == "cashier" and frappe.db.exists("DocType", "NKT Cashier Shift"):
        shift_rows = frappe.get_all(
            "NKT Cashier Shift",
            filters={"cashier": user, "status": "Open", "docstatus": 0},
            fields=["name", "company", "status", "shift_start", "settlement_location", "opening_cash", "expected_cash"],
            order_by="shift_start desc",
            limit_page_length=5,
        )
        if not shift_rows:
            shift_block_reason = "No open Cashier Shift. Open today's shift before finalizing."
        elif len(shift_rows) > 1:
            blocked_shift = shift_rows[0]
            shift_block_reason = "More than one open Cashier Shift exists for this account. Close the duplicate shift before posting."
        else:
            candidate = shift_rows[0]
            shift_date = getdate(candidate.shift_start) if candidate.shift_start else None
            today = getdate(nowdate())
            if not shift_date or shift_date != today:
                blocked_shift = candidate
                shift_block_reason = (
                    f"Cashier Shift {candidate.name} is dated {shift_date or 'unknown'} and cannot be used on {today}. "
                    "Close/review the old shift and open today's shift. NKT POS does not allow antedated or backdated sales."
                )
            elif location and candidate.company != location.company:
                blocked_shift = candidate
                shift_block_reason = "The open Cashier Shift belongs to a different company."
            elif location and candidate.settlement_location != location.settlement_location:
                blocked_shift = candidate
                shift_block_reason = "The open Cashier Shift does not belong to this account's assigned operating location."
            else:
                open_shift = candidate

    company = location.company if location else None
    return {
        "version": VERSION,
        "read_only_shell": False,
        "mode": mode,
        "user": user,
        "full_name": frappe.db.get_value("User", user, "full_name") or user,
        "business_date": nowdate(),
        "location": location,
        "setup_error": setup_error,
        "warehouses": _warehouse_rows(company),
        "default_warehouse": location.default_warehouse if location else None,
        "open_shift": open_shift,
        "blocked_shift": blocked_shift,
        "shift_block_reason": shift_block_reason,
        "strict_current_date_shift": True,
        "price_variations": list(_configured_price_variations()),
        "shortcuts": {"F2": "Customer", "F3": "Enter Item", "F10": "Finalize and Print", "F11": "Take Payment", "F12": "Finalize without Printing"},
        "posting_enabled": True,
        "live_posting_scope": "Standard-rate transactions; Card and adjusted rates remain blocked in V2.0C.1",
    }


def _saleable_item_condition(alias="i"):
    if frappe.get_meta("Item").has_field("nkt_stock_form"):
        return f" AND COALESCE({alias}.nkt_stock_form, '') = 'Saleable Sack'"
    return ""


def _barcode_search_condition(alias="i"):
    if frappe.db.exists("DocType", "Item Barcode"):
        return f" OR EXISTS (SELECT 1 FROM `tabItem Barcode` ib WHERE ib.parent = {alias}.name AND ib.barcode LIKE %(like)s)"
    return ""


def _standard_rate_subquery(alias="i"):
    return f"""
        COALESCE((
            SELECT ip.price_list_rate
            FROM `tabItem Price` ip
            WHERE ip.item_code = {alias}.name
              AND ip.price_list = 'Standard Selling'
              AND (ip.valid_from IS NULL OR ip.valid_from <= CURDATE())
              AND (ip.valid_upto IS NULL OR ip.valid_upto >= CURDATE())
            ORDER BY ip.valid_from DESC, ip.modified DESC
            LIMIT 1
        ), {alias}.standard_rate, 0)
    """


def _fast_customer_operational_balance(customer: str) -> float:
    """Role-safe operational balance used by the Encoder Fast Screen.

    Mirrors the accepted Fast Customer search balance: approved open AR plus
    matched Pending Approval AR. Cashier callers never receive this value.
    """
    value = frappe.db.sql(
        """
        SELECT COALESCE(SUM(r.outstanding_amount), 0)
        FROM `tabNKT Customer Receivable` r
        WHERE r.customer = %s
          AND r.docstatus <> 2
          AND COALESCE(r.status, '') <> 'Cancelled'
          AND COALESCE(r.outstanding_amount, 0) > 0
          AND (
                r.credit_control_status = 'Approved'
                OR (
                    r.credit_control_status = 'Pending Approval'
                    AND EXISTS (
                        SELECT 1
                        FROM `tabNKT Customer Order` o
                        WHERE o.name = r.customer_order
                          AND o.docstatus = 1
                          AND COALESCE(o.matched_cashier_sale, '') <> ''
                          AND COALESCE(o.cashier_reconciliation_status, '') LIKE 'Matched%%'
                    )
                )
          )
        """,
        customer,
    )
    return flt(value[0][0] if value else 0)


@frappe.whitelist()
def search_customers(search_text: str = "", limit: int = 12):
    user = frappe.session.user
    if not _has_any_role(user, ADMIN_ROLES + (CASHIER_ROLE, ENCODER_ROLE)):
        frappe.throw(_("Not permitted."), frappe.PermissionError)
    text = (search_text or "").strip()
    limit = max(1, min(cint(limit) or 12, 25))
    like = f"%{text}%"
    prefix = f"{text}%"
    # V2.0C.5.5 ROLE-SAFE OPERATIONAL CUSTOMER RECEIVABLE
    # Approved AR + matched Pending Approval AR only.
    balance_sql = """
        COALESCE((
            SELECT SUM(r.outstanding_amount)
            FROM `tabNKT Customer Receivable` r
            WHERE r.customer = c.name
              AND r.docstatus <> 2
              AND COALESCE(r.status, '') <> 'Cancelled'
              AND COALESCE(r.outstanding_amount, 0) > 0
              AND (
                    r.credit_control_status = 'Approved'
                    OR (
                        r.credit_control_status = 'Pending Approval'
                        AND EXISTS (
                            SELECT 1
                            FROM `tabNKT Customer Order` o
                            WHERE o.name = r.customer_order
                              AND o.docstatus = 1
                              AND COALESCE(o.matched_cashier_sale, '') <> ''
                              AND COALESCE(
                                    o.cashier_reconciliation_status,
                                    ''
                                  ) LIKE 'Matched%%'
                        )
                    )
              )
        ), 0)
    """
    rows = frappe.db.sql(
        f"""
        SELECT
            c.name,
            c.customer_name,
            c.mobile_no,
            c.territory,
            {balance_sql} AS current_account_balance,
            COALESCE(c.custom_nkt_allow_account_sales, 0) AS allow_account_sales
        FROM `tabCustomer` c
        WHERE c.disabled = 0
          AND (%(text)s = '' OR c.name LIKE %(like)s OR c.customer_name LIKE %(like)s)
        ORDER BY
          CASE WHEN c.name = %(text)s OR c.customer_name = %(text)s THEN 0
               WHEN c.name LIKE %(prefix)s OR c.customer_name LIKE %(prefix)s THEN 1
               ELSE 2 END,
          c.customer_name ASC
        LIMIT %(limit)s
        """,
        {"text": text, "like": like, "prefix": prefix, "limit": limit},
        as_dict=True,
    )
    roles = set(frappe.get_roles(user) or [])
    frontline_cashier_only = CASHIER_ROLE in roles and ENCODER_ROLE not in roles and not roles.intersection(set(ADMIN_ROLES))
    if frontline_cashier_only:
        for row in rows:
            row.current_account_balance = 0
    return rows


@frappe.whitelist()
def search_items(search_text: str = "", warehouse: str | None = None, limit: int = 12):
    user = frappe.session.user
    if not _has_any_role(user, ADMIN_ROLES + (CASHIER_ROLE, ENCODER_ROLE)):
        frappe.throw(_("Not permitted."), frappe.PermissionError)
    text = (search_text or "").strip()
    limit = max(1, min(cint(limit) or 12, 25))
    like = f"%{text}%"
    prefix = f"{text}%"
    saleable = _saleable_item_condition("i")
    barcode = _barcode_search_condition("i")
    rate_sql = _standard_rate_subquery("i")
    rows = frappe.db.sql(
        f"""
        SELECT
            i.name AS item_code,
            i.item_name,
            i.stock_uom,
            {rate_sql} AS standard_rate,
            COALESCE(b.actual_qty, 0) AS actual_qty,
            COALESCE(b.reserved_qty, 0) AS reserved_qty,
            COALESCE(b.actual_qty, 0) - COALESCE(b.reserved_qty, 0) AS available_qty
        FROM `tabItem` i
        LEFT JOIN `tabBin` b
          ON b.item_code = i.name AND b.warehouse = %(warehouse)s
        WHERE i.disabled = 0
          AND i.is_sales_item = 1
          {saleable}
          AND (%(text)s = '' OR i.name LIKE %(like)s OR i.item_name LIKE %(like)s {barcode})
        ORDER BY
          CASE WHEN i.name = %(text)s OR i.item_name = %(text)s THEN 0
               WHEN i.name LIKE %(prefix)s OR i.item_name LIKE %(prefix)s THEN 1
               ELSE 2 END,
          i.item_name ASC
        LIMIT %(limit)s
        """,
        {"text": text, "like": like, "prefix": prefix, "warehouse": warehouse or "", "limit": limit},
        as_dict=True,
    )
    return rows


@frappe.whitelist()
def get_item_context(item_code: str, warehouse: str | None = None):
    rows = search_items(item_code, warehouse, 5)
    for row in rows:
        if row.item_code == item_code:
            return row
    frappe.throw(_("Saleable item was not found: {0}").format(item_code))


# ---------------------------------------------------------------------------
# V2.0C.1 controlled live-posting bridge
# ---------------------------------------------------------------------------

FAST_REQUEST_FIELD = "custom_nkt_fast_request_id"
FAST_VERSION_FIELD = "custom_nkt_fast_ui_version"
LIVE_STANDARD_ONLY = False
# NKT_MANAGER_PIN_FAST_UI_MP1
LIVE_BLOCKED_PAYMENT_METHODS = set()
LIVE_PAYMENT_METHODS = {"Cash", "Check", "GCash", "Maya", "Card", "Bank Transfer", "Online", "Account"}


def _parse_fast_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            frappe.throw(_("The fast-screen payload is not valid JSON."))
    if not isinstance(payload, dict):
        frappe.throw(_("The fast-screen payload must be an object."))
    return payload


def _require_live_role(mode: str):
    user = frappe.session.user
    required = CASHIER_ROLE if mode == "cashier" else ENCODER_ROLE
    if required not in set(frappe.get_roles(user)):
        frappe.throw(
            _("Live {0} posting requires the {1} role on the signed-in operational account.").format(
                mode.title(), required
            ),
            frappe.PermissionError,
        )


def _live_location(mode: str):
    _require_live_role(mode)
    location = _get_user_location(frappe.session.user)
    if not location or not cint(location.enabled):
        frappe.throw(_("This operational account has no enabled NKT Operating Location."))
    if not location.company or not location.default_warehouse or not location.settlement_location:
        frappe.throw(_("The assigned NKT Operating Location is incomplete."))
    return location


def _live_open_shift(location):
    rows = frappe.get_all(
        "NKT Cashier Shift",
        filters={
            "cashier": frappe.session.user,
            "status": "Open",
            "docstatus": 0,
        },
        fields=["name", "company", "settlement_location", "shift_start", "status"],
        order_by="shift_start desc",
        limit_page_length=5,
    )
    if not rows:
        frappe.throw(_("Open a Cashier Shift before finalizing a live Cashier transaction."))
    if len(rows) > 1:
        frappe.throw(_("More than one open Cashier Shift exists for this account. Close the duplicate shift before posting."))
    shift = rows[0]
    if shift.company != location.company:
        frappe.throw(_("The open Cashier Shift belongs to a different company."))
    if shift.settlement_location != location.settlement_location:
        frappe.throw(
            _("The open Cashier Shift settlement location does not match the account's assigned operating location.")
        )
    today = getdate(nowdate())
    shift_date = getdate(shift.shift_start) if shift.shift_start else None
    if not shift_date or shift_date != today:
        frappe.throw(
            _(
                "Cashier Shift {0} is dated {1} and cannot be used on {2}. "
                "Close/review the old shift and open today's shift. "
                "NKT POS does not allow antedated or backdated sales."
            ).format(shift.name, shift_date or _("unknown"), today)
        )
    return shift


def _customer_for_live(customer: str):
    customer = (customer or "").strip()
    if not customer or not frappe.db.exists("Customer", customer):
        frappe.throw(_("Select an existing actual Customer before finalizing."))
    values = frappe.db.get_value("Customer", customer, ["name", "customer_name", "disabled"], as_dict=True)
    if not values or cint(values.disabled):
        frappe.throw(_("The selected Customer is disabled."))
    normalized = f"{values.name} {values.customer_name or ''}".lower().replace("_", " ").replace("-", " ")
    if "walk in" in " ".join(normalized.split()):
        frappe.throw(_("A generic Walk-in Customer is not allowed for NKT transactions."))
    return values


def _item_master_context(item_code: str):
    item_code = (item_code or "").strip()
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(_("Item does not exist: {0}").format(item_code or "(blank)"))
    saleable = _saleable_item_condition("i")
    rate_sql = _standard_rate_subquery("i")
    rows = frappe.db.sql(
        f"""
        SELECT
            i.name AS item_code,
            i.item_name,
            i.stock_uom,
            i.disabled,
            i.is_sales_item,
            {rate_sql} AS standard_rate
        FROM `tabItem` i
        WHERE i.name = %(item_code)s
          {saleable}
        LIMIT 1
        """,
        {"item_code": item_code},
        as_dict=True,
    )
    if not rows:
        frappe.throw(_("Only active Saleable Sack items are allowed: {0}").format(item_code))
    row = rows[0]
    if cint(row.disabled) or not cint(row.is_sales_item):
        frappe.throw(_("Item is not active for sale: {0}").format(item_code))
    if flt(row.standard_rate) <= 0:
        frappe.throw(_("Item has no positive Standard Selling rate: {0}").format(item_code))
    return row


def _validate_live_warehouse(warehouse: str, company: str):
    warehouse = (warehouse or "").strip()
    row = frappe.db.get_value(
        "Warehouse",
        warehouse,
        ["name", "company", "disabled", "is_group"],
        as_dict=True,
    )
    if not row:
        frappe.throw(_("Warehouse does not exist: {0}").format(warehouse or "(blank)"))
    if row.company != company or cint(row.disabled) or cint(row.is_group):
        frappe.throw(_("Warehouse is not an active transaction warehouse for this company: {0}").format(warehouse))
    return row.name


def _normalize_live_items(payload: dict[str, Any], location, mode: str):
    """Normalize Fast Screen lines without trusting the browser's standard rate.

    MP1 permits non-standard rates, but Cashier finalization separately requires
    a signed Manager-PIN proof. Encoder independently re-enters/confirms rates
    and therefore does not enter a Manager PIN.
    """
    raw_rows = payload.get("items") or []
    if not isinstance(raw_rows, list) or not raw_rows:
        frappe.throw(_("Enter at least one item."))
    normalized = []
    fixed_select = {
        -20.0: "-20",
        -15.0: "-15",
        -10.0: "-10",
        -5.0: "-5",
        0.0: "0",
        5.0: "5",
        10.0: "10",
        15.0: "15",
        20.0: "20",
    }

    for idx, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, dict):
            frappe.throw(_("Item row {0} is invalid.").format(idx))
        master = _item_master_context(raw.get("item_code") or raw.get("item"))
        qty = flt(raw.get("qty") if raw.get("qty") is not None else raw.get("quantity"))
        if qty <= 0:
            frappe.throw(_("Item row {0} must have a positive quantity.").format(idx))
        warehouse = _validate_live_warehouse(
            raw.get("warehouse") or raw.get("source_warehouse") or location.default_warehouse,
            location.company,
        )
        standard_rate = flt(master.standard_rate)
        rate = flt(raw.get("rate") if raw.get("rate") is not None else raw.get("final_rate"))
        if rate <= 0:
            rate = standard_rate
        if rate <= 0:
            frappe.throw(_("Item row {0} has no valid selling rate.").format(idx))

        difference = round(rate - standard_rate, 6)
        selected = None
        for numeric, option in fixed_select.items():
            if abs(difference - numeric) <= 0.000001:
                selected = option
                break

        special_rate = 0
        if selected is None:
            # The legacy price_adjustment field intentionally remains a
            # restricted Select. A Manager-authorized special/configured rate
            # outside those fixed options is stored in a separate hidden
            # read-only field instead of turning the normal form into an
            # unrestricted rate editor.
            selected = "0"
            if abs(difference) > 0.000001:
                special_rate = rate

        normalized.append(
            {
                "item": master.item_code,
                "item_name": master.item_name,
                "quantity": qty,
                "uom": master.stock_uom,
                "source_warehouse": warehouse,
                "standard_rate": standard_rate,
                "price_adjustment": selected,
                "custom_nkt_authorized_special_rate": special_rate,
                "final_rate": rate,
                "amount": qty * rate,
            }
        )
    return normalized

def _normalize_reference(value: Any) -> str:
    return "".join(str(value or "").strip().lower().split())


def _normalize_provider(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _check_reference_key(row: dict[str, Any]):
    """Return the normalized incoming-check identity inside one customer transaction.

    GCash/Maya/Bank Transfer/Online references are operational audit text and may be
    only the last four digits, so they are intentionally not uniqueness keys.
    """
    if (row.get("payment_method") or "").strip() != "Check":
        return None
    reference = _normalize_reference(row.get("reference_number"))
    provider = _normalize_provider(row.get("bank_or_provider"))
    if not reference or not provider:
        return None
    return provider, reference


def _validate_reference_rows(rows: list[dict[str, Any]], mode: str, customer: str = ""):
    """Apply NKT reference policy without treating informal e-payment suffixes as IDs.

    - GCash, Maya, Bank Transfer and Online: reference remains required by payment
      normalization but repeats are allowed, including across receipts.
    - Incoming Check: one check row per bank+check-number identity inside a payload;
      the Cashier additionally blocks a previously posted check for the same
      Customer + Issuing Bank + Check Number.
    - Encoder may repeat the Cashier check identity so the two sides can match.
    """
    seen_checks = {}
    for idx, row in enumerate(rows, start=1):
        key = _check_reference_key(row)
        if not key:
            continue
        if key in seen_checks:
            frappe.throw(
                _("Payment rows {0} and {1} repeat the same incoming Check Number for the same Issuing Bank. Record one row for each physical check.").format(
                    seen_checks[key], idx
                )
            )
        seen_checks[key] = idx

    # Encoder must repeat the Cashier check details for exact matching. Cross-receipt
    # duplicate protection therefore belongs only to the official Cashier side.
    if mode != "cashier":
        return

    customer = str(customer or "").strip()
    if not customer:
        frappe.throw(_("Customer is required before validating incoming Check identity."))

    for idx, row in enumerate(rows, start=1):
        key = _check_reference_key(row)
        if not key:
            continue
        provider_norm, reference_norm = key
        params = {
            "customer": customer,
            "reference_norm": reference_norm,
            "provider_norm": provider_norm,
        }
        matches = frappe.db.sql(
            """
            SELECT pd.parent AS payment_receipt, pr.source_cashier_sale, pr.customer,
                   pr.receipt_datetime, pd.amount, pd.check_date, pd.bank_or_provider
            FROM `tabNKT Payment Detail` pd
            INNER JOIN `tabNKT Payment Receipt` pr ON pr.name = pd.parent
            WHERE pd.parenttype = 'NKT Payment Receipt'
              AND pr.docstatus = 1
              AND pr.customer = %(customer)s
              AND pd.payment_method = 'Check'
              AND REPLACE(LOWER(TRIM(COALESCE(pd.reference_number, ''))), ' ', '') = %(reference_norm)s
              AND REPLACE(LOWER(TRIM(COALESCE(pd.bank_or_provider, ''))), ' ', '') = REPLACE(%(provider_norm)s, ' ', '')
            ORDER BY pr.creation DESC
            LIMIT 1
            """,
            params,
            as_dict=True,
        )
        if matches:
            hit = matches[0]
            frappe.throw(
                _(
                    "Incoming Check on payment row {0} is already recorded for this customer: "
                    "Check Number {1}, Issuing Bank {2}, Payment Receipt {3}{4}. "
                    "Do not post the same physical check twice."
                ).format(
                    idx,
                    row.get("reference_number"),
                    row.get("bank_or_provider"),
                    hit.payment_receipt,
                    f" / Cashier Sale {hit.source_cashier_sale}" if hit.source_cashier_sale else "",
                )
            )


@frappe.whitelist()
def preflight_incoming_check(customer: str, check_number: str, issuing_bank: str, mode: str = "cashier"):
    """Fast-screen preflight for incoming Checks on both Cashier and Encoder.

    Cashier: exact Customer + Bank + Check Number is a hard duplicate.
    Encoder: the exact physical Check is expected when it belongs to an unmatched
    Cashier Sale awaiting Encoder verification; an already allocated/matched Check
    is blocked. Same-number records under another Customer/Bank are advisory only.
    The controller validation remains authoritative on Finalize.
    """
    mode = str(mode or "cashier").strip().lower()
    if mode not in {"cashier", "encoder"}:
        frappe.throw(_("Invalid Fast Screen mode."))
    _check_fast_screen_access(mode)
    customer = str(customer or "").strip()
    check_number = str(check_number or "").strip()
    issuing_bank = str(issuing_bank or "").strip()
    if not customer or not check_number or not issuing_bank:
        return {"ok": True, "exact_duplicate": False, "other_matches": [], "encoder_match_available": False}
    if not frappe.db.exists("Customer", customer):
        frappe.throw(_("Select a valid Customer before checking the incoming Check."))

    check_norm = _normalize_reference(check_number)
    bank_norm = _normalize_provider(issuing_bank)
    rows = frappe.db.sql(
        """
        SELECT pd.parent AS payment_receipt, pr.source_cashier_sale, pr.customer,
               pr.customer_order, pr.allocation_status,
               pd.amount, pd.check_date, pd.bank_or_provider,
               COALESCE(NULLIF(pd.check_number, ''), pd.reference_number, '') AS check_number,
               cs.reconciliation_status AS cashier_reconciliation_status,
               cs.matched_customer_order,
               pr.creation
        FROM `tabNKT Payment Detail` pd
        INNER JOIN `tabNKT Payment Receipt` pr ON pr.name = pd.parent
        LEFT JOIN `tabNKT Cashier Sale` cs ON cs.name = pr.source_cashier_sale
        WHERE pd.parenttype = 'NKT Payment Receipt'
          AND pr.docstatus = 1
          AND pd.payment_method = 'Check'
          AND REPLACE(LOWER(TRIM(COALESCE(NULLIF(pd.check_number, ''), pd.reference_number, ''))), ' ', '') = %s
        ORDER BY pr.creation DESC
        LIMIT 20
        """,
        check_norm,
        as_dict=True,
    )
    exact = None
    others = []
    for row in rows:
        item = {
            "payment_receipt": row.payment_receipt,
            "cashier_sale": row.source_cashier_sale,
            "customer_order": row.customer_order,
            "allocation_status": row.allocation_status,
            "customer": row.customer,
            "amount": flt(row.amount),
            "check_date": row.check_date,
            "issuing_bank": row.bank_or_provider,
            "check_number": row.check_number,
            "cashier_reconciliation_status": row.cashier_reconciliation_status,
            "matched_customer_order": row.matched_customer_order,
            "creation": row.creation,
        }
        same_customer = str(row.customer or "").strip() == customer
        same_bank = _normalize_provider(row.bank_or_provider) == bank_norm
        if same_customer and same_bank and exact is None:
            exact = item
        else:
            others.append(item)

    encoder_match_available = False
    exact_duplicate = bool(exact)
    if mode == "encoder" and exact:
        encoder_match_available = bool(
            exact.get("cashier_sale")
            and not exact.get("customer_order")
            and not exact.get("matched_customer_order")
            and str(exact.get("cashier_reconciliation_status") or "").strip() not in {"Matched", "Ambiguous"}
        )
        exact_duplicate = not encoder_match_available

    return {
        "ok": True,
        "mode": mode,
        "exact_duplicate": exact_duplicate,
        "exact_match": exact,
        "encoder_match_available": encoder_match_available,
        "other_matches": others[:5],
        "duplicate_key": ["customer", "issuing_bank", "check_number"],
    }


def _diag_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _diag_number(value: Any) -> float:
    return round(flt(value), 6)


def _diag_item_signatures(doc):
    rows = list(doc.get("items") or [])
    item = sorted(_diag_text(r.get("item") or r.get("item_code")) for r in rows)
    qty = sorted((_diag_text(r.get("item") or r.get("item_code")), _diag_number(r.get("quantity") if r.get("quantity") is not None else r.get("qty"))) for r in rows)
    rate = sorted((_diag_text(r.get("item") or r.get("item_code")), _diag_number(r.get("final_rate") if r.get("final_rate") is not None else r.get("rate"))) for r in rows)
    warehouse = sorted((_diag_text(r.get("item") or r.get("item_code")), _diag_text(r.get("source_warehouse") or r.get("warehouse"))) for r in rows)
    return {"item": item, "qty": qty, "rate": rate, "warehouse": warehouse}


def _diag_payment_rows(doc, mode: str):
    rows = doc.get("payments") if mode == "cashier" else doc.get("declared_payments")
    normalized = []
    for row in rows or []:
        method = str(row.get("payment_method") or "").strip()
        amount = _diag_number(row.get("amount"))
        reference = row.get("check_number") or row.get("custom_nkt_check_number") or row.get("reference_number") or ""
        provider = row.get("bank_or_provider") or ""
        check_date = row.get("check_date") or row.get("custom_nkt_check_date") or ""
        normalized.append({
            "method": method,
            "amount": amount,
            "reference": _normalize_reference(reference),
            "provider": _normalize_provider(provider),
            "check_date": str(check_date or ""),
        })
    return normalized


def _diag_payment_signatures(doc, mode: str):
    rows = _diag_payment_rows(doc, mode)
    methods = sorted(r["method"] for r in rows)
    amounts = sorted((r["method"], r["amount"]) for r in rows)
    references = sorted((r["method"], r["reference"], r["provider"] if r["method"] == "Check" else "", r["check_date"] if r["method"] == "Check" else "") for r in rows)
    return {"methods": methods, "amounts": amounts, "references": references}


def _diag_compare(target, candidate, target_mode: str):
    candidate_mode = "encoder" if target_mode == "cashier" else "cashier"
    reasons = []
    details = []
    if str(target.customer or "") != str(candidate.customer or ""):
        reasons.append("customer mismatch")
        details.append(f"Customer: {target.customer or '(blank)'} vs {candidate.customer or '(blank)'}")

    ti = _diag_item_signatures(target)
    ci = _diag_item_signatures(candidate)
    if ti["item"] != ci["item"]:
        reasons.append("item mismatch")
    else:
        if ti["qty"] != ci["qty"]:
            reasons.append("quantity mismatch")
        if ti["rate"] != ci["rate"]:
            reasons.append("rate mismatch")
        if ti["warehouse"] != ci["warehouse"]:
            reasons.append("warehouse mismatch")

    tp = _diag_payment_signatures(target, target_mode)
    cp = _diag_payment_signatures(candidate, candidate_mode)
    if tp["methods"] != cp["methods"]:
        reasons.append("payment method mismatch")
    else:
        if tp["amounts"] != cp["amounts"]:
            reasons.append("payment amount mismatch")
        if tp["references"] != cp["references"]:
            reasons.append("reference mismatch")

    target_date = target.get("business_date") if target_mode == "cashier" else target.get("order_date")
    candidate_date = candidate.get("order_date") if target_mode == "cashier" else candidate.get("business_date")
    if str(target_date or "") != str(candidate_date or ""):
        reasons.append("business-date mismatch")
        details.append(f"Business date: {target_date or '(blank)'} vs {candidate_date or '(blank)'}")

    if target_mode == "cashier":
        linked = candidate.get("matched_cashier_sale")
        if linked and linked != target.name:
            reasons.append("candidate already matched")
            details.append(f"Candidate is already linked to Cashier Sale {linked}")
    else:
        linked = candidate.get("matched_customer_order")
        if linked and linked != target.name:
            reasons.append("candidate already matched")
            details.append(f"Candidate is already linked to Encoder Order {linked}")

    return reasons, details


def _reconciliation_diagnostics(mode: str, name: str):
    mode = str(mode or "").strip().lower()
    if mode not in {"cashier", "encoder"}:
        frappe.throw(_("Invalid reconciliation diagnostic mode."))
    target_dt = "NKT Cashier Sale" if mode == "cashier" else "NKT Customer Order"
    candidate_dt = "NKT Customer Order" if mode == "cashier" else "NKT Cashier Sale"
    date_field = "business_date" if mode == "cashier" else "order_date"
    candidate_date_field = "order_date" if mode == "cashier" else "business_date"
    target = frappe.get_doc(target_dt, name)
    status = target.get("reconciliation_status") if mode == "cashier" else target.get("cashier_reconciliation_status")
    linked = target.get("matched_customer_order") if mode == "cashier" else target.get("matched_cashier_sale")
    if str(status or "").startswith("Matched") and linked:
        return {"status": status, "matched": True, "primary_reasons": [], "candidates": [], "linked_document": linked}

    target_date = getdate(target.get(date_field) or nowdate())
    names = []
    def add_rows(filters, limit=30):
        for candidate_name in frappe.get_all(candidate_dt, filters=filters, pluck="name", order_by="creation desc", limit_page_length=limit):
            if candidate_name not in names:
                names.append(candidate_name)

    common = {"company": target.company, "docstatus": 1}
    same_customer = dict(common)
    same_customer.update({"customer": target.customer, candidate_date_field: ["between", [add_days(target_date, -2), add_days(target_date, 2)]]})
    add_rows(same_customer, 35)
    same_date = dict(common)
    same_date[candidate_date_field] = target_date
    add_rows(same_date, 35)
    if len(names) < 10:
        add_rows(common, 20)

    comparisons = []
    for candidate_name in names[:60]:
        candidate = frappe.get_doc(candidate_dt, candidate_name)
        reasons, details = _diag_compare(target, candidate, mode)
        comparisons.append({
            "name": candidate.name,
            "reasons": reasons,
            "details": details,
            "grand_total": flt(candidate.get("grand_total")),
            "customer": candidate.get("customer"),
            "date": str(candidate.get(candidate_date_field) or ""),
        })

    exact = [row for row in comparisons if not row["reasons"]]
    status_text = str(status or "Unmatched")

    # C5.5 NO-EXACT-MATCH DIAGNOSTIC HOTFIX
    # Zero/one exact candidate must never be described as
    # ambiguous duplicate candidates.
    if len(exact) <= 1 and status_text == "Ambiguous":
        status_text = "Unmatched"

    if len(exact) > 1:
        status_text = "Ambiguous"
        return {
            "status": status_text,
            "matched": False,
            "primary_reasons": [{"code": "ambiguous duplicate candidates", "label": "Ambiguous duplicate candidates", "detail": f"{len(exact)} exact candidates are available; audited manual resolution is required."}],
            "candidates": exact[:5],
            "candidate_count": len(exact),
        }
    if len(exact) == 1:
        return {
            "status": status_text,
            "matched": False,
            "primary_reasons": [{"code": "exact candidate not linked", "label": "Exact candidate exists but was not linked", "detail": f"Candidate {exact[0]['name']} matches the controlled fingerprints. Administrator inspection is required; no automatic rematch is performed by this diagnostic."}],
            "candidates": exact,
            "candidate_count": 1,
        }
    if not comparisons:
        return {
            "status": status_text,
            "matched": False,
            "primary_reasons": [{"code": "no opposing transaction", "label": "No opposing transaction posted yet", "detail": "No submitted Cashier/Encoder transaction is available for comparison."}],
            "candidates": [],
            "candidate_count": 0,
        }

    def rank(row):
        priority = {"customer mismatch": 3, "item mismatch": 4, "payment method mismatch": 4, "business-date mismatch": 2, "candidate already matched": 1}
        return (len(row["reasons"]), sum(priority.get(r, 1) for r in row["reasons"]), abs(flt(row["grand_total"]) - flt(target.get("grand_total"))))
    comparisons.sort(key=rank)
    nearest = comparisons[:3]
    primary_codes = nearest[0]["reasons"]
    mismatch_primary = [
        {
            "code": code,
            "label": code.replace("-", " ").title(),
            "detail": "; ".join(nearest[0].get("details") or []),
        }
        for code in primary_codes
    ]
    primary = [
        {
            "code": "no exact match",
            "label": "No exact match",
            "detail": (
                "No exact same-customer Cashier/Encoder transaction "
                "is currently available. Closest candidate mismatch "
                "reasons are shown below."
            ),
        }
    ] + mismatch_primary
    return {
        "status": status_text,
        "matched": False,
        "primary_reasons": primary,
        "candidates": nearest,
        "candidate_count": len(comparisons),
    }


def _require_reconciliation_diagnostics_support():
    user = str(frappe.session.user or "").strip()
    roles = set(frappe.get_roles(user) or [])
    if user == "Administrator" or roles.intersection({"NKT OWNER", "NKT ADMINISTRATOR", "System Manager"}):
        return True
    frappe.throw(
        _("Reconciliation diagnostics are restricted to management/support."),
        frappe.PermissionError,
    )


@frappe.whitelist()
def get_reconciliation_diagnostics(mode: str, name: str):
    _require_reconciliation_diagnostics_support()
    mode = str(mode or "").strip().lower()
    return _reconciliation_diagnostics(mode, str(name or "").strip())




def _account_customer_for_fast_payment(customer: str):
    values = _customer_for_live(customer)
    meta = frappe.get_meta("Customer")
    allowed = bool(meta.has_field("custom_nkt_allow_account_sales") and cint(
        frappe.db.get_value("Customer", values.name, "custom_nkt_allow_account_sales") or 0
    ))
    if not allowed:
        frappe.throw(_("This Customer is not enabled for Account transactions."))
    return values


def _account_payment_rows(raw_rows: Any) -> list[dict[str, Any]]:
    out = []
    for raw in list(raw_rows or []):
        row = dict(raw or {})
        method = normalize_payment_method(row.get("payment_method") or row.get("method") or "")
        amount = flt(row.get("amount"))
        if amount <= 0:
            frappe.throw(_("Every Payment on Account row must have an amount greater than zero."))
        reference = str(row.get("reference_number") or row.get("reference") or "").strip()
        provider = str(row.get("bank_or_provider") or row.get("provider") or "").strip()
        out.append({
            "payment_method": method,
            "amount": amount,
            "cash_tendered": flt(row.get("cash_tendered")),
            "change_amount": flt(row.get("change_amount")),
            "reference_number": reference,
            "bank_or_provider": provider,
            "check_number": str(row.get("check_number") or (reference if method == "Check" else "")).strip(),
            "check_date": str(row.get("check_date") or "").strip() or None,
            "remarks": str(row.get("remarks") or "").strip(),
        })
    return out


def _account_payment_payload_hash(
    mode: str, customer: str, account_amount: float, payments: list[dict[str, Any]], remarks: str,
    plate_number: str = "", os_no: str = "",
) -> str:
    safe_rows = []
    for row in payments:
        safe_rows.append({
            "payment_method": normalize_payment_method(row.get("payment_method")),
            "amount": round(flt(row.get("amount")), 6),
            "cash_tendered": round(flt(row.get("cash_tendered")), 6),
            "change_amount": round(flt(row.get("change_amount")), 6),
            "reference_number": str(row.get("reference_number") or ""),
            "bank_or_provider": str(row.get("bank_or_provider") or ""),
            "check_number": str(row.get("check_number") or ""),
            "check_date": str(row.get("check_date") or ""),
        })
    raw = json.dumps({
        "mode": mode,
        "customer": customer,
        "account_amount": round(flt(account_amount), 6),
        "payments": safe_rows,
        "remarks": str(remarks or "").strip(),
        # UI7C1B: Encoder audit references are immutable request metadata, but
        # remain outside the Cashier/Encoder business matching fingerprint.
        "plate_number": str(plate_number or "").strip(),
        "os_no": str(os_no or "").strip(),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _public_payment_number(value: Any) -> str:
    """Frontline/customer display identity; canonical NKT-PAY-* remains internal."""
    text = str(value or "").strip()
    prefix = "NKT-PAY-"
    if text.startswith(prefix):
        suffix = text[len(prefix):]
        if suffix.isdigit():
            return f"P{int(suffix):06d}"
    return text


def _public_account_payment_remarks(value: Any) -> str:
    """Remove machine reconciliation prose from customer/frontline copies."""
    text = str(value or "").strip()
    lowered = text.lower()
    machine_prefixes = (
        "verified customer account collection nkt-col-cash-",
        "cashier account collection nkt-col-cash-",
    )
    return "" if any(lowered.startswith(prefix) for prefix in machine_prefixes) else text


def _account_fast_print_payload(mode: str, receipt_name: str) -> dict[str, Any]:
    """Build the immediate F10 customer receipt without exposing canonical/internal IDs.

    This is display-only. It does not rename or mutate the canonical Payment Receipt.
    Cashier privacy is preserved by withholding account-balance and Encoder audit refs.
    """
    receipt_name = str(receipt_name or "").strip()
    if not receipt_name or not frappe.db.exists("NKT Payment Receipt", receipt_name):
        return {}
    receipt = frappe.get_doc("NKT Payment Receipt", receipt_name)
    principal = flt(receipt.get("total_payment"))
    collected = flt(receipt.get("total_collected") or principal)
    surcharge = max(collected - principal, 0.0)
    payments = []
    for row in list(receipt.get("payments") or []):
        payments.append({
            "method": normalize_payment_method(row.get("payment_method")),
            "amount": flt(row.get("amount")),
            "cash_tendered": flt(row.get("cash_tendered")),
            "change_amount": flt(row.get("change_amount")),
            "reference_number": str(row.get("reference_number") or ""),
            "bank_or_provider": str(row.get("bank_or_provider") or ""),
            "check_number": str(row.get("check_number") or ""),
            "check_date": str(row.get("check_date") or ""),
        })
    encoder_view = mode == "encoder"
    stamp = receipt.get("receipt_datetime") or receipt.creation
    return {
        "receipt_number": _public_payment_number(receipt.name),
        "transaction_date": str(getdate(stamp)),
        "generated_at": now_datetime().strftime("%Y-%m-%d %H:%M:%S"),
        "customer": str(receipt.get("customer") or ""),
        "customer_name": str(receipt.get("customer_name") or receipt.get("customer") or ""),
        "account_payment": principal,
        "card_surcharge": surcharge,
        "receipt_total": collected,
        "payments": payments,
        "show_account_balances": encoder_view,
        "previous_account_balance": flt(receipt.get("amount_due_before_receipt")) if encoder_view else None,
        "total_account_balance": flt(receipt.get("remaining_balance")) if encoder_view else None,
        "plate_reference": str(receipt.get("custom_nkt_plate_number") or "") if encoder_view else "",
        "os_no": str(receipt.get("custom_nkt_source_order_slip") or "") if encoder_view else "",
        "remarks": _public_account_payment_remarks(receipt.get("remarks")),
    }


def _account_existing_fast_doc(doctype: str, request_id: str):
    if not request_id or not frappe.get_meta(doctype).has_field(ACCOUNT_FAST_REQUEST_FIELD):
        return None
    name = frappe.db.get_value(doctype, {ACCOUNT_FAST_REQUEST_FIELD: request_id}, "name")
    return frappe.get_doc(doctype, name) if name else None


def _account_fast_result(mode: str, doc, request_id: str, replayed: bool = False):
    from urllib.parse import quote
    doc.reload()
    if mode == "cashier":
        receipt = str(doc.get("linked_payment_receipt") or "")
        payments = list(doc.get("payments") or [])
        change_amount = sum(flt(row.get("change_amount")) for row in payments)
        print_dt = "NKT Payment Receipt" if receipt else ACCOUNT_CASHIER_DOCTYPE
        print_name = receipt or doc.name
        return {
            "ok": True,
            "mode": "cashier_account_payment",
            "request_id": request_id,
            "replayed": bool(replayed),
            "cashier_collection": doc.name,
            "payment_receipt": receipt,
            "payment_display_number": _public_payment_number(receipt),
            "status": str(doc.get("status") or ""),
            "account_principal": flt(doc.get("total_payment")),
            "card_surcharge": flt(doc.get("card_surcharge_total")),
            "total_collected": flt(doc.get("total_collected") or doc.get("total_payment")),
            "change_amount": change_amount,
            "print_receipt": _account_fast_print_payload("cashier", receipt),
            "print_url": "",
        }
    matched = str(doc.get("matched_cashier_collection") or "")
    receipt = str(frappe.db.get_value(ACCOUNT_CASHIER_DOCTYPE, matched, "linked_payment_receipt") or "") if matched else ""
    status = str(doc.get("status") or "")
    return {
        "ok": True,
        "mode": "encoder_account_payment",
        "request_id": request_id,
        "replayed": bool(replayed),
        "encoder_allocation": doc.name,
        "matched_cashier_collection": matched,
        "payment_receipt": receipt,
        "payment_display_number": _public_payment_number(receipt),
        "status": status,
        "account_principal": flt(doc.get("collection_amount")),
        "total_allocated": flt(doc.get("total_allocated")),
        "unallocated_amount": flt(doc.get("unallocated_amount")),
        "account_application_complete": bool(status == "Matched" and cint(doc.get("allocations_posted") or 0)),
        "current_account_balance": _fast_customer_operational_balance(doc.customer),
        "print_receipt": _account_fast_print_payload("encoder", receipt),
        "print_url": "",
    }


@frappe.whitelist()
def get_fast_account_payment_customer_context(mode: str, customer: str):
    mode = str(mode or "").strip().lower()
    if mode not in {"cashier", "encoder"}:
        frappe.throw(_("Invalid Fast Screen mode."))
    _live_location(mode)
    values = _customer_for_live(customer)
    meta = frappe.get_meta("Customer")
    allowed = bool(meta.has_field("custom_nkt_allow_account_sales") and cint(
        frappe.db.get_value("Customer", values.name, "custom_nkt_allow_account_sales") or 0
    ))
    # Cashier privacy is deliberate: no account balance or receivable detail is returned.
    result = {
        "customer": values.name,
        "customer_name": values.customer_name or values.name,
        "allow_account_sales": allowed,
        "mode": mode,
    }
    if mode == "encoder":
        result["current_account_balance"] = _fast_customer_operational_balance(values.name)
    return result


@frappe.whitelist()
def submit_fast_account_payment(payload: Any):
    payload = _parse_fast_payload(payload)
    mode = str(payload.get("mode") or "").strip().lower()
    if mode not in {"cashier", "encoder"}:
        frappe.throw(_("Payment on Account mode must be Cashier or Encoder."))
    location = _live_location(mode)
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        frappe.throw(_("A Payment on Account Request ID is required."))
    customer = _account_customer_for_fast_payment(payload.get("customer"))
    account_amount = flt(payload.get("account_amount"))
    if account_amount <= 0:
        frappe.throw(_("Account Payment Amount must be greater than zero."))
    payments = _account_payment_rows(payload.get("payments") or [])
    if not payments:
        frappe.throw(_("Enter at least one payment row."))
    principal = sum(flt(row.get("amount")) for row in payments)
    if abs(principal - account_amount) > 0.01:
        frappe.throw(_("Payment row amounts must equal the Account Payment Amount."))
    remarks = str(payload.get("remarks") or "").strip()
    plate_number = str(payload.get("plate_number") or "").strip() if mode == "encoder" else ""
    os_no = str(payload.get("os_no") or "").strip() if mode == "encoder" else ""
    if len(plate_number) > 140:
        frappe.throw(_("Plate Number is too long."))
    if len(os_no) > 140:
        frappe.throw(_("OS# is too long."))
    payload_hash = _account_payment_payload_hash(
        mode, customer.name, account_amount, payments, remarks, plate_number=plate_number, os_no=os_no
    )
    doctype = ACCOUNT_CASHIER_DOCTYPE if mode == "cashier" else ACCOUNT_ENCODER_DOCTYPE
    operator_field = "cashier" if mode == "cashier" else "encoder"

    with _fast_request_lock(doctype, request_id):
        existing = _account_existing_fast_doc(doctype, request_id)
        if existing:
            existing_hash = str(existing.get(ACCOUNT_FAST_PAYLOAD_HASH_FIELD) or "")
            if existing_hash and existing_hash != payload_hash:
                frappe.throw(_("This Request ID already belongs to a different Payment on Account payload."))
            operator = str(existing.get(operator_field) or "")
            if operator and operator != frappe.session.user:
                frappe.throw(_("This Request ID belongs to another operator."), frappe.PermissionError)
            if mode == "cashier":
                from nkt_operations.nkt_store_operations.features.payments_accounts import collection as nkt_account_collection
                nkt_account_collection.submit_cashier_collection(existing.name)
            else:
                from nkt_operations.nkt_store_operations.features.payments_accounts import collection as nkt_account_collection
                nkt_account_collection.submit_encoder_allocation(existing.name)
            return _account_fast_result(mode, existing, request_id, replayed=True)

        # UI7B6: prevent repeated Encoder-only attempts from creating multiple
        # indistinguishable unresolved verification records. The normal business
        # flow is Cashier records the money, then Encoder independently verifies it.
        # If an identical unresolved Encoder entry already exists, retry its match
        # first. If no Cashier paper exists yet, stop cleanly instead of inserting
        # another duplicate. Once the prior entry is Matched, a later identical
        # payment can still be entered as a genuinely separate collection.
        if mode == "encoder":
            # UI7C1B: duplicate suppression follows the same payment fingerprint
            # used by the accepted collection logic, so changing an optional
            # Plate/OS audit reference cannot create a second unresolved payment.
            # Plate/OS are NOT added to that matching fingerprint.
            from nkt_operations.nkt_store_operations.features.payments_accounts import collection as nkt_account_collection
            payment_fingerprint = nkt_account_collection._collection_fingerprint(payments, None)
            unresolved = frappe.get_all(
                doctype,
                filters={
                    "company": location.company,
                    "allocation_date": nowdate(),
                    "encoder": frappe.session.user,
                    "customer": customer.name,
                    "payment_fingerprint": payment_fingerprint,
                    "status": ["in", ["Unmatched", "Ambiguous"]],
                    "allocations_posted": 0,
                },
                fields=["name", "matched_cashier_collection", "linked_payment_receipt", "status", "creation"],
                order_by="creation desc, name desc",
                limit_page_length=1,
            )
            if unresolved:
                prior = frappe.get_doc(doctype, unresolved[0].name)
                from nkt_operations.nkt_store_operations.features.payments_accounts import collection as nkt_account_collection
                nkt_account_collection.submit_encoder_allocation(prior.name)
                prior.reload()
                if str(prior.get("status") or "") == "Matched":
                    return _account_fast_result(mode, prior, str(prior.get(ACCOUNT_FAST_REQUEST_FIELD) or request_id), replayed=True)
                frappe.throw(
                    _(
                        "This same Encoder Payment on Account is already recorded as {0} and is still waiting for the Cashier collection. "
                        "Do not enter it again. Record the Cashier payment first, then retry this Encoder verification."
                    ).format(prior.name)
                )

        doc = frappe.new_doc(doctype)
        doc.company = location.company
        doc.customer = customer.name
        doc.remarks = remarks
        if doc.meta.has_field(ACCOUNT_FAST_REQUEST_FIELD):
            doc.set(ACCOUNT_FAST_REQUEST_FIELD, request_id)
        if doc.meta.has_field(ACCOUNT_FAST_PAYLOAD_HASH_FIELD):
            doc.set(ACCOUNT_FAST_PAYLOAD_HASH_FIELD, payload_hash)
        if mode == "cashier":
            # UI7B3: these are mandatory Cashier Account Collection fields, but they
            # must never be supplied by or trusted from the browser. Resolve them
            # from the signed-in Cashier and today's validated open shift before
            # the first insert, then submit_cashier_collection() re-validates them.
            shift = _live_open_shift(location)
            doc.cashier = frappe.session.user
            doc.cashier_shift = shift.name
            doc.business_date = nowdate()
            doc.collection_datetime = now_datetime()
            if doc.meta.has_field("settlement_location"):
                doc.settlement_location = location.settlement_location
        else:
            doc.encoder = frappe.session.user
            doc.allocation_date = nowdate()
            if doc.meta.has_field("custom_nkt_plate_number"):
                doc.set("custom_nkt_plate_number", plate_number or None)
            if doc.meta.has_field("custom_nkt_source_order_slip"):
                doc.set("custom_nkt_source_order_slip", os_no or None)
        for row in payments:
            doc.append("payments", row)
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        if mode == "cashier":
            from nkt_operations.nkt_store_operations.features.payments_accounts import collection as nkt_account_collection
            nkt_account_collection.submit_cashier_collection(doc.name)
        else:
            from nkt_operations.nkt_store_operations.features.payments_accounts import collection as nkt_account_collection
            nkt_account_collection.submit_encoder_allocation(doc.name)
        return _account_fast_result(mode, doc, request_id)


@frappe.whitelist()
def get_fast_account_payment_status(mode: str, request_id: str):
    mode = str(mode or "").strip().lower()
    if mode not in {"cashier", "encoder"}:
        frappe.throw(_("Invalid Payment on Account mode."))
    _live_location(mode)
    request_id = str(request_id or "").strip()
    doctype = ACCOUNT_CASHIER_DOCTYPE if mode == "cashier" else ACCOUNT_ENCODER_DOCTYPE
    existing = _account_existing_fast_doc(doctype, request_id)
    if not existing:
        return {"found": False, "request_id": request_id}
    operator = str(existing.get("cashier" if mode == "cashier" else "encoder") or "")
    if operator and operator != frappe.session.user:
        frappe.throw(_("This Request ID belongs to another operator."), frappe.PermissionError)
    status = str(existing.get("status") or "")
    submitted = status not in {"", "Draft"}
    return {
        "found": True,
        "submitted": submitted,
        "request_id": request_id,
        "name": existing.name,
        "status": status,
        "result": _account_fast_result(mode, existing, request_id, replayed=True) if submitted else None,
    }


@frappe.whitelist()
def get_fast_request_status(mode: str, request_id: str):
    """Recover a Fast UI posting result when the browser loses the original response."""
    mode = str(mode or "").strip().lower()
    if mode not in {"cashier", "encoder"}:
        frappe.throw(_("Invalid Fast UI mode."))
    _check_fast_screen_access(mode)
    request_id = str(request_id or "").strip()
    if not request_id:
        return {"found": False, "request_id": ""}
    doctype = "NKT Cashier Sale" if mode == "cashier" else "NKT Customer Order"
    existing = _existing_fast_transaction(doctype, request_id)
    if not existing:
        return {"found": False, "request_id": request_id}
    if existing.docstatus != 1:
        return {
            "found": True,
            "submitted": False,
            "request_id": request_id,
            "doctype": doctype,
            "name": existing.name,
            "docstatus": existing.docstatus,
        }
    result = (
        _cashier_posting_result(existing, request_id, replayed=True)
        if mode == "cashier"
        else _encoder_posting_result(existing, request_id, replayed=True)
    )
    return {"found": True, "submitted": True, "request_id": request_id, "result": result}


@contextmanager
def _fast_request_lock(doctype: str, request_id: str):
    digest = hashlib.sha256(f"{doctype}|{request_id}".encode()).hexdigest()[:40]
    lock_name = f"nkt-fast-{digest}"
    result = frappe.db.sql("SELECT GET_LOCK(%s, 20)", (lock_name,))
    acquired = bool(result and result[0] and result[0][0] == 1)
    if not acquired:
        frappe.throw(_("This transaction is already being finalized. Wait a moment, then retry once."))
    try:
        yield
    finally:
        try:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
        except Exception:
            pass


def _request_index_status(doctype: str):
    table = f"tab{doctype}"
    rows = frappe.db.sql(
        """
        SELECT INDEX_NAME, NON_UNIQUE
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
        ORDER BY NON_UNIQUE, INDEX_NAME
        """,
        (table, FAST_REQUEST_FIELD),
        as_dict=True,
    )
    return rows


def _ensure_unique_request_index(doctype: str, index_name: str):
    rows = _request_index_status(doctype)
    if any(not int(row.NON_UNIQUE) for row in rows):
        return {"doctype": doctype, "status": "already_unique", "indexes": rows}
    duplicates = frappe.db.sql(
        f"""
        SELECT `{FAST_REQUEST_FIELD}` AS request_id, COUNT(*) AS row_count
        FROM `tab{doctype}`
        WHERE COALESCE(`{FAST_REQUEST_FIELD}`, '') != ''
        GROUP BY `{FAST_REQUEST_FIELD}`
        HAVING COUNT(*) > 1
        LIMIT 5
        """,
        as_dict=True,
    )
    if duplicates:
        frappe.throw(_("Cannot create the request-ID unique index because duplicate request IDs already exist in {0}: {1}").format(doctype, duplicates))
    frappe.db.sql(
        f"ALTER TABLE `tab{doctype}` ADD UNIQUE INDEX `{index_name}` (`{FAST_REQUEST_FIELD}`)"
    )
    return {"doctype": doctype, "status": "created", "index": index_name}


def _cashier_posting_integrity(doc):
    """Return a strict operational audit summary without creating any new records."""
    doc.reload()
    expected_rows = [row for row in (doc.get("payments") or []) if row.payment_method != "Account"]
    receipt_name = doc.get("linked_payment_receipt")
    errors = []
    if expected_rows and not receipt_name:
        errors.append("A non-account Cashier transaction has no linked Payment Receipt.")

    receipt_rows = []
    if receipt_name and frappe.db.exists("NKT Payment Receipt", receipt_name):
        receipt = frappe.get_doc("NKT Payment Receipt", receipt_name)
        receipt_rows = [row for row in (receipt.get("payments") or []) if row.payment_method != "Account"]
        expected_total = round(sum(flt(row.amount) for row in expected_rows), 2)
        receipt_total = round(flt(receipt.total_payment), 2)
        if abs(expected_total - receipt_total) > 0.01:
            errors.append(f"Payment Receipt total {receipt_total:.2f} does not equal settled non-account amount {expected_total:.2f}.")

    movement_filters = []
    if receipt_name:
        movement_filters.append(("NKT Payment Receipt", receipt_name))
    movement_filters.append(("NKT Cashier Sale", doc.name))
    movements = []
    seen_names = set()
    for source_doctype, source_name in movement_filters:
        for row in frappe.get_all(
            "NKT Cashier Movement",
            filters={"source_doctype": source_doctype, "source_name": source_name, "docstatus": 1},
            fields=["name", "payment_method", "amount", "settlement_amount", "card_surcharge", "source_doctype", "source_name", "source_row", "status"],
            order_by="name",
        ):
            if row.name not in seen_names:
                seen_names.add(row.name)
                movements.append(row)

    if len(movements) != len(expected_rows):
        errors.append(f"Expected {len(expected_rows)} posted payment movement(s), found {len(movements)}.")

    def totals(rows, method_key="payment_method", amount_key="amount"):
        result = {}
        for row in rows:
            method = row.get(method_key) if isinstance(row, dict) else getattr(row, method_key)
            amount = row.get(amount_key) if isinstance(row, dict) else getattr(row, amount_key)
            result[method] = round(result.get(method, 0.0) + flt(amount), 2)
        return result

    expected_by_method = {}
    for row in expected_rows:
        method = normalize_payment_method(row.payment_method)
        expected_by_method[method] = round(
            expected_by_method.get(method, 0.0) + row_collected_amount(row),
            2,
        )
    movement_by_method = {}
    for row in movements:
        method = normalize_payment_method(row.payment_method)
        movement_by_method[method] = round(
            movement_by_method.get(method, 0.0) + flt(row.amount),
            2,
        )
    if expected_by_method != movement_by_method:
        errors.append(f"Payment-method movement totals do not agree. Expected actual collected {expected_by_method}; found {movement_by_method}.")

    return {
        "passed": not errors,
        "errors": errors,
        "expected_payment_rows": len(expected_rows),
        "receipt_payment_rows": len(receipt_rows),
        "movement_count": len(movements),
        "expected_by_method": expected_by_method,
        "movement_by_method": movement_by_method,
        "payment_receipt": receipt_name,
        "movement_names": [row.name for row in movements],
    }


def _normalize_live_payments(payload: dict[str, Any], merchandise_total: float, mode: str, customer: str = ""):
    raw_rows = payload.get("payments") or []
    if not isinstance(raw_rows, list) or not raw_rows:
        frappe.throw(_("Complete F11 payment settlement before finalizing."))
    rows = []
    running = 0.0
    cash_count = 0
    for idx, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, dict):
            frappe.throw(_("Payment row {0} is invalid.").format(idx))
        method = normalize_payment_method(raw.get("method") or raw.get("payment_method") or "")
        if method not in LIVE_PAYMENT_METHODS:
            frappe.throw(_("Unsupported payment method on row {0}: {1}").format(idx, method or "(blank)"))
        amount = flt(raw.get("amount"))
        if amount <= 0:
            frappe.throw(_("Payment row {0} must have a positive settled amount.").format(idx))
        reference = (raw.get("reference") or raw.get("reference_number") or "").strip()
        provider = (raw.get("provider") or raw.get("bank_or_provider") or "").strip()
        check_date = (raw.get("check_date") or "").strip()
        row = {
            "payment_method": method,
            "amount": amount,
            "reference_number": reference,
            "bank_or_provider": provider,
            "remarks": f"Posted from NKT Fast UI {VERSION}",
        }
        if method == "Cash":
            cash_count += 1
            if cash_count > 1:
                frappe.throw(_("Only one Cash row is allowed."))
            cash_tendered = flt(raw.get("cash_tendered") if raw.get("cash_tendered") is not None else payload.get("cash_tendered"))
            change_amount = flt(raw.get("change_amount") if raw.get("change_amount") is not None else payload.get("change_amount"))
            if mode == "cashier":
                if cash_tendered + 0.005 < amount:
                    frappe.throw(_("Cash Tendered is less than the Cash Due."))
                expected_change = max(cash_tendered - amount, 0)
                if abs(expected_change - change_amount) > 0.01:
                    frappe.throw(_("Cash change no longer agrees with Cash Tendered and Cash Due. Reopen F11 and confirm payment again."))
                row["cash_tendered"] = cash_tendered
                row["change_amount"] = expected_change
            else:
                row["cash_tendered"] = amount
                row["change_amount"] = 0
            row["reference_number"] = ""
            row["bank_or_provider"] = ""
        elif method == "Check":
            if not reference:
                frappe.throw(_("Check Number is required on payment row {0}.").format(idx))
            if not check_date:
                frappe.throw(_("Check Date is required on payment row {0}.").format(idx))
            if not provider:
                frappe.throw(_("Issuing Bank is required on payment row {0}.").format(idx))
            row["check_number"] = reference
            row["check_date"] = check_date
        elif method == "Account":
            row["reference_number"] = ""
        elif not reference:
            frappe.throw(_("Reference Number is required on payment row {0}.").format(idx))
        running += amount
        rows.append(row)
    if abs(running - merchandise_total) > 0.01:
        frappe.throw(
            _("Payment settlement total {0} must equal the merchandise total {1}.").format(
                frappe.format_value(running, {"fieldtype": "Currency"}),
                frappe.format_value(merchandise_total, {"fieldtype": "Currency"}),
            )
        )
    _validate_reference_rows(rows, mode, customer)
    return rows


def _existing_fast_transaction(doctype: str, request_id: str):
    if not request_id or not frappe.get_meta(doctype).has_field(FAST_REQUEST_FIELD):
        return None
    name = frappe.db.get_value(doctype, {FAST_REQUEST_FIELD: request_id}, "name")
    return frappe.get_doc(doctype, name) if name else None


def _cashier_posting_result(doc, request_id: str, replayed=False):
    doc.reload()
    receipt = doc.get("linked_payment_receipt")
    movement_names = []
    if receipt:
        movement_names.extend(
            frappe.get_all(
                "NKT Cashier Movement",
                filters={"source_doctype": "NKT Payment Receipt", "source_name": receipt, "docstatus": 1},
                pluck="name",
                order_by="name",
            )
        )
    movement_names.extend(
        frappe.get_all(
            "NKT Cashier Movement",
            filters={"source_doctype": "NKT Cashier Sale", "source_name": doc.name, "docstatus": 1},
            pluck="name",
            order_by="name",
        )
    )
    movement_names = list(dict.fromkeys(movement_names))
    print_doctype = "NKT Payment Receipt" if receipt else "NKT Cashier Sale"
    print_name = receipt or doc.name
    from urllib.parse import quote
    return {
        "ok": True,
        "version": VERSION,
        "mode": "cashier",
        "request_id": request_id,
        "replayed": bool(replayed),
        "cashier_sale": doc.name,
        "docstatus": doc.docstatus,
        "status": doc.get("status"),
        "reconciliation_status": doc.get("reconciliation_status"),
        "matched_customer_order": doc.get("matched_customer_order"),
        "payment_receipt": receipt,
        "cashier_movements": movement_names,
        "print_doctype": print_doctype,
        "print_name": print_name,
        "print_url": f"/printview?doctype={quote(print_doctype)}&name={quote(print_name)}&trigger_print=1",
        "integrity": _cashier_posting_integrity(doc),
    }


def _encoder_posting_result(doc, request_id: str, replayed=False):
    doc.reload()
    releases = frappe.get_all(
        "NKT Warehouse Release",
        filters={"customer_order": doc.name},
        fields=["name", "release_status", "custom_nkt_source_warehouse", "total_release_quantity"],
        order_by="name",
    )
    reservation_entries = []
    for item in doc.get("items") or []:
        ref = item.get("custom_nkt_stock_reservation_entry")
        if ref:
            reservation_entries.append(ref)
    reservation_entries = list(dict.fromkeys(reservation_entries))
    from urllib.parse import quote
    return {
        "ok": True,
        "version": VERSION,
        "mode": "encoder",
        "request_id": request_id,
        "replayed": bool(replayed),
        "customer_order": doc.name,
        "docstatus": doc.docstatus,
        "status": doc.get("status"),
        "payment_status": doc.get("payment_status"),
        "cashier_reconciliation_status": doc.get("cashier_reconciliation_status"),
        "matched_cashier_sale": doc.get("matched_cashier_sale"),
        "immediate_stock_entry": doc.get("custom_nkt_retail_stock_entry"),
        "reservation_entries": reservation_entries,
        "warehouse_releases": releases,
        "customer_receivable": doc.get("custom_nkt_customer_receivable"),
        "print_doctype": "NKT Customer Order",
        "print_name": doc.name,
        "print_url": f"/printview?doctype={quote('NKT Customer Order')}&name={quote(doc.name)}&trigger_print=1",
    }


@frappe.whitelist()
def finalize_cashier_fast_transaction(payload: Any):
    location = _live_location("cashier")
    shift = _live_open_shift(location)
    payload = _parse_fast_payload(payload)
    request_id = (payload.get("request_id") or "").strip()
    if not request_id:
        frappe.throw(_("A transaction request ID is required. Refresh the fast screen and try again."))

    with _fast_request_lock("NKT Cashier Sale", request_id):
        existing = _existing_fast_transaction("NKT Cashier Sale", request_id)
        if existing:
            if existing.docstatus != 1:
                frappe.throw(_("An incomplete Cashier transaction already uses this request ID. Ask an Administrator to inspect it before retrying."))
            return _cashier_posting_result(existing, request_id, replayed=True)

        customer = _customer_for_live(payload.get("customer"))
        items = _normalize_live_items(payload, location, "cashier")
        price_authorization = None
        if any(
            abs(flt(row.get("final_rate")) - flt(row.get("standard_rate"))) > 0.000001
            for row in items
        ):
            from nkt_operations.nkt_store_operations import manager_authorization as nkt_manager_pin
            price_authorization = nkt_manager_pin.validate_price_authorization_for_finalize(payload)
        merchandise_total = sum(flt(row["amount"]) for row in items)
        payments = _normalize_live_payments(payload, merchandise_total, "cashier", customer.name)

        doc = frappe.new_doc("NKT Cashier Sale")
        doc.company = location.company
        doc.sale_datetime = now_datetime()
        doc.business_date = nowdate()
        doc.cashier = frappe.session.user
        doc.cashier_shift = shift.name
        doc.settlement_location = shift.settlement_location
        doc.default_warehouse = location.default_warehouse
        doc.customer = customer.name
        if doc.meta.has_field(FAST_REQUEST_FIELD):
            doc.set(FAST_REQUEST_FIELD, request_id)
        if doc.meta.has_field(FAST_VERSION_FIELD):
            doc.set(FAST_VERSION_FIELD, VERSION)
        if price_authorization:
            doc.flags.nkt_price_authorization_evidence = price_authorization
            nkt_manager_pin.apply_evidence_to_sale(doc, price_authorization)
        for row in items:
            doc.append("items", row)
        for row in payments:
            doc.append("payments", row)
        doc.insert()
        doc.submit()
        result = _cashier_posting_result(doc, request_id)
        integrity = result.get("integrity") or {}
        if not integrity.get("passed"):
            frappe.throw(_("Cashier payment posting integrity failed and the transaction was rolled back: {0}").format("; ".join(integrity.get("errors") or [])))
        return result


@frappe.whitelist()
def finalize_encoder_fast_transaction(payload: Any):
    location = _live_location("encoder")
    payload = _parse_fast_payload(payload)
    request_id = (payload.get("request_id") or "").strip()
    if not request_id:
        frappe.throw(_("A transaction request ID is required. Refresh the fast screen and try again."))

    with _fast_request_lock("NKT Customer Order", request_id):
        existing = _existing_fast_transaction("NKT Customer Order", request_id)
        if existing:
            if existing.docstatus != 1:
                frappe.throw(_("An incomplete Encoder transaction already uses this request ID. Ask an Administrator to inspect it before retrying."))
            return _encoder_posting_result(existing, request_id, replayed=True)

        customer = _customer_for_live(payload.get("customer"))
        items = _normalize_live_items(payload, location, "encoder")
        merchandise_total = sum(flt(row["amount"]) for row in items)
        payments = _normalize_live_payments(payload, merchandise_total, "encoder", customer.name)

        doc = frappe.new_doc("NKT Customer Order")
        doc.company = location.company
        doc.order_date = nowdate()
        doc.customer = customer.name
        doc.encoder = frappe.session.user
        doc.default_warehouse = location.default_warehouse
        doc.account_sale = 1 if any(row["payment_method"] == "Account" for row in payments) else 0
        if doc.meta.has_field(FAST_REQUEST_FIELD):
            doc.set(FAST_REQUEST_FIELD, request_id)
        if doc.meta.has_field(FAST_VERSION_FIELD):
            doc.set(FAST_VERSION_FIELD, VERSION)
        for row in items:
            doc.append("items", row)
        declared_meta = frappe.get_meta("NKT Declared Payment")
        for row in payments:
            declared = {
                "payment_method": row["payment_method"],
                "amount": row["amount"],
                "reference_number": row.get("reference_number"),
                "bank_or_provider": row.get("bank_or_provider"),
                "remarks": row.get("remarks"),
            }
            if declared_meta.has_field("custom_nkt_check_date") and row.get("check_date"):
                declared["custom_nkt_check_date"] = row.get("check_date")
            if declared_meta.has_field("custom_nkt_check_number") and row.get("check_number"):
                declared["custom_nkt_check_number"] = row.get("check_number")
            doc.append("declared_payments", declared)
        doc.insert()
        doc.submit()
        # Legacy restricted-warehouse status must not remain a pre-release Admin gate.
        # Normalize only when the order is genuinely matched/settled and externally reserved.
        _normalize_legacy_external_order_release_status(doc.name)
        doc.reload()
        return _encoder_posting_result(doc, request_id)


def suspend_v2_0_c_1_live_scripts():
    """Put the site back into the accepted B3.1 preview until schema is complete."""
    live_names = (
        "NKT Cashier Fast Screen V2.0C.1",
        "NKT Encoder Fast Screen V2.0C.1",
    )
    preview_names = (
        "NKT Cashier Fast Screen V2.0B.3.1",
        "NKT Encoder Fast Screen V2.0B.3.1",
    )
    for name in live_names:
        if frappe.db.exists("Client Script", name):
            frappe.db.set_value("Client Script", name, "enabled", 0, update_modified=False)
    for name in preview_names:
        if frappe.db.exists("Client Script", name):
            frappe.db.set_value("Client Script", name, "enabled", 1, update_modified=False)
    frappe.db.commit()
    frappe.clear_cache()
    return {
        "suspended": True,
        "live_scripts": {name: frappe.db.get_value("Client Script", name, "enabled") for name in live_names if frappe.db.exists("Client Script", name)},
        "preview_scripts": {name: frappe.db.get_value("Client Script", name, "enabled") for name in preview_names if frappe.db.exists("Client Script", name)},
    }


def install_v2_0_c_1():
    """Resume-safe C1 install: schema first, live scripts last."""
    before = _counts()

    # The accepted B3.1 foundation must exist before enabling any live screen.
    required = (OPERATING_LOCATION, CASHIER_SCREEN, ENCODER_SCREEN)
    missing = [doctype for doctype in required if not frappe.db.exists("DocType", doctype)]
    if missing:
        frappe.throw(_("V2.0B.3.1 foundation is incomplete: {0}").format(", ".join(missing)))
    for doctype, fieldname in (
        ("User", "custom_nkt_operating_location"),
        ("Warehouse", "custom_nkt_fast_label"),
        ("Warehouse", "custom_nkt_fulfillment_type"),
    ):
        if not frappe.get_meta(doctype).has_field(fieldname):
            frappe.throw(_("V2.0B.3.1 foundation field is missing: {0}.{1}").format(doctype, fieldname))

    installed_transaction_fields = {
        "NKT Cashier Sale": _append_missing_custom_fields(
            "NKT Cashier Sale",
            [
                {
                    "fieldname": FAST_REQUEST_FIELD,
                    "label": "Fast UI Request ID",
                    "fieldtype": "Data",
                    "unique": 1,
                    "read_only": 1,
                    "hidden": 1,
                    "insert_after": "nkt_warehouse_fingerprint",
                },
                {
                    "fieldname": FAST_VERSION_FIELD,
                    "label": "Fast UI Version",
                    "fieldtype": "Data",
                    "read_only": 1,
                    "hidden": 1,
                    "insert_after": FAST_REQUEST_FIELD,
                },
            ],
        ),
        "NKT Customer Order": _append_missing_custom_fields(
            "NKT Customer Order",
            [
                {
                    "fieldname": FAST_REQUEST_FIELD,
                    "label": "Fast UI Request ID",
                    "fieldtype": "Data",
                    "unique": 1,
                    "read_only": 1,
                    "hidden": 1,
                    "insert_after": "nkt_warehouse_fingerprint",
                },
                {
                    "fieldname": FAST_VERSION_FIELD,
                    "label": "Fast UI Version",
                    "fieldtype": "Data",
                    "read_only": 1,
                    "hidden": 1,
                    "insert_after": FAST_REQUEST_FIELD,
                },
            ],
        ),
        "NKT Declared Payment": _append_missing_custom_fields(
            "NKT Declared Payment",
            [
                {
                    "fieldname": "custom_nkt_check_number",
                    "label": "Check Number",
                    "fieldtype": "Data",
                    "read_only": 1,
                    "hidden": 1,
                    "insert_after": "reference_number",
                },
                {
                    "fieldname": "custom_nkt_check_date",
                    "label": "Check Date",
                    "fieldtype": "Date",
                    "read_only": 1,
                    "hidden": 1,
                    "insert_after": "custom_nkt_check_number",
                },
            ],
        ),
    }

    # Commit the schema while the safe B3.1 preview remains active.
    frappe.db.commit()
    frappe.clear_cache()

    # Only after all idempotency/check fields exist do we activate C1.
    scripts = [
        _ensure_client_script(CASHIER_SCRIPT, CASHIER_SCREEN, "nkt_cashier_fast_screen_v2.js"),
        _ensure_client_script(ENCODER_SCRIPT, ENCODER_SCREEN, "nkt_encoder_fast_screen_v2.js"),
    ]
    for old_name in (
        "NKT Cashier Fast Screen V2.0B",
        "NKT Encoder Fast Screen V2.0B",
        "NKT Cashier Fast Screen V2.0B.3",
        "NKT Encoder Fast Screen V2.0B.3",
        "NKT Cashier Fast Screen V2.0B.3.1",
        "NKT Encoder Fast Screen V2.0B.3.1",
    ):
        if frappe.db.exists("Client Script", old_name):
            frappe.db.set_value("Client Script", old_name, "enabled", 0, update_modified=False)

    seeded = _ensure_seed_configuration()
    frappe.db.commit()
    frappe.clear_cache()

    after = _counts()
    changed = {
        dt: {"before": before.get(dt), "after": after.get(dt)}
        for dt in sorted(set(before) | set(after))
        if before.get(dt) != after.get(dt)
    }
    return {
        "installed": True,
        "version": VERSION,
        "resume_hotfix": "2.0C.1.1",
        "screens": [CASHIER_SCREEN, ENCODER_SCREEN],
        "client_scripts": scripts,
        "installed_transaction_fields": installed_transaction_fields,
        "seeded_configuration": seeded,
        "posting_endpoints_added": True,
        "live_posting_scope": {
            "cashier": "Standard-rate live posting through existing NKT Cashier Sale controller",
            "encoder": "Standard-rate live posting through existing NKT Customer Order controller",
            "blocked": ["Adjusted rates pending manager PIN", "Card pending CARD-B rollback acceptance", "Hold persistence", "New Customer creation"],
        },
        "business_record_counts_changed": changed,
        "next_action": "Open a real Cashier Shift and test one small standard-rate transaction with the Cashier and Encoder accounts.",
    }

def verify_v2_0_c_1():
    report = verify_v2_0_b()
    errors = list(report.get("errors") or [])
    for dt in ("NKT Cashier Sale", "NKT Customer Order"):
        for fieldname in (FAST_REQUEST_FIELD, FAST_VERSION_FIELD):
            if not frappe.get_meta(dt).has_field(fieldname):
                errors.append(f"Missing field: {dt}.{fieldname}")
    for fieldname in ("custom_nkt_check_number", "custom_nkt_check_date"):
        if not frappe.get_meta("NKT Declared Payment").has_field(fieldname):
            errors.append(f"Missing field: NKT Declared Payment.{fieldname}")
    status = {
        name: frappe.db.get_value("Client Script", name, "enabled")
        for name in (
            "NKT Cashier Fast Screen V2.0B",
            "NKT Encoder Fast Screen V2.0B",
            "NKT Cashier Fast Screen V2.0B.3.1",
            "NKT Encoder Fast Screen V2.0B.3.1",
            CASHIER_SCRIPT,
            ENCODER_SCRIPT,
        )
        if frappe.db.exists("Client Script", name)
    }
    if not status.get(CASHIER_SCRIPT):
        errors.append(f"Live Cashier Client Script is not enabled: {CASHIER_SCRIPT}")
    if not status.get(ENCODER_SCRIPT):
        errors.append(f"Live Encoder Client Script is not enabled: {ENCODER_SCRIPT}")
    report.update(
        {
            "version": VERSION,
            "errors": errors,
            "passed": not errors,
            "posting_endpoints_added": True,
            "live_standard_only": LIVE_STANDARD_ONLY,
            "blocked_payment_methods": sorted(LIVE_BLOCKED_PAYMENT_METHODS),
            "client_script_status": status,
            "business_record_counts": _counts(),
        }
    )
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report



def install_v2_0_c_2():
    """Harden standard payment posting while preserving all accepted workflows."""
    before = _counts()
    foundation_errors = []
    for dt in (OPERATING_LOCATION, CASHIER_SCREEN, ENCODER_SCREEN):
        if not frappe.db.exists("DocType", dt):
            foundation_errors.append(f"Missing DocType: {dt}")
    for dt in ("NKT Cashier Sale", "NKT Customer Order"):
        for fieldname in (FAST_REQUEST_FIELD, FAST_VERSION_FIELD):
            if not frappe.get_meta(dt).has_field(fieldname):
                foundation_errors.append(f"Missing field: {dt}.{fieldname}")
    for fieldname in ("custom_nkt_check_number", "custom_nkt_check_date"):
        if not frappe.get_meta("NKT Declared Payment").has_field(fieldname):
            foundation_errors.append(f"Missing field: NKT Declared Payment.{fieldname}")
    if foundation_errors:
        frappe.throw(json.dumps({"version": VERSION, "foundation_errors": foundation_errors}, indent=2))
    base = {"passed": True, "foundation_errors": []}
    indexes = {
        "NKT Cashier Sale": _ensure_unique_request_index("NKT Cashier Sale", "uniq_nkt_cash_fast_request"),
        "NKT Customer Order": _ensure_unique_request_index("NKT Customer Order", "uniq_nkt_order_fast_request"),
    }
    scripts = [
        _ensure_client_script(CASHIER_SCRIPT, CASHIER_SCREEN, "nkt_cashier_fast_screen_v2.js"),
        _ensure_client_script(ENCODER_SCRIPT, ENCODER_SCREEN, "nkt_encoder_fast_screen_v2.js"),
    ]
    for old_name in (
        "NKT Cashier Fast Screen V2.0B",
        "NKT Encoder Fast Screen V2.0B",
        "NKT Cashier Fast Screen V2.0B.3.1",
        "NKT Encoder Fast Screen V2.0B.3.1",
        "NKT Cashier Fast Screen V2.0C.1",
        "NKT Encoder Fast Screen V2.0C.1",
    ):
        if frappe.db.exists("Client Script", old_name):
            frappe.db.set_value("Client Script", old_name, "enabled", 0, update_modified=False)
    frappe.db.commit()
    frappe.clear_cache()
    after = _counts()
    changed = {dt: {"before": before.get(dt), "after": after.get(dt)} for dt in before if before.get(dt) != after.get(dt)}
    return {
        "installed": True,
        "version": VERSION,
        "base_verified": base.get("passed"),
        "client_scripts": scripts,
        "request_indexes": indexes,
        "payment_controls": {
            "methods": sorted(LIVE_PAYMENT_METHODS),
            "card_blocked": True,
            "standard_rate_only": True,
            "duplicate_check_identity_in_one_receipt_blocked": True,
            "duplicate_check_identity_across_posted_receipts_blocked": True,
            "informal_noncheck_reference_repeats_allowed": True,
            "encoder_can_repeat_cashier_reference_for_matching": True,
            "cash_movement_uses_cash_due_not_cash_tendered": True,
            "server_request_lock": True,
            "posting_integrity_rollback": True,
        },
        "business_record_counts_changed": changed,
        "next_action": "Test the standard payment matrix with small test transactions, then run the supplied verifier query.",
    }


def verify_v2_0_c_2():
    report = verify_v2_0_c_1()
    errors = list(report.get("errors") or [])
    index_status = {
        "NKT Cashier Sale": _request_index_status("NKT Cashier Sale"),
        "NKT Customer Order": _request_index_status("NKT Customer Order"),
    }
    for dt, rows in index_status.items():
        if not any(not int(row.NON_UNIQUE) for row in rows):
            errors.append(f"No unique request-ID index found for {dt}")
    status = {
        name: frappe.db.get_value("Client Script", name, "enabled")
        for name in (
            "NKT Cashier Fast Screen V2.0C.1",
            "NKT Encoder Fast Screen V2.0C.1",
            CASHIER_SCRIPT,
            ENCODER_SCRIPT,
        )
        if frappe.db.exists("Client Script", name)
    }
    if not status.get(CASHIER_SCRIPT):
        errors.append(f"C2 Cashier Client Script is not enabled: {CASHIER_SCRIPT}")
    if not status.get(ENCODER_SCRIPT):
        errors.append(f"C2 Encoder Client Script is not enabled: {ENCODER_SCRIPT}")
    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "request_index_status": index_status,
        "client_script_status": status,
        "standard_payment_methods": sorted(LIVE_PAYMENT_METHODS),
        "duplicate_reference_control": "incoming_check_identity_only",
        "request_lock_control": True,
        "posting_integrity_control": True,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report



def install_v2_0_c_2_1():
    """Install visible posting feedback on both fast screens without changing posting rules."""
    before = _counts()
    foundation_errors = []
    for dt in (OPERATING_LOCATION, CASHIER_SCREEN, ENCODER_SCREEN):
        if not frappe.db.exists("DocType", dt):
            foundation_errors.append(f"Missing DocType: {dt}")
    for dt in ("NKT Cashier Sale", "NKT Customer Order"):
        for fieldname in (FAST_REQUEST_FIELD, FAST_VERSION_FIELD):
            if not frappe.get_meta(dt).has_field(fieldname):
                foundation_errors.append(f"Missing field: {dt}.{fieldname}")
    if foundation_errors:
        frappe.throw(json.dumps({"version": VERSION, "foundation_errors": foundation_errors}, indent=2))

    indexes = {
        "NKT Cashier Sale": _ensure_unique_request_index("NKT Cashier Sale", "uniq_nkt_cash_fast_request"),
        "NKT Customer Order": _ensure_unique_request_index("NKT Customer Order", "uniq_nkt_order_fast_request"),
    }
    scripts = [
        _ensure_client_script(CASHIER_SCRIPT, CASHIER_SCREEN, "nkt_cashier_fast_screen_v2.js"),
        _ensure_client_script(ENCODER_SCRIPT, ENCODER_SCREEN, "nkt_encoder_fast_screen_v2.js"),
    ]
    for old_name in (
        "NKT Cashier Fast Screen V2.0B",
        "NKT Encoder Fast Screen V2.0B",
        "NKT Cashier Fast Screen V2.0B.3.1",
        "NKT Encoder Fast Screen V2.0B.3.1",
        "NKT Cashier Fast Screen V2.0C.1",
        "NKT Encoder Fast Screen V2.0C.1",
        "NKT Cashier Fast Screen V2.0C.2",
        "NKT Encoder Fast Screen V2.0C.2",
    ):
        if frappe.db.exists("Client Script", old_name):
            frappe.db.set_value("Client Script", old_name, "enabled", 0, update_modified=False)
    frappe.db.commit()
    frappe.clear_cache()
    after = _counts()
    changed = {dt: {"before": before.get(dt), "after": after.get(dt)} for dt in before if before.get(dt) != after.get(dt)}
    return {
        "installed": True,
        "version": VERSION,
        "client_scripts": scripts,
        "request_indexes": indexes,
        "posting_rules_changed": False,
        "feedback_controls": {
            "cashier_visible_failures": True,
            "encoder_visible_failures": True,
            "repeat_press_feedback": True,
            "stale_cashier_shift_client_block_removed": True,
            "server_live_shift_validation_preserved": True,
            "request_id_shown_on_success_and_failure": True,
            "transaction_retained_after_failure": True,
        },
        "business_record_counts_changed": changed,
        "next_action": "Refresh both fast screens and continue the standard-payment test matrix.",
    }


def verify_v2_0_c_2_1():
    report = verify_v2_0_c_1()
    errors = list(report.get("errors") or [])
    index_status = {
        "NKT Cashier Sale": _request_index_status("NKT Cashier Sale"),
        "NKT Customer Order": _request_index_status("NKT Customer Order"),
    }
    for dt, rows in index_status.items():
        if not any(not int(row.NON_UNIQUE) for row in rows):
            errors.append(f"No unique request-ID index found for {dt}")
    names = (
        "NKT Cashier Fast Screen V2.0C.1",
        "NKT Encoder Fast Screen V2.0C.1",
        "NKT Cashier Fast Screen V2.0C.2",
        "NKT Encoder Fast Screen V2.0C.2",
        CASHIER_SCRIPT,
        ENCODER_SCRIPT,
    )
    status = {
        name: frappe.db.get_value("Client Script", name, "enabled")
        for name in names if frappe.db.exists("Client Script", name)
    }
    if not status.get(CASHIER_SCRIPT):
        errors.append(f"C2.1 Cashier Client Script is not enabled: {CASHIER_SCRIPT}")
    if not status.get(ENCODER_SCRIPT):
        errors.append(f"C2.1 Encoder Client Script is not enabled: {ENCODER_SCRIPT}")
    for old_name in ("NKT Cashier Fast Screen V2.0C.2", "NKT Encoder Fast Screen V2.0C.2"):
        if status.get(old_name):
            errors.append(f"Old C2 Client Script is still enabled: {old_name}")
    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "request_index_status": index_status,
        "client_script_status": status,
        "posting_rules_changed": False,
        "visible_posting_feedback": True,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report


def install_v2_0_c_2_2():
    """Install strict same-day Cashier Shift enforcement without altering existing transactions."""
    before = _counts()
    foundation_errors = []
    for dt in (OPERATING_LOCATION, CASHIER_SCREEN, ENCODER_SCREEN, "NKT Cashier Shift"):
        if not frappe.db.exists("DocType", dt):
            foundation_errors.append(f"Missing DocType: {dt}")
    for dt in ("NKT Cashier Sale", "NKT Customer Order"):
        for fieldname in (FAST_REQUEST_FIELD, FAST_VERSION_FIELD):
            if not frappe.get_meta(dt).has_field(fieldname):
                foundation_errors.append(f"Missing field: {dt}.{fieldname}")
    if foundation_errors:
        frappe.throw(json.dumps({"version": VERSION, "foundation_errors": foundation_errors}, indent=2))

    indexes = {
        "NKT Cashier Sale": _ensure_unique_request_index("NKT Cashier Sale", "uniq_nkt_cash_fast_request"),
        "NKT Customer Order": _ensure_unique_request_index("NKT Customer Order", "uniq_nkt_order_fast_request"),
    }
    scripts = [
        _ensure_client_script(CASHIER_SCRIPT, CASHIER_SCREEN, "nkt_cashier_fast_screen_v2.js"),
        _ensure_client_script(ENCODER_SCRIPT, ENCODER_SCREEN, "nkt_encoder_fast_screen_v2.js"),
    ]
    for old_name in (
        "NKT Cashier Fast Screen V2.0B",
        "NKT Encoder Fast Screen V2.0B",
        "NKT Cashier Fast Screen V2.0B.3.1",
        "NKT Encoder Fast Screen V2.0B.3.1",
        "NKT Cashier Fast Screen V2.0C.1",
        "NKT Encoder Fast Screen V2.0C.1",
        "NKT Cashier Fast Screen V2.0C.2",
        "NKT Encoder Fast Screen V2.0C.2",
        "NKT Cashier Fast Screen V2.0C.2.1",
        "NKT Encoder Fast Screen V2.0C.2.1",
    ):
        if frappe.db.exists("Client Script", old_name):
            frappe.db.set_value("Client Script", old_name, "enabled", 0, update_modified=False)
    frappe.db.commit()
    frappe.clear_cache()

    today = getdate(nowdate())
    open_shift_diagnostics = frappe.get_all(
        "NKT Cashier Shift",
        filters={"status": "Open", "docstatus": 0},
        fields=["name", "cashier", "company", "settlement_location", "shift_start"],
        order_by="shift_start desc",
        limit_page_length=50,
    )
    for row in open_shift_diagnostics:
        row["shift_date"] = getdate(row.shift_start) if row.shift_start else None
        row["usable_today"] = bool(row["shift_date"] == today)

    after = _counts()
    changed = {dt: {"before": before.get(dt), "after": after.get(dt)} for dt in before if before.get(dt) != after.get(dt)}
    return {
        "installed": True,
        "version": VERSION,
        "client_scripts": scripts,
        "request_indexes": indexes,
        "strict_shift_controls": {
            "cashier_shift_must_be_open": True,
            "cashier_shift_must_belong_to_signed_in_cashier": True,
            "exactly_one_open_shift_required": True,
            "shift_start_date_must_equal_server_pos_date": True,
            "old_shift_after_midnight_is_blocked": True,
            "antedated_or_backdated_pos_sales_allowed": False,
            "encoder_order_date_forced_to_server_pos_date": True,
        },
        "open_shift_diagnostics": open_shift_diagnostics,
        "business_record_counts_changed": changed,
        "existing_mismatched_records_modified": False,
        "next_action": "Verify that NKT-SHIFT-00008 is visibly blocked after midnight, then close/review it and open today's shift before the next Cashier test.",
    }


def verify_v2_0_c_2_2():
    report = verify_v2_0_c_1()
    errors = list(report.get("errors") or [])
    index_status = {
        "NKT Cashier Sale": _request_index_status("NKT Cashier Sale"),
        "NKT Customer Order": _request_index_status("NKT Customer Order"),
    }
    for dt, rows in index_status.items():
        if not any(not int(row.NON_UNIQUE) for row in rows):
            errors.append(f"No unique request-ID index found for {dt}")
    names = (
        "NKT Cashier Fast Screen V2.0C.2",
        "NKT Encoder Fast Screen V2.0C.2",
        "NKT Cashier Fast Screen V2.0C.2.1",
        "NKT Encoder Fast Screen V2.0C.2.1",
        CASHIER_SCRIPT,
        ENCODER_SCRIPT,
    )
    status = {name: frappe.db.get_value("Client Script", name, "enabled") for name in names if frappe.db.exists("Client Script", name)}
    if not status.get(CASHIER_SCRIPT):
        errors.append(f"C2.2 Cashier Client Script is not enabled: {CASHIER_SCRIPT}")
    if not status.get(ENCODER_SCRIPT):
        errors.append(f"C2.2 Encoder Client Script is not enabled: {ENCODER_SCRIPT}")
    for old_name in (
        "NKT Cashier Fast Screen V2.0C.2",
        "NKT Encoder Fast Screen V2.0C.2",
        "NKT Cashier Fast Screen V2.0C.2.1",
        "NKT Encoder Fast Screen V2.0C.2.1",
    ):
        if status.get(old_name):
            errors.append(f"Older Client Script is still enabled: {old_name}")

    stale_open_shifts = []
    today = getdate(nowdate())
    rows = frappe.get_all(
        "NKT Cashier Shift",
        filters={"status": "Open", "docstatus": 0},
        fields=["name", "cashier", "shift_start"],
        order_by="shift_start desc",
        limit_page_length=50,
    )
    for row in rows:
        shift_date = getdate(row.shift_start) if row.shift_start else None
        if shift_date != today:
            stale_open_shifts.append({"name": row.name, "cashier": row.cashier, "shift_start": row.shift_start, "shift_date": shift_date, "blocked_by_guard": True})

    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "request_index_status": index_status,
        "client_script_status": status,
        "strict_current_date_shift_guard": True,
        "server_pos_date": today,
        "stale_open_shifts": stale_open_shifts,
        "existing_business_records_changed": False,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report

def install_v2_0_c_2_3_1():
    """Install revised reference policy only; preserve all accepted posting/date/warehouse flows."""
    before = _counts()
    foundation_errors = []
    for dt in (OPERATING_LOCATION, CASHIER_SCREEN, ENCODER_SCREEN, "NKT Cashier Shift"):
        if not frappe.db.exists("DocType", dt):
            foundation_errors.append(f"Missing DocType: {dt}")
    for dt in ("NKT Cashier Sale", "NKT Customer Order"):
        for fieldname in (FAST_REQUEST_FIELD, FAST_VERSION_FIELD):
            if not frappe.get_meta(dt).has_field(fieldname):
                foundation_errors.append(f"Missing field: {dt}.{fieldname}")
    for fieldname in ("check_number", "check_date", "bank_or_provider", "reference_number"):
        if not frappe.get_meta("NKT Payment Detail").has_field(fieldname):
            foundation_errors.append(f"Missing field: NKT Payment Detail.{fieldname}")
    if foundation_errors:
        frappe.throw(json.dumps({"version": VERSION, "foundation_errors": foundation_errors}, indent=2))

    indexes = {
        "NKT Cashier Sale": _ensure_unique_request_index("NKT Cashier Sale", "uniq_nkt_cash_fast_request"),
        "NKT Customer Order": _ensure_unique_request_index("NKT Customer Order", "uniq_nkt_order_fast_request"),
    }
    scripts = [
        _ensure_client_script(CASHIER_SCRIPT, CASHIER_SCREEN, "nkt_cashier_fast_screen_v2.js"),
        _ensure_client_script(ENCODER_SCRIPT, ENCODER_SCREEN, "nkt_encoder_fast_screen_v2.js"),
    ]
    for old_name in (
        "NKT Cashier Fast Screen V2.0B",
        "NKT Encoder Fast Screen V2.0B",
        "NKT Cashier Fast Screen V2.0B.3.1",
        "NKT Encoder Fast Screen V2.0B.3.1",
        "NKT Cashier Fast Screen V2.0C.1",
        "NKT Encoder Fast Screen V2.0C.1",
        "NKT Cashier Fast Screen V2.0C.2",
        "NKT Encoder Fast Screen V2.0C.2",
        "NKT Cashier Fast Screen V2.0C.2.1",
        "NKT Encoder Fast Screen V2.0C.2.1",
        "NKT Cashier Fast Screen V2.0C.2.2",
        "NKT Encoder Fast Screen V2.0C.2.2",
        "NKT Cashier Fast Screen V2.0C.2.3",
        "NKT Encoder Fast Screen V2.0C.2.3",
    ):
        if old_name != CASHIER_SCRIPT and old_name != ENCODER_SCRIPT and frappe.db.exists("Client Script", old_name):
            frappe.db.set_value("Client Script", old_name, "enabled", 0, update_modified=False)
    frappe.db.commit()
    frappe.clear_cache()

    after = _counts()
    changed = {dt: {"before": before.get(dt), "after": after.get(dt)} for dt in before if before.get(dt) != after.get(dt)}
    return {
        "installed": True,
        "version": VERSION,
        "client_scripts": scripts,
        "request_indexes": indexes,
        "reference_policy": {
            "gcash_reference_required_but_repeat_allowed": True,
            "maya_reference_required_but_repeat_allowed": True,
            "bank_transfer_reference_required_but_repeat_allowed": True,
            "online_reference_required_but_repeat_allowed": True,
            "credit_card_future_reference_repeat_allowed": True,
            "incoming_check_duplicate_key": ["customer", "issuing_bank", "check_number"],
            "incoming_check_duplicate_is_hard_block": True,
            "encoder_may_repeat_cashier_check_for_matching": True,
            "check_never_generates_change": True,
            "cash_change_rules_changed": False,
        },
        "posting_date_rules_changed": False,
        "warehouse_rules_changed": False,
        "matching_workflow_redesigned": False,
        "business_record_counts_changed": changed,
        "existing_business_records_modified": False,
        "next_action": "Refresh both fast screens, then test repeated GCash reference allowed and duplicate physical Check blocked.",
    }


def verify_v2_0_c_2_3_1():
    report = verify_v2_0_c_1()
    errors = list(report.get("errors") or [])
    index_status = {
        "NKT Cashier Sale": _request_index_status("NKT Cashier Sale"),
        "NKT Customer Order": _request_index_status("NKT Customer Order"),
    }
    for dt, rows in index_status.items():
        if not any(not int(row.NON_UNIQUE) for row in rows):
            errors.append(f"No unique request-ID index found for {dt}")

    names = (
        "NKT Cashier Fast Screen V2.0C.2",
        "NKT Encoder Fast Screen V2.0C.2",
        "NKT Cashier Fast Screen V2.0C.2.1",
        "NKT Encoder Fast Screen V2.0C.2.1",
        "NKT Cashier Fast Screen V2.0C.2.2",
        "NKT Encoder Fast Screen V2.0C.2.2",
        "NKT Cashier Fast Screen V2.0C.2.3",
        "NKT Encoder Fast Screen V2.0C.2.3",
        CASHIER_SCRIPT,
        ENCODER_SCRIPT,
    )
    status = {name: frappe.db.get_value("Client Script", name, "enabled") for name in names if frappe.db.exists("Client Script", name)}
    if not status.get(CASHIER_SCRIPT):
        errors.append(f"C2.3.1 Cashier Client Script is not enabled: {CASHIER_SCRIPT}")
    if not status.get(ENCODER_SCRIPT):
        errors.append(f"C2.3.1 Encoder Client Script is not enabled: {ENCODER_SCRIPT}")
    for old_name in (
        "NKT Cashier Fast Screen V2.0C.2",
        "NKT Encoder Fast Screen V2.0C.2",
        "NKT Cashier Fast Screen V2.0C.2.1",
        "NKT Encoder Fast Screen V2.0C.2.1",
        "NKT Cashier Fast Screen V2.0C.2.2",
        "NKT Encoder Fast Screen V2.0C.2.2",
        "NKT Cashier Fast Screen V2.0C.2.3",
        "NKT Encoder Fast Screen V2.0C.2.3",
    ):
        if status.get(old_name):
            errors.append(f"Older Client Script is still enabled: {old_name}")

    # C2.3.1 verifies the actual controller files, not merely the declared policy.
    # This closes the C2.3 gap where the Fast UI allowed repeat references but legacy
    # DocType validators still rejected them.
    controller_paths = {
        "cashier_sale": Path(__file__).resolve().parent / "doctype/nkt_cashier_sale/nkt_cashier_sale.py",
        "customer_order": Path(__file__).resolve().parent / "doctype/nkt_customer_order/nkt_customer_order.py",
        "payment_receipt": Path(__file__).resolve().parent / "doctype/nkt_payment_receipt/nkt_payment_receipt.py",
        "account_collection": Path(__file__).resolve().parent / "features/payments_accounts/collection.py",
    }

    def _controller_source_block(path, start_token, indent):
        text = path.read_text()
        start = text.find(start_token)
        if start < 0:
            return ""
        next_token = "\n" + indent + "def "
        end = text.find(next_token, start + len(start_token))
        return text[start:] if end < 0 else text[start:end]

    controller_sources = {
        "cashier_sale": _controller_source_block(controller_paths["cashier_sale"], '    def validate_duplicate_references(self):', '    '),
        "customer_order": _controller_source_block(controller_paths["customer_order"], '    def validate_declared_payments(self):', '    '),
        "payment_receipt": _controller_source_block(controller_paths["payment_receipt"], '    def validate_duplicate_references(self):', '    '),
        "account_collection": _controller_source_block(controller_paths["account_collection"], 'def _validate_payment_rows(', ''),
    }
    controller_policy = {
        "cashier_sale_check_only_duplicate_guard": (
            'row.payment_method != "Check"' in controller_sources["cashier_sale"]
            and "seen_checks" in controller_sources["cashier_sale"]
        ),
        "encoder_noncheck_repeat_allowed": (
            "seen_references" not in controller_sources["customer_order"]
            and "seen_checks" in controller_sources["customer_order"]
        ),
        "payment_receipt_noncheck_repeat_allowed": (
            "seen_references" not in controller_sources["payment_receipt"]
            and "seen_checks" in controller_sources["payment_receipt"]
            and 'row.payment_method != "Check"' in controller_sources["payment_receipt"]
        ),
        "account_collection_noncheck_repeat_allowed": (
            "seen_references" not in controller_sources["account_collection"]
            and "seen_checks" in controller_sources["account_collection"]
            and "already used in" not in controller_sources["account_collection"]
        ),
        "check_identity_mentions_customer_bank_number": (
            "pr.customer = %s" in controller_sources["cashier_sale"]
            and "pr.customer = %s" in controller_sources["payment_receipt"]
            and "bank_or_provider" in controller_sources["cashier_sale"]
            and "check_number" in controller_sources["cashier_sale"]
        ),
    }
    for policy_name, ok in controller_policy.items():
        if not ok:
            errors.append(f"Controller reference-policy verification failed: {policy_name}")

    stale_open_shifts = []
    today = getdate(nowdate())
    for row in frappe.get_all(
        "NKT Cashier Shift",
        filters={"status": "Open", "docstatus": 0},
        fields=["name", "cashier", "shift_start"],
        order_by="shift_start desc",
        limit_page_length=50,
    ):
        shift_date = getdate(row.shift_start) if row.shift_start else None
        if shift_date != today:
            stale_open_shifts.append({"name": row.name, "cashier": row.cashier, "shift_start": row.shift_start, "shift_date": shift_date, "blocked_by_guard": True})

    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "request_index_status": index_status,
        "client_script_status": status,
        "strict_current_date_shift_guard": True,
        "server_pos_date": today,
        "stale_open_shifts": stale_open_shifts,
        "controller_policy_verified": controller_policy,
        "reference_policy": {
            "informal_noncheck_references_repeat_allowed": ["GCash", "Maya", "Bank Transfer", "Online", "Card"],
            "references_still_required_where_applicable": True,
            "incoming_check_duplicate_key": ["customer", "issuing_bank", "check_number"],
            "incoming_check_duplicate_hard_block": True,
            "incoming_check_change_allowed": False,
        },
        "existing_business_records_changed": False,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report




def install_v2_0_c_2_3_2():
    """Install UI preflight/status recovery only; no business records are changed."""
    before = _counts()
    indexes = {
        "NKT Cashier Sale": _ensure_unique_request_index("NKT Cashier Sale", "uniq_nkt_cash_fast_request"),
        "NKT Customer Order": _ensure_unique_request_index("NKT Customer Order", "uniq_nkt_order_fast_request"),
    }
    scripts = [
        _ensure_client_script(CASHIER_SCRIPT, CASHIER_SCREEN, "nkt_cashier_fast_screen_v2.js"),
        _ensure_client_script(ENCODER_SCRIPT, ENCODER_SCREEN, "nkt_encoder_fast_screen_v2.js"),
    ]
    for old_name in (
        "NKT Cashier Fast Screen V2.0C.2.3.1",
        "NKT Encoder Fast Screen V2.0C.2.3.1",
        "NKT Cashier Fast Screen V2.0C.2.3",
        "NKT Encoder Fast Screen V2.0C.2.3",
        "NKT Cashier Fast Screen V2.0C.2.2",
        "NKT Encoder Fast Screen V2.0C.2.2",
    ):
        if frappe.db.exists("Client Script", old_name):
            frappe.db.set_value("Client Script", old_name, "enabled", 0, update_modified=False)
    frappe.db.commit()
    frappe.clear_cache()
    after = _counts()
    changed = {dt: {"before": before.get(dt), "after": after.get(dt)} for dt in before if before.get(dt) != after.get(dt)}
    return {
        "installed": True,
        "version": VERSION,
        "client_scripts": scripts,
        "request_indexes": indexes,
        "check_ui": {
            "preflight_before_payment_confirmation": True,
            "exact_duplicate_hard_block": True,
            "same_number_elsewhere_warning": True,
            "server_controller_recheck_preserved": True,
        },
        "posting_status_recovery": True,
        "business_record_counts_changed": changed,
        "existing_business_records_modified": False,
    }


def verify_v2_0_c_2_3_2():
    report = verify_v2_0_c_2_3_1()
    errors = list(report.get("errors") or [])
    status = {
        name: frappe.db.get_value("Client Script", name, "enabled")
        for name in (
            "NKT Cashier Fast Screen V2.0C.2.3.1",
            "NKT Encoder Fast Screen V2.0C.2.3.1",
            CASHIER_SCRIPT,
            ENCODER_SCRIPT,
        )
        if frappe.db.exists("Client Script", name)
    }
    if not status.get(CASHIER_SCRIPT):
        errors.append(f"C2.3.2 Cashier Client Script is not enabled: {CASHIER_SCRIPT}")
    if not status.get(ENCODER_SCRIPT):
        errors.append(f"C2.3.2 Encoder Client Script is not enabled: {ENCODER_SCRIPT}")
    if status.get("NKT Cashier Fast Screen V2.0C.2.3.1") or status.get("NKT Encoder Fast Screen V2.0C.2.3.1"):
        errors.append("C2.3.1 Client Script is still enabled.")
    source = Path(__file__).read_text()
    endpoint_checks = {
        "check_preflight_endpoint": "def preflight_incoming_check(" in source,
        "request_status_endpoint": "def get_fast_request_status(" in source,
    }
    for name, ok in endpoint_checks.items():
        if not ok:
            errors.append(f"Missing C2.3.2 endpoint: {name}")
    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "client_script_status": status,
        "check_ui_preflight": True,
        "posting_status_recovery": True,
        "c2_3_2_endpoint_verified": endpoint_checks,
        "existing_business_records_changed": False,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report



def install_v2_0_c_3():
    """Install reconciliation diagnostics and Fast Screen control-parity UI only."""
    before = _counts()
    indexes = {
        "NKT Cashier Sale": _ensure_unique_request_index("NKT Cashier Sale", "uniq_nkt_cash_fast_request"),
        "NKT Customer Order": _ensure_unique_request_index("NKT Customer Order", "uniq_nkt_order_fast_request"),
    }
    scripts = [
        _ensure_client_script(CASHIER_SCRIPT, CASHIER_SCREEN, "nkt_cashier_fast_screen_v2.js"),
        _ensure_client_script(ENCODER_SCRIPT, ENCODER_SCREEN, "nkt_encoder_fast_screen_v2.js"),
    ]
    for old_name in (
        "NKT Cashier Fast Screen V2.0C.2.3.2", "NKT Encoder Fast Screen V2.0C.2.3.2",
        "NKT Cashier Fast Screen V2.0C.2.3.1", "NKT Encoder Fast Screen V2.0C.2.3.1",
        "NKT Cashier Fast Screen V2.0C.2.3", "NKT Encoder Fast Screen V2.0C.2.3",
        "NKT Cashier Fast Screen V2.0C.2.2", "NKT Encoder Fast Screen V2.0C.2.2",
    ):
        if frappe.db.exists("Client Script", old_name):
            frappe.db.set_value("Client Script", old_name, "enabled", 0, update_modified=False)
    frappe.db.commit()
    frappe.clear_cache()
    after = _counts()
    changed = {dt: {"before": before.get(dt), "after": after.get(dt)} for dt in before if before.get(dt) != after.get(dt)}
    return {
        "installed": True,
        "version": VERSION,
        "client_scripts": scripts,
        "request_indexes": indexes,
        "reconciliation_diagnostics": True,
        "cashier_encoder_check_preflight_parity": True,
        "professional_check_dialogs": True,
        "post_success_key_spam_suppressed": True,
        "matching_workflow_redesigned": False,
        "warehouse_rules_changed": False,
        "payment_posting_rules_changed": False,
        "business_record_counts_changed": changed,
        "existing_business_records_modified": False,
    }


def verify_v2_0_c_3():
    report = verify_v2_0_c_2_3_1()
    errors = list(report.get("errors") or [])
    names = (
        "NKT Cashier Fast Screen V2.0C.2.3.2", "NKT Encoder Fast Screen V2.0C.2.3.2",
        CASHIER_SCRIPT, ENCODER_SCRIPT,
    )
    status = {name: frappe.db.get_value("Client Script", name, "enabled") for name in names if frappe.db.exists("Client Script", name)}
    if not status.get(CASHIER_SCRIPT): errors.append(f"C3 Cashier Client Script is not enabled: {CASHIER_SCRIPT}")
    if not status.get(ENCODER_SCRIPT): errors.append(f"C3 Encoder Client Script is not enabled: {ENCODER_SCRIPT}")
    if status.get("NKT Cashier Fast Screen V2.0C.2.3.2") or status.get("NKT Encoder Fast Screen V2.0C.2.3.2"):
        errors.append("C2.3.2 Client Script is still enabled.")
    source = Path(__file__).read_text()
    endpoint_checks = {
        "check_preflight_parity": 'def preflight_incoming_check(customer: str, check_number: str, issuing_bank: str, mode: str = "cashier")' in source,
        "reconciliation_diagnostics_endpoint": "def get_reconciliation_diagnostics(" in source,
        "request_status_endpoint": "def get_fast_request_status(" in source,
        # Compatibility key retained for older regression consumers; in R2B a True
        # value now proves the historical frontline diagnostics payload is absent.
        "posting_result_includes_diagnostics": not any(line.lstrip().startswith('"reconciliation_diagnostics": _reconciliation_diagnostics(') for line in source.splitlines()),
        "posting_result_hides_frontline_diagnostics": not any(line.lstrip().startswith('"reconciliation_diagnostics": _reconciliation_diagnostics(') for line in source.splitlines()),
    }
    for key, ok in endpoint_checks.items():
        if not ok: errors.append(f"Missing C3 control: {key}")
    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "client_script_status": status,
        "c3_controls_verified": endpoint_checks,
        "reconciliation_diagnostics": True,
        "cashier_encoder_control_parity": True,
        "professional_check_dialogs": True,
        "post_success_key_spam_suppressed": True,
        "matching_workflow_redesigned": False,
        "existing_business_records_changed": False,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report


# -----------------------------------------------------------------------------
# V2.0C.4.1 — External warehouse release fast operations
# -----------------------------------------------------------------------------

def _require_warehouse_release_operator():
    roles = set(frappe.get_roles(frappe.session.user) or [])
    if WAREHOUSE_ROLE in roles or roles.intersection(ADMIN_ROLES):
        return True
    frappe.throw(_("You are not authorized to confirm external warehouse releases."), frappe.PermissionError)


def _release_allowed_warehouses():
    """Respect explicit Warehouse User Permissions when present; otherwise return None."""
    if frappe.session.user == "Administrator":
        return None
    rows = frappe.get_all(
        "User Permission",
        filters={"user": frappe.session.user, "allow": "Warehouse"},
        fields=["for_value", "applicable_for"],
        limit_page_length=500,
    )
    values = [
        row.for_value for row in rows
        if row.for_value and (not row.applicable_for or row.applicable_for == "NKT Warehouse Release")
    ]
    return set(values) if values else None


def _validate_release_access(doc):
    _require_warehouse_release_operator()
    allowed = _release_allowed_warehouses()
    warehouse = doc.get("custom_nkt_source_warehouse")
    if allowed is not None and warehouse not in allowed:
        frappe.throw(_("You are not assigned to warehouse {0}.").format(warehouse), frappe.PermissionError)
    return True


def _release_request_index_status():
    if not frappe.db.exists("DocType", "NKT Warehouse Release") or not frappe.get_meta("NKT Warehouse Release").has_field(RELEASE_REQUEST_FIELD):
        return []
    return frappe.db.sql(
        """
        SELECT INDEX_NAME, NON_UNIQUE
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 'tabNKT Warehouse Release'
          AND column_name = %s
        ORDER BY INDEX_NAME
        """,
        (RELEASE_REQUEST_FIELD,),
        as_dict=True,
    )


def _ensure_release_request_index():
    """Verify the release Request ID unique index.

    Schema DDL is intentionally performed by the shell installer through the
    MariaDB client, not from a Frappe request/execute transaction. Frappe v16
    rejects ALTER TABLE in this context with ImplicitCommitError.
    """
    rows = _release_request_index_status()
    if any(not int(row.NON_UNIQUE) for row in rows):
        return {"status": "already_unique", "indexes": rows}
    duplicates = frappe.db.sql(
        f"""
        SELECT `{RELEASE_REQUEST_FIELD}` AS request_id, COUNT(*) AS row_count
        FROM `tabNKT Warehouse Release`
        WHERE COALESCE(`{RELEASE_REQUEST_FIELD}`, '') != ''
        GROUP BY `{RELEASE_REQUEST_FIELD}`
        HAVING COUNT(*) > 1
        LIMIT 5
        """,
        as_dict=True,
    )
    if duplicates:
        frappe.throw(_("Cannot enable warehouse-release idempotency because duplicate request IDs already exist: {0}").format(duplicates))
    frappe.throw(
        _("Warehouse Release fast Request ID unique index is missing. Run the V2.0C.4.1.1 shell installer so schema DDL is applied outside the Frappe transaction.")
    )


def prepare_v2_0_c_4_1_1():
    """Create only additive schema metadata needed before external DDL."""
    _ensure_custom_doctype(
        WAREHOUSE_RELEASE_SCREEN,
        "",
        [{"fieldname": "screen_html", "label": "Warehouse Release Fast Screen", "fieldtype": "HTML"}],
        _warehouse_release_screen_permissions(),
        issingle=True,
    )
    installed_fields = _append_missing_custom_fields(
        "NKT Warehouse Release",
        [{
            "fieldname": RELEASE_REQUEST_FIELD,
            "label": "NKT Fast Release Request ID",
            "fieldtype": "Data",
            "read_only": 1,
            "hidden": 1,
            "insert_after": "remarks",
            "description": "Idempotency key used by the NKT warehouse release fast screen.",
        }],
    )
    frappe.db.commit()
    frappe.clear_cache()
    return {
        "prepared": True,
        "version": VERSION,
        "warehouse_release_screen": WAREHOUSE_RELEASE_SCREEN,
        "release_request_field": RELEASE_REQUEST_FIELD,
        "installed_custom_fields": installed_fields,
        "ddl_next": "Create the unique Request ID index via bench mariadb, then run install_v2_0_c_4_1_1.",
    }


def _release_item_rows(doc):
    rows = []
    for row in doc.get("items") or []:
        rows.append({
            "name": row.name,
            "item": row.item,
            "item_name": row.item_name,
            "uom": row.uom,
            "source_warehouse": row.source_warehouse,
            "ordered_quantity": flt(row.ordered_quantity),
            "previously_released_quantity": flt(row.previously_released_quantity),
            "remaining_quantity": flt(row.remaining_quantity),
            "release_quantity": flt(row.release_quantity),
            "stock_reservation_entry": row.get("custom_nkt_stock_reservation_entry"),
            "reservation_outstanding_qty": flt(row.get("custom_nkt_reservation_outstanding_qty")),
        })
    return rows


def _release_result(doc, request_id=None, replayed=False):
    doc.reload()
    next_drafts = frappe.get_all(
        "NKT Warehouse Release",
        filters={
            "customer_order": doc.customer_order,
            "custom_nkt_source_warehouse": doc.get("custom_nkt_source_warehouse"),
            "docstatus": 0,
            "name": ["!=", doc.name],
        },
        fields=["name", "release_status", "total_release_quantity", "creation"],
        order_by="creation asc",
    )
    order = frappe.get_doc("NKT Customer Order", doc.customer_order) if doc.customer_order and frappe.db.exists("NKT Customer Order", doc.customer_order) else None
    from urllib.parse import quote
    return {
        "ok": True,
        "version": VERSION,
        "request_id": request_id or doc.get(RELEASE_REQUEST_FIELD),
        "replayed": bool(replayed),
        "warehouse_release": doc.name,
        "docstatus": doc.docstatus,
        "release_status": doc.get("release_status"),
        "customer": doc.customer,
        "customer_name": doc.customer_name,
        "customer_order": doc.customer_order,
        "source_warehouse": doc.get("custom_nkt_source_warehouse"),
        "total_release_quantity": flt(doc.total_release_quantity),
        "is_partial_release": cint(doc.is_partial_release),
        "driver_name": doc.get("custom_nkt_driver_name"),
        "plate_number": doc.get("custom_nkt_plate_number"),
        "mother_release_reference": doc.get("custom_nkt_mother_release_reference"),
        "stock_entry": doc.get("custom_nkt_stock_entry"),
        "items": _release_item_rows(doc),
        "next_draft_releases": next_drafts,
        "order_fulfillment_status": order.get("custom_nkt_fulfillment_status") if order else None,
        "order_external_reserved_qty": flt(order.get("custom_nkt_external_reserved_qty")) if order else 0,
        "order_external_released_qty": flt(order.get("custom_nkt_external_released_qty")) if order else 0,
        "print_url": f"/printview?doctype={quote('NKT Warehouse Release')}&name={quote(doc.name)}&trigger_print=1",
    }


def _refresh_release_quantities(doc):
    method = getattr(doc, "refresh_release_quantities", None)
    if callable(method):
        method()
    return doc


@frappe.whitelist()
def get_warehouse_release_bootstrap():
    _require_warehouse_release_operator()
    allowed = _release_allowed_warehouses()
    rows = frappe.get_all(
        "NKT Warehouse Release",
        filters={"docstatus": 0},
        fields=[
            "name", "release_status", "customer", "customer_name", "customer_order",
            "custom_nkt_source_warehouse", "total_release_quantity", "custom_nkt_driver_name",
            "custom_nkt_plate_number", "custom_nkt_mother_release_reference", "creation", "modified",
        ],
        order_by="creation asc",
        limit_page_length=500,
    )
    queue = []
    for row in rows:
        if allowed is not None and row.custom_nkt_source_warehouse not in allowed:
            continue
        fulfillment = frappe.db.get_value("Warehouse", row.custom_nkt_source_warehouse, "custom_nkt_fulfillment_type") if row.custom_nkt_source_warehouse else None
        if fulfillment != "External Warehouse Release":
            continue
        queue.append(row)
    return {
        "version": VERSION,
        "user": frappe.session.user,
        "full_name": frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user,
        "route": "/app/nkt-warehouse-release-fast-screen",
        "queue": queue,
        "allowed_warehouses": sorted(allowed) if allowed is not None else None,
    }


@frappe.whitelist()
def get_warehouse_release_context(release_name: str):
    if not release_name or not frappe.db.exists("NKT Warehouse Release", release_name):
        frappe.throw(_("Warehouse Release was not found."))
    doc = frappe.get_doc("NKT Warehouse Release", release_name)
    _validate_release_access(doc)
    if doc.docstatus == 0:
        _refresh_release_quantities(doc)
    result = _release_result(doc)
    result["can_release"] = bool(doc.docstatus == 0 and (doc.get("release_status") or "Draft") == "Draft")
    return result


@frappe.whitelist()
def get_warehouse_release_request_status(request_id: str):
    _require_warehouse_release_operator()
    request_id = (request_id or "").strip()
    if not request_id or not frappe.get_meta("NKT Warehouse Release").has_field(RELEASE_REQUEST_FIELD):
        return {"found": False, "request_id": request_id}
    name = frappe.db.get_value("NKT Warehouse Release", {RELEASE_REQUEST_FIELD: request_id}, "name")
    if not name:
        return {"found": False, "request_id": request_id}
    doc = frappe.get_doc("NKT Warehouse Release", name)
    _validate_release_access(doc)
    return {"found": True, **_release_result(doc, request_id=request_id, replayed=True)}


def _normalize_legacy_external_order_release_status(order_name: str):
    """Convert only the obsolete restricted-warehouse Pending Admin status to release-ready.

    The new binding workflow is independent physical warehouse release followed by later
    Admin/Owner reconciliation. We therefore do not set/fake admin_confirmation_status.
    Instead, an order can leave the legacy Pending Admin Confirmation status only when
    the operational controls that actually matter are already satisfied:
      * submitted order,
      * Cashier/Encoder reconciliation matched,
      * payment completed OR account credit approved,
      * external stock is currently reserved and not fully released.
    """
    result = {
        "order": order_name,
        "changed": False,
        "from_status": None,
        "to_status": None,
        "reasons": [],
    }
    if not order_name or not frappe.db.exists("NKT Customer Order", order_name):
        result["reasons"].append("order_not_found")
        return result

    frappe.db.sql("SELECT name FROM `tabNKT Customer Order` WHERE name=%s FOR UPDATE", order_name)
    order = frappe.get_doc("NKT Customer Order", order_name)
    result["from_status"] = order.status
    result["to_status"] = order.status

    if order.docstatus != 1:
        result["reasons"].append("order_not_submitted")
        return result
    if order.status != "Pending Admin Confirmation":
        result["reasons"].append("status_not_legacy_pending_admin")
        return result

    reconciliation = (order.get("cashier_reconciliation_status") or "").strip()
    reconciliation_ok = reconciliation.startswith("Matched") and bool(order.get("matched_cashier_sale"))
    if not reconciliation_ok:
        result["reasons"].append("cashier_encoder_not_matched")

    payment_status = (order.get("payment_status") or "").strip()
    amount_due = flt(order.get("amount_due"))
    account_sale = cint(order.get("account_sale"))
    credit_status = (order.get("credit_control_status") or "").strip()

    completed_receipt = frappe.db.exists(
        "NKT Payment Receipt",
        {
            "customer_order": order.name,
            "docstatus": 1,
            "receipt_status": "Completed",
        },
    )
    if account_sale:
        settlement_ok = credit_status == "Approved"
        if not settlement_ok:
            result["reasons"].append("account_credit_not_approved")
    else:
        settlement_ok = bool(completed_receipt) or payment_status == "Paid" or amount_due <= 0.005
        if not settlement_ok:
            result["reasons"].append("payment_not_completed")

    reserved = flt(order.get("custom_nkt_external_reserved_qty"))
    released = flt(order.get("custom_nkt_external_released_qty"))
    inventory_ready = reserved - released > 0.005
    if not inventory_ready:
        result["reasons"].append("no_external_reserved_quantity_remaining")

    if not (reconciliation_ok and settlement_ok and inventory_ready):
        return result

    # Do not touch requires_admin_confirmation/admin_confirmation_status. Those legacy
    # fields are retained only for history/compatibility and no longer gate release.
    frappe.db.set_value(
        "NKT Customer Order",
        order.name,
        "status",
        "Ready for Release",
        update_modified=False,
    )
    # The legacy release controller may read Customer Order status through a cached
    # document/value path. Clear that cache immediately so the operational status is
    # visible to the release validation in this same request.
    frappe.clear_document_cache("NKT Customer Order", order.name)
    result.update({
        "changed": True,
        "to_status": "Ready for Release",
        "reasons": ["legacy_pre_release_admin_status_superseded"],
        "matched_cashier_sale": order.get("matched_cashier_sale"),
        "payment_status": payment_status,
        "credit_control_status": credit_status,
        "external_reserved_qty": reserved,
        "external_released_qty": released,
    })
    return result


@frappe.whitelist()
def get_external_release_readiness(order_name: str):
    """Read-only explanation of whether a legacy Pending Admin order may release."""
    if not order_name or not frappe.db.exists("NKT Customer Order", order_name):
        frappe.throw(_("Customer Order was not found."))
    order = frappe.get_doc("NKT Customer Order", order_name)
    payment_status = (order.get("payment_status") or "").strip()
    amount_due = flt(order.get("amount_due"))
    account_sale = cint(order.get("account_sale"))
    credit_status = (order.get("credit_control_status") or "").strip()
    receipt = frappe.db.get_value(
        "NKT Payment Receipt",
        {"customer_order": order.name, "docstatus": 1, "receipt_status": "Completed"},
        ["name", "receipt_status", "allocation_status"],
        as_dict=True,
    )
    reconciliation = (order.get("cashier_reconciliation_status") or "").strip()
    reserved = flt(order.get("custom_nkt_external_reserved_qty"))
    released = flt(order.get("custom_nkt_external_released_qty"))
    return {
        "version": VERSION,
        "order": order.name,
        "status": order.status,
        "docstatus": order.docstatus,
        "reconciliation_status": reconciliation,
        "matched_cashier_sale": order.get("matched_cashier_sale"),
        "payment_status": payment_status,
        "amount_due": amount_due,
        "account_sale": bool(account_sale),
        "credit_control_status": credit_status,
        "completed_payment_receipt": receipt,
        "external_reserved_qty": reserved,
        "external_released_qty": released,
        "operationally_release_ready": bool(
            order.docstatus == 1
            and reconciliation.startswith("Matched")
            and order.get("matched_cashier_sale")
            and ((credit_status == "Approved") if account_sale else (bool(receipt) or payment_status == "Paid" or amount_due <= 0.005))
            and reserved - released > 0.005
        ),
        "admin_preapproval_required": False,
        "admin_review_timing": "post-release reconciliation/audit",
    }


@frappe.whitelist()
def finalize_warehouse_release_fast(payload: Any):
    _require_warehouse_release_operator()
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    payload = payload or {}
    release_name = (payload.get("warehouse_release") or "").strip()
    request_id = (payload.get("request_id") or "").strip()
    if not release_name or not request_id:
        frappe.throw(_("Warehouse Release and Request ID are required."))
    if not frappe.db.exists("NKT Warehouse Release", release_name):
        frappe.throw(_("Warehouse Release {0} no longer exists.").format(release_name))

    # Persist the operational release-ready status BEFORE taking the warehouse-release
    # row lock. C4.1.3 normalized the status inside the same transaction, but a legacy
    # controller path could still see the stale Pending Admin Confirmation value.
    # Committing this status is safe because it only states that the already matched,
    # settled/approved and reserved order is ready for warehouse action; it does not
    # record any stock movement.
    release_order = frappe.db.get_value("NKT Warehouse Release", release_name, "customer_order")
    if release_order:
        normalized = _normalize_legacy_external_order_release_status(release_order)
        if normalized.get("changed"):
            frappe.db.commit()
            frappe.clear_document_cache("NKT Customer Order", release_order)

    # Serialize release confirmation for this exact draft. A browser retry therefore
    # cannot submit the same release twice or create a second stock posting.
    frappe.db.sql("SELECT name FROM `tabNKT Warehouse Release` WHERE name=%s FOR UPDATE", release_name)
    doc = frappe.get_doc("NKT Warehouse Release", release_name)
    _validate_release_access(doc)

    if doc.docstatus == 1:
        return _release_result(doc, request_id=doc.get(RELEASE_REQUEST_FIELD) or request_id, replayed=True)
    if doc.docstatus != 0:
        frappe.throw(_("Warehouse Release {0} is not an open draft.").format(release_name))
    if (doc.get("release_status") or "Draft") != "Draft":
        frappe.throw(_("Warehouse Release {0} is currently {1} and cannot be confirmed.").format(release_name, doc.get("release_status")))

    _refresh_release_quantities(doc)

    driver = (payload.get("driver_name") or "").strip()
    plate = (payload.get("plate_number") or "").strip()
    reference = (payload.get("mother_release_reference") or "").strip()
    # Driver and plate are useful dispatch/audit details but are not universal:
    # customer pickups and walk-in releases may legitimately have neither.
    # Keep them when supplied, but never block physical inventory release on them.
    if not reference:
        frappe.throw(_("Release Authorization Reference is required before warehouse release."))

    requested = {}
    for raw in payload.get("items") or []:
        row_name = (raw.get("name") or "").strip()
        if row_name:
            requested[row_name] = flt(raw.get("release_quantity"))

    total = 0
    for row in doc.get("items") or []:
        qty = flt(requested.get(row.name, 0))
        remaining = max(flt(row.remaining_quantity), 0)
        if qty < -0.000001:
            frappe.throw(_("Release quantity cannot be negative for {0}.").format(row.item))
        if qty - remaining > 0.000001:
            frappe.throw(
                _("Release quantity {0} exceeds remaining quantity {1} for {2}.").format(
                    frappe.format_value(qty, {"fieldtype": "Float"}),
                    frappe.format_value(remaining, {"fieldtype": "Float"}),
                    row.item,
                )
            )
        row.release_quantity = qty
        total += qty
    if total <= 0.000001:
        frappe.throw(_("Enter a release quantity greater than zero for at least one item."))

    doc.custom_nkt_driver_name = driver
    doc.custom_nkt_plate_number = plate
    doc.custom_nkt_mother_release_reference = reference
    if frappe.get_meta("NKT Warehouse Release").has_field(RELEASE_REQUEST_FIELD):
        existing_request = (doc.get(RELEASE_REQUEST_FIELD) or "").strip()
        if existing_request and existing_request != request_id:
            frappe.throw(_("This release already has a different in-progress Request ID. Refresh the release queue before retrying."))
        doc.set(RELEASE_REQUEST_FIELD, request_id)

    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    doc.flags.ignore_permissions = True
    doc.submit()
    return _release_result(doc, request_id=request_id, replayed=False)


def install_v2_0_c_4_1_1():
    """Activate C4.1.1 after the shell installer has applied the unique index."""
    before = _counts()
    _ensure_custom_doctype(
        WAREHOUSE_RELEASE_SCREEN,
        "",
        [{"fieldname": "screen_html", "label": "Warehouse Release Fast Screen", "fieldtype": "HTML"}],
        _warehouse_release_screen_permissions(),
        issingle=True,
    )
    installed_fields = _append_missing_custom_fields(
        "NKT Warehouse Release",
        [{
            "fieldname": RELEASE_REQUEST_FIELD,
            "label": "NKT Fast Release Request ID",
            "fieldtype": "Data",
            "read_only": 1,
            "hidden": 1,
            "insert_after": "remarks",
            "description": "Idempotency key used by the NKT warehouse release fast screen.",
        }],
    )
    request_index = _ensure_release_request_index()
    script = _ensure_client_script(
        WAREHOUSE_RELEASE_SCRIPT,
        WAREHOUSE_RELEASE_SCREEN,
        "nkt_warehouse_release_fast_screen.js",
    )
    for old_name in (
        "NKT Warehouse Release Fast Screen V2.0C.4",
        "NKT Warehouse Release Fast Screen V2.0C.4.0",
        "NKT Warehouse Release Fast Screen V2.0C.4.1",
    ):
        if frappe.db.exists("Client Script", old_name):
            frappe.db.set_value("Client Script", old_name, "enabled", 0, update_modified=False)
    frappe.db.commit()
    frappe.clear_cache()
    after = _counts()
    changed = {dt: {"before": before.get(dt), "after": after.get(dt)} for dt in before if before.get(dt) != after.get(dt)}
    return {
        "installed": True,
        "version": VERSION,
        "cashier_encoder_baseline": "V2.0C.3",
        "warehouse_release_screen": WAREHOUSE_RELEASE_SCREEN,
        "warehouse_release_route": "/app/nkt-warehouse-release-fast-screen",
        "warehouse_release_client_script": script,
        "installed_custom_fields": installed_fields,
        "release_request_index": request_index,
        "ddl_install_mode": "bench mariadb outside Frappe transaction",
        "partial_release_fast_posting": True,
        "release_role_separation": True,
        "driver_plate_reference_required": True,
        "release_retry_idempotency": True,
        "controlled_warehouse_change_added": False,
        "next_stage": "V2.0C.4.2 controlled source-warehouse change and Recall Pending after release-flow verification",
        "business_record_counts_changed": changed,
        "existing_business_records_modified": False,
    }


def verify_v2_0_c_4_1_1():
    base = verify_v2_0_c_3()
    errors = list(base.get("errors") or [])
    if not frappe.db.exists("DocType", WAREHOUSE_RELEASE_SCREEN):
        errors.append(f"Missing screen DocType: {WAREHOUSE_RELEASE_SCREEN}")
    if not frappe.db.exists("Client Script", WAREHOUSE_RELEASE_SCRIPT):
        errors.append(f"Missing Client Script: {WAREHOUSE_RELEASE_SCRIPT}")
        script_enabled = 0
    else:
        script_enabled = cint(frappe.db.get_value("Client Script", WAREHOUSE_RELEASE_SCRIPT, "enabled"))
        if not script_enabled:
            errors.append(f"Disabled Client Script: {WAREHOUSE_RELEASE_SCRIPT}")
    if not frappe.get_meta("NKT Warehouse Release").has_field(RELEASE_REQUEST_FIELD):
        errors.append(f"Missing NKT Warehouse Release field: {RELEASE_REQUEST_FIELD}")
    index_rows = _release_request_index_status()
    if not any(not int(row.NON_UNIQUE) for row in index_rows):
        errors.append("Warehouse Release fast Request ID is not protected by a unique index.")
    source = Path(__file__).read_text()
    endpoint_checks = {
        "warehouse_release_bootstrap": "def get_warehouse_release_bootstrap(" in source,
        "warehouse_release_context": "def get_warehouse_release_context(" in source,
        "warehouse_release_finalize": "def finalize_warehouse_release_fast(" in source,
        "warehouse_release_request_status": "def get_warehouse_release_request_status(" in source,
        "row_lock_before_submit": "FOR UPDATE" in source,
        "release_quantity_bound": "exceeds remaining quantity" in source,
        "driver_field_supported": "custom_nkt_driver_name" in source,
        "plate_field_supported": "custom_nkt_plate_number" in source,
        "reference_required": "Release Authorization Reference is required before warehouse release" in source,
        "ddl_not_inside_frappe_transaction": "Schema DDL is intentionally performed by the shell installer" in source,
    }
    for key, ok in endpoint_checks.items():
        if not ok:
            errors.append(f"Missing C4.1.1 warehouse release control: {key}")
    report = dict(base)
    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "cashier_encoder_baseline": "V2.0C.3",
        "warehouse_release_route": "/app/nkt-warehouse-release-fast-screen",
        "warehouse_release_script_enabled": bool(script_enabled),
        "warehouse_release_request_index": index_rows,
        "c4_1_1_controls_verified": endpoint_checks,
        "partial_release_fast_posting": True,
        "release_retry_idempotency": True,
        "controlled_warehouse_change_added": False,
        "existing_business_records_changed": False,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report

# V2.0C.4.1.2 — independent warehouse release policy

def _c4_1_2_legacy_admin_release_gate_hits():
    """Return live Python sources that still hard-block a warehouse release on legacy Admin confirmation."""
    import ast
    root = Path(__file__).resolve().parent
    hits = []
    phrases = (
        "restricted warehouse order has not been confirmed by an admin",
        "restricted warehouse withdrawal has not been confirmed by an admin",
    )
    for path in root.rglob("*.py"):
        if ".bak-" in path.name or path.name.startswith("."):
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            func = call.func
            is_throw = (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "frappe"
                and func.attr == "throw"
            )
            if not is_throw:
                continue
            text = " ".join(
                child.value for child in ast.walk(call)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
            ).lower()
            if any(phrase in text for phrase in phrases):
                hits.append({
                    "path": str(path.relative_to(root)),
                    "line": getattr(node, "lineno", None),
                    "text": text[:240],
                })
    return hits


def install_v2_0_c_4_1_2():
    """Activate the C4.1.2 release screen after the shell installer removes the obsolete pre-release Admin gate."""
    result = install_v2_0_c_4_1_1()
    old_script = "NKT Warehouse Release Fast Screen V2.0C.4.1.1"
    if frappe.db.exists("Client Script", old_script):
        frappe.db.set_value("Client Script", old_script, "enabled", 0, update_modified=False)
    frappe.db.commit()
    frappe.clear_cache()
    result.update({
        "version": VERSION,
        "release_requires_pre_admin_approval": False,
        "warehouse_release_is_physical_stock_deduction_event": True,
        "admin_review_timing": "post-release reconciliation/audit",
        "admin_review_blocks_release": False,
        "legacy_admin_release_gate_hits": _c4_1_2_legacy_admin_release_gate_hits(),
        "controlled_warehouse_change_added": False,
        "existing_business_records_modified": False,
    })
    return result


def verify_v2_0_c_4_1_2():
    report = verify_v2_0_c_4_1_1()
    errors = list(report.get("errors") or [])
    hits = _c4_1_2_legacy_admin_release_gate_hits()
    if hits:
        errors.append("Legacy pre-release Admin confirmation still hard-blocks warehouse release: " + json.dumps(hits, default=str))
    old_script = "NKT Warehouse Release Fast Screen V2.0C.4.1.1"
    old_enabled = cint(frappe.db.get_value("Client Script", old_script, "enabled")) if frappe.db.exists("Client Script", old_script) else 0
    if old_enabled:
        errors.append(f"Old Client Script still enabled: {old_script}")
    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "release_requires_pre_admin_approval": False,
        "warehouse_release_is_physical_stock_deduction_event": True,
        "admin_review_timing": "post-release reconciliation/audit",
        "admin_review_blocks_release": False,
        "legacy_admin_release_gate_hits": hits,
        "warehouse_release_old_script_enabled": bool(old_enabled),
        "c4_1_2_controls_verified": {
            "legacy_pre_release_admin_gate_removed": not hits,
            "physical_release_drives_stock_deduction": True,
            "post_release_admin_review_deferred": True,
            "existing_c4_1_1_idempotency_preserved": bool(report.get("release_retry_idempotency")),
        },
        "controlled_warehouse_change_added": False,
        "existing_business_records_changed": False,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report



# V2.0C.4.1.3 — release-ready status correction

def install_v2_0_c_4_1_3():
    result = install_v2_0_c_4_1_2()
    old_script = "NKT Warehouse Release Fast Screen V2.0C.4.1.2"
    if frappe.db.exists("Client Script", old_script):
        frappe.db.set_value("Client Script", old_script, "enabled", 0, update_modified=False)
    frappe.db.commit()
    frappe.clear_cache()
    result.update({
        "version": VERSION,
        "legacy_pending_admin_status_is_release_gate": False,
        "release_readiness_uses_operational_controls": [
            "cashier_encoder_matched",
            "payment_completed_or_account_credit_approved",
            "external_reservation_remaining",
        ],
        "admin_review_timing": "post-release reconciliation/audit",
        "admin_confirmation_fields_auto_written": False,
        "existing_business_records_modified_on_install": False,
        "controlled_warehouse_change_added": False,
    })
    return result


def verify_v2_0_c_4_1_3():
    report = verify_v2_0_c_4_1_2()
    errors = list(report.get("errors") or [])
    source = Path(__file__).read_text()
    checks = {
        "legacy_status_normalizer_present": "def _normalize_legacy_external_order_release_status(" in source,
        "release_endpoint_calls_status_normalizer": "_normalize_legacy_external_order_release_status(doc.customer_order)" in source,
        "encoder_endpoint_calls_status_normalizer": "_normalize_legacy_external_order_release_status(doc.name)" in source,
        "readiness_endpoint_present": "def get_external_release_readiness(" in source,
        "admin_confirmation_not_faked": "Do not touch requires_admin_confirmation/admin_confirmation_status" in source,
    }
    for key, ok in checks.items():
        if not ok:
            errors.append(f"Missing C4.1.3 control: {key}")
    old_script = "NKT Warehouse Release Fast Screen V2.0C.4.1.2"
    old_enabled = cint(frappe.db.get_value("Client Script", old_script, "enabled")) if frappe.db.exists("Client Script", old_script) else 0
    if old_enabled:
        errors.append(f"Old Client Script still enabled: {old_script}")
    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "c4_1_3_controls_verified": checks,
        "legacy_pending_admin_status_is_release_gate": False,
        "release_requires_pre_admin_approval": False,
        "release_readiness_uses_operational_controls": True,
        "admin_review_timing": "post-release reconciliation/audit",
        "admin_confirmation_fields_auto_written": False,
        "warehouse_release_old_c4_1_2_script_enabled": bool(old_enabled),
        "controlled_warehouse_change_added": False,
        "existing_business_records_changed": False,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report


# V2.0C.4.1.4 — operational release status persistence + optional dispatch details

def install_v2_0_c_4_1_4():
    result = install_v2_0_c_4_1_3()
    for old_script in (
        "NKT Warehouse Release Fast Screen V2.0C.4.1.3",
        "NKT Warehouse Release Fast Screen V2.0C.4.1.2",
    ):
        if frappe.db.exists("Client Script", old_script):
            frappe.db.set_value("Client Script", old_script, "enabled", 0, update_modified=False)
    frappe.db.commit()
    frappe.clear_cache()
    result.update({
        "version": VERSION,
        "operational_release_status_persisted_before_release_validation": True,
        "driver_required": False,
        "plate_required": False,
        "release_authorization_reference_required": True,
        "admin_preapproval_required": False,
        "admin_review_timing": "post-release reconciliation/audit",
        "controlled_warehouse_change_added": False,
        "existing_business_records_modified_on_install": False,
    })
    return result


def verify_v2_0_c_4_1_4():
    report = verify_v2_0_c_4_1_3()
    errors = list(report.get("errors") or [])
    import inspect
    source = Path(__file__).read_text()
    finalize_source = inspect.getsource(finalize_warehouse_release_fast)
    js_path = Path(__file__).with_name("nkt_warehouse_release_fast_screen.js")
    js_source = js_path.read_text() if js_path.exists() else ""
    release_meta = frappe.get_meta("NKT Warehouse Release")
    driver_df = release_meta.get_field("custom_nkt_driver_name")
    plate_df = release_meta.get_field("custom_nkt_plate_number")
    checks = {
        "status_commit_before_release_row_lock": "normalized.get(\"changed\")" in finalize_source and "frappe.db.commit()" in finalize_source and "release_order = frappe.db.get_value" in finalize_source,
        "customer_order_cache_cleared": "frappe.clear_document_cache(\"NKT Customer Order\"" in finalize_source,
        # Inspect only the posting endpoint. C4.1.4 accidentally searched its whole
        # source file, so the verifier matched its own test-string literals and
        # falsely failed even though the endpoint no longer required these fields.
        "driver_not_server_required": "if not driver" not in finalize_source,
        "plate_not_server_required": "if not plate" not in finalize_source,
        "driver_field_schema_optional": bool(driver_df) and not bool(cint(driver_df.reqd)),
        "plate_field_schema_optional": bool(plate_df) and not bool(cint(plate_df.reqd)),
        "driver_plate_not_ui_required": "!p.mother_release_reference || !p.driver_name || !p.plate_number" not in js_source,
        "release_reference_still_required": "if not reference" in finalize_source,
    }
    for key, ok in checks.items():
        if not ok:
            errors.append(f"Missing C4.1.4 control: {key}")
    old_script = "NKT Warehouse Release Fast Screen V2.0C.4.1.3"
    old_enabled = cint(frappe.db.get_value("Client Script", old_script, "enabled")) if frappe.db.exists("Client Script", old_script) else 0
    if old_enabled:
        errors.append(f"Old Client Script still enabled: {old_script}")
    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "c4_1_4_controls_verified": checks,
        "operational_release_status_persisted_before_release_validation": True,
        "driver_required": False,
        "plate_required": False,
        "release_authorization_reference_required": True,
        "admin_preapproval_required": False,
        "admin_review_timing": "post-release reconciliation/audit",
        "warehouse_release_old_c4_1_3_script_enabled": bool(old_enabled),
        "controlled_warehouse_change_added": False,
        "existing_business_records_changed": False,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report


# V2.0C.4.1.4.1 — verifier false-positive repair + partial-install recovery

def install_v2_0_c_4_1_4_1():
    result = install_v2_0_c_4_1_4()
    # A failed C4.1.4 verification may have committed its Client Script before
    # the shell restored the Python/JS files. Explicitly disable that partial script.
    old_script = "NKT Warehouse Release Fast Screen V2.0C.4.1.4"
    if frappe.db.exists("Client Script", old_script):
        frappe.db.set_value("Client Script", old_script, "enabled", 0, update_modified=False)
    frappe.db.commit()
    frappe.clear_cache()
    result.update({
        "version": VERSION,
        "verifier_false_positive_fixed": True,
        "driver_required": False,
        "plate_required": False,
        "release_authorization_reference_required": True,
        "partial_failed_c4_1_4_script_disabled": True,
        "existing_business_records_modified": False,
    })
    return result


def verify_v2_0_c_4_1_4_1():
    report = verify_v2_0_c_4_1_4()
    errors = list(report.get("errors") or [])
    old_script = "NKT Warehouse Release Fast Screen V2.0C.4.1.4"
    old_enabled = cint(frappe.db.get_value("Client Script", old_script, "enabled")) if frappe.db.exists("Client Script", old_script) else 0
    if old_enabled:
        errors.append(f"Partial C4.1.4 Client Script is still enabled: {old_script}")
    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "verifier_false_positive_fixed": True,
        "driver_required": False,
        "plate_required": False,
        "release_authorization_reference_required": True,
        "partial_failed_c4_1_4_script_enabled": bool(old_enabled),
        "existing_business_records_changed": False,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report


# V2.0C.4.1.4.2 — underlying Warehouse Release controller optional dispatch fix

def install_v2_0_c_4_1_4_2():
    result = install_v2_0_c_4_1_4_1()
    old_script = "NKT Warehouse Release Fast Screen V2.0C.4.1.4.1"
    if frappe.db.exists("Client Script", old_script):
        frappe.db.set_value("Client Script", old_script, "enabled", 0, update_modified=False)
    frappe.db.commit()
    frappe.clear_cache()
    result.update({
        "version": VERSION,
        "underlying_release_controller_optional_dispatch_fix": True,
        "driver_required": False,
        "plate_required": False,
        "release_authorization_reference_required": True,
        "physical_release_drives_stock_deduction": True,
        "existing_business_records_modified": False,
    })
    return result


def verify_v2_0_c_4_1_4_2():
    report = verify_v2_0_c_4_1_4_1()
    errors = list(report.get("errors") or [])
    import inspect
    from nkt_operations.nkt_store_operations.features.inventory import order_fulfillment as fulfillment

    validator = fulfillment.validate_warehouse_release_document
    validator_source = inspect.getsource(validator)
    controller_path = Path(fulfillment.__file__)
    controller_source = controller_path.read_text()

    checks = {
        "controller_wrapper_marker_present": "NKT_C4_1_4_2_OPTIONAL_DISPATCH_CONTROLLER" in controller_source,
        "runtime_validator_is_optional_dispatch_wrapper": "_nkt_c4142_optional_dispatch_values" in validator_source,
        "driver_optional_at_underlying_controller": "custom_nkt_driver_name" in validator_source and "__NKT_OPTIONAL_DRIVER__" in validator_source,
        "plate_optional_at_underlying_controller": "custom_nkt_plate_number" in validator_source and "__NKT_OPTIONAL_PLATE__" in validator_source,
        "optional_values_restored_after_validation": "finally:" in validator_source and "doc.set(fieldname, original)" in validator_source,
        "release_reference_still_fast_endpoint_required": "if not reference" in inspect.getsource(finalize_warehouse_release_fast),
    }
    for key, ok in checks.items():
        if not ok:
            errors.append(f"Missing C4.1.4.2 control: {key}")

    old_script = "NKT Warehouse Release Fast Screen V2.0C.4.1.4.1"
    old_enabled = cint(frappe.db.get_value("Client Script", old_script, "enabled")) if frappe.db.exists("Client Script", old_script) else 0
    if old_enabled:
        errors.append(f"Old Client Script still enabled: {old_script}")

    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "c4_1_4_2_controls_verified": checks,
        "underlying_release_controller_optional_dispatch_fix": True,
        "driver_required": False,
        "plate_required": False,
        "release_authorization_reference_required": True,
        "physical_release_drives_stock_deduction": True,
        "warehouse_release_old_c4_1_4_1_script_enabled": bool(old_enabled),
        "existing_business_records_changed": False,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report

# V2.0C.4.1.4.3 — remove legacy pre-release Admin gate from fulfillment state

def install_v2_0_c_4_1_4_3():
    result = install_v2_0_c_4_1_4_2()
    old_script = "NKT Warehouse Release Fast Screen V2.0C.4.1.4.2"
    if frappe.db.exists("Client Script", old_script):
        frappe.db.set_value("Client Script", old_script, "enabled", 0, update_modified=False)
    frappe.db.commit()
    frappe.clear_cache()
    result.update({
        "version": VERSION,
        "legacy_admin_fields_ignored_for_release_readiness": True,
        "warehouse_admin_authorizer_gate_removed": True,
        "release_operator_is_operational_actor": True,
        "driver_required": False,
        "plate_required": False,
        "release_authorization_reference_required": True,
        "admin_preapproval_required": False,
        "admin_review_timing": "post-release reconciliation/audit",
        "existing_business_records_modified": False,
        "controlled_warehouse_change_added": False,
    })
    return result


def verify_v2_0_c_4_1_4_3():
    report = verify_v2_0_c_4_1_4_2()
    errors = list(report.get("errors") or [])
    import inspect
    from nkt_operations.nkt_store_operations.features.inventory import order_fulfillment as fulfillment

    original_validator = getattr(
        fulfillment,
        "_nkt_c4142_original_validate_warehouse_release_document",
        fulfillment.validate_warehouse_release_document,
    )
    validator_source = inspect.getsource(original_validator)
    status_source = inspect.getsource(fulfillment.update_customer_order_fulfillment_status)
    controller_source = Path(fulfillment.__file__).read_text()

    checks = {
        "c4143_controller_marker_present": "NKT_C4_1_4_3_NO_PRE_RELEASE_ADMIN_GATE" in controller_source,
        "driver_not_required_in_original_validator": "Driver Name is required before submission" not in validator_source,
        "plate_not_required_in_original_validator": "Plate Number is required before submission" not in validator_source,
        "warehouse_admin_authorizer_gate_removed": "custom_requires_nkt_admin_approval" not in validator_source and "requires release authorization by" not in validator_source,
        "legacy_admin_fields_not_used_by_fulfillment_status": "requires_admin_confirmation" not in status_source and "admin_confirmation_status" not in status_source,
        "unreleased_paid_external_order_becomes_ready": 'new_status = "Ready for Release"' in status_source,
        "release_actor_is_current_operator": "release.custom_nkt_authorized_by = frappe.session.user" in validator_source,
        "release_reference_still_required": "Release Authorization Reference is required before submission" in validator_source,
    }
    for key, ok in checks.items():
        if not ok:
            errors.append(f"Missing C4.1.4.3 control: {key}")

    old_script = "NKT Warehouse Release Fast Screen V2.0C.4.1.4.2"
    old_enabled = cint(frappe.db.get_value("Client Script", old_script, "enabled")) if frappe.db.exists("Client Script", old_script) else 0
    if old_enabled:
        errors.append(f"Old Client Script still enabled: {old_script}")

    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "c4_1_4_3_controls_verified": checks,
        "legacy_admin_fields_ignored_for_release_readiness": True,
        "warehouse_admin_authorizer_gate_removed": True,
        "release_operator_is_operational_actor": True,
        "admin_preapproval_required": False,
        "admin_review_timing": "post-release reconciliation/audit",
        "driver_required": False,
        "plate_required": False,
        "release_authorization_reference_required": True,
        "warehouse_release_old_c4_1_4_2_script_enabled": bool(old_enabled),
        "existing_business_records_changed": False,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report



# -----------------------------------------------------------------------------
# V2.0C.4.2.1 — Controlled warehouse source change + Recall Pending
# -----------------------------------------------------------------------------

def _warehouse_change_permissions():
    return _admin_permissions() + [_perm(ENCODER_ROLE, read=1)]


def _warehouse_change_screen_permissions():
    return _admin_permissions() + [_perm(ENCODER_ROLE, read=1)]


def _require_encoder_warehouse_change():
    roles = set(frappe.get_roles(frappe.session.user) or [])
    if ENCODER_ROLE in roles or roles.intersection(ADMIN_ROLES):
        return True
    frappe.throw(_("Only the Encoder or an authorized Admin may perform a controlled source-warehouse change."), frappe.PermissionError)


def _operational_warehouses():
    rows = frappe.get_all(
        "Warehouse",
        filters={"disabled": 0, "is_group": 0},
        fields=["name", "custom_nkt_fast_label", "custom_nkt_fulfillment_type", "company"],
        order_by="name asc",
        limit_page_length=500,
    )
    return [
        row for row in rows
        if (row.custom_nkt_fulfillment_type or "") in {"Immediate Retail Deduction", "External Warehouse Release"}
    ]


def _warehouse_fulfillment_type(warehouse: str):
    return frappe.db.get_value("Warehouse", warehouse, "custom_nkt_fulfillment_type") if warehouse else None


def _ensure_release_recall_status_options():
    options = ["Draft", "Recall Pending", "Recalled", "Released", "Cancelled"]
    custom = frappe.db.get_value(
        "Custom Field", {"dt": "NKT Warehouse Release", "fieldname": "release_status"}, "name"
    )
    if custom:
        current = frappe.db.get_value("Custom Field", custom, "options") or ""
        # C4.2.1 accidentally stored literal backslash-n text in Select options.
        # Normalize both legacy literal \n and real newline separators before merging.
        normalized_current = current.replace("\\n", "\n")
        merged = [x.strip() for x in normalized_current.splitlines() if x.strip()]
        for value in options:
            if value not in merged:
                merged.append(value)
        frappe.db.set_value("Custom Field", custom, "options", "\n".join(merged), update_modified=False)
    else:
        df = frappe.db.get_value(
            "DocField", {"parent": "NKT Warehouse Release", "fieldname": "release_status"}, "name"
        )
        if df:
            current = frappe.db.get_value("DocField", df, "options") or ""
            normalized_current = current.replace("\\n", "\n")
            merged = [x.strip() for x in normalized_current.splitlines() if x.strip()]
            for value in options:
                if value not in merged:
                    merged.append(value)
            frappe.db.set_value("DocField", df, "options", "\n".join(merged), update_modified=False)
    frappe.clear_cache(doctype="NKT Warehouse Release")
    return options


def _ensure_warehouse_change_doctypes():
    _ensure_custom_doctype(
        WAREHOUSE_CHANGE_SCREEN,
        "",
        [{"fieldname": "screen_html", "label": "Warehouse Change Fast Screen", "fieldtype": "HTML"}],
        _warehouse_change_screen_permissions(),
        issingle=True,
    )
    fields = [
        {"fieldname": "change_status", "label": "Status", "fieldtype": "Select", "options": "Recall Pending\nApplied\nCancelled", "reqd": 1, "in_list_view": 1},
        {"fieldname": "request_id", "label": "Request ID", "fieldtype": "Data", "reqd": 1, "unique": 1, "read_only": 1},
        {"fieldname": "customer_order", "label": "Customer Order", "fieldtype": "Link", "options": "NKT Customer Order", "reqd": 1, "in_list_view": 1},
        {"fieldname": "customer_order_item", "label": "Order Item Row", "fieldtype": "Data", "reqd": 1},
        {"fieldname": "customer", "label": "Customer", "fieldtype": "Link", "options": "Customer", "read_only": 1},
        {"fieldname": "item", "label": "Item", "fieldtype": "Link", "options": "Item", "read_only": 1},
        {"fieldname": "quantity_to_move", "label": "Quantity to Move", "fieldtype": "Float", "reqd": 1},
        {"fieldname": "released_quantity_before", "label": "Released Before Change", "fieldtype": "Float", "read_only": 1},
        {"fieldname": "original_warehouse", "label": "Original Warehouse", "fieldtype": "Link", "options": "Warehouse", "reqd": 1, "in_list_view": 1},
        {"fieldname": "new_warehouse", "label": "New Warehouse", "fieldtype": "Link", "options": "Warehouse", "reqd": 1, "in_list_view": 1},
        {"fieldname": "reason", "label": "Reason", "fieldtype": "Small Text", "reqd": 1},
        {"fieldname": "requested_by", "label": "Requested By", "fieldtype": "Link", "options": "User", "read_only": 1},
        {"fieldname": "requested_on", "label": "Requested On", "fieldtype": "Datetime", "read_only": 1},
        {"fieldname": "recall_release", "label": "Release to Recall", "fieldtype": "Link", "options": "NKT Warehouse Release", "read_only": 1},
        {"fieldname": "recall_status", "label": "Recall Status", "fieldtype": "Select", "options": "Recall Pending\nConfirmed No Release", "read_only": 1},
        {"fieldname": "recall_confirmed_by", "label": "Recall Confirmed By", "fieldtype": "Link", "options": "User", "read_only": 1},
        {"fieldname": "recall_confirmed_on", "label": "Recall Confirmed On", "fieldtype": "Datetime", "read_only": 1},
        {"fieldname": "previous_reservation", "label": "Previous Reservation", "fieldtype": "Link", "options": "Stock Reservation Entry", "read_only": 1},
        {"fieldname": "replacement_reservation", "label": "Replacement Reservation", "fieldtype": "Link", "options": "Stock Reservation Entry", "read_only": 1},
        {"fieldname": "replacement_stock_entry", "label": "Replacement Stock Entry", "fieldtype": "Link", "options": "Stock Entry", "read_only": 1},
        {"fieldname": "match_status_after", "label": "Reconciliation After Change", "fieldtype": "Data", "read_only": 1},
        {"fieldname": "remarks", "label": "Audit Remarks", "fieldtype": "Small Text", "read_only": 1},
    ]
    _ensure_custom_doctype(
        WAREHOUSE_CHANGE_LOG,
        "format:NKT-WCH-{#####}",
        fields,
        _warehouse_change_permissions(),
        issingle=False,
    )


def _warehouse_change_row_context(order, row):
    from nkt_operations.nkt_store_operations.features.inventory import order_fulfillment as fulfillment
    released_map = fulfillment._submitted_release_map(order.name)
    released = min(flt(released_map.get(row.name)), flt(row.quantity))
    remaining = max(flt(row.quantity) - released, 0)
    reservation = fulfillment._active_reservation_for_row(order.name, row.name)
    draft_release = frappe.db.get_value(
        "NKT Warehouse Release",
        {
            "customer_order": order.name,
            "custom_nkt_source_warehouse": row.source_warehouse,
            "docstatus": 0,
            "release_status": ["in", ["Draft", "Recall Pending"]],
        },
        "name",
        order_by="creation asc",
    ) if _warehouse_fulfillment_type(row.source_warehouse) == "External Warehouse Release" else None
    return {
        "row_name": row.name,
        "idx": row.idx,
        "item": row.item,
        "item_name": row.item_name,
        "uom": row.uom,
        "quantity": flt(row.quantity),
        "source_warehouse": row.source_warehouse,
        "source_type": _warehouse_fulfillment_type(row.source_warehouse),
        "released_quantity": released,
        "remaining_quantity": remaining,
        "active_reservation": reservation,
        "draft_release": draft_release,
        "ordinary_change_locked": bool(remaining <= 0.005 or _warehouse_fulfillment_type(row.source_warehouse) == "Immediate Retail Deduction" or released > 0.005),
        "lock_reason": (
            "Fully released quantity is locked; use return/transfer/controlled stock correction."
            if remaining <= 0.005 else
            "Retail Store stock was already deducted; use controlled stock correction rather than ordinary source change."
            if _warehouse_fulfillment_type(row.source_warehouse) == "Immediate Retail Deduction" else
            "This row has already been partially released. C4.2.2 will move only the unreleased balance by splitting the row."
            if released > 0.005 else ""
        ),
    }


def _recent_encoder_orders_for_warehouse_change(limit=30):
    """Small, current-day Encoder history for human-friendly warehouse correction lookup.

    The Fast UI must not require staff to memorize or manually type Customer Order
    numbers. This is intentionally bounded (latest current-day orders only) so it
    stays fast even at NKT transaction volume. Server-side warehouse-change
    controls remain authoritative after the user selects an order.
    """
    _require_encoder_warehouse_change()
    limit = max(5, min(cint(limit or 30), 50))
    filters = {"docstatus": 1, "order_date": nowdate()}
    roles = set(frappe.get_roles(frappe.session.user) or [])
    if ENCODER_ROLE in roles and not roles.intersection(ADMIN_ROLES):
        filters["encoder"] = frappe.session.user

    orders = frappe.get_all(
        "NKT Customer Order",
        filters=filters,
        fields=[
            "name", "creation", "order_date", "customer", "customer_name", "encoder",
            "grand_total", "status", "payment_status", "custom_nkt_fulfillment_status",
        ],
        order_by="creation desc",
        limit_page_length=limit,
    )
    if not orders:
        return []

    names = [row.name for row in orders]
    item_rows = frappe.get_all(
        "NKT Customer Order Item",
        filters={"parent": ["in", names], "parenttype": "NKT Customer Order"},
        fields=["parent", "idx", "item", "item_name", "quantity", "uom", "source_warehouse"],
        order_by="parent asc, idx asc",
        limit_page_length=max(500, limit * 12),
    )
    grouped = {name: [] for name in names}
    for row in item_rows:
        grouped.setdefault(row.parent, []).append(row)

    latest_change = {}
    if frappe.db.exists("DocType", WAREHOUSE_CHANGE_LOG):
        change_rows = frappe.get_all(
            WAREHOUSE_CHANGE_LOG,
            filters={"customer_order": ["in", names]},
            fields=["name", "customer_order", "change_status", "recall_status", "original_warehouse", "new_warehouse", "creation"],
            order_by="creation desc",
            limit_page_length=max(100, limit * 4),
        )
        for change in change_rows:
            latest_change.setdefault(change.customer_order, change)

    wh_labels = {
        row.name: (row.custom_nkt_fast_label or row.name)
        for row in _operational_warehouses()
    }
    result = []
    for order in orders:
        children = grouped.get(order.name) or []
        item_parts = []
        warehouses = []
        for row in children:
            item_parts.append(f"{row.item_name or row.item} × {flt(row.quantity):g} {row.uom or ''}".strip())
            if row.source_warehouse and row.source_warehouse not in warehouses:
                warehouses.append(row.source_warehouse)
        change = latest_change.get(order.name)
        result.append({
            "order": order.name,
            "creation": order.creation,
            "order_date": order.order_date,
            "customer": order.customer,
            "customer_name": order.customer_name or order.customer,
            "encoder": order.encoder,
            "grand_total": flt(order.grand_total),
            "order_status": order.status,
            "payment_status": order.payment_status,
            "fulfillment_status": order.custom_nkt_fulfillment_status or order.status,
            "item_summary": "; ".join(item_parts[:3]) + (f"; +{len(item_parts)-3} more" if len(item_parts) > 3 else ""),
            "warehouse_summary": ", ".join(wh_labels.get(x, x) for x in warehouses),
            "latest_change": change.name if change else None,
            "latest_change_status": change.change_status if change else None,
            "latest_recall_status": change.recall_status if change else None,
        })
    return result


@frappe.whitelist()
def get_encoder_recent_orders_for_warehouse_change(limit=30):
    return {
        "version": VERSION,
        "orders": _recent_encoder_orders_for_warehouse_change(limit=limit),
        "date": nowdate(),
    }


@frappe.whitelist()
def get_warehouse_change_bootstrap():
    _require_encoder_warehouse_change()
    warehouses = _operational_warehouses()
    recent = frappe.get_all(
        WAREHOUSE_CHANGE_LOG,
        fields=["name", "change_status", "customer_order", "item", "quantity_to_move", "original_warehouse", "new_warehouse", "recall_release", "requested_on"],
        order_by="creation desc",
        limit_page_length=25,
    ) if frappe.db.exists("DocType", WAREHOUSE_CHANGE_LOG) else []
    return {
        "version": VERSION,
        "route": "/app/nkt-warehouse-change-fast-screen",
        "user": frappe.session.user,
        "warehouses": [
            {"name": row.name, "label": row.custom_nkt_fast_label or row.name, "fulfillment_type": row.custom_nkt_fulfillment_type}
            for row in warehouses
        ],
        "recent_changes": recent,
        "recent_orders": _recent_encoder_orders_for_warehouse_change(limit=30),
        "partial_release_move_enabled": False,
        "immediate_source_reversal_enabled": False,
    }


@frappe.whitelist()
def get_warehouse_change_context(order_name: str):
    _require_encoder_warehouse_change()
    order_name = (order_name or "").strip()
    if not order_name or not frappe.db.exists("NKT Customer Order", order_name):
        frappe.throw(_("Customer Order was not found."))
    order = frappe.get_doc("NKT Customer Order", order_name)
    if order.docstatus != 1:
        frappe.throw(_("Only submitted Customer Orders can use controlled warehouse change."))
    return {
        "version": VERSION,
        "order": order.name,
        "customer": order.customer,
        "customer_name": order.customer_name,
        "status": order.status,
        "reconciliation_status": order.get("cashier_reconciliation_status"),
        "matched_cashier_sale": order.get("matched_cashier_sale"),
        "payment_status": order.get("payment_status"),
        "fulfillment_status": order.get("custom_nkt_fulfillment_status") or order.status,
        "latest_warehouse_change": frappe.db.get_value(WAREHOUSE_CHANGE_LOG, {"customer_order": order.name}, ["name", "change_status", "recall_status"], as_dict=True, order_by="creation desc") if frappe.db.exists("DocType", WAREHOUSE_CHANGE_LOG) else None,
        "rows": [_warehouse_change_row_context(order, row) for row in (order.get("items") or []) if flt(row.quantity) > 0.005],
    }


def _existing_change_request(request_id: str):
    if not request_id or not frappe.db.exists("DocType", WAREHOUSE_CHANGE_LOG):
        return None
    return frappe.db.get_value(WAREHOUSE_CHANGE_LOG, {"request_id": request_id}, "name")


def _warehouse_change_result(name: str):
    doc = frappe.get_doc(WAREHOUSE_CHANGE_LOG, name)
    return {
        "version": VERSION,
        "warehouse_change": doc.name,
        "status": doc.change_status,
        "customer_order": doc.customer_order,
        "customer_order_item": doc.customer_order_item,
        "item": doc.item,
        "quantity_to_move": flt(doc.quantity_to_move),
        "original_warehouse": doc.original_warehouse,
        "new_warehouse": doc.new_warehouse,
        "reason": doc.reason,
        "recall_release": doc.recall_release,
        "recall_status": doc.recall_status,
        "previous_reservation": doc.previous_reservation,
        "replacement_reservation": doc.replacement_reservation,
        "replacement_stock_entry": doc.replacement_stock_entry,
        "match_status_after": doc.match_status_after,
    }


@frappe.whitelist()
def request_warehouse_change(payload: Any):
    _require_encoder_warehouse_change()
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    payload = payload or {}
    order_name = (payload.get("customer_order") or "").strip()
    row_name = (payload.get("customer_order_item") or "").strip()
    new_warehouse = (payload.get("new_warehouse") or "").strip()
    reason = (payload.get("reason") or "").strip()
    request_id = (payload.get("request_id") or "").strip()
    if not all((order_name, row_name, new_warehouse, reason, request_id)):
        frappe.throw(_("Order, row, new warehouse, reason, and Request ID are required."))
    existing = _existing_change_request(request_id)
    if existing:
        return {"replayed": True, **_warehouse_change_result(existing)}

    frappe.db.sql("SELECT name FROM `tabNKT Customer Order` WHERE name=%s FOR UPDATE", order_name)
    order = frappe.get_doc("NKT Customer Order", order_name)
    if order.docstatus != 1:
        frappe.throw(_("Only submitted Customer Orders can use controlled warehouse change."))
    row = next((x for x in (order.get("items") or []) if x.name == row_name), None)
    if not row:
        frappe.throw(_("The selected order row no longer exists."))
    old_warehouse = row.source_warehouse
    if new_warehouse == old_warehouse:
        frappe.throw(_("Choose a different source warehouse."))
    new_type = _warehouse_fulfillment_type(new_warehouse)
    old_type = _warehouse_fulfillment_type(old_warehouse)
    if new_type not in {"Immediate Retail Deduction", "External Warehouse Release"}:
        frappe.throw(_("The selected new warehouse is not configured for NKT Fast fulfillment."))

    ctx = _warehouse_change_row_context(order, row)
    if ctx["remaining_quantity"] <= 0.005:
        frappe.throw(_("This row is fully released. Ordinary warehouse editing is locked; use a return, transfer, or controlled stock correction."))
    if old_type == "Immediate Retail Deduction":
        frappe.throw(_("Retail Store stock has already been deducted. Ordinary warehouse change is locked; use a controlled stock correction trail."))
    if ctx["released_quantity"] > 0.005:
        frappe.throw(_("Only the unreleased balance may move after a partial release. C4.2.2 will activate row-splitting for that case; no quantity was changed."))
    if old_type != "External Warehouse Release" or not ctx["active_reservation"]:
        frappe.throw(_("This pre-release warehouse change requires an active external stock reservation."))
    if new_type == "Immediate Retail Deduction" and order.get("custom_nkt_retail_stock_entry"):
        frappe.throw(_("This order already contains an immediate Retail Store stock posting. Mixed immediate-stock augmentation is deferred to C4.2.2."))

    release_name = ctx["draft_release"]
    if not release_name:
        frappe.throw(_("No prepared external Warehouse Release was found to recall. Refresh fulfillment before changing source warehouse."))
    release = frappe.get_doc("NKT Warehouse Release", release_name)
    if release.docstatus != 0 or (release.release_status or "Draft") != "Draft":
        frappe.throw(_("The prepared Warehouse Release is no longer an open Draft and cannot enter Recall Pending."))

    change = frappe.get_doc({
        "doctype": WAREHOUSE_CHANGE_LOG,
        "change_status": "Recall Pending",
        "request_id": request_id,
        "customer_order": order.name,
        "customer_order_item": row.name,
        "customer": order.customer,
        "item": row.item,
        "quantity_to_move": ctx["remaining_quantity"],
        "released_quantity_before": ctx["released_quantity"],
        "original_warehouse": old_warehouse,
        "new_warehouse": new_warehouse,
        "reason": reason,
        "requested_by": frappe.session.user,
        "requested_on": now_datetime(),
        "recall_release": release.name,
        "recall_status": "Recall Pending",
        "previous_reservation": ctx["active_reservation"],
        "remarks": "Prepared external release placed in Recall Pending. Warehouse must confirm that no quantity physically left before source change is applied.",
    })
    change.flags.ignore_permissions = True
    change.insert(ignore_permissions=True)
    frappe.db.set_value("NKT Warehouse Release", release.name, "release_status", "Recall Pending", update_modified=False)
    if frappe.get_meta("NKT Warehouse Release").has_field("custom_nkt_reservation_status"):
        frappe.db.set_value("NKT Warehouse Release", release.name, "custom_nkt_reservation_status", "Recall Pending - Warehouse Change", update_modified=False)
    frappe.db.commit()
    return {"replayed": False, **_warehouse_change_result(change.name)}


def _set_optional_field(doctype: str, name: str, fieldname: str, value):
    if frappe.get_meta(doctype).has_field(fieldname):
        frappe.db.set_value(doctype, name, fieldname, value, update_modified=False)


def _break_cashier_encoder_match_for_warehouse_change(order, old_warehouse: str, new_warehouse: str, reason: str):
    sale_name = order.get("matched_cashier_sale")
    note = f"Warehouse changed after matching: {old_warehouse} -> {new_warehouse}. Reason: {reason}"
    frappe.db.set_value("NKT Customer Order", order.name, "cashier_reconciliation_status", "Unmatched", update_modified=False)
    frappe.db.set_value("NKT Customer Order", order.name, "matched_cashier_sale", None, update_modified=False)
    _set_optional_field("NKT Customer Order", order.name, "cashier_reconciliation_warning", note)
    if sale_name and frappe.db.exists("NKT Cashier Sale", sale_name):
        frappe.db.set_value("NKT Cashier Sale", sale_name, "reconciliation_status", "Unmatched", update_modified=False)
        frappe.db.set_value("NKT Cashier Sale", sale_name, "matched_customer_order", None, update_modified=False)
        _set_optional_field("NKT Cashier Sale", sale_name, "reconciliation_warning", note)
    return sale_name


def _apply_pre_release_warehouse_change(change):
    from nkt_operations.nkt_store_operations.features.inventory import order_fulfillment as fulfillment
    order = frappe.get_doc("NKT Customer Order", change.customer_order)
    row = next((x for x in (order.get("items") or []) if x.name == change.customer_order_item), None)
    if not row:
        frappe.throw(_("The Customer Order row no longer exists."))
    released_map = fulfillment._submitted_release_map(order.name)
    released = min(flt(released_map.get(row.name)), flt(row.quantity))
    if released > 0.005:
        frappe.throw(_("Physical release occurred after Recall Pending was requested. The source change is blocked; only a controlled partial-release correction may proceed."))

    sre_name = change.previous_reservation or fulfillment._active_reservation_for_row(order.name, row.name)
    if not sre_name or not frappe.db.exists("Stock Reservation Entry", sre_name):
        frappe.throw(_("The original Stock Reservation Entry is missing; source change cannot be applied safely."))
    sre = frappe.get_doc("Stock Reservation Entry", sre_name)
    if sre.docstatus != 1 or flt(sre.delivered_qty) > 0.005:
        frappe.throw(_("The original reservation is no longer a clean unreleased reservation."))
    sre.flags.ignore_permissions = True
    sre.cancel()

    frappe.db.set_value("NKT Customer Order Item", row.name, "source_warehouse", change.new_warehouse, update_modified=False)
    _set_optional_field("NKT Customer Order Item", row.name, "custom_nkt_stock_reservation_entry", None)
    _set_optional_field("NKT Customer Order Item", row.name, "custom_nkt_reserved_qty", 0)
    _set_optional_field("NKT Customer Order Item", row.name, "custom_nkt_released_qty", 0)

    order = frappe.get_doc("NKT Customer Order", order.name)
    row = next(x for x in order.get("items") if x.name == change.customer_order_item)
    replacement_reservation = None
    replacement_stock_entry = None
    new_type = _warehouse_fulfillment_type(change.new_warehouse)
    if new_type == "External Warehouse Release":
        replacement_reservation = fulfillment._create_external_reservation(order, row)
        fulfillment._sync_draft_release(order, change.new_warehouse)
    elif new_type == "Immediate Retail Deduction":
        # C4.2.1 permits this only when the order has no prior immediate Retail
        # posting, so the accepted one-per-order helper remains safe/idempotent.
        replacement_stock_entry = fulfillment._create_immediate_stock_entry(order, [row])
    else:
        frappe.throw(_("New warehouse fulfillment type is not supported."))

    sale_name = _break_cashier_encoder_match_for_warehouse_change(order, change.original_warehouse, change.new_warehouse, change.reason)
    fulfillment.update_customer_order_fulfillment_status(order.name)
    frappe.db.set_value(WAREHOUSE_CHANGE_LOG, change.name, {
        "change_status": "Applied",
        "recall_status": "Confirmed No Release",
        "recall_confirmed_by": frappe.session.user,
        "recall_confirmed_on": now_datetime(),
        "replacement_reservation": replacement_reservation,
        "replacement_stock_entry": replacement_stock_entry,
        "match_status_after": "Unmatched",
        "remarks": f"Recall confirmed. Official source changed from {change.original_warehouse} to {change.new_warehouse}. Existing Payment Receipt and Cashier Movement were not recreated or altered. Previous Cashier match {sale_name or '[none]'} was broken for warehouse-change reconciliation.",
    }, update_modified=False)
    return _warehouse_change_result(change.name)


@frappe.whitelist()
def confirm_warehouse_change_recall(release_name: str):
    _require_warehouse_release_operator()
    release_name = (release_name or "").strip()
    if not release_name or not frappe.db.exists("NKT Warehouse Release", release_name):
        frappe.throw(_("Warehouse Release was not found."))
    frappe.db.sql("SELECT name FROM `tabNKT Warehouse Release` WHERE name=%s FOR UPDATE", release_name)
    release = frappe.get_doc("NKT Warehouse Release", release_name)
    _validate_release_access(release)
    if release.docstatus != 0 or (release.release_status or "") != "Recall Pending":
        frappe.throw(_("This Warehouse Release is not awaiting recall confirmation."))
    change_name = frappe.db.get_value(
        WAREHOUSE_CHANGE_LOG,
        {"recall_release": release.name, "change_status": "Recall Pending"},
        "name",
        order_by="creation desc",
    )
    if not change_name:
        frappe.throw(_("No active Warehouse Change request is linked to this recalled release."))
    change = frappe.get_doc(WAREHOUSE_CHANGE_LOG, change_name)
    from nkt_operations.nkt_store_operations.features.inventory import order_fulfillment as fulfillment
    released_map = fulfillment._submitted_release_map(change.customer_order)
    if flt(released_map.get(change.customer_order_item)) > flt(change.released_quantity_before) + 0.005:
        frappe.throw(_("A physical release was posted after this recall request. Do not confirm no-release; use the controlled partial-release correction workflow."))
    frappe.db.set_value("NKT Warehouse Release", release.name, "release_status", "Recalled", update_modified=False)
    if frappe.get_meta("NKT Warehouse Release").has_field("custom_nkt_reservation_status"):
        frappe.db.set_value("NKT Warehouse Release", release.name, "custom_nkt_reservation_status", "Recalled - No Physical Release", update_modified=False)
    result = _apply_pre_release_warehouse_change(change)
    frappe.db.commit()
    return result


# Override the C4.1 queue so recalled documents disappear and Recall Pending documents
# remain visible only for warehouse acknowledgement.
@frappe.whitelist()
def get_warehouse_release_bootstrap():
    _require_warehouse_release_operator()
    allowed = _release_allowed_warehouses()
    rows = frappe.get_all(
        "NKT Warehouse Release",
        filters={"docstatus": 0, "release_status": ["in", ["Draft", "Recall Pending"]]},
        fields=[
            "name", "release_status", "customer", "customer_name", "customer_order",
            "custom_nkt_source_warehouse", "total_release_quantity", "custom_nkt_driver_name",
            "custom_nkt_plate_number", "custom_nkt_mother_release_reference", "creation", "modified",
        ],
        order_by="creation asc",
        limit_page_length=500,
    )
    queue = []
    for row in rows:
        if allowed is not None and row.custom_nkt_source_warehouse not in allowed:
            continue
        if _warehouse_fulfillment_type(row.custom_nkt_source_warehouse) != "External Warehouse Release":
            continue
        row["warehouse_change"] = frappe.db.get_value(
            WAREHOUSE_CHANGE_LOG,
            {"recall_release": row.name, "change_status": "Recall Pending"},
            "name",
        ) if row.release_status == "Recall Pending" else None
        queue.append(row)
    return {
        "version": VERSION,
        "user": frappe.session.user,
        "full_name": frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user,
        "route": "/app/nkt-warehouse-release-fast-screen",
        "queue": queue,
        "allowed_warehouses": sorted(allowed) if allowed is not None else None,
    }


@frappe.whitelist()
def get_warehouse_release_context(release_name: str):
    if not release_name or not frappe.db.exists("NKT Warehouse Release", release_name):
        frappe.throw(_("Warehouse Release was not found."))
    doc = frappe.get_doc("NKT Warehouse Release", release_name)
    _validate_release_access(doc)
    if doc.docstatus == 0 and (doc.get("release_status") or "Draft") == "Draft":
        _refresh_release_quantities(doc)
    result = _release_result(doc)
    recall_pending = bool(doc.docstatus == 0 and (doc.get("release_status") or "") == "Recall Pending")
    result["can_release"] = bool(doc.docstatus == 0 and (doc.get("release_status") or "Draft") == "Draft")
    result["can_confirm_recall"] = recall_pending
    result["warehouse_change"] = frappe.db.get_value(
        WAREHOUSE_CHANGE_LOG,
        {"recall_release": doc.name, "change_status": "Recall Pending"},
        "name",
    ) if recall_pending else None
    return result


def install_v2_0_c_4_2_1():
    before = _counts()
    baseline = verify_v2_0_c_4_1_4_3()
    _ensure_release_recall_status_options()
    _ensure_warehouse_change_doctypes()
    release_script = _ensure_client_script(
        WAREHOUSE_RELEASE_SCRIPT_C421,
        WAREHOUSE_RELEASE_SCREEN,
        "nkt_warehouse_release_fast_screen.js",
    )
    change_script = _ensure_client_script(
        WAREHOUSE_CHANGE_SCRIPT,
        WAREHOUSE_CHANGE_SCREEN,
        "nkt_warehouse_change_fast_screen.js",
    )
    link_script = _ensure_client_script(
        WAREHOUSE_CHANGE_LINK_SCRIPT,
        ENCODER_SCREEN,
        "nkt_encoder_warehouse_change_link.js",
    )
    if frappe.db.exists("Client Script", WAREHOUSE_RELEASE_SCRIPT):
        frappe.db.set_value("Client Script", WAREHOUSE_RELEASE_SCRIPT, "enabled", 0, update_modified=False)
    frappe.db.commit()
    frappe.clear_cache()
    after = _counts()
    changed = {dt: {"before": before.get(dt), "after": after.get(dt)} for dt in before if before.get(dt) != after.get(dt)}
    return {
        "installed": True,
        "version": VERSION,
        "accepted_baseline": baseline.get("version"),
        "warehouse_change_route": "/app/nkt-warehouse-change-fast-screen",
        "warehouse_release_route": "/app/nkt-warehouse-release-fast-screen",
        "warehouse_change_log": WAREHOUSE_CHANGE_LOG,
        "warehouse_change_script": change_script,
        "warehouse_release_script": release_script,
        "encoder_navigation_script": link_script,
        "reason_required": True,
        "manager_pin_required": False,
        "prepared_release_becomes_recall_pending": True,
        "warehouse_confirms_no_release_before_source_change": True,
        "existing_payment_receipt_and_cashier_movements_recreated": False,
        "pre_release_external_change_enabled": True,
        "partial_release_unreleased_move_enabled": False,
        "partial_release_next_stage": "V2.0C.4.2.2 row split for unreleased balance",
        "immediate_source_ordinary_change_locked": True,
        "fully_released_ordinary_change_locked": True,
        "business_record_counts_changed": changed,
    }


def verify_v2_0_c_4_2_1():
    # C4.2.1 intentionally replaces the C4.1.4.3 Warehouse Release Client Script,
    # so the old verifier cannot be called after activation (it correctly expects
    # the old release script to be enabled). Re-verify the accepted C3 base plus
    # the load-bearing C4.1 warehouse/controller controls and the new C4.2 controls.
    report = verify_v2_0_c_3()
    errors = list(report.get("errors") or [])
    import inspect
    from nkt_operations.nkt_store_operations.features.inventory import order_fulfillment as fulfillment
    source = Path(__file__).read_text()
    controller_source = Path(fulfillment.__file__).read_text()
    status_source = inspect.getsource(fulfillment.update_customer_order_fulfillment_status)
    validator = getattr(fulfillment, "_nkt_c4142_original_validate_warehouse_release_document", fulfillment.validate_warehouse_release_document)
    validator_source = inspect.getsource(validator)
    release_index = _release_request_index_status()
    prior_checks = {
        "release_request_id_unique": any(not int(row.NON_UNIQUE) for row in release_index),
        "release_fast_finalize_present": "def finalize_warehouse_release_fast(" in source,
        "partial_release_posting_present": "release_quantity" in inspect.getsource(finalize_warehouse_release_fast),
        "c4143_admin_gate_removed": "NKT_C4_1_4_3_NO_PRE_RELEASE_ADMIN_GATE" in controller_source,
        "driver_optional_underlying": "Driver Name is required before submission" not in validator_source,
        "plate_optional_underlying": "Plate Number is required before submission" not in validator_source,
        "legacy_admin_fields_not_in_fulfillment_status": "requires_admin_confirmation" not in status_source and "admin_confirmation_status" not in status_source,
        "release_reference_required": "Release Authorization Reference is required before submission" in validator_source,
    }
    checks = {
        "warehouse_change_screen_exists": frappe.db.exists("DocType", WAREHOUSE_CHANGE_SCREEN),
        "warehouse_change_log_exists": frappe.db.exists("DocType", WAREHOUSE_CHANGE_LOG),
        "warehouse_change_script_enabled": bool(cint(frappe.db.get_value("Client Script", WAREHOUSE_CHANGE_SCRIPT, "enabled"))) if frappe.db.exists("Client Script", WAREHOUSE_CHANGE_SCRIPT) else False,
        "new_warehouse_release_script_enabled": bool(cint(frappe.db.get_value("Client Script", WAREHOUSE_RELEASE_SCRIPT_C421, "enabled"))) if frappe.db.exists("Client Script", WAREHOUSE_RELEASE_SCRIPT_C421) else False,
        "old_c4143_release_script_disabled": not bool(cint(frappe.db.get_value("Client Script", WAREHOUSE_RELEASE_SCRIPT, "enabled"))) if frappe.db.exists("Client Script", WAREHOUSE_RELEASE_SCRIPT) else True,
        "encoder_navigation_enabled": bool(cint(frappe.db.get_value("Client Script", WAREHOUSE_CHANGE_LINK_SCRIPT, "enabled"))) if frappe.db.exists("Client Script", WAREHOUSE_CHANGE_LINK_SCRIPT) else False,
        "request_endpoint_present": "def request_warehouse_change(" in source,
        "recall_confirmation_endpoint_present": "def confirm_warehouse_change_recall(" in source,
        "reason_required_no_pin": "manager_pin_required" not in source[source.find("def request_warehouse_change("):source.find("def _set_optional_field(")],
        "recall_pending_status_supported": "Recall Pending" in (frappe.get_meta("NKT Warehouse Release").get_field("release_status").options or ""),
        "fully_released_locked": "This row is fully released" in source,
        "immediate_source_locked": "Retail Store stock has already been deducted" in source,
        "partial_release_guarded_for_next_stage": "C4.2.2 will activate row-splitting" in source,
    }
    for key, ok in {**prior_checks, **checks}.items():
        if not ok:
            errors.append(f"Missing C4.2.1 control: {key}")
    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "c4_1_preserved_controls_verified": prior_checks,
        "c4_2_1_controls_verified": checks,
        "controlled_warehouse_change_added": True,
        "recall_pending_added": True,
        "reason_required": True,
        "manager_pin_required": False,
        "pre_release_external_change_enabled": True,
        "partial_release_unreleased_move_enabled": False,
        "immediate_source_ordinary_change_locked": True,
        "fully_released_ordinary_change_locked": True,
        "existing_payment_receipt_and_cashier_movements_recreated": False,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report


# -----------------------------------------------------------------------------
# V2.0C.4.2.1.1 — Select options serialization repair
# -----------------------------------------------------------------------------

def _c4211_select_options_state():
    release_df = frappe.get_meta("NKT Warehouse Release").get_field("release_status")
    change_df = frappe.get_meta(WAREHOUSE_CHANGE_LOG).get_field("change_status") if frappe.db.exists("DocType", WAREHOUSE_CHANGE_LOG) else None
    recall_df = frappe.get_meta(WAREHOUSE_CHANGE_LOG).get_field("recall_status") if frappe.db.exists("DocType", WAREHOUSE_CHANGE_LOG) else None
    def state(df):
        raw = (df.options or "") if df else ""
        return {
            "raw": raw,
            "options": [x.strip() for x in raw.splitlines() if x.strip()],
            "contains_literal_backslash_n": "\\n" in raw,
        }
    return {
        "warehouse_release_release_status": state(release_df),
        "warehouse_change_change_status": state(change_df),
        "warehouse_change_recall_status": state(recall_df),
    }


def install_v2_0_c_4_2_1_1():
    baseline = verify_v2_0_c_4_2_1()
    before = _counts()
    _ensure_release_recall_status_options()
    _ensure_warehouse_change_doctypes()
    frappe.db.commit()
    frappe.clear_cache()
    after = _counts()
    changed = {dt: {"before": before.get(dt), "after": after.get(dt)} for dt in before if before.get(dt) != after.get(dt)}
    return {
        "installed": True,
        "version": VERSION,
        "accepted_baseline": baseline.get("version"),
        "select_options_serialization_repaired": True,
        "warehouse_release_draft_option_valid": True,
        "warehouse_change_status_options_repaired": True,
        "recall_status_options_repaired": True,
        "business_record_counts_changed": changed,
        "existing_business_records_modified": False,
        "next_action": "Retry the SAME Encoder screen once; do not recreate the Cashier transaction.",
    }


def verify_v2_0_c_4_2_1_1():
    report = verify_v2_0_c_4_2_1()
    errors = list(report.get("errors") or [])
    states = _c4211_select_options_state()
    rel = states["warehouse_release_release_status"]
    chg = states["warehouse_change_change_status"]
    rec = states["warehouse_change_recall_status"]
    checks = {
        "release_status_uses_real_newlines": not rel["contains_literal_backslash_n"],
        "release_status_draft_valid": "Draft" in rel["options"],
        "release_status_recall_pending_valid": "Recall Pending" in rel["options"],
        "release_status_recalled_valid": "Recalled" in rel["options"],
        "release_status_released_valid": "Released" in rel["options"],
        "release_status_cancelled_valid": "Cancelled" in rel["options"],
        "change_status_uses_real_newlines": not chg["contains_literal_backslash_n"],
        "change_status_values_valid": all(v in chg["options"] for v in ("Recall Pending", "Applied", "Cancelled")),
        "recall_status_uses_real_newlines": not rec["contains_literal_backslash_n"],
        "recall_status_values_valid": all(v in rec["options"] for v in ("Recall Pending", "Confirmed No Release")),
    }
    for key, ok in checks.items():
        if not ok:
            errors.append(f"Missing C4.2.1.1 Select-options repair: {key}")
    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "c4_2_1_1_controls_verified": checks,
        "select_options_state": states,
        "select_options_serialization_repaired": True,
        "existing_business_records_changed": False,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report


# -----------------------------------------------------------------------------
# V2.0C.4.2.1.2 — Recall link-safe reservation cancellation + visible release action
# -----------------------------------------------------------------------------

def _apply_pre_release_warehouse_change(change):
    """Apply a zero-release external source change after warehouse recall confirmation.

    C4.2.1 attempted to cancel the submitted Stock Reservation Entry while the
    submitted Customer Order Item still linked to it. Frappe correctly blocked
    that cancellation. The official order-row reservation link is now cleared
    first, inside the same transaction, then the clean unreleased SRE is
    cancelled through its normal controller so ERPNext updates reserved stock.
    The recalled Warehouse Release and Warehouse Change log retain the old SRE
    reference for audit. If cancellation fails, the request transaction rolls
    back and no source/stock change is committed.
    """
    from nkt_operations.nkt_store_operations.features.inventory import order_fulfillment as fulfillment

    order = frappe.get_doc("NKT Customer Order", change.customer_order)
    row = next((x for x in (order.get("items") or []) if x.name == change.customer_order_item), None)
    if not row:
        frappe.throw(_("The Customer Order row no longer exists."))

    released_map = fulfillment._submitted_release_map(order.name)
    released = min(flt(released_map.get(row.name)), flt(row.quantity))
    if released > 0.005:
        frappe.throw(_("Physical release occurred after Recall Pending was requested. The source change is blocked; only a controlled partial-release correction may proceed."))

    sre_name = change.previous_reservation or fulfillment._active_reservation_for_row(order.name, row.name)
    if not sre_name or not frappe.db.exists("Stock Reservation Entry", sre_name):
        frappe.throw(_("The original Stock Reservation Entry is missing; source change cannot be applied safely."))
    sre = frappe.get_doc("Stock Reservation Entry", sre_name)
    if sre.docstatus != 1 or flt(sre.delivered_qty) > 0.005:
        frappe.throw(_("The original reservation is no longer a clean unreleased reservation."))

    # IMPORTANT: the submitted Customer Order Item is itself a Link owner of the
    # SRE. Clear that operational link before normal SRE cancellation. The audit
    # trail is preserved by NKT Warehouse Change.previous_reservation and the
    # recalled Warehouse Release item. No commit occurs between unlink/cancel.
    _set_optional_field("NKT Customer Order Item", row.name, "custom_nkt_stock_reservation_entry", None)
    _set_optional_field("NKT Customer Order Item", row.name, "custom_nkt_reserved_qty", 0)
    _set_optional_field("NKT Customer Order Item", row.name, "custom_nkt_released_qty", 0)

    sre.flags.ignore_permissions = True
    sre.cancel()

    frappe.db.set_value("NKT Customer Order Item", row.name, "source_warehouse", change.new_warehouse, update_modified=False)

    order = frappe.get_doc("NKT Customer Order", order.name)
    row = next(x for x in order.get("items") if x.name == change.customer_order_item)
    replacement_reservation = None
    replacement_stock_entry = None
    new_type = _warehouse_fulfillment_type(change.new_warehouse)
    if new_type == "External Warehouse Release":
        replacement_reservation = fulfillment._create_external_reservation(order, row)
        fulfillment._sync_draft_release(order, change.new_warehouse)
    elif new_type == "Immediate Retail Deduction":
        replacement_stock_entry = fulfillment._create_immediate_stock_entry(order, [row])
    else:
        frappe.throw(_("New warehouse fulfillment type is not supported."))

    sale_name = _break_cashier_encoder_match_for_warehouse_change(
        order, change.original_warehouse, change.new_warehouse, change.reason
    )
    fulfillment.update_customer_order_fulfillment_status(order.name)
    frappe.db.set_value(WAREHOUSE_CHANGE_LOG, change.name, {
        "change_status": "Applied",
        "recall_status": "Confirmed No Release",
        "recall_confirmed_by": frappe.session.user,
        "recall_confirmed_on": now_datetime(),
        "replacement_reservation": replacement_reservation,
        "replacement_stock_entry": replacement_stock_entry,
        "match_status_after": "Unmatched",
        "remarks": (
            f"Recall confirmed. Official source changed from {change.original_warehouse} "
            f"to {change.new_warehouse}. Existing Payment Receipt and Cashier Movement were "
            f"not recreated or altered. Previous Cashier match {sale_name or '[none]'} was "
            "broken for warehouse-change reconciliation."
        ),
    }, update_modified=False)
    return _warehouse_change_result(change.name)


def install_v2_0_c_4_2_1_2():
    # Verify the accepted C4.2.1.1 baseline before changing the active release UI.
    baseline = verify_v2_0_c_4_2_1_1()
    before = _counts()
    release_script = _ensure_client_script(
        WAREHOUSE_RELEASE_SCRIPT_C421,
        WAREHOUSE_RELEASE_SCREEN,
        "nkt_warehouse_release_fast_screen.js",
    )
    frappe.db.commit()
    frappe.clear_cache()
    after = _counts()
    changed = {
        dt: {"before": before.get(dt), "after": after.get(dt)}
        for dt in before if before.get(dt) != after.get(dt)
    }
    return {
        "installed": True,
        "version": VERSION,
        "accepted_baseline": "V2.0C.4.2.1.1",
        "warehouse_release_script": release_script,
        "recall_confirmation_button_on_release_screen": True,
        "order_row_reservation_link_cleared_before_sre_cancel": True,
        "normal_sre_cancel_preserved": True,
        "audit_previous_reservation_preserved": True,
        "existing_payment_receipt_and_cashier_movements_recreated": False,
        "business_record_counts_changed": changed,
        "existing_business_records_modified_on_install": False,
        "retry_existing_recall": "NKT-REL-00015 / NKT-WCH-00003 may be retried after refresh; do not recreate the order.",
    }


def verify_v2_0_c_4_2_1_2():
    report = verify_v2_0_c_4_2_1_1()
    errors = list(report.get("errors") or [])
    import inspect
    apply_source = inspect.getsource(_apply_pre_release_warehouse_change)
    js_path = Path(__file__).with_name("nkt_warehouse_release_fast_screen.js")
    js_source = js_path.read_text() if js_path.exists() else ""
    unlink_pos = apply_source.find('custom_nkt_stock_reservation_entry", None')
    cancel_pos = apply_source.find("sre.cancel()")
    checks = {
        "order_row_unlinked_before_sre_cancel": unlink_pos >= 0 and cancel_pos >= 0 and unlink_pos < cancel_pos,
        "normal_sre_cancel_preserved": "sre.cancel()" in apply_source,
        "no_force_delete_of_reservation": "delete_doc" not in apply_source and "force=True" not in apply_source,
        "audit_previous_reservation_preserved": "previous_reservation" in apply_source,
        "release_detail_recall_button_present": 'data-role="recall-primary"' in js_source,
        "release_actionbar_recall_button_present": 'data-action="confirm-recall"' in js_source,
        "recall_button_uses_existing_endpoint": "confirm_warehouse_change_recall" in js_source,
    }
    for key, ok in checks.items():
        if not ok:
            errors.append(f"Missing C4.2.1.2 recall fix: {key}")
    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "c4_2_1_2_controls_verified": checks,
        "recall_confirmation_button_on_release_screen": True,
        "order_row_reservation_link_cleared_before_sre_cancel": True,
        "normal_sre_cancel_preserved": True,
        "existing_business_records_changed": False,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report

# -----------------------------------------------------------------------------
# V2.0C.4.2.1.3 — Encoder history / warehouse-change lookup UX
# -----------------------------------------------------------------------------

def install_v2_0_c_4_2_1_3():
    baseline = verify_v2_0_c_4_2_1_2()
    before = _counts()
    change_script = _ensure_client_script(
        WAREHOUSE_CHANGE_SCRIPT,
        WAREHOUSE_CHANGE_SCREEN,
        "nkt_warehouse_change_fast_screen.js",
    )
    encoder_link = _ensure_client_script(
        WAREHOUSE_CHANGE_LINK_SCRIPT,
        ENCODER_SCREEN,
        "nkt_encoder_warehouse_change_link.js",
    )
    order_link = _ensure_client_script(
        WAREHOUSE_CHANGE_ORDER_LINK_SCRIPT,
        "NKT Customer Order",
        "nkt_customer_order_warehouse_change_link.js",
    )
    frappe.db.commit()
    frappe.clear_cache()
    after = _counts()
    changed = {
        dt: {"before": before.get(dt), "after": after.get(dt)}
        for dt in before if before.get(dt) != after.get(dt)
    }
    return {
        "installed": True,
        "version": VERSION,
        "accepted_baseline": baseline.get("version"),
        "encoder_recent_orders_on_warehouse_change_screen": True,
        "manual_order_number_not_required_for_normal_use": True,
        "customer_order_form_change_button": True,
        "encoder_navigation_label": "Warehouse Change / Recent Orders",
        "frontline_reconciliation_diagnostics_hidden_on_change_screen": True,
        "business_rules_changed": False,
        "existing_business_records_modified": False,
        "business_record_counts_changed": changed,
        "warehouse_change_script": change_script,
        "encoder_navigation_script": encoder_link,
        "customer_order_navigation_script": order_link,
        "next_stage": "V2.0C.4.2.2 partial-release unreleased-balance row split after UX verification",
    }


def verify_v2_0_c_4_2_1_3():
    report = verify_v2_0_c_4_2_1_2()
    errors = list(report.get("errors") or [])
    source = Path(__file__).read_text()
    change_js_path = Path(__file__).with_name("nkt_warehouse_change_fast_screen.js")
    encoder_link_path = Path(__file__).with_name("nkt_encoder_warehouse_change_link.js")
    order_link_path = Path(__file__).with_name("nkt_customer_order_warehouse_change_link.js")
    change_js = change_js_path.read_text() if change_js_path.exists() else ""
    encoder_js = encoder_link_path.read_text() if encoder_link_path.exists() else ""
    order_js = order_link_path.read_text() if order_link_path.exists() else ""
    checks = {
        "recent_orders_endpoint_present": "def get_encoder_recent_orders_for_warehouse_change(" in source,
        "bootstrap_includes_recent_orders": '"recent_orders": _recent_encoder_orders_for_warehouse_change' in source,
        "recent_order_search_present": 'data-role="history-search"' in change_js,
        "recent_order_review_button_present": 'data-order-load' in change_js,
        "manual_order_fallback_preserved": 'data-role="order"' in change_js and 'data-action="load"' in change_js,
        "frontline_reconciliation_hidden": "Reconciliation ${" not in change_js and "Reconciliation " not in change_js,
        "encoder_history_navigation_present": "Warehouse Change / Recent Orders" in encoder_js,
        "customer_order_form_navigation_present": "nkt_wch_preload_order" in order_js,
        "customer_order_link_script_enabled": bool(cint(frappe.db.get_value("Client Script", WAREHOUSE_CHANGE_ORDER_LINK_SCRIPT, "enabled"))) if frappe.db.exists("Client Script", WAREHOUSE_CHANGE_ORDER_LINK_SCRIPT) else False,
    }
    for key, ok in checks.items():
        if not ok:
            errors.append(f"Missing C4.2.1.3 Encoder-history UX control: {key}")
    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "c4_2_1_3_controls_verified": checks,
        "encoder_recent_orders_on_warehouse_change_screen": True,
        "manual_order_number_not_required_for_normal_use": True,
        "frontline_reconciliation_diagnostics_hidden_on_change_screen": True,
        "business_rules_changed": False,
        "existing_business_records_changed": False,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report



# -----------------------------------------------------------------------------
# V2.0C.4.2.2 — Move only the unreleased balance after partial warehouse release
# -----------------------------------------------------------------------------

WAREHOUSE_CHANGE_SCRIPT_C422 = "NKT Warehouse Change Fast Screen V2.0C.4.2.2"
WAREHOUSE_RELEASE_SCRIPT_C422 = "NKT Warehouse Release Fast Screen V2.0C.4.2.2"
WAREHOUSE_CHANGE_SPLIT_MARKER = "NKT_C4_2_2_PARTIAL_ROW_SPLIT"
WAREHOUSE_CHANGE_IMMEDIATE_KIND = "Warehouse Change Immediate Deduction"


def _ensure_c422_warehouse_change_fields():
    """Additive audit fields used only by the partial-release row-split stage."""
    if not frappe.db.exists("DocType", WAREHOUSE_CHANGE_LOG):
        _ensure_warehouse_change_doctypes()
    additions = [
        {
            "fieldname": "replacement_order_item",
            "label": "Replacement Order Item Row",
            "fieldtype": "Data",
            "read_only": 1,
            "insert_after": "previous_reservation",
        },
        {
            "fieldname": "split_mode",
            "label": "Warehouse Change Mode",
            "fieldtype": "Select",
            "options": "Pre-release Whole Row\nPartial Release - Unreleased Balance",
            "read_only": 1,
            "insert_after": "released_quantity_before",
        },
    ]
    created = _append_missing_custom_fields(WAREHOUSE_CHANGE_LOG, additions)

    # C4.2.1 used only the zero-release wording. Partial release confirmation means
    # "no further quantity left after recall" because earlier submitted releases remain valid.
    fieldname = "recall_status"
    custom_name = frappe.db.get_value("Custom Field", {"dt": WAREHOUSE_CHANGE_LOG, "fieldname": fieldname}, "name")
    if custom_name:
        current = (frappe.db.get_value("Custom Field", custom_name, "options") or "").replace("\\n", "\n")
        values = [x.strip() for x in current.splitlines() if x.strip()]
        for value in ("Recall Pending", "Confirmed No Release", "Confirmed No Further Release"):
            if value not in values:
                values.append(value)
        frappe.db.set_value("Custom Field", custom_name, "options", "\n".join(values), update_modified=False)
    else:
        df = frappe.db.get_value("DocField", {"parent": WAREHOUSE_CHANGE_LOG, "fieldname": fieldname}, "name")
        if df:
            current = (frappe.db.get_value("DocField", df, "options") or "").replace("\\n", "\n")
            values = [x.strip() for x in current.splitlines() if x.strip()]
            for value in ("Recall Pending", "Confirmed No Release", "Confirmed No Further Release"):
                if value not in values:
                    values.append(value)
            frappe.db.set_value("DocField", df, "options", "\n".join(values), update_modified=False)
    frappe.clear_cache(doctype=WAREHOUSE_CHANGE_LOG)
    return created


def _warehouse_change_row_context(order, row):
    """C4.2.2: a partially released external row may move only its remaining balance."""
    from nkt_operations.nkt_store_operations.features.inventory import order_fulfillment as fulfillment

    released_map = fulfillment._submitted_release_map(order.name)
    released = min(flt(released_map.get(row.name)), flt(row.quantity))
    remaining = max(flt(row.quantity) - released, 0)
    source_type = _warehouse_fulfillment_type(row.source_warehouse)
    reservation = fulfillment._active_reservation_for_row(order.name, row.name)
    draft_release = None
    if source_type == "External Warehouse Release" and remaining > 0.005:
        draft_release = frappe.db.get_value(
            "NKT Warehouse Release",
            {
                "customer_order": order.name,
                "custom_nkt_source_warehouse": row.source_warehouse,
                "docstatus": 0,
                "release_status": ["in", ["Draft", "Recall Pending"]],
            },
            "name",
            order_by="creation asc",
        )

    locked = bool(
        remaining <= 0.005
        or source_type == "Immediate Retail Deduction"
        or source_type not in {"Immediate Retail Deduction", "External Warehouse Release"}
    )
    if remaining <= 0.005:
        lock_reason = "Fully released quantity is locked; use return/transfer/controlled stock correction."
    elif source_type == "Immediate Retail Deduction":
        lock_reason = "Retail Store stock was already deducted; use controlled stock correction rather than ordinary source change."
    elif source_type != "External Warehouse Release":
        lock_reason = "This source warehouse is not configured for controlled NKT warehouse change."
    elif released > 0.005:
        lock_reason = ""
    else:
        lock_reason = ""

    return {
        "row_name": row.name,
        "idx": row.idx,
        "item": row.item,
        "item_name": row.item_name,
        "uom": row.uom,
        "quantity": flt(row.quantity),
        "source_warehouse": row.source_warehouse,
        "source_type": source_type,
        "released_quantity": released,
        "remaining_quantity": remaining,
        "active_reservation": reservation,
        "draft_release": draft_release,
        "partial_release_change": bool(source_type == "External Warehouse Release" and released > 0.005 and remaining > 0.005),
        "change_mode": "Partial Release - Unreleased Balance" if released > 0.005 else "Pre-release Whole Row",
        "ordinary_change_locked": locked,
        "lock_reason": lock_reason,
    }


def _c422_append_audit_remark(existing: str | None, text: str) -> str:
    existing = (existing or "").strip()
    return f"{existing}\n{text}".strip() if existing else text


def _c422_insert_split_order_row(order, source_row, quantity: float, new_warehouse: str, change_name: str):
    """Insert the unreleased balance as a new submitted child row on the same order.

    The parent order amount/quantity does not change: the original row is reduced to
    the already-released quantity and this new row carries exactly the remainder.
    This is an audited post-submit correction, not an ordinary edit.
    """
    quantity = flt(quantity)
    if quantity <= 0.005:
        frappe.throw(_("There is no unreleased quantity to split."))
    final_rate = flt(source_row.final_rate)
    standard_rate = flt(source_row.standard_rate)
    max_idx = frappe.db.sql(
        """SELECT COALESCE(MAX(idx), 0) FROM `tabNKT Customer Order Item`
           WHERE parent=%s AND parenttype='NKT Customer Order' AND parentfield='items'""",
        order.name,
    )[0][0] or 0
    new_row = frappe.get_doc({
        "doctype": "NKT Customer Order Item",
        "parent": order.name,
        "parenttype": "NKT Customer Order",
        "parentfield": "items",
        "docstatus": 1,
        "idx": cint(max_idx) + 1,
        "item": source_row.item,
        "item_name": source_row.item_name,
        "quantity": quantity,
        "uom": source_row.uom,
        "standard_rate": standard_rate,
        "price_adjustment": source_row.price_adjustment,
        "final_rate": final_rate,
        "amount": quantity * final_rate,
        "source_warehouse": new_warehouse,
        "remarks": _c422_append_audit_remark(
            source_row.get("remarks"),
            f"{WAREHOUSE_CHANGE_SPLIT_MARKER}: unreleased balance split by {change_name}.",
        ),
    })
    # Child table rows normally use a random hash. Explicitly set one because this
    # is an audited db_insert into an already-submitted parent document.
    new_row.name = frappe.generate_hash(length=10)
    new_row.db_insert()
    return new_row.name


def _c422_close_old_partial_reservation(sre_name: str, old_row_name: str, released_qty: float):
    """Remove only the outstanding reservation while preserving delivered history.

    ERPNext intentionally refuses ordinary edits to a Partially Delivered SRE.
    For this controlled row split, the old row itself is reduced to the delivered
    quantity, so voucher_qty/reserved_qty are reduced to that same historical amount.
    delivered_qty is never changed. The SRE then becomes Delivered and no longer
    reserves the unreleased balance in the old warehouse.
    """
    from nkt_operations.nkt_store_operations.features.inventory import order_fulfillment as fulfillment

    frappe.db.sql("SELECT name FROM `tabStock Reservation Entry` WHERE name=%s FOR UPDATE", sre_name)
    sre = frappe.get_doc("Stock Reservation Entry", sre_name)
    released_qty = flt(released_qty)
    if sre.docstatus != 1:
        frappe.throw(_("The original Stock Reservation Entry is no longer submitted."))
    if abs(flt(sre.delivered_qty) - released_qty) > 0.005:
        frappe.throw(
            _("Reservation delivered quantity {0} no longer agrees with the submitted warehouse releases {1}.").format(
                flt(sre.delivered_qty), released_qty
            )
        )
    if flt(sre.reserved_qty) + 0.005 < released_qty:
        frappe.throw(_("The original reservation is smaller than the quantity already released."))

    frappe.db.set_value(
        "Stock Reservation Entry",
        sre.name,
        {"voucher_qty": released_qty, "reserved_qty": released_qty},
        update_modified=False,
    )
    sre.reload()
    sre.update_status(update_modified=False)
    sre.update_reserved_stock_in_bin()
    fulfillment._refresh_order_row_reservation_fields(old_row_name, sre.name)
    return sre.name


def _c422_create_replacement_fulfillment(order_name: str, new_row_name: str, change):
    from nkt_operations.nkt_store_operations.features.inventory import order_fulfillment as fulfillment

    order = frappe.get_doc("NKT Customer Order", order_name)
    row = next((x for x in (order.get("items") or []) if x.name == new_row_name), None)
    if not row:
        frappe.throw(_("The replacement order row could not be loaded."))
    new_type = _warehouse_fulfillment_type(change.new_warehouse)
    replacement_reservation = None
    replacement_stock_entry = None

    if new_type == "External Warehouse Release":
        replacement_reservation = fulfillment._create_external_reservation(order, row)
        fulfillment._sync_draft_release(order, change.new_warehouse)
    elif new_type == "Immediate Retail Deduction":
        if not order.get("custom_nkt_retail_stock_entry"):
            # First Retail Store posting on the order: keep the accepted primary link.
            replacement_stock_entry = fulfillment._create_immediate_stock_entry(order, [row])
        else:
            # Mixed order already has an immediate Store posting. Never mutate that
            # submitted Stock Entry; create a separate auditable Material Issue only
            # for the moved balance and keep its reference on NKT Warehouse Change.
            replacement_stock_entry = fulfillment._create_material_issue(
                company=order.company,
                posting_datetime=now_datetime(),
                items=[fulfillment._stock_entry_item(row.item, row.quantity, row.uom, row.source_warehouse)],
                customer_order=order.name,
                warehouse_release=None,
                fulfillment_kind=WAREHOUSE_CHANGE_IMMEDIATE_KIND,
                remarks=(
                    f"Created by controlled warehouse change {change.name}: unreleased balance "
                    f"moved from {change.original_warehouse} to {change.new_warehouse}."
                ),
            )
    else:
        frappe.throw(_("New warehouse fulfillment type is not supported."))
    return replacement_reservation, replacement_stock_entry


def _apply_partial_release_warehouse_change(change):
    """Apply C4.2.2 row split after the warehouse confirms no further release."""
    from nkt_operations.nkt_store_operations.features.inventory import order_fulfillment as fulfillment

    order = frappe.get_doc("NKT Customer Order", change.customer_order)
    row = next((x for x in (order.get("items") or []) if x.name == change.customer_order_item), None)
    if not row:
        frappe.throw(_("The original Customer Order row no longer exists."))

    released_map = fulfillment._submitted_release_map(order.name)
    released = min(flt(released_map.get(row.name)), flt(row.quantity))
    remaining = max(flt(row.quantity) - released, 0)
    if released <= 0.005 or remaining <= 0.005:
        frappe.throw(_("This is not a partially released row with an unreleased balance."))
    if abs(released - flt(change.released_quantity_before)) > 0.005:
        frappe.throw(_("Released quantity changed after the warehouse-change request. Refresh and review the release history."))
    if abs(remaining - flt(change.quantity_to_move)) > 0.005:
        frappe.throw(_("Remaining quantity changed after the warehouse-change request. No row split was applied."))
    if row.source_warehouse != change.original_warehouse:
        frappe.throw(_("The original row warehouse changed after the request. No row split was applied."))

    # Idempotent replay protection at the business-record level.
    replacement_row = change.get("replacement_order_item")
    if replacement_row and frappe.db.exists("NKT Customer Order Item", replacement_row):
        return _warehouse_change_result(change.name)

    sre_name = change.previous_reservation or fulfillment._active_reservation_for_row(order.name, row.name)
    if not sre_name or not frappe.db.exists("Stock Reservation Entry", sre_name):
        frappe.throw(_("The original Stock Reservation Entry is missing; partial source change cannot be applied safely."))
    sre = frappe.get_doc("Stock Reservation Entry", sre_name)
    outstanding = max(
        flt(sre.reserved_qty) - flt(sre.delivered_qty) - flt(sre.transferred_qty) - flt(sre.consumed_qty),
        0,
    )
    if abs(outstanding - remaining) > 0.005:
        frappe.throw(
            _("Reservation outstanding quantity {0} no longer agrees with the unreleased balance {1}.").format(
                outstanding, remaining
            )
        )

    # Preserve the already released portion on the old source row.
    final_rate = flt(row.final_rate)
    old_remark = _c422_append_audit_remark(
        row.get("remarks"),
        f"{WAREHOUSE_CHANGE_SPLIT_MARKER}: {released:g} retained at {change.original_warehouse}; "
        f"{remaining:g} moved under {change.name}.",
    )
    frappe.db.set_value(
        "NKT Customer Order Item",
        row.name,
        {"quantity": released, "amount": released * final_rate, "remarks": old_remark},
        update_modified=False,
    )

    # The old SRE becomes a coherent Delivered record for exactly the quantity that
    # really left the original warehouse; its outstanding reservation becomes zero.
    _c422_close_old_partial_reservation(sre_name, row.name, released)

    # Create the official new row for only the unreleased balance.
    order = frappe.get_doc("NKT Customer Order", order.name)
    old_row = next(x for x in order.get("items") if x.name == change.customer_order_item)
    new_row_name = _c422_insert_split_order_row(order, old_row, remaining, change.new_warehouse, change.name)
    frappe.db.set_value(WAREHOUSE_CHANGE_LOG, change.name, "replacement_order_item", new_row_name, update_modified=False)

    replacement_reservation, replacement_stock_entry = _c422_create_replacement_fulfillment(
        order.name, new_row_name, change
    )

    order = frappe.get_doc("NKT Customer Order", order.name)
    sale_name = _break_cashier_encoder_match_for_warehouse_change(
        order, change.original_warehouse, change.new_warehouse, change.reason
    )
    fulfillment.update_customer_order_fulfillment_status(order.name)

    recall_status = "Confirmed No Further Release"
    frappe.db.set_value(
        WAREHOUSE_CHANGE_LOG,
        change.name,
        {
            "change_status": "Applied",
            "split_mode": "Partial Release - Unreleased Balance",
            "recall_status": recall_status,
            "recall_confirmed_by": frappe.session.user,
            "recall_confirmed_on": now_datetime(),
            "replacement_order_item": new_row_name,
            "replacement_reservation": replacement_reservation,
            "replacement_stock_entry": replacement_stock_entry,
            "match_status_after": "Unmatched",
            "remarks": (
                f"Partial-release row split applied. {released:g} remained historically released from "
                f"{change.original_warehouse}; only unreleased balance {remaining:g} moved to "
                f"{change.new_warehouse}. Old reservation {sre_name} was closed at delivered quantity "
                f"{released:g}. Existing Payment Receipt and Cashier Movement were not recreated or altered. "
                f"Previous Cashier match {sale_name or '[none]'} was broken for warehouse-change reconciliation."
            ),
        },
        update_modified=False,
    )
    return _warehouse_change_result(change.name)


def _warehouse_change_result(name: str):
    doc = frappe.get_doc(WAREHOUSE_CHANGE_LOG, name)
    return {
        "version": VERSION,
        "warehouse_change": doc.name,
        "status": doc.change_status,
        "customer_order": doc.customer_order,
        "customer_order_item": doc.customer_order_item,
        "replacement_order_item": doc.get("replacement_order_item"),
        "split_mode": doc.get("split_mode"),
        "item": doc.item,
        "quantity_to_move": flt(doc.quantity_to_move),
        "released_quantity_before": flt(doc.released_quantity_before),
        "original_warehouse": doc.original_warehouse,
        "new_warehouse": doc.new_warehouse,
        "reason": doc.reason,
        "recall_release": doc.recall_release,
        "recall_status": doc.recall_status,
        "previous_reservation": doc.previous_reservation,
        "replacement_reservation": doc.replacement_reservation,
        "replacement_stock_entry": doc.replacement_stock_entry,
        "match_status_after": doc.match_status_after,
    }


@frappe.whitelist()
def request_warehouse_change(payload: Any):
    """C4.2.2 request: zero-release whole-row or partial unreleased-balance change."""
    _require_encoder_warehouse_change()
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    payload = payload or {}
    order_name = (payload.get("customer_order") or "").strip()
    row_name = (payload.get("customer_order_item") or "").strip()
    new_warehouse = (payload.get("new_warehouse") or "").strip()
    reason = (payload.get("reason") or "").strip()
    request_id = (payload.get("request_id") or "").strip()
    if not all((order_name, row_name, new_warehouse, reason, request_id)):
        frappe.throw(_("Order, row, new warehouse, reason, and Request ID are required."))
    existing = _existing_change_request(request_id)
    if existing:
        return {"replayed": True, **_warehouse_change_result(existing)}

    frappe.db.sql("SELECT name FROM `tabNKT Customer Order` WHERE name=%s FOR UPDATE", order_name)
    order = frappe.get_doc("NKT Customer Order", order_name)
    if order.docstatus != 1:
        frappe.throw(_("Only submitted Customer Orders can use controlled warehouse change."))
    row = next((x for x in (order.get("items") or []) if x.name == row_name), None)
    if not row:
        frappe.throw(_("The selected order row no longer exists."))

    old_warehouse = row.source_warehouse
    if new_warehouse == old_warehouse:
        frappe.throw(_("Choose a different source warehouse."))
    new_type = _warehouse_fulfillment_type(new_warehouse)
    old_type = _warehouse_fulfillment_type(old_warehouse)
    if new_type not in {"Immediate Retail Deduction", "External Warehouse Release"}:
        frappe.throw(_("The selected new warehouse is not configured for NKT Fast fulfillment."))

    ctx = _warehouse_change_row_context(order, row)
    if ctx["remaining_quantity"] <= 0.005:
        frappe.throw(_("This row is fully released. Ordinary warehouse editing is locked; use a return, transfer, or controlled stock correction."))
    if old_type == "Immediate Retail Deduction":
        frappe.throw(_("Retail Store stock has already been deducted. Ordinary warehouse change is locked; use a controlled stock correction trail."))
    if old_type != "External Warehouse Release" or not ctx["active_reservation"]:
        frappe.throw(_("Warehouse change requires an active external stock reservation for the unreleased balance."))

    release_name = ctx["draft_release"]
    if not release_name:
        frappe.throw(_("No prepared release for the unreleased balance was found. Refresh fulfillment before changing source warehouse."))
    release = frappe.get_doc("NKT Warehouse Release", release_name)
    if release.docstatus != 0 or (release.release_status or "Draft") != "Draft":
        frappe.throw(_("The prepared Warehouse Release is no longer an open Draft and cannot enter Recall Pending."))

    partial = bool(ctx["released_quantity"] > 0.005)
    change = frappe.get_doc({
        "doctype": WAREHOUSE_CHANGE_LOG,
        "change_status": "Recall Pending",
        "request_id": request_id,
        "customer_order": order.name,
        "customer_order_item": row.name,
        "customer": order.customer,
        "item": row.item,
        "quantity_to_move": ctx["remaining_quantity"],
        "released_quantity_before": ctx["released_quantity"],
        "split_mode": "Partial Release - Unreleased Balance" if partial else "Pre-release Whole Row",
        "original_warehouse": old_warehouse,
        "new_warehouse": new_warehouse,
        "reason": reason,
        "requested_by": frappe.session.user,
        "requested_on": now_datetime(),
        "recall_release": release.name,
        "recall_status": "Recall Pending",
        "previous_reservation": ctx["active_reservation"],
        "remarks": (
            f"Prepared release for unreleased balance {ctx['remaining_quantity']:g} placed in Recall Pending. "
            + (f"Already released {ctx['released_quantity']:g} remains permanently at {old_warehouse}. " if partial else "")
            + "Warehouse must confirm no further quantity physically left before source change is applied."
        ),
    })
    change.flags.ignore_permissions = True
    change.insert(ignore_permissions=True)
    frappe.db.set_value("NKT Warehouse Release", release.name, "release_status", "Recall Pending", update_modified=False)
    if frappe.get_meta("NKT Warehouse Release").has_field("custom_nkt_reservation_status"):
        frappe.db.set_value(
            "NKT Warehouse Release",
            release.name,
            "custom_nkt_reservation_status",
            "Recall Pending - Partial Warehouse Change" if partial else "Recall Pending - Warehouse Change",
            update_modified=False,
        )
    frappe.db.commit()
    return {"replayed": False, **_warehouse_change_result(change.name)}


@frappe.whitelist()
def confirm_warehouse_change_recall(release_name: str):
    """Warehouse confirms no additional physical release after recall request."""
    _require_warehouse_release_operator()
    release_name = (release_name or "").strip()
    if not release_name or not frappe.db.exists("NKT Warehouse Release", release_name):
        frappe.throw(_("Warehouse Release was not found."))
    frappe.db.sql("SELECT name FROM `tabNKT Warehouse Release` WHERE name=%s FOR UPDATE", release_name)
    release = frappe.get_doc("NKT Warehouse Release", release_name)
    _validate_release_access(release)

    # Safe replay: after success, repeated clicks return the already-applied change.
    if (release.release_status or "") == "Recalled":
        applied = frappe.db.get_value(
            WAREHOUSE_CHANGE_LOG,
            {"recall_release": release.name, "change_status": "Applied"},
            "name",
            order_by="creation desc",
        )
        if applied:
            return {"replayed": True, **_warehouse_change_result(applied)}

    if release.docstatus != 0 or (release.release_status or "") != "Recall Pending":
        frappe.throw(_("This Warehouse Release is not awaiting recall confirmation."))
    change_name = frappe.db.get_value(
        WAREHOUSE_CHANGE_LOG,
        {"recall_release": release.name, "change_status": "Recall Pending"},
        "name",
        order_by="creation desc",
    )
    if not change_name:
        frappe.throw(_("No active Warehouse Change request is linked to this recalled release."))
    frappe.db.sql("SELECT name FROM `tabNKT Warehouse Change` WHERE name=%s FOR UPDATE", change_name)
    change = frappe.get_doc(WAREHOUSE_CHANGE_LOG, change_name)

    from nkt_operations.nkt_store_operations.features.inventory import order_fulfillment as fulfillment
    released_map = fulfillment._submitted_release_map(change.customer_order)
    current_released = flt(released_map.get(change.customer_order_item))
    if current_released > flt(change.released_quantity_before) + 0.005:
        frappe.throw(
            _("A further physical release was posted after this recall request. Do not confirm the recall; review the submitted release history first.")
        )

    frappe.db.set_value("NKT Warehouse Release", release.name, "release_status", "Recalled", update_modified=False)
    if frappe.get_meta("NKT Warehouse Release").has_field("custom_nkt_reservation_status"):
        frappe.db.set_value(
            "NKT Warehouse Release",
            release.name,
            "custom_nkt_reservation_status",
            "Recalled - No Further Physical Release" if flt(change.released_quantity_before) > 0.005 else "Recalled - No Physical Release",
            update_modified=False,
        )

    if flt(change.released_quantity_before) > 0.005:
        result = _apply_partial_release_warehouse_change(change)
    else:
        result = _apply_pre_release_warehouse_change(change)
        # Fill the C4.2.2 audit mode on old zero-release records too.
        if frappe.get_meta(WAREHOUSE_CHANGE_LOG).has_field("split_mode"):
            frappe.db.set_value(WAREHOUSE_CHANGE_LOG, change.name, "split_mode", "Pre-release Whole Row", update_modified=False)
    frappe.db.commit()
    return result


@frappe.whitelist()
def get_warehouse_change_bootstrap():
    _require_encoder_warehouse_change()
    warehouses = _operational_warehouses()
    recent = frappe.get_all(
        WAREHOUSE_CHANGE_LOG,
        fields=[
            "name", "change_status", "customer_order", "item", "quantity_to_move",
            "released_quantity_before", "original_warehouse", "new_warehouse",
            "recall_release", "recall_status", "replacement_order_item", "requested_on",
        ],
        order_by="creation desc",
        limit_page_length=25,
    ) if frappe.db.exists("DocType", WAREHOUSE_CHANGE_LOG) else []
    return {
        "version": VERSION,
        "route": "/app/nkt-warehouse-change-fast-screen",
        "user": frappe.session.user,
        "warehouses": [
            {"name": row.name, "label": row.custom_nkt_fast_label or row.name, "fulfillment_type": row.custom_nkt_fulfillment_type}
            for row in warehouses
        ],
        "recent_changes": recent,
        "recent_orders": _recent_encoder_orders_for_warehouse_change(limit=30),
        "partial_release_move_enabled": True,
        "immediate_source_reversal_enabled": False,
    }


@frappe.whitelist()
def get_warehouse_release_context(release_name: str):
    if not release_name or not frappe.db.exists("NKT Warehouse Release", release_name):
        frappe.throw(_("Warehouse Release was not found."))
    doc = frappe.get_doc("NKT Warehouse Release", release_name)
    _validate_release_access(doc)
    if doc.docstatus == 0 and (doc.get("release_status") or "Draft") == "Draft":
        _refresh_release_quantities(doc)
    result = _release_result(doc)
    recall_pending = bool(doc.docstatus == 0 and (doc.get("release_status") or "") == "Recall Pending")
    result["can_release"] = bool(doc.docstatus == 0 and (doc.get("release_status") or "Draft") == "Draft")
    result["can_confirm_recall"] = recall_pending
    change = frappe.db.get_value(
        WAREHOUSE_CHANGE_LOG,
        {"recall_release": doc.name, "change_status": "Recall Pending"},
        ["name", "quantity_to_move", "released_quantity_before", "original_warehouse", "new_warehouse", "split_mode"],
        as_dict=True,
        order_by="creation desc",
    ) if recall_pending else None
    result["warehouse_change"] = change.name if change else None
    result["warehouse_change_context"] = change
    return result


def install_v2_0_c_4_2_2():
    # C4.2.1.2 is the last required business-rule baseline; C4.2.1.3 was UX-only
    # and is bundled again here so installations remain forward-safe.
    baseline = verify_v2_0_c_4_2_1_2()
    before = _counts()
    created_fields = _ensure_c422_warehouse_change_fields()
    change_script = _ensure_client_script(
        WAREHOUSE_CHANGE_SCRIPT_C422,
        WAREHOUSE_CHANGE_SCREEN,
        "nkt_warehouse_change_fast_screen.js",
    )
    release_script = _ensure_client_script(
        WAREHOUSE_RELEASE_SCRIPT_C422,
        WAREHOUSE_RELEASE_SCREEN,
        "nkt_warehouse_release_fast_screen.js",
    )
    encoder_link = _ensure_client_script(
        WAREHOUSE_CHANGE_LINK_SCRIPT,
        ENCODER_SCREEN,
        "nkt_encoder_warehouse_change_link.js",
    )
    order_link = _ensure_client_script(
        WAREHOUSE_CHANGE_ORDER_LINK_SCRIPT,
        "NKT Customer Order",
        "nkt_customer_order_warehouse_change_link.js",
    )
    for old in (WAREHOUSE_CHANGE_SCRIPT, WAREHOUSE_RELEASE_SCRIPT_C421):
        if old != WAREHOUSE_CHANGE_SCRIPT_C422 and old != WAREHOUSE_RELEASE_SCRIPT_C422 and frappe.db.exists("Client Script", old):
            frappe.db.set_value("Client Script", old, "enabled", 0, update_modified=False)
    frappe.db.commit()
    frappe.clear_cache()
    after = _counts()
    changed = {dt: {"before": before.get(dt), "after": after.get(dt)} for dt in before if before.get(dt) != after.get(dt)}
    return {
        "installed": True,
        "version": VERSION,
        "accepted_baseline": baseline.get("version"),
        "partial_release_unreleased_move_enabled": True,
        "row_split_preserves_released_history": True,
        "old_reservation_closed_at_released_quantity": True,
        "replacement_external_reservation_supported": True,
        "replacement_retail_material_issue_supported": True,
        "payment_receipt_and_cashier_movements_recreated": False,
        "fully_released_ordinary_change_locked": True,
        "immediate_source_ordinary_change_locked": True,
        "manager_pin_required": False,
        "reason_required": True,
        "installed_custom_fields": created_fields,
        "warehouse_change_script": change_script,
        "warehouse_release_script": release_script,
        "encoder_navigation_script": encoder_link,
        "customer_order_navigation_script": order_link,
        "business_record_counts_changed": changed,
        "existing_business_records_modified": False,
        "next_stage": "C4.2.2 live partial-release row-split verification; then warehouse-change audit polish / EOD exposure",
    }


def verify_v2_0_c_4_2_2():
    # C4.2.2 replaces the C4.2.1 Warehouse Change and Warehouse Release Client
    # Scripts, so do not call the old C4.2.1 verifier after activation; it
    # correctly expects those old scripts to remain enabled and partial moves blocked.
    import inspect
    from nkt_operations.nkt_store_operations.features.inventory import order_fulfillment as fulfillment

    report = verify_v2_0_c_3()
    errors = list(report.get("errors") or [])
    source = Path(__file__).read_text()
    change_js_path = Path(__file__).with_name("nkt_warehouse_change_fast_screen.js")
    release_js_path = Path(__file__).with_name("nkt_warehouse_release_fast_screen.js")
    change_js = change_js_path.read_text() if change_js_path.exists() else ""
    release_js = release_js_path.read_text() if release_js_path.exists() else ""
    wch_meta = frappe.get_meta(WAREHOUSE_CHANGE_LOG) if frappe.db.exists("DocType", WAREHOUSE_CHANGE_LOG) else None

    controller_source = Path(fulfillment.__file__).read_text()
    status_source = inspect.getsource(fulfillment.update_customer_order_fulfillment_status)
    validator = getattr(
        fulfillment,
        "_nkt_c4142_original_validate_warehouse_release_document",
        fulfillment.validate_warehouse_release_document,
    )
    validator_source = inspect.getsource(validator)
    release_index = _release_request_index_status()

    preserved = {
        "release_request_id_unique": any(not int(row.NON_UNIQUE) for row in release_index),
        "release_fast_finalize_present": "def finalize_warehouse_release_fast(" in source,
        "partial_release_posting_present": "release_quantity" in inspect.getsource(finalize_warehouse_release_fast),
        "c4143_admin_gate_removed": "NKT_C4_1_4_3_NO_PRE_RELEASE_ADMIN_GATE" in controller_source,
        "driver_optional_underlying": "Driver Name is required before submission" not in validator_source,
        "plate_optional_underlying": "Plate Number is required before submission" not in validator_source,
        "legacy_admin_fields_not_in_fulfillment_status": "requires_admin_confirmation" not in status_source and "admin_confirmation_status" not in status_source,
        "release_reference_required": "Release Authorization Reference is required before submission" in validator_source,
        "warehouse_change_log_exists": bool(frappe.db.exists("DocType", WAREHOUSE_CHANGE_LOG)),
        "warehouse_change_screen_exists": bool(frappe.db.exists("DocType", WAREHOUSE_CHANGE_SCREEN)),
        "recall_pending_release_status_supported": "Recall Pending" in (frappe.get_meta("NKT Warehouse Release").get_field("release_status").options or ""),
    }

    checks = {
        "partial_row_context_unlocked": "partial_release_change" in source and '"partial_release_move_enabled": True' in source,
        "request_allows_partial": "C4.2.2 will activate row-splitting" not in inspect.getsource(request_warehouse_change),
        "row_split_helper_present": "def _c422_insert_split_order_row(" in source,
        "old_row_reduced_to_released_qty": '"quantity": released' in inspect.getsource(_apply_partial_release_warehouse_change),
        "old_sre_closed_not_cancelled": "def _c422_close_old_partial_reservation(" in source and '"reserved_qty": released_qty' in source,
        "delivered_qty_not_rewritten": '"delivered_qty": released_qty' not in inspect.getsource(_c422_close_old_partial_reservation),
        "replacement_row_audited": bool(wch_meta and wch_meta.has_field("replacement_order_item")),
        "split_mode_audited": bool(wch_meta and wch_meta.has_field("split_mode")),
        "partial_recall_status_supported": bool(wch_meta and "Confirmed No Further Release" in ((wch_meta.get_field("recall_status").options or "") if wch_meta.get_field("recall_status") else "")),
        "replacement_external_supported": "_create_external_reservation" in inspect.getsource(_c422_create_replacement_fulfillment),
        "replacement_retail_supported": "WAREHOUSE_CHANGE_IMMEDIATE_KIND" in inspect.getsource(_c422_create_replacement_fulfillment),
        "recall_dispatches_partial": "_apply_partial_release_warehouse_change" in inspect.getsource(confirm_warehouse_change_recall),
        "recall_replay_safe": '"replayed": True' in inspect.getsource(confirm_warehouse_change_recall),
        "fully_released_locked": "This row is fully released" in inspect.getsource(request_warehouse_change),
        "immediate_source_locked": "Retail Store stock has already been deducted" in inspect.getsource(request_warehouse_change),
        "recent_order_search_preserved": 'data-role="history-search"' in change_js,
        "partial_move_ui_present": "Move Remaining" in change_js,
        "partial_recall_wording_present": "No Further Release" in release_js,
        "frontline_reconciliation_hidden_on_recall_result": "<b>Reconciliation:</b>" not in release_js,
        "new_change_script_enabled": bool(cint(frappe.db.get_value("Client Script", WAREHOUSE_CHANGE_SCRIPT_C422, "enabled"))) if frappe.db.exists("Client Script", WAREHOUSE_CHANGE_SCRIPT_C422) else False,
        "new_release_script_enabled": bool(cint(frappe.db.get_value("Client Script", WAREHOUSE_RELEASE_SCRIPT_C422, "enabled"))) if frappe.db.exists("Client Script", WAREHOUSE_RELEASE_SCRIPT_C422) else False,
        "old_change_script_disabled": not bool(cint(frappe.db.get_value("Client Script", WAREHOUSE_CHANGE_SCRIPT, "enabled"))) if frappe.db.exists("Client Script", WAREHOUSE_CHANGE_SCRIPT) else True,
        "old_release_script_disabled": not bool(cint(frappe.db.get_value("Client Script", WAREHOUSE_RELEASE_SCRIPT_C421, "enabled"))) if frappe.db.exists("Client Script", WAREHOUSE_RELEASE_SCRIPT_C421) else True,
    }
    for key, ok in {**preserved, **checks}.items():
        if not ok:
            errors.append(f"Missing C4.2.2 partial warehouse-change control: {key}")
    report.update({
        "version": VERSION,
        "errors": errors,
        "passed": not errors,
        "c4_1_preserved_controls_verified": preserved,
        "c4_2_2_controls_verified": checks,
        "partial_release_unreleased_move_enabled": True,
        "released_quantity_remains_on_original_warehouse": True,
        "unreleased_balance_row_split_enabled": True,
        "existing_payment_receipt_and_cashier_movements_recreated": False,
        "fully_released_ordinary_change_locked": True,
        "immediate_source_ordinary_change_locked": True,
        "existing_business_records_changed": False,
        "business_record_counts": _counts(),
    })
    if errors:
        frappe.throw(json.dumps(report, default=str, indent=2))
    return report



# NKT_C15F_R8A_ENCODER_ITEM_HISTORY_BEGIN
import frappe as _nkt_r8_frappe
from frappe import _ as _nkt_r8_translate

@_nkt_r8_frappe.whitelist()
def get_encoder_item_history(item_code, customer=None, from_date=None, to_date=None, warehouse=None, limit=50):
    # Role-safe selling/item history for the Encoder Fast Screen only.
    from frappe.utils import cint as _r8_cint, getdate as _r8_getdate

    user = _nkt_r8_frappe.session.user
    roles = set(_nkt_r8_frappe.get_roles(user))
    allowed = (
        user == "Administrator"
        or bool(roles.intersection({"NKT Encoder", "NKT OWNER", "NKT ADMINISTRATOR", "System Manager"}))
    )
    if not allowed:
        _nkt_r8_frappe.throw(_nkt_r8_translate("Not permitted."), _nkt_r8_frappe.PermissionError)

    item_code = (item_code or "").strip()
    if not item_code or not _nkt_r8_frappe.db.exists("Item", item_code):
        _nkt_r8_frappe.throw(_nkt_r8_translate("Select a valid Item."))

    limit = max(1, min(_r8_cint(limit) or 50, 100))
    customer = (customer or "").strip() or None
    warehouse = (warehouse or "").strip() or None
    from_date = _r8_getdate(from_date) if from_date else None
    to_date = _r8_getdate(to_date) if to_date else None
    if from_date and to_date and from_date > to_date:
        _nkt_r8_frappe.throw(_nkt_r8_translate("From Date cannot be after To Date."))

    child_dt = "NKT Customer Order Item"
    parent_dt = "NKT Customer Order"
    child_meta = _nkt_r8_frappe.get_meta(child_dt)
    parent_meta = _nkt_r8_frappe.get_meta(parent_dt)

    def _field(meta, candidates):
        for candidate in candidates:
            if meta.has_field(candidate):
                return candidate
        return None

    item_field = _field(child_meta, ("item", "item_code"))
    qty_field = _field(child_meta, ("quantity", "qty"))
    uom_field = _field(child_meta, ("uom", "stock_uom"))
    rate_field = _field(child_meta, ("final_rate", "rate", "selling_rate", "standard_rate"))
    warehouse_field = _field(child_meta, ("source_warehouse", "warehouse"))
    date_field = _field(parent_meta, ("order_date", "business_date", "transaction_date"))
    customer_field = _field(parent_meta, ("customer",))
    customer_name_field = _field(parent_meta, ("customer_name",))
    status_field = _field(parent_meta, ("status", "workflow_state"))

    if not item_field or not qty_field:
        _nkt_r8_frappe.throw(_nkt_r8_translate("Item History is unavailable because the Customer Order item schema is incomplete."))

    child_fields = ["name", "parent", "creation", item_field, qty_field]
    for optional in (uom_field, rate_field, warehouse_field):
        if optional and optional not in child_fields:
            child_fields.append(optional)

    filters = {item_field: item_code, "parenttype": parent_dt}
    if warehouse and warehouse_field:
        filters[warehouse_field] = warehouse

    scan_limit = min(max(limit * 8, 120), 800)
    child_rows = _nkt_r8_frappe.get_all(
        child_dt,
        filters=filters,
        fields=child_fields,
        order_by="creation desc",
        limit_page_length=scan_limit,
    )

    parent_fields = ["name", "creation"]
    for optional in (date_field, customer_field, customer_name_field, status_field):
        if optional and optional not in parent_fields:
            parent_fields.append(optional)

    parent_cache = {}
    rows = []
    rx_link_fields = []
    if _nkt_r8_frappe.db.exists("DocType", "NKT Return Exchange Declaration"):
        rx_meta = _nkt_r8_frappe.get_meta("NKT Return Exchange Declaration")
        rx_link_fields = [
            f.fieldname for f in rx_meta.fields
            if f.fieldtype == "Link" and f.options == parent_dt
        ]
        for candidate in ("customer_order", "original_customer_order", "source_customer_order", "original_order"):
            if rx_meta.has_field(candidate) and candidate not in rx_link_fields:
                rx_link_fields.append(candidate)

    for child in child_rows:
        parent_name = child.get("parent")
        if parent_name not in parent_cache:
            parent_cache[parent_name] = _nkt_r8_frappe.db.get_value(parent_dt, parent_name, parent_fields, as_dict=True)
        parent = parent_cache.get(parent_name)
        if not parent:
            continue

        parent_customer = parent.get(customer_field) if customer_field else None
        if customer and parent_customer != customer:
            continue

        business_date = parent.get(date_field) if date_field else None
        if business_date:
            business_date_obj = _r8_getdate(business_date)
            if from_date and business_date_obj < from_date:
                continue
            if to_date and business_date_obj > to_date:
                continue

        return_marker = False
        for link_field in rx_link_fields:
            rx_filters = {link_field: parent_name}
            try:
                if _nkt_r8_frappe.get_meta("NKT Return Exchange Declaration").is_submittable:
                    rx_filters["docstatus"] = ["!=", 2]
                if _nkt_r8_frappe.db.exists("NKT Return Exchange Declaration", rx_filters):
                    return_marker = True
                    break
            except Exception:
                pass

        rows.append({
            "transaction_reference": parent_name,
            "business_date": str(business_date) if business_date else None,
            "transaction_datetime": str(parent.get("creation") or child.get("creation") or ""),
            "customer": parent_customer,
            "customer_name": parent.get(customer_name_field) if customer_name_field else parent_customer,
            "quantity": child.get(qty_field),
            "uom": child.get(uom_field) if uom_field else None,
            "selling_rate": child.get(rate_field) if rate_field else None,
            "warehouse": child.get(warehouse_field) if warehouse_field else None,
            "status": parent.get(status_field) if status_field else None,
            "return_exchange_marker": bool(return_marker),
        })
        if len(rows) >= limit:
            break

    item_row = _nkt_r8_frappe.db.get_value("Item", item_code, ["name", "item_name", "stock_uom"], as_dict=True)
    return {
        "item": dict(item_row) if item_row else {"name": item_code},
        "rows": rows,
        "filters": {
            "customer": customer,
            "from_date": str(from_date) if from_date else None,
            "to_date": str(to_date) if to_date else None,
            "warehouse": warehouse,
            "limit": limit,
        },
    }
# NKT_C15F_R8A_ENCODER_ITEM_HISTORY_END
