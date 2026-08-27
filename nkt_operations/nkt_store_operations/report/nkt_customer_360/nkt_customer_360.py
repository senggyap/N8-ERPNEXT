from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate

ALLOWED_ROLES = {"NKT OWNER", "NKT ADMINISTRATOR", "NKT Credit Controller"}
AGING_BUCKETS = ("Current", "1–30", "31–60", "61–90", "Over 90")


def _assert_access():
    user = frappe.session.user
    if user == "Administrator":
        return
    roles = set(frappe.get_roles(user))
    if not (roles & ALLOWED_ROLES):
        frappe.throw(
            _("Only NKT Owner, Administrator, or Credit Controller may view Customer 360."),
            frappe.PermissionError,
        )


def _bucket(due_date, as_of_date):
    if not due_date:
        return "Current", 0
    due = getdate(due_date)
    as_of = getdate(as_of_date)
    days = (as_of - due).days
    if days <= 0:
        return "Current", max(days, 0)
    if days <= 30:
        return "1–30", days
    if days <= 60:
        return "31–60", days
    if days <= 90:
        return "61–90", days
    return "Over 90", days


def _customer_name(customer):
    return frappe.db.get_value("Customer", customer, "customer_name") or customer


def _sum_available_advances(customer, company=None):
    filters = {"customer": customer}
    if company and frappe.get_meta("NKT Customer Advance").has_field("company"):
        filters["company"] = company
    rows = frappe.get_all(
        "NKT Customer Advance",
        filters=filters,
        fields=["available_advance_amount"],
        limit_page_length=100000,
    )
    return sum(max(flt(r.available_advance_amount), 0) for r in rows)


def _receivables(customer, as_of_date, company=None, include_closed=False):
    filters = {"customer": customer}
    if company:
        filters["company"] = company
    if not include_closed:
        filters["outstanding_amount"] = [">", 0]
        filters["status"] = ["!=", "Cancelled"]

    rows = frappe.get_all(
        "NKT Customer Receivable",
        filters=filters,
        fields=[
            "name", "company", "customer", "customer_name", "customer_order",
            "posting_date", "due_date", "original_amount", "amount_paid",
            "outstanding_amount", "status", "credit_control_status",
            "custom_nkt_last_collection_on", "custom_nkt_collection_count",
            "remarks",
        ],
        order_by="due_date asc, posting_date asc, name asc",
        limit_page_length=100000,
    )

    result = []
    for r in rows:
        bucket, days = _bucket(r.due_date, as_of_date)
        result.append({
            "section": "Receivable",
            "activity_date": r.posting_date,
            "due_date": r.due_date,
            "reference_doctype": "NKT Customer Receivable",
            "reference_name": r.name,
            "linked_order": r.customer_order,
            "status": r.status,
            "description": f"Account receivable | {r.credit_control_status or ''}".rstrip(" |"),
            "principal_debit": flt(r.original_amount),
            "principal_credit": flt(r.amount_paid),
            "outstanding_principal": flt(r.outstanding_amount),
            "card_surcharge": 0.0,
            "actual_collected": 0.0,
            "days_overdue": days,
            "aging_bucket": bucket,
        })
    return result


def _payment_receipts(customer, as_of_date, company=None):
    filters = {"customer": customer, "docstatus": 1}
    if company:
        filters["company"] = company
    rows = frappe.get_all(
        "NKT Payment Receipt",
        filters=filters,
        fields=[
            "name", "receipt_datetime", "payment_purpose", "customer_order",
            "source_cashier_sale", "custom_nkt_cashier_account_collection",
            "custom_nkt_encoder_account_allocation", "receipt_status",
            "total_payment", "card_surcharge_total", "total_collected",
            "remaining_balance", "allocation_status",
        ],
        order_by="receipt_datetime desc, name desc",
        limit_page_length=100000,
    )
    out = []
    cutoff = getdate(as_of_date)
    for r in rows:
        if r.receipt_datetime and getdate(r.receipt_datetime) > cutoff:
            continue
        out.append({
            "section": "Payment Receipt",
            "activity_date": getdate(r.receipt_datetime) if r.receipt_datetime else None,
            "due_date": None,
            "reference_doctype": "NKT Payment Receipt",
            "reference_name": r.name,
            "linked_order": r.customer_order,
            "status": r.receipt_status,
            "description": r.payment_purpose or r.allocation_status or "Payment",
            "principal_debit": 0.0,
            "principal_credit": flt(r.total_payment),
            "outstanding_principal": None,
            "card_surcharge": flt(r.card_surcharge_total),
            "actual_collected": flt(r.total_collected),
            "days_overdue": None,
            "aging_bucket": "",
        })
    return out


