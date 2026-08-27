from __future__ import annotations

import hashlib
import json
from datetime import datetime, time, timedelta

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, nowdate

DOCTYPE = "NKT EOD Reconciliation"
VERSION = "V2.0C.6.3"
PRINT_FORMAT = "NKT EOD Reconciliation"
CLIENT_SCRIPT = "NKT EOD Reconciliation Controls V2.0C.6.3"
WORKSPACE = "NKT EOD Control"
TOLERANCE = 0.005

METHODS = ["Cash", "Check", "GCash", "Maya", "Bank Transfer", "Card", "Online"]
METHOD_LABELS = {
    "Cash": "Cash",
    "Check": "Physical Check",
    "GCash": "GCash",
    "Maya": "Maya",
    "Bank Transfer": "Bank Transfer",
    "Card": "Card",
    "Online": "Online / Other Online",
}
ADMIN_ROLES = {"System Manager", "NKT OWNER", "NKT ADMINISTRATOR"}

CLIENT_SCRIPT_BODY = '\nfrappe.ui.form.on("NKT EOD Reconciliation", {\n    async onload(frm) {\n        if (frm.is_new()) {\n            const r = await frappe.call({\n                method: "nkt_operations.nkt_store_operations.features.cashier.reconciliation.get_defaults"\n            });\n            const d = r.message || {};\n            if (!frm.doc.company && d.company) await frm.set_value("company", d.company);\n            if (!frm.doc.business_date && d.business_date) await frm.set_value("business_date", d.business_date);\n        }\n    },\n\n    refresh(frm) {\n        if (frm.doc.docstatus === 0) {\n            frm.set_intro(\n                __("Compare Cashier close(s) and finalized Z-Out(s). Document variances here; do not change either source to force a match."),\n                "blue"\n            );\n            if (!frm.is_new()) {\n                frm.add_custom_button(__("Refresh Reconciliation"), async () => {\n                    const r = await frappe.call({\n                        method: "nkt_operations.nkt_store_operations.features.cashier.reconciliation.refresh_preview",\n                        type: "POST",\n                        args: {name: frm.doc.name},\n                        freeze: true,\n                        freeze_message: __("Refreshing EOD reconciliation...")\n                    });\n                    await frm.reload_doc();\n                    const ready = ((r.message || {}).readiness || {}).ready_to_finalize;\n                    frappe.show_alert({\n                        message: ready ? __("Close data is ready for review.") : __("Close is incomplete. Review open shifts / missing Z-Outs."),\n                        indicator: ready ? "green" : "orange"\n                    });\n                }, __("EOD"));\n            }\n        } else if (frm.doc.docstatus === 1) {\n            frm.set_intro(__("Management reconciliation finalized."), "green");\n            frm.add_custom_button(__("Print Reconciliation"), () => {\n                const url = `/printview?doctype=${encodeURIComponent(frm.doctype)}&name=${encodeURIComponent(frm.doc.name)}&format=${encodeURIComponent("NKT EOD Reconciliation")}&no_letterhead=1&_lang=en`;\n                window.open(url, "_blank");\n            }, __("EOD"));\n        }\n    }\n});\n'
PRINT_HTML = '\n<style>\n@page { size: Letter; margin: 8mm; }\n.print-format { font-family: Arial, Helvetica, sans-serif; font-size: 8pt; color:#202a32; line-height:1.2; }\n.e-title { font-size:15pt; font-weight:700; color:#203847; }\n.e-top { width:100%; border-collapse:collapse; margin-bottom:5px; }\n.e-top td:last-child { text-align:right; }\n.e-scope { border-top:1.5px solid #506979; border-bottom:1px solid #9aabb6; padding:4px 0; margin-bottom:7px; }\n.e-section { font-size:9pt; font-weight:700; color:#243b4b; border-bottom:1px solid #738b9b; margin:7px 0 2px; padding-bottom:2px; }\n.e-table { width:100%; border-collapse:collapse; }\n.e-table th { font-size:7pt; color:#52636f; text-align:left; border-bottom:1px solid #bac5cc; padding:2px 4px; }\n.e-table td { padding:2px 4px; border-bottom:1px dotted #d6dde2; vertical-align:top; }\n.e-table tr:last-child td { border-bottom:0; }\n.e-money { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }\n.e-total td { font-weight:700; border-top:1px solid #748893; }\n.e-warn { font-weight:700; }\n.e-muted { color:#6c7a84; }\n.e-grid { width:100%; border-collapse:separate; border-spacing:12px 0; margin-left:-12px; }\n.e-grid > tbody > tr > td { width:50%; vertical-align:top; padding-left:12px; }\n.e-sign { width:100%; border-collapse:separate; border-spacing:20px 0; margin-top:22px; }\n.e-sign td { width:50%; text-align:center; }\n.e-line { border-top:1px solid #5d6b75; padding-top:3px; }\n</style>\n\n{% set data = json.loads(doc.snapshot_json or \'{}\') %}\n{% set f = data.get(\'financial\', {}) %}\n\n<table class="e-top"><tr>\n<td><div class="e-title">End-of-Day Reconciliation</div></td>\n<td>{{ doc.business_date }} · {{ doc.company }}</td>\n</tr></table>\n\n<div class="e-scope">\n<strong>{{ doc.name }}</strong> · {{ doc.status }} · Reviewed by {{ doc.reviewed_by }} on {{ frappe.utils.format_datetime(doc.reviewed_on) }}\n</div>\n\n<div class="e-section">Cashier vs Z-Out</div>\n<table class="e-table">\n<tr><th>Control</th><th class="e-money">Cashier</th><th class="e-money">Z-Out</th><th class="e-money">Difference</th></tr>\n<tr><td>Sales</td><td class="e-money">{{ frappe.utils.fmt_money(f.get(\'cashier_sales_total\',0), currency=\'PHP\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(f.get(\'zout_sales_total\',0), currency=\'PHP\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(f.get(\'sales_difference\',0), currency=\'PHP\') }}</td></tr>\n<tr><td>Account Sales</td><td class="e-money">{{ frappe.utils.fmt_money(f.get(\'cashier_account_sales\',0), currency=\'PHP\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(f.get(\'zout_account_sales\',0), currency=\'PHP\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(f.get(\'account_sales_difference\',0), currency=\'PHP\') }}</td></tr>\n<tr><td>Account Collections</td><td class="e-money">{{ frappe.utils.fmt_money(f.get(\'cashier_account_collections\',0), currency=\'PHP\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(f.get(\'zout_account_collections\',0), currency=\'PHP\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(f.get(\'collection_difference\',0), currency=\'PHP\') }}</td></tr>\n<tr><td>Card Surcharge (2%)</td><td class="e-money">{{ frappe.utils.fmt_money(f.get(\'cashier_card_surcharge\',0), currency=\'PHP\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(f.get(\'zout_card_surcharge\',0), currency=\'PHP\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(f.get(\'card_surcharge_difference\',0), currency=\'PHP\') }}</td></tr>\n<tr class="e-total"><td>Customer Tender</td><td class="e-money">{{ frappe.utils.fmt_money(f.get(\'cashier_tender_total\',0), currency=\'PHP\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(f.get(\'zout_tender_total\',0), currency=\'PHP\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(f.get(\'tender_difference\',0), currency=\'PHP\') }}</td></tr>\n</table>\n\n<div class="e-section">Tender Comparison</div>\n<table class="e-table">\n<tr><th>Method</th><th class="e-money">Cashier</th><th class="e-money">Z-Out</th><th class="e-money">Difference</th></tr>\n{% for row in data.get(\'tender_comparison\', []) %}\n<tr><td>{{ row.get(\'method\',\'\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(row.get(\'cashier\',0), currency=\'PHP\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(row.get(\'zout\',0), currency=\'PHP\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(row.get(\'difference\',0), currency=\'PHP\') }}</td></tr>\n{% endfor %}\n</table>\n\n<div class="e-section">Cash Drawer Summary</div>\n<table class="e-table">\n<tr><th>Shift</th><th>Cashier</th><th>Status</th><th class="e-money">Expected</th><th class="e-money">Actual</th><th class="e-money">Over / (Short)</th></tr>\n{% for row in data.get(\'drawer_rows\', []) %}\n<tr><td>{{ row.get(\'shift\',\'\') }}</td><td>{{ row.get(\'cashier\',\'\') }}</td><td>{{ row.get(\'status\',\'\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(row.get(\'expected_cash\',0), currency=\'PHP\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(row.get(\'actual_cash\',0), currency=\'PHP\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(row.get(\'over_short\',0), currency=\'PHP\') }}</td></tr>\n{% endfor %}\n<tr class="e-total"><td colspan="5">Total Over / (Short)</td><td class="e-money">{{ frappe.utils.fmt_money(f.get(\'cash_over_short_total\',0), currency=\'PHP\') }}</td></tr>\n</table>\n\n<table class="e-grid"><tr>\n<td>\n<div class="e-section">Cashier Exceptions</div>\n<table class="e-table">\n<tr><th>Status</th><th>Reference</th><th>Customer</th><th class="e-money">Amount</th></tr>\n{% for row in data.get(\'cashier_exceptions\', []) %}\n<tr><td class="e-warn">{{ row.get(\'attention\',\'\') }}</td><td>{{ row.get(\'reference\',\'\') }}</td><td>{{ row.get(\'customer\',\'\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(row.get(\'amount\',0), currency=\'PHP\') }}</td></tr>\n{% else %}<tr><td colspan="4" class="e-muted">None</td></tr>{% endfor %}\n</table>\n</td>\n<td>\n<div class="e-section">Z-Out / Order Exceptions</div>\n<table class="e-table">\n<tr><th>Status</th><th>Reference</th><th>Customer</th><th class="e-money">Amount</th></tr>\n{% for row in data.get(\'encoder_exceptions\', []) %}\n<tr><td class="e-warn">{{ row.get(\'attention\',\'\') }}</td><td>{{ row.get(\'reference\',\'\') }}</td><td>{{ row.get(\'customer\',\'\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(row.get(\'amount\',0), currency=\'PHP\') }}</td></tr>\n{% else %}<tr><td colspan="4" class="e-muted">None</td></tr>{% endfor %}\n</table>\n</td>\n</tr></table>\n\n{% if data.get(\'variance_rows\') %}\n<div class="e-section">Variance Summary</div>\n<table class="e-table">\n<tr><th>Control</th><th class="e-money">Difference</th></tr>\n{% for row in data.get(\'variance_rows\', []) %}\n<tr><td class="e-warn">{{ row.get(\'type\',\'\') }}</td><td class="e-money">{{ frappe.utils.fmt_money(row.get(\'difference\',0), currency=\'PHP\') }}</td></tr>\n{% endfor %}\n</table>\n{% endif %}\n\n<div class="e-section">Management Explanation</div>\n<div>{{ doc.discrepancy_reason or \'No variance/exception explanation required.\' }}</div>\n{% if doc.notes %}<div style="margin-top:4px;">{{ doc.notes }}</div>{% endif %}\n\n<table class="e-sign"><tr>\n<td><div class="e-line">{{ doc.reviewed_by }}<br><span class="e-muted">Reviewed by</span></div></td>\n<td><div class="e-line"><br><span class="e-muted">Owner / Final Review</span></div></td>\n</tr></table>\n'


