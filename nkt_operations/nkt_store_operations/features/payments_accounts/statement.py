from __future__ import annotations

import json
from datetime import datetime, time

import frappe
from frappe import _
from frappe.utils import cint, flt, get_first_day, getdate, now_datetime, today


STATEMENT_DOCTYPE = "NKT Customer Statement"
STATEMENT_LINE_DOCTYPE = "NKT Customer Statement Line"
STATEMENT_PRINT_FORMAT = "NKT Customer Statement of Account"
PAYMENT_RECEIPT_PRINT_FORMAT = "NKT Account Payment Receipt"
STATEMENT_CLIENT_SCRIPT = "NKT Customer Statement V1.7"
CUSTOMER_CLIENT_SCRIPT = "NKT Customer Account History V1.7"
PAYMENT_RECEIPT_CLIENT_SCRIPT = "NKT Account Payment Receipt Button V1.7"
RECEIVABLE_DOCTYPE = "NKT Customer Receivable"
PAYMENT_VERIFICATION_DOCTYPE = "NKT Encoder Account Allocation"
ALLOCATION_ROW_DOCTYPE = "NKT Account Allocation Row"
CASHIER_COLLECTION_DOCTYPE = "NKT Cashier Account Collection"
TOLERANCE = 0.005


def install_schema():
    required = [
        RECEIVABLE_DOCTYPE,
        PAYMENT_VERIFICATION_DOCTYPE,
        ALLOCATION_ROW_DOCTYPE,
        CASHIER_COLLECTION_DOCTYPE,
    ]
    missing = [doctype for doctype in required if not frappe.db.exists("DocType", doctype)]
    if missing:
        frappe.throw(
            _("Install the earlier NKT account-credit and account-collection patches first. Missing: {0}").format(
                ", ".join(missing)
            )
        )

    _ensure_statement_line_doctype()
    _ensure_statement_doctype()
    _ensure_print_format(
        STATEMENT_PRINT_FORMAT,
        STATEMENT_DOCTYPE,
        _statement_print_html(),
    )
    _ensure_print_format(
        PAYMENT_RECEIPT_PRINT_FORMAT,
        CASHIER_COLLECTION_DOCTYPE,
        _payment_receipt_print_html(),
    )
    _ensure_client_script(
        STATEMENT_CLIENT_SCRIPT,
        STATEMENT_DOCTYPE,
        _statement_client_script(),
    )
    _ensure_client_script(
        CUSTOMER_CLIENT_SCRIPT,
        "Customer",
        _customer_client_script(),
    )
    _ensure_client_script(
        PAYMENT_RECEIPT_CLIENT_SCRIPT,
        CASHIER_COLLECTION_DOCTYPE,
        _cashier_print_client_script(),
    )
    _set_default_print_format(STATEMENT_DOCTYPE, STATEMENT_PRINT_FORMAT)
    _set_default_print_format(CASHIER_COLLECTION_DOCTYPE, PAYMENT_RECEIPT_PRINT_FORMAT)

    for doctype in (
        STATEMENT_LINE_DOCTYPE,
        STATEMENT_DOCTYPE,
        CASHIER_COLLECTION_DOCTYPE,
        "Customer",
    ):
        frappe.clear_cache(doctype=doctype)
    frappe.cache.delete_key("bootinfo")

    return {
        "installed": True,
        "statement_doctype": STATEMENT_DOCTYPE,
        "statement_print_format": STATEMENT_PRINT_FORMAT,
        "payment_receipt_print_format": PAYMENT_RECEIPT_PRINT_FORMAT,
    }


def _base_permissions():
    full_roles = [
        "System Manager",
        "NKT OWNER",
        "NKT ADMINISTRATOR",
        "NKT Credit Controller",
        "NKT Encoder",
    ]
    permissions = []
    for role in full_roles:
        permissions.append(
            {
                "role": role,
                "read": 1,
                "write": 1,
                "create": 1,
                "delete": 1 if role in {"System Manager", "NKT OWNER", "NKT ADMINISTRATOR"} else 0,
                "report": 1,
                "export": 1,
                "print": 1,
                "email": 1,
                "share": 1 if role in {"System Manager", "NKT OWNER", "NKT ADMINISTRATOR"} else 0,
            }
        )
    permissions.append(
        {
            "role": "NKT Cashier",
            "read": 1,
            "report": 1,
            "print": 1,
        }
    )
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