def _advances(customer, as_of_date, company=None):
    filters = {"customer": customer}
    if company:
        filters["company"] = company
    rows = frappe.get_all(
        "NKT Customer Advance",
        filters=filters,
        fields=[
            "name", "posting_datetime", "source_payment_receipt",
            "source_customer_order", "custom_nkt_credit_origin",
            "original_advance_amount", "applied_amount",
            "available_advance_amount", "advance_status",
        ],
        order_by="posting_datetime desc, name desc",
        limit_page_length=100000,
    )
    out = []
    cutoff = getdate(as_of_date)
    for r in rows:
        if r.posting_datetime and getdate(r.posting_datetime) > cutoff:
            continue
        out.append({
            "section": "Customer Advance",
            "activity_date": getdate(r.posting_datetime) if r.posting_datetime else None,
            "due_date": None,
            "reference_doctype": "NKT Customer Advance",
            "reference_name": r.name,
            "linked_order": r.source_customer_order,
            "status": r.advance_status,
            "description": r.custom_nkt_credit_origin or "Customer Advance",
            "principal_debit": 0.0,
            "principal_credit": flt(r.original_advance_amount),
            "outstanding_principal": -flt(r.available_advance_amount),
            "card_surcharge": 0.0,
            "actual_collected": 0.0,
            "days_overdue": None,
            "aging_bucket": "",
        })
    return out


def _account_collections(customer, as_of_date, company=None):
    filters = {"customer": customer}
    if company:
        filters["company"] = company
    rows = frappe.get_all(
        "NKT Cashier Account Collection",
        filters=filters,
        fields=[
            "name", "business_date", "collection_datetime",
            "referenced_customer_order", "status",
            "total_payment", "card_surcharge_total", "total_collected",
            "balance_after_collection", "linked_payment_receipt",
            "matched_encoder_allocation",
        ],
        order_by="business_date desc, collection_datetime desc, name desc",
        limit_page_length=100000,
    )
    out = []
    cutoff = getdate(as_of_date)
    for r in rows:
        if r.business_date and getdate(r.business_date) > cutoff:
            continue
        out.append({
            "section": "Account Collection",
            "activity_date": r.business_date,
            "due_date": None,
            "reference_doctype": "NKT Cashier Account Collection",
            "reference_name": r.name,
            "linked_order": r.referenced_customer_order,
            "status": r.status,
            "description": "Cashier customer-account collection",
            "principal_debit": 0.0,
            "principal_credit": flt(r.total_payment),
            "outstanding_principal": flt(r.balance_after_collection) if r.balance_after_collection is not None else None,
            "card_surcharge": flt(r.card_surcharge_total),
            "actual_collected": flt(r.total_collected),
            "days_overdue": None,
            "aging_bucket": "",
        })
    return out


def _returns(customer, as_of_date, company=None):
    filters = {"customer": customer, "posting_status": "Posted"}
    if company:
        filters["company"] = company
    rows = frappe.get_all(
        "NKT Return Exchange Declaration",
        filters=filters,
        fields=[
            "name", "business_date", "transaction_type", "settlement_destination",
            "old_customer_order", "new_customer_order", "return_credit",
            "account_adjustment_amount", "customer_credit_amount",
            "refund_money", "customer_pays", "charge_to_account",
            "reconciliation_status", "posting_status",
        ],
        order_by="business_date desc, name desc",
        limit_page_length=100000,
    )
    out = []
    cutoff = getdate(as_of_date)
    for r in rows:
        if r.business_date and getdate(r.business_date) > cutoff:
            continue
        principal_credit = flt(r.account_adjustment_amount) + flt(r.customer_credit_amount)
        principal_debit = flt(r.charge_to_account)
        out.append({
            "section": "Return / Exchange",
            "activity_date": r.business_date,
            "due_date": None,
            "reference_doctype": "NKT Return Exchange Declaration",
            "reference_name": r.name,
            "linked_order": r.old_customer_order or r.new_customer_order,
            "status": r.posting_status,
            "description": f"{r.transaction_type or 'Return/Exchange'} | {r.settlement_destination or 'No settlement'}",
            "principal_debit": principal_debit,
            "principal_credit": principal_credit,
            "outstanding_principal": None,
            "card_surcharge": 0.0,
            "actual_collected": flt(r.customer_pays),
            "days_overdue": None,
            "aging_bucket": "",
        })
    return out