def _is_admin():
    return bool(set(frappe.get_roles()) & ADMIN_ROLES)


def _money(value):
    return round(flt(value), 2)


def _norm_method(value):
    value = (value or "").strip()
    aliases = {
        "Cheque": "Check",
        "CC": "Card",
        "Credit Card": "Card",
        "Other Online": "Online",
        "Online Payment": "Online",
    }
    value = aliases.get(value, value)
    return value if value in METHODS else "Online"


def _day_window(business_date):
    day = getdate(business_date)
    start = datetime.combine(day, time.min)
    end = datetime.combine(day + timedelta(days=1), time.min)
    return day, start, end


def _cashier_shifts(company, business_date):
    day, start, end = _day_window(business_date)
    return frappe.db.sql(
        """
        SELECT
            name, cashier, company, settlement_location,
            shift_start, shift_end, status, docstatus,
            opening_cash, total_cash_in, total_cash_out,
            expected_cash, actual_cash_count, over_short,
            custom_nkt_v19_invalid_role_shift,
            approved_by, approved_on
        FROM `tabNKT Cashier Shift`
        WHERE company = %s
          AND shift_start >= %s
          AND shift_start < %s
          AND COALESCE(status, '') != 'Cancelled'
          AND COALESCE(custom_nkt_v19_invalid_role_shift, 0) = 0
        ORDER BY shift_start, name
        """,
        (company, start, end),
        as_dict=True,
    )


