from __future__ import annotations

import frappe
from frappe.utils import cint

PRINT_FORMAT_NAME = "NKT Cashier Shift Report"
VERSION = "V2.0C.6.1"

PRINT_HTML = r"""
<style>
@page { size: Letter; margin: 8mm 8mm 10mm; }
.print-format { font-family: Arial, Helvetica, sans-serif; font-size: 8.4pt; color: #1c2733; line-height: 1.28; }
.nkt-header { border-bottom: 2px solid #274d6b; padding-bottom: 6px; margin-bottom: 7px; }
.nkt-title { font-size: 17pt; font-weight: 700; letter-spacing: .2px; color: #173d5b; }
.nkt-kicker { font-size: 7.5pt; font-weight: 700; text-transform: uppercase; letter-spacing: .8px; color: #607487; margin-top: 1px; }
.nkt-meta { width: 100%; border-collapse: collapse; margin-top: 6px; }
.nkt-meta td { padding: 2px 5px 2px 0; vertical-align: top; }
.nkt-label { font-size: 7pt; font-weight: 700; color: #647687; text-transform: uppercase; letter-spacing: .35px; }
.nkt-value { font-weight: 600; color: #1f2d3a; }
.nkt-grid2 { width: 100%; border-collapse: separate; border-spacing: 5px 0; margin-left: -5px; margin-right: -5px; }
.nkt-grid2 > tbody > tr > td { width: 50%; vertical-align: top; padding: 0 0 5px 5px; }
.nkt-card { border: 1px solid #c9d3dc; border-radius: 3px; overflow: hidden; margin-bottom: 6px; }
.nkt-card-title { background: #edf3f7; color: #173d5b; font-weight: 700; font-size: 8.7pt; padding: 4px 6px; border-bottom: 1px solid #c9d3dc; }
.nkt-table { width: 100%; border-collapse: collapse; }
.nkt-table th { background: #f7f9fb; color: #526779; font-size: 7.2pt; text-transform: uppercase; letter-spacing: .25px; font-weight: 700; border-bottom: 1px solid #d7dfe6; padding: 3px 5px; }
.nkt-table td { border-bottom: 1px solid #e3e8ed; padding: 3px 5px; vertical-align: top; }
.nkt-table tr:last-child td { border-bottom: 0; }
.nkt-money { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.nkt-center { text-align: center; }
.nkt-strong { font-weight: 700; }
.nkt-total td { background: #f4f7f9; font-weight: 700; border-top: 1px solid #c7d1da; }
.nkt-hero { background: #173d5b; color: white; }
.nkt-hero td { border-bottom-color: #173d5b; }
.nkt-hero .nkt-muted { color: #d7e3ec; }
.nkt-difference { font-size: 10pt; font-weight: 700; }
.nkt-positive { color: #1f6a45; }
.nkt-negative { color: #9a2d2d; }
.nkt-muted { color: #6e7f8d; }
.nkt-small { font-size: 7.2pt; }
.nkt-method { display: inline-block; min-width: 72px; padding: 1px 5px; border: 1px solid #c9d5df; background: #f4f7f9; border-radius: 8px; font-size: 7pt; font-weight: 700; text-align: center; }
.nkt-section { color: #173d5b; font-size: 9.4pt; font-weight: 700; border-bottom: 1px solid #aebdca; padding-bottom: 2px; margin: 7px 0 4px; }
.nkt-notes { min-height: 34px; border: 1px solid #c9d3dc; padding: 5px 6px; background: #fbfcfd; }
.nkt-signatures { width: 100%; border-collapse: separate; border-spacing: 18px 0; margin-top: 22px; }
.nkt-signatures td { width: 50%; text-align: center; vertical-align: bottom; }
.nkt-signline { border-top: 1px solid #6c7b88; padding-top: 3px; }
.nkt-avoid { page-break-inside: avoid; }
</style>

{% set sales_summary = frappe.db.sql("SELECT COUNT(*) AS sale_count, COALESCE(SUM(grand_total),0) AS total_sales, COALESCE(SUM(total_account_charge),0) AS account_sales, COALESCE(SUM(total_cash),0) AS sale_cash_declared, COALESCE(SUM(total_non_cash),0) AS sale_non_cash_declared FROM `tabNKT Cashier Sale` WHERE cashier_shift=%s AND docstatus=1 AND COALESCE(status,'')!='Cancelled'", doc.name, as_dict=True)[0] %}
{% set account_sales = frappe.db.sql("SELECT sale_datetime, customer, customer_name, total_account_charge, name FROM `tabNKT Cashier Sale` WHERE cashier_shift=%s AND docstatus=1 AND COALESCE(status,'')!='Cancelled' AND COALESCE(total_account_charge,0)>0.005 ORDER BY sale_datetime, creation", doc.name, as_dict=True) %}
{% set tender_rows = frappe.db.sql("SELECT m.posting_datetime,m.customer,m.movement_type,m.payment_method,m.amount,m.source_doctype,m.source_name,m.reference_number,COALESCE(NULLIF(pd.check_number,''),NULLIF(pd.reference_number,''),NULLIF(m.reference_number,''),'') AS tender_reference,COALESCE(NULLIF(pd.bank_or_provider,''),'') AS provider,pd.check_date FROM `tabNKT Cashier Movement` m LEFT JOIN `tabNKT Payment Detail` pd ON pd.name=m.source_row WHERE m.cashier_shift=%s AND m.docstatus=1 AND m.status='Posted' AND m.direction='In' AND m.payment_method!='Cash' ORDER BY FIELD(m.payment_method,'Check','GCash','Maya','Bank Transfer','Credit Card','Online','Other Online'),m.posting_datetime,m.creation", doc.name, as_dict=True) %}
{% set collection_rows = frappe.db.sql("SELECT posting_datetime,customer,payment_method,amount,reference_number,source_name FROM `tabNKT Cashier Movement` WHERE cashier_shift=%s AND docstatus=1 AND status='Posted' AND direction='In' AND movement_type IN ('Customer Account Collection','Account Collection') ORDER BY posting_datetime,creation", doc.name, as_dict=True) %}
{% set drawing_rows = frappe.db.sql("SELECT posting_datetime,adjustment_type,party_name,purpose,amount,name FROM `tabNKT Cash Drawer Adjustment` WHERE cashier_shift=%s AND docstatus=1 AND status IN ('Posted','Reversed') AND adjustment_type IN ('Petty Cash Release','Cash Drop','Advance / Mid-Shift Deposit','Other Cash Out') ORDER BY posting_datetime,creation", doc.name, as_dict=True) %}
{% set return_rows = frappe.db.sql("SELECT return_datetime,customer,customer_name,settlement_type,calculated_return_credit,refund_due,customer_pays,return_status,name FROM `tabNKT Customer Return` WHERE cashier_shift=%s AND docstatus!=2 AND COALESCE(return_status,'')!='Cancelled' ORDER BY return_datetime,creation", doc.name, as_dict=True) %}
{% set check_total = doc.custom_nkt_check_in or 0 %}
{% set gcash_total = doc.custom_nkt_gcash_in or 0 %}
{% set maya_total = doc.custom_nkt_maya_in or 0 %}
{% set bank_total = doc.custom_nkt_bank_transfer_in or 0 %}
{% set cc_total = doc.custom_nkt_credit_card_in or 0 %}
{% set online_total = doc.custom_nkt_online_in or 0 %}
{% set noncash_net = (doc.total_non_cash_in or 0) - (doc.total_non_cash_out or 0) %}
{% set variance = doc.over_short or 0 %}

<div class="nkt-header">
  <table style="width:100%; border-collapse:collapse;">
    <tr>
      <td style="width:62%; vertical-align:bottom;">
        <div class="nkt-title">Cashier Shift Report</div>
        <div class="nkt-kicker">Independent Cashier Control Copy · End-of-Shift Cash & Tender Accountability</div>
      </td>
      <td style="width:38%; text-align:right; vertical-align:bottom;">
        <div class="nkt-value">{{ doc.company }}</div>
        <div class="nkt-muted">{{ doc.settlement_location }}</div>
      </td>
    </tr>
  </table>
  <table class="nkt-meta">
    <tr>
      <td><div class="nkt-label">Shift No.</div><div class="nkt-value">{{ doc.name }}</div></td>
      <td><div class="nkt-label">Cashier</div><div class="nkt-value">{{ doc.cashier }}</div></td>
      <td><div class="nkt-label">Opened</div><div class="nkt-value">{{ frappe.utils.format_datetime(doc.shift_start) if doc.shift_start else '' }}</div></td>
      <td><div class="nkt-label">Closed / Turned Over</div><div class="nkt-value">{{ frappe.utils.format_datetime(doc.shift_end or doc.count_locked_on) if (doc.shift_end or doc.count_locked_on) else 'Open' }}</div></td>
    </tr>
    <tr>
      <td><div class="nkt-label">Status</div><div class="nkt-value">{{ doc.status }}</div></td>
      <td><div class="nkt-label">Sales Count</div><div class="nkt-value">{{ sales_summary.sale_count or 0 }}</div></td>
      <td><div class="nkt-label">Admin Review</div><div class="nkt-value">{{ doc.approved_by or 'Awaiting review' }}</div></td>
      <td><div class="nkt-label">Printed</div><div class="nkt-value">{{ frappe.utils.format_datetime(frappe.utils.now_datetime()) }}</div></td>
    </tr>
  </table>
</div>

<table class="nkt-grid2">
<tr>
<td>
  <div class="nkt-card nkt-avoid">
    <div class="nkt-card-title">Cash Accountability</div>
    <table class="nkt-table">
      <tr><td>Opening Cash / Fresh Float</td><td class="nkt-money">{{ frappe.utils.fmt_money(doc.opening_cash or 0, currency='PHP') }}</td></tr>
      <tr><td>Cash Sales</td><td class="nkt-money">{{ frappe.utils.fmt_money(doc.custom_nkt_cash_sales or 0, currency='PHP') }}</td></tr>
      <tr><td>Cash Account Collections</td><td class="nkt-money">{{ frappe.utils.fmt_money(doc.custom_nkt_cash_account_collections or 0, currency='PHP') }}</td></tr>
      <tr><td>Petty Cash Returns</td><td class="nkt-money">{{ frappe.utils.fmt_money(doc.custom_nkt_petty_cash_returns or 0, currency='PHP') }}</td></tr>
      <tr><td>Other Cash In / Deposit Reversal</td><td class="nkt-money">{{ frappe.utils.fmt_money(doc.custom_nkt_cash_other_in or 0, currency='PHP') }}</td></tr>
      <tr><td>Less: Cash Refunds</td><td class="nkt-money">({{ frappe.utils.fmt_money(doc.custom_nkt_cash_refunds or 0, currency='PHP') }})</td></tr>
      <tr><td>Less: Petty Cash Releases</td><td class="nkt-money">({{ frappe.utils.fmt_money(doc.custom_nkt_petty_cash_releases or 0, currency='PHP') }})</td></tr>
      <tr><td>Less: Cash Drops</td><td class="nkt-money">({{ frappe.utils.fmt_money(doc.custom_nkt_cash_drops or 0, currency='PHP') }})</td></tr>
      <tr><td>Less: Advance / Mid-Shift Deposits</td><td class="nkt-money">({{ frappe.utils.fmt_money(doc.custom_nkt_advance_deposits or 0, currency='PHP') }})</td></tr>
      <tr><td>Less: Other Cash Out</td><td class="nkt-money">({{ frappe.utils.fmt_money(doc.custom_nkt_cash_other_out or 0, currency='PHP') }})</td></tr>
      <tr class="nkt-total"><td>System Expected Cash</td><td class="nkt-money">{{ frappe.utils.fmt_money(doc.expected_cash or 0, currency='PHP') }}</td></tr>
      <tr class="nkt-hero"><td><span class="nkt-muted">Cashier denomination count</span><br><strong>Actual Cash on Hand</strong></td><td class="nkt-money nkt-difference">{{ frappe.utils.fmt_money(doc.actual_cash_count or 0, currency='PHP') }}</td></tr>
      <tr><td class="nkt-strong">Over / (Short)</td><td class="nkt-money nkt-difference {% if variance < -0.005 %}nkt-negative{% elif variance > 0.005 %}nkt-positive{% endif %}">{{ frappe.utils.fmt_money(variance, currency='PHP') }}</td></tr>
    </table>
  </div>
</td>
<td>
  <div class="nkt-card nkt-avoid">
    <div class="nkt-card-title">Shift Activity Summary</div>
    <table class="nkt-table">
      <tr><td>Total Sales Declared by Cashier</td><td class="nkt-money">{{ frappe.utils.fmt_money(sales_summary.total_sales or 0, currency='PHP') }}</td></tr>
      <tr><td>On Account Sales Declared</td><td class="nkt-money">{{ frappe.utils.fmt_money(sales_summary.account_sales or 0, currency='PHP') }}</td></tr>
      <tr><td>Total Cash In</td><td class="nkt-money">{{ frappe.utils.fmt_money(doc.total_cash_in or 0, currency='PHP') }}</td></tr>
      <tr><td>Total Cash Out</td><td class="nkt-money">{{ frappe.utils.fmt_money(doc.total_cash_out or 0, currency='PHP') }}</td></tr>
      <tr><td>Total Non-Cash In</td><td class="nkt-money">{{ frappe.utils.fmt_money(doc.total_non_cash_in or 0, currency='PHP') }}</td></tr>
      <tr><td>Total Non-Cash Out</td><td class="nkt-money">{{ frappe.utils.fmt_money(doc.total_non_cash_out or 0, currency='PHP') }}</td></tr>
      <tr class="nkt-total"><td>Net Non-Cash Tender</td><td class="nkt-money">{{ frappe.utils.fmt_money(noncash_net, currency='PHP') }}</td></tr>
    </table>
  </div>

  <div class="nkt-card nkt-avoid">
    <div class="nkt-card-title">Tender Summary</div>
    <table class="nkt-table">
      <tr><td><span class="nkt-method">CHECK</span> Physical Checks</td><td class="nkt-money">{{ frappe.utils.fmt_money(check_total, currency='PHP') }}</td></tr>
      <tr><td><span class="nkt-method">GCASH</span> GCash</td><td class="nkt-money">{{ frappe.utils.fmt_money(gcash_total, currency='PHP') }}</td></tr>
      <tr><td><span class="nkt-method">MAYA</span> Maya Wallet</td><td class="nkt-money">{{ frappe.utils.fmt_money(maya_total, currency='PHP') }}</td></tr>
      <tr><td><span class="nkt-method">BANK</span> Bank Transfer</td><td class="nkt-money">{{ frappe.utils.fmt_money(bank_total, currency='PHP') }}</td></tr>
      <tr><td><span class="nkt-method">CC</span> Credit Card</td><td class="nkt-money">{{ frappe.utils.fmt_money(cc_total, currency='PHP') }}</td></tr>
      <tr><td><span class="nkt-method">ONLINE</span> Online / Other Online</td><td class="nkt-money">{{ frappe.utils.fmt_money(online_total, currency='PHP') }}</td></tr>
      <tr class="nkt-total"><td>Total Non-Cash In</td><td class="nkt-money">{{ frappe.utils.fmt_money(doc.total_non_cash_in or 0, currency='PHP') }}</td></tr>
    </table>
  </div>
</td>
</tr>
</table>

<div class="nkt-section">Cash Denomination Count</div>
<table class="nkt-table nkt-avoid" style="border:1px solid #c9d3dc;">
  <tr><th>Denomination</th><th class="nkt-center">Qty</th><th class="nkt-money">Amount</th><th>Denomination</th><th class="nkt-center">Qty</th><th class="nkt-money">Amount</th></tr>
  <tr><td>₱1,000 Bills</td><td class="nkt-center">{{ doc.bill_1000_qty or 0 }}</td><td class="nkt-money">{{ frappe.utils.fmt_money((doc.bill_1000_qty or 0)*1000, currency='PHP') }}</td><td>₱20 Coins</td><td class="nkt-center">{{ doc.coin_20_qty or 0 }}</td><td class="nkt-money">{{ frappe.utils.fmt_money((doc.coin_20_qty or 0)*20, currency='PHP') }}</td></tr>
  <tr><td>₱500 Bills</td><td class="nkt-center">{{ doc.bill_500_qty or 0 }}</td><td class="nkt-money">{{ frappe.utils.fmt_money((doc.bill_500_qty or 0)*500, currency='PHP') }}</td><td>₱10 Coins</td><td class="nkt-center">{{ doc.coin_10_qty or 0 }}</td><td class="nkt-money">{{ frappe.utils.fmt_money((doc.coin_10_qty or 0)*10, currency='PHP') }}</td></tr>
  <tr><td>₱200 Bills</td><td class="nkt-center">{{ doc.bill_200_qty or 0 }}</td><td class="nkt-money">{{ frappe.utils.fmt_money((doc.bill_200_qty or 0)*200, currency='PHP') }}</td><td>₱5 Coins</td><td class="nkt-center">{{ doc.coin_5_qty or 0 }}</td><td class="nkt-money">{{ frappe.utils.fmt_money((doc.coin_5_qty or 0)*5, currency='PHP') }}</td></tr>
  <tr><td>₱100 Bills</td><td class="nkt-center">{{ doc.bill_100_qty or 0 }}</td><td class="nkt-money">{{ frappe.utils.fmt_money((doc.bill_100_qty or 0)*100, currency='PHP') }}</td><td>₱1 Coins</td><td class="nkt-center">{{ doc.coin_1_qty or 0 }}</td><td class="nkt-money">{{ frappe.utils.fmt_money((doc.coin_1_qty or 0)*1, currency='PHP') }}</td></tr>
  <tr><td>₱50 Bills</td><td class="nkt-center">{{ doc.bill_50_qty or 0 }}</td><td class="nkt-money">{{ frappe.utils.fmt_money((doc.bill_50_qty or 0)*50, currency='PHP') }}</td><td>₱0.25 Coins</td><td class="nkt-center">{{ doc.coin_025_qty or 0 }}</td><td class="nkt-money">{{ frappe.utils.fmt_money((doc.coin_025_qty or 0)*0.25, currency='PHP') }}</td></tr>
  <tr><td>₱20 Bills</td><td class="nkt-center">{{ doc.bill_20_qty or 0 }}</td><td class="nkt-money">{{ frappe.utils.fmt_money((doc.bill_20_qty or 0)*20, currency='PHP') }}</td><td class="nkt-strong">Total Cash Count</td><td></td><td class="nkt-money nkt-strong">{{ frappe.utils.fmt_money(doc.actual_cash_count or 0, currency='PHP') }}</td></tr>
</table>

<div class="nkt-section">Non-Cash Tender Details</div>
<table class="nkt-table" style="border:1px solid #c9d3dc;">
  <tr><th>Method</th><th>Customer</th><th>Bank / Provider</th><th>Reference / Check No.</th><th>Check Date</th><th class="nkt-money">Amount</th></tr>
  {% for row in tender_rows %}
  <tr>
    <td><span class="nkt-method">{{ row.payment_method }}</span></td>
    <td>{{ row.customer or '—' }}</td>
    <td>{{ row.provider or '—' }}</td>
    <td>{{ row.tender_reference or '—' }}</td>
    <td>{{ frappe.utils.formatdate(row.check_date) if row.check_date else '—' }}</td>
    <td class="nkt-money">{{ frappe.utils.fmt_money(row.amount or 0, currency='PHP') }}</td>
  </tr>
  {% else %}
  <tr><td colspan="6" class="nkt-center nkt-muted">No non-cash tender for this shift.</td></tr>
  {% endfor %}
</table>

<table class="nkt-grid2" style="margin-top:7px;">
<tr>
<td>
  <div class="nkt-card">
    <div class="nkt-card-title">Drawings / Cash Out</div>
    <table class="nkt-table">
      <tr><th>Type / Details</th><th class="nkt-money">Amount</th></tr>
      {% for row in drawing_rows %}
      <tr><td><strong>{{ row.adjustment_type }}</strong>{% if row.party_name %}<br>{{ row.party_name }}{% endif %}{% if row.purpose %}<br><span class="nkt-muted">{{ row.purpose }}</span>{% endif %}</td><td class="nkt-money">{{ frappe.utils.fmt_money(row.amount or 0, currency='PHP') }}</td></tr>
      {% else %}
      <tr><td colspan="2" class="nkt-center nkt-muted">None</td></tr>
      {% endfor %}
    </table>
  </div>
</td>
<td>
  <div class="nkt-card">
    <div class="nkt-card-title">Account Collections Received</div>
    <table class="nkt-table">
      <tr><th>Customer</th><th>Method / Ref.</th><th class="nkt-money">Amount</th></tr>
      {% for row in collection_rows %}
      <tr><td>{{ row.customer or '—' }}</td><td>{{ row.payment_method }}{% if row.reference_number %}<br><span class="nkt-muted">{{ row.reference_number }}</span>{% endif %}</td><td class="nkt-money">{{ frappe.utils.fmt_money(row.amount or 0, currency='PHP') }}</td></tr>
      {% else %}
      <tr><td colspan="3" class="nkt-center nkt-muted">None</td></tr>
      {% endfor %}
    </table>
  </div>
</td>
</tr>
</table>

<table class="nkt-grid2">
<tr>
<td>
  <div class="nkt-card">
    <div class="nkt-card-title">On Account Sales Declared by Cashier</div>
    <table class="nkt-table">
      <tr><th>Customer</th><th class="nkt-money">Account Amount</th></tr>
      {% for row in account_sales %}
      <tr><td>{{ row.customer_name or row.customer or '—' }}</td><td class="nkt-money">{{ frappe.utils.fmt_money(row.total_account_charge or 0, currency='PHP') }}</td></tr>
      {% else %}
      <tr><td colspan="2" class="nkt-center nkt-muted">None</td></tr>
      {% endfor %}
      <tr class="nkt-total"><td>Total</td><td class="nkt-money">{{ frappe.utils.fmt_money(sales_summary.account_sales or 0, currency='PHP') }}</td></tr>
    </table>
  </div>
</td>
<td>
  <div class="nkt-card">
    <div class="nkt-card-title">Returns / Refunds / Exchanges</div>
    <table class="nkt-table">
      <tr><th>Customer / Settlement</th><th class="nkt-money">Return Credit</th><th class="nkt-money">Cash Refund</th></tr>
      {% for row in return_rows %}
      <tr><td>{{ row.customer_name or row.customer or '—' }}<br><span class="nkt-muted">{{ row.settlement_type or row.return_status }}</span></td><td class="nkt-money">{{ frappe.utils.fmt_money(row.calculated_return_credit or 0, currency='PHP') }}</td><td class="nkt-money">{{ frappe.utils.fmt_money(row.refund_due or 0, currency='PHP') }}</td></tr>
      {% else %}
      <tr><td colspan="3" class="nkt-center nkt-muted">None</td></tr>
      {% endfor %}
    </table>
  </div>
</td>
</tr>
</table>

<div class="nkt-section">Remarks / Cash Difference Explanation</div>
<div class="nkt-notes">{{ doc.count_notes or 'No additional remarks.' }}</div>

<table class="nkt-signatures">
  <tr>
    <td><div class="nkt-signline"><strong>{{ doc.cashier }}</strong><br><span class="nkt-small">Cashier - counted and turned over</span></div></td>
    <td><div class="nkt-signline"><strong>{{ doc.approved_by or '' }}</strong><br><span class="nkt-small">Owner / Administrator review</span></div></td>
  </tr>
</table>

<div class="nkt-small nkt-muted" style="margin-top:11px; border-top:1px solid #d5dde4; padding-top:4px;">
  Independent Cashier control report. Cashier expected cash is intentionally visible to the Cashier.
  Encoder Z-Out totals are independently produced and reconciled only during management review.
</div>
"""


