# Copyright (c) 2026, NKT Grains Trading
# C11B Owner/Admin Control Center Foundation — READ ONLY
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, today

ALLOWED_ROLES = {"NKT ADMINISTRATOR", "NKT OWNER"}
TOL = 0.00001

CATEGORIES = [
    "Daily Control",
    "Cashier / Encoder Reconciliation",
    "Cashier Shift",
    "Credit Control / Receivables",
    "Returns / Exchanges",
    "Physical Inventory",
    "Supplier / Purchasing",
    "Warehouse Customer Release",
    "Internal Warehouse Transfer",
]


def _require_owner_admin():
    if frappe.session.user == "Administrator":
        return
    if not ALLOWED_ROLES.intersection(set(frappe.get_roles())):
        frappe.throw(
            _("Only NKT Owner/Administrator may view the Owner Control Center."),
            frappe.PermissionError,
        )


def _day_bounds(day):
    d = getdate(day)
    return (
        datetime.combine(d, time.min),
        datetime.combine(d, time.max),
    )


def _company_filter(company):
    return {"company": company} if company else {}


def _sum(doctype, field, filters):
    if not frappe.db.exists("DocType", doctype):
        return 0.0

    # Frappe v16 query-builder syntax: SQL functions in select fields must
    # use dictionary notation rather than legacy string expressions.
    query = frappe.qb.get_query(
        doctype,
        fields=[{"SUM": field, "as": "total"}],
        filters=filters,
    )
    rows = query.run(as_dict=True)
    return flt((rows[0].get("total") if rows else 0) or 0)


def _count(doctype, filters=None):
    if not frappe.db.exists("DocType", doctype):
        return 0
    return int(frappe.db.count(doctype, filters=filters or {}))


def _rows(doctype, fields, filters=None, order_by=None, limit=1000):
    if not frappe.db.exists("DocType", doctype):
        return []
    kwargs = {
        "doctype": doctype,
        "fields": fields,
        "filters": filters or {},
        "limit_page_length": limit,
    }
    if order_by:
        kwargs["order_by"] = order_by
    return frappe.get_all(**kwargs)


def _add(queue, *, severity, category, ref_doctype=None, reference=None,
         status=None, summary=None, amount=None, age_days=None, actor=None,
         business_date=None):
    queue.append({
        "severity": severity,
        "category": category,
        "reference_doctype": ref_doctype,
        "reference": reference,
        "status": status,
        "summary": summary,
        "amount": flt(amount) if amount is not None else None,
        "age_days": age_days,
        "actor": actor,
        "business_date": business_date,
    })


def _matches_filters(row, filters):
    if filters.get("category") and row.get("category") != filters.category:
        return False
    if filters.get("severity") and row.get("severity") != filters.severity:
        return False
    needle = (filters.get("search_text") or "").strip().lower()
    if needle:
        haystack = " ".join(
            str(row.get(k) or "")
            for k in (
                "severity", "category", "reference_doctype", "reference",
                "status", "summary", "actor", "business_date",
            )
        ).lower()
        if needle not in haystack:
            return False
    return True


def execute(filters=None):
    _require_owner_admin()
    filters = frappe._dict(filters or {})
    filters.business_date = filters.get("business_date") or today()

    columns = get_columns()
    queue, metrics = build_control_center(filters)
    queue = [row for row in queue if _matches_filters(row, filters)]
    queue.sort(key=_sort_key)

    summary = get_report_summary(metrics)
    message = (
        "READ-ONLY OWNER CONTROL CENTER. "
        "Live cards are operational read-model totals; finalized EOD/Z-Out documents remain the formal daily control records."
    )
    return columns, queue, message, None, summary


def _sort_key(row):
    severity_order = {"Critical": 0, "Attention": 1, "Info": 2}
    return (
        severity_order.get(row.get("severity"), 9),
        CATEGORIES.index(row.get("category")) if row.get("category") in CATEGORIES else 99,
        str(row.get("reference") or ""),
    )