def _cashier_sales(company, business_date):
    return frappe.db.sql(
        """
        SELECT
            name, creation, business_date, cashier_shift,
            customer, customer_name, grand_total, total_account_charge,
            reconciliation_status, matched_customer_order, status
        FROM `tabNKT Cashier Sale`
        WHERE company = %s
          AND business_date = %s
          AND docstatus = 1
          AND COALESCE(status, '') != 'Cancelled'
        ORDER BY creation, name
        """,
        (company, getdate(business_date)),
        as_dict=True,
    )


def _cashier_customer_movements(company, business_date):
    day, start, end = _day_window(business_date)
    return frappe.db.sql(
        """
        SELECT
            m.name, m.posting_datetime, m.cashier_shift,
            m.customer, m.movement_type, m.payment_method,
            m.amount, m.settlement_amount, m.card_surcharge, m.source_doctype, m.source_name,
            m.reference_number
        FROM `tabNKT Cashier Movement` m
        INNER JOIN `tabNKT Cashier Shift` s ON s.name = m.cashier_shift
        WHERE s.company = %s
          AND m.docstatus = 1
          AND m.status = 'Posted'
          AND m.direction = 'In'
          AND m.posting_datetime >= %s
          AND m.posting_datetime < %s
          AND m.movement_type IN ('Customer Order Payment', 'Customer Account Collection')
        ORDER BY m.posting_datetime, m.name
        """,
        (company, start, end),
        as_dict=True,
    )


