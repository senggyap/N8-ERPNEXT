from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, getdate, today

from nkt_operations.nkt_store_operations.doctype.nkt_compliance_document.nkt_compliance_document import (
    calculate_compliance_status,
)


ALLOWED_ROLES = {"NKT OWNER", "NKT ADMINISTRATOR"}
ATTENTION_STATUSES = {"Expiring Soon", "Expired"}


def _assert_access():
    if frappe.session.user == "Administrator":
        return

    roles = set(frappe.get_roles(frappe.session.user))
    if not (roles & ALLOWED_ROLES):
        frappe.throw(
            _("Only NKT Owner or NKT Administrator may view the Compliance Control Center."),
            frappe.PermissionError,
        )


def _columns():
    return [
        {"label": _("Document"), "fieldname": "name", "fieldtype": "Link", "options": "NKT Compliance Document", "width": 135},
        {"label": _("Title"), "fieldname": "document_title", "fieldtype": "Data", "width": 220},
        {"label": _("Category"), "fieldname": "document_category", "fieldtype": "Data", "width": 130},
        {"label": _("Subtype"), "fieldname": "document_subtype", "fieldtype": "Data", "width": 140},
        {"label": _("Reference No."), "fieldname": "document_number", "fieldtype": "Data", "width": 145},
        {"label": _("Live Status"), "fieldname": "live_status", "fieldtype": "Data", "width": 120},
        {"label": _("Record State"), "fieldname": "record_state", "fieldtype": "Data", "width": 105},
        {"label": _("Due Date"), "fieldname": "due_date", "fieldtype": "Date", "width": 105},
        {"label": _("Days to Due"), "fieldname": "days_to_due", "fieldtype": "Int", "width": 95},
        {"label": _("Reminder Days"), "fieldname": "reminder_days_before", "fieldtype": "Int", "width": 105},
        {"label": _("Agency / Authority"), "fieldname": "issuing_agency", "fieldtype": "Data", "width": 170},
        {"label": _("Responsible"), "fieldname": "responsible_user", "fieldtype": "Link", "options": "User", "width": 165},
        {"label": _("Company"), "fieldname": "company", "fieldtype": "Link", "options": "Company", "width": 150},
        {"label": _("Branch"), "fieldname": "branch", "fieldtype": "Link", "options": "Branch", "width": 125},
        {"label": _("Warehouse / Site"), "fieldname": "warehouse", "fieldtype": "Link", "options": "Warehouse", "width": 160},
        {"label": _("Evidence"), "fieldname": "document_file", "fieldtype": "Data", "width": 160},
    ]


def _base_filters(filters):
    db_filters = {}

    for fieldname in [
        "company",
        "document_category",
        "record_state",
        "responsible_user",
    ]:
        value = filters.get(fieldname)
        if value:
            db_filters[fieldname] = value

    title_contains = (filters.get("title_contains") or "").strip()
    if title_contains:
        db_filters["document_title"] = ["like", f"%{title_contains}%"]

    return db_filters


def execute(filters=None):
    _assert_access()

    filters = frappe._dict(filters or {})
    as_of_date = getdate(filters.get("as_of_date") or today())
    attention_only = cint(filters.get("attention_only") if filters.get("attention_only") is not None else 1)
    due_within_days = filters.get("due_within_days")
    due_within_days = cint(due_within_days) if str(due_within_days or "").strip() else None

    rows = frappe.get_all(
        "NKT Compliance Document",
        filters=_base_filters(filters),
        fields=[
            "name",
            "document_title",
            "document_category",
            "document_subtype",
            "document_number",
            "record_state",
            "compliance_status",
            "issuing_agency",
            "responsible_user",
            "company",
            "branch",
            "warehouse",
            "location_details",
            "issue_date",
            "expiry_date",
            "renewal_due_date",
            "reminder_days_before",
            "document_file",
            "modified",
        ],
        order_by="modified desc, name desc",
        limit_page_length=100000,
    )

    data = []
    status_counts = {
        "Active": 0,
        "Expiring Soon": 0,
        "Expired": 0,
        "No Expiry": 0,
        "Superseded": 0,
        "Cancelled": 0,
    }

    for row in rows:
        live = calculate_compliance_status(row, as_of_date=as_of_date)
        live_status = live["status"]
        status_counts[live_status] = status_counts.get(live_status, 0) + 1

        if attention_only and live_status not in ATTENTION_STATUSES:
            continue

        days_to_due = live["days_to_due"]
        if due_within_days is not None:
            # Expired items remain visible because they are already overdue.
            if days_to_due is None or days_to_due > due_within_days:
                continue

        due_date = row.renewal_due_date or row.expiry_date

        data.append({
            "name": row.name,
            "document_title": row.document_title,
            "document_category": row.document_category,
            "document_subtype": row.document_subtype,
            "document_number": row.document_number,
            "live_status": live_status,
            "record_state": row.record_state,
            "due_date": due_date,
            "days_to_due": days_to_due,
            "reminder_days_before": row.reminder_days_before,
            "issuing_agency": row.issuing_agency,
            "responsible_user": row.responsible_user,
            "company": row.company,
            "branch": row.branch,
            "warehouse": row.warehouse,
            "document_file": row.document_file,
        })

    status_rank = {
        "Expired": 0,
        "Expiring Soon": 1,
        "Active": 2,
        "No Expiry": 3,
        "Superseded": 4,
        "Cancelled": 5,
    }
    data.sort(
        key=lambda r: (
            status_rank.get(r["live_status"], 99),
            r["days_to_due"] if r["days_to_due"] is not None else 999999,
            r["document_title"] or "",
            r["name"],
        )
    )

    attention_count = status_counts.get("Expired", 0) + status_counts.get("Expiring Soon", 0)

    summary = [
        {"value": attention_count, "label": _("Attention Needed"), "datatype": "Int", "indicator": "Red" if status_counts.get("Expired") else ("Orange" if attention_count else "Green")},
        {"value": status_counts.get("Expired", 0), "label": _("Expired"), "datatype": "Int", "indicator": "Red"},
        {"value": status_counts.get("Expiring Soon", 0), "label": _("Expiring Soon"), "datatype": "Int", "indicator": "Orange"},
        {"value": status_counts.get("Active", 0), "label": _("Active"), "datatype": "Int", "indicator": "Green"},
        {"value": status_counts.get("No Expiry", 0), "label": _("No Expiry"), "datatype": "Int", "indicator": "Blue"},
        {"value": status_counts.get("Superseded", 0) + status_counts.get("Cancelled", 0), "label": _("Inactive Records"), "datatype": "Int", "indicator": "Gray"},
    ]

    chart = {
        "data": {
            "labels": ["Active", "Expiring Soon", "Expired", "No Expiry", "Superseded", "Cancelled"],
            "datasets": [{
                "name": _("Documents"),
                "values": [
                    status_counts.get("Active", 0),
                    status_counts.get("Expiring Soon", 0),
                    status_counts.get("Expired", 0),
                    status_counts.get("No Expiry", 0),
                    status_counts.get("Superseded", 0),
                    status_counts.get("Cancelled", 0),
                ],
            }],
        },
        "type": "bar",
        "height": 240,
    }

    message = _(
        "Live Compliance Status is recalculated from each document's Record State and its own "
        "Renewal Due Date / Expiry Date against the selected As-of Date. 'Expiring Soon' respects "
        "that document's Reminder Days Before Due. This Control Center is the C13C alert surface; "
        "it does not send duplicate generic reminder emails."
    )

    return _columns(), data, message, chart, summary
