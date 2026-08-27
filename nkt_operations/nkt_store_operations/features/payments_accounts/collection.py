from __future__ import annotations

import hashlib
import json
from collections import defaultdict

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import flt, getdate, now_datetime, today
from frappe.utils.password import check_password

from nkt_operations.nkt_store_operations.features.payments_accounts.card_surcharge import (
    apply_payment_row_card_fields,
    ensure_card_posting_allowed,
    normalize_payment_method,
    row_card_surcharge,
    row_collected_amount,
)


TOLERANCE = 0.005
CASHIER_COLLECTION_DOCTYPE = "NKT Cashier Account Collection"
ENCODER_ALLOCATION_DOCTYPE = "NKT Encoder Account Allocation"
PAYMENT_ROW_DOCTYPE = "NKT Account Collection Payment"
ALLOCATION_ROW_DOCTYPE = "NKT Account Allocation Row"
RECEIVABLE_DOCTYPE = "NKT Customer Receivable"
CASHIER_SCRIPT_NAME = "NKT Cashier Account Collection V1.5"
ENCODER_SCRIPT_NAME = "NKT Encoder Account Allocation V1.5"
RECEIVABLE_SCRIPT_NAME = "NKT Receivable Collection Shortcut V1.5"
DIRECT_AUTHORITY_ROLES = {
    "NKT OWNER",
    "NKT ADMINISTRATOR",
    "NKT Credit Controller",
}
REFERENCE_METHODS = {
    "Check",
    "GCash",
    "Maya",
    "Card",
    "Bank Transfer",
    "Online",
}
ACTIVE_RECEIVABLE_STATUSES = {"Open", "Partially Paid"}


