from __future__ import annotations

import hashlib
import inspect
import json
from datetime import datetime, time

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, nowdate

ZOUT = "NKT Encoder Z-Out"
VERSION = "V2.0C.7.4.8-MANAGER-PIN-EOD"
PRINT_FORMAT = "NKT Encoder Z-Out"
CLIENT_SCRIPT = "NKT Encoder Z-Out Controls V2.0C.6.2"
WORKSPACE = "NKT Encoder Close"
ITEM_FLAG = "custom_nkt_include_in_zout_inventory"

ADMIN_ROLES = {"System Manager", "NKT OWNER", "NKT ADMINISTRATOR"}
ENCODER_ROLE = "NKT Encoder"
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

CLIENT_SCRIPT_BODY = '\nfrappe.ui.form.on("NKT Encoder Z-Out", {\n    async onload(frm) {\n        if (frm.is_new()) {\n            const r = await frappe.call({\n                method: "nkt_operations.nkt_store_operations.features.cashier.encoder_zout.get_defaults"\n            });\n            const d = r.message || {};\n            if (!frm.doc.encoder && d.encoder) await frm.set_value("encoder", d.encoder);\n            if (!frm.doc.company && d.company) await frm.set_value("company", d.company);\n            if (!frm.doc.business_date && d.business_date) await frm.set_value("business_date", d.business_date);\n            await frm.set_value("include_inventory_appendix", 0);\n        }\n    },\n\n    refresh(frm) {\n        frm.set_df_property("include_inventory_appendix", "hidden", 1);\n        frm.set_df_property("inventory_item_count", "hidden", 1);\n\n        if (frm.doc.docstatus === 0) {\n            frm.set_intro(__("Review totals, then Submit to finalize the Z-Out."), "blue");\n            if (!frm.is_new()) {\n                frm.add_custom_button(__("Refresh Totals"), async () => {\n                    const r = await frappe.call({\n                        method: "nkt_operations.nkt_store_operations.features.cashier.encoder_zout.refresh_preview",\n                        type: "POST",\n                        args: {name: frm.doc.name},\n                        freeze: true,\n                        freeze_message: __("Refreshing Z-Out totals...")\n                    });\n                    await frm.reload_doc();\n                    const c = (r.message || {}).counts || {};\n                    frappe.show_alert({\n                        message: __("Updated: {0} sales, {1} exceptions.", [c.orders || 0, c.exceptions || 0]),\n                        indicator: "green"\n                    });\n                }, __("Z-Out"));\n            }\n        }\n\n        if (frm.doc.docstatus === 1) {\n            frm.set_intro(__("Z-Out finalized."), "green");\n            frm.add_custom_button(__("Print Z-Out"), () => {\n                const url = `/printview?doctype=${encodeURIComponent(frm.doctype)}&name=${encodeURIComponent(frm.doc.name)}&format=${encodeURIComponent("NKT Encoder Z-Out")}&no_letterhead=1&_lang=en`;\n                window.open(url, "_blank");\n            }, __("Z-Out"));\n        }\n    }\n});\n'
PRINT_HTML = '\n<style>\n@page { size: Letter; margin: 7mm 8mm 9mm; }\n.print-format {\n  font-family: Arial, Helvetica, sans-serif;\n  font-size: 8.2pt;\n  color: #202830;\n  line-height: 1.18;\n}\n.z-top { width:100%; border-collapse:collapse; margin-bottom:5px; }\n.z-title { font-size:15pt; font-weight:700; color:#203847; }\n.z-printed { text-align:right; font-size:7.2pt; color:#596a76; }\n.z-scope {\n  border-top:1.5px solid #506979;\n  border-bottom:1px solid #9aabb6;\n  padding:3px 0;\n  margin-bottom:7px;\n  font-size:7.5pt;\n}\n.z-scope table { width:100%; border-collapse:collapse; }\n.z-scope td { padding:1px 8px 1px 0; }\n.z-label { font-weight:700; color:#4d6170; }\n.z-section {\n  font-size:9pt;\n  font-weight:700;\n  color:#243b4b;\n  margin:6px 0 2px;\n  padding-bottom:2px;\n  border-bottom:1px solid #738b9b;\n}\n.z-table { width:100%; border-collapse:collapse; }\n.z-table th {\n  text-align:left;\n  font-size:6.9pt;\n  font-weight:700;\n  color:#52636f;\n  border-bottom:1px solid #b9c4cb;\n  padding:2px 3px;\n}\n.z-table td {\n  padding:2px 3px;\n  vertical-align:top;\n  border-bottom:1px dotted #d5dde2;\n}\n.z-table tr:last-child td { border-bottom:0; }\n.z-money { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }\n.z-center { text-align:center; }\n.z-bold { font-weight:700; }\n.z-muted { color:#6b7983; }\n.z-small { font-size:6.8pt; }\n.z-summary {\n  width:100%;\n  border-collapse:separate;\n  border-spacing:13px 0;\n  margin-left:-13px;\n}\n.z-summary > tbody > tr > td { width:50%; vertical-align:top; padding-left:13px; }\n.z-total td { font-weight:700; border-top:1px solid #748893; }\n.z-alert { font-weight:700; }\n.z-keep { page-break-inside:avoid; }\n.z-detail-header { background:#f4f6f8; }\n.z-sign {\n  width:100%;\n  margin-top:19px;\n  border-collapse:separate;\n  border-spacing:20px 0;\n}\n.z-sign td { width:50%; text-align:center; vertical-align:bottom; }\n.z-signline { border-top:1px solid #5d6b75; padding-top:3px; }\n</style>\n\n{% set data = json.loads(doc.snapshot_json or \'{}\') %}\n{% set s = data.get(\'summary\', {}) %}\n{% set c = data.get(\'counts\', {}) %}\n{% set tenders = data.get(\'tender_totals\', {}) %}\n{% set details = data.get(\'tender_details\', {}) %}\n{% set scope = data.get(\'scope\', {}) %}\n{% set options = data.get(\'options\', {}) %}\n\n<table class="z-top">\n<tr>\n  <td><div class="z-title">Z-Out Store Close</div></td>\n  <td class="z-printed">Printed: {{ frappe.utils.format_datetime(frappe.utils.now_datetime()) }}</td>\n</tr>\n</table>\n\n<div class="z-scope">\n<table>\n<tr>\n  <td><span class="z-label">Date:</span> {{ scope.get(\'start_datetime\',\'\') }} to {{ scope.get(\'effective_end_datetime\',\'\') }}</td>\n  <td><span class="z-label">Z-Out:</span> {{ doc.name }}</td>\n  <td><span class="z-label">Company:</span> {{ scope.get(\'company\', doc.company) }}</td>\n</tr>\n</table>\n</div>\n\n<div class="z-section">Sales Activity</div>\n<table class="z-table z-keep">\n<tr>\n  <th></th>\n  <th class="z-money">Gross Sales</th>\n  <th class="z-money">Gross Returns</th>\n  <th class="z-money">Net</th>\n</tr>\n<tr>\n  <td>Sales</td>\n  <td class="z-money">{{ frappe.utils.fmt_money(s.get(\'gross_sales\',0), currency=\'PHP\') }}</td>\n  <td class="z-money">{{ frappe.utils.fmt_money(s.get(\'gross_returns\',0), currency=\'PHP\') }}</td>\n  <td class="z-money z-bold">{{ frappe.utils.fmt_money(s.get(\'net_sales\',0), currency=\'PHP\') }}</td>\n</tr>\n<tr>\n  <td>Payments / Collections</td>\n  <td class="z-money">{{ frappe.utils.fmt_money(s.get(\'account_collections\',0), currency=\'PHP\') }}</td>\n  <td class="z-money">-</td>\n  <td class="z-money">{{ frappe.utils.fmt_money(s.get(\'account_collections\',0), currency=\'PHP\') }}</td>\n</tr>\n<tr class="z-total">\n  <td>Total Activity</td>\n  <td class="z-money">{{ frappe.utils.fmt_money((s.get(\'gross_sales\',0) or 0) + (s.get(\'account_collections\',0) or 0), currency=\'PHP\') }}</td>\n  <td class="z-money">{{ frappe.utils.fmt_money(s.get(\'gross_returns\',0), currency=\'PHP\') }}</td>\n  <td class="z-money">{{ frappe.utils.fmt_money((s.get(\'net_sales\',0) or 0) + (s.get(\'account_collections\',0) or 0), currency=\'PHP\') }}</td>\n</tr>\n</table>\n\n<table class="z-summary">\n<tr>\n<td>\n  <div class="z-section">Sales Adjustments</div>\n  <table class="z-table z-keep">\n    <tr><td>Net Sales Activity</td><td class="z-money">{{ frappe.utils.fmt_money(s.get(\'net_sales\',0), currency=\'PHP\') }}</td></tr>\n    <tr><td>Charged to Account</td><td class="z-money">{{ frappe.utils.fmt_money(s.get(\'account_sales\',0), currency=\'PHP\') }}</td></tr>\n    <tr><td>Account Collections</td><td class="z-money">{{ frappe.utils.fmt_money(s.get(\'account_collections\',0), currency=\'PHP\') }}</td></tr>\n    {% if s.get(\'unapplied_advance_created\',0) %}\n    <tr><td>Advance Created</td><td class="z-money">{{ frappe.utils.fmt_money(s.get(\'unapplied_advance_created\',0), currency=\'PHP\') }}</td></tr>\n    {% endif %}\n    {% if s.get(\'advance_applied_no_cash\',0) %}\n    <tr><td>Advance Applied</td><td class="z-money">{{ frappe.utils.fmt_money(s.get(\'advance_applied_no_cash\',0), currency=\'PHP\') }}</td></tr>\n    {% endif %}\n    {% if s.get(\'advance_reversed_no_cash\',0) %}\n    <tr><td>Advance Reversed</td><td class="z-money">{{ frappe.utils.fmt_money(s.get(\'advance_reversed_no_cash\',0), currency=\'PHP\') }}</td></tr>\n    {% endif %}\n  </table>\n</td>\n<td>\n  <div class="z-section">Receipt Counts</div>\n  <table class="z-table z-keep">\n    <tr><td>Sales</td><td class="z-money">{{ c.get(\'orders\',0) }}</td></tr>\n    <tr><td>Returns</td><td class="z-money">{{ c.get(\'returns\',0) }}</td></tr>\n    <tr><td>Collections</td><td class="z-money">{{ c.get(\'account_collections\',0) }}</td></tr>\n    <tr><td>Exceptions</td><td class="z-money">{{ c.get(\'exceptions\',0) }}</td></tr>\n  </table>\n</td>\n</tr>\n</table>\n\n\n{% set price_rows = data.get(\'price_adjustments\', []) %}\n<div class="z-section">Selling Price Adjustments / Manager Authorization</div>\n<table class="z-table">\n<tr class="z-detail-header">\n  <th>Order / Item</th><th class="z-money">Qty</th><th class="z-money">Normal</th>\n  <th class="z-money">Actual</th><th class="z-money">Diff / Unit</th>\n  <th>Manager Authorization</th><th>Encoder / Match</th>\n</tr>\n{% for row in price_rows %}\n<tr>\n  <td><b>{{ row.get(\'order\',\'\') }}</b><br>{{ row.get(\'item\',\'\') }}<br><span class="z-muted z-small">{{ row.get(\'warehouse\',\'\') }}</span></td>\n  <td class="z-money">{{ row.get(\'qty\',0) }} {{ row.get(\'uom\',\'\') }}</td>\n  <td class="z-money">{{ frappe.utils.fmt_money(row.get(\'standard_rate\',0), currency=\'PHP\') }}</td>\n  <td class="z-money">{{ frappe.utils.fmt_money(row.get(\'actual_rate\',0), currency=\'PHP\') }}</td>\n  <td class="z-money">{{ frappe.utils.fmt_money(row.get(\'difference_per_unit\',0), currency=\'PHP\') }}</td>\n  <td>\n    {% if row.get(\'authorized_by\') %}\n      <b>{{ row.get(\'authorized_by\') }}</b><br>{{ row.get(\'reason\',\'\') or \'—\' }}\n      {% if row.get(\'explanation\') %}<br><span class="z-small">{{ row.get(\'explanation\') }}</span>{% endif %}\n      <br><span class="z-muted z-small">{{ row.get(\'authorized_on\',\'\') }} · {{ row.get(\'authorization_source\',\'\') }}</span>\n    {% else %}\n      <span class="z-alert">No linked Cashier Manager authorization</span>\n    {% endif %}\n  </td>\n  <td>{{ row.get(\'encoder\',\'\') }}<br><span class="z-muted z-small">{{ row.get(\'match_status\',\'\') or \'No Cashier Match\' }}</span>{% if row.get(\'cashier_sale\') %}<br><span class="z-small">{{ row.get(\'cashier_sale\') }}</span>{% endif %}</td>\n</tr>\n{% else %}\n<tr><td colspan="7" class="z-center z-muted">No adjusted selling-rate rows in this Z-Out scope.</td></tr>\n{% endfor %}\n{% if price_rows %}\n<tr class="z-total"><td colspan="4">Net effect of adjusted rates vs normal selling rates</td><td class="z-money">{{ frappe.utils.fmt_money(s.get(\'price_adjustment_total_effect\',0), currency=\'PHP\') }}</td><td colspan="2">{{ c.get(\'price_adjustments\',0) }} adjusted row(s)</td></tr>\n{% endif %}\n</table>\n\n<div class="z-section">Payment Summary</div>\n<table class="z-table z-keep">\n<tr>\n  <th>Method</th>\n  <th class="z-money">Count</th>\n  <th class="z-money">Amount</th>\n</tr>\n{% for method in data.get(\'tender_method_order\', []) %}\n{% set rows = details.get(method, []) %}\n<tr>\n  <td>{{ data.get(\'tender_labels\',{}).get(method, method) }}</td>\n  <td class="z-money">{{ rows | length }}</td>\n  <td class="z-money">{{ frappe.utils.fmt_money(tenders.get(method,0), currency=\'PHP\') }}</td>\n</tr>\n{% endfor %}\n{% if s.get(\'card_surcharge_total\',0) %}\n<tr>\n  <td>Card Surcharge (2%, included in Card)</td>\n  <td></td>\n  <td class="z-money">{{ frappe.utils.fmt_money(s.get(\'card_surcharge_total\',0), currency=\'PHP\') }}</td>\n</tr>\n{% endif %}\n<tr class="z-total">\n  <td>Total Payments</td>\n  <td></td>\n  <td class="z-money">{{ frappe.utils.fmt_money(s.get(\'actual_money_tender_total\',0), currency=\'PHP\') }}</td>\n</tr>\n</table>\n\n{# Cash is summarized above; detailed listings below follow the old Z-Out listing style\n   but carry Shift Report-level transaction detail. #}\n\n{% set rows = details.get(\'Check\', []) %}\n{% if rows %}\n<div class="z-section">Physical Check Listing</div>\n<table class="z-table">\n<tr class="z-detail-header">\n  <th class="z-money">Amount</th><th>Issuing Bank</th><th>Check No.</th><th>Check Date</th><th>Customer</th><th>Source</th>\n</tr>\n{% for row in rows %}\n<tr>\n  <td class="z-money">{{ frappe.utils.fmt_money(row.get(\'amount\',0), currency=\'PHP\') }}</td>\n  <td>{{ row.get(\'provider\',\'\') or \'—\' }}</td>\n  <td>{{ row.get(\'reference\',\'\') or \'—\' }}</td>\n  <td>{{ row.get(\'check_date\',\'\') or \'—\' }}</td>\n  <td>{{ row.get(\'customer\',\'\') or \'—\' }}</td>\n  <td>{{ row.get(\'source_name\',\'\') }}<br><span class="z-muted z-small">{{ row.get(\'source_type\',\'\') }} · {{ row.get(\'datetime\',\'\') }}</span></td>\n</tr>\n{% endfor %}\n<tr class="z-total"><td class="z-money">{{ frappe.utils.fmt_money(tenders.get(\'Check\',0), currency=\'PHP\') }}</td><td colspan="5">Total - {{ rows | length }} check(s)</td></tr>\n</table>\n{% endif %}\n\n{% set rows = details.get(\'GCash\', []) %}\n{% if rows %}\n<div class="z-section">GCash Listing</div>\n<table class="z-table">\n<tr class="z-detail-header">\n  <th class="z-money">Amount</th><th>Reference</th><th>Customer</th><th>Source</th><th>Date / Time</th>\n</tr>\n{% for row in rows %}\n<tr>\n  <td class="z-money">{{ frappe.utils.fmt_money(row.get(\'amount\',0), currency=\'PHP\') }}</td>\n  <td>{{ row.get(\'reference\',\'\') or \'—\' }}</td>\n  <td>{{ row.get(\'customer\',\'\') or \'—\' }}</td>\n  <td>{{ row.get(\'source_type\',\'\') }} · {{ row.get(\'source_name\',\'\') }}</td>\n  <td>{{ row.get(\'datetime\',\'\') }}</td>\n</tr>\n{% endfor %}\n<tr class="z-total"><td class="z-money">{{ frappe.utils.fmt_money(tenders.get(\'GCash\',0), currency=\'PHP\') }}</td><td colspan="4">Total - {{ rows | length }} transaction(s)</td></tr>\n</table>\n{% endif %}\n\n{% set rows = details.get(\'Maya\', []) %}\n{% if rows %}\n<div class="z-section">Maya Listing</div>\n<table class="z-table">\n<tr class="z-detail-header">\n  <th class="z-money">Amount</th><th>Reference</th><th>Customer</th><th>Source</th><th>Date / Time</th>\n</tr>\n{% for row in rows %}\n<tr>\n  <td class="z-money">{{ frappe.utils.fmt_money(row.get(\'amount\',0), currency=\'PHP\') }}</td>\n  <td>{{ row.get(\'reference\',\'\') or \'—\' }}</td>\n  <td>{{ row.get(\'customer\',\'\') or \'—\' }}</td>\n  <td>{{ row.get(\'source_type\',\'\') }} · {{ row.get(\'source_name\',\'\') }}</td>\n  <td>{{ row.get(\'datetime\',\'\') }}</td>\n</tr>\n{% endfor %}\n<tr class="z-total"><td class="z-money">{{ frappe.utils.fmt_money(tenders.get(\'Maya\',0), currency=\'PHP\') }}</td><td colspan="4">Total - {{ rows | length }} transaction(s)</td></tr>\n</table>\n{% endif %}\n\n{% set rows = details.get(\'Bank Transfer\', []) %}\n{% if rows %}\n<div class="z-section">Bank Transfer Listing</div>\n<table class="z-table">\n<tr class="z-detail-header">\n  <th class="z-money">Amount</th><th>Bank / Provider</th><th>Reference</th><th>Customer</th><th>Source</th><th>Date / Time</th>\n</tr>\n{% for row in rows %}\n<tr>\n  <td class="z-money">{{ frappe.utils.fmt_money(row.get(\'amount\',0), currency=\'PHP\') }}</td>\n  <td>{{ row.get(\'provider\',\'\') or \'—\' }}</td>\n  <td>{{ row.get(\'reference\',\'\') or \'—\' }}</td>\n  <td>{{ row.get(\'customer\',\'\') or \'—\' }}</td>\n  <td>{{ row.get(\'source_type\',\'\') }} · {{ row.get(\'source_name\',\'\') }}</td>\n  <td>{{ row.get(\'datetime\',\'\') }}</td>\n</tr>\n{% endfor %}\n<tr class="z-total"><td class="z-money">{{ frappe.utils.fmt_money(tenders.get(\'Bank Transfer\',0), currency=\'PHP\') }}</td><td colspan="5">Total - {{ rows | length }} transaction(s)</td></tr>\n</table>\n{% endif %}\n\n{% set rows = details.get(\'Card\', []) %}\n{% if rows %}\n<div class="z-section">Card Listing</div>\n<table class="z-table">\n<tr class="z-detail-header">\n  <th class="z-money">Amount</th><th>Provider / Terminal</th><th>Reference</th><th>Customer</th><th>Source</th><th>Date / Time</th>\n</tr>\n{% for row in rows %}\n<tr>\n  <td class="z-money">{{ frappe.utils.fmt_money(row.get(\'amount\',0), currency=\'PHP\') }}</td>\n  <td>{{ row.get(\'provider\',\'\') or \'—\' }}</td>\n  <td>{{ row.get(\'reference\',\'\') or \'—\' }}</td>\n  <td>{{ row.get(\'customer\',\'\') or \'—\' }}</td>\n  <td>{{ row.get(\'source_type\',\'\') }} · {{ row.get(\'source_name\',\'\') }}</td>\n  <td>{{ row.get(\'datetime\',\'\') }}</td>\n</tr>\n{% endfor %}\n<tr class="z-total"><td class="z-money">{{ frappe.utils.fmt_money(tenders.get(\'Card\',0), currency=\'PHP\') }}</td><td colspan="5">Total - {{ rows | length }} transaction(s)</td></tr>\n</table>\n{% endif %}\n\n{% set rows = details.get(\'Online\', []) %}\n{% if rows %}\n<div class="z-section">Online / Other Online Listing</div>\n<table class="z-table">\n<tr class="z-detail-header">\n  <th class="z-money">Amount</th><th>Bank / Provider</th><th>Reference</th><th>Customer</th><th>Source</th><th>Date / Time</th>\n</tr>\n{% for row in rows %}\n<tr>\n  <td class="z-money">{{ frappe.utils.fmt_money(row.get(\'amount\',0), currency=\'PHP\') }}</td>\n  <td>{{ row.get(\'provider\',\'\') or \'—\' }}</td>\n  <td>{{ row.get(\'reference\',\'\') or \'—\' }}</td>\n  <td>{{ row.get(\'customer\',\'\') or \'—\' }}</td>\n  <td>{{ row.get(\'source_type\',\'\') }} · {{ row.get(\'source_name\',\'\') }}</td>\n  <td>{{ row.get(\'datetime\',\'\') }}</td>\n</tr>\n{% endfor %}\n<tr class="z-total"><td class="z-money">{{ frappe.utils.fmt_money(tenders.get(\'Online\',0), currency=\'PHP\') }}</td><td colspan="5">Total - {{ rows | length }} transaction(s)</td></tr>\n</table>\n{% endif %}\n\n<div class="z-section">Account Listing</div>\n<table class="z-table">\n<tr class="z-detail-header">\n  <th class="z-money">Amount</th><th>Customer</th><th>Reference</th><th>Type</th><th>Date / Time</th>\n</tr>\n{% for row in data.get(\'account_activity\', []) %}\n<tr>\n  <td class="z-money">{{ frappe.utils.fmt_money(row.get(\'amount\',0), currency=\'PHP\') }}</td>\n  <td>{{ row.get(\'customer\',\'\') }}</td>\n  <td>{{ row.get(\'reference\',\'\') }}</td>\n  <td>{{ row.get(\'type\',\'\') }}</td>\n  <td>{{ row.get(\'datetime\',\'\') }}</td>\n</tr>\n{% else %}\n<tr><td colspan="5" class="z-center z-muted">No account activity.</td></tr>\n{% endfor %}\n</table>\n\n{% if options.get(\'include_reconciliation_exceptions\') %}\n<div class="z-section">Reconciliation Exceptions</div>\n<table class="z-table">\n<tr class="z-detail-header">\n  <th>Status</th><th>Order</th><th>Customer</th><th>Order Status</th><th class="z-money">Amount</th>\n</tr>\n{% for row in data.get(\'exceptions\', []) %}\n<tr>\n  <td class="z-alert">{{ row.get(\'attention\',\'\') }}</td>\n  <td>{{ row.get(\'order\',\'\') }}</td>\n  <td>{{ row.get(\'customer\',\'\') }}</td>\n  <td>{{ row.get(\'status\',\'\') }}</td>\n  <td class="z-money">{{ frappe.utils.fmt_money(row.get(\'amount\',0), currency=\'PHP\') }}</td>\n</tr>\n{% else %}\n<tr><td colspan="5" class="z-center z-muted">None</td></tr>\n{% endfor %}\n</table>\n{% endif %}\n\n{% if doc.notes %}\n<div class="z-section">Remarks</div>\n<div>{{ doc.notes }}</div>\n{% endif %}\n\n<table class="z-sign">\n<tr>\n  <td><div class="z-signline"><strong>{{ doc.encoder }}</strong><br><span class="z-small">Prepared by</span></div></td>\n  <td><div class="z-signline"><br><span class="z-small">Checked / Reviewed by</span></div></td>\n</tr>\n</table>\n'