def _ensure_statement_line_doctype():
    fields = [
        {
            "fieldname": "posting_date",
            "label": "Date",
            "fieldtype": "Date",
            "reqd": 1,
            "in_list_view": 1,
            "columns": 1,
        },
        {
            "fieldname": "entry_type",
            "label": "Entry Type",
            "fieldtype": "Select",
            "options": "Account Sale\nAccount Payment",
            "reqd": 1,
            "in_list_view": 1,
            "columns": 1,
        },
        {
            "fieldname": "reference_doctype",
            "label": "Reference Type",
            "fieldtype": "Link",
            "options": "DocType",
            "read_only": 1,
        },
        {
            "fieldname": "reference_name",
            "label": "Reference",
            "fieldtype": "Dynamic Link",
            "options": "reference_doctype",
            "read_only": 1,
            "in_list_view": 1,
            "columns": 2,
        },
        {
            "fieldname": "customer_order",
            "label": "Customer Order",
            "fieldtype": "Link",
            "options": "NKT Customer Order",
            "read_only": 1,
            "in_list_view": 1,
            "columns": 2,
        },
        {
            "fieldname": "due_date",
            "label": "Due Date",
            "fieldtype": "Date",
            "read_only": 1,
            "in_list_view": 1,
            "columns": 1,
        },
        {
            "fieldname": "description",
            "label": "Description",
            "fieldtype": "Small Text",
            "read_only": 1,
            "in_list_view": 1,
            "columns": 2,
        },
        {
            "fieldname": "debit",
            "label": "Charge",
            "fieldtype": "Currency",
            "read_only": 1,
            "in_list_view": 1,
            "columns": 1,
        },
        {
            "fieldname": "credit",
            "label": "Payment",
            "fieldtype": "Currency",
            "read_only": 1,
            "in_list_view": 1,
            "columns": 1,
        },
        {
            "fieldname": "running_balance",
            "label": "Balance",
            "fieldtype": "Currency",
            "read_only": 1,
            "in_list_view": 1,
            "columns": 1,
        },
        {
            "fieldname": "days_overdue",
            "label": "Days Overdue",
            "fieldtype": "Int",
            "read_only": 1,
        },
        {
            "fieldname": "aging_bucket",
            "label": "Aging Bucket",
            "fieldtype": "Data",
            "read_only": 1,
        },
    ]
    _ensure_custom_doctype(STATEMENT_LINE_DOCTYPE, None, fields, [], istable=True)


def _ensure_statement_doctype():
    fields = [
        {"fieldname": "statement_details", "label": "Statement Details", "fieldtype": "Section Break"},
        {
            "fieldname": "company",
            "label": "Company",
            "fieldtype": "Link",
            "options": "Company",
            "reqd": 1,
            "in_list_view": 1,
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
            "fieldname": "currency",
            "label": "Currency",
            "fieldtype": "Link",
            "options": "Currency",
            "read_only": 1,
        },
        {
            "fieldname": "from_date",
            "label": "From Date",
            "fieldtype": "Date",
            "reqd": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "to_date",
            "label": "To Date / Aging Date",
            "fieldtype": "Date",
            "reqd": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "status",
            "label": "Statement Status",
            "fieldtype": "Select",
            "options": "Draft\nGenerated",
            "default": "Draft",
            "read_only": 1,
            "in_list_view": 1,
        },
        {"fieldname": "summary_section", "label": "Statement Summary", "fieldtype": "Section Break"},
        {
            "fieldname": "opening_balance",
            "label": "Opening Balance",
            "fieldtype": "Currency",
            "read_only": 1,
        },
        {
            "fieldname": "period_charges",
            "label": "Charges During Period",
            "fieldtype": "Currency",
            "read_only": 1,
        },
        {
            "fieldname": "period_payments",
            "label": "Payments During Period",
            "fieldtype": "Currency",
            "read_only": 1,
        },
        {
            "fieldname": "closing_balance",
            "label": "Closing Balance",
            "fieldtype": "Currency",
            "read_only": 1,
        },
        {"fieldname": "aging_section", "label": "Aging as of To Date", "fieldtype": "Section Break"},
        {
            "fieldname": "aging_current",
            "label": "Current / Not Yet Due",
            "fieldtype": "Currency",
            "read_only": 1,
        },
        {
            "fieldname": "aging_1_30",
            "label": "1–30 Days Overdue",
            "fieldtype": "Currency",
            "read_only": 1,
        },
        {
            "fieldname": "aging_31_60",
            "label": "31–60 Days Overdue",
            "fieldtype": "Currency",
            "read_only": 1,
        },
        {
            "fieldname": "aging_61_90",
            "label": "61–90 Days Overdue",
            "fieldtype": "Currency",
            "read_only": 1,
        },
        {
            "fieldname": "aging_over_90",
            "label": "Over 90 Days Overdue",
            "fieldtype": "Currency",
            "read_only": 1,
        },
        {"fieldname": "transactions_section", "label": "Account History", "fieldtype": "Section Break"},
        {
            "fieldname": "lines",
            "label": "Statement Lines",
            "fieldtype": "Table",
            "options": STATEMENT_LINE_DOCTYPE,
            "read_only": 1,
        },
        {"fieldname": "generation_section", "label": "Generation Audit", "fieldtype": "Section Break"},
        {
            "fieldname": "generated_by",
            "label": "Generated By",
            "fieldtype": "Link",
            "options": "User",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "generated_on",
            "label": "Generated On",
            "fieldtype": "Datetime",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "notes",
            "label": "Statement Notes",
            "fieldtype": "Small Text",
        },
    ]
    _ensure_custom_doctype(
        STATEMENT_DOCTYPE,
        "NKT-SOA-.YYYY.-.#####",
        fields,
        _base_permissions(),
    )