def install_schema():
    if not frappe.db.exists("DocType", RECEIVABLE_DOCTYPE):
        frappe.throw(_("Install NKT Account Credit V1.4 before Account Collection V1.5."))

    _ensure_payment_row_doctype()
    _ensure_allocation_row_doctype()
    # Create the cashier DocType first without its reciprocal Link field.
    # The encoder DocType can then link to the already-existing cashier DocType.
    # Finally, add the cashier -> encoder Link after both DocTypes exist.
    _ensure_cashier_collection_doctype()
    _ensure_encoder_allocation_doctype()
    _ensure_cashier_encoder_link_field()
    _ensure_link_fields()
    _update_v152_labels()
    _append_select_option("NKT Payment Receipt", "payment_purpose", "Customer Account Collection")
    _append_select_option("NKT Payment Receipt", "allocation_status", "Allocated to Account Receivables")
    _append_select_option("NKT Cashier Movement", "movement_type", "Account Collection")
    _install_client_script(CASHIER_SCRIPT_NAME, CASHIER_COLLECTION_DOCTYPE, _cashier_client_script())
    _install_client_script(ENCODER_SCRIPT_NAME, ENCODER_ALLOCATION_DOCTYPE, _encoder_client_script())
    _install_client_script(RECEIVABLE_SCRIPT_NAME, RECEIVABLE_DOCTYPE, _receivable_client_script())

    for dt in (
        CASHIER_COLLECTION_DOCTYPE,
        ENCODER_ALLOCATION_DOCTYPE,
        PAYMENT_ROW_DOCTYPE,
        ALLOCATION_ROW_DOCTYPE,
        RECEIVABLE_DOCTYPE,
        "NKT Payment Receipt",
        "NKT Cashier Movement",
    ):
        frappe.clear_cache(doctype=dt)

    return {
        "installed": True,
        "cashier_collection_doctype": CASHIER_COLLECTION_DOCTYPE,
        "encoder_allocation_doctype": ENCODER_ALLOCATION_DOCTYPE,
    }


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
            for perm in permissions:
                doc.append("permissions", perm)
            changed = True
        if changed:
            doc.flags.ignore_permissions = True
            doc.save(ignore_permissions=True)
        return

    values = {
        "doctype": "DocType",
        "name": name,
        "module": "NKT Store Operations",
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


def _admin_permissions():
    return [
        {
            "role": "System Manager",
            "read": 1,
            "write": 1,
            "create": 1,
            "delete": 1,
            "report": 1,
            "export": 1,
            "print": 1,
            "email": 1,
            "share": 1,
        },
        {
            "role": "NKT OWNER",
            "read": 1,
            "write": 1,
            "create": 1,
            "delete": 1,
            "report": 1,
            "export": 1,
            "print": 1,
            "email": 1,
            "share": 1,
        },
        {
            "role": "NKT ADMINISTRATOR",
            "read": 1,
            "write": 1,
            "create": 1,
            "delete": 1,
            "report": 1,
            "export": 1,
            "print": 1,
            "email": 1,
            "share": 1,
        },
    ]


def _ensure_payment_row_doctype():
    fields = [
        {
            "fieldname": "payment_method",
            "label": "Payment Method",
            "fieldtype": "Select",
            "options": "Cash\nCheck\nGCash\nMaya\nCard\nBank Transfer\nOnline",
            "reqd": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "amount",
            "label": "Account Amount Applied",
            "fieldtype": "Currency",
            "reqd": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "card_surcharge",
            "label": "Card Surcharge (2%)",
            "fieldtype": "Currency",
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "collected_amount",
            "label": "Actual Collected",
            "fieldtype": "Currency",
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "cash_tendered",
            "label": "Cash Tendered",
            "fieldtype": "Currency",
            "in_list_view": 1,
        },
        {
            "fieldname": "change_amount",
            "label": "Change",
            "fieldtype": "Currency",
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "reference_number",
            "label": "Reference Number",
            "fieldtype": "Data",
            "in_list_view": 1,
        },
        {
            "fieldname": "reference_datetime",
            "label": "Reference Date and Time",
            "fieldtype": "Datetime",
        },
        {
            "fieldname": "bank_or_provider",
            "label": "Bank or Provider",
            "fieldtype": "Data",
        },
        {
            "fieldname": "check_number",
            "label": "Check Number",
            "fieldtype": "Data",
        },
        {
            "fieldname": "check_date",
            "label": "Check Date",
            "fieldtype": "Date",
        },
        {
            "fieldname": "remarks",
            "label": "Remarks",
            "fieldtype": "Small Text",
        },
    ]
    _ensure_custom_doctype(PAYMENT_ROW_DOCTYPE, None, fields, [], istable=True)

    # Existing sites already have this dynamically-created child DocType.
    # _ensure_custom_doctype() appends missing fields but intentionally does not
    # rewrite existing field definitions, so normalize the controlled payment
    # nomenclature/labels here without touching any business rows.
    doc = frappe.get_doc("DocType", PAYMENT_ROW_DOCTYPE)
    changed = False
    payment_options = "Cash\nCheck\nGCash\nMaya\nCard\nBank Transfer\nOnline"
    for row in doc.get("fields") or []:
        if row.fieldname == "payment_method" and (row.options or "") != payment_options:
            row.options = payment_options
            changed = True
        elif row.fieldname == "amount" and row.label != "Account Amount Applied":
            row.label = "Account Amount Applied"
            changed = True
    if changed:
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
        frappe.clear_cache(doctype=PAYMENT_ROW_DOCTYPE)


def _ensure_allocation_row_doctype():
    fields = [
        {
            "fieldname": "receivable",
            "label": "Customer Receivable",
            "fieldtype": "Link",
            "options": RECEIVABLE_DOCTYPE,
            "reqd": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "customer_order",
            "label": "Customer Order",
            "fieldtype": "Link",
            "options": "NKT Customer Order",
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "due_date",
            "label": "Due Date",
            "fieldtype": "Date",
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "outstanding_before",
            "label": "Outstanding Before",
            "fieldtype": "Currency",
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "allocated_amount",
            "label": "Allocated Amount",
            "fieldtype": "Currency",
            "reqd": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "outstanding_after",
            "label": "Outstanding After",
            "fieldtype": "Currency",
            "read_only": 1,
            "in_list_view": 1,
        },
    ]
    _ensure_custom_doctype(ALLOCATION_ROW_DOCTYPE, None, fields, [], istable=True)



def _ensure_customer_advance_application_doctype():
    # V2.0C.5.2 audit ledger for applying already-received customer advances.
    name = "NKT Customer Advance Application"
    fields = [
        {"fieldname": "company", "label": "Company", "fieldtype": "Link", "options": "Company", "reqd": 1},
        {"fieldname": "posting_datetime", "label": "Posting Date/Time", "fieldtype": "Datetime", "reqd": 1},
        {"fieldname": "customer", "label": "Customer", "fieldtype": "Link", "options": "Customer", "reqd": 1},
        {"fieldname": "customer_name", "label": "Customer Name", "fieldtype": "Data", "read_only": 1},
        {"fieldname": "customer_advance", "label": "Customer Advance", "fieldtype": "Link", "options": "NKT Customer Advance", "reqd": 1},
        {"fieldname": "source_payment_receipt", "label": "Original Payment Receipt", "fieldtype": "Link", "options": "NKT Payment Receipt", "reqd": 1},
        {"fieldname": "customer_order", "label": "Customer Order", "fieldtype": "Link", "options": "NKT Customer Order", "reqd": 1},
        {"fieldname": "applied_amount", "label": "Applied Amount", "fieldtype": "Currency", "reqd": 1},
        {"fieldname": "application_status", "label": "Application Status", "fieldtype": "Select", "options": "Applied\nReversed", "default": "Applied", "reqd": 1},
        {"fieldname": "applied_by", "label": "Applied By", "fieldtype": "Link", "options": "User", "read_only": 1},
        {"fieldname": "remarks", "label": "Remarks", "fieldtype": "Small Text"},
    ]
    _ensure_custom_doctype(
        name,
        "NKT-ADV-APP-.#####",
        fields,
        _admin_permissions(),
    )
    if frappe.db.exists("DocType", name):
        frappe.db.set_value("DocType", name, "is_submittable", 1, update_modified=False)
        frappe.clear_cache(doctype=name)
    return name


def _ensure_cashier_collection_doctype():
    permissions = _admin_permissions() + [
        {
            "role": "NKT Cashier",
            "read": 1,
            "write": 1,
            "create": 1,
            "delete": 1,
            "print": 1,
            "report": 1,
        },
        {
            "role": "NKT Encoder",
            "read": 1,
            "print": 1,
            "report": 1,
        },
        {
            "role": "NKT Credit Controller",
            "read": 1,
            "print": 1,
            "report": 1,
        },
    ]
    fields = [
        {"fieldname": "collection_details", "label": "Collection Details", "fieldtype": "Section Break"},
        {
            "fieldname": "company",
            "label": "Company",
            "fieldtype": "Link",
            "options": "Company",
            "reqd": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "collection_datetime",
            "label": "Collection Date and Time",
            "fieldtype": "Datetime",
            "default": "Now",
            "reqd": 1,
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "business_date",
            "label": "Business Date",
            "fieldtype": "Date",
            "reqd": 1,
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "cashier_shift",
            "label": "Cashier Shift",
            "fieldtype": "Link",
            "options": "NKT Cashier Shift",
            "reqd": 1,
            "read_only": 1,
        },
        {
            "fieldname": "settlement_location",
            "label": "Settlement Location",
            "fieldtype": "Link",
            "options": "Warehouse",
            "read_only": 1,
        },
        {
            "fieldname": "cashier",
            "label": "Cashier",
            "fieldtype": "Link",
            "options": "User",
            "reqd": 1,
            "read_only": 1,
        },
        {
            "fieldname": "customer",
            "label": "Customer",
            "fieldtype": "Link",
            "options": "Customer",
            "reqd": 1,
            "in_list_view": 1,
            "search_index": 1,
        },
        {
            "fieldname": "customer_name",
            "label": "Customer Name",
            "fieldtype": "Data",
            "read_only": 1,
        },
        {
            "fieldname": "custom_nkt_fast_request_id",
            "label": "Fast Request ID",
            "fieldtype": "Data",
            "read_only": 1,
            "hidden": 1,
            "no_copy": 1,
            "unique": 1,
            "search_index": 1,
        },
        {
            "fieldname": "custom_nkt_fast_payload_hash",
            "label": "Fast Payload Hash",
            "fieldtype": "Data",
            "read_only": 1,
            "hidden": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "referenced_customer_order",
            "label": "Specific Account Order Reference (Optional)",
            "fieldtype": "Link",
            "options": "NKT Customer Order",
            "description": "Use only when the customer identifies a specific account order.",
        },
        {
            "fieldname": "previous_balance",
            "label": "Previous Account Balance",
            "fieldtype": "Currency",
            "read_only": 1,
        },
        {"fieldname": "payment_section", "label": "Payment Received", "fieldtype": "Section Break"},
        {
            "fieldname": "payments",
            "label": "Payments",
            "fieldtype": "Table",
            "options": PAYMENT_ROW_DOCTYPE,
            "reqd": 1,
        },
        {
            "fieldname": "total_payment",
            "label": "Account Amount Applied",
            "fieldtype": "Currency",
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "card_surcharge_total",
            "label": "Card Surcharge (2%)",
            "fieldtype": "Currency",
            "read_only": 1,
        },
        {
            "fieldname": "total_collected",
            "label": "Actual Collected",
            "fieldtype": "Currency",
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "balance_after_collection",
            "label": "Expected Balance After Collection",
            "fieldtype": "Currency",
            "read_only": 1,
        },
        {"fieldname": "reconciliation_section", "label": "Encoder Reconciliation", "fieldtype": "Section Break"},
        {
            "fieldname": "status",
            "label": "Collection Status",
            "fieldtype": "Select",
            "options": "Draft\nSubmitted - Unmatched\nAmbiguous\nMatched\nCancelled",
            "default": "Draft",
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "payment_fingerprint",
            "label": "Payment Fingerprint",
            "fieldtype": "Data",
            "read_only": 1,
            "hidden": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "linked_payment_receipt",
            "label": "Payment Receipt",
            "fieldtype": "Link",
            "options": "NKT Payment Receipt",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "reconciliation_warning",
            "label": "Reconciliation Warning",
            "fieldtype": "Small Text",
            "read_only": 1,
        },
        {
            "fieldname": "submitted_on",
            "label": "Submitted On",
            "fieldtype": "Datetime",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "matched_on",
            "label": "Matched On",
            "fieldtype": "Datetime",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "remarks",
            "label": "Remarks",
            "fieldtype": "Small Text",
        },
    ]
    _ensure_custom_doctype(CASHIER_COLLECTION_DOCTYPE, "NKT-COL-CASH-.#####", fields, permissions)


def _ensure_encoder_allocation_doctype():
    permissions = _admin_permissions() + [
        {
            "role": "NKT Encoder",
            "read": 1,
            "write": 1,
            "create": 1,
            "delete": 1,
            "print": 1,
            "report": 1,
        },
        {
            "role": "NKT Credit Controller",
            "read": 1,
            "write": 1,
            "create": 1,
            "print": 1,
            "report": 1,
        },
        {
            "role": "NKT Cashier",
            "read": 1,
            "print": 1,
            "report": 1,
        },
    ]
    fields = [
        {"fieldname": "allocation_details", "label": "Encoder Collection Entry", "fieldtype": "Section Break"},
        {
            "fieldname": "company",
            "label": "Company",
            "fieldtype": "Link",
            "options": "Company",
            "reqd": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "allocation_date",
            "label": "Collection Business Date",
            "fieldtype": "Date",
            "default": "Today",
            "reqd": 1,
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "encoder",
            "label": "Encoder",
            "fieldtype": "Link",
            "options": "User",
            "reqd": 1,
            "read_only": 1,
        },
        {
            "fieldname": "customer",
            "label": "Customer",
            "fieldtype": "Link",
            "options": "Customer",
            "reqd": 1,
            "in_list_view": 1,
            "search_index": 1,
        },
        {
            "fieldname": "customer_name",
            "label": "Customer Name",
            "fieldtype": "Data",
            "read_only": 1,
        },
        {
            "fieldname": "custom_nkt_fast_request_id",
            "label": "Fast Request ID",
            "fieldtype": "Data",
            "read_only": 1,
            "hidden": 1,
            "no_copy": 1,
            "unique": 1,
            "search_index": 1,
        },
        {
            "fieldname": "custom_nkt_fast_payload_hash",
            "label": "Fast Payload Hash",
            "fieldtype": "Data",
            "read_only": 1,
            "hidden": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "referenced_customer_order",
            "label": "Specific Account Order Reference (Optional)",
            "fieldtype": "Link",
            "options": "NKT Customer Order",
            "description": "Use only when the customer identifies a specific account order.",
        },
        {"fieldname": "declared_payment_section", "label": "Independently Encoded Payment", "fieldtype": "Section Break"},
        {
            "fieldname": "payments",
            "label": "Declared Payments",
            "fieldtype": "Table",
            "options": PAYMENT_ROW_DOCTYPE,
            "reqd": 1,
        },
        {
            "fieldname": "collection_amount",
            "label": "Collection Amount",
            "fieldtype": "Currency",
            "read_only": 1,
            "in_list_view": 1,
        },
        {"fieldname": "allocation_section", "label": "Receivable Allocations", "fieldtype": "Section Break"},
        {
            "fieldname": "allocations",
            "label": "Allocations",
            "fieldtype": "Table",
            "options": ALLOCATION_ROW_DOCTYPE,
            "read_only": 1,
        },
        {
            "fieldname": "total_allocated",
            "label": "Total Allocated",
            "fieldtype": "Currency",
            "read_only": 1,
        },
        {
            "fieldname": "unallocated_amount",
            "label": "Unallocated Amount",
            "fieldtype": "Currency",
            "read_only": 1,
        },
        {
            "fieldname": "application_rule",
            "label": "Automatic Application Rule",
            "fieldtype": "Data",
            "read_only": 1,
        },
        {
            "fieldname": "application_summary",
            "label": "Automatic Application Preview",
            "fieldtype": "Small Text",
            "read_only": 1,
        },
        {"fieldname": "matching_section", "label": "Cashier Reconciliation", "fieldtype": "Section Break"},
        {
            "fieldname": "status",
            "label": "Allocation Status",
            "fieldtype": "Select",
            "options": "Draft\nUnmatched\nAmbiguous\nMatched\nCancelled",
            "default": "Draft",
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "payment_fingerprint",
            "label": "Payment Fingerprint",
            "fieldtype": "Data",
            "read_only": 1,
            "hidden": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "matched_cashier_collection",
            "label": "Matched Cashier Collection",
            "fieldtype": "Link",
            "options": CASHIER_COLLECTION_DOCTYPE,
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "linked_payment_receipt",
            "label": "Payment Receipt",
            "fieldtype": "Link",
            "options": "NKT Payment Receipt",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "reconciliation_warning",
            "label": "Reconciliation Warning",
            "fieldtype": "Small Text",
            "read_only": 1,
        },
        {
            "fieldname": "allocations_posted",
            "label": "Allocations Posted",
            "fieldtype": "Check",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "posted_on",
            "label": "Posted On",
            "fieldtype": "Datetime",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "resolved_by",
            "label": "Resolved By",
            "fieldtype": "Link",
            "options": "User",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "resolution_reason",
            "label": "Resolution Reason",
            "fieldtype": "Small Text",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "remarks",
            "label": "Remarks",
            "fieldtype": "Small Text",
        },
    ]
    _ensure_custom_doctype(ENCODER_ALLOCATION_DOCTYPE, "NKT-COL-ENC-.#####", fields, permissions)


def _ensure_cashier_encoder_link_field():
    """Add the reciprocal cashier -> encoder Link only after both DocTypes exist.

    Frappe validates Link options while a DocType is inserted. Creating two new
    DocTypes that link to each other in a single first-pass insert therefore
    fails because one side does not exist yet. This second pass is idempotent.
    """
    doc = frappe.get_doc("DocType", CASHIER_COLLECTION_DOCTYPE)
    if any(
        row.fieldname == "matched_encoder_allocation"
        for row in (doc.get("fields") or [])
    ):
        return

    doc.append(
        "fields",
        {
            "fieldname": "matched_encoder_allocation",
            "label": "Matched Encoder Allocation",
            "fieldtype": "Link",
            "options": ENCODER_ALLOCATION_DOCTYPE,
            "read_only": 1,
            "no_copy": 1,
        },
    )
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)