def _columns():
    return [
        {"label": _("Section"), "fieldname": "section", "fieldtype": "Data", "width": 145},
        {"label": _("Date"), "fieldname": "activity_date", "fieldtype": "Date", "width": 105},
        {"label": _("Due Date"), "fieldname": "due_date", "fieldtype": "Date", "width": 105},
        {"label": _("Reference Type"), "fieldname": "reference_doctype", "fieldtype": "Data", "width": 180},
        {"label": _("Reference"), "fieldname": "reference_name", "fieldtype": "Dynamic Link", "options": "reference_doctype", "width": 160},
        {"label": _("Customer Order"), "fieldname": "linked_order", "fieldtype": "Link", "options": "NKT Customer Order", "width": 145},
        {"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 120},
        {"label": _("Description"), "fieldname": "description", "fieldtype": "Data", "width": 220},
        {"label": _("Principal Debit"), "fieldname": "principal_debit", "fieldtype": "Currency", "width": 120},
        {"label": _("Principal Credit"), "fieldname": "principal_credit", "fieldtype": "Currency", "width": 120},
        {"label": _("Outstanding Principal"), "fieldname": "outstanding_principal", "fieldtype": "Currency", "width": 135},
        {"label": _("Card Surcharge"), "fieldname": "card_surcharge", "fieldtype": "Currency", "width": 115},
        {"label": _("Actual Collected"), "fieldname": "actual_collected", "fieldtype": "Currency", "width": 120},
        {"label": _("Days Overdue"), "fieldname": "days_overdue", "fieldtype": "Int", "width": 100},
        {"label": _("Aging Bucket"), "fieldname": "aging_bucket", "fieldtype": "Data", "width": 100},
    ]


def execute(filters=None):
    _assert_access()
    filters = frappe._dict(filters or {})

    customer = (filters.get("customer") or "").strip()
    if not customer:
        frappe.throw(_("Customer is required."))

    if not frappe.db.exists("Customer", customer):
        frappe.throw(_("Customer {0} does not exist.").format(customer))

    as_of_date = getdate(filters.get("as_of_date") or nowdate())
    company = (filters.get("company") or "").strip() or None
    include_closed = cint(filters.get("include_closed_receivables") or 0)

    receivables = _receivables(customer, as_of_date, company, bool(include_closed))
    open_receivables = [r for r in receivables if flt(r.get("outstanding_principal")) > 0]

    outstanding = sum(flt(r.get("outstanding_principal")) for r in open_receivables)
    available_advance = _sum_available_advances(customer, company)

    aging_totals = {bucket: 0.0 for bucket in AGING_BUCKETS}
    aging_counts = {bucket: 0 for bucket in AGING_BUCKETS}
    for r in open_receivables:
        bucket = r["aging_bucket"]
        aging_totals[bucket] += flt(r["outstanding_principal"])
        aging_counts[bucket] += 1

    timeline = []
    timeline.extend(receivables)
    timeline.extend(_payment_receipts(customer, as_of_date, company))
    timeline.extend(_account_collections(customer, as_of_date, company))
    timeline.extend(_advances(customer, as_of_date, company))
    timeline.extend(_returns(customer, as_of_date, company))
    timeline.sort(
        key=lambda r: (
            getdate(r["activity_date"]) if r.get("activity_date") else getdate("1900-01-01"),
            r.get("section") or "",
            r.get("reference_name") or "",
        ),
        reverse=True,
    )

    summary = [
        {"value": outstanding, "indicator": "Red" if aging_totals["Over 90"] else "Blue", "label": _("Outstanding Principal"), "datatype": "Currency"},
        {"value": available_advance, "indicator": "Green", "label": _("Available Customer Advance"), "datatype": "Currency"},
        {"value": len(open_receivables), "indicator": "Orange" if open_receivables else "Green", "label": _("Open Receivables"), "datatype": "Int"},
        {"value": aging_totals["Current"], "indicator": "Blue", "label": _("Current"), "datatype": "Currency"},
        {"value": aging_totals["1–30"], "indicator": "Orange", "label": _("1–30"), "datatype": "Currency"},
        {"value": aging_totals["31–60"], "indicator": "Orange", "label": _("31–60"), "datatype": "Currency"},
        {"value": aging_totals["61–90"], "indicator": "Red", "label": _("61–90"), "datatype": "Currency"},
        {"value": aging_totals["Over 90"], "indicator": "Red", "label": _("Over 90"), "datatype": "Currency"},
    ]

    chart = {
        "data": {
            "labels": list(AGING_BUCKETS),
            "datasets": [{"name": _("Outstanding Principal"), "values": [aging_totals[x] for x in AGING_BUCKETS]}],
        },
        "type": "bar",
        "height": 240,
    }

    message = _(
        "Customer 360 is a read-only operational view. Aging uses NKT Customer Receivable Due Date "
        "against the selected As-of Date. Card surcharge is shown separately and is never aged as "
        "receivable principal. The existing NKT Customer Statement remains the printable SOA workflow."
    )

    return _columns(), timeline, message, chart, summary