def _finalized_zouts(company, business_date):
    return frappe.get_all(
        "NKT Encoder Z-Out",
        filters={"company": company, "business_date": getdate(business_date), "docstatus": 1},
        fields=["name","encoder","business_date","finalized_by","finalized_on","snapshot_hash","snapshot_json"],
        order_by="encoder, finalized_on, name",
    )


def _encoders_with_activity(company, business_date):
    rows = frappe.db.sql(
        """
        SELECT DISTINCT encoder
        FROM `tabNKT Customer Order`
        WHERE company = %s
          AND order_date = %s
          AND docstatus = 1
          AND COALESCE(status, '') != 'Cancelled'
          AND COALESCE(custom_nkt_archived_from_operations, 0) = 0
          AND COALESCE(encoder, '') != ''
        ORDER BY encoder
        """,
        (company, getdate(business_date)),
        as_dict=True,
    )
    return [row.encoder for row in rows]


def _encoder_order_exceptions(company, business_date):
    return frappe.db.sql(
        """
        SELECT
            name, creation, encoder, customer, customer_name,
            grand_total, status, payment_status,
            cashier_reconciliation_status,
            custom_nkt_account_credit_status
        FROM `tabNKT Customer Order`
        WHERE company = %s
          AND order_date = %s
          AND docstatus = 1
          AND COALESCE(status, '') != 'Cancelled'
          AND COALESCE(custom_nkt_archived_from_operations, 0) = 0
          AND (
                COALESCE(cashier_reconciliation_status, '') NOT LIKE 'Matched%%'
                OR custom_nkt_account_credit_status = 'Pending Approval'
              )
        ORDER BY creation, name
        """,
        (company, getdate(business_date)),
        as_dict=True,
    )


def _aggregate_zouts(zouts):
    tender = {method: 0.0 for method in METHODS}
    summary = {"gross_sales": 0.0, "account_sales": 0.0, "account_collections": 0.0, "card_surcharge_total": 0.0}
    rows = []
    by_encoder = {}
    for row in zouts:
        try:
            data = json.loads(row.snapshot_json or "{}")
        except Exception:
            data = {}
        s = data.get("summary", {})
        t = data.get("tender_totals", {})
        summary["gross_sales"] += flt(s.get("gross_sales"))
        summary["account_sales"] += flt(s.get("account_sales"))
        summary["account_collections"] += flt(s.get("account_collections"))
        summary["card_surcharge_total"] += flt(s.get("card_surcharge_total"))
        for method in METHODS:
            tender[method] += flt(t.get(method))
        by_encoder.setdefault(row.encoder, []).append(row.name)
        rows.append({
            "name": row.name,
            "encoder": row.encoder,
            "finalized_on": str(row.finalized_on or ""),
            "snapshot_hash": row.snapshot_hash or "",
        })
    return {
        "summary": {k: _money(v) for k, v in summary.items()},
        "tender": {k: _money(v) for k, v in tender.items()},
        "rows": rows,
        "by_encoder": by_encoder,
    }