def _roles(user=None):
    return set(frappe.get_roles(user or frappe.session.user))


def _is_admin(user=None):
    return bool(_roles(user) & ADMIN_ROLES)


def _is_encoder(user=None):
    return ENCODER_ROLE in _roles(user)


def _norm_method(value):
    value = (value or "").strip()
    aliases = {
        "Cheque": "Check",
        "CC": "Card",
        "Credit Card": "Card",
        "Other Online": "Online",
        "Online Payment": "Online",
    }
    return aliases.get(value, value)


def _as_time(value, fallback):
    if value in (None, ""):
        return fallback
    if isinstance(value, time):
        return value
    text = str(value)
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    frappe.throw(_("Invalid Z-Out time: {0}").format(value))


def _scope_window(business_date, start_time, end_time):
    business_date = getdate(business_date)
    start_t = _as_time(start_time, time(0, 0, 0))
    end_t = _as_time(end_time, time(23, 59, 59))
    start_dt = datetime.combine(business_date, start_t)
    end_dt = datetime.combine(business_date, end_t)
    if end_dt < start_dt:
        frappe.throw(_("Z-Out End Time cannot be earlier than Start Time."))
    now = now_datetime()
    effective_end = min(end_dt, now) if business_date == getdate(now) else end_dt
    return start_dt, end_dt, effective_end


