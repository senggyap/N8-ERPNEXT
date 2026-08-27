# Copyright (c) 2026, NKT Grains Trading
# C10G Admin/Owner Internal Warehouse Transfer Reconciliation Report
from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime

REVIEW_ROLES = {"NKT ADMINISTRATOR", "NKT OWNER"}
TOL = 0.0000001


def _require_admin_owner():
    if frappe.session.user == "Administrator":
        return
    if not REVIEW_ROLES.intersection(set(frappe.get_roles())):
        frappe.throw(
            _("Only NKT Admin/Owner may view Warehouse Transfer Reconciliation."),
            frappe.PermissionError,
        )


def derive_view_status(stored_status, released_qty, arrived_qty, open_discrepancy_count=0):
    released = flt(released_qty)
    arrived = flt(arrived_qty)
    remaining = max(released - arrived, 0.0)

    if cint(open_discrepancy_count):
        return "Discrepancy"
    if stored_status == "Draft" or released <= TOL:
        return "Draft"
    if remaining <= TOL:
        return "Completed"
    if arrived > TOL:
        return "Partially Arrived"
    return "Released / In Transit"


def overdue_state(released_at, released_qty, arrived_qty, threshold_hours=24, current_time=None):
    released = flt(released_qty)
    arrived = flt(arrived_qty)
    remaining = max(released - arrived, 0.0)
    if remaining <= TOL or not released_at:
        return False, None

    threshold = max(flt(threshold_hours), 0.0)
    now = get_datetime(current_time) if current_time else now_datetime()
    release_dt = get_datetime(released_at)
    age_hours = max((now - release_dt).total_seconds() / 3600.0, 0.0)
    return age_hours > threshold, age_hours


def execute(filters=None):
    _require_admin_owner()
    filters = frappe._dict(filters or {})
    columns = get_columns()
    data = get_data(filters)
    report_summary = get_report_summary(data)
    return columns, data, None, None, report_summary


def get_columns():
    return [
        {"label": _("Transfer"), "fieldname": "warehouse_transfer", "fieldtype": "Link", "options": "NKT Warehouse Transfer", "width": 150},
        {"label": _("Business Date"), "fieldname": "transfer_date", "fieldtype": "Date", "width": 100},
        {"label": _("View Status"), "fieldname": "view_status", "fieldtype": "Data", "width": 135},
        {"label": _("Stored Status"), "fieldname": "stored_status", "fieldtype": "Data", "width": 105},
        {"label": _("Source Warehouse"), "fieldname": "source_warehouse", "fieldtype": "Link", "options": "Warehouse", "width": 180},
        {"label": _("Destination Warehouse"), "fieldname": "destination_warehouse", "fieldtype": "Link", "options": "Warehouse", "width": 180},
        {"label": _("Item"), "fieldname": "item_code", "fieldtype": "Link", "options": "Item", "width": 165},
        {"label": _("UOM"), "fieldname": "uom", "fieldtype": "Link", "options": "UOM", "width": 75},
        {"label": _("Requested"), "fieldname": "requested_qty", "fieldtype": "Float", "width": 90},
        {"label": _("Released"), "fieldname": "released_qty", "fieldtype": "Float", "width": 85},
        {"label": _("Arrived"), "fieldname": "arrived_qty", "fieldtype": "Float", "width": 85},
        {"label": _("Release vs Arrival Variance"), "fieldname": "variance_qty", "fieldtype": "Float", "width": 155},
        {"label": _("Hours In Transit"), "fieldname": "hours_in_transit", "fieldtype": "Float", "precision": 1, "width": 110},
        {"label": _("Overdue"), "fieldname": "overdue", "fieldtype": "Check", "width": 75},
        {"label": _("Discrepancy"), "fieldname": "discrepancy", "fieldtype": "Link", "options": "NKT Warehouse Transfer Discrepancy", "width": 145},
        {"label": _("Discrepancy Review"), "fieldname": "discrepancy_status", "fieldtype": "Data", "width": 120},
        {"label": _("Issue / Responsibility"), "fieldname": "issue_responsibility", "fieldtype": "Data", "width": 220},
        {"label": _("Internal DR No."), "fieldname": "internal_dr_no", "fieldtype": "Data", "width": 115},
        {"label": _("Released At"), "fieldname": "released_at", "fieldtype": "Datetime", "width": 145},
        {"label": _("Released By"), "fieldname": "released_by", "fieldtype": "Link", "options": "User", "width": 145},
        {"label": _("Last Arrival At"), "fieldname": "arrived_at", "fieldtype": "Datetime", "width": 145},
        {"label": _("Last Arrival By"), "fieldname": "arrived_by", "fieldtype": "Link", "options": "User", "width": 145},
    ]