def _ensure_link_fields():
    custom_fields = {
        "NKT Payment Receipt": [
            {
                "fieldname": "custom_nkt_payment_kind",
                "label": "NKT Payment Kind",
                "fieldtype": "Select",
                "options": "\nCashier Sale Payment\nCustomer Account Collection",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "allocation_status",
            },
            {
                "fieldname": "custom_nkt_cashier_account_collection",
                "label": "Cashier Account Collection",
                "fieldtype": "Link",
                "options": CASHIER_COLLECTION_DOCTYPE,
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_payment_kind",
            },
            {
                "fieldname": "custom_nkt_encoder_account_allocation",
                "label": "Encoder Account Allocation",
                "fieldtype": "Link",
                "options": ENCODER_ALLOCATION_DOCTYPE,
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_cashier_account_collection",
            },
            {
                "fieldname": "custom_nkt_plate_number",
                "label": "Plate Number",
                "fieldtype": "Data",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_encoder_account_allocation",
            },
            {
                "fieldname": "custom_nkt_source_order_slip",
                "label": "OS# (Physical Order Slip)",
                "fieldtype": "Data",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_plate_number",
            },
        ],
        ENCODER_ALLOCATION_DOCTYPE: [
            {
                "fieldname": "custom_nkt_plate_number",
                "label": "Plate Number",
                "fieldtype": "Data",
                "reqd": 0,
                "no_copy": 1,
                "insert_after": "customer_name",
                "description": "Optional vehicle plate reference captured by Encoder. Audit/reference only; never used for matching.",
            },
            {
                "fieldname": "custom_nkt_source_order_slip",
                "label": "OS# (Physical Order Slip)",
                "fieldtype": "Data",
                "reqd": 0,
                "no_copy": 1,
                "insert_after": "custom_nkt_plate_number",
                "description": "Optional number printed/written on the physical NKT Order Slip. Audit/reference only; never used for matching.",
            },
        ],
        RECEIVABLE_DOCTYPE: [
            {
                "fieldname": "custom_nkt_last_collection_on",
                "label": "Last Collection On",
                "fieldtype": "Datetime",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "outstanding_amount",
            },
            {
                "fieldname": "custom_nkt_collection_count",
                "label": "Collection Count",
                "fieldtype": "Int",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_last_collection_on",
            },
        ],
    }
    create_custom_fields(custom_fields, ignore_validate=True, update=True)


def _update_v152_labels():
    updates = {
        ENCODER_ALLOCATION_DOCTYPE: {
            "allocation_details": "Account Payment Verification",
            "allocation_section": "Automatic Account Application",
            "allocations": "Automatic Applications",
            "total_allocated": "Automatically Applied",
            "unallocated_amount": "Unapplied Amount",
            "status": "Verification Status",
        },
        CASHIER_COLLECTION_DOCTYPE: {
            "reconciliation_section": "Encoder Payment Verification",
            "matched_encoder_allocation": "Matched Payment Verification",
        },
    }

    def move_after(rows, fieldname, after_fieldname):
        item = next((row for row in rows if row.fieldname == fieldname), None)
        anchor = next((row for row in rows if row.fieldname == after_fieldname), None)
        if not item or not anchor or item is anchor:
            return False
        current_index = rows.index(item)
        target_index = rows.index(anchor) + 1
        if current_index == target_index or current_index + 1 == target_index:
            return False
        rows.remove(item)
        target_index = rows.index(anchor) + 1
        rows.insert(target_index, item)
        return True

    for doctype, labels in updates.items():
        if not frappe.db.exists("DocType", doctype):
            continue
        doc = frappe.get_doc("DocType", doctype)
        rows = doc.get("fields") or []
        changed = False
        for row in rows:
            if row.fieldname in labels and row.label != labels[row.fieldname]:
                row.label = labels[row.fieldname]
                changed = True
            if doctype == ENCODER_ALLOCATION_DOCTYPE and row.fieldname == "allocations":
                if row.reqd:
                    row.reqd = 0
                    changed = True
                if not row.read_only:
                    row.read_only = 1
                    changed = True

        if doctype == CASHIER_COLLECTION_DOCTYPE:
            changed = move_after(rows, "referenced_customer_order", "customer_name") or changed
        if doctype == ENCODER_ALLOCATION_DOCTYPE:
            changed = move_after(rows, "referenced_customer_order", "customer_name") or changed
            changed = move_after(rows, "application_rule", "unallocated_amount") or changed
            changed = move_after(rows, "application_summary", "application_rule") or changed

        if changed:
            for idx, row in enumerate(rows, 1):
                row.idx = idx
            doc.flags.ignore_permissions = True
            doc.save(ignore_permissions=True)

def _append_select_option(doctype, fieldname, option):
    meta = frappe.get_meta(doctype)
    field = meta.get_field(fieldname)
    if not field:
        return
    options = [line.strip() for line in (field.options or "").splitlines() if line.strip()]
    if option in options:
        return
    options.append(option)
    from frappe.custom.doctype.property_setter.property_setter import make_property_setter

    make_property_setter(
        doctype,
        fieldname,
        "options",
        "\n".join(options),
        "Text",
        validate_fields_for_doctype=False,
    )


def _install_client_script(name, dt, script):
    values = {"dt": dt, "view": "Form", "enabled": 1, "script": script}
    if frappe.db.exists("Client Script", name):
        doc = frappe.get_doc("Client Script", name)
        doc.update(values)
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc({"doctype": "Client Script", "name": name, **values})
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)


def _cashier_client_script():
    return r'''
(function () {
    const ROOT = "nkt_operations.nkt_store_operations.features.payments_accounts.collection";

    function calculate(frm) {
        let total = 0;
        (frm.doc.payments || []).forEach(row => {
            const amount = flt(row.amount);
            total += amount;
            row.change_amount = row.payment_method === "Cash"
                ? Math.max(flt(row.cash_tendered) - amount, 0)
                : 0;
        });
        frm.set_value("total_payment", total);
        frm.set_value("balance_after_collection", Math.max(flt(frm.doc.previous_balance) - total, 0));
        frm.refresh_field("payments");
    }

    async function load_context(frm) {
        if (!frm.doc.company) return;
        const r = await frappe.call({method: `${ROOT}.get_cashier_context`, args: {company: frm.doc.company}});
        if (!r.message) return;
        await frm.set_value("cashier", r.message.cashier || frappe.session.user);
        await frm.set_value("cashier_shift", r.message.cashier_shift);
        await frm.set_value("business_date", r.message.business_date);
        await frm.set_value("collection_datetime", r.message.collection_datetime);
        if (r.message.settlement_location) {
            await frm.set_value("settlement_location", r.message.settlement_location);
        }
    }

    async function load_balance(frm) {
        if (!frm.doc.customer) return;
        const r = await frappe.call({method: `${ROOT}.get_customer_collection_snapshot`, args: {customer: frm.doc.customer}});
        if (!r.message) return;
        await frm.set_value("customer_name", r.message.customer_name);
        await frm.set_value("previous_balance", r.message.current_balance);
        calculate(frm);
    }

    frappe.ui.form.on("NKT Cashier Account Collection", {
        setup(frm) {
            frm.set_query("customer", () => ({
                filters: {
                    custom_nkt_allow_account_sales: 1
                }
            }));
            frm.set_query("referenced_customer_order", () => ({
                filters: {
                    customer: frm.doc.customer,
                    payment_status: ["in", ["Charged to Account", "Partially Paid"]],
                    amount_due: [">", 0]
                }
            }));
        },
        async refresh(frm) {
            if (frm.is_new()) await load_context(frm);
            calculate(frm);
            if (frm.doc.status === "Draft" && !frm.is_new()) {
                frm.add_custom_button(__("Submit Collection"), async () => {
                    await frappe.call({
                        method: `${ROOT}.submit_cashier_collection`,
                        type: "POST",
                        args: {collection: frm.doc.name},
                        freeze: true,
                        freeze_message: __("Recording cashier collection...")
                    });
                    frm.reload_doc();
                });
            }
            if (["Submitted - Unmatched", "Ambiguous", "Matched"].includes(frm.doc.status)) {
                frm.add_custom_button(__("Print Collection Receipt"), () => frm.print_doc(), __("Print"));
            }
            if (["Submitted - Unmatched", "Ambiguous"].includes(frm.doc.status)) {
                frm.add_custom_button(__("Retry Reconciliation"), async () => {
                    await frappe.call({method: `${ROOT}.retry_collection_match`, type: "POST", args: {cashier_collection: frm.doc.name}, freeze: true});
                    frm.reload_doc();
                }, __("Reconciliation"));
            }
        },
        company(frm) { if (frm.is_new()) load_context(frm); },
        customer(frm) { load_balance(frm); },
        validate(frm) { calculate(frm); },
        payments_remove(frm) { calculate(frm); }
    });

    frappe.ui.form.on("NKT Account Collection Payment", {
        payment_method(frm) { calculate(frm); },
        amount(frm) { calculate(frm); },
        cash_tendered(frm) { calculate(frm); }
    });
})();
'''