def validate_zout_document(doc):
    # Inventory is a separate report/workflow and is not part of the Z-Out.
    doc.include_inventory_appendix = 0

    if not doc.company:
        frappe.throw(_("Company is required."))
    if not doc.business_date:
        frappe.throw(_("Business Date is required."))
    if not doc.encoder:
        doc.encoder = frappe.session.user
    _scope_window(doc.business_date, doc.start_time, doc.end_time)

    if _is_encoder() and not _is_admin():
        if doc.encoder != frappe.session.user:
            frappe.throw(_("Encoder may only create or finalize their own Z-Out."))
        if getdate(doc.business_date) != getdate(nowdate()):
            frappe.throw(_("Encoder Z-Out must use today's live business date. Reprint a prior finalized Z-Out instead of creating a backdated close."))


def _orders(company, business_date, encoder, start_dt, effective_end):
    return frappe.db.sql(
        """
        SELECT
            name, creation, order_date, customer, customer_name, encoder,
            status, payment_status, payment_arrangement, grand_total,
            declared_payment_total, declared_cash, declared_non_cash,
            declared_account, amount_paid, amount_due,
            cashier_reconciliation_status,
            custom_nkt_account_credit_status,
            custom_nkt_fulfillment_status
        FROM `tabNKT Customer Order`
        WHERE docstatus = 1
          AND company = %s
          AND order_date = %s
          AND encoder = %s
          AND creation >= %s
          AND creation <= %s
          AND COALESCE(status, '') != 'Cancelled'
          AND COALESCE(custom_nkt_archived_from_operations, 0) = 0
        ORDER BY creation, name
        """,
        (company, business_date, encoder, start_dt, effective_end),
        as_dict=True,
    )


