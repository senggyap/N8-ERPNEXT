from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, now_datetime, today


VERSION = "1.8.0"
MODULE = "NKT Store Operations"
TOLERANCE = 0.005

RECEIVABLE = "NKT Customer Receivable"
ORDER = "NKT Customer Order"
VERIFICATION = "NKT Encoder Account Allocation"
ALLOCATION_ROW = "NKT Account Allocation Row"
CASHIER_COLLECTION = "NKT Cashier Account Collection"
PAYMENT_RECEIPT = "NKT Payment Receipt"
STATEMENT = "NKT Customer Statement"

AGING_ALERT = "NKT Account Aging Alert"
CONTROL_LOG = "NKT Account Control Log"
DELIVERY_LOG = "NKT Statement Delivery Log"
CORRECTION = "NKT Account Payment Correction"
CORRECTION_ROW = "NKT Account Payment Correction Allocation"
CLEANUP_LOG = "NKT Test Data Cleanup Log"

AUTHORIZED_CONTROL_ROLES = {
    "System Manager",
    "NKT OWNER",
    "NKT ADMINISTRATOR",
    "NKT Credit Controller",
}
ADMIN_ROLES = {"System Manager", "NKT OWNER", "NKT ADMINISTRATOR"}


def _has_role(roles):
    user_roles = set(frappe.get_roles(frappe.session.user))
    return bool(user_roles.intersection(set(roles)))


def _require_roles(roles, action):
    if frappe.session.user == "Administrator" or _has_role(roles):
        return
    frappe.throw(_("You are not authorized to {0}.").format(action), frappe.PermissionError)


def _field_exists(doctype, fieldname):
    if not frappe.db.exists("DocType", doctype):
        return False
    return bool(frappe.get_meta(doctype).has_field(fieldname))


def _table_exists(doctype):
    return bool(frappe.db.exists("DocType", doctype))


def _append_missing_fields(doctype, fields):
    if not frappe.db.exists("DocType", doctype):
        frappe.throw(_("Required DocType is missing: {0}").format(doctype))
    meta = frappe.get_meta(doctype)
    missing = [field for field in fields if field.get("fieldname") and not meta.has_field(field["fieldname"])]
    if not missing:
        return False

    is_custom_doctype = cint(frappe.db.get_value("DocType", doctype, "custom"))
    if is_custom_doctype:
        doc = frappe.get_doc("DocType", doctype)
        for field in missing:
            doc.append("fields", field)
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
    else:
        custom_field_meta = frappe.get_meta("Custom Field")
        for field in missing:
            values = {"doctype": "Custom Field", "dt": doctype}
            for key, value in field.items():
                if key == "fieldname" or custom_field_meta.has_field(key):
                    values[key] = value
            custom_field = frappe.get_doc(values)
            custom_field.flags.ignore_permissions = True
            custom_field.insert(ignore_permissions=True)
    frappe.clear_cache(doctype=doctype)
    return True


def _base_permissions(*, cashier_read=False, encoder_write=False):
    permissions = []
    for role in ("System Manager", "NKT OWNER", "NKT ADMINISTRATOR", "NKT Credit Controller"):
        permissions.append(
            {
                "role": role,
                "read": 1,
                "write": 1,
                "create": 1,
                "delete": 1 if role in ADMIN_ROLES else 0,
                "report": 1,
                "export": 1,
                "print": 1,
                "email": 1,
                "share": 1 if role in ADMIN_ROLES else 0,
            }
        )
    permissions.append(
        {
            "role": "NKT Encoder",
            "read": 1,
            "write": 1 if encoder_write else 0,
            "create": 1 if encoder_write else 0,
            "report": 1,
            "print": 1,
        }
    )
    if cashier_read:
        permissions.append({"role": "NKT Cashier", "read": 1, "report": 1, "print": 1})
    return permissions


def _ensure_custom_doctype(name, autoname, fields, permissions, *, istable=False):
    if frappe.db.exists("DocType", name):
        doc = frappe.get_doc("DocType", name)
        existing = {row.fieldname for row in (doc.get("fields") or []) if row.fieldname}
        changed = False
        for field in fields:
            if field.get("fieldname") and field["fieldname"] not in existing:
                doc.append("fields", field)
                changed = True
        if not istable and not doc.get("permissions"):
            for permission in permissions:
                doc.append("permissions", permission)
            changed = True
        if changed:
            doc.flags.ignore_permissions = True
            doc.save(ignore_permissions=True)
        return

    values = {
        "doctype": "DocType",
        "name": name,
        "module": MODULE,
        "custom": 1,
        "track_changes": 1,
        "allow_rename": 0,
        "fields": fields,
        "sort_field": "creation",
        "sort_order": "DESC",
    }
    if istable:
        values["istable"] = 1
    else:
        values.update(
            {
                "autoname": autoname,
                "permissions": permissions,
                "allow_import": 0,
                "allow_bulk_edit": 0,
            }
        )
    doc = frappe.get_doc(values)
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)


def _ensure_customer_fields():
    _append_missing_fields(
        "Customer",
        [
            {"fieldname": "custom_nkt_v18_account_control", "label": "NKT Account Control V1.8", "fieldtype": "Section Break", "insert_after": "custom_nkt_available_credit"},
            {"fieldname": "custom_nkt_manual_account_hold", "label": "Manual Account Hold", "fieldtype": "Check", "default": "0", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_manual_hold_reason", "label": "Manual Hold Reason", "fieldtype": "Small Text", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_manual_hold_by", "label": "Manual Hold Set By", "fieldtype": "Link", "options": "User", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_manual_hold_on", "label": "Manual Hold Set On", "fieldtype": "Datetime", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_max_overdue_days", "label": "Automatic Hold After Days Overdue", "fieldtype": "Int", "default": "0", "read_only": 1, "description": "0 disables automatic overdue hold."},
            {"fieldname": "custom_nkt_automatic_account_hold", "label": "Automatic Overdue Hold", "fieldtype": "Check", "default": "0", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_automatic_hold_reason", "label": "Automatic Hold Reason", "fieldtype": "Small Text", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_effective_account_hold", "label": "Effective Account Hold", "fieldtype": "Check", "default": "0", "read_only": 1, "in_list_view": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_overdue_balance", "label": "Overdue Balance", "fieldtype": "Currency", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_oldest_overdue_days", "label": "Oldest Days Overdue", "fieldtype": "Int", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_aging_alert_status", "label": "Aging Alert Status", "fieldtype": "Select", "options": "Clear\nDue Soon\nOverdue\nSeverely Overdue\nOn Hold", "default": "Clear", "read_only": 1, "in_list_view": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_last_aging_refresh", "label": "Last Aging Refresh", "fieldtype": "Datetime", "read_only": 1, "no_copy": 1},
        ],
    )