def get_columns():
    return [
        {"label": _("Severity"), "fieldname": "severity", "fieldtype": "Data", "width": 85},
        {"label": _("Category"), "fieldname": "category", "fieldtype": "Data", "width": 185},
        {"label": _("Reference Type"), "fieldname": "reference_doctype", "fieldtype": "Data", "width": 150},
        {
            "label": _("Reference"),
            "fieldname": "reference",
            "fieldtype": "Dynamic Link",
            "options": "reference_doctype",
            "width": 150,
        },
        {"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 145},
        {"label": _("Summary"), "fieldname": "summary", "fieldtype": "Data", "width": 430},
        {"label": _("Amount"), "fieldname": "amount", "fieldtype": "Currency", "width": 115},
        {"label": _("Age / Days"), "fieldname": "age_days", "fieldtype": "Int", "width": 85},
        {"label": _("User / Actor"), "fieldname": "actor", "fieldtype": "Data", "width": 145},
        {"label": _("Business Date"), "fieldname": "business_date", "fieldtype": "Date", "width": 105},
    ]


def build_control_center(filters):
    business_date = getdate(filters.business_date)
    company = filters.get("company")
    start_dt, end_dt = _day_bounds(business_date)
    queue = []

    metrics = {
        "cashier_sales": 0.0,
        "actual_collections": 0.0,
        "card_surcharge_collected": 0.0,
        "cash_over_short": 0.0,
        "outstanding_receivables": 0.0,
        "available_advances": 0.0,
        "open_shifts": 0,
        "attention_count": 0,
        "critical_count": 0,
        "transfer_issue_count": 0,
        "supplier_exception_count": 0,
        "physical_review_count": 0,
    }

    # ------------------------------------------------------------------
    # Live operational cards — read-only sums from NKT source-of-truth docs.
    # ------------------------------------------------------------------
    sale_filters = {
        "business_date": business_date,
        "docstatus": 1,
        "status": ["!=", "Cancelled"],
    }
    if company:
        sale_filters["company"] = company
    metrics["cashier_sales"] = _sum("NKT Cashier Sale", "grand_total", sale_filters)

    receipt_filters = {
        "receipt_datetime": ["between", [start_dt, end_dt]],
        "docstatus": 1,
        "receipt_status": "Completed",
    }
    if company:
        receipt_filters["company"] = company
    metrics["card_surcharge_collected"] = _sum(
        "NKT Payment Receipt", "card_surcharge_total", receipt_filters
    )
    metrics["actual_collections"] = (
        _sum("NKT Payment Receipt", "total_payment", receipt_filters)
        + metrics["card_surcharge_collected"]
    )

    shift_filters = {
        "shift_start": ["between", [start_dt, end_dt]],
        "status": ["not in", ["Cancelled", "Not Opened"]],
    }
    if company:
        shift_filters["company"] = company
    shift_rows = _rows(
        "NKT Cashier Shift",
        [
            "name", "company", "cashier", "status", "shift_start", "shift_end",
            "blind_count_confirmed", "over_short", "expected_cash", "actual_cash_count",
            "turnover_status",
        ],
        shift_filters,
        order_by="shift_start asc",
        limit=500,
    )
    metrics["cash_over_short"] = sum(
        flt(r.over_short or 0)
        for r in shift_rows
        if cint(r.blind_count_confirmed or 0)
    )
    closed_statuses = {"Reviewed / Closed", "Closed", "Cancelled"}
    open_shift_rows = [r for r in shift_rows if r.status not in closed_statuses]
    metrics["open_shifts"] = len(open_shift_rows)

    receivable_filters = {
        "status": ["in", ["Open", "Partially Paid"]],
        "outstanding_amount": [">", 0],
    }
    if company:
        receivable_filters["company"] = company
    receivables = _rows(
        "NKT Customer Receivable",
        [
            "name", "company", "customer", "customer_name", "customer_order",
            "posting_date", "due_date", "outstanding_amount", "status",
            "credit_control_status", "custom_nkt_days_overdue", "custom_nkt_aging_bucket",
            "source_encoder",
        ],
        receivable_filters,
        order_by="due_date asc",
        limit=5000,
    )
    metrics["outstanding_receivables"] = sum(flt(r.outstanding_amount or 0) for r in receivables)

    advance_filters = {
        "advance_status": ["in", ["Available", "Partially Used"]],
        "available_advance_amount": [">", 0],
    }
    if company:
        advance_filters["company"] = company
    metrics["available_advances"] = _sum(
        "NKT Customer Advance", "available_advance_amount", advance_filters
    )

    # ------------------------------------------------------------------
    # Daily Control — EOD / Z-Out state.
    # ------------------------------------------------------------------
    eod_filters = {"business_date": business_date}
    if company:
        eod_filters["company"] = company
    eods = _rows(
        "NKT EOD Reconciliation",
        [
            "name", "company", "business_date", "status", "reviewed_by", "reviewed_on",
            "cashier_shift_count", "reviewed_shift_count", "open_shift_count",
            "zout_count", "expected_zout_count", "cash_over_short_total",
            "exception_count", "sales_difference", "tender_difference",
            "account_sales_difference", "collection_difference",
        ],
        eod_filters,
        order_by="modified desc",
        limit=20,
    )
    if eods:
        eod = eods[0]
        if eod.status == "Reviewed - With Variance / Exceptions":
            _add(
                queue,
                severity="Critical",
                category="Daily Control",
                ref_doctype="NKT EOD Reconciliation",
                reference=eod.name,
                status=eod.status,
                summary=(
                    f"EOD reviewed with variance/exceptions: "
                    f"sales diff {flt(eod.sales_difference):,.2f}, "
                    f"tender diff {flt(eod.tender_difference):,.2f}, "
                    f"exceptions {int(eod.exception_count or 0)}."
                ),
                amount=flt(eod.cash_over_short_total or 0),
                actor=eod.reviewed_by,
                business_date=eod.business_date,
            )
        elif eod.status == "Draft":
            _add(
                queue,
                severity="Attention" if business_date < getdate(today()) else "Info",
                category="Daily Control",
                ref_doctype="NKT EOD Reconciliation",
                reference=eod.name,
                status=eod.status,
                summary="Owner/Admin EOD reconciliation exists but is not yet finalized.",
                actor=eod.reviewed_by,
                business_date=eod.business_date,
            )
    else:
        _add(
            queue,
            severity="Attention" if business_date < getdate(today()) else "Info",
            category="Daily Control",
            ref_doctype="NKT EOD Reconciliation",
            reference=None,
            status="Not Created / Reviewed",
            summary=(
                "No EOD Reconciliation exists for this business date."
                if business_date < getdate(today())
                else "Today's EOD Reconciliation has not been created/reviewed yet."
            ),
            business_date=business_date,
        )

    # Encoders with orders but no finalized Z-Out.
    order_filters = {
        "order_date": business_date,
        "docstatus": 1,
        "status": ["!=", "Cancelled"],
    }
    if company:
        order_filters["company"] = company
    orders = _rows(
        "NKT Customer Order",
        ["name", "company", "order_date", "encoder", "status", "cashier_reconciliation_status"],
        order_filters,
        limit=10000,
    )
    active_encoders = sorted({r.encoder for r in orders if r.encoder})
    zout_filters = {
        "business_date": business_date,
        "status": "Finalized",
    }
    if company:
        zout_filters["company"] = company
    zouts = _rows(
        "NKT Encoder Z-Out",
        ["name", "company", "business_date", "encoder", "status", "exception_count", "finalized_by"],
        zout_filters,
        limit=1000,
    )
    finalized_encoders = {r.encoder for r in zouts if r.encoder}
    for encoder in active_encoders:
        if encoder not in finalized_encoders:
            _add(
                queue,
                severity="Attention" if business_date < getdate(today()) else "Info",
                category="Daily Control",
                ref_doctype="NKT Encoder Z-Out",
                reference=None,
                status="No Finalized Z-Out",
                summary=f"Encoder {encoder} has posted orders but no finalized Z-Out for the selected date.",
                actor=encoder,
                business_date=business_date,
            )

    # ------------------------------------------------------------------
    # Cashier / Encoder reconciliation exceptions.
    # ------------------------------------------------------------------
    cashier_exception_filters = {
        "business_date": business_date,
        "docstatus": 1,
        "reconciliation_status": [
            "in",
            [
                "Unmatched", "Ambiguous",
                "Matched with Customer Warning",
                "Matched with Warehouse Warning",
                "Matched with Customer and Warehouse Warning",
            ],
        ],
    }
    if company:
        cashier_exception_filters["company"] = company
    cashier_exceptions = _rows(
        "NKT Cashier Sale",
        [
            "name", "business_date", "customer", "customer_name", "cashier",
            "grand_total", "status", "reconciliation_status", "reconciliation_warning",
        ],
        cashier_exception_filters,
        order_by="sale_datetime asc",
        limit=5000,
    )
    for r in cashier_exceptions:
        severity = "Critical" if r.reconciliation_status == "Ambiguous" else "Attention"
        _add(
            queue,
            severity=severity,
            category="Cashier / Encoder Reconciliation",
            ref_doctype="NKT Cashier Sale",
            reference=r.name,
            status=r.reconciliation_status,
            summary=(
                f"{r.customer_name or r.customer}: "
                f"{r.reconciliation_warning or 'Cashier sale requires reconciliation attention.'}"
            ),
            amount=r.grand_total,
            actor=r.cashier,
            business_date=r.business_date,
        )

    order_exception_filters = {
        "order_date": business_date,
        "docstatus": 1,
        "cashier_reconciliation_status": [
            "in",
            [
                "Unmatched", "Ambiguous",
                "Matched with Customer Warning",
                "Matched with Warehouse Warning",
                "Matched with Customer and Warehouse Warning",
            ],
        ],
    }
    if company:
        order_exception_filters["company"] = company
    order_exceptions = _rows(
        "NKT Customer Order",
        [
            "name", "order_date", "customer", "customer_name", "encoder",
            "grand_total", "status", "cashier_reconciliation_status", "cashier_reconciliation_warning",
        ],
        order_exception_filters,
        order_by="creation asc",
        limit=5000,
    )
    for r in order_exceptions:
        severity = "Critical" if r.cashier_reconciliation_status == "Ambiguous" else "Attention"
        _add(
            queue,
            severity=severity,
            category="Cashier / Encoder Reconciliation",
            ref_doctype="NKT Customer Order",
            reference=r.name,
            status=r.cashier_reconciliation_status,
            summary=(
                f"{r.customer_name or r.customer}: "
                f"{r.cashier_reconciliation_warning or 'Encoder order requires reconciliation attention.'}"
            ),
            amount=r.grand_total,
            actor=r.encoder,
            business_date=r.order_date,
        )

    # ------------------------------------------------------------------
    # Cashier Shift control.
    # ------------------------------------------------------------------
    for r in open_shift_rows:
        _add(
            queue,
            severity="Attention" if business_date < getdate(today()) else "Info",
            category="Cashier Shift",
            ref_doctype="NKT Cashier Shift",
            reference=r.name,
            status=r.status,
            summary=f"Cashier shift remains open/not fully reviewed. Turnover: {r.turnover_status or '—'}.",
            actor=r.cashier,
            business_date=business_date,
        )

    for r in shift_rows:
        if cint(r.blind_count_confirmed or 0) and abs(flt(r.over_short or 0)) > TOL:
            _add(
                queue,
                severity="Critical" if abs(flt(r.over_short or 0)) >= 100 else "Attention",
                category="Cashier Shift",
                ref_doctype="NKT Cashier Shift",
                reference=r.name,
                status=r.status,
                summary=(
                    f"Cash count variance: expected {flt(r.expected_cash):,.2f}, "
                    f"actual {flt(r.actual_cash_count):,.2f}, over/short {flt(r.over_short):,.2f}."
                ),
                amount=r.over_short,
                actor=r.cashier,
                business_date=business_date,
            )

    # ------------------------------------------------------------------
    # Credit control / receivables.
    # ------------------------------------------------------------------
    for r in receivables:
        due = getdate(r.due_date) if r.due_date else None
        if due and due < business_date:
            days = (business_date - due).days
            severity = "Critical" if days > 90 else "Attention"
            _add(
                queue,
                severity=severity,
                category="Credit Control / Receivables",
                ref_doctype="NKT Customer Receivable",
                reference=r.name,
                status=f"{r.status} / {r.custom_nkt_aging_bucket or 'Unbucketed'}",
                summary=f"{r.customer_name or r.customer} has overdue receivable.",
                amount=r.outstanding_amount,
                age_days=days,
                actor=r.source_encoder,
                business_date=r.posting_date,
            )

    pending_credit_filters = {
        "order_date": business_date,
        "docstatus": 1,
        "custom_nkt_account_credit_status": "Pending Approval",
    }
    if company:
        pending_credit_filters["company"] = company
    pending_credit = _rows(
        "NKT Customer Order",
        [
            "name", "order_date", "customer", "customer_name", "encoder",
            "grand_total", "status", "custom_nkt_account_review_reason",
        ],
        pending_credit_filters,
        limit=1000,
    )
    for r in pending_credit:
        _add(
            queue,
            severity="Attention",
            category="Credit Control / Receivables",
            ref_doctype="NKT Customer Order",
            reference=r.name,
            status="Pending Credit Approval",
            summary=f"{r.customer_name or r.customer}: {r.custom_nkt_account_review_reason or 'Manual credit review required.'}",
            amount=r.grand_total,
            actor=r.encoder,
            business_date=r.order_date,
        )

    # ------------------------------------------------------------------
    # Return / Exchange exceptions.
    # ------------------------------------------------------------------
    return_filters = {
        "business_date": business_date,
        "reconciliation_status": ["in", ["Unmatched", "Ambiguous"]],
    }
    if company:
        return_filters["company"] = company
    returns = _rows(
        "NKT Return Exchange Declaration",
        [
            "name", "business_date", "entry_user", "customer", "customer_name",
            "side", "transaction_type", "reconciliation_status", "posting_status",
            "refund_money", "customer_pays", "charge_to_account",
        ],
        return_filters,
        order_by="entry_datetime asc",
        limit=5000,
    )
    for r in returns:
        _add(
            queue,
            severity="Critical" if r.reconciliation_status == "Ambiguous" else "Attention",
            category="Returns / Exchanges",
            ref_doctype="NKT Return Exchange Declaration",
            reference=r.name,
            status=f"{r.reconciliation_status} / {r.posting_status}",
            summary=f"{r.side} {r.transaction_type} for {r.customer_name or r.customer} is not fully reconciled.",
            amount=max(flt(r.refund_money), flt(r.customer_pays), flt(r.charge_to_account)),
            actor=r.entry_user,
            business_date=r.business_date,
        )

    # Posted matching exception: Matched but still Not Posted.
    matched_not_posted_filters = {
        "business_date": business_date,
        "reconciliation_status": "Matched",
        "posting_status": "Not Posted",
    }
    if company:
        matched_not_posted_filters["company"] = company
    for r in _rows(
        "NKT Return Exchange Declaration",
        [
            "name", "business_date", "entry_user", "customer", "customer_name",
            "side", "transaction_type", "reconciliation_status", "posting_status",
        ],
        matched_not_posted_filters,
        limit=5000,
    ):
        _add(
            queue,
            severity="Attention",
            category="Returns / Exchanges",
            ref_doctype="NKT Return Exchange Declaration",
            reference=r.name,
            status="Matched / Not Posted",
            summary=f"Matched {r.transaction_type} for {r.customer_name or r.customer} has not posted.",
            actor=r.entry_user,
            business_date=r.business_date,
        )

    # ------------------------------------------------------------------
    # Physical inventory — unresolved review backlog up through selected date.
    # ------------------------------------------------------------------
    physical_filters = {
        "business_date": ["<=", business_date],
        "review_status": ["in", ["Pending Admin Review", "Under Review", "Discrepancy Flagged"]],
    }
    if company:
        physical_filters["company"] = company
    physical_rows = _rows(
        "NKT Physical Inventory Adjustment",
        [
            "name", "company", "warehouse", "business_date", "counted_by",
            "adjustment_status", "review_status", "variance_line_count",
            "accountability_classification", "reviewed_by",
        ],
        physical_filters,
        order_by="business_date asc",
        limit=5000,
    )
    metrics["physical_review_count"] = len(physical_rows)
    for r in physical_rows:
        _add(
            queue,
            severity="Critical" if r.review_status == "Discrepancy Flagged" else "Attention",
            category="Physical Inventory",
            ref_doctype="NKT Physical Inventory Adjustment",
            reference=r.name,
            status=f"{r.adjustment_status} / {r.review_status}",
            summary=(
                f"{r.warehouse}: {int(r.variance_line_count or 0)} variance line(s); "
                f"classification {r.accountability_classification or 'Unreviewed'}."
            ),
            actor=r.reviewed_by or r.counted_by,
            business_date=r.business_date,
        )

    # ------------------------------------------------------------------
    # Supplier / Purchasing unresolved operational exceptions.
    # ------------------------------------------------------------------
    supplier_exception_filters = {
        "review_status": ["!=", "Resolved"],
    }
    if company:
        supplier_exception_filters["company"] = company
    supplier_exceptions = _rows(
        "NKT Supplier Delivery Exception",
        [
            "name", "company", "supplier", "review_status", "claim_status",
            "gross_claim_amount", "agreed_supplier_deduction_amount",
            "created_from_receiving_by",
        ],
        supplier_exception_filters,
        order_by="creation asc",
        limit=5000,
    )
    metrics["supplier_exception_count"] = len(supplier_exceptions)
    for r in supplier_exceptions:
        _add(
            queue,
            severity="Attention",
            category="Supplier / Purchasing",
            ref_doctype="NKT Supplier Delivery Exception",
            reference=r.name,
            status=f"{r.review_status} / {r.claim_status}",
            summary=f"Supplier delivery exception for {r.supplier} requires review/resolution.",
            amount=max(flt(r.gross_claim_amount), flt(r.agreed_supplier_deduction_amount)),
            actor=r.created_from_receiving_by,
        )

    receiving_filters = {
        "receiving_date": ["<=", business_date],
        "posting_status": ["in", ["Foundation Draft", "Posting Locked"]],
    }
    if company:
        receiving_filters["company"] = company
    for r in _rows(
        "NKT Supplier Receiving",
        [
            "name", "company", "supplier", "receiving_date", "receiving_warehouse",
            "posting_status", "total_delivered_qty", "total_accepted_qty",
            "total_shortage_qty", "total_damaged_qty", "posted_by",
        ],
        receiving_filters,
        order_by="receiving_date asc",
        limit=5000,
    ):
        _add(
            queue,
            severity="Attention" if r.receiving_date < business_date else "Info",
            category="Supplier / Purchasing",
            ref_doctype="NKT Supplier Receiving",
            reference=r.name,
            status=r.posting_status,
            summary=(
                f"{r.supplier} receiving at {r.receiving_warehouse}: "
                f"delivered {flt(r.total_delivered_qty):g}, accepted {flt(r.total_accepted_qty):g}, "
                f"short {flt(r.total_shortage_qty):g}, damaged {flt(r.total_damaged_qty):g}."
            ),
            actor=r.posted_by,
            business_date=r.receiving_date,
        )

    # ------------------------------------------------------------------
    # Customer warehouse release / recall attention.
    # ------------------------------------------------------------------
    release_filters = {
        "release_datetime": ["between", [start_dt, end_dt]],
        "release_status": ["in", ["Draft", "Recall Pending"]],
    }
    if company:
        release_filters["company"] = company
    for r in _rows(
        "NKT Warehouse Release",
        [
            "name", "company", "release_datetime", "customer", "customer_name",
            "customer_order", "custom_nkt_source_warehouse", "release_status",
            "total_release_quantity", "released_by",
        ],
        release_filters,
        limit=5000,
    ):
        _add(
            queue,
            severity="Attention",
            category="Warehouse Customer Release",
            ref_doctype="NKT Warehouse Release",
            reference=r.name,
            status=r.release_status,
            summary=(
                f"{r.customer_name or r.customer}: release from "
                f"{r.custom_nkt_source_warehouse or 'warehouse'} requires attention."
            ),
            actor=r.released_by,
            business_date=business_date,
        )

    for r in _rows(
        "NKT Warehouse Change",
        [
            "name", "change_status", "customer_order", "customer", "item",
            "original_warehouse", "new_warehouse", "requested_by", "requested_on",
            "recall_status",
        ],
        {"change_status": "Recall Pending"},
        order_by="requested_on asc",
        limit=5000,
    ):
        _add(
            queue,
            severity="Attention",
            category="Warehouse Customer Release",
            ref_doctype="NKT Warehouse Change",
            reference=r.name,
            status=f"{r.change_status} / {r.recall_status}",
            summary=(
                f"Warehouse change for {r.item}: {r.original_warehouse} → {r.new_warehouse}; "
                "physical recall confirmation is still pending."
            ),
            actor=r.requested_by,
        )

    # ------------------------------------------------------------------
    # C10 Internal Warehouse Transfer: REUSE accepted C10G report logic.
    # ------------------------------------------------------------------
    try:
        from nkt_operations.nkt_store_operations.report.nkt_warehouse_transfer_reconciliation import (
            nkt_warehouse_transfer_reconciliation as transfer_report,
        )
        transfer_filters = {
            "to_date": business_date,
            "company": company,
            "overdue_after_hours": 24,
        }
        transfer_rows = transfer_report.get_data(frappe._dict(transfer_filters))
    except Exception as exc:
        frappe.log_error(
            title="NKT C11B Owner Control Center — transfer read model",
            message=frappe.get_traceback(),
        )
        transfer_rows = []
        _add(
            queue,
            severity="Critical",
            category="Internal Warehouse Transfer",
            status="Read Model Error",
            summary=f"Unable to load accepted Warehouse Transfer Reconciliation data: {exc}",
            business_date=business_date,
        )

    transfer_seen = set()
    for r in transfer_rows:
        if r.get("view_status") not in ("Released / In Transit", "Partially Arrived", "Discrepancy") and not r.get("overdue"):
            continue
        key = (r.get("warehouse_transfer"), r.get("item_code"))
        if key in transfer_seen:
            continue
        transfer_seen.add(key)
        severity = "Critical" if r.get("view_status") == "Discrepancy" or r.get("overdue") else "Attention"
        _add(
            queue,
            severity=severity,
            category="Internal Warehouse Transfer",
            ref_doctype="NKT Warehouse Transfer",
            reference=r.get("warehouse_transfer"),
            status=r.get("view_status"),
            summary=(
                f"{r.get('item_code')}: released {flt(r.get('released_qty')):g}, "
                f"arrived {flt(r.get('arrived_qty')):g}, "
                f"variance {flt(r.get('variance_qty')):g}; "
                f"{r.get('source_warehouse')} → {r.get('destination_warehouse')}."
            ),
            age_days=int(flt(r.get("hours_in_transit")) / 24) if r.get("hours_in_transit") is not None else None,
            actor=r.get("released_by"),
            business_date=r.get("transfer_date"),
        )
    metrics["transfer_issue_count"] = len(transfer_seen)

    metrics["critical_count"] = sum(1 for row in queue if row["severity"] == "Critical")
    metrics["attention_count"] = sum(1 for row in queue if row["severity"] in ("Critical", "Attention"))

    return queue, metrics


def get_report_summary(metrics):
    return [
        {
            "value": metrics["cashier_sales"],
            "label": _("Live Cashier Sales"),
            "datatype": "Currency",
            "indicator": "Blue",
        },
        {
            "value": metrics["actual_collections"],
            "label": _("Actual Collections"),
            "datatype": "Currency",
            "indicator": "Blue",
        },
        {
            "value": metrics["card_surcharge_collected"],
            "label": _("Card Surcharge (2%)"),
            "datatype": "Currency",
            "indicator": "Blue",
        },
        {
            "value": metrics["cash_over_short"],
            "label": _("Cash Over / Short"),
            "datatype": "Currency",
            "indicator": "Red" if abs(flt(metrics["cash_over_short"])) > TOL else "Green",
        },
        {
            "value": metrics["outstanding_receivables"],
            "label": _("Outstanding Receivables"),
            "datatype": "Currency",
            "indicator": "Orange",
        },
        {
            "value": metrics["available_advances"],
            "label": _("Available Customer Advances"),
            "datatype": "Currency",
            "indicator": "Blue",
        },
        {
            "value": metrics["open_shifts"],
            "label": _("Open / Unreviewed Shifts"),
            "datatype": "Int",
            "indicator": "Orange" if metrics["open_shifts"] else "Green",
        },
        {
            "value": metrics["critical_count"],
            "label": _("Critical Exceptions"),
            "datatype": "Int",
            "indicator": "Red" if metrics["critical_count"] else "Green",
        },
        {
            "value": metrics["attention_count"],
            "label": _("Items Needing Attention"),
            "datatype": "Int",
            "indicator": "Orange" if metrics["attention_count"] else "Green",
        },
    ]