def _ensure_print_format(name: str, doc_type: str, html: str):
    if frappe.db.exists("Print Format", name):
        doc = frappe.get_doc("Print Format", name)
    else:
        doc = frappe.new_doc("Print Format")
        doc.name = name

    values = {
        "doc_type": doc_type,
        "module": "NKT Store Operations",
        "standard": "No",
        "custom_format": 1,
        "print_format_type": "Jinja",
        "html": html,
        "disabled": 0,
    }
    meta = frappe.get_meta("Print Format")
    for fieldname, value in values.items():
        if fieldname == "doc_type" or meta.has_field(fieldname):
            doc.set(fieldname, value)
    doc.flags.ignore_permissions = True
    if doc.is_new():
        doc.insert(ignore_permissions=True)
    else:
        doc.save(ignore_permissions=True)



def _set_default_print_format(doctype: str, print_format: str):
    meta = frappe.get_meta("DocType")
    if meta.has_field("default_print_format") and frappe.db.exists("DocType", doctype):
        frappe.db.set_value(
            "DocType",
            doctype,
            "default_print_format",
            print_format,
            update_modified=False,
        )


def _ensure_client_script(name: str, dt: str, script: str):
    exists = bool(frappe.db.exists("Client Script", name))
    if exists:
        doc = frappe.get_doc("Client Script", name)
    else:
        # In Frappe v16, a named object made through frappe.get_doc({...})
        # may not be marked as a new/local document. Use new_doc so insert()
        # creates the database row instead of save() trying to reload it.
        doc = frappe.new_doc("Client Script")
        doc.name = name

    doc.dt = dt
    doc.view = "Form"
    doc.enabled = 1
    doc.script = script
    doc.flags.ignore_permissions = True

    if exists:
        doc.save(ignore_permissions=True)
    else:
        doc.insert(ignore_permissions=True)


def _date_start(value):
    return datetime.combine(getdate(value), time.min)


def _date_end(value):
    return datetime.combine(getdate(value), time.max)


def _load_receivables(company: str, customer: str, to_date_value):
    return frappe.db.sql(
        """
        SELECT
            name,
            customer_order,
            posting_date,
            due_date,
            original_amount,
            status,
            credit_control_status,
            creation
        FROM `tabNKT Customer Receivable`
        WHERE company=%s
          AND customer=%s
          AND posting_date <= %s
          AND IFNULL(credit_control_status, '')='Approved'
          AND IFNULL(status, '')!='Cancelled'
        ORDER BY posting_date, creation, name
        """,
        (company, customer, getdate(to_date_value)),
        as_dict=True,
    )


def _load_allocations(company: str, customer: str, to_date_value):
    reversal_filter = ""
    if frappe.db.exists("DocType", ALLOCATION_ROW_DOCTYPE) and frappe.get_meta(ALLOCATION_ROW_DOCTYPE).has_field(
        "custom_nkt_is_reversed"
    ):
        reversal_filter = " AND IFNULL(row.custom_nkt_is_reversed, 0)=0"

    allocations = frappe.db.sql(
        """
        SELECT
            row.parent AS payment_verification,
            row.idx,
            row.receivable,
            row.customer_order,
            row.allocated_amount,
            verification.allocation_date,
            verification.posted_on,
            verification.linked_payment_receipt,
            verification.matched_cashier_collection,
            verification.creation
        FROM `tabNKT Account Allocation Row` row
        INNER JOIN `tabNKT Encoder Account Allocation` verification
            ON verification.name=row.parent
        WHERE verification.company=%s
          AND verification.customer=%s
          AND verification.allocation_date <= %s
          AND IFNULL(verification.allocations_posted, 0)=1
          AND verification.status='Matched'
          {reversal_filter}
        """.format(reversal_filter=reversal_filter),
        (company, customer, getdate(to_date_value)),
        as_dict=True,
    )

    correction_doctype = "NKT Account Payment Correction"
    correction_row_doctype = "NKT Account Payment Correction Allocation"
    if frappe.db.exists("DocType", correction_doctype) and frappe.db.exists("DocType", correction_row_doctype):
        allocations.extend(
            frappe.db.sql(
                """
                SELECT
                    correction.payment_verification,
                    row.idx,
                    row.receivable,
                    row.customer_order,
                    row.allocated_amount,
                    correction.original_allocation_date AS allocation_date,
                    correction.applied_on AS posted_on,
                    correction.linked_payment_receipt,
                    correction.cashier_collection AS matched_cashier_collection,
                    correction.creation
                FROM `tabNKT Account Payment Correction Allocation` row
                INNER JOIN `tabNKT Account Payment Correction` correction
                    ON correction.name=row.parent
                WHERE correction.company=%s
                  AND correction.customer=%s
                  AND correction.original_allocation_date <= %s
                  AND correction.status='Applied'
                  AND row.allocation_kind='New'
                  AND IFNULL(row.is_applied, 0)=1
                """,
                (company, customer, getdate(to_date_value)),
                as_dict=True,
            )
        )

    allocations.sort(
        key=lambda row: (
            getdate(row.allocation_date),
            row.posted_on or row.creation or _date_start(row.allocation_date),
            row.idx or 0,
            row.receivable or "",
        )
    )
    return allocations