def _encoder_client_script():
    return r'''(function () {
    const ROOT = "nkt_operations.nkt_store_operations.features.payments_accounts.collection";

    function calculate(frm) {
        let payment = 0;
        let allocated = 0;
        (frm.doc.payments || []).forEach(row => {
            payment += flt(row.amount);
            row.change_amount = 0;
        });
        (frm.doc.allocations || []).forEach(row => {
            allocated += flt(row.allocated_amount);
            row.outstanding_after = Math.max(flt(row.outstanding_before) - flt(row.allocated_amount), 0);
        });
        frm.set_value("collection_amount", payment);
        frm.set_value("total_allocated", allocated);
        frm.set_value("unallocated_amount", Math.max(payment - allocated, 0));
        frm.refresh_field("payments");
        frm.refresh_field("allocations");
    }

    function invalidate_preview(frm) {
        if ((frm.doc.allocations || []).length || frm.doc.application_rule || frm.doc.application_summary) {
            frm.clear_table("allocations");
            frm.set_value("application_rule", "");
            frm.set_value("application_summary", "");
        }
        calculate(frm);
    }

    async function preview_application(frm) {
        calculate(frm);
        if (!frm.doc.customer) {
            frappe.msgprint(__("Select a Customer first."));
            return;
        }
        if (flt(frm.doc.collection_amount) <= 0) {
            frappe.msgprint(__("Enter the independently encoded payment rows first."));
            return;
        }
        const r = await frappe.call({
            method: `${ROOT}.preview_automatic_application`,
            args: {
                customer: frm.doc.customer,
                amount: frm.doc.collection_amount,
                referenced_customer_order: frm.doc.referenced_customer_order || null
            },
            freeze: true,
            freeze_message: __("Building automatic account application...")
        });
        const result = r.message || {};
        frm.clear_table("allocations");
        (result.allocations || []).forEach(row => {
            const child = frm.add_child("allocations");
            Object.assign(child, row);
        });
        await frm.set_value("application_rule", result.rule || "");
        await frm.set_value("application_summary", result.summary || "");
        calculate(frm);
    }

    async function resolve(frm) {
        const r = await frappe.call({method: `${ROOT}.get_collection_candidates`, args: {encoder_allocation: frm.doc.name}});
        const candidates = r.message || [];
        if (!candidates.length) {
            frappe.msgprint(__("No exact cashier collection candidates are currently available."));
            return;
        }
        const dialog = new frappe.ui.Dialog({
            title: __("Resolve Ambiguous Account Collection"),
            fields: [
                {
                    fieldname: "cashier_collection",
                    fieldtype: "Select",
                    label: __("Cashier Collection"),
                    options: candidates.map(c => c.name),
                    reqd: 1,
                    description: candidates.map(c => `${c.name} — ${c.cashier} — ${c.collection_datetime} — ${format_currency(c.total_payment)}`).join("<br>")
                },
                {fieldname: "reason", fieldtype: "Small Text", label: __("Resolution Reason"), reqd: 1},
                {fieldtype: "Section Break", label: __("Authority")},
                {fieldname: "authorized_user", fieldtype: "Link", options: "User", label: __("Authorized User")},
                {fieldname: "authorized_password", fieldtype: "Password", label: __("Authorized Password")}
            ],
            primary_action_label: __("Link Selected Pair"),
            async primary_action(values) {
                await frappe.call({
                    method: `${ROOT}.resolve_collection_match`,
                    type: "POST",
                    args: {
                        encoder_allocation: frm.doc.name,
                        cashier_collection: values.cashier_collection,
                        reason: values.reason,
                        authorized_user: values.authorized_user || null,
                        authorized_password: values.authorized_password || null
                    },
                    freeze: true
                });
                dialog.hide();
                frm.reload_doc();
            }
        });
        dialog.show();
    }

    frappe.ui.form.on("NKT Encoder Account Allocation", {
        setup(frm) {
            frm.set_query("referenced_customer_order", () => ({
                filters: {
                    customer: frm.doc.customer,
                    payment_status: ["in", ["Charged to Account", "Partially Paid"]],
                    amount_due: [">", 0]
                }
            }));
        },
        async refresh(frm) {
            if (frm.is_new()) {
                await frm.set_value("encoder", frappe.session.user);
                await frm.set_value("allocation_date", frappe.datetime.get_today());
            }
            frm.set_df_property("allocations", "read_only", 1);
            calculate(frm);
            if (frm.doc.status === "Draft" && !frm.is_new()) {
                frm.add_custom_button(__("Preview Automatic Application"), () => preview_application(frm), __("Verification"));
                frm.add_custom_button(__("Submit Payment Verification"), async () => {
                    await frappe.call({
                        method: `${ROOT}.submit_encoder_allocation`,
                        type: "POST",
                        args: {allocation: frm.doc.name},
                        freeze: true,
                        freeze_message: __("Verifying collection and applying it to the customer account...")
                    });
                    frm.reload_doc();
                });
            }
            if (["Unmatched", "Ambiguous"].includes(frm.doc.status)) {
                frm.add_custom_button(__("Retry Reconciliation"), async () => {
                    await frappe.call({method: `${ROOT}.retry_collection_match`, type: "POST", args: {encoder_allocation: frm.doc.name}, freeze: true});
                    frm.reload_doc();
                }, __("Reconciliation"));
            }
            if (frm.doc.status === "Ambiguous") {
                frm.add_custom_button(__("Resolve Ambiguous Match"), () => resolve(frm), __("Reconciliation"));
            }
        },
        customer(frm) { invalidate_preview(frm); },
        referenced_customer_order(frm) { invalidate_preview(frm); },
        validate(frm) { calculate(frm); },
        payments_remove(frm) { invalidate_preview(frm); }
    });

    frappe.ui.form.on("NKT Account Collection Payment", {
        payment_method(frm) { invalidate_preview(frm); },
        amount(frm) { invalidate_preview(frm); },
        reference_number(frm) { invalidate_preview(frm); },
        check_number(frm) { invalidate_preview(frm); }
    });
})();
'''

def _receivable_client_script():
    return r'''
frappe.ui.form.on("NKT Customer Receivable", {
    refresh(frm) {
        if (["Open", "Partially Paid"].includes(frm.doc.status) && flt(frm.doc.outstanding_amount) > 0) {
            frm.add_custom_button(__("New Account Payment Verification"), () => {
                frappe.new_doc("NKT Encoder Account Allocation", {
                    company: frm.doc.company,
                    customer: frm.doc.customer,
                    customer_name: frm.doc.customer_name,
                    referenced_customer_order: frm.doc.customer_order,
                    allocation_date: frappe.datetime.get_today()
                });
            }, __("Collection"));
        }
    }
});
'''


def _clean(value):
    return " ".join((value or "").strip().lower().split())


def _r(value, places=2):
    return round(flt(value), places)