def get_data(filters):
    transfer_filters = {}
    if filters.get("from_date"):
        transfer_filters["transfer_date"] = [">=", filters.from_date]
    if filters.get("to_date"):
        if "transfer_date" in transfer_filters:
            transfer_filters["transfer_date"] = ["between", [filters.from_date, filters.to_date]]
        else:
            transfer_filters["transfer_date"] = ["<=", filters.to_date]
    if filters.get("company"):
        transfer_filters["company"] = filters.company
    if filters.get("source_warehouse"):
        transfer_filters["source_warehouse"] = filters.source_warehouse
    if filters.get("destination_warehouse"):
        transfer_filters["destination_warehouse"] = filters.destination_warehouse

    transfers = frappe.get_all(
        "NKT Warehouse Transfer",
        filters=transfer_filters,
        fields=[
            "name", "company", "transfer_date", "internal_dr_no", "status",
            "source_warehouse", "destination_warehouse", "released_by", "released_at",
            "arrived_by", "arrived_at", "outgoing_stock_entry", "incoming_stock_entry",
        ],
        order_by="transfer_date desc, creation desc",
        limit_page_length=10000,
    )
    if not transfers:
        return []

    names = [x.name for x in transfers]
    items = frappe.get_all(
        "NKT Warehouse Transfer Item",
        filters={
            "parent": ["in", names],
            "parenttype": "NKT Warehouse Transfer",
            "parentfield": "items",
        },
        fields=[
            "name", "parent", "idx", "item_code", "item_name", "uom",
            "requested_qty", "released_qty", "arrived_qty",
        ],
        order_by="parent asc, idx asc",
        limit_page_length=50000,
    )

    if filters.get("item_code"):
        items = [r for r in items if r.item_code == filters.item_code]

    discrepancy_parents = frappe.get_all(
        "NKT Warehouse Transfer Discrepancy",
        filters={"warehouse_transfer": ["in", names]},
        fields=["name", "warehouse_transfer", "status", "reviewed_by", "reviewed_at"],
        order_by="creation asc",
        limit_page_length=10000,
    )
    disc_by_transfer = defaultdict(list)
    for d in discrepancy_parents:
        disc_by_transfer[d.warehouse_transfer].append(d)

    disc_names = [d.name for d in discrepancy_parents]
    disc_items = []
    if disc_names:
        disc_items = frappe.get_all(
            "NKT Warehouse Transfer Discrepancy Item",
            filters={
                "parent": ["in", disc_names],
                "parenttype": "NKT Warehouse Transfer Discrepancy",
                "parentfield": "items",
            },
            fields=["parent", "item_code", "issue_type", "discrepancy_qty", "responsibility"],
            order_by="parent asc, idx asc",
            limit_page_length=50000,
        )
    disc_item_map = defaultdict(list)
    for r in disc_items:
        disc_item_map[(r.parent, r.item_code)].append(r)

    transfer_map = {x.name: x for x in transfers}
    threshold = flt(filters.get("overdue_after_hours") or 24)
    rows = []

    for item in items:
        tr = transfer_map[item.parent]
        discrepancies = disc_by_transfer.get(tr.name, [])
        open_discrepancies = [d for d in discrepancies if d.status not in ("Resolved", "Cancelled")]

        relevant = []
        for d in discrepancies:
            drows = disc_item_map.get((d.name, item.item_code), [])
            if drows:
                relevant.append((d, drows))

        first_open = next((pair for pair in relevant if pair[0].status not in ("Resolved", "Cancelled")), None)
        selected = first_open or (relevant[-1] if relevant else None)

        issue_text = ""
        discrepancy_name = None
        discrepancy_status = None
        if selected:
            d, drows = selected
            discrepancy_name = d.name
            discrepancy_status = d.status
            issue_text = "; ".join(
                f"{r.issue_type} — {r.responsibility}" for r in drows
            )

        released = flt(item.released_qty)
        arrived = flt(item.arrived_qty)
        variance = max(released - arrived, 0.0)
        view_status = derive_view_status(
            tr.status,
            released,
            arrived,
            len(open_discrepancies),
        )
        overdue, age_hours = overdue_state(
            tr.released_at,
            released,
            arrived,
            threshold,
        )

        row = {
            "warehouse_transfer": tr.name,
            "transfer_date": tr.transfer_date,
            "view_status": view_status,
            "stored_status": tr.status,
            "source_warehouse": tr.source_warehouse,
            "destination_warehouse": tr.destination_warehouse,
            "item_code": item.item_code,
            "uom": item.uom,
            "requested_qty": flt(item.requested_qty),
            "released_qty": released,
            "arrived_qty": arrived,
            "variance_qty": variance,
            "hours_in_transit": age_hours,
            "overdue": 1 if overdue else 0,
            "discrepancy": discrepancy_name,
            "discrepancy_status": discrepancy_status,
            "issue_responsibility": issue_text,
            "internal_dr_no": tr.internal_dr_no,
            "released_at": tr.released_at,
            "released_by": tr.released_by,
            "arrived_at": tr.arrived_at,
            "arrived_by": tr.arrived_by,
        }

        if filters.get("view_status") and row["view_status"] != filters.view_status:
            continue
        if cint(filters.get("only_overdue")) and not row["overdue"]:
            continue
        if cint(filters.get("only_discrepancy")) and not row["discrepancy"]:
            continue

        rows.append(row)

    return rows


def get_report_summary(data):
    transfer_states = {}
    overdue_transfers = set()
    discrepancy_transfers = set()

    for row in data:
        name = row["warehouse_transfer"]
        transfer_states[name] = row["view_status"]
        if row.get("overdue"):
            overdue_transfers.add(name)
        if row.get("discrepancy"):
            discrepancy_transfers.add(name)

    total = len(transfer_states)
    partial = sum(1 for state in transfer_states.values() if state == "Partially Arrived")
    in_transit = sum(1 for state in transfer_states.values() if state == "Released / In Transit")
    completed = sum(1 for state in transfer_states.values() if state == "Completed")
    discrepancy = len(discrepancy_transfers)

    return [
        {"value": total, "label": _("Transfers"), "datatype": "Int"},
        {"value": in_transit, "label": _("In Transit"), "datatype": "Int"},
        {"value": partial, "label": _("Partially Arrived"), "datatype": "Int"},
        {"value": completed, "label": _("Completed"), "datatype": "Int"},
        {"value": len(overdue_transfers), "label": _("Overdue"), "datatype": "Int"},
        {"value": discrepancy, "label": _("With Discrepancy"), "datatype": "Int"},
    ]