def _bucket_for(due_date, as_of_date):
    if not due_date:
        return "Current", 0
    days = (getdate(as_of_date) - getdate(due_date)).days
    if days <= 0:
        return "Current", 0
    if days <= 30:
        return "1–30", days
    if days <= 60:
        return "31–60", days
    if days <= 90:
        return "61–90", days
    return "Over 90", days


def _as_float(value):
    value = flt(value)
    return 0.0 if abs(value) <= TOLERANCE else value


def _c5_3_original_build_statement(company: str, customer: str, from_date_value, to_date_value):
    from_date_value = getdate(from_date_value)
    to_date_value = getdate(to_date_value)
    if from_date_value > to_date_value:
        frappe.throw(_("From Date cannot be after To Date."))
    if not frappe.db.exists("Customer", customer):
        frappe.throw(_("Customer {0} does not exist.").format(customer))
    if not frappe.db.exists("Company", company):
        frappe.throw(_("Company {0} does not exist.").format(company))

    receivables = _load_receivables(company, customer, to_date_value)
    allocations = _load_allocations(company, customer, to_date_value)
    receivable_map = {row.name: row for row in receivables}

    events = []
    opening_balance = 0.0
    period_charges = 0.0
    period_payments = 0.0

    for receivable in receivables:
        amount = _as_float(receivable.original_amount)
        event_datetime = receivable.creation or _date_start(receivable.posting_date)
        if getdate(receivable.posting_date) < from_date_value:
            opening_balance += amount
        elif from_date_value <= getdate(receivable.posting_date) <= to_date_value:
            period_charges += amount
            events.append(
                {
                    "posting_date": getdate(receivable.posting_date),
                    "sort_datetime": event_datetime,
                    "sort_order": 0,
                    "entry_type": "Account Sale",
                    "reference_doctype": RECEIVABLE_DOCTYPE,
                    "reference_name": receivable.name,
                    "customer_order": receivable.customer_order,
                    "due_date": receivable.due_date,
                    "description": "Account sale {0}".format(receivable.customer_order or receivable.name),
                    "debit": amount,
                    "credit": 0.0,
                }
            )

    allocated_by_receivable = {}
    for allocation in allocations:
        amount = _as_float(allocation.allocated_amount)
        allocation_date = getdate(allocation.allocation_date)
        allocated_by_receivable[allocation.receivable] = (
            allocated_by_receivable.get(allocation.receivable, 0.0) + amount
        )
        if allocation_date < from_date_value:
            opening_balance -= amount
        elif from_date_value <= allocation_date <= to_date_value:
            period_payments += amount
            receivable = receivable_map.get(allocation.receivable)
            due_date = receivable.due_date if receivable else None
            reference_name = allocation.linked_payment_receipt or allocation.payment_verification
            reference_doctype = (
                "NKT Payment Receipt" if allocation.linked_payment_receipt else PAYMENT_VERIFICATION_DOCTYPE
            )
            event_datetime = allocation.posted_on or allocation.creation or _date_start(allocation_date)
            events.append(
                {
                    "posting_date": allocation_date,
                    "sort_datetime": event_datetime,
                    "sort_order": 1,
                    "entry_type": "Account Payment",
                    "reference_doctype": reference_doctype,
                    "reference_name": reference_name,
                    "customer_order": allocation.customer_order,
                    "due_date": due_date,
                    "description": "Payment applied to {0}".format(
                        allocation.customer_order or allocation.receivable
                    ),
                    "debit": 0.0,
                    "credit": amount,
                }
            )

    events.sort(
        key=lambda row: (
            getdate(row["posting_date"]),
            row.get("sort_datetime") or _date_start(row["posting_date"]),
            row.get("sort_order", 0),
            row.get("reference_name") or "",
        )
    )

    running_balance = _as_float(opening_balance)
    lines = []
    for event in events:
        running_balance = _as_float(running_balance + event["debit"] - event["credit"])
        bucket, days_overdue = _bucket_for(event.get("due_date"), to_date_value)
        lines.append(
            {
                "posting_date": event["posting_date"],
                "entry_type": event["entry_type"],
                "reference_doctype": event["reference_doctype"],
                "reference_name": event["reference_name"],
                "customer_order": event.get("customer_order"),
                "due_date": event.get("due_date"),
                "description": event["description"],
                "debit": _as_float(event["debit"]),
                "credit": _as_float(event["credit"]),
                "running_balance": running_balance,
                "days_overdue": days_overdue,
                "aging_bucket": bucket,
            }
        )

    aging = {
        "aging_current": 0.0,
        "aging_1_30": 0.0,
        "aging_31_60": 0.0,
        "aging_61_90": 0.0,
        "aging_over_90": 0.0,
    }
    for receivable in receivables:
        outstanding_as_of = _as_float(
            flt(receivable.original_amount) - flt(allocated_by_receivable.get(receivable.name, 0.0))
        )
        if outstanding_as_of <= TOLERANCE:
            continue
        bucket, _days = _bucket_for(receivable.due_date, to_date_value)
        if bucket == "Current":
            aging["aging_current"] += outstanding_as_of
        elif bucket == "1–30":
            aging["aging_1_30"] += outstanding_as_of
        elif bucket == "31–60":
            aging["aging_31_60"] += outstanding_as_of
        elif bucket == "61–90":
            aging["aging_61_90"] += outstanding_as_of
        else:
            aging["aging_over_90"] += outstanding_as_of

    closing_balance = _as_float(opening_balance + period_charges - period_payments)
    aged_total = _as_float(sum(aging.values()))
    if abs(closing_balance - aged_total) > TOLERANCE:
        frappe.log_error(
            title="NKT SOA aging mismatch",
            message=json.dumps(
                {
                    "company": company,
                    "customer": customer,
                    "from_date": str(from_date_value),
                    "to_date": str(to_date_value),
                    "closing_balance": closing_balance,
                    "aged_total": aged_total,
                },
                indent=2,
            ),
        )

    return {
        "company": company,
        "customer": customer,
        "customer_name": frappe.db.get_value("Customer", customer, "customer_name") or customer,
        "currency": frappe.db.get_value("Company", company, "default_currency"),
        "from_date": from_date_value,
        "to_date": to_date_value,
        "opening_balance": _as_float(opening_balance),
        "period_charges": _as_float(period_charges),
        "period_payments": _as_float(period_payments),
        "closing_balance": closing_balance,
        **{key: _as_float(value) for key, value in aging.items()},
        "lines": lines,
        "generated_by": frappe.session.user,
        "generated_on": now_datetime(),
        "status": "Generated",
    }