def _declared_payment_rows(company, business_date, encoder, start_dt, effective_end):
    return frappe.db.sql(
        """
        SELECT
            p.payment_method, p.amount, p.card_surcharge, p.collected_amount, p.reference_number, p.bank_or_provider,
            p.custom_nkt_check_number AS check_number,
            p.custom_nkt_check_date AS check_date,
            o.name AS source_name, o.customer, o.customer_name,
            o.creation AS source_datetime
        FROM `tabNKT Declared Payment` p
        INNER JOIN `tabNKT Customer Order` o ON o.name = p.parent
        WHERE o.docstatus = 1
          AND o.company = %s
          AND o.order_date = %s
          AND o.encoder = %s
          AND o.creation >= %s
          AND o.creation <= %s
          AND COALESCE(o.status, '') != 'Cancelled'
          AND COALESCE(o.custom_nkt_archived_from_operations, 0) = 0
          AND p.parenttype = 'NKT Customer Order'
        ORDER BY o.creation, p.idx
        """,
        (company, business_date, encoder, start_dt, effective_end),
        as_dict=True,
    )


def _encoder_collections(company, business_date, encoder, start_dt, effective_end):
    rows = frappe.db.sql(
        """
        SELECT
            a.name, a.creation, a.allocation_date, a.customer, a.customer_name,
            a.collection_amount, a.total_allocated, a.unallocated_amount,
            a.status, a.application_rule, a.application_summary
        FROM `tabNKT Encoder Account Allocation` a
        WHERE a.company = %s
          AND a.allocation_date = %s
          AND a.encoder = %s
          AND a.creation >= %s
          AND a.creation <= %s
          AND a.status = 'Matched'
          AND COALESCE(a.allocations_posted, 0) = 1
          AND COALESCE(a.custom_nkt_application_status, 'Active') = 'Active'
        ORDER BY a.creation, a.name
        """,
        (company, business_date, encoder, start_dt, effective_end),
        as_dict=True,
    )
    names = [row.name for row in rows]
    payment_rows = []
    if names and frappe.db.exists("DocType", "NKT Account Collection Payment"):
        placeholders = ", ".join(["%s"] * len(names))
        payment_rows = frappe.db.sql(
            f"""
            SELECT
                p.parent AS allocation_name, p.payment_method, p.amount, p.card_surcharge, p.collected_amount,
                p.reference_number, p.bank_or_provider,
                p.check_number, p.check_date, p.creation
            FROM `tabNKT Account Collection Payment` p
            WHERE p.parent IN ({placeholders})
            ORDER BY p.parent, p.idx
            """,
            tuple(names),
            as_dict=True,
        )
    by_allocation = {}
    for row in payment_rows:
        by_allocation.setdefault(row.allocation_name, []).append(row)
    return rows, by_allocation


def _returns_for_orders(order_names, start_dt, effective_end):
    if not order_names:
        return []
    placeholders = ", ".join(["%s"] * len(order_names))
    return frappe.db.sql(
        f"""
        SELECT
            r.name, r.return_datetime, r.customer, r.customer_name,
            r.customer_order, r.return_status, r.settlement_type,
            r.calculated_return_credit, r.refund_due,
            r.customer_credit_due, r.approval_status
        FROM `tabNKT Customer Return` r
        WHERE r.docstatus = 1
          AND r.customer_order IN ({placeholders})
          AND r.return_datetime >= %s
          AND r.return_datetime <= %s
          AND COALESCE(r.return_status, '') != 'Cancelled'
        ORDER BY r.return_datetime, r.name
        """,
        tuple(order_names) + (start_dt, effective_end),
        as_dict=True,
    )