def build_reconciliation_data(company, business_date):
    day = getdate(business_date)
    shifts = _cashier_shifts(company, day)
    sales = _cashier_sales(company, day)
    movements = _cashier_customer_movements(company, day)
    zouts = _finalized_zouts(company, day)
    active_encoders = _encoders_with_activity(company, day)
    encoder_exceptions_raw = _encoder_order_exceptions(company, day)

    reviewed_statuses = {"Reviewed / Closed", "Closed"}
    reviewed_shifts = [r for r in shifts if r.status in reviewed_statuses and cint(r.docstatus) == 1]
    unready_shifts = [r for r in shifts if not (r.status in reviewed_statuses and cint(r.docstatus) == 1)]

    cashier_tender = {method: 0.0 for method in METHODS}
    cashier_account_collections = 0.0
    cashier_card_surcharge = 0.0
    for row in movements:
        method = _norm_method(row.payment_method)
        cashier_tender[method] += flt(row.amount)
        if method == "Card":
            cashier_card_surcharge += flt(row.card_surcharge)
        if row.movement_type == "Customer Account Collection":
            principal = flt(row.settlement_amount)
            if principal <= TOLERANCE:
                principal = max(flt(row.amount) - flt(row.card_surcharge), 0)
            cashier_account_collections += principal
    cashier_tender = {k: _money(v) for k, v in cashier_tender.items()}

    cashier_sales_total = _money(sum(flt(r.grand_total) for r in sales))
    cashier_account_sales = _money(sum(flt(r.total_account_charge) for r in sales))
    cashier_account_collections = _money(cashier_account_collections)
    cashier_card_surcharge = _money(cashier_card_surcharge)

    cashier_exceptions = []
    for row in sales:
        status = (row.reconciliation_status or "").strip()
        if status.startswith("Matched"):
            continue
        label = "Multiple Possible Matches" if status == "Ambiguous" else ("No Encoder Match" if status == "Unmatched" else (status or "Reconciliation Review Needed"))
        cashier_exceptions.append({
            "side":"Cashier","reference":row.name,"datetime":str(row.creation),
            "customer":row.customer_name or row.customer,"amount":_money(row.grand_total),
            "attention":label,
        })

    encoder_exceptions = []
    for row in encoder_exceptions_raw:
        labels = []
        recon = (row.cashier_reconciliation_status or "").strip()
        if recon == "Ambiguous":
            labels.append("Multiple Possible Matches")
        elif recon == "Unmatched":
            labels.append("No Cashier Match")
        elif recon and not recon.startswith("Matched"):
            labels.append("Reconciliation Review Needed")
        if row.custom_nkt_account_credit_status == "Pending Approval":
            labels.append("Pending Credit Approval")
        encoder_exceptions.append({
            "side":"Z-Out / Order","reference":row.name,"datetime":str(row.creation),
            "encoder":row.encoder or "","customer":row.customer_name or row.customer,
            "amount":_money(row.grand_total),"attention":" / ".join(labels),
        })

    zout = _aggregate_zouts(zouts)
    finalized_encoders = set(zout["by_encoder"])
    missing_zout_encoders = [u for u in active_encoders if u not in finalized_encoders]
    duplicate_zout_encoders = {u:names for u,names in zout["by_encoder"].items() if len(names) > 1}

    drawer_rows = []
    cash_over_short_total = 0.0
    for row in shifts:
        cash_over_short_total += flt(row.over_short)
        drawer_rows.append({
            "shift":row.name,"cashier":row.cashier,"location":row.settlement_location or "",
            "status":row.status or "","start":str(row.shift_start or ""),"end":str(row.shift_end or ""),
            "expected_cash":_money(row.expected_cash),"actual_cash":_money(row.actual_cash_count),
            "over_short":_money(row.over_short),"reviewed_by":row.approved_by or "",
        })
    cash_over_short_total = _money(cash_over_short_total)

    zout_sales_total = zout["summary"]["gross_sales"]
    zout_account_sales = zout["summary"]["account_sales"]
    zout_account_collections = zout["summary"]["account_collections"]
    zout_card_surcharge = zout["summary"]["card_surcharge_total"]
    cashier_tender_total = _money(sum(cashier_tender.values()))
    zout_tender_total = _money(sum(zout["tender"].values()))

    financial = {
        "cashier_sales_total":cashier_sales_total,
        "zout_sales_total":zout_sales_total,
        "sales_difference":_money(cashier_sales_total - zout_sales_total),
        "cashier_account_sales":cashier_account_sales,
        "zout_account_sales":zout_account_sales,
        "account_sales_difference":_money(cashier_account_sales - zout_account_sales),
        "cashier_account_collections":cashier_account_collections,
        "zout_account_collections":zout_account_collections,
        "collection_difference":_money(cashier_account_collections - zout_account_collections),
        "cashier_card_surcharge":cashier_card_surcharge,
        "zout_card_surcharge":zout_card_surcharge,
        "card_surcharge_difference":_money(cashier_card_surcharge - zout_card_surcharge),
        "cashier_tender_total":cashier_tender_total,
        "zout_tender_total":zout_tender_total,
        "tender_difference":_money(cashier_tender_total - zout_tender_total),
        "cash_over_short_total":cash_over_short_total,
    }

    tender_comparison = []
    for method in METHODS:
        cashier_amount = cashier_tender[method]
        zout_amount = zout["tender"][method]
        tender_comparison.append({
            "method":METHOD_LABELS[method],"cashier":cashier_amount,"zout":zout_amount,
            "difference":_money(cashier_amount - zout_amount),
        })

    blockers = []
    if unready_shifts:
        blockers.append(f"{len(unready_shifts)} Cashier shift(s) are still open or awaiting management review.")
    if missing_zout_encoders:
        blockers.append("Missing finalized Z-Out for: " + ", ".join(missing_zout_encoders))
    if duplicate_zout_encoders:
        blockers.append("More than one finalized Z-Out exists for: " + ", ".join(sorted(duplicate_zout_encoders)))
    if active_encoders and not zouts:
        blockers.append("No finalized Z-Out is available for the business date.")

    variance_rows = []
    for label, key in [
        ("Sales","sales_difference"),
        ("Account Sales","account_sales_difference"),
        ("Account Collections","collection_difference"),
        ("Card Surcharge","card_surcharge_difference"),
        ("Customer Tender","tender_difference"),
    ]:
        if abs(flt(financial[key])) > TOLERANCE:
            variance_rows.append({"type":label,"difference":financial[key]})
    for row in tender_comparison:
        if abs(flt(row["difference"])) > TOLERANCE:
            variance_rows.append({"type":"Tender - " + row["method"],"difference":row["difference"]})
    if abs(cash_over_short_total) > TOLERANCE:
        variance_rows.append({"type":"Cash Drawer Over / (Short)","difference":cash_over_short_total})

    exception_count = len(cashier_exceptions) + len(encoder_exceptions) + len(variance_rows) + len(blockers)

    return {
        "version":VERSION,
        "scope":{"company":company,"business_date":str(day),"generated_on":str(now_datetime())},
        "readiness":{
            "ready_to_finalize":not blockers,"blockers":blockers,
            "cashier_shift_count":len(shifts),"reviewed_shift_count":len(reviewed_shifts),
            "open_shift_count":len(unready_shifts),"expected_zout_count":len(active_encoders),
            "zout_count":len(zouts),"encoders_with_activity":active_encoders,
            "missing_zout_encoders":missing_zout_encoders,"duplicate_zout_encoders":duplicate_zout_encoders,
        },
        "financial":financial,
        "cashier_tender":cashier_tender,
        "zout_tender":zout["tender"],
        "tender_comparison":tender_comparison,
        "drawer_rows":drawer_rows,
        "zout_rows":zout["rows"],
        "cashier_exceptions":cashier_exceptions,
        "encoder_exceptions":encoder_exceptions,
        "variance_rows":variance_rows,
        "exception_count":exception_count,
    }