# V2.0C.5.3 ADVANCE-AWARE STATEMENT WRAPPER
def _build_statement(
    company: str,
    customer: str,
    from_date_value,
    to_date_value,
):
    data = _c5_3_original_build_statement(
        company,
        customer,
        from_date_value,
        to_date_value,
    )

    from nkt_operations.nkt_store_operations.features.payments_accounts.internal.advance_statement import (
        augment_statement,
    )

    return augment_statement(
        data,
        company,
        customer,
        from_date_value,
        to_date_value,
    )



@frappe.whitelist()
def get_statement_data(company, customer, from_date=None, to_date=None):
    to_date = to_date or today()
    from_date = from_date or get_first_day(to_date)
    return _build_statement(company, customer, from_date, to_date)


@frappe.whitelist()
def create_statement(company, customer, from_date=None, to_date=None, notes=None):
    data = get_statement_data(company, customer, from_date, to_date)
    doc = frappe.get_doc(
        {
            "doctype": STATEMENT_DOCTYPE,
            "company": data["company"],
            "customer": data["customer"],
            "customer_name": data["customer_name"],
            "currency": data["currency"],
            "from_date": data["from_date"],
            "to_date": data["to_date"],
            "status": data["status"],
            "opening_balance": data["opening_balance"],
            "period_charges": data["period_charges"],
            "period_payments": data["period_payments"],
            "closing_balance": data["closing_balance"],
            "aging_current": data["aging_current"],
            "aging_1_30": data["aging_1_30"],
            "aging_31_60": data["aging_31_60"],
            "aging_61_90": data["aging_61_90"],
            "aging_over_90": data["aging_over_90"],
            "generated_by": data["generated_by"],
            "generated_on": data["generated_on"],
            "notes": notes,
            "lines": data["lines"],
        }
    )
    doc.insert()
    return {"name": doc.name, "closing_balance": data["closing_balance"]}


@frappe.whitelist()
def refresh_statement(statement_name):
    doc = frappe.get_doc(STATEMENT_DOCTYPE, statement_name)
    doc.check_permission("write")
    data = get_statement_data(doc.company, doc.customer, doc.from_date, doc.to_date)
    for fieldname in (
        "customer_name",
        "currency",
        "opening_balance",
        "period_charges",
        "period_payments",
        "closing_balance",
        "aging_current",
        "aging_1_30",
        "aging_31_60",
        "aging_61_90",
        "aging_over_90",
        "generated_by",
        "generated_on",
        "status",
    ):
        doc.set(fieldname, data[fieldname])
    doc.set("lines", [])
    for row in data["lines"]:
        doc.append("lines", row)
    doc.save()
    return {"name": doc.name, "closing_balance": data["closing_balance"]}