def _payment_fingerprint(rows):
    grouped = defaultdict(float)
    detailed = []
    for row in rows or []:
        method = normalize_payment_method(
            row.get("payment_method") if isinstance(row, dict) else row.payment_method
        )
        amount = flt(row.get("amount") if isinstance(row, dict) else row.amount)
        if not method or amount <= TOLERANCE:
            continue
        if method == "Cash":
            grouped[method] += amount
        else:
            reference = ""
            if isinstance(row, dict):
                reference = row.get("check_number") or row.get("reference_number") or ""
            else:
                reference = row.check_number or row.reference_number or ""
            detailed.append({"method": method, "amount": _r(amount), "reference": _clean(reference)})
    value = [{"method": method, "amount": _r(amount), "reference": ""} for method, amount in sorted(grouped.items())]
    value.extend(sorted(detailed, key=lambda d: (d["method"], d["reference"], d["amount"])))
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _collection_fingerprint(rows, referenced_customer_order=None):
    base = _payment_fingerprint(rows)
    reference = _clean(referenced_customer_order)
    if not reference:
        return base
    raw = json.dumps({"payment": base, "account_order_reference": reference}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_referenced_customer_order(customer, referenced_customer_order):
    if not referenced_customer_order:
        return None
    row = frappe.db.get_value(
        RECEIVABLE_DOCTYPE,
        {"customer_order": referenced_customer_order},
        ["name", "customer", "customer_order", "posting_date", "due_date", "outstanding_amount", "status", "credit_control_status", "creation"],
        as_dict=True,
    )
    if not row:
        frappe.throw(_("Specific Account Order Reference {0} has no customer receivable.").format(referenced_customer_order))
    if row.customer != customer:
        frappe.throw(_("Specific Account Order Reference {0} belongs to another customer.").format(referenced_customer_order))
    if row.status not in ACTIVE_RECEIVABLE_STATUSES or row.credit_control_status != "Approved" or flt(row.outstanding_amount) <= TOLERANCE:
        frappe.throw(_("Specific Account Order Reference {0} is not an approved open account order.").format(referenced_customer_order))
    return row


def _payment_total(rows):
    return sum(max(flt(row.amount), 0) for row in (rows or []))


def _validate_payment_rows(rows, *, cashier_side=False, parent_name=None):
    if not rows:
        frappe.throw(_("Enter at least one payment row."))
    seen_checks = set()
    for row in rows:
        apply_payment_row_card_fields(row)
        method = row.payment_method
        ensure_card_posting_allowed(method, "Card Account Collection")
        amount = flt(row.amount)
        if not method:
            frappe.throw(_("Payment Method is required on row {0}.").format(row.idx))
        if method == "Account":
            frappe.throw(_("Account is not money received and cannot be used for a customer collection."))
        if amount <= TOLERANCE:
            frappe.throw(_("Payment amount must be greater than zero on row {0}.").format(row.idx))
        if method == "Cash":
            if cashier_side and flt(row.cash_tendered) + TOLERANCE < amount:
                frappe.throw(_("Cash Tendered cannot be less than the amount on row {0}.").format(row.idx))
            row.change_amount = max(flt(row.cash_tendered) - amount, 0) if cashier_side else 0
            continue

        reference = (row.get("check_number") or row.get("reference_number") or "").strip()
        if method in REFERENCE_METHODS and not reference:
            frappe.throw(_("Reference Number is required for {0} on row {1}.").format(method, row.idx))

        if method == "Check":
            provider = (row.get("bank_or_provider") or "").strip()
            if not provider:
                frappe.throw(_("Issuing Bank is required for Check on row {0}.").format(row.idx))
            key = (
                "".join(provider.lower().split()),
                "".join(reference.lower().split()),
            )
            if key in seen_checks:
                frappe.throw(_("Duplicate physical Check in this collection: {0} / {1}.").format(provider, reference))
            seen_checks.add(key)
            # Cross-transaction Check duplicate protection is enforced by the
            # NKT Payment Receipt controller using Customer + Bank + Check Number.
            # Encoder verification may repeat the Cashier's check identity.
        # C2.3.1: GCash/Maya/Card/Bank Transfer/Online references
        # remain required, but are intentionally not uniqueness keys.
def _customer_balance(customer):
    return flt(
        frappe.db.sql(
            f"""
            SELECT COALESCE(SUM(outstanding_amount), 0)
            FROM `tab{RECEIVABLE_DOCTYPE}`
            WHERE customer = %s
              AND status IN ('Open', 'Partially Paid')
              AND credit_control_status = 'Approved'
            """,
            customer,
        )[0][0]
    )


@frappe.whitelist()
def get_cashier_context(company=None):
    from nkt_operations.nkt_store_operations.doctype.nkt_cashier_sale.nkt_cashier_sale import (
        get_active_cashier_context,
    )

    context = get_active_cashier_context(company=company or None)
    if not context:
        frappe.throw(_("Open a cashier shift before recording an account collection."))
    return {
        "company": context.get("company") or company,
        "cashier": frappe.session.user,
        "cashier_shift": context.get("cashier_shift") or context.get("shift"),
        "business_date": str(context.get("business_date") or today()),
        "collection_datetime": str(now_datetime()),
        "settlement_location": context.get("settlement_location") or context.get("default_warehouse"),
    }


@frappe.whitelist()
def get_customer_collection_snapshot(customer):
    if not customer:
        return None
    customer_name = frappe.db.get_value("Customer", customer, "customer_name")
    return {
        "customer": customer,
        "customer_name": customer_name,
        "current_balance": _customer_balance(customer),
    }


@frappe.whitelist()
def preview_automatic_application(customer, amount=None, referenced_customer_order=None):
    amount = max(flt(amount), 0)
    if amount <= TOLERANCE:
        frappe.throw(_("Collection amount must be greater than zero."))

    rows = frappe.get_all(
        RECEIVABLE_DOCTYPE,
        filters={
            "customer": customer,
            "status": ["in", ["Open", "Partially Paid"]],
            "credit_control_status": "Approved",
            "outstanding_amount": [">", TOLERANCE],
        },
        fields=["name", "customer_order", "posting_date", "due_date", "outstanding_amount", "creation"],
        order_by="posting_date asc, creation asc, name asc",
    )
    total_outstanding = sum(flt(row.outstanding_amount) for row in rows)

    if not rows:
        return {
            "allocations": [],
            "unallocated_amount": amount,
            "rule": "No approved open receivables; entire payment remains unapplied customer advance",
            "summary": _("No approved open receivables. {0} will remain as unapplied customer advance after exact Encoder verification.").format(
                frappe.format_value(amount, {"fieldtype": "Currency"})
            ),
        }

    if referenced_customer_order:
        referenced = _validate_referenced_customer_order(customer, referenced_customer_order)
        referenced = next((row for row in rows if row.customer_order == referenced_customer_order), referenced)
        ordered = [referenced] + [row for row in rows if row.customer_order != referenced_customer_order]
        rule = "Direct account reference first; any remainder applied oldest outstanding account first"
    else:
        exact_matches = [row for row in rows if abs(flt(row.outstanding_amount) - amount) <= TOLERANCE]
        if len(exact_matches) == 1:
            ordered = [exact_matches[0]]
            rule = "Unique exact outstanding-value match"
        elif len(exact_matches) > 1:
            ordered = rows
            rule = "Multiple exact matches; oldest outstanding account first"
        else:
            ordered = rows
            rule = "Oldest outstanding account first (earliest to latest)"

    remaining = amount
    result = []
    summary_lines = []
    for row in ordered:
        if remaining <= TOLERANCE:
            break
        allocate = min(flt(row.outstanding_amount), remaining)
        if allocate <= TOLERANCE:
            continue
        after = max(flt(row.outstanding_amount) - allocate, 0)
        result.append({
            "receivable": row.name,
            "customer_order": row.customer_order,
            "due_date": row.due_date,
            "outstanding_before": flt(row.outstanding_amount),
            "allocated_amount": allocate,
            "outstanding_after": after,
        })
        summary_lines.append(
            f"{row.name} / {row.customer_order}: {frappe.format_value(allocate, {'fieldtype': 'Currency'})} "
            f"applied; balance {frappe.format_value(after, {'fieldtype': 'Currency'})}"
        )
        remaining -= allocate

    unallocated = max(remaining, 0)
    if unallocated > TOLERANCE:
        summary_lines.append(
            _("{0} remains as unapplied customer advance.").format(
                frappe.format_value(unallocated, {"fieldtype": "Currency"})
            )
        )

    return {
        "allocations": result,
        "unallocated_amount": unallocated,
        "rule": rule,
        "summary": "\n".join(summary_lines),
    }


@frappe.whitelist()
def get_open_receivables(customer, amount=None, referenced_customer_order=None):
    # Backward-compatible endpoint used by V1.5/V1.5.1 clients.
    return preview_automatic_application(customer, amount, referenced_customer_order).get("allocations")


def _build_automatic_application(doc):
    plan = preview_automatic_application(doc.customer, _payment_total(doc.get("payments")), doc.get("referenced_customer_order"))
    doc.set("allocations", [])
    for row in plan.get("allocations") or []:
        doc.append("allocations", row)
    doc.application_rule = plan.get("rule")
    doc.application_summary = plan.get("summary")
    doc.unallocated_amount = flt(plan.get("unallocated_amount"))
    return plan



def _set_if_field(doc, fieldname, value):
    if doc.meta.has_field(fieldname):
        doc.set(fieldname, value)


def _choose_payment_purpose():
    field = frappe.get_meta("NKT Payment Receipt").get_field("payment_purpose")
    options = [line.strip() for line in (field.options or "").splitlines() if line.strip()] if field else []
    for candidate in ("Customer Account Collection", "Account Collection", "Customer Order Payment", "Cashier Sale Payment"):
        if candidate in options:
            return candidate
    return "Customer Account Collection"


def _ensure_payment_receipt(collection):
    existing = collection.get("linked_payment_receipt") or frappe.db.get_value(
        "NKT Payment Receipt",
        {"custom_nkt_cashier_account_collection": collection.name, "docstatus": ["!=", 2]},
        "name",
    )
    if existing:
        return existing

    receipt = frappe.get_doc(
        {
            "doctype": "NKT Payment Receipt",
            "company": collection.company,
            "receipt_datetime": collection.collection_datetime,
            "payment_purpose": _choose_payment_purpose(),
            "customer": collection.customer,
            "customer_name": collection.customer_name,
            "received_by": collection.cashier,
            "encoded_by": collection.cashier,
            "allocation_status": "Unallocated - Awaiting Encoder",
            "remarks": f"Cashier account collection {collection.name}; awaiting independent encoder allocation.",
        }
    )
    _set_if_field(receipt, "custom_nkt_payment_kind", "Customer Account Collection")
    _set_if_field(receipt, "custom_nkt_cashier_account_collection", collection.name)
    # UI7C1B1: freeze the customer account envelope at the physical collection
    # event. `previous_balance` is captured by Cashier Collection before receipt
    # creation. Account balance is reduced by principal payment only; Card
    # surcharge is a fee collected in addition and must not reduce receivables.
    _set_if_field(receipt, "amount_due_before_receipt", flt(collection.get("previous_balance")))
    _set_if_field(
        receipt,
        "remaining_balance",
        flt(collection.get("previous_balance")) - flt(collection.get("total_payment")),
    )

    child_meta = frappe.get_meta("NKT Payment Detail")
    for row in collection.get("payments") or []:
        values = {
            "payment_method": normalize_payment_method(row.payment_method),
            "amount": row.amount,
            "card_surcharge": row_card_surcharge(row),
            "collected_amount": row_collected_amount(row),
            "cash_tendered": row.cash_tendered if normalize_payment_method(row.payment_method) == "Cash" else 0,
            "reference_number": row.reference_number,
            "reference_datetime": row.reference_datetime,
            "bank_or_provider": row.bank_or_provider,
            "check_number": row.check_number,
            "check_date": row.check_date,
            "verification_status": "Not Required",
            "affects_cash_drawer": 1 if row.payment_method == "Cash" else 0,
            "remarks": row.remarks,
        }
        values = {key: value for key, value in values.items() if child_meta.has_field(key)}
        receipt.append("payments", values)
    receipt.flags.ignore_permissions = True
    receipt.insert(ignore_permissions=True)
    receipt.submit()
    frappe.db.set_value(CASHIER_COLLECTION_DOCTYPE, collection.name, "linked_payment_receipt", receipt.name, update_modified=False)
    return receipt.name


def _resolve_cashier_movement_creator():
    candidates = [
        "nkt_operations.nkt_store_operations.doctype.nkt_cashier_movement.nkt_cashier_movement.create_cashier_movement",
        "nkt_operations.nkt_store_operations.nkt_cashier_movement.create_cashier_movement",
    ]
    for path in candidates:
        try:
            return frappe.get_attr(path)
        except Exception:
            continue
    frappe.throw(_("Could not locate the existing create_cashier_movement function."))


def _ensure_cashier_movements(collection):
    # Some live versions of NKT Payment Receipt may create their own cashier
    # movements on submit. If that happened, do not create a second set.
    receipt_name = collection.get("linked_payment_receipt")
    if receipt_name:
        receipt_movements = frappe.get_all(
            "NKT Cashier Movement",
            filters={
                "source_doctype": "NKT Payment Receipt",
                "source_name": receipt_name,
                "docstatus": ["!=", 2],
            },
            fields=["name", "amount"],
        )
        if receipt_movements:
            movement_total = sum(flt(row.amount) for row in receipt_movements)
            if abs(movement_total - flt(collection.get("total_collected") or collection.total_payment)) > TOLERANCE:
                frappe.throw(
                    _(
                        "Payment Receipt {0} created cashier movements totaling {1}, but the collection total is {2}. Review before retrying."
                    ).format(
                        receipt_name,
                        frappe.format_value(movement_total, {"fieldtype": "Currency"}),
                        frappe.format_value(collection.get("total_collected") or collection.total_payment, {"fieldtype": "Currency"}),
                    )
                )
            return

    create_cashier_movement = _resolve_cashier_movement_creator()
    for row in collection.get("payments") or []:
        existing = frappe.db.get_value(
            "NKT Cashier Movement",
            {
                "source_doctype": CASHIER_COLLECTION_DOCTYPE,
                "source_name": collection.name,
                "source_row": row.name,
                "docstatus": ["!=", 2],
            },
            "name",
        )
        if existing:
            continue
        create_cashier_movement(
            company=collection.company,
            posting_datetime=collection.collection_datetime,
            cashier_shift=collection.cashier_shift,
            settlement_location=collection.settlement_location,
            cashier=collection.cashier,
            movement_type="Account Collection",
            direction="In",
            payment_method=row.payment_method,
            amount=row_collected_amount(row),
            settlement_amount=row.amount,
            card_surcharge=row_card_surcharge(row),
            source_doctype=CASHIER_COLLECTION_DOCTYPE,
            source_name=collection.name,
            source_row=row.name,
            customer=collection.customer,
            reference_number=row.reference_number or row.check_number or "",
            remarks=f"Cashier customer-account collection {collection.name}.",
        )


@frappe.whitelist()
def submit_cashier_collection(collection):
    doc = frappe.get_doc(CASHIER_COLLECTION_DOCTYPE, collection)
    if doc.status == "Matched":
        return {"cashier_collection": doc.name, "status": doc.status, "already_matched": True}
    if doc.status not in {"Draft", "Submitted - Unmatched", "Ambiguous"}:
        frappe.throw(_("Cashier Collection {0} cannot be submitted from status {1}.").format(doc.name, doc.status))

    context = get_cashier_context(doc.company)
    doc.cashier = context["cashier"]
    doc.cashier_shift = context["cashier_shift"]
    doc.business_date = context["business_date"]
    doc.collection_datetime = now_datetime()
    if context.get("settlement_location"):
        doc.settlement_location = context["settlement_location"]
    doc.customer_name = frappe.db.get_value("Customer", doc.customer, "customer_name")
    doc.previous_balance = _customer_balance(doc.customer)
    _validate_referenced_customer_order(doc.customer, doc.get("referenced_customer_order"))
    _validate_payment_rows(doc.get("payments"), cashier_side=True, parent_name=doc.name)
    total = _payment_total(doc.get("payments"))
    if total <= TOLERANCE:
        frappe.throw(_("Collection amount must be greater than zero."))
    # C5 Payment on Account: money may be received even when it exceeds the
    # current receivable balance or when no receivable exists. The verified
    # remainder becomes unapplied Customer Advance; this is not an Order
    # Payment overpayment override.
    doc.total_payment = total
    card_surcharge_total = sum(row_card_surcharge(row) for row in (doc.get("payments") or []))
    total_collected = sum(row_collected_amount(row) for row in (doc.get("payments") or []))
    if doc.meta.has_field("card_surcharge_total"):
        doc.card_surcharge_total = card_surcharge_total
    if doc.meta.has_field("total_collected"):
        doc.total_collected = total_collected
    doc.balance_after_collection = max(flt(doc.previous_balance) - total, 0)
    doc.payment_fingerprint = _collection_fingerprint(doc.get("payments"), doc.get("referenced_customer_order"))
    doc.status = "Submitted - Unmatched"
    doc.submitted_on = doc.submitted_on or now_datetime()
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)

    receipt_name = _ensure_payment_receipt(doc)
    doc.reload()
    _ensure_cashier_movements(doc)
    _try_match_cashier_collection(doc.name)
    return {
        "cashier_collection": doc.name,
        "payment_receipt": receipt_name,
        "status": frappe.db.get_value(CASHIER_COLLECTION_DOCTYPE, doc.name, "status"),
    }