def _ensure_property_setter(doctype, fieldname, property_name, value, property_type="Data"):
    name = f"{doctype}-{fieldname}-{property_name}"
    values = {
        "doctype_or_field": "DocField",
        "doc_type": doctype,
        "field_name": fieldname,
        "property": property_name,
        "property_type": property_type,
        "value": value,
    }
    if frappe.db.exists("Property Setter", name):
        doc = frappe.get_doc("Property Setter", name)
        doc.update(values)
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc({"doctype": "Property Setter", "name": name, **values})
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)


@frappe.whitelist()
def install():
    frappe.set_user("Administrator")

    if not frappe.db.exists("DocType", "NKT Cashier Shift"):
        frappe.throw("NKT Cashier Shift is missing.")

    accepted = frappe.db.get_value(
        "NKT Cashier Shift",
        "NKT-SHIFT-00004",
        ["status", "docstatus"],
        as_dict=True,
    )
    if not accepted or accepted.status != "Reviewed / Closed" or cint(accepted.docstatus) != 1:
        frappe.throw("C6.1 baseline guard failed: NKT-SHIFT-00004 is not the accepted Reviewed / Closed shift.")

    ord49 = frappe.get_attr(
        "nkt_operations.nkt_store_operations.features.setup_validation.internal.order_integration_verify.verify"
    )()
    if not ord49.get("passed"):
        frappe.throw("C6.1 baseline guard failed: ORD-00049 integration verifier is not passing.")

    values = {
        "doc_type": "NKT Cashier Shift",
        "module": "NKT Store Operations",
        "standard": "No",
        "custom_format": 1,
        "print_format_type": "Jinja",
        "html": PRINT_HTML,
        "disabled": 0,
    }

    if frappe.db.exists("Print Format", PRINT_FORMAT_NAME):
        pf = frappe.get_doc("Print Format", PRINT_FORMAT_NAME)
        pf.update(values)
        pf.flags.ignore_permissions = True
        pf.save(ignore_permissions=True)
    else:
        pf = frappe.get_doc({
            "doctype": "Print Format",
            "name": PRINT_FORMAT_NAME,
            **values,
        })
        pf.flags.ignore_permissions = True
        pf.insert(ignore_permissions=True)

    if frappe.get_meta("DocType").has_field("default_print_format"):
        frappe.db.set_value(
            "DocType",
            "NKT Cashier Shift",
            "default_print_format",
            PRINT_FORMAT_NAME,
            update_modified=False,
        )

    _ensure_property_setter(
        "NKT Cashier Shift",
        "blind_count_confirmed",
        "label",
        "Cashier Count Finalized",
    )

    frappe.clear_cache(doctype="NKT Cashier Shift")
    frappe.clear_cache()
    frappe.db.commit()

    return {
        "version": VERSION,
        "print_format": PRINT_FORMAT_NAME,
        "cashier_expected_cash_visible": True,
        "blind_control": "Cashier vs Encoder independent close; not Cashier vs own expected cash",
        "business_records_modified": False,
    }