def _apply_fields(doc, data):
    r=data["readiness"]; f=data["financial"]
    doc.cashier_shift_count=r["cashier_shift_count"]; doc.reviewed_shift_count=r["reviewed_shift_count"]
    doc.open_shift_count=r["open_shift_count"]; doc.zout_count=r["zout_count"]; doc.expected_zout_count=r["expected_zout_count"]
    doc.cash_over_short_total=f["cash_over_short_total"]; doc.exception_count=data["exception_count"]
    doc.cashier_sales_total=f["cashier_sales_total"]; doc.zout_sales_total=f["zout_sales_total"]; doc.sales_difference=f["sales_difference"]
    doc.cashier_tender_total=f["cashier_tender_total"]; doc.zout_tender_total=f["zout_tender_total"]; doc.tender_difference=f["tender_difference"]
    doc.cashier_account_sales=f["cashier_account_sales"]; doc.zout_account_sales=f["zout_account_sales"]; doc.account_sales_difference=f["account_sales_difference"]
    doc.cashier_account_collections=f["cashier_account_collections"]; doc.zout_account_collections=f["zout_account_collections"]; doc.collection_difference=f["collection_difference"]


def validate_reconciliation(doc):
    if not _is_admin():
        frappe.throw(_("Only Owner/Admin roles may use EOD Reconciliation."))
    if not doc.company or not doc.business_date:
        frappe.throw(_("Company and Business Date are required."))
    if getdate(doc.business_date) > getdate(nowdate()):
        frappe.throw(_("Business Date cannot be in the future."))