def _validate_encoder_allocations(doc):
    _validate_payment_rows(doc.get("payments"), cashier_side=False, parent_name=doc.name)
    payment_total = _payment_total(doc.get("payments"))
    if payment_total <= TOLERANCE:
        frappe.throw(_("Encoder collection amount must be greater than zero."))
    # C5: a verified customer payment may be fully unapplied when there are
    # no approved open receivables. Allocation rows are therefore optional.
    seen = set()
    allocated_total = 0
    for row in doc.allocations:
        if not row.receivable:
            frappe.throw(_("Customer Receivable is required on allocation row {0}.").format(row.idx))
        if row.receivable in seen:
            frappe.throw(_("Receivable {0} appears more than once.").format(row.receivable))
        seen.add(row.receivable)
        receivable = frappe.db.get_value(
            RECEIVABLE_DOCTYPE,
            row.receivable,
            ["customer", "customer_order", "due_date", "outstanding_amount", "status", "credit_control_status"],
            as_dict=True,
        )
        if not receivable:
            frappe.throw(_("Receivable {0} does not exist.").format(row.receivable))
        if receivable.customer != doc.customer:
            frappe.throw(_("Receivable {0} belongs to another customer.").format(row.receivable))
        if receivable.status not in ACTIVE_RECEIVABLE_STATUSES or receivable.credit_control_status != "Approved":
            frappe.throw(_("Receivable {0} is not an approved open receivable.").format(row.receivable))
        amount = flt(row.allocated_amount)
        if amount <= TOLERANCE:
            frappe.throw(_("Allocated Amount must be greater than zero on row {0}.").format(row.idx))
        if amount > flt(receivable.outstanding_amount) + TOLERANCE:
            frappe.throw(_("Allocation on row {0} exceeds the current outstanding amount of {1}.").format(row.idx, frappe.format_value(receivable.outstanding_amount, {"fieldtype": "Currency"})))
        row.customer_order = receivable.customer_order
        row.due_date = receivable.due_date
        row.outstanding_before = receivable.outstanding_amount
        row.outstanding_after = max(flt(receivable.outstanding_amount) - amount, 0)
        allocated_total += amount

    if allocated_total > payment_total + TOLERANCE:
        frappe.throw(_("Total receivable allocation cannot exceed the independently encoded collection amount. Payment: {0}; allocated: {1}.").format(
            frappe.format_value(payment_total, {"fieldtype": "Currency"}),
            frappe.format_value(allocated_total, {"fieldtype": "Currency"}),
        ))
    doc.collection_amount = payment_total
    doc.total_allocated = allocated_total
    doc.unallocated_amount = max(payment_total - allocated_total, 0)
    doc.payment_fingerprint = _collection_fingerprint(doc.get("payments"), doc.get("referenced_customer_order"))


@frappe.whitelist()
def submit_encoder_allocation(allocation):
    doc = frappe.get_doc(ENCODER_ALLOCATION_DOCTYPE, allocation)
    if doc.status == "Matched" and doc.allocations_posted:
        return {"encoder_allocation": doc.name, "status": doc.status, "already_posted": True}
    if doc.status not in {"Draft", "Unmatched", "Ambiguous"}:
        frappe.throw(_("Encoder Allocation {0} cannot be submitted from status {1}.").format(doc.name, doc.status))
    doc.encoder = frappe.session.user
    doc.allocation_date = doc.allocation_date or today()
    doc.customer_name = frappe.db.get_value("Customer", doc.customer, "customer_name")
    _validate_referenced_customer_order(doc.customer, doc.get("referenced_customer_order"))
    _build_automatic_application(doc)
    _validate_encoder_allocations(doc)
    doc.status = "Unmatched"
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    _try_match_encoder_allocation(doc.name)
    return {
        "encoder_allocation": doc.name,
        "status": frappe.db.get_value(ENCODER_ALLOCATION_DOCTYPE, doc.name, "status"),
        "matched_cashier_collection": frappe.db.get_value(ENCODER_ALLOCATION_DOCTYPE, doc.name, "matched_cashier_collection"),
    }


def _cashier_candidates_for_encoder(doc):
    return frappe.get_all(
        CASHIER_COLLECTION_DOCTYPE,
        filters={
            "company": doc.company,
            "business_date": doc.allocation_date,
            "customer": doc.customer,
            "payment_fingerprint": doc.payment_fingerprint,
            "status": ["in", ["Submitted - Unmatched", "Ambiguous"]],
        },
        fields=["name", "cashier", "collection_datetime", "total_payment", "linked_payment_receipt"],
        order_by="collection_datetime asc, creation asc",
    )


def _encoder_candidates_for_cashier(doc):
    return frappe.get_all(
        ENCODER_ALLOCATION_DOCTYPE,
        filters={
            "company": doc.company,
            "allocation_date": doc.business_date,
            "customer": doc.customer,
            "payment_fingerprint": doc.payment_fingerprint,
            "status": ["in", ["Unmatched", "Ambiguous"]],
        },
        fields=["name", "encoder", "creation", "collection_amount"],
        order_by="creation asc",
    )


def _mark_ambiguous(cashier_names, encoder_names):
    warning = _("Several exact same-customer account collection candidates exist. Review the retained collection papers before linking.")
    for name in cashier_names:
        frappe.db.set_value(CASHIER_COLLECTION_DOCTYPE, name, {"status": "Ambiguous", "reconciliation_warning": warning}, update_modified=False)
    for name in encoder_names:
        frappe.db.set_value(ENCODER_ALLOCATION_DOCTYPE, name, {"status": "Ambiguous", "reconciliation_warning": warning}, update_modified=False)


def _try_match_cashier_collection(name):
    doc = frappe.get_doc(CASHIER_COLLECTION_DOCTYPE, name)
    if doc.status == "Matched":
        return
    candidates = _encoder_candidates_for_cashier(doc)
    if len(candidates) == 1:
        _complete_collection_match(doc.name, candidates[0].name)
    elif len(candidates) > 1:
        _mark_ambiguous([doc.name], [row.name for row in candidates])
    else:
        frappe.db.set_value(CASHIER_COLLECTION_DOCTYPE, doc.name, {"status": "Submitted - Unmatched", "reconciliation_warning": ""}, update_modified=False)


def _try_match_encoder_allocation(name):
    doc = frappe.get_doc(ENCODER_ALLOCATION_DOCTYPE, name)
    if doc.status == "Matched" and doc.allocations_posted:
        return
    candidates = _cashier_candidates_for_encoder(doc)
    if len(candidates) == 1:
        _complete_collection_match(candidates[0].name, doc.name)
    elif len(candidates) > 1:
        _mark_ambiguous([row.name for row in candidates], [doc.name])
    else:
        frappe.db.set_value(ENCODER_ALLOCATION_DOCTYPE, doc.name, {"status": "Unmatched", "reconciliation_warning": ""}, update_modified=False)


def _ensure_unapplied_customer_advance(receipt_name, cashier, encoder):
    amount = max(flt(encoder.get("unallocated_amount")), 0)
    if amount <= TOLERANCE:
        _set_if_field_value = {"customer_advance_amount": 0}
        meta = frappe.get_meta("NKT Payment Receipt")
        _set_if_field_value = {k: v for k, v in _set_if_field_value.items() if meta.has_field(k)}
        if _set_if_field_value:
            frappe.db.set_value("NKT Payment Receipt", receipt_name, _set_if_field_value, update_modified=False)
        return None

    receipt_meta = frappe.get_meta("NKT Payment Receipt")
    if receipt_meta.has_field("customer_advance_amount"):
        frappe.db.set_value("NKT Payment Receipt", receipt_name, "customer_advance_amount", amount, update_modified=False)

    existing = frappe.db.get_value(
        "NKT Customer Advance",
        {"source_payment_receipt": receipt_name, "docstatus": ["!=", 2]},
        ["name", "original_advance_amount"],
        as_dict=True,
    )
    if existing:
        if abs(flt(existing.original_advance_amount) - amount) > TOLERANCE:
            frappe.throw(_("Customer Advance {0} does not match the verified unapplied amount.").format(existing.name))
        return existing.name

    advance = frappe.get_doc({
        "doctype": "NKT Customer Advance",
        "company": cashier.company,
        "posting_datetime": cashier.collection_datetime or now_datetime(),
        "customer": cashier.customer,
        "customer_name": cashier.customer_name,
        "source_payment_receipt": receipt_name,
        "source_customer_order": cashier.get("referenced_customer_order"),
        "original_advance_amount": amount,
        "applied_amount": 0,
        "available_advance_amount": amount,
        "advance_status": "Available",
        "remarks": "Created from verified Customer Payment / Payment on Account unapplied balance.",
    })
    advance.flags.ignore_permissions = True
    advance.insert(ignore_permissions=True)
    advance.submit()
    return advance.name