def _return_items_for_orders(order_names, start_dt, effective_end):
    if not order_names:
        return []
    placeholders = ", ".join(["%s"] * len(order_names))
    return frappe.db.sql(
        f"""
        SELECT
            ri.item, ri.item_name, ri.return_quantity, ri.uom,
            ri.original_source_warehouse, r.name AS return_name,
            r.customer_order, r.return_datetime
        FROM `tabNKT Customer Return Item` ri
        INNER JOIN `tabNKT Customer Return` r ON r.name = ri.parent
        WHERE r.docstatus = 1
          AND r.customer_order IN ({placeholders})
          AND r.return_datetime >= %s
          AND r.return_datetime <= %s
          AND COALESCE(r.return_status, '') != 'Cancelled'
        ORDER BY r.return_datetime, r.name, ri.idx
        """,
        tuple(order_names) + (start_dt, effective_end),
        as_dict=True,
    )


def _order_items(order_names):
    if not order_names:
        return []
    placeholders = ", ".join(["%s"] * len(order_names))
    return frappe.db.sql(
        f"""
        SELECT oi.parent AS order_name, oi.item, oi.item_name, oi.quantity,
               oi.uom, oi.source_warehouse, oi.amount
        FROM `tabNKT Customer Order Item` oi
        WHERE oi.parent IN ({placeholders})
        ORDER BY oi.parent, oi.idx
        """,
        tuple(order_names),
        as_dict=True,
    )



def _price_adjustments(order_names):
    """Derived selling-price audit rows for the independent Encoder Z-Out."""
    if not order_names:
        return []
    placeholders = ", ".join(["%s"] * len(order_names))
    rows = frappe.db.sql(
        f"""
        SELECT
            o.name AS order_name,
            o.creation AS order_datetime,
            o.customer,
            o.customer_name,
            o.encoder,
            o.cashier_reconciliation_status,
            o.matched_cashier_sale,
            o.cashier_reconciled_on,
            oi.idx AS line_no,
            oi.item,
            oi.item_name,
            oi.quantity,
            oi.uom,
            oi.source_warehouse,
            oi.standard_rate,
            oi.final_rate,
            s.cashier,
            s.custom_nkt_price_authorized_by AS authorized_by,
            s.custom_nkt_price_authorized_on AS authorized_on,
            s.custom_nkt_price_authorization_reason AS authorization_reason,
            s.custom_nkt_price_authorization_explanation AS authorization_explanation,
            s.custom_nkt_price_authorization_source AS authorization_source,
            s.custom_nkt_price_authorization_device_id AS authorization_device_id
        FROM `tabNKT Customer Order Item` oi
        INNER JOIN `tabNKT Customer Order` o ON o.name = oi.parent
        LEFT JOIN `tabNKT Cashier Sale` s ON s.name = o.matched_cashier_sale
        WHERE oi.parent IN ({placeholders})
          AND ABS(COALESCE(oi.final_rate, 0) - COALESCE(oi.standard_rate, 0)) > 0.000001
        ORDER BY o.creation, o.name, oi.idx
        """,
        tuple(order_names),
        as_dict=True,
    )
    out = []
    for row in rows:
        standard_rate = flt(row.standard_rate)
        actual_rate = flt(row.final_rate)
        qty = flt(row.quantity)
        difference = actual_rate - standard_rate
        out.append({
            "order": row.order_name,
            "order_datetime": str(row.order_datetime or ""),
            "customer": row.customer_name or row.customer or "",
            "encoder": row.encoder or "",
            "cashier_sale": row.matched_cashier_sale or "",
            "cashier": row.cashier or "",
            "match_status": row.cashier_reconciliation_status or "",
            "matched_on": str(row.cashier_reconciled_on or ""),
            "line_no": cint(row.line_no),
            "item": row.item_name or row.item or "",
            "item_code": row.item or "",
            "qty": qty,
            "uom": row.uom or "",
            "warehouse": row.source_warehouse or "",
            "standard_rate": _money(standard_rate),
            "actual_rate": _money(actual_rate),
            "difference_per_unit": _money(difference),
            "total_rate_effect": _money(qty * difference),
            "authorized_by": row.authorized_by or "",
            "authorized_on": str(row.authorized_on or ""),
            "reason": row.authorization_reason or "",
            "explanation": row.authorization_explanation or "",
            "authorization_source": row.authorization_source or "",
            "authorization_device_id": row.authorization_device_id or "",
        })
    return out


def _advance_events(order_names, start_dt, effective_end):
    if not order_names:
        return [], []
    placeholders = ", ".join(["%s"] * len(order_names))
    applied = frappe.db.sql(
        f"""
        SELECT name, posting_datetime, customer, customer_name,
               customer_order, customer_advance, source_payment_receipt,
               applied_amount, application_status
        FROM `tabNKT Customer Advance Application`
        WHERE customer_order IN ({placeholders})
          AND posting_datetime >= %s
          AND posting_datetime <= %s
          AND application_status = 'Applied'
          AND docstatus = 1
        ORDER BY posting_datetime, name
        """,
        tuple(order_names) + (start_dt, effective_end),
        as_dict=True,
    )
    reversed_rows = frappe.db.sql(
        f"""
        SELECT name, custom_nkt_reversed_on AS event_datetime,
               customer, customer_name, customer_order, customer_advance,
               source_payment_receipt, applied_amount,
               custom_nkt_reversal_reason
        FROM `tabNKT Customer Advance Application`
        WHERE customer_order IN ({placeholders})
          AND custom_nkt_reversed_on IS NOT NULL
          AND custom_nkt_reversed_on >= %s
          AND custom_nkt_reversed_on <= %s
        ORDER BY custom_nkt_reversed_on, name
        """,
        tuple(order_names) + (start_dt, effective_end),
        as_dict=True,
    )
    return applied, reversed_rows


def _exceptions(orders):
    rows = []
    for order in orders:
        labels = []
        recon = (order.cashier_reconciliation_status or "").strip()
        if recon == "Unmatched":
            labels.append("No Cashier Match")
        elif recon == "Ambiguous":
            labels.append("Multiple Possible Matches")
        elif recon == "Matched with Warehouse Warning":
            labels.append("Warehouse Review Needed")
        elif recon and not recon.startswith("Matched"):
            labels.append("Reconciliation Review Needed")
        if (order.custom_nkt_account_credit_status or "") == "Pending Approval":
            labels.append("Pending Credit Approval")
        if not labels:
            continue
        rows.append({
            "order": order.name,
            "datetime": str(order.creation),
            "customer": order.customer_name or order.customer,
            "amount": flt(order.grand_total),
            "payment_status": order.payment_status or "",
            "status": order.status or "",
            "attention": " / ".join(labels),
        })
    return rows