@frappe.whitelist()
def get_customer_default_company(customer):
    company = frappe.db.get_value(
        RECEIVABLE_DOCTYPE,
        {"customer": customer},
        "company",
        order_by="creation desc",
    )
    if not company:
        company = frappe.defaults.get_user_default("Company")
    return company


def _statement_client_script():
    return r'''
frappe.ui.form.on('NKT Customer Statement', {
    setup(frm) {
        frm.set_query('customer', () => ({ filters: { disabled: 0 } }));
    },

    onload(frm) {
        if (frm.is_new()) {
            if (!frm.doc.to_date) {
                frm.set_value('to_date', frappe.datetime.get_today());
            }
            if (!frm.doc.from_date) {
                frm.set_value('from_date', frappe.datetime.month_start());
            }
            if (!frm.doc.company) {
                frm.set_value('company', frappe.defaults.get_user_default('Company'));
            }
        }
    },

    customer(frm) {
        if (!frm.doc.customer || frm.doc.company) return;
        frappe.call({
            method: 'nkt_operations.nkt_store_operations.features.payments_accounts.statement.get_customer_default_company',
            args: { customer: frm.doc.customer },
            callback(r) {
                if (r.message) frm.set_value('company', r.message);
            }
        });
    },

    refresh(frm) {
        frm.add_custom_button(__('Generate / Refresh Statement'), () => {
            if (!frm.doc.company || !frm.doc.customer || !frm.doc.from_date || !frm.doc.to_date) {
                frappe.msgprint(__('Company, Customer, From Date, and To Date are required.'));
                return;
            }
            frappe.call({
                method: 'nkt_operations.nkt_store_operations.features.payments_accounts.statement.get_statement_data',
                args: {
                    company: frm.doc.company,
                    customer: frm.doc.customer,
                    from_date: frm.doc.from_date,
                    to_date: frm.doc.to_date
                },
                freeze: true,
                freeze_message: __('Generating statement...'),
                callback(r) {
                    const data = r.message || {};
                    const fields = [
                        'customer_name', 'currency', 'opening_balance', 'period_charges',
                        'period_payments', 'closing_balance', 'aging_current', 'aging_1_30',
                        'aging_31_60', 'aging_61_90', 'aging_over_90', 'generated_by',
                        'generated_on', 'status'
                    ];
                    fields.forEach((fieldname) => frm.set_value(fieldname, data[fieldname]));
                    frm.clear_table('lines');
                    (data.lines || []).forEach((row) => {
                        const child = frm.add_child('lines');
                        Object.keys(row).forEach((key) => child[key] = row[key]);
                    });
                    frm.refresh_field('lines');
                    frm.save().then(() => {
                        frappe.show_alert({ message: __('Statement generated.'), indicator: 'green' });
                    });
                }
            });
        }).addClass('btn-primary');

        if (!frm.is_new() && frm.doc.status === 'Generated') {
            frm.add_custom_button(__('Print Statement'), () => {
                const url = frappe.urllib.get_full_url(
                    '/printview?doctype=NKT%20Customer%20Statement&name=' +
                    encodeURIComponent(frm.doc.name) +
                    '&format=NKT%20Customer%20Statement%20of%20Account&no_letterhead=0'
                );
                window.open(url, '_blank');
            });
        }
    }
});
'''


def _customer_client_script():
    return r'''
frappe.ui.form.on('Customer', {
    refresh(frm) {
        if (frm.is_new()) return;
        frm.add_custom_button(__('Create Statement of Account'), () => {
            frappe.route_options = { customer: frm.doc.name };
            frappe.new_doc('NKT Customer Statement');
        }, __('NKT Account'));

        frm.add_custom_button(__('View Receivables'), () => {
            frappe.set_route('List', 'NKT Customer Receivable', { customer: frm.doc.name });
        }, __('NKT Account'));

        frm.add_custom_button(__('Statement History'), () => {
            frappe.set_route('List', 'NKT Customer Statement', { customer: frm.doc.name });
        }, __('NKT Account'));
    }
});
'''



def _cashier_print_client_script():
    return r'''
frappe.ui.form.on('NKT Cashier Account Collection', {
    refresh(frm) {
        if (frm.is_new() || frm.doc.status === 'Draft') return;
        frm.add_custom_button(__('Print Account Payment Receipt'), () => {
            const url = frappe.urllib.get_full_url(
                '/printview?doctype=NKT%20Cashier%20Account%20Collection&name=' +
                encodeURIComponent(frm.doc.name) +
                '&format=NKT%20Account%20Payment%20Receipt&no_letterhead=0'
            );
            window.open(url, '_blank');
        }, __('Print'));
    }
});
'''