def _apply_receivable_allocations(doc):
    if doc.allocations_posted:
        return
    now = now_datetime()
    affected_customers = set()
    for row in doc.allocations:
        frappe.db.sql(f"SELECT name FROM `tab{RECEIVABLE_DOCTYPE}` WHERE name = %s FOR UPDATE", row.receivable)
        receivable = frappe.get_doc(RECEIVABLE_DOCTYPE, row.receivable)
        amount = flt(row.allocated_amount)
        if amount > flt(receivable.outstanding_amount) + TOLERANCE:
            frappe.throw(_("Receivable {0} no longer has enough outstanding balance for this allocation.").format(receivable.name))
        new_paid = min(flt(receivable.amount_paid) + amount, flt(receivable.original_amount))
        new_outstanding = max(flt(receivable.original_amount) - new_paid, 0)
        new_status = "Paid" if new_outstanding <= TOLERANCE else "Partially Paid"
        collection_count = int(receivable.get("custom_nkt_collection_count") or 0) + 1
        frappe.db.set_value(
            RECEIVABLE_DOCTYPE,
            receivable.name,
            {
                "amount_paid": new_paid,
                "outstanding_amount": new_outstanding,
                "status": new_status,
                "custom_nkt_last_collection_on": now,
                "custom_nkt_collection_count": collection_count,
            },
            update_modified=False,
        )
        order = frappe.get_doc("NKT Customer Order", receivable.customer_order)
        frappe.db.set_value(
            "NKT Customer Order",
            order.name,
            {
                "amount_paid": max(flt(order.grand_total) - new_outstanding, 0),
                "amount_due": new_outstanding,
                "payment_status": "Paid" if new_outstanding <= TOLERANCE else "Partially Paid",
            },
            update_modified=False,
        )
        affected_customers.add(receivable.customer)

    from nkt_operations.nkt_store_operations.features.payments_accounts.credit import refresh_customer_credit

    for customer in affected_customers:
        refresh_customer_credit(customer)
    frappe.db.set_value(
        ENCODER_ALLOCATION_DOCTYPE,
        doc.name,
        {"allocations_posted": 1, "posted_on": now},
        update_modified=False,
    )


def _complete_collection_match(cashier_name, encoder_name, resolved_by=None, reason=None):
    frappe.db.sql(f"SELECT name FROM `tab{CASHIER_COLLECTION_DOCTYPE}` WHERE name = %s FOR UPDATE", cashier_name)
    frappe.db.sql(f"SELECT name FROM `tab{ENCODER_ALLOCATION_DOCTYPE}` WHERE name = %s FOR UPDATE", encoder_name)
    cashier = frappe.get_doc(CASHIER_COLLECTION_DOCTYPE, cashier_name)
    encoder = frappe.get_doc(ENCODER_ALLOCATION_DOCTYPE, encoder_name)

    if cashier.matched_encoder_allocation and cashier.matched_encoder_allocation != encoder.name:
        frappe.throw(_("Cashier Collection {0} is already matched to {1}.").format(cashier.name, cashier.matched_encoder_allocation))
    if encoder.matched_cashier_collection and encoder.matched_cashier_collection != cashier.name:
        frappe.throw(_("Encoder Allocation {0} is already matched to {1}.").format(encoder.name, encoder.matched_cashier_collection))
    if cashier.company != encoder.company or getdate(cashier.business_date) != getdate(encoder.allocation_date):
        frappe.throw(_("Cashier and encoder collection company/business date do not match."))
    if cashier.customer != encoder.customer:
        frappe.throw(_("Cashier and encoder collection Customer records do not match exactly."))
    if cashier.payment_fingerprint != encoder.payment_fingerprint:
        frappe.throw(_("Cashier and encoder collection payment details or specific account order references are not identical."))
    if (cashier.get("referenced_customer_order") or "") != (encoder.get("referenced_customer_order") or ""):
        frappe.throw(_("Cashier and encoder specific account order references do not match."))

    _validate_encoder_allocations(encoder)
    _apply_receivable_allocations(encoder)
    receipt_name = cashier.linked_payment_receipt or _ensure_payment_receipt(cashier)
    advance_name = _ensure_unapplied_customer_advance(receipt_name, cashier, encoder)
    now = now_datetime()
    cashier_values = {
        "status": "Matched",
        "matched_encoder_allocation": encoder.name,
        "reconciliation_warning": "",
        "matched_on": now,
    }
    if frappe.get_meta(CASHIER_COLLECTION_DOCTYPE).has_field("custom_nkt_application_status"):
        cashier_values["custom_nkt_application_status"] = "Active"
    frappe.db.set_value(
        CASHIER_COLLECTION_DOCTYPE,
        cashier.name,
        cashier_values,
        update_modified=False,
    )
    encoder_values = {
        "status": "Matched",
        "matched_cashier_collection": cashier.name,
        "linked_payment_receipt": receipt_name,
        "reconciliation_warning": "",
        "posted_on": now,
    }
    if frappe.get_meta(ENCODER_ALLOCATION_DOCTYPE).has_field("custom_nkt_application_status"):
        encoder_values["custom_nkt_application_status"] = "Active"
    if resolved_by:
        encoder_values.update({"resolved_by": resolved_by, "resolution_reason": reason})
    frappe.db.set_value(ENCODER_ALLOCATION_DOCTYPE, encoder.name, encoder_values, update_modified=False)
    receipt_values = {
        "allocation_status": "Account Collection",
        "custom_nkt_payment_kind": "Customer Account Collection",
        "custom_nkt_cashier_account_collection": cashier.name,
        "custom_nkt_encoder_account_allocation": encoder.name,
        # UI7C1B1: preserve the Cashier Collection's transaction-time account
        # envelope on the official receipt. Negative remaining balance represents
        # customer credit / unapplied advance and is intentional.
        "amount_due_before_receipt": flt(cashier.get("previous_balance")),
        "remaining_balance": flt(cashier.get("previous_balance")) - flt(cashier.get("total_payment")),
        "remarks": f"Verified Customer Account Collection {cashier.name}; matched Encoder Allocation {encoder.name}.",
    }
    existing_fields = frappe.get_meta("NKT Payment Receipt")
    # UI7C1B: Plate Number and physical OS# are Encoder-entered audit references.
    # They are copied to the already-existing official Payment Receipt only after
    # the normal Cashier/Encoder match has succeeded. They are deliberately not
    # read anywhere in the matching comparisons above.
    encoder_plate = str(encoder.get("custom_nkt_plate_number") or "").strip()
    encoder_os = str(encoder.get("custom_nkt_source_order_slip") or "").strip()
    if encoder_plate and existing_fields.has_field("custom_nkt_plate_number"):
        receipt_values["custom_nkt_plate_number"] = encoder_plate
    if encoder_os and existing_fields.has_field("custom_nkt_source_order_slip"):
        receipt_values["custom_nkt_source_order_slip"] = encoder_os
    receipt_values = {key: value for key, value in receipt_values.items() if existing_fields.has_field(key)}
    frappe.db.set_value("NKT Payment Receipt", receipt_name, receipt_values, update_modified=False)

    # UI7B6: preserve the accepted C5 directionality. A verified account payment
    # allocates to receivables that are already Approved at verification time.
    # Only the genuine remainder becomes Customer Advance. Do NOT run a broad
    # customer-wide sweep here: doing so can retroactively consume a later
    # Return Credit against an older already-approved receivable. Advance
    # auto-application remains tied to the relevant Account order becoming
    # eligible through its normal match / Credit Control approval gate.
    if advance_name:
        message = _("Cashier account collection {0} matched to encoder allocation {1}; existing Payment Receipt {2} allocated and unapplied balance stored in Customer Advance {3}. No second cashier movement created.").format(cashier.name, encoder.name, receipt_name, advance_name)
    else:
        message = _("Cashier account collection {0} matched to encoder allocation {1}; existing Payment Receipt {2} allocated. No second cashier movement created.").format(cashier.name, encoder.name, receipt_name)
    frappe.get_doc(CASHIER_COLLECTION_DOCTYPE, cashier.name).add_comment("Info", message)
    frappe.get_doc(ENCODER_ALLOCATION_DOCTYPE, encoder.name).add_comment("Info", message)


@frappe.whitelist()
def retry_collection_match(cashier_collection=None, encoder_allocation=None):
    if cashier_collection:
        _try_match_cashier_collection(cashier_collection)
        return frappe.db.get_value(CASHIER_COLLECTION_DOCTYPE, cashier_collection, ["status", "matched_encoder_allocation", "reconciliation_warning"], as_dict=True)
    if encoder_allocation:
        _try_match_encoder_allocation(encoder_allocation)
        return frappe.db.get_value(ENCODER_ALLOCATION_DOCTYPE, encoder_allocation, ["status", "matched_cashier_collection", "reconciliation_warning"], as_dict=True)
    frappe.throw(_("Specify a cashier collection or encoder allocation."))


@frappe.whitelist()
def get_collection_candidates(encoder_allocation):
    doc = frappe.get_doc(ENCODER_ALLOCATION_DOCTYPE, encoder_allocation)
    return _cashier_candidates_for_encoder(doc)



# V2.0C.5.2 CUSTOMER ADVANCE CONSUMPTION

def _customer_available_advance(customer, company=None):
    filters = {
        "customer": customer,
        "docstatus": 1,
        "advance_status": ["in", ["Available", "Partially Used"]],
        "available_advance_amount": [">", TOLERANCE],
    }
    if company:
        filters["company"] = company
    return frappe.get_all(
        "NKT Customer Advance",
        filters=filters,
        fields=[
            "name", "company", "posting_datetime", "customer",
            "source_payment_receipt", "available_advance_amount",
            "applied_amount", "original_advance_amount", "creation",
        ],
        order_by="posting_datetime asc, creation asc, name asc",
    )


@frappe.whitelist()
def get_customer_advance_balance(customer, company=None):
    rows = _customer_available_advance(customer, company)
    return {
        "customer": customer,
        "available_advance": sum(flt(row.available_advance_amount) for row in rows),
        "advances": rows,
    }