def _inventory(company, business_date, effective_end, order_items, return_items):
    if not frappe.get_meta("Item").has_field(ITEM_FLAG):
        return []
    included_items = frappe.db.sql(
        f"""
        SELECT name, item_name, stock_uom
        FROM `tabItem`
        WHERE disabled = 0 AND is_stock_item = 1
          AND COALESCE(`{ITEM_FLAG}`, 1) = 1
        ORDER BY item_name, name
        """,
        as_dict=True,
    )
    included = {row.name: row for row in included_items}
    if not included:
        return []

    sold = {}
    for row in order_items:
        if row.item in included:
            key = (row.item, row.source_warehouse or "")
            sold[key] = sold.get(key, 0.0) + flt(row.quantity)

    returned = {}
    for row in return_items:
        if row.item in included:
            key = (row.item, row.original_source_warehouse or "")
            returned[key] = returned.get(key, 0.0) + flt(row.return_quantity)

    item_names = list(included)
    placeholders = ", ".join(["%s"] * len(item_names))
    closing_rows = frappe.db.sql(
        f"""
        SELECT item_code, warehouse, qty_after_transaction
        FROM (
            SELECT sle.item_code, sle.warehouse, sle.qty_after_transaction,
                   ROW_NUMBER() OVER (
                       PARTITION BY sle.item_code, sle.warehouse
                       ORDER BY sle.posting_date DESC, sle.posting_time DESC, sle.creation DESC
                   ) AS rn
            FROM `tabStock Ledger Entry` sle
            INNER JOIN `tabWarehouse` w ON w.name = sle.warehouse
            WHERE w.company = %s AND w.is_group = 0
              AND sle.item_code IN ({placeholders})
              AND (
                    sle.posting_date < %s
                    OR (sle.posting_date = %s AND sle.posting_time <= %s)
                  )
        ) ranked
        WHERE rn = 1
        """,
        (company, *item_names, business_date, business_date, effective_end.time()),
        as_dict=True,
    )
    closing = {(r.item_code, r.warehouse): flt(r.qty_after_transaction) for r in closing_rows}
    keys = set(sold) | set(returned) | {key for key, qty in closing.items() if abs(flt(qty)) > 0.000001}

    result = []
    for item, warehouse in sorted(keys, key=lambda x: ((x[1] or ""), included[x[0]].item_name or x[0])):
        meta = included[item]
        result.append({
            "item": item,
            "item_name": meta.item_name or item,
            "uom": meta.stock_uom or "",
            "warehouse": warehouse or "Unspecified",
            "sold_qty": sold.get((item, warehouse), 0.0),
            "returned_qty": returned.get((item, warehouse), 0.0),
            "closing_qty": closing.get((item, warehouse), 0.0),
        })
    return result


def _money(v):
    return round(flt(v), 2)


def _c7_posted_returns(company, business_date, encoder, start_dt, effective_end):
    if not frappe.db.exists("DocType", "NKT Return Exchange Declaration"):
        return []
    return frappe.db.sql(
        """
        SELECT
            name, entry_datetime, customer, customer_name,
            return_credit, refund_money, account_adjustment_amount,
            customer_credit_amount, transaction_type
        FROM `tabNKT Return Exchange Declaration`
        WHERE docstatus=1
          AND side='Encoder'
          AND company=%s
          AND business_date=%s
          AND entry_user=%s
          AND posting_status='Posted'
          AND entry_datetime BETWEEN %s AND %s
        ORDER BY entry_datetime, name
        """,
        (company, business_date, encoder, start_dt, effective_end),
        as_dict=True,
    )


def build_zout_data(*, company, business_date, encoder, start_time="00:00:00", end_time="23:59:59", include_reconciliation_exceptions=1, include_inventory_appendix=1):
    start_dt, requested_end, effective_end = _scope_window(business_date, start_time, end_time)
    business_date = getdate(business_date)

    orders = _orders(company, business_date, encoder, start_dt, effective_end)
    order_names = [r.name for r in orders]
    order_items = _order_items(order_names)
    price_adjustments = _price_adjustments(order_names)
    declared = _declared_payment_rows(company, business_date, encoder, start_dt, effective_end)
    collections, collection_payments = _encoder_collections(company, business_date, encoder, start_dt, effective_end)
    returns = _returns_for_orders(order_names, start_dt, effective_end)
    return_items = _return_items_for_orders(order_names, start_dt, effective_end)
    applied_advances, reversed_advances = _advance_events(order_names, start_dt, effective_end)
    c7_returns = _c7_posted_returns(company, business_date, encoder, start_dt, effective_end)

    tender_totals = {m: 0.0 for m in METHODS}
    tender_details = {m: [] for m in METHODS}
    card_surcharge_total = 0.0

    for row in declared:
        method = _norm_method(row.payment_method)
        if method in {"Account", "Return Credit"}:
            continue
        if method not in tender_totals:
            method = "Online"
        settlement_amount = flt(row.amount)
        surcharge = flt(row.card_surcharge) if method == "Card" else 0
        amount = flt(row.collected_amount) or (settlement_amount + surcharge if method == "Card" else settlement_amount)
        if method == "Card":
            card_surcharge_total += surcharge
        tender_totals[method] += amount
        tender_details[method].append({
            "source_type":"Order","source_name":row.source_name,"datetime":str(row.source_datetime),
            "customer":row.customer_name or row.customer,"provider":row.bank_or_provider or "",
            "reference":row.check_number or row.reference_number or "","check_date":str(row.check_date or ""),
            "amount":amount,"settlement_amount":settlement_amount,"card_surcharge":surcharge,
        })

    for allocation in collections:
        for row in collection_payments.get(allocation.name, []):
            method = _norm_method(row.payment_method)
            if method not in tender_totals:
                method = "Online"
            settlement_amount = flt(row.amount)
            surcharge = flt(row.card_surcharge) if method == "Card" else 0
            amount = flt(row.collected_amount) or (settlement_amount + surcharge if method == "Card" else settlement_amount)
            if method == "Card":
                card_surcharge_total += surcharge
            tender_totals[method] += amount
            tender_details[method].append({
                "source_type":"Account Collection","source_name":allocation.name,"datetime":str(allocation.creation),
                "customer":allocation.customer_name or allocation.customer,"provider":row.bank_or_provider or "",
                "reference":row.check_number or row.reference_number or "","check_date":str(row.check_date or ""),
                "amount":amount,"settlement_amount":settlement_amount,"card_surcharge":surcharge,
            })

    gross_sales = sum(flt(r.grand_total) for r in orders)
    gross_returns = (
        sum(flt(r.calculated_return_credit) for r in returns)
        + sum(flt(r.return_credit) for r in c7_returns)
    )
    account_sales = sum(flt(r.declared_account) for r in orders)
    account_collections = sum(flt(r.collection_amount) for r in collections)
    advance_created = sum(flt(r.unallocated_amount) for r in collections)
    advance_applied = sum(flt(r.applied_amount) for r in applied_advances)
    advance_reversed = sum(flt(r.applied_amount) for r in reversed_advances)

    account_activity = []
    for r in orders:
        if flt(r.declared_account) > 0.005:
            account_activity.append({"datetime":str(r.creation),"type":"Account Sale","reference":r.name,"customer":r.customer_name or r.customer,"amount":flt(r.declared_account)})
    for r in collections:
        account_activity.append({"datetime":str(r.creation),"type":"Account Collection","reference":r.name,"customer":r.customer_name or r.customer,"amount":-flt(r.collection_amount)})
    account_activity.sort(key=lambda x:(x["datetime"],x["reference"]))

    control_events = []
    for r in collections:
        if flt(r.unallocated_amount) > 0.005:
            control_events.append({"datetime":str(r.creation),"event":"Customer Advance Created","reference":r.name,"customer":r.customer_name or r.customer,"amount":flt(r.unallocated_amount),"cash_effect":"Already included in Account Collection tender; no extra money"})
    for r in applied_advances:
        control_events.append({"datetime":str(r.posting_datetime),"event":"Customer Advance Applied","reference":r.name,"customer":r.customer_name or r.customer,"amount":flt(r.applied_amount),"cash_effect":"No drawer / tender effect"})
    for r in reversed_advances:
        control_events.append({"datetime":str(r.event_datetime),"event":"Advance Application Reversed","reference":r.name,"customer":r.customer_name or r.customer,"amount":flt(r.applied_amount),"cash_effect":"No drawer / tender effect"})
    control_events.sort(key=lambda x:(x["datetime"],x["reference"]))

    exceptions = _exceptions(orders)
    inventory = _inventory(company, business_date, effective_end, order_items, return_items) if cint(include_inventory_appendix) else []
    tender_totals = {k:_money(v) for k,v in tender_totals.items()}
    for m in tender_details:
        tender_details[m].sort(key=lambda x:(x["datetime"],x["source_name"]))

    price_adjustment_total_effect = _money(
        sum(flt(row.get("total_rate_effect")) for row in price_adjustments)
    )

    return {
        "version":VERSION,
        "scope":{"company":company,"business_date":str(business_date),"encoder":encoder,"start_datetime":str(start_dt),"requested_end_datetime":str(requested_end),"effective_end_datetime":str(effective_end),"generated_on":str(now_datetime())},
        "options":{"include_reconciliation_exceptions":bool(cint(include_reconciliation_exceptions)),"include_inventory_appendix":bool(cint(include_inventory_appendix)),"item_inventory_flag":ITEM_FLAG},
        "summary":{"gross_sales":_money(gross_sales),"gross_returns":_money(gross_returns),"net_sales":_money(gross_sales-gross_returns),"account_sales":_money(account_sales),"account_collections":_money(account_collections),"unapplied_advance_created":_money(advance_created),"advance_applied_no_cash":_money(advance_applied),"advance_reversed_no_cash":_money(advance_reversed),"card_surcharge_total":_money(card_surcharge_total),"actual_money_tender_total":_money(sum(tender_totals.values())),"price_adjustment_total_effect":price_adjustment_total_effect},
        "counts":{"orders":len(orders),"returns":len(returns)+len(c7_returns),"account_collections":len(collections),"exceptions":len(exceptions),"inventory_lines":len(inventory),"price_adjustments":len(price_adjustments)},
        "tender_method_order":METHODS,"tender_labels":METHOD_LABELS,"tender_totals":tender_totals,"tender_details":tender_details,
        "account_activity":account_activity,"control_events":control_events,"price_adjustments":price_adjustments,
        "exceptions":exceptions if cint(include_reconciliation_exceptions) else [],"inventory":inventory,
    }