def _statement_print_html():
    return r'''
<style>
    .nkt-soa { font-family: Arial, sans-serif; font-size: 10px; color: #111; }
    .nkt-soa h2, .nkt-soa h3 { margin: 0; }
    .nkt-soa .muted { color: #555; }
    .nkt-soa .header { display: flex; justify-content: space-between; margin-bottom: 14px; }
    .nkt-soa .summary, .nkt-soa .aging { width: 100%; border-collapse: collapse; margin-top: 10px; }
    .nkt-soa .summary td, .nkt-soa .aging td, .nkt-soa .aging th { border: 1px solid #777; padding: 5px; }
    .nkt-soa .transactions { width: 100%; border-collapse: collapse; margin-top: 12px; }
    .nkt-soa .transactions th, .nkt-soa .transactions td { border: 1px solid #777; padding: 4px; vertical-align: top; }
    .nkt-soa .transactions th { background: #efefef; }
    .nkt-soa .num { text-align: right; white-space: nowrap; }
    .nkt-soa .center { text-align: center; }
    .nkt-soa .footer { margin-top: 16px; font-size: 9px; }
</style>
<div class="nkt-soa">
    <div class="header">
        <div>
            <h2>{{ doc.company }}</h2>
            <h3>STATEMENT OF ACCOUNT</h3>
            <div class="muted">Statement No.: {{ doc.name }}</div>
        </div>
        <div style="text-align:right;">
            <b>Customer:</b> {{ doc.customer_name or doc.customer }}<br>
            <b>Customer ID:</b> {{ doc.customer }}<br>
            <b>Period:</b> {{ frappe.format(doc.from_date, {'fieldtype':'Date'}) }} to {{ frappe.format(doc.to_date, {'fieldtype':'Date'}) }}<br>
            <b>Generated:</b> {{ frappe.format(doc.generated_on, {'fieldtype':'Datetime'}) }}
        </div>
    </div>

    <table class="summary">
        <tr>
            <td><b>Opening Balance</b></td><td class="num">{{ frappe.format(doc.opening_balance, {'fieldtype':'Currency', 'options': doc.currency}) }}</td>
            <td><b>Charges</b></td><td class="num">{{ frappe.format(doc.period_charges, {'fieldtype':'Currency', 'options': doc.currency}) }}</td>
            <td><b>Payments</b></td><td class="num">{{ frappe.format(doc.period_payments, {'fieldtype':'Currency', 'options': doc.currency}) }}</td>
            <td><b>Closing Balance</b></td><td class="num"><b>{{ frappe.format(doc.closing_balance, {'fieldtype':'Currency', 'options': doc.currency}) }}</b></td>
        </tr>
    </table>

    <table class="transactions">
        <thead>
            <tr>
                <th>Date</th><th>Type</th><th>Reference</th><th>Order</th><th>Due Date</th><th>Description</th>
                <th class="num">Charge</th><th class="num">Payment</th><th class="num">Balance</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td>{{ frappe.format(doc.from_date, {'fieldtype':'Date'}) }}</td>
                <td colspan="7"><b>Opening Balance</b></td>
                <td class="num"><b>{{ frappe.format(doc.opening_balance, {'fieldtype':'Currency', 'options': doc.currency}) }}</b></td>
            </tr>
            {% for row in doc.lines %}
            <tr>
                <td>{{ frappe.format(row.posting_date, {'fieldtype':'Date'}) }}</td>
                <td>{{ row.entry_type }}</td>
                <td>{{ row.reference_name or '' }}</td>
                <td>{{ row.customer_order or '' }}</td>
                <td>{{ frappe.format(row.due_date, {'fieldtype':'Date'}) if row.due_date else '' }}</td>
                <td>{{ row.description or '' }}</td>
                <td class="num">{{ frappe.format(row.debit, {'fieldtype':'Currency', 'options': doc.currency}) if row.debit else '' }}</td>
                <td class="num">{{ frappe.format(row.credit, {'fieldtype':'Currency', 'options': doc.currency}) if row.credit else '' }}</td>
                <td class="num">{{ frappe.format(row.running_balance, {'fieldtype':'Currency', 'options': doc.currency}) }}</td>
            </tr>
            {% endfor %}
        </tbody>
    </table>

    <h3 style="margin-top:14px;">AGING AS OF {{ frappe.format(doc.to_date, {'fieldtype':'Date'}) }}</h3>
    <table class="aging">
        <tr>
            <th>Current</th><th>1–30 Days</th><th>31–60 Days</th><th>61–90 Days</th><th>Over 90 Days</th><th>Total</th>
        </tr>
        <tr>
            <td class="num">{{ frappe.format(doc.aging_current, {'fieldtype':'Currency', 'options': doc.currency}) }}</td>
            <td class="num">{{ frappe.format(doc.aging_1_30, {'fieldtype':'Currency', 'options': doc.currency}) }}</td>
            <td class="num">{{ frappe.format(doc.aging_31_60, {'fieldtype':'Currency', 'options': doc.currency}) }}</td>
            <td class="num">{{ frappe.format(doc.aging_61_90, {'fieldtype':'Currency', 'options': doc.currency}) }}</td>
            <td class="num">{{ frappe.format(doc.aging_over_90, {'fieldtype':'Currency', 'options': doc.currency}) }}</td>
            <td class="num"><b>{{ frappe.format(doc.closing_balance, {'fieldtype':'Currency', 'options': doc.currency}) }}</b></td>
        </tr>
    </table>

    {% if doc.notes %}<div style="margin-top:12px;"><b>Notes:</b> {{ doc.notes }}</div>{% endif %}
    <div class="footer">
        This statement is generated from approved NKT customer receivables and matched encoder-verified account payments.
        Please report discrepancies with the referenced order or payment receipt numbers.
    </div>
</div>
'''