@frappe.whitelist()
def verify():
    errors = []

    pf = frappe.db.get_value(
        "Print Format",
        PRINT_FORMAT_NAME,
        ["name", "doc_type", "disabled", "html"],
        as_dict=True,
    )
    html = (pf.html or "") if pf else ""
    if not pf:
        errors.append("C6.1 Cashier Shift Report print format is missing.")
    else:
        if pf.doc_type != "NKT Cashier Shift":
            errors.append("C6.1 print format is attached to the wrong DocType.")
        if cint(pf.disabled):
            errors.append("C6.1 print format is disabled.")

    required_text = [
        "Independent Cashier Control Copy",
        "System Expected Cash",
        "Actual Cash on Hand",
        "Physical Checks",
        "GCash",
        "Maya Wallet",
        "Bank Transfer",
        "Credit Card",
        "Online / Other Online",
        "Non-Cash Tender Details",
        "Drawings / Cash Out",
        "Account Collections Received",
        "On Account Sales Declared by Cashier",
        "Returns / Refunds / Exchanges",
    ]
    missing_text = [text for text in required_text if text not in html]
    if missing_text:
        errors.append("Print format is missing required sections: " + ", ".join(missing_text))

    default_pf = frappe.db.get_value("DocType", "NKT Cashier Shift", "default_print_format")
    if default_pf != PRINT_FORMAT_NAME:
        errors.append("NKT Cashier Shift default print format is not C6.1.")

    shift_script = frappe.db.get_value(
        "Client Script",
        "NKT Cashier Shift Controls V1.9",
        "script",
    ) or ""
    own_expected_visible = (
        "expected_cash_display" in shift_script
        and "difference_display" in shift_script
        and "Current Expected Cash" in shift_script
    )
    if not own_expected_visible:
        errors.append("Cashier closing dialog no longer exposes the Cashier's own expected cash.")

    from nkt_operations.nkt_store_operations.features.cashier import shift_engine as shift_control
    encoder_condition = shift_control.get_shift_permission_query_conditions("encoder@example.com")
    if encoder_condition != "1=0":
        errors.append("Encoder unexpectedly has direct Cashier Shift access.")

    accepted = frappe.db.get_value(
        "NKT Cashier Shift",
        "NKT-SHIFT-00004",
        ["status", "docstatus"],
        as_dict=True,
    )
    if not accepted or accepted.status != "Reviewed / Closed" or cint(accepted.docstatus) != 1:
        errors.append("Accepted NKT-SHIFT-00004 was changed.")

    ord49 = frappe.get_attr(
        "nkt_operations.nkt_store_operations.features.setup_validation.internal.order_integration_verify.verify"
    )()
    if not ord49.get("passed"):
        errors.append("C5/C5.5/C5.6 regression after C6.1.")

    return {
        "version": VERSION,
        "print_format": PRINT_FORMAT_NAME,
        "cashier_expected_cash_visible": own_expected_visible,
        "encoder_direct_cashier_shift_access": encoder_condition,
        "accepted_shift_00004": accepted,
        "c5_6_regression_passed": bool(ord49.get("passed")),
        "errors": errors,
        "passed": not errors,
    }