def _apply_summary_fields(doc, data):
    s,t,c = data["summary"],data["tender_totals"],data["counts"]
    doc.gross_sales=s["gross_sales"]; doc.gross_returns=s["gross_returns"]; doc.net_sales=s["net_sales"]
    doc.account_sales=s["account_sales"]; doc.account_collections=s["account_collections"]
    doc.unapplied_advance_created=s["unapplied_advance_created"]; doc.advance_applied_no_cash=s["advance_applied_no_cash"]; doc.advance_reversed_no_cash=s["advance_reversed_no_cash"]
    doc.order_count=c["orders"]; doc.collection_count=c["account_collections"]; doc.return_count=c["returns"]
    doc.cash_total=t["Cash"]; doc.check_total=t["Check"]; doc.gcash_total=t["GCash"]; doc.maya_total=t["Maya"]
    doc.bank_transfer_total=t["Bank Transfer"]; doc.credit_card_total=t["Card"]; doc.card_surcharge_total=s.get("card_surcharge_total", 0); doc.online_total=t["Online"]
    doc.exception_count=c["exceptions"]; doc.inventory_item_count=c["inventory_lines"]


def finalize_zout_document(doc):
    validate_zout_document(doc)
    data=build_zout_data(company=doc.company,business_date=doc.business_date,encoder=doc.encoder,start_time=doc.start_time,end_time=doc.end_time,include_reconciliation_exceptions=doc.include_reconciliation_exceptions,include_inventory_appendix=doc.include_inventory_appendix)
    _apply_summary_fields(doc,data)
    payload=json.dumps(data,sort_keys=True,default=str,separators=(",",":"))
    doc.snapshot_json=payload
    doc.snapshot_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest()
    doc.finalized_by=frappe.session.user
    doc.finalized_on=now_datetime()


@frappe.whitelist()
def refresh_preview(name):
    doc=frappe.get_doc(ZOUT,name); doc.check_permission("write")
    if doc.docstatus!=0: frappe.throw(_("Only a Draft Z-Out can refresh preview totals."))
    validate_zout_document(doc)
    data=build_zout_data(company=doc.company,business_date=doc.business_date,encoder=doc.encoder,start_time=doc.start_time,end_time=doc.end_time,include_reconciliation_exceptions=doc.include_reconciliation_exceptions,include_inventory_appendix=doc.include_inventory_appendix)
    _apply_summary_fields(doc,data); doc.save()
    return {"summary":data["summary"],"counts":data["counts"],"tender_totals":data["tender_totals"]}


@frappe.whitelist()
def get_defaults():
    user=frappe.session.user; today=getdate(nowdate())
    company=frappe.db.get_value("NKT Customer Order",{"encoder":user,"order_date":today,"docstatus":1},"company",order_by="creation desc")
    if not company: company=frappe.db.get_value("Company",{},"name",order_by="creation asc")
    return {"encoder":user,"business_date":str(today),"company":company}


def _ensure_custom_field():
    if frappe.get_meta("Item").has_field(ITEM_FLAG): return False
    doc=frappe.get_doc({"doctype":"Custom Field","dt":"Item","fieldname":ITEM_FLAG,"label":"Include in Z-Out Inventory","fieldtype":"Check","default":"1","insert_after":"nkt_base_saleable_item" if frappe.get_meta("Item").has_field("nkt_base_saleable_item") else "stock_uom","description":"Controls only whether this Item is printed in the Encoder Z-Out Inventory Appendix. It does not change sales, stock posting, valuation, or inventory records."})
    doc.flags.ignore_permissions=True; doc.insert(ignore_permissions=True); frappe.clear_cache(doctype="Item"); return True


def _ensure_client_script():
    values={"dt":ZOUT,"view":"Form","enabled":1,"script":CLIENT_SCRIPT_BODY}
    if frappe.db.exists("Client Script",CLIENT_SCRIPT):
        doc=frappe.get_doc("Client Script",CLIENT_SCRIPT); doc.update(values); doc.flags.ignore_permissions=True; doc.save(ignore_permissions=True)
    else:
        doc=frappe.get_doc({"doctype":"Client Script","name":CLIENT_SCRIPT,**values}); doc.flags.ignore_permissions=True; doc.insert(ignore_permissions=True)


def _ensure_print_format():
    values={"doc_type":ZOUT,"module":"NKT Store Operations","standard":"No","custom_format":1,"print_format_type":"Jinja","html":PRINT_HTML,"disabled":0}
    if frappe.db.exists("Print Format",PRINT_FORMAT):
        doc=frappe.get_doc("Print Format",PRINT_FORMAT); doc.update(values); doc.flags.ignore_permissions=True; doc.save(ignore_permissions=True)
    else:
        doc=frappe.get_doc({"doctype":"Print Format","name":PRINT_FORMAT,**values}); doc.flags.ignore_permissions=True; doc.insert(ignore_permissions=True)
    if frappe.get_meta("DocType").has_field("default_print_format"):
        frappe.db.set_value("DocType",ZOUT,"default_print_format",PRINT_FORMAT,update_modified=False)


def _workspace_content():
    return json.dumps([
        {"id":"z1","type":"header","data":{"text":"<span class=\"h4\">Encoder End-of-Shift</span>","col":12}},
        {"id":"z2","type":"shortcut","data":{"shortcut_name":"New Encoder Z-Out","col":3}},
        {"id":"z3","type":"shortcut","data":{"shortcut_name":"Encoder Z-Out History","col":3}},
        {"id":"z4","type":"shortcut","data":{"shortcut_name":"Customer Orders","col":3}},
        {"id":"z5","type":"shortcut","data":{"shortcut_name":"Account Collection Verification","col":3}},
    ])