def _ensure_existing_doctype_fields():
    _append_missing_fields(
        RECEIVABLE,
        [
            {"fieldname": "nkt_v18_aging_section", "label": "Aging Control", "fieldtype": "Section Break"},
            {"fieldname": "custom_nkt_days_overdue", "label": "Days Overdue", "fieldtype": "Int", "read_only": 1, "in_list_view": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_aging_bucket", "label": "Aging Bucket", "fieldtype": "Select", "options": "Current\n1–30\n31–60\n61–90\nOver 90", "read_only": 1, "in_list_view": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_last_aging_refresh", "label": "Last Aging Refresh", "fieldtype": "Datetime", "read_only": 1, "no_copy": 1},
        ],
    )
    _append_missing_fields(
        VERIFICATION,
        [
            {"fieldname": "nkt_v18_correction_section", "label": "Application Correction", "fieldtype": "Section Break"},
            {"fieldname": "custom_nkt_application_status", "label": "Application Status", "fieldtype": "Select", "options": "Active\nReversed\nCorrected", "default": "Active", "read_only": 1, "in_list_view": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_correction_reference", "label": "Correction Reference", "fieldtype": "Link", "options": CORRECTION, "read_only": 1, "no_copy": 1},
        ],
    )
    _append_missing_fields(
        ALLOCATION_ROW,
        [
            {"fieldname": "custom_nkt_is_reversed", "label": "Reversed", "fieldtype": "Check", "default": "0", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_reversed_by_correction", "label": "Reversed By Correction", "fieldtype": "Link", "options": CORRECTION, "read_only": 1, "no_copy": 1},
        ],
    )
    _append_missing_fields(
        CASHIER_COLLECTION,
        [
            {"fieldname": "nkt_v18_correction_section", "label": "Account Application Correction", "fieldtype": "Section Break"},
            {"fieldname": "custom_nkt_application_status", "label": "Application Status", "fieldtype": "Select", "options": "Unmatched\nActive\nReversed\nCorrected", "default": "Unmatched", "read_only": 1, "in_list_view": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_correction_reference", "label": "Correction Reference", "fieldtype": "Link", "options": CORRECTION, "read_only": 1, "no_copy": 1},
        ],
    )
    _append_missing_fields(
        STATEMENT,
        [
            {"fieldname": "nkt_v18_delivery_section", "label": "Delivery Audit", "fieldtype": "Section Break"},
            {"fieldname": "custom_nkt_delivery_status", "label": "Delivery Status", "fieldtype": "Select", "options": "Not Sent\nPrinted\nEmailed\nHand Delivered\nMultiple Methods", "default": "Not Sent", "read_only": 1, "in_list_view": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_print_count", "label": "Print Count", "fieldtype": "Int", "default": "0", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_last_printed_on", "label": "Last Printed On", "fieldtype": "Datetime", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_last_printed_by", "label": "Last Printed By", "fieldtype": "Link", "options": "User", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_email_count", "label": "Email Count", "fieldtype": "Int", "default": "0", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_last_emailed_on", "label": "Last Emailed On", "fieldtype": "Datetime", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_last_emailed_by", "label": "Last Emailed By", "fieldtype": "Link", "options": "User", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_last_email_recipient", "label": "Last Email Recipient", "fieldtype": "Data", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_last_delivered_on", "label": "Last Hand Delivered On", "fieldtype": "Datetime", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_last_delivered_by", "label": "Last Hand Delivered By", "fieldtype": "Link", "options": "User", "read_only": 1, "no_copy": 1},
            {"fieldname": "custom_nkt_last_delivery_recipient", "label": "Last Hand Delivery Recipient", "fieldtype": "Data", "read_only": 1, "no_copy": 1},
        ],
    )
    if frappe.db.exists("DocType", ORDER):
        _append_missing_fields(
            ORDER,
            [
                {"fieldname": "nkt_v18_test_control", "label": "Test Data Control", "fieldtype": "Section Break"},
                {"fieldname": "custom_nkt_test_artifact", "label": "Test Artifact", "fieldtype": "Check", "default": "0", "read_only": 1, "no_copy": 1},
                {"fieldname": "custom_nkt_archived_from_operations", "label": "Archived from Operations", "fieldtype": "Check", "default": "0", "read_only": 1, "in_list_view": 1, "no_copy": 1},
                {"fieldname": "custom_nkt_test_archive_reason", "label": "Archive Reason", "fieldtype": "Small Text", "read_only": 1, "no_copy": 1},
                {"fieldname": "custom_nkt_test_archived_by", "label": "Archived By", "fieldtype": "Link", "options": "User", "read_only": 1, "no_copy": 1},
                {"fieldname": "custom_nkt_test_archived_on", "label": "Archived On", "fieldtype": "Datetime", "read_only": 1, "no_copy": 1},
            ],
        )


def _ensure_v18_doctypes():
    _ensure_custom_doctype(
        AGING_ALERT,
        "NKT-AGE-.YYYY.-.#####",
        [
            {"fieldname": "company", "label": "Company", "fieldtype": "Link", "options": "Company", "reqd": 1, "in_list_view": 1},
            {"fieldname": "customer", "label": "Customer", "fieldtype": "Link", "options": "Customer", "reqd": 1, "in_list_view": 1, "search_index": 1},
            {"fieldname": "as_of_date", "label": "As of Date", "fieldtype": "Date", "reqd": 1, "in_list_view": 1},
            {"fieldname": "total_outstanding", "label": "Total Outstanding", "fieldtype": "Currency", "read_only": 1, "in_list_view": 1},
            {"fieldname": "overdue_balance", "label": "Overdue Balance", "fieldtype": "Currency", "read_only": 1, "in_list_view": 1},
            {"fieldname": "oldest_days_overdue", "label": "Oldest Days Overdue", "fieldtype": "Int", "read_only": 1, "in_list_view": 1},
            {"fieldname": "severity", "label": "Severity", "fieldtype": "Select", "options": "Due Soon\nOverdue\nSeverely Overdue\nOn Hold", "read_only": 1, "in_list_view": 1},
            {"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "Open\nAcknowledged\nResolved", "default": "Open", "in_list_view": 1},
            {"fieldname": "message", "label": "Alert Message", "fieldtype": "Small Text", "read_only": 1},
            {"fieldname": "generated_by", "label": "Generated By", "fieldtype": "Link", "options": "User", "read_only": 1, "no_copy": 1},
            {"fieldname": "generated_on", "label": "Generated On", "fieldtype": "Datetime", "read_only": 1, "no_copy": 1},
            {"fieldname": "acknowledged_by", "label": "Acknowledged By", "fieldtype": "Link", "options": "User", "read_only": 1, "no_copy": 1},
            {"fieldname": "acknowledged_on", "label": "Acknowledged On", "fieldtype": "Datetime", "read_only": 1, "no_copy": 1},
            {"fieldname": "notes", "label": "Notes", "fieldtype": "Small Text"},
        ],
        _base_permissions(),
    )
    _ensure_custom_doctype(
        CONTROL_LOG,
        "NKT-CTL-.YYYY.-.#####",
        [
            {"fieldname": "company", "label": "Company", "fieldtype": "Link", "options": "Company", "in_list_view": 1},
            {"fieldname": "customer", "label": "Customer", "fieldtype": "Link", "options": "Customer", "reqd": 1, "in_list_view": 1},
            {"fieldname": "action", "label": "Action", "fieldtype": "Select", "options": "Manual Hold Placed\nManual Hold Cleared\nAutomatic Hold Applied\nAutomatic Hold Cleared\nAging Refreshed", "reqd": 1, "in_list_view": 1},
            {"fieldname": "reason", "label": "Reason", "fieldtype": "Small Text"},
            {"fieldname": "performed_by", "label": "Performed By", "fieldtype": "Link", "options": "User", "read_only": 1, "in_list_view": 1},
            {"fieldname": "performed_on", "label": "Performed On", "fieldtype": "Datetime", "read_only": 1, "in_list_view": 1},
            {"fieldname": "snapshot", "label": "Control Snapshot", "fieldtype": "Code", "options": "JSON", "read_only": 1},
        ],
        _base_permissions(),
    )
    _ensure_custom_doctype(
        DELIVERY_LOG,
        "NKT-SDL-.YYYY.-.#####",
        [
            {"fieldname": "statement", "label": "Customer Statement", "fieldtype": "Link", "options": STATEMENT, "reqd": 1, "in_list_view": 1},
            {"fieldname": "company", "label": "Company", "fieldtype": "Link", "options": "Company", "read_only": 1},
            {"fieldname": "customer", "label": "Customer", "fieldtype": "Link", "options": "Customer", "read_only": 1, "in_list_view": 1},
            {"fieldname": "delivery_method", "label": "Method", "fieldtype": "Select", "options": "Printed\nEmailed\nHand Delivered", "reqd": 1, "in_list_view": 1},
            {"fieldname": "recipient", "label": "Recipient", "fieldtype": "Data", "in_list_view": 1},
            {"fieldname": "delivered_by", "label": "Recorded By", "fieldtype": "Link", "options": "User", "read_only": 1, "in_list_view": 1},
            {"fieldname": "delivered_on", "label": "Recorded On", "fieldtype": "Datetime", "read_only": 1, "in_list_view": 1},
            {"fieldname": "notes", "label": "Notes", "fieldtype": "Small Text"},
        ],
        _base_permissions(encoder_write=True),
    )
    _ensure_custom_doctype(
        CORRECTION_ROW,
        None,
        [
            {"fieldname": "allocation_kind", "label": "Kind", "fieldtype": "Select", "options": "Original\nNew", "reqd": 1, "in_list_view": 1},
            {"fieldname": "source_allocation_row", "label": "Original Allocation Row", "fieldtype": "Data", "read_only": 1},
            {"fieldname": "receivable", "label": "Receivable", "fieldtype": "Link", "options": RECEIVABLE, "reqd": 1, "in_list_view": 1},
            {"fieldname": "customer_order", "label": "Customer Order", "fieldtype": "Link", "options": ORDER, "read_only": 1, "in_list_view": 1},
            {"fieldname": "allocated_amount", "label": "Amount", "fieldtype": "Currency", "reqd": 1, "in_list_view": 1},
            {"fieldname": "outstanding_before", "label": "Outstanding Before", "fieldtype": "Currency", "read_only": 1},
            {"fieldname": "outstanding_after", "label": "Outstanding After", "fieldtype": "Currency", "read_only": 1},
            {"fieldname": "application_rule", "label": "Application Rule", "fieldtype": "Data", "read_only": 1},
            {"fieldname": "is_applied", "label": "Applied", "fieldtype": "Check", "default": "0", "read_only": 1},
        ],
        [],
        istable=True,
    )
    _ensure_custom_doctype(
        CORRECTION,
        "NKT-COR-.YYYY.-.#####",
        [
            {"fieldname": "payment_verification", "label": "Payment Verification", "fieldtype": "Link", "options": VERIFICATION, "reqd": 1, "in_list_view": 1, "search_index": 1},
            {"fieldname": "company", "label": "Company", "fieldtype": "Link", "options": "Company", "read_only": 1, "in_list_view": 1},
            {"fieldname": "customer", "label": "Customer", "fieldtype": "Link", "options": "Customer", "read_only": 1, "in_list_view": 1},
            {"fieldname": "cashier_collection", "label": "Cashier Collection", "fieldtype": "Link", "options": CASHIER_COLLECTION, "read_only": 1},
            {"fieldname": "linked_payment_receipt", "label": "Existing Payment Receipt", "fieldtype": "Link", "options": PAYMENT_RECEIPT, "read_only": 1},
            {"fieldname": "original_allocation_date", "label": "Original Payment Date", "fieldtype": "Date", "read_only": 1},
            {"fieldname": "payment_amount", "label": "Payment Amount", "fieldtype": "Currency", "read_only": 1},
            {"fieldname": "correction_mode", "label": "Correction Mode", "fieldtype": "Select", "options": "Reverse Only\nReverse and Reapply", "default": "Reverse and Reapply", "reqd": 1, "in_list_view": 1},
            {"fieldname": "specific_customer_order", "label": "Specific Customer Order", "fieldtype": "Link", "options": ORDER, "description": "Optional. When provided, direct application takes priority."},
            {"fieldname": "reason", "label": "Correction Reason", "fieldtype": "Small Text", "reqd": 1},
            {"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "Draft\nPreviewed\nApplied", "default": "Draft", "read_only": 1, "in_list_view": 1},
            {"fieldname": "allocations", "label": "Original and New Allocations", "fieldtype": "Table", "options": CORRECTION_ROW, "read_only": 1},
            {"fieldname": "previewed_by", "label": "Previewed By", "fieldtype": "Link", "options": "User", "read_only": 1, "no_copy": 1},
            {"fieldname": "previewed_on", "label": "Previewed On", "fieldtype": "Datetime", "read_only": 1, "no_copy": 1},
            {"fieldname": "applied_by", "label": "Applied By", "fieldtype": "Link", "options": "User", "read_only": 1, "no_copy": 1},
            {"fieldname": "applied_on", "label": "Applied On", "fieldtype": "Datetime", "read_only": 1, "no_copy": 1},
        ],
        _base_permissions(),
    )
    _ensure_custom_doctype(
        CLEANUP_LOG,
        "NKT-TCL-.YYYY.-.#####",
        [
            {"fieldname": "customer_order", "label": "Customer Order", "fieldtype": "Link", "options": ORDER, "reqd": 1, "in_list_view": 1},
            {"fieldname": "action", "label": "Action", "fieldtype": "Select", "options": "Archived from Operations", "default": "Archived from Operations", "read_only": 1, "in_list_view": 1},
            {"fieldname": "reason", "label": "Reason", "fieldtype": "Small Text", "reqd": 1},
            {"fieldname": "precondition_snapshot", "label": "Precondition Snapshot", "fieldtype": "Code", "options": "JSON", "read_only": 1},
            {"fieldname": "performed_by", "label": "Performed By", "fieldtype": "Link", "options": "User", "read_only": 1, "in_list_view": 1},
            {"fieldname": "performed_on", "label": "Performed On", "fieldtype": "Datetime", "read_only": 1, "in_list_view": 1},
        ],
        _base_permissions(),
    )


def _ensure_client_script(name, dt, script):
    if frappe.db.exists("Client Script", name):
        doc = frappe.get_doc("Client Script", name)
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


def _customer_client_script():
    return r'''
frappe.ui.form.on('Customer', {
    refresh(frm) {
        if (frm.is_new()) return;
        const allowed = frappe.user.has_role('System Manager') ||
            frappe.user.has_role('NKT OWNER') ||
            frappe.user.has_role('NKT ADMINISTRATOR') ||
            frappe.user.has_role('NKT Credit Controller');
        if (!allowed) return;

        frm.add_custom_button(__('Refresh Aging Control'), () => {
            frappe.call({
                method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.refresh_customer_account_control',
                args: {customer: frm.doc.name},
                freeze: true,
                callback: () => frm.reload_doc()
            });
        }, __('NKT Account'));

        frm.add_custom_button(__('Set Overdue Hold Threshold'), () => {
            frappe.prompt([{fieldname:'days', label:__('Days Overdue (0 disables)'), fieldtype:'Int', reqd:1, default: frm.doc.custom_nkt_max_overdue_days || 0}],
                values => frappe.call({
                    method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.set_overdue_hold_threshold',
                    args: {customer: frm.doc.name, days: values.days},
                    freeze: true,
                    callback: () => frm.reload_doc()
                }), __('Overdue Hold Threshold'));
        }, __('NKT Account'));

        if (frm.doc.custom_nkt_manual_account_hold) {
            frm.add_custom_button(__('Clear Manual Hold'), () => {
                frappe.prompt([{fieldname:'reason', label:__('Reason'), fieldtype:'Small Text', reqd:1}],
                    values => frappe.call({
                        method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.clear_customer_account_hold',
                        args: {customer: frm.doc.name, reason: values.reason},
                        freeze: true,
                        callback: () => frm.reload_doc()
                    }), __('Clear Account Hold'));
            }, __('NKT Account'));
        } else {
            frm.add_custom_button(__('Place Manual Hold'), () => {
                frappe.prompt([{fieldname:'reason', label:__('Reason'), fieldtype:'Small Text', reqd:1}],
                    values => frappe.call({
                        method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.set_customer_account_hold',
                        args: {customer: frm.doc.name, reason: values.reason},
                        freeze: true,
                        callback: () => frm.reload_doc()
                    }), __('Place Account Hold'));
            }, __('NKT Account'));
        }
    }
});
'''


def _statement_delivery_client_script():
    return r'''
frappe.ui.form.on('NKT Customer Statement', {
    refresh(frm) {
        if (frm.is_new() || frm.doc.status !== 'Generated') return;
        frm.add_custom_button(__('Record Printed Copy'), () => {
            frappe.call({
                method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.record_statement_delivery',
                args: {statement: frm.doc.name, delivery_method: 'Printed'},
                freeze: true,
                callback: () => frm.reload_doc()
            });
        }, __('Delivery Audit'));
        frm.add_custom_button(__('Record Email Sent'), () => {
            frappe.prompt([
                {fieldname:'recipient', label:__('Recipient Email'), fieldtype:'Data', reqd:1},
                {fieldname:'notes', label:__('Notes'), fieldtype:'Small Text'}
            ], values => frappe.call({
                method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.record_statement_delivery',
                args: {statement: frm.doc.name, delivery_method: 'Emailed', recipient: values.recipient, notes: values.notes},
                freeze: true,
                callback: () => frm.reload_doc()
            }), __('Record Email Sent'));
        }, __('Delivery Audit'));
        frm.add_custom_button(__('Record Hand Delivery'), () => {
            frappe.prompt([
                {fieldname:'recipient', label:__('Received By'), fieldtype:'Data', reqd:1},
                {fieldname:'notes', label:__('Notes'), fieldtype:'Small Text'}
            ], values => frappe.call({
                method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.record_statement_delivery',
                args: {statement: frm.doc.name, delivery_method: 'Hand Delivered', recipient: values.recipient, notes: values.notes},
                freeze: true,
                callback: () => frm.reload_doc()
            }), __('Record Hand Delivery'));
        }, __('Delivery Audit'));
    }
});
'''


def _correction_client_script():
    return r'''
frappe.ui.form.on('NKT Account Payment Correction', {
    setup(frm) {
        frm.set_query('payment_verification', () => ({filters: {status: 'Matched', allocations_posted: 1}}));
        frm.set_query('specific_customer_order', () => ({filters: {customer: frm.doc.customer, company: frm.doc.company}}));
    },
    refresh(frm) {
        if (frm.is_new()) return;
        if (frm.doc.status !== 'Applied' && frm.doc.payment_verification) {
            frm.add_custom_button(__('Load Payment Source'), () => frappe.call({
                method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.load_payment_correction_source',
                args: {correction_name: frm.doc.name, payment_verification: frm.doc.payment_verification},
                freeze: true,
                callback: () => frm.reload_doc()
            }));
            frm.add_custom_button(__('Preview Correction'), () => frappe.call({
                method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.preview_payment_correction',
                args: {correction_name: frm.doc.name},
                freeze: true,
                callback: () => frm.reload_doc()
            }));
        }
        if (frm.doc.status === 'Previewed') {
            frm.add_custom_button(__('Apply Correction'), () => {
                frappe.confirm(__('This changes only the receivable application. The existing cashier receipt and cashier movement remain unchanged. Continue?'),
                    () => frappe.call({
                        method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.apply_payment_correction',
                        args: {correction_name: frm.doc.name},
                        freeze: true,
                        callback: () => frm.reload_doc()
                    }));
            }).addClass('btn-primary');
        }
    }
});
'''


def _install_item_warning_scripts():
    installed = []
    for parent in ("NKT Cashier Sale", ORDER):
        if not frappe.db.exists("DocType", parent):
            continue
        parent_meta = frappe.get_meta(parent)
        for table_field in parent_meta.fields:
            if table_field.fieldtype != "Table" or not table_field.options or not frappe.db.exists("DocType", table_field.options):
                continue
            child = table_field.options
            child_meta = frappe.get_meta(child)
            item_fields = [f.fieldname for f in child_meta.fields if f.fieldtype == "Link" and f.options == "Item"]
            if not item_fields:
                continue
            item_field = item_fields[0]
            script_name = "NKT Saleable Item Warning V1.8 - {0}".format(child)
            script = """
frappe.ui.form.on(%(child)s, {
    %(item_field)s(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (!row.%(item_field)s) return;
        frappe.call({
            method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.get_item_saleability',
            args: {item: row.%(item_field)s},
            callback: r => {
                if (r.message && !r.message.allowed) {
                    frappe.msgprint({
                        title: __('Item Not Allowed for Ordinary Sale'),
                        indicator: 'red',
                        message: r.message.message
                    });
                    frappe.model.set_value(cdt, cdn, '%(item_field)s', null);
                }
            }
        });
    }
});
""" % {"child": json.dumps(child), "item_field": item_field}
            _ensure_client_script(script_name, child, script)
            installed.append(script_name)
    return installed


def install_schema():
    required = [RECEIVABLE, VERIFICATION, ALLOCATION_ROW, CASHIER_COLLECTION, STATEMENT]
    missing = [doctype for doctype in required if not frappe.db.exists("DocType", doctype)]
    if missing:
        frappe.throw(_("Install the controlling V1.7.1 account workflow first. Missing: {0}").format(", ".join(missing)))

    _ensure_v18_doctypes()
    _ensure_customer_fields()
    _ensure_existing_doctype_fields()
    _ensure_client_script("NKT Customer Account Controls V1.8", "Customer", _customer_client_script())
    _ensure_client_script("NKT Statement Delivery Audit V1.8", STATEMENT, _statement_delivery_client_script())
    _ensure_client_script("NKT Payment Correction V1.8", CORRECTION, _correction_client_script())
    item_scripts = _install_item_warning_scripts()

    # Backfill only the new audit status fields. Existing operational status,
    # cashier records, payment receipts, allocations, and movements are untouched.
    frappe.db.sql(
        """
        UPDATE `tabNKT Encoder Account Allocation`
        SET custom_nkt_application_status='Active'
        WHERE status='Matched'
          AND IFNULL(allocations_posted, 0)=1
          AND IFNULL(custom_nkt_application_status, '')=''
        """
    )
    frappe.db.sql(
        """
        UPDATE `tabNKT Cashier Account Collection`
        SET custom_nkt_application_status=CASE WHEN status='Matched' THEN 'Active' ELSE 'Unmatched' END
        WHERE IFNULL(custom_nkt_application_status, '')=''
        """
    )

    for doctype in ["Customer", RECEIVABLE, VERIFICATION, ALLOCATION_ROW, CASHIER_COLLECTION, STATEMENT, AGING_ALERT, CONTROL_LOG, DELIVERY_LOG, CORRECTION, CORRECTION_ROW, CLEANUP_LOG]:
        if frappe.db.exists("DocType", doctype):
            frappe.clear_cache(doctype=doctype)
    frappe.cache.delete_key("bootinfo")
    return {"installed": True, "version": VERSION, "item_warning_scripts": item_scripts}


def _receivables(company=None, customer=None, as_of_date=None, include_zero=False):
    filters = ["IFNULL(credit_control_status, '')='Approved'", "IFNULL(status, '')!='Cancelled'"]
    values = []
    if company:
        filters.append("company=%s")
        values.append(company)
    if customer:
        filters.append("customer=%s")
        values.append(customer)
    if as_of_date:
        filters.append("posting_date<=%s")
        values.append(getdate(as_of_date))
    if not include_zero:
        filters.append("IFNULL(outstanding_amount, 0)>%s")
        values.append(TOLERANCE)
    return frappe.db.sql(
        """
        SELECT name, company, customer, customer_order, posting_date, due_date,
               original_amount, amount_paid, outstanding_amount, status, credit_control_status
        FROM `tabNKT Customer Receivable`
        WHERE {where}
        ORDER BY company, customer, due_date, posting_date, creation, name
        """.format(where=" AND ".join(filters)),
        tuple(values),
        as_dict=True,
    )


def _days_overdue(due_date, as_of_date):
    if not due_date:
        return 0
    return max(0, (getdate(as_of_date) - getdate(due_date)).days)


def _aging_bucket(days):
    if days <= 0:
        return "Current"
    if days <= 30:
        return "1–30"
    if days <= 60:
        return "31–60"
    if days <= 90:
        return "61–90"
    return "Over 90"


def _control_snapshot(customer, company, rows, as_of_date):
    total = sum(flt(row.outstanding_amount) for row in rows)
    overdue_rows = [row for row in rows if _days_overdue(row.due_date, as_of_date) > 0]
    overdue = sum(flt(row.outstanding_amount) for row in overdue_rows)
    oldest = max([_days_overdue(row.due_date, as_of_date) for row in overdue_rows] or [0])
    due_soon = any(row.due_date and getdate(as_of_date) <= getdate(row.due_date) <= add_days(getdate(as_of_date), 7) for row in rows)
    customer_values = frappe.db.get_value(
        "Customer",
        customer,
        ["custom_nkt_manual_account_hold", "custom_nkt_max_overdue_days"],
        as_dict=True,
    ) or frappe._dict()
    manual_hold = cint(customer_values.get("custom_nkt_manual_account_hold"))
    threshold = cint(customer_values.get("custom_nkt_max_overdue_days"))
    automatic_hold = bool(threshold > 0 and oldest > threshold and overdue > TOLERANCE)
    effective_hold = bool(manual_hold or automatic_hold)
    if effective_hold:
        severity = "On Hold"
    elif oldest > 90:
        severity = "Severely Overdue"
    elif overdue > TOLERANCE:
        severity = "Overdue"
    elif due_soon:
        severity = "Due Soon"
    else:
        severity = "Clear"
    return {
        "company": company,
        "customer": customer,
        "as_of_date": str(getdate(as_of_date)),
        "total_outstanding": flt(total),
        "overdue_balance": flt(overdue),
        "oldest_days_overdue": oldest,
        "manual_hold": manual_hold,
        "automatic_hold": cint(automatic_hold),
        "effective_hold": cint(effective_hold),
        "automatic_hold_threshold": threshold,
        "severity": severity,
    }


def _log_control(customer, company, action, reason, snapshot):
    doc = frappe.get_doc(
        {
            "doctype": CONTROL_LOG,
            "company": company,
            "customer": customer,
            "action": action,
            "reason": reason,
            "performed_by": frappe.session.user,
            "performed_on": now_datetime(),
            "snapshot": json.dumps(snapshot, default=str, indent=2),
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return doc.name


def _upsert_alert(snapshot):
    customer = snapshot["customer"]
    company = snapshot["company"]
    severity = snapshot["severity"]
    open_name = frappe.db.get_value(AGING_ALERT, {"customer": customer, "company": company, "status": ["in", ["Open", "Acknowledged"]]}, "name", order_by="creation desc")
    if severity == "Clear":
        if open_name:
            frappe.db.set_value(AGING_ALERT, open_name, {"status": "Resolved", "message": "Account is current as of {0}.".format(snapshot["as_of_date"])})
        return None
    message = "{severity}: outstanding {total:,.2f}; overdue {overdue:,.2f}; oldest {days} day(s).".format(
        severity=severity,
        total=flt(snapshot["total_outstanding"]),
        overdue=flt(snapshot["overdue_balance"]),
        days=cint(snapshot["oldest_days_overdue"]),
    )
    if open_name:
        doc = frappe.get_doc(AGING_ALERT, open_name)
    else:
        doc = frappe.new_doc(AGING_ALERT)
        doc.company = company
        doc.customer = customer
    doc.as_of_date = snapshot["as_of_date"]
    doc.total_outstanding = snapshot["total_outstanding"]
    doc.overdue_balance = snapshot["overdue_balance"]
    doc.oldest_days_overdue = snapshot["oldest_days_overdue"]
    doc.severity = severity
    if doc.status == "Resolved" or not doc.status:
        doc.status = "Open"
    doc.message = message
    doc.generated_by = frappe.session.user
    doc.generated_on = now_datetime()
    doc.flags.ignore_permissions = True
    if doc.is_new():
        doc.insert(ignore_permissions=True)
    else:
        doc.save(ignore_permissions=True)
    return doc.name


def _update_receivable_aging(rows, as_of_date):
    now = now_datetime()
    for row in rows:
        days = _days_overdue(row.due_date, as_of_date) if flt(row.outstanding_amount) > TOLERANCE else 0
        frappe.db.set_value(
            RECEIVABLE,
            row.name,
            {
                "custom_nkt_days_overdue": days,
                "custom_nkt_aging_bucket": _aging_bucket(days),
                "custom_nkt_last_aging_refresh": now,
            },
            update_modified=False,
        )


def _update_customer_control(snapshot, *, log_automatic_changes=True):
    customer = snapshot["customer"]
    old = frappe.db.get_value(
        "Customer",
        customer,
        ["custom_nkt_automatic_account_hold", "custom_nkt_automatic_hold_reason"],
        as_dict=True,
    ) or frappe._dict()
    automatic_reason = ""
    if snapshot["automatic_hold"]:
        automatic_reason = "Oldest overdue balance is {0} days, above the configured {1}-day threshold.".format(
            snapshot["oldest_days_overdue"], snapshot["automatic_hold_threshold"]
        )
    values = {
        "custom_nkt_automatic_account_hold": snapshot["automatic_hold"],
        "custom_nkt_automatic_hold_reason": automatic_reason,
        "custom_nkt_effective_account_hold": snapshot["effective_hold"],
        "custom_nkt_overdue_balance": snapshot["overdue_balance"],
        "custom_nkt_oldest_overdue_days": snapshot["oldest_days_overdue"],
        "custom_nkt_aging_alert_status": snapshot["severity"],
        "custom_nkt_last_aging_refresh": now_datetime(),
    }
    frappe.db.set_value("Customer", customer, values, update_modified=False)
    old_auto = cint(old.get("custom_nkt_automatic_account_hold"))
    new_auto = cint(snapshot["automatic_hold"])
    if log_automatic_changes and old_auto != new_auto:
        _log_control(
            customer,
            snapshot["company"],
            "Automatic Hold Applied" if new_auto else "Automatic Hold Cleared",
            automatic_reason or "Overdue threshold is no longer exceeded.",
            snapshot,
        )


@frappe.whitelist()
def refresh_customer_account_control(customer, company=None, as_of_date=None):
    _require_roles(AUTHORIZED_CONTROL_ROLES | {"NKT Encoder"}, "refresh customer account aging")
    as_of_date = getdate(as_of_date or today())
    if not company:
        company = frappe.db.get_value(RECEIVABLE, {"customer": customer}, "company", order_by="creation desc")
    if not company:
        frappe.throw(_("No NKT customer receivable company was found for {0}.").format(customer))
    all_rows = _receivables(company=company, customer=customer, as_of_date=as_of_date, include_zero=True)
    active_rows = [row for row in all_rows if flt(row.outstanding_amount) > TOLERANCE]
    snapshot = _control_snapshot(customer, company, active_rows, as_of_date)
    _update_receivable_aging(all_rows, as_of_date)
    _update_customer_control(snapshot)
    alert = _upsert_alert(snapshot)
    return {"snapshot": snapshot, "alert": alert}


@frappe.whitelist()
def refresh_aging_alerts(company=None, as_of_date=None):
    _require_roles(AUTHORIZED_CONTROL_ROLES | {"NKT Encoder"}, "refresh aging alerts")
    as_of_date = getdate(as_of_date or today())
    rows = _receivables(company=company, as_of_date=as_of_date, include_zero=True)
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row.company, row.customer)].append(row)
    results = []
    for (row_company, customer), customer_rows in grouped.items():
        active_rows = [row for row in customer_rows if flt(row.outstanding_amount) > TOLERANCE]
        snapshot = _control_snapshot(customer, row_company, active_rows, as_of_date)
        _update_receivable_aging(customer_rows, as_of_date)
        _update_customer_control(snapshot)
        alert = _upsert_alert(snapshot)
        results.append({"customer": customer, "company": row_company, "severity": snapshot["severity"], "alert": alert})
    return {"as_of_date": str(as_of_date), "count": len(results), "results": results}


def scheduled_refresh_aging_alerts():
    frappe.set_user("Administrator")
    return refresh_aging_alerts(as_of_date=today())


@frappe.whitelist()
def set_overdue_hold_threshold(customer, days, company=None):
    _require_roles(AUTHORIZED_CONTROL_ROLES, "set an overdue hold threshold")
    days = cint(days)
    if days < 0:
        frappe.throw(_("Days overdue cannot be negative."))
    frappe.db.set_value("Customer", customer, "custom_nkt_max_overdue_days", days)
    if not company:
        company = frappe.db.get_value(RECEIVABLE, {"customer": customer}, "company", order_by="creation desc")
    result = refresh_customer_account_control(customer, company, today()) if company else {"snapshot": None}
    return {"customer": customer, "days": days, **result}


@frappe.whitelist()
def set_customer_account_hold(customer, reason, company=None):
    _require_roles(AUTHORIZED_CONTROL_ROLES, "place a customer account hold")
    if not reason or not str(reason).strip():
        frappe.throw(_("A hold reason is required."))
    if not company:
        company = frappe.db.get_value(RECEIVABLE, {"customer": customer}, "company", order_by="creation desc")
    values = {
        "custom_nkt_manual_account_hold": 1,
        "custom_nkt_manual_hold_reason": str(reason).strip(),
        "custom_nkt_manual_hold_by": frappe.session.user,
        "custom_nkt_manual_hold_on": now_datetime(),
        "custom_nkt_effective_account_hold": 1,
        "custom_nkt_aging_alert_status": "On Hold",
    }
    frappe.db.set_value("Customer", customer, values)
    rows = _receivables(company=company, customer=customer, as_of_date=today()) if company else []
    snapshot = _control_snapshot(customer, company, rows, today()) if company else {"customer": customer, "company": company, "effective_hold": 1}
    log = _log_control(customer, company, "Manual Hold Placed", str(reason).strip(), snapshot)
    return {"customer": customer, "held": True, "log": log}


@frappe.whitelist()
def clear_customer_account_hold(customer, reason, company=None):
    _require_roles(AUTHORIZED_CONTROL_ROLES, "clear a customer account hold")
    if not reason or not str(reason).strip():
        frappe.throw(_("A clearing reason is required."))
    if not company:
        company = frappe.db.get_value(RECEIVABLE, {"customer": customer}, "company", order_by="creation desc")
    automatic_hold = cint(frappe.db.get_value("Customer", customer, "custom_nkt_automatic_account_hold"))
    values = {
        "custom_nkt_manual_account_hold": 0,
        "custom_nkt_manual_hold_reason": "",
        "custom_nkt_manual_hold_by": None,
        "custom_nkt_manual_hold_on": None,
        "custom_nkt_effective_account_hold": automatic_hold,
        "custom_nkt_aging_alert_status": "On Hold" if automatic_hold else "Clear",
    }
    frappe.db.set_value("Customer", customer, values)
    rows = _receivables(company=company, customer=customer, as_of_date=today()) if company else []
    snapshot = _control_snapshot(customer, company, rows, today()) if company else {"customer": customer, "company": company, "effective_hold": automatic_hold}
    log = _log_control(customer, company, "Manual Hold Cleared", str(reason).strip(), snapshot)
    if company:
        refresh_customer_account_control(customer, company, today())
    return {"customer": customer, "held": bool(automatic_hold), "log": log}


def validate_new_receivable(doc, method=None):
    """Backend gate for customer-level account holds. Registered in hooks.py."""
    if cint(getattr(frappe.flags, "nkt_v18_bypass_account_hold", 0)):
        return
    customer = getattr(doc, "customer", None)
    company = getattr(doc, "company", None)
    if not customer:
        return
    values = frappe.db.get_value(
        "Customer",
        customer,
        ["custom_nkt_manual_account_hold", "custom_nkt_automatic_account_hold", "custom_nkt_manual_hold_reason", "custom_nkt_automatic_hold_reason"],
        as_dict=True,
    ) or frappe._dict()
    manual = cint(values.get("custom_nkt_manual_account_hold"))
    automatic = cint(values.get("custom_nkt_automatic_account_hold"))
    if manual or automatic:
        reason = values.get("custom_nkt_manual_hold_reason") if manual else values.get("custom_nkt_automatic_hold_reason")
        frappe.throw(
            _("Account sale blocked for {0}. Customer account is on hold. Reason: {1}").format(customer, reason or "Account control hold"),
            title=_("Customer Account Hold"),
        )
    if company:
        # Recalculate current overdue state before allowing a new receivable.
        # Do not write here: a thrown validation error rolls the transaction back.
        rows = _receivables(company=company, customer=customer, as_of_date=today())
        snapshot = _control_snapshot(customer, company, rows, today())
        if snapshot["effective_hold"]:
            frappe.throw(
                _("Account sale blocked for {0}. The overdue policy places this customer on hold. Run Refresh Aging Control to persist the alert details.").format(customer),
                title=_("Customer Account Hold"),
            )


@frappe.whitelist()
def get_item_saleability(item):
    if not item or not frappe.db.exists("Item", item):
        return {"allowed": False, "message": _("The selected item does not exist.")}
    fieldname = "nkt_stock_form" if _field_exists("Item", "nkt_stock_form") else "custom_nkt_stock_form"
    stock_form = frappe.db.get_value("Item", item, fieldname) if _field_exists("Item", fieldname) else None
    allowed = stock_form == "Saleable Sack"
    return {
        "allowed": allowed,
        "stock_form": stock_form,
        "message": _("Item {0} is classified as {1}. Ordinary cashier and encoder sales accept only Saleable Sack items.").format(item, stock_form or "Unclassified"),
    }


@frappe.whitelist()
def record_statement_delivery(statement, delivery_method, recipient=None, notes=None):
    _require_roles(AUTHORIZED_CONTROL_ROLES | {"NKT Encoder"}, "record statement delivery")
    allowed = {"Printed", "Emailed", "Hand Delivered"}
    if delivery_method not in allowed:
        frappe.throw(_("Invalid delivery method."))
    if delivery_method in {"Emailed", "Hand Delivered"} and not (recipient or "").strip():
        frappe.throw(_("Recipient is required for {0}.").format(delivery_method))
    doc = frappe.get_doc(STATEMENT, statement)
    doc.check_permission("read")
    now = now_datetime()
    log = frappe.get_doc(
        {
            "doctype": DELIVERY_LOG,
            "statement": doc.name,
            "company": doc.company,
            "customer": doc.customer,
            "delivery_method": delivery_method,
            "recipient": (recipient or "").strip(),
            "delivered_by": frappe.session.user,
            "delivered_on": now,
            "notes": notes,
        }
    )
    log.insert()
    methods = set(frappe.get_all(DELIVERY_LOG, filters={"statement": doc.name}, pluck="delivery_method"))
    status = next(iter(methods)) if len(methods) == 1 else "Multiple Methods"
    values = {"custom_nkt_delivery_status": status}
    if delivery_method == "Printed":
        values.update({"custom_nkt_print_count": cint(doc.get("custom_nkt_print_count")) + 1, "custom_nkt_last_printed_on": now, "custom_nkt_last_printed_by": frappe.session.user})
    elif delivery_method == "Emailed":
        values.update({"custom_nkt_email_count": cint(doc.get("custom_nkt_email_count")) + 1, "custom_nkt_last_emailed_on": now, "custom_nkt_last_emailed_by": frappe.session.user, "custom_nkt_last_email_recipient": (recipient or "").strip()})
    else:
        values.update({"custom_nkt_last_delivered_on": now, "custom_nkt_last_delivered_by": frappe.session.user, "custom_nkt_last_delivery_recipient": (recipient or "").strip()})
    frappe.db.set_value(STATEMENT, doc.name, values)
    return {"statement": doc.name, "delivery_log": log.name, "delivery_status": status}


def _verification_allocations(payment_verification):
    reversal_filter = ""
    if _field_exists(ALLOCATION_ROW, "custom_nkt_is_reversed"):
        reversal_filter = " AND IFNULL(custom_nkt_is_reversed, 0)=0"
    return frappe.db.sql(
        """
        SELECT name, parent, idx, receivable, customer_order, allocated_amount
        FROM `tabNKT Account Allocation Row`
        WHERE parent=%s AND parenttype=%s {reversal_filter}
        ORDER BY idx, name
        """.format(reversal_filter=reversal_filter),
        (payment_verification, VERIFICATION),
        as_dict=True,
    )


def _open_receivables(company, customer, exclude_original_allocations=None):
    rows = _receivables(company=company, customer=customer, as_of_date=today(), include_zero=True)
    credits = defaultdict(float)
    for row in exclude_original_allocations or []:
        credits[row.receivable] += flt(row.allocated_amount)
    for row in rows:
        row.effective_outstanding = flt(row.outstanding_amount) + flt(credits.get(row.name, 0))
    return [row for row in rows if row.effective_outstanding > TOLERANCE]


def _plan_reapplication(company, customer, payment_amount, specific_order=None, original_allocations=None):
    remaining = flt(payment_amount)
    open_rows = _open_receivables(company, customer, original_allocations)
    planned = []
    if specific_order:
        direct = [row for row in open_rows if row.customer_order == specific_order]
        if not direct:
            frappe.throw(_("The selected order has no eligible outstanding receivable for this customer."))
        available = sum(flt(row.effective_outstanding) for row in direct)
        if available + TOLERANCE < remaining:
            frappe.throw(_("The selected order has only {0:,.2f} outstanding, below the payment amount {1:,.2f}.").format(available, remaining))
        for row in direct:
            amount = min(remaining, flt(row.effective_outstanding))
            if amount > TOLERANCE:
                planned.append((row, amount, "Specific customer-order reference"))
                remaining -= amount
            if remaining <= TOLERANCE:
                break
    else:
        exact = [row for row in open_rows if abs(flt(row.effective_outstanding) - remaining) <= TOLERANCE]
        if len(exact) == 1:
            row = exact[0]
            planned.append((row, remaining, "Unique exact outstanding-value match"))
            remaining = 0
        else:
            open_rows.sort(key=lambda row: (getdate(row.due_date or row.posting_date), getdate(row.posting_date), row.name))
            for row in open_rows:
                amount = min(remaining, flt(row.effective_outstanding))
                if amount > TOLERANCE:
                    planned.append((row, amount, "Oldest outstanding account first"))
                    remaining -= amount
                if remaining <= TOLERANCE:
                    break
    if remaining > TOLERANCE:
        frappe.throw(_("Payment cannot be fully reapplied. Unallocated balance: {0:,.2f}.").format(remaining))
    return planned


@frappe.whitelist()
def load_payment_correction_source(correction_name, payment_verification=None):
    _require_roles(AUTHORIZED_CONTROL_ROLES, "prepare an account payment correction")
    correction = frappe.get_doc(CORRECTION, correction_name)
    if correction.status == "Applied":
        frappe.throw(_("Applied corrections cannot be changed."))
    verification_name = payment_verification or correction.payment_verification
    if not verification_name:
        frappe.throw(_("Select a payment verification."))
    verification = frappe.get_doc(VERIFICATION, verification_name)
    if verification.status != "Matched" or not cint(verification.allocations_posted):
        frappe.throw(_("Only matched, posted payment verifications can be corrected."))
    if verification.get("custom_nkt_correction_reference"):
        frappe.throw(_("This payment verification is already controlled by correction {0}.").format(verification.custom_nkt_correction_reference))
    original = _verification_allocations(verification.name)
    if not original:
        frappe.throw(_("No active allocation rows were found."))
    correction.payment_verification = verification.name
    correction.company = verification.company
    correction.customer = verification.customer
    correction.cashier_collection = verification.matched_cashier_collection
    correction.linked_payment_receipt = verification.linked_payment_receipt
    correction.original_allocation_date = verification.allocation_date
    correction.payment_amount = sum(flt(row.allocated_amount) for row in original)
    correction.status = "Draft"
    correction.set("allocations", [])
    for row in original:
        receivable = frappe.db.get_value(RECEIVABLE, row.receivable, ["outstanding_amount"], as_dict=True) or frappe._dict()
        correction.append(
            "allocations",
            {
                "allocation_kind": "Original",
                "source_allocation_row": row.name,
                "receivable": row.receivable,
                "customer_order": row.customer_order,
                "allocated_amount": row.allocated_amount,
                "outstanding_before": flt(receivable.get("outstanding_amount")),
                "outstanding_after": flt(receivable.get("outstanding_amount")) + flt(row.allocated_amount),
                "application_rule": "Original posted allocation",
                "is_applied": 1,
            },
        )
    correction.save()
    return {"correction": correction.name, "payment_amount": correction.payment_amount, "original_allocations": len(original)}


@frappe.whitelist()
def preview_payment_correction(correction_name):
    _require_roles(AUTHORIZED_CONTROL_ROLES, "preview an account payment correction")
    correction = frappe.get_doc(CORRECTION, correction_name)
    if not correction.payment_verification:
        frappe.throw(_("Select a payment verification first."))
    load_payment_correction_source(correction.name, correction.payment_verification)
    correction.reload()
    original_rows = [row for row in correction.allocations if row.allocation_kind == "Original"]
    if correction.correction_mode == "Reverse and Reapply":
        originals = [frappe._dict({"receivable": row.receivable, "allocated_amount": row.allocated_amount}) for row in original_rows]
        planned = _plan_reapplication(correction.company, correction.customer, correction.payment_amount, correction.specific_customer_order, originals)
        for receivable, amount, rule in planned:
            before = flt(receivable.effective_outstanding)
            correction.append(
                "allocations",
                {
                    "allocation_kind": "New",
                    "receivable": receivable.name,
                    "customer_order": receivable.customer_order,
                    "allocated_amount": amount,
                    "outstanding_before": before,
                    "outstanding_after": before - amount,
                    "application_rule": rule,
                    "is_applied": 0,
                },
            )
    correction.status = "Previewed"
    correction.previewed_by = frappe.session.user
    correction.previewed_on = now_datetime()
    correction.save()
    return {"correction": correction.name, "status": correction.status, "rows": len(correction.allocations)}


def _receivable_status_for(new_paid, new_outstanding):
    options = []
    field = frappe.get_meta(RECEIVABLE).get_field("status")
    if field and field.options:
        options = [value.strip() for value in str(field.options).split("\n") if value.strip()]
    if new_outstanding <= TOLERANCE:
        return "Paid" if "Paid" in options or not options else options[-1]
    if new_paid > TOLERANCE:
        if "Partially Paid" in options:
            return "Partially Paid"
        if "Open" in options:
            return "Open"
    if "Open" in options:
        return "Open"
    if "Unpaid" in options:
        return "Unpaid"
    return options[0] if options else "Unpaid"


def _set_receivable_amounts(receivable_name, paid_delta):
    row = frappe.db.get_value(RECEIVABLE, receivable_name, ["original_amount", "amount_paid", "outstanding_amount", "customer_order", "customer"], as_dict=True)
    if not row:
        frappe.throw(_("Receivable not found: {0}").format(receivable_name))
    new_paid = flt(row.amount_paid) + flt(paid_delta)
    new_outstanding = flt(row.outstanding_amount) - flt(paid_delta)
    if new_paid < -TOLERANCE or new_outstanding < -TOLERANCE:
        frappe.throw(_("Correction would create an invalid balance on {0}.").format(receivable_name))
    new_paid = max(0, new_paid)
    new_outstanding = max(0, new_outstanding)
    status = _receivable_status_for(new_paid, new_outstanding)
    frappe.db.set_value(RECEIVABLE, receivable_name, {"amount_paid": new_paid, "outstanding_amount": new_outstanding, "status": status})
    return row.customer_order, row.customer


def _sync_order_account_totals(customer_order):
    if not customer_order or not frappe.db.exists(ORDER, customer_order):
        return
    rows = frappe.db.sql(
        """
        SELECT original_amount, amount_paid, outstanding_amount
        FROM `tabNKT Customer Receivable`
        WHERE customer_order=%s
          AND IFNULL(credit_control_status, '')='Approved'
          AND IFNULL(status, '')!='Cancelled'
        """,
        (customer_order,),
        as_dict=True,
    )
    if not rows:
        return
    original = sum(flt(row.original_amount) for row in rows)
    paid = sum(flt(row.amount_paid) for row in rows)
    due = sum(flt(row.outstanding_amount) for row in rows)
    meta = frappe.get_meta(ORDER)
    values = {}
    if meta.has_field("amount_due"):
        values["amount_due"] = due
    total_field = next((name for name in ("grand_total", "total_amount", "net_total") if meta.has_field(name)), None)
    order_total = flt(frappe.db.get_value(ORDER, customer_order, total_field)) if total_field else original
    account_only = abs(order_total - original) <= TOLERANCE
    if meta.has_field("amount_paid") and account_only:
        values["amount_paid"] = paid
    if meta.has_field("payment_status"):
        values["payment_status"] = "Paid" if due <= TOLERANCE else ("Partially Paid" if paid > TOLERANCE else "Charged to Account")
    if values:
        frappe.db.set_value(ORDER, customer_order, values)


def _sync_customer_balance(customer):
    outstanding = frappe.db.sql(
        """
        SELECT COALESCE(SUM(outstanding_amount), 0)
        FROM `tabNKT Customer Receivable`
        WHERE customer=%s
          AND IFNULL(credit_control_status, '')='Approved'
          AND IFNULL(status, '')!='Cancelled'
        """,
        (customer,),
    )[0][0]
    meta = frappe.get_meta("Customer")
    values = {}
    if meta.has_field("custom_nkt_current_account_balance"):
        values["custom_nkt_current_account_balance"] = flt(outstanding)
    if meta.has_field("custom_nkt_available_credit") and meta.has_field("custom_nkt_credit_limit"):
        credit_limit = flt(frappe.db.get_value("Customer", customer, "custom_nkt_credit_limit"))
        values["custom_nkt_available_credit"] = max(0, credit_limit - flt(outstanding))
    if values:
        frappe.db.set_value("Customer", customer, values)


@frappe.whitelist()
def apply_payment_correction(correction_name):
    _require_roles(AUTHORIZED_CONTROL_ROLES, "apply an account payment correction")
    correction = frappe.get_doc(CORRECTION, correction_name)
    if correction.status != "Previewed":
        frappe.throw(_("Preview the correction immediately before applying it."))
    verification = frappe.get_doc(VERIFICATION, correction.payment_verification)
    if verification.get("custom_nkt_correction_reference"):
        frappe.throw(_("This verification is already corrected by {0}.").format(verification.custom_nkt_correction_reference))
    active_originals = _verification_allocations(verification.name)
    preview_originals = [row for row in correction.allocations if row.allocation_kind == "Original"]
    active_map = {row.name: flt(row.allocated_amount) for row in active_originals}
    preview_map = {row.source_allocation_row: flt(row.allocated_amount) for row in preview_originals}
    if active_map != preview_map:
        frappe.throw(_("Original allocations changed after preview. Refresh and preview again."))
    touched_orders = set()
    touched_customer = correction.customer
    # Reverse only the receivable application. Cashier receipt and movement remain untouched.
    for row in active_originals:
        order_name, customer = _set_receivable_amounts(row.receivable, -flt(row.allocated_amount))
        if order_name:
            touched_orders.add(order_name)
        touched_customer = customer or touched_customer
        frappe.db.set_value(ALLOCATION_ROW, row.name, {"custom_nkt_is_reversed": 1, "custom_nkt_reversed_by_correction": correction.name}, update_modified=False)
    # Apply replacement rows, if any.
    for row in correction.allocations:
        if row.allocation_kind != "New":
            continue
        order_name, customer = _set_receivable_amounts(row.receivable, flt(row.allocated_amount))
        if order_name:
            touched_orders.add(order_name)
        touched_customer = customer or touched_customer
        frappe.db.set_value(CORRECTION_ROW, row.name, "is_applied", 1, update_modified=False)
    final_status = "Corrected" if any(row.allocation_kind == "New" for row in correction.allocations) else "Reversed"
    frappe.db.set_value(VERIFICATION, verification.name, {"custom_nkt_application_status": final_status, "custom_nkt_correction_reference": correction.name})
    if correction.cashier_collection and frappe.db.exists(CASHIER_COLLECTION, correction.cashier_collection):
        frappe.db.set_value(CASHIER_COLLECTION, correction.cashier_collection, {"custom_nkt_application_status": final_status, "custom_nkt_correction_reference": correction.name})
    for order_name in touched_orders:
        _sync_order_account_totals(order_name)
    if touched_customer:
        _sync_customer_balance(touched_customer)
    frappe.db.set_value(CORRECTION, correction.name, {"status": "Applied", "applied_by": frappe.session.user, "applied_on": now_datetime()})
    refresh_customer_account_control(correction.customer, correction.company, today())
    return {
        "correction": correction.name,
        "status": "Applied",
        "application_status": final_status,
        "existing_payment_receipt": correction.linked_payment_receipt,
        "cashier_collection": correction.cashier_collection,
        "created_new_payment_receipt": False,
        "created_new_cashier_movement": False,
    }


@frappe.whitelist()
def preview_test_data_cleanup(order_names=None):
    _require_roles(ADMIN_ROLES, "preview controlled test-data cleanup")
    if isinstance(order_names, str):
        try:
            order_names = json.loads(order_names)
        except Exception:
            order_names = [name.strip() for name in order_names.split(",") if name.strip()]
    order_names = order_names or ["NKT-ORD-00018", "NKT-ORD-00019", "NKT-ORD-00020", "NKT-ORD-00021"]
    results = []
    for name in order_names:
        if not frappe.db.exists(ORDER, name):
            results.append({"order": name, "eligible": False, "reason": "Order not found"})
            continue
        doc = frappe.get_doc(ORDER, name)
        receivable_count = frappe.db.count(RECEIVABLE, {"customer_order": name})
        snapshot = {"status": doc.get("status"), "payment_status": doc.get("payment_status"), "match_status": doc.get("match_status"), "credit_control_status": doc.get("credit_control_status"), "receivable_count": receivable_count, "docstatus": doc.docstatus}
        expected_text = " ".join(str(snapshot.get(key) or "") for key in ("status", "payment_status", "match_status", "credit_control_status"))
        eligible = receivable_count == 0 and doc.docstatus < 2 and any(token in expected_text for token in ("Unpaid", "Awaiting Payment", "Ambiguous", "Not Required"))
        results.append({"order": name, "eligible": eligible, "snapshot": snapshot, "reason": "Matches incomplete test-artifact safeguards" if eligible else "Preconditions do not match the V1.7.1 incomplete test artifact description"})
    return results


@frappe.whitelist()
def archive_incomplete_test_orders(order_names=None, reason=None):
    _require_roles(ADMIN_ROLES, "archive incomplete test records")
    if not reason or not str(reason).strip():
        frappe.throw(_("An archive reason is required."))
    preview = preview_test_data_cleanup(order_names)
    ineligible = [row for row in preview if not row["eligible"]]
    if ineligible:
        frappe.throw(_("Cleanup stopped because one or more records failed safeguards: {0}").format(json.dumps(ineligible, default=str)))
    logs = []
    for row in preview:
        values = {
            "custom_nkt_test_artifact": 1,
            "custom_nkt_archived_from_operations": 1,
            "custom_nkt_test_archive_reason": str(reason).strip(),
            "custom_nkt_test_archived_by": frappe.session.user,
            "custom_nkt_test_archived_on": now_datetime(),
        }
        frappe.db.set_value(ORDER, row["order"], values)
        log = frappe.get_doc(
            {
                "doctype": CLEANUP_LOG,
                "customer_order": row["order"],
                "action": "Archived from Operations",
                "reason": str(reason).strip(),
                "precondition_snapshot": json.dumps(row["snapshot"], default=str, indent=2),
                "performed_by": frappe.session.user,
                "performed_on": now_datetime(),
            }
        )
        log.insert()
        logs.append(log.name)
    return {"archived": [row["order"] for row in preview], "logs": logs, "deleted": False}


@frappe.whitelist()
def verify_live_v1_7_1_state(strict=0):
    strict = cint(strict)
    customer = "TEST - ACCOUNT CUSTOMER"
    company = "NKT (Dev)"
    expected = {"customer_balance": 12000.0, "auto_approval_limit": 0.0, "period_charges": 40300.0, "period_payments": 28300.0, "closing_balance": 12000.0}
    report = {"version_expected": "1.7.1", "customer": customer, "company": company, "expected": expected, "checks": {}, "errors": []}
    if not frappe.db.exists("Customer", customer):
        report["errors"].append("Test customer not found")
    else:
        values = frappe.db.get_value("Customer", customer, ["custom_nkt_current_account_balance", "custom_nkt_available_credit", "custom_nkt_auto_approval_limit"], as_dict=True) or frappe._dict()
        report["checks"]["customer"] = values
        if abs(flt(values.get("custom_nkt_current_account_balance")) - expected["customer_balance"]) > TOLERANCE:
            report["errors"].append("Customer balance differs from 12,000")
        if abs(flt(values.get("custom_nkt_auto_approval_limit")) - expected["auto_approval_limit"]) > TOLERANCE:
            report["errors"].append("Automatic Approval Limit per Order is not restored to 0")
    receivables = frappe.get_all(RECEIVABLE, filters={"customer": customer}, fields=["name", "customer_order", "original_amount", "amount_paid", "outstanding_amount", "status", "credit_control_status"], order_by="creation")
    report["checks"]["receivables"] = receivables
    valid_names = {"NKT-REC-00002", "NKT-REC-00003", "NKT-REC-00004", "NKT-REC-00005", "NKT-REC-00006", "NKT-REC-00007", "NKT-REC-00008"}
    missing_valid = sorted(valid_names - {row.name for row in receivables})
    if missing_valid:
        report["errors"].append("Missing valid V1.7.1 receivables: {0}".format(", ".join(missing_valid)))
    for order_name in ("NKT-ORD-00018", "NKT-ORD-00019", "NKT-ORD-00020", "NKT-ORD-00021"):
        if frappe.db.exists(RECEIVABLE, {"customer_order": order_name}):
            report["errors"].append("Incomplete test order {0} unexpectedly has a receivable".format(order_name))
    try:
        from nkt_operations.nkt_store_operations.features.payments_accounts.statement import get_statement_data
        statement = get_statement_data(company, customer, "2026-08-01", "2026-08-06")
        report["checks"]["statement"] = {key: statement.get(key) for key in ("period_charges", "period_payments", "closing_balance", "aging_current", "aging_1_30", "aging_31_60", "aging_61_90", "aging_over_90")}
        for key in ("period_charges", "period_payments", "closing_balance"):
            if abs(flt(statement.get(key)) - expected[key]) > TOLERANCE:
                report["errors"].append("SOA {0} differs from expected {1}".format(key, expected[key]))
    except Exception as exc:
        report["errors"].append("SOA verification failed: {0}".format(exc))
    report["passed"] = not report["errors"]
    if strict and report["errors"]:
        frappe.throw(_("V1.7.1 live-state verification failed. Stop before V1.8. Details: {0}").format(json.dumps(report, default=str, indent=2)))
    return report


def _permission_matrix():
    doctypes = [AGING_ALERT, CONTROL_LOG, DELIVERY_LOG, CORRECTION, CLEANUP_LOG, STATEMENT]
    matrix = {}
    for doctype in doctypes:
        if not frappe.db.exists("DocType", doctype):
            continue
        rows = []
        for permission in frappe.get_meta(doctype).permissions:
            rows.append(
                {
                    "role": permission.role,
                    "read": cint(permission.read),
                    "write": cint(permission.write),
                    "create": cint(permission.create),
                    "delete": cint(permission.delete),
                    "print": cint(permission.print),
                    "email": cint(permission.email),
                }
            )
        matrix[doctype] = rows
    return matrix


def _role_access(matrix, doctype, role):
    rows = [row for row in matrix.get(doctype, []) if row["role"] == role]
    if not rows:
        return {"read": 0, "write": 0, "create": 0, "delete": 0, "print": 0, "email": 0}
    result = {key: 0 for key in ("read", "write", "create", "delete", "print", "email")}
    for row in rows:
        for key in result:
            result[key] = max(result[key], cint(row.get(key)))
    return result


@frappe.whitelist()
def verify_role_permissions():
    matrix = _permission_matrix()
    errors = []
    cashier_correction = _role_access(matrix, CORRECTION, "NKT Cashier")
    encoder_correction = _role_access(matrix, CORRECTION, "NKT Encoder")
    credit_correction = _role_access(matrix, CORRECTION, "NKT Credit Controller")
    encoder_alert = _role_access(matrix, AGING_ALERT, "NKT Encoder")
    cashier_alert = _role_access(matrix, AGING_ALERT, "NKT Cashier")
    encoder_delivery = _role_access(matrix, DELIVERY_LOG, "NKT Encoder")
    if any(cashier_correction[key] for key in ("write", "create", "delete")):
        errors.append("NKT Cashier must not create/write/delete payment corrections")
    if any(encoder_correction[key] for key in ("write", "create", "delete")):
        errors.append("NKT Encoder must not create/write/delete payment corrections")
    if not (credit_correction["read"] and credit_correction["write"] and credit_correction["create"]):
        errors.append("NKT Credit Controller lacks payment-correction authority")
    if not encoder_alert["read"]:
        errors.append("NKT Encoder must be able to read aging alerts")
    if cashier_alert["create"] or cashier_alert["write"]:
        errors.append("NKT Cashier must not maintain aging alerts")
    if not (encoder_delivery["read"] and encoder_delivery["create"] and encoder_delivery["write"]):
        errors.append("NKT Encoder must be able to record statement delivery")
    return {"matrix": matrix, "errors": errors, "passed": not errors}


@frappe.whitelist()
def verify_v1_8():
    required_doctypes = [AGING_ALERT, CONTROL_LOG, DELIVERY_LOG, CORRECTION, CORRECTION_ROW, CLEANUP_LOG]
    missing_doctypes = [name for name in required_doctypes if not frappe.db.exists("DocType", name)]
    required_fields = {
        "Customer": ["custom_nkt_manual_account_hold", "custom_nkt_automatic_account_hold", "custom_nkt_effective_account_hold", "custom_nkt_overdue_balance"],
        RECEIVABLE: ["custom_nkt_days_overdue", "custom_nkt_aging_bucket"],
        VERIFICATION: ["custom_nkt_application_status", "custom_nkt_correction_reference"],
        ALLOCATION_ROW: ["custom_nkt_is_reversed", "custom_nkt_reversed_by_correction"],
        STATEMENT: ["custom_nkt_delivery_status", "custom_nkt_last_printed_on", "custom_nkt_last_emailed_on"],
    }
    missing_fields = []
    for doctype, fields in required_fields.items():
        for fieldname in fields:
            if not _field_exists(doctype, fieldname):
                missing_fields.append("{0}.{1}".format(doctype, fieldname))
    scripts = frappe.get_all("Client Script", filters={"name": ["in", ["NKT Customer Account Controls V1.8", "NKT Statement Delivery Audit V1.8", "NKT Payment Correction V1.8"]]}, pluck="name")
    permission_report = verify_role_permissions()
    report = {"version": VERSION, "missing_doctypes": missing_doctypes, "missing_fields": missing_fields, "client_scripts": scripts, "permission_report": permission_report, "passed": not missing_doctypes and not missing_fields and len(scripts) == 3 and permission_report["passed"], "gl_posting_enabled": False, "accounting_model": "Operational customer subledger"}
    if not report["passed"]:
        frappe.throw(_("V1.8 verification failed: {0}").format(json.dumps(report, indent=2)))
    return report