def finalize_reconciliation(doc):
    validate_reconciliation(doc)
    data=build_reconciliation_data(doc.company, doc.business_date)
    _apply_fields(doc,data)
    if not data["readiness"]["ready_to_finalize"]:
        frappe.throw(_("EOD Reconciliation cannot be finalized yet:<br>{0}").format("<br>".join(data["readiness"]["blockers"])))
    has_variance_or_exception=bool(data["variance_rows"] or data["cashier_exceptions"] or data["encoder_exceptions"])
    if has_variance_or_exception and len((doc.discrepancy_reason or "").strip()) < 10:
        frappe.throw(_("A variance/exception explanation of at least 10 characters is required."))
    payload=json.dumps(data,sort_keys=True,default=str,separators=(",",":"))
    doc.snapshot_json=payload
    doc.snapshot_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest()
    doc.reviewed_by=frappe.session.user
    doc.reviewed_on=now_datetime()
    doc.status="Reviewed - With Variance / Exceptions" if has_variance_or_exception else "Reviewed - Balanced"


@frappe.whitelist()
def refresh_preview(name):
    doc=frappe.get_doc(DOCTYPE,name); doc.check_permission("write")
    if doc.docstatus != 0:
        frappe.throw(_("Only a Draft reconciliation can refresh."))
    validate_reconciliation(doc)
    data=build_reconciliation_data(doc.company, doc.business_date)
    _apply_fields(doc,data); doc.save()
    return {"readiness":data["readiness"],"financial":data["financial"],"tender_comparison":data["tender_comparison"],"exception_count":data["exception_count"]}


@frappe.whitelist()
def get_defaults():
    company=frappe.db.get_value("Company",{},"name",order_by="creation asc")
    return {"company":company,"business_date":str(getdate(nowdate()))}


def _ensure_client_script():
    values={"dt":DOCTYPE,"view":"Form","enabled":1,"script":CLIENT_SCRIPT_BODY}
    if frappe.db.exists("Client Script",CLIENT_SCRIPT):
        doc=frappe.get_doc("Client Script",CLIENT_SCRIPT); doc.update(values); doc.flags.ignore_permissions=True; doc.save(ignore_permissions=True)
    else:
        doc=frappe.get_doc({"doctype":"Client Script","name":CLIENT_SCRIPT,**values}); doc.flags.ignore_permissions=True; doc.insert(ignore_permissions=True)


def _ensure_print_format():
    values={"doc_type":DOCTYPE,"module":"NKT Store Operations","standard":"No","custom_format":1,"print_format_type":"Jinja","html":PRINT_HTML,"disabled":0}
    if frappe.db.exists("Print Format",PRINT_FORMAT):
        doc=frappe.get_doc("Print Format",PRINT_FORMAT); doc.update(values); doc.flags.ignore_permissions=True; doc.save(ignore_permissions=True)
    else:
        doc=frappe.get_doc({"doctype":"Print Format","name":PRINT_FORMAT,**values}); doc.flags.ignore_permissions=True; doc.insert(ignore_permissions=True)
    if frappe.get_meta("DocType").has_field("default_print_format"):
        frappe.db.set_value("DocType",DOCTYPE,"default_print_format",PRINT_FORMAT,update_modified=False)