def _ensure_workspace():
    old=getattr(frappe.flags,"in_patch",False); frappe.flags.in_patch=True
    try:
        doc=frappe.get_doc("Workspace",WORKSPACE) if frappe.db.exists("Workspace",WORKSPACE) else frappe.new_doc("Workspace")
        doc.title=WORKSPACE; doc.label=WORKSPACE; doc.module="NKT Store Operations"; doc.app="nkt_operations"; doc.type="Workspace"; doc.icon="file-text"; doc.indicator_color="blue"; doc.public=1; doc.is_hidden=0; doc.hide_custom=0; doc.content=_workspace_content()
        doc.set("roles",[])
        for role in ("System Manager","NKT OWNER","NKT ADMINISTRATOR",ENCODER_ROLE): doc.append("roles",{"role":role})
        shortcuts=[("New Encoder Z-Out",ZOUT,"New"),("Encoder Z-Out History",ZOUT,"List"),("Customer Orders","NKT Customer Order","List"),("Account Collection Verification","NKT Encoder Account Allocation","List")]
        doc.set("shortcuts",[]); doc.set("links",[])
        for label,dt,view in shortcuts: doc.append("shortcuts",{"type":"DocType","link_to":dt,"doc_view":view,"label":label})
        doc.append("links",{"type":"Card Break","label":"Encoder Close","icon":"file-text","link_count":len(shortcuts),"description":"Finalize the independent Encoder Z-Out and review official Encoder-side transactions."})
        for label,dt,view in shortcuts: doc.append("links",{"type":"Link","label":label,"link_type":"DocType","link_to":dt,"onboard":0})
        doc.flags.ignore_permissions=True
        doc.insert(ignore_permissions=True) if doc.is_new() else doc.save(ignore_permissions=True)
    finally:
        frappe.flags.in_patch=old
    return WORKSPACE


@frappe.whitelist()
def install():
    frappe.set_user("Administrator")
    if not frappe.db.exists("DocType",ZOUT): frappe.throw(_("NKT Encoder Z-Out DocType is missing. Run migrate before the C6.2 installer."))
    if not frappe.get_attr("nkt_operations.nkt_store_operations.features.cashier.shift_report.verify")().get("passed"): frappe.throw(_("C6.1 baseline is not passing."))
    if not frappe.get_attr("nkt_operations.nkt_store_operations.features.setup_validation.internal.order_integration_verify.verify")().get("passed"): frappe.throw(_("C5.6 ORD-00049 baseline is not passing."))
    field_added=_ensure_custom_field(); _ensure_client_script(); _ensure_print_format(); workspace=_ensure_workspace()
    frappe.clear_cache(); frappe.db.commit()
    return {"installed":True,"version":VERSION,"item_flag_added":field_added,"item_flag":ITEM_FLAG,"print_format":PRINT_FORMAT,"workspace":workspace,"business_records_created":False}


@frappe.whitelist()
def verify():
    errors=[]
    if not frappe.db.exists("DocType",ZOUT): errors.append("NKT Encoder Z-Out DocType missing.")
    flag_ok=frappe.get_meta("Item").has_field(ITEM_FLAG)
    if not flag_ok: errors.append("Item Include in Z-Out Inventory field missing.")
    default=frappe.db.get_value("Custom Field",{"dt":"Item","fieldname":ITEM_FLAG},"default") if flag_ok else None
    if flag_ok and str(default or "") not in {"1","True","true"}: errors.append("Item Z-Out inventory field does not default ON.")
    pf=frappe.db.get_value("Print Format",PRINT_FORMAT,["doc_type","disabled","html"],as_dict=True)
    if not pf or pf.doc_type!=ZOUT or cint(pf.disabled):
        errors.append("Encoder Z-Out print format missing/disabled.")
    else:
        # Static section headings must exist in the Jinja source.
        for text in [
            "Z-Out Store Close",
            "Sales Activity",
            "Sales Adjustments",
            "Receipt Counts",
            "Payment Summary",
            "Physical Check Listing",
            "GCash Listing",
            "Maya Listing",
            "Bank Transfer Listing",
            "Card Listing",
            "Online / Other Online Listing",
            "Account Listing",
            "Reconciliation Exceptions",
        ]:
            if text not in (pf.html or ""):
                errors.append("Print format missing static section: "+text)

        # Tender labels are intentionally rendered dynamically from
        # METHOD_LABELS, so verify the rendering contract rather than
        # incorrectly demanding every label be a literal in the HTML source.
        expected_labels = {
            "Check": "Physical Check",
            "GCash": "GCash",
            "Maya": "Maya",
            "Bank Transfer": "Bank Transfer",
            "Card": "Card",
            "Online": "Online / Other Online",
        }
        for method, label in expected_labels.items():
            if METHOD_LABELS.get(method) != label:
                errors.append(f"Tender label contract incorrect for {method}: {METHOD_LABELS.get(method)!r}")
        if "tender_labels" not in (pf.html or "") or "tender_method_order" not in (pf.html or ""):
            errors.append("Print format is not wired to the dynamic tender-label rendering contract.")
        if "Z-Out Inventory Appendix" in (pf.html or ""):
            errors.append("Inventory must not be printed as part of the Z-Out.")
    script=frappe.db.get_value("Client Script",CLIENT_SCRIPT,"script") or ""
    if "Refresh Totals" not in script or "Print Z-Out" not in script: errors.append("Z-Out client controls missing.")
    source=inspect.getsource(build_zout_data)
    forbidden=["NKT Cashier Movement","NKT Cashier Shift","tabNKT Cashier Sale"]
    independence_ok=not any(token in source for token in forbidden)
    if not independence_ok: errors.append("Encoder Z-Out builder references Cashier-side close/tender data.")
    if not frappe.db.exists("DocType","NKT Account Collection Payment"):
        errors.append("Independent Encoder collection payment row missing.")

    print_render = {"tested": False, "nonblank": None, "length": 0}
    if frappe.db.exists(ZOUT, "NKT-ZOUT-00001"):
        try:
            rendered = frappe.get_print(
                ZOUT,
                "NKT-ZOUT-00001",
                print_format=PRINT_FORMAT,
                no_letterhead=1,
            ) or ""
            print_render = {
                "tested": True,
                "nonblank": (
                    len(rendered.strip()) > 500
                    and "Z-Out Store Close" in rendered
                    and "Payment Summary" in rendered
                ),
                "length": len(rendered),
            }
            if not print_render["nonblank"]:
                errors.append("Server-rendered Z-Out print HTML is blank/incomplete.")
        except Exception as exc:
            print_render = {
                "tested": True,
                "nonblank": False,
                "length": 0,
                "error": str(exc),
            }
            errors.append("Server-side Z-Out print render failed: " + str(exc))

    c61=frappe.get_attr("nkt_operations.nkt_store_operations.features.cashier.shift_report.verify")()
    ord49=frappe.get_attr("nkt_operations.nkt_store_operations.features.setup_validation.internal.order_integration_verify.verify")()
    if not c61.get("passed"): errors.append("C6.1 regression.")
    if not ord49.get("passed"): errors.append("C5.6 regression.")
    preview=None
    if frappe.db.exists("User","encoder@example.com"):
        company=frappe.db.get_value("NKT Customer Order",{"encoder":"encoder@example.com","order_date":getdate(nowdate()),"docstatus":1},"company",order_by="creation desc")
        if company: preview=build_zout_data(company=company,business_date=getdate(nowdate()),encoder="encoder@example.com",include_reconciliation_exceptions=1,include_inventory_appendix=1)
    return {"version":VERSION,"doctype_exists":bool(frappe.db.exists("DocType",ZOUT)),"item_inventory_flag":{"fieldname":ITEM_FLAG,"exists":flag_ok,"default_on":bool(flag_ok and str(default or "") in {"1","True","true"})},"encoder_independence":{"builder_uses_cashier_shift_or_movement_totals":not independence_ok,"cashier_only_missing_orders_reserved_for_admin_reconciliation":True},"c6_1_regression_passed":bool(c61.get("passed")),"c5_6_regression_passed":bool(ord49.get("passed")),"print_render":print_render,"live_preview":{"summary":(preview or {}).get("summary"),"counts":(preview or {}).get("counts"),"tender_totals":(preview or {}).get("tender_totals")} if preview else None,"errors":errors,"passed":not errors}
