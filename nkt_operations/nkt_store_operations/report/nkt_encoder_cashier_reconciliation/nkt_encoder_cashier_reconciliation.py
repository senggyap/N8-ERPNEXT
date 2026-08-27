import frappe
from frappe import _
from frappe.utils import flt

from nkt_operations.nkt_store_operations.features.sales.matching import (
    basket_summary_text,
    payment_summary_text,
    warehouse_summary_text,
)


def execute(filters=None):
    filters = frappe._dict(filters or {})
    columns = [
        {"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 220},
        {"label": _("Cashier Entry"), "fieldname": "cashier_sale", "fieldtype": "Link", "options": "NKT Cashier Sale", "width": 140},
        {"label": _("Cash-Basis Payment"), "fieldname": "payment_receipt", "fieldtype": "Link", "options": "NKT Payment Receipt", "width": 150},
        {"label": _("Encoder Order"), "fieldname": "customer_order", "fieldtype": "Link", "options": "NKT Customer Order", "width": 140},
        {"label": _("Cashier Customer"), "fieldname": "cashier_customer", "fieldtype": "Data", "width": 180},
        {"label": _("Encoder Customer"), "fieldname": "encoder_customer", "fieldtype": "Data", "width": 180},
        {"label": _("Cashier Total"), "fieldname": "cashier_total", "fieldtype": "Currency", "width": 120},
        {"label": _("Encoder Total"), "fieldname": "encoder_total", "fieldtype": "Currency", "width": 120},
        {"label": _("Difference"), "fieldname": "difference", "fieldtype": "Currency", "width": 100},
        {"label": _("Cashier Items"), "fieldname": "cashier_items", "fieldtype": "Data", "width": 300},
        {"label": _("Encoder Items"), "fieldname": "encoder_items", "fieldtype": "Data", "width": 300},
        {"label": _("Cashier Warehouses"), "fieldname": "cashier_warehouses", "fieldtype": "Data", "width": 320},
        {"label": _("Encoder Warehouses"), "fieldname": "encoder_warehouses", "fieldtype": "Data", "width": 320},
        {"label": _("Cashier Payment Rows"), "fieldname": "cashier_payment", "fieldtype": "Data", "width": 300},
        {"label": _("Encoder Payment Rows"), "fieldname": "encoder_payment", "fieldtype": "Data", "width": 300},
        {"label": _("Cashier"), "fieldname": "cashier", "fieldtype": "Link", "options": "User", "width": 170},
        {"label": _("Encoder"), "fieldname": "encoder", "fieldtype": "Link", "options": "User", "width": 170},
        {"label": _("Warning / Reason"), "fieldname": "warning", "fieldtype": "Data", "width": 400},
    ]
    if not filters.company or not filters.business_date:
        return columns, []
    rows = []
    cashier_sales = frappe.get_all(
        "NKT Cashier Sale",
        filters={"company": filters.company, "business_date": filters.business_date, "docstatus": 1},
        fields=["name","customer_name","customer","grand_total","cashier","matched_customer_order","linked_payment_receipt","reconciliation_status","reconciliation_warning"],
        order_by="sale_datetime asc",
    )
    matched_orders = set()
    for sale_row in cashier_sales:
        if filters.status and sale_row.reconciliation_status != filters.status:
            continue
        sale = frappe.get_doc("NKT Cashier Sale", sale_row.name)
        order = frappe.get_doc("NKT Customer Order", sale_row.matched_customer_order) if sale_row.matched_customer_order else None
        if order:
            matched_orders.add(order.name)
        rows.append({
            "status": sale_row.reconciliation_status,
            "cashier_sale": sale_row.name,
            "payment_receipt": sale_row.linked_payment_receipt,
            "customer_order": order.name if order else None,
            "cashier_customer": sale_row.customer_name or sale_row.customer,
            "encoder_customer": (order.customer_name or order.customer) if order else "",
            "cashier_total": sale_row.grand_total,
            "encoder_total": order.grand_total if order else 0,
            "difference": flt(sale_row.grand_total) - flt(order.grand_total if order else 0),
            "cashier_items": basket_summary_text(sale.get("items") or []),
            "encoder_items": basket_summary_text(order.get("items") or []) if order else "",
            "cashier_warehouses": warehouse_summary_text(sale.get("items") or []),
            "encoder_warehouses": warehouse_summary_text(order.get("items") or []) if order else "",
            "cashier_payment": payment_summary_text(sale.get("payments") or []),
            "encoder_payment": payment_summary_text(order.get("declared_payments") or []) if order else "",
            "cashier": sale_row.cashier,
            "encoder": order.encoder if order else "",
            "warning": sale_row.reconciliation_warning or "No encoder-side order match found.",
        })
    orders = frappe.get_all(
        "NKT Customer Order",
        filters={"company": filters.company, "order_date": filters.business_date, "docstatus": 1},
        fields=["name","customer_name","customer","grand_total","encoder","cashier_reconciliation_status","cashier_reconciliation_warning"],
        order_by="creation asc",
    )
    for order_row in orders:
        if order_row.name in matched_orders:
            continue
        status = order_row.cashier_reconciliation_status or "Unmatched"
        if filters.status and status != filters.status:
            continue
        order = frappe.get_doc("NKT Customer Order", order_row.name)
        rows.append({
            "status": status,
            "cashier_sale": None,
            "payment_receipt": None,
            "customer_order": order_row.name,
            "cashier_customer": "",
            "encoder_customer": order_row.customer_name or order_row.customer,
            "cashier_total": 0,
            "encoder_total": order_row.grand_total,
            "difference": -flt(order_row.grand_total),
            "cashier_items": "",
            "encoder_items": basket_summary_text(order.get("items") or []),
            "cashier_warehouses": "",
            "encoder_warehouses": warehouse_summary_text(order.get("items") or []),
            "cashier_payment": "",
            "encoder_payment": payment_summary_text(order.get("declared_payments") or []),
            "cashier": "",
            "encoder": order_row.encoder,
            "warning": order_row.cashier_reconciliation_warning or "No cashier-side sale match found.",
        })
    return columns, rows