def _ensure_workspace():
    old=getattr(frappe.flags,"in_patch",False); frappe.flags.in_patch=True
    try:
        doc=frappe.get_doc("Workspace",WORKSPACE) if frappe.db.exists("Workspace",WORKSPACE) else frappe.new_doc("Workspace")
        doc.title=WORKSPACE; doc.label=WORKSPACE; doc.module="NKT Store Operations"; doc.app="nkt_operations"
        doc.type="Workspace"; doc.icon="check-square"; doc.indicator_color="orange"; doc.public=1; doc.is_hidden=0; doc.hide_custom=0
        doc.content=json.dumps([
            {"id":"e1","type":"header","data":{"text":"<span class=\"h4\">End-of-Day Control</span>","col":12}},
            {"id":"e2","type":"shortcut","data":{"shortcut_name":"New EOD Reconciliation","col":4}},
            {"id":"e3","type":"shortcut","data":{"shortcut_name":"EOD History","col":4}},
            {"id":"e4","type":"shortcut","data":{"shortcut_name":"Cashier Shifts","col":4}},
        ])
        doc.set("roles",[])
        for role in ("System Manager","NKT OWNER","NKT ADMINISTRATOR"): doc.append("roles",{"role":role})
        doc.set("shortcuts",[])
        for label,dt,view in [
            ("New EOD Reconciliation",DOCTYPE,"New"),("EOD History",DOCTYPE,"List"),
            ("Cashier Shifts","NKT Cashier Shift","List"),("Encoder Z-Outs","NKT Encoder Z-Out","List")
        ]:
            doc.append("shortcuts",{"type":"DocType","link_to":dt,"doc_view":view,"label":label})
        doc.flags.ignore_permissions=True
        doc.insert(ignore_permissions=True) if doc.is_new() else doc.save(ignore_permissions=True)
    finally:
        frappe.flags.in_patch=old


@frappe.whitelist()
def install():
    frappe.set_user("Administrator")
    if not frappe.db.exists("DocType",DOCTYPE):
        frappe.throw(_("NKT EOD Reconciliation DocType is missing. Run migrate first."))
    if not frappe.get_attr("nkt_operations.nkt_store_operations.features.cashier.shift_report.verify")().get("passed"):
        frappe.throw(_("C6.1 baseline is not passing."))
    if not frappe.get_attr("nkt_operations.nkt_store_operations.features.cashier.encoder_zout.verify")().get("passed"):
        frappe.throw(_("C6.2 baseline is not passing."))
    _ensure_client_script(); _ensure_print_format(); _ensure_workspace()
    frappe.clear_cache(); frappe.db.commit()
    return {"version":VERSION,"installed":True,"doctype":DOCTYPE,"workspace":WORKSPACE,"business_records_created":False}


@frappe.whitelist()
def verify():
    errors=[]
    if not frappe.db.exists("DocType",DOCTYPE): errors.append("NKT EOD Reconciliation DocType missing.")
    pf=frappe.db.get_value("Print Format",PRINT_FORMAT,["doc_type","disabled","html"],as_dict=True)
    if not pf or pf.doc_type != DOCTYPE or cint(pf.disabled): errors.append("EOD Reconciliation print format missing/disabled.")
    else:
        for text in ["Cashier vs Z-Out","Tender Comparison","Cash Drawer Summary","Cashier Exceptions","Z-Out / Order Exceptions","Management Explanation"]:
            if text not in (pf.html or ""): errors.append("Print format missing: "+text)
    script=frappe.db.get_value("Client Script",CLIENT_SCRIPT,"script") or ""
    if "Refresh Reconciliation" not in script or "Print Reconciliation" not in script: errors.append("EOD client controls missing.")
    c61=frappe.get_attr("nkt_operations.nkt_store_operations.features.cashier.shift_report.verify")()
    c62=frappe.get_attr("nkt_operations.nkt_store_operations.features.cashier.encoder_zout.verify")()
    if not c61.get("passed"): errors.append("C6.1 regression.")
    if not c62.get("passed"): errors.append("C6.2 regression.")
    preview=None
    company=frappe.db.get_value("Company",{},"name",order_by="creation asc")
    if company: preview=build_reconciliation_data(company,getdate(nowdate()))
    return {
        "version":VERSION,
        "c6_1_regression_passed":bool(c61.get("passed")),
        "c6_2_regression_passed":bool(c62.get("passed")),
        "live_preview":{
            "readiness":(preview or {}).get("readiness"),
            "financial":(preview or {}).get("financial"),
            "tender_comparison":(preview or {}).get("tender_comparison"),
            "cashier_exception_count":len((preview or {}).get("cashier_exceptions",[])),
            "encoder_exception_count":len((preview or {}).get("encoder_exceptions",[])),
        } if preview else None,
        "errors":errors,
        "passed":not errors,
    }