def _set_advance_usage(advance_name, amount):
    frappe.db.sql(
        "SELECT name FROM `tabNKT Customer Advance` WHERE name=%s FOR UPDATE",
        advance_name,
    )
    advance = frappe.get_doc("NKT Customer Advance", advance_name)
    available = flt(advance.available_advance_amount)
    use = min(max(flt(amount), 0), available)
    if use <= TOLERANCE:
        return 0

    applied = flt(advance.applied_amount) + use
    remaining = max(flt(advance.original_advance_amount) - applied, 0)
    status = "Fully Used" if remaining <= TOLERANCE else "Partially Used"

    frappe.db.set_value(
        "NKT Customer Advance",
        advance.name,
        {
            "applied_amount": applied,
            "available_advance_amount": remaining,
            "advance_status": status,
        },
        update_modified=False,
    )
    return use


def _record_advance_application(order, advance, amount, remarks=None):
    app = frappe.get_doc({
        "doctype": "NKT Customer Advance Application",
        "company": order.company,
        "posting_datetime": now_datetime(),
        "customer": order.customer,
        "customer_name": frappe.db.get_value("Customer", order.customer, "customer_name"),
        "customer_advance": advance.name,
        "source_payment_receipt": advance.source_payment_receipt,
        "custom_nkt_source_return_exchange": (
            advance.get("custom_nkt_source_return_exchange")
            if frappe.get_meta("NKT Customer Advance Application").has_field("custom_nkt_source_return_exchange")
            else None
        ),
        "customer_order": order.name,
        "applied_amount": amount,
        "application_status": "Applied",
        "applied_by": frappe.session.user,
        "remarks": remarks or "Applied existing verified customer advance to Customer Order.",
    })
    app.flags.ignore_permissions = True
    app.insert(ignore_permissions=True)
    app.submit()
    return app.name


def _reduce_order_receivable_from_advance(order, amount):
    receivable_name = frappe.db.get_value(
        RECEIVABLE_DOCTYPE,
        {
            "customer_order": order.name,
            "customer": order.customer,
            "status": ["in", list(ACTIVE_RECEIVABLE_STATUSES)],
            "credit_control_status": "Approved",
        },
        "name",
    )
    if not receivable_name:
        return None

    frappe.db.sql(
        f"SELECT name FROM `tab{RECEIVABLE_DOCTYPE}` WHERE name=%s FOR UPDATE",
        receivable_name,
    )
    receivable = frappe.get_doc(RECEIVABLE_DOCTYPE, receivable_name)
    use = min(flt(amount), flt(receivable.outstanding_amount))
    if use <= TOLERANCE:
        return receivable.name

    new_paid = min(flt(receivable.amount_paid) + use, flt(receivable.original_amount))
    new_outstanding = max(flt(receivable.original_amount) - new_paid, 0)

    frappe.db.set_value(
        RECEIVABLE_DOCTYPE,
        receivable.name,
        {
            "amount_paid": new_paid,
            "outstanding_amount": new_outstanding,
            "status": "Paid" if new_outstanding <= TOLERANCE else "Partially Paid",
            "custom_nkt_last_collection_on": now_datetime(),
            "custom_nkt_collection_count": int(receivable.get("custom_nkt_collection_count") or 0) + 1,
        },
        update_modified=False,
    )

    from nkt_operations.nkt_store_operations.features.payments_accounts.credit import refresh_customer_credit
    refresh_customer_credit(order.customer)
    return receivable.name


def _update_order_after_advance(order, amount):
    current_paid = max(flt(order.get("amount_paid")), 0)
    current_due = max(flt(order.get("amount_due")), 0)

    if current_due <= TOLERANCE and (order.get("payment_status") or "") not in {"Paid", "Charged to Account"}:
        current_due = max(flt(order.grand_total) - current_paid, 0)

    new_due = max(current_due - flt(amount), 0)
    new_paid = min(flt(order.grand_total), current_paid + flt(amount))

    current_status = order.get("status") or ""
    protected = {
        "Released",
        "Partially Released",
        "Pending Admin Confirmation",
        "Pending Credit Control",
    }

    if new_due <= TOLERANCE:
        payment_status = "Paid"
        order_status = current_status if current_status in protected else "Ready for Release"
    else:
        payment_status = "Partially Paid"
        order_status = current_status if current_status in protected else "Partially Paid"

    frappe.db.set_value(
        "NKT Customer Order",
        order.name,
        {
            "amount_paid": new_paid,
            "amount_due": new_due,
            "payment_status": payment_status,
            "status": order_status,
        },
        update_modified=False,
    )


@frappe.whitelist(methods=["POST"])
def apply_customer_advance_to_order(customer_order, amount=None, remarks=None):
    # Uses already-received money. Never creates a new Payment Receipt or Cashier Movement.
    _ensure_customer_advance_application_doctype()

    frappe.db.sql(
        "SELECT name FROM `tabNKT Customer Order` WHERE name=%s FOR UPDATE",
        customer_order,
    )
    order = frappe.get_doc("NKT Customer Order", customer_order)

    # V2.0C.5.2.1 manual Credit Control gate
    if flt(order.get("declared_account")) > TOLERANCE:
        credit_status = (order.get("custom_nkt_account_credit_status") or "").strip()
        if credit_status and credit_status != "Approved":
            frappe.throw(
                _(
                    "This Account sale is still awaiting Credit Control. "
                    "Customer Advance will be applied automatically after approval."
                )
            )

    if order.docstatus != 1:
        frappe.throw(
            _("Customer Order {0} must be submitted before applying customer advance.").format(order.name)
        )

    due = max(flt(order.get("amount_due")), 0)
    if due <= TOLERANCE:
        frappe.throw(_("Customer Order {0} has no remaining amount due.").format(order.name))

    requested = due if amount in (None, "") else max(flt(amount), 0)
    if requested <= TOLERANCE:
        frappe.throw(_("Advance application amount must be greater than zero."))

    requested = min(requested, due)
    approved_receivable_outstanding = None
    if flt(order.get("declared_account")) > TOLERANCE:
        receivable_row = frappe.db.get_value(
            RECEIVABLE_DOCTYPE,
            {
                "customer_order": order.name,
                "customer": order.customer,
                "status": ["in", list(ACTIVE_RECEIVABLE_STATUSES)],
                "credit_control_status": "Approved",
            },
            ["name", "outstanding_amount"],
            as_dict=True,
        )
        if not receivable_row or flt(receivable_row.outstanding_amount) <= TOLERANCE:
            frappe.throw(
                _("Customer Order {0} has no approved open receivable available for Customer Advance application.").format(order.name)
            )
        approved_receivable_outstanding = max(flt(receivable_row.outstanding_amount), 0)
        requested = min(requested, approved_receivable_outstanding)

    advances = _customer_available_advance(order.customer, order.company)
    available_total = sum(flt(row.available_advance_amount) for row in advances)

    if available_total <= TOLERANCE:
        frappe.throw(_("Customer {0} has no available customer advance.").format(order.customer))

    target = min(requested, available_total, due)
    if approved_receivable_outstanding is not None:
        target = min(target, approved_receivable_outstanding)
    remaining = target
    applications = []

    for row in advances:
        if remaining <= TOLERANCE:
            break

        use = _set_advance_usage(row.name, remaining)
        if use <= TOLERANCE:
            continue

        app_name = _record_advance_application(order, row, use, remarks)
        applications.append({
            "application": app_name,
            "customer_advance": row.name,
            "source_payment_receipt": row.source_payment_receipt,
            "applied_amount": use,
        })
        remaining -= use

    applied = target - remaining
    if applied <= TOLERANCE:
        frappe.throw(_("No customer advance could be applied."))

    receivable = _reduce_order_receivable_from_advance(order, applied)
    _update_order_after_advance(order, applied)

    return {
        "customer_order": order.name,
        "customer": order.customer,
        "applied_amount": applied,
        "remaining_order_due": max(due - applied, 0),
        "available_advance_before": available_total,
        "available_advance_after": max(available_total - applied, 0),
        "approved_receivable_outstanding_before": approved_receivable_outstanding,
        "receivable": receivable,
        "applications": applications,
        "new_payment_receipt_created": False,
        "new_cashier_movement_created": False,
    }


def _is_direct_authority(user):
    if user == "Administrator":
        return True
    return bool(set(frappe.get_roles(user)).intersection(DIRECT_AUTHORITY_ROLES))


def _get_authorizing_user(authorized_user=None, authorized_password=None):
    requester = frappe.session.user
    if _is_direct_authority(requester):
        return requester
    if not authorized_user or not authorized_password:
        frappe.throw(_("Authorized credentials are required to resolve an ambiguous collection match."))
    if not _is_direct_authority(authorized_user):
        frappe.throw(_("User {0} does not have authority to resolve collection matches.").format(authorized_user))
    check_password(authorized_user, authorized_password)
    return authorized_user


@frappe.whitelist()
def resolve_collection_match(encoder_allocation, cashier_collection, reason, authorized_user=None, authorized_password=None):
    reason = (reason or "").strip()
    if not reason:
        frappe.throw(_("Resolution Reason is required."))
    approver = _get_authorizing_user(authorized_user, authorized_password)
    encoder = frappe.get_doc(ENCODER_ALLOCATION_DOCTYPE, encoder_allocation)
    candidate_names = {row.name for row in _cashier_candidates_for_encoder(encoder)}
    if cashier_collection not in candidate_names:
        frappe.throw(_("Selected Cashier Collection is not an exact available candidate."))
    _complete_collection_match(cashier_collection, encoder_allocation, resolved_by=approver, reason=reason)
    # Other exact candidates remain available for their real encoder paper.
    for name in candidate_names - {cashier_collection}:
        frappe.db.set_value(CASHIER_COLLECTION_DOCTYPE, name, {"status": "Submitted - Unmatched", "reconciliation_warning": ""}, update_modified=False)
    return {
        "encoder_allocation": encoder_allocation,
        "cashier_collection": cashier_collection,
        "resolved_by": approver,
    }


@frappe.whitelist()
def repair_collection_pair(cashier_collection, encoder_allocation=None):
    """Idempotent helper for a submitted test pair."""
    if encoder_allocation:
        _complete_collection_match(cashier_collection, encoder_allocation)
    else:
        _try_match_cashier_collection(cashier_collection)
    return {
        "cashier": frappe.db.get_value(CASHIER_COLLECTION_DOCTYPE, cashier_collection, ["status", "matched_encoder_allocation", "linked_payment_receipt"], as_dict=True),
        "encoder": frappe.db.get_value(ENCODER_ALLOCATION_DOCTYPE, encoder_allocation, ["status", "matched_cashier_collection", "allocations_posted"], as_dict=True) if encoder_allocation else None,
    }