def _payment_receipt_print_html():
    return r'''
<style>
    .nkt-payment-receipt { font-family: Arial, sans-serif; font-size: 11px; color: #111; max-width: 760px; margin: 0 auto; }
    .nkt-payment-receipt h2, .nkt-payment-receipt h3 { margin: 0; text-align: center; }
    .nkt-payment-receipt table { width: 100%; border-collapse: collapse; margin-top: 10px; }
    .nkt-payment-receipt th, .nkt-payment-receipt td { border: 1px solid #777; padding: 5px; }
    .nkt-payment-receipt .num { text-align: right; white-space: nowrap; }
    .nkt-payment-receipt .status { margin-top: 10px; padding: 7px; border: 1px solid #777; text-align: center; font-weight: bold; }
    .nkt-payment-receipt .signatures { margin-top: 35px; display:flex; justify-content:space-between; }
    .nkt-payment-receipt .signature { width: 42%; border-top:1px solid #111; text-align:center; padding-top:4px; }
</style>
<div class="nkt-payment-receipt">
    <h2>{{ doc.company }}</h2>
    <h3>ACCOUNT PAYMENT RECEIPT</h3>
    <div style="margin-top:10px; display:flex; justify-content:space-between;">
        <div>
            <b>Collection No.:</b> {{ doc.name }}<br>
            <b>Payment Receipt:</b> {{ doc.linked_payment_receipt or 'Pending' }}<br>
            <b>Date:</b> {{ frappe.format(doc.collection_datetime, {'fieldtype':'Datetime'}) }}
        </div>
        <div style="text-align:right;">
            <b>Customer:</b> {{ doc.customer_name or doc.customer }}<br>
            <b>Customer ID:</b> {{ doc.customer }}<br>
            {% if doc.referenced_customer_order %}<b>Specific Order:</b> {{ doc.referenced_customer_order }}{% endif %}
        </div>
    </div>

    <table>
        <thead><tr><th>Payment Method</th><th>Reference / Bank / Check</th><th class="num">Amount</th></tr></thead>
        <tbody>
        {% for row in doc.payments %}
            <tr>
                <td>{{ row.payment_method }}</td>
                <td>
                    {% if row.reference_number %}Ref: {{ row.reference_number }}{% endif %}
                    {% if row.bank_or_provider %} {{ row.bank_or_provider }}{% endif %}
                    {% if row.check_number %} Check: {{ row.check_number }}{% endif %}
                    {% if row.check_date %} / {{ frappe.format(row.check_date, {'fieldtype':'Date'}) }}{% endif %}
                </td>
                <td class="num">{{ frappe.format(row.amount, {'fieldtype':'Currency'}) }}</td>
            </tr>
        {% endfor %}
        </tbody>
    </table>

    <table>
        <tr><td><b>Previous Account Balance</b></td><td class="num">{{ frappe.format(doc.previous_balance, {'fieldtype':'Currency'}) }}</td></tr>
        <tr><td><b>Amount Received</b></td><td class="num">{{ frappe.format(doc.total_payment, {'fieldtype':'Currency'}) }}</td></tr>
        <tr><td><b>Expected Balance After Collection</b></td><td class="num"><b>{{ frappe.format(doc.balance_after_collection, {'fieldtype':'Currency'}) }}</b></td></tr>
    </table>

    <div class="status">
        {% if doc.status == 'Matched' %}
            VERIFIED AND APPLIED TO CUSTOMER ACCOUNT
        {% else %}
            PAYMENT RECEIVED — ACCOUNT APPLICATION PENDING ENCODER VERIFICATION
        {% endif %}
    </div>

    {% if doc.remarks %}<div style="margin-top:8px;"><b>Remarks:</b> {{ doc.remarks }}</div>{% endif %}

    <div class="signatures">
        <div class="signature">Cashier: {{ doc.cashier }}</div>
        <div class="signature">Customer / Authorized Representative</div>
    </div>
</div>
'''
