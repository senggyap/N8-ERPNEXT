from __future__ import annotations

import hashlib
import json
import inspect
from datetime import datetime
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import cint, flt, getdate, now_datetime, nowdate
from nkt_operations.nkt_store_operations.features.payments_accounts.card_surcharge import (
    apply_payment_row_card_fields,
    ensure_card_posting_allowed,
    normalize_payment_method,
    row_collected_amount,
)

VERSION = "V2.0C.7.12A-C.1"
DOCTYPE = "NKT Return Exchange Declaration"
TOLERANCE = 0.005

ADMIN_ROLES = {"System Manager", "NKT OWNER", "NKT ADMINISTRATOR"}
SIDE_ROLE = {"Cashier": "NKT Cashier", "Encoder": "NKT Encoder"}
MONEY_METHODS = {"Cash","Check","GCash","Maya","Card","Bank Transfer","Online"}
SETTLEMENT_METHODS = MONEY_METHODS | {"Account"}
REFERENCE_METHODS = {"Check","GCash","Maya","Card","Bank Transfer","Online"}


def _money(v):
    return round(flt(v), 2)


def _qty(v):
    return round(flt(v), 6)


def _normalize_submit_request_id(value):
    value = (value or "").strip()
    if not value:
        frappe.throw(_("This Return/Exchange screen needs to be refreshed before it can be submitted. Refresh the screen and try again."))
    if len(value) > 140:
        frappe.throw(_("This Return/Exchange screen is no longer valid for submission. Refresh the screen and try again."))
    return value


def _submission_result(doc, replayed=False):
    return {
        "name": doc.name,
        "side": doc.side,
        "status": doc.reconciliation_status,
        "matched_declaration": doc.matched_declaration,
        "posting_status": doc.posting_status,
        "new_cashier_sale": doc.new_cashier_sale,
        "new_customer_order": doc.new_customer_order,
        "return_stock_entry": doc.return_stock_entry,
        "account_adjustment_record": doc.account_adjustment_record,
        "customer_credit_record": doc.customer_credit_record,
        "posting_enabled": True,
        "submit_request_id": doc.get("custom_nkt_submit_request_id") or "",
        "idempotent_replay": bool(replayed),
    }


def _existing_request_submission(request_id, expected_side):
    row = frappe.db.get_value(
        DOCTYPE,
        {"custom_nkt_submit_request_id": request_id},
        ["name", "side"],
        as_dict=True,
    )
    if not row:
        return None
    if row.side != expected_side:
        frappe.throw(_("This Return/Exchange attempt has already been used. Refresh the screen and try again."))
    return frappe.get_doc(DOCTYPE, row.name)


def _lock_submission_source(old_cashier_sale, old_customer_order):
    # Deterministic lock order is intentional. It serializes same-lineage
    # submissions so remaining quantity / basis is re-evaluated after any
    # earlier concurrent submission commits. Cashier and Encoder still remain
    # independent business declarations; this lock is only an atomicity guard.
    if old_customer_order:
        frappe.db.sql(
            "SELECT name FROM `tabNKT Customer Order` WHERE name=%s FOR UPDATE",
            old_customer_order,
        )
    if old_cashier_sale:
        frappe.db.sql(
            "SELECT name FROM `tabNKT Cashier Sale` WHERE name=%s FOR UPDATE",
            old_cashier_sale,
        )


def _has_side_role(side, user=None):
    user = user or frappe.session.user
    roles = set(frappe.get_roles(user))
    return bool(roles & ADMIN_ROLES) or SIDE_ROLE.get(side) in roles


def _assert_side(side):
    if side not in SIDE_ROLE:
        frappe.throw(_("Side must be Cashier or Encoder."))
    if not _has_side_role(side):
        frappe.throw(_("You are not allowed to create this return/exchange side."), frappe.PermissionError)


def _current_business_date():
    return getdate(nowdate())


def _source_detail(side, source_name):
    helper = frappe.get_attr("nkt_operations.nkt_store_operations.features.returns.service._detail")
    return helper(side.lower(), source_name)


def _source_pair(side, source_name):
    detail = _source_detail(side, source_name)
    if side == "Cashier":
        return source_name, detail.get("counterpart"), detail
    return detail.get("counterpart"), source_name, detail


def _legacy_returned_qty(customer_order, item, warehouse):
    if not customer_order:
        return 0.0
    rows = frappe.db.sql(
        """
        SELECT COALESCE(SUM(ri.return_quantity),0)
        FROM `tabNKT Customer Return` r
        INNER JOIN `tabNKT Customer Return Item` ri
          ON ri.parent=r.name AND ri.parenttype='NKT Customer Return'
        WHERE r.docstatus=1
          AND r.customer_order=%s
          AND ri.item=%s
          AND COALESCE(ri.original_source_warehouse,'')=%s
        """,
        (customer_order, item, warehouse or ""),
    )
    return flt(rows[0][0] if rows else 0)


def _declared_returned_qty(side, old_cashier_sale, old_customer_order, item, warehouse, exclude_name=None):
    filters = [
        "d.docstatus=1",
        "d.side=%s",
        "d.old_cashier_sale=%s",
        "d.old_customer_order=%s",
        "i.item=%s",
        "COALESCE(i.original_source_warehouse,'')=%s",
    ]
    params = [side, old_cashier_sale, old_customer_order, item, warehouse or ""]
    if exclude_name:
        filters.append("d.name!=%s")
        params.append(exclude_name)
    rows = frappe.db.sql(
        f"""
        SELECT COALESCE(SUM(i.quantity),0)
        FROM `tabNKT Return Exchange Declaration` d
        INNER JOIN `tabNKT Return Exchange Returned Item` i
          ON i.parent=d.name
         AND i.parenttype='NKT Return Exchange Declaration'
         AND i.parentfield='returned_items'
        WHERE {' AND '.join(filters)}
        """,
        tuple(params),
    )
    return flt(rows[0][0] if rows else 0)


def _prior_money_refunds(side, old_cashier_sale, old_customer_order, exclude_name=None):
    filters = [
        "docstatus=1",
        "side=%s",
        "old_cashier_sale=%s",
        "old_customer_order=%s",
    ]
    params = [side, old_cashier_sale, old_customer_order]
    if exclude_name:
        filters.append("name!=%s")
        params.append(exclude_name)
    rows = frappe.db.sql(
        f"""SELECT COALESCE(SUM(refund_money),0)
            FROM `tabNKT Return Exchange Declaration`
            WHERE {' AND '.join(filters)}""",
        tuple(params),
    )
    return flt(rows[0][0] if rows else 0)


def _get_item_context(item_code):
    row = frappe.db.get_value(
        "Item", item_code,
        ["item_name","stock_uom","disabled","is_stock_item",
         "nkt_stock_form","nkt_standard_sack_weight_kg",
         "nkt_damaged_item","nkt_fraction_item"],
        as_dict=True,
    )
    if not row or row.disabled or not row.is_stock_item:
        frappe.throw(_("Item {0} is not an active stock item.").format(item_code))
    return row


def _standard_rate(item_code):
    return flt(frappe.db.get_value(
        "Item Price",
        {"item_code":item_code,"price_list":"Standard Selling","selling":1},
        "price_list_rate",
    ))


def _source_item_map(detail):
    result = {}
    for row in detail.get("items") or []:
        result[(row["item"], row.get("source_warehouse") or "")] = row
    return result


def _canonical_returns(doc):
    # Financial treatment is independently declared by Cashier and Encoder.
    # Inventory classification remains Encoder-only and is intentionally NOT
    # part of the match key.
    rows = []
    for r in doc.get("returned_items") or []:
        treatment = (r.get("custom_nkt_return_value_treatment") or "Full Value").strip()
        value_kg = (
            _qty(r.get("custom_nkt_value_kg_received"))
            if treatment == "Deduct Missing kg"
            else 0
        )
        manual = (
            _money(r.get("custom_nkt_manual_deduction"))
            if treatment == "Manual Deduction"
            else 0
        )
        rows.append([
            r.item,
            _qty(r.quantity),
            _money(r.original_rate),
            treatment,
            value_kg,
            manual,
        ])
    return sorted(rows)


def _canonical_new(doc):
    rows = []
    for r in doc.get("new_items") or []:
        rows.append([
            r.item,
            _qty(r.quantity),
            _money(r.rate),
        ])
    return sorted(rows)



def _canonical_settlement_payments(doc):
    rows = []
    for r in doc.get("settlement_payments") or []:
        method = normalize_payment_method(r.payment_method)
        reference = (r.reference_number or "").strip()
        provider = (r.bank_or_provider or "").strip()
        check_date = str(r.check_date or "")
        rows.append([
            method,
            _money(r.amount),
            reference,
            provider,
            check_date,
        ])
    return sorted(rows)


def build_match_key(doc):
    payload = {
        "business_date": str(getdate(doc.business_date)),
        "customer": doc.customer,
        "old_cashier_sale": doc.old_cashier_sale or "",
        "old_customer_order": doc.old_customer_order or "",
        "transaction_type": doc.transaction_type,
        "returned_items": _canonical_returns(doc),
        "new_items": _canonical_new(doc),
        "return_credit": _money(doc.return_credit),
        "new_order_value": _money(doc.new_order_value),
        "customer_pays": _money(doc.customer_pays),
        "customer_pays_mode": doc.customer_pays_mode or "None",
        "charge_to_account": _money(doc.charge_to_account),
        "settlement_payments": _canonical_settlement_payments(doc),
        "refund_money": _money(doc.refund_money),
        "account_adjustment_amount": _money(doc.account_adjustment_amount),
        "customer_credit_amount": _money(doc.customer_credit_amount),
        "credit_adjustment": _money(doc.credit_adjustment),
        "settlement_destination": doc.settlement_destination or "",
        "settlement_method": doc.settlement_method or "",
        "settlement_reference": (doc.settlement_reference or "").strip(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",",":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _populate_source(doc):
    if doc.side == "Cashier":
        source_name = doc.old_cashier_sale
        if not source_name:
            frappe.throw(_("OLD ORDER selection is required."))
        old_cash, old_order, detail = _source_pair("Cashier", source_name)
    else:
        source_name = doc.old_customer_order
        if not source_name:
            frappe.throw(_("OLD ORDER selection is required."))
        old_cash, old_order, detail = _source_pair("Encoder", source_name)

    if not old_cash or not old_order:
        frappe.throw(_("The OLD ORDER must have both Cashier and Encoder sides before it can be returned/exchanged."))

    if detail.get("customer") != doc.customer:
        frappe.throw(_("OLD ORDER belongs to a different Customer."))

    doc.old_cashier_sale = old_cash
    doc.old_customer_order = old_order
    doc.company = frappe.db.get_value("NKT Customer Order", old_order, "company")
    doc.customer_name = detail.get("customer_name")
    doc.source_generation = cint(detail.get("generation"))
    return detail


def _prepare_return_rows(doc, detail):
    source_map = _source_item_map(detail)
    total_credit = 0.0
    if not doc.get("returned_items"):
        frappe.throw(_("Enter at least one returned item."))

    resolved = []
    requested_by_source = {}
    for row in doc.returned_items:
        key = (row.item, row.original_source_warehouse or "")
        source = source_map.get(key)
        if not source:
            hits = [x for (item,_wh),x in source_map.items() if item == row.item]
            if len(hits) == 1:
                source = hits[0]
                row.original_source_warehouse = source.get("source_warehouse")
                key = (row.item, row.original_source_warehouse or "")
        if not source:
            frappe.throw(_("Returned item {0} is not part of the OLD ORDER.").format(row.item))
        qty = flt(row.quantity)
        if qty <= TOLERANCE:
            frappe.throw(_("Return Qty must be greater than zero for {0}.").format(row.item))
        requested_by_source[key] = requested_by_source.get(key, 0.0) + qty
        resolved.append((row,key,source))

    for key, requested in requested_by_source.items():
        item, warehouse = key
        source = source_map.get(key)
        if not source:
            hits = [x for (it,_wh),x in source_map.items() if it == item]
            source = hits[0] if len(hits)==1 else None
        legacy = _legacy_returned_qty(doc.old_customer_order, item, warehouse)
        prior = _declared_returned_qty(
            doc.side, doc.old_cashier_sale, doc.old_customer_order,
            item, warehouse, doc.name if not doc.is_new() else None
        )
        available = max(flt(source["original_qty"]) - legacy - prior, 0)
        if requested > available + TOLERANCE:
            frappe.throw(
                _("Total Return Qty for {0} exceeds remaining returnable quantity {1}.").format(
                    item, available
                )
            )

    for row,key,source in resolved:
        ctx = _get_item_context(row.item)
        row.item_name = ctx.item_name
        row.uom = ctx.stock_uom
        row.source_row = source.get("source_row")
        legacy = _legacy_returned_qty(doc.old_customer_order, row.item, row.original_source_warehouse)
        prior = _declared_returned_qty(
            doc.side, doc.old_cashier_sale, doc.old_customer_order,
            row.item, row.original_source_warehouse, doc.name if not doc.is_new() else None
        )
        row.available_quantity = max(flt(source["original_qty"]) - legacy - prior, 0)
        row.original_rate = flt(source.get("original_rate"))
        full_value = flt(row.quantity) * flt(row.original_rate)
        row.damaged_item = ctx.nkt_damaged_item
        row.fraction_item = ctx.nkt_fraction_item

        treatment = (row.get("custom_nkt_return_value_treatment") or "Full Value").strip()
        if treatment not in {"Full Value","Deduct Missing kg","Manual Deduction"}:
            frappe.throw(_("Select Full Value, Deduct Missing kg, or Manual Deduction for {0}.").format(row.item))

        standard_kg = flt(ctx.nkt_standard_sack_weight_kg)
        expected_kg = flt(row.quantity) * standard_kg if standard_kg > TOLERANCE else 0
        actual_value_kg = flt(row.get("custom_nkt_value_kg_received"))
        manual_deduction = max(flt(row.get("custom_nkt_manual_deduction")), 0)
        deduction = 0.0

        if treatment == "Deduct Missing kg":
            if abs(flt(row.quantity) - 1.0) > TOLERANCE:
                frappe.throw(
                    _("For a partial/Fraction return, enter each sack separately. "
                      "Return Qty must be 1 for {0} when using Deduct Missing kg.").format(row.item)
                )
            if standard_kg <= TOLERANCE:
                frappe.throw(_("Standard Sack Weight is required on Item {0}.").format(row.item))
            if actual_value_kg <= TOLERANCE:
                frappe.throw(_("Actual kg Returned is required for {0}.").format(row.item))
            if actual_value_kg > expected_kg + TOLERANCE:
                frappe.throw(
                    _("Actual kg Returned exceeds the expected {0} kg for {1}.").format(
                        expected_kg, row.item
                    )
                )
            missing_kg = max(expected_kg - actual_value_kg, 0)
            deduction = full_value * (missing_kg / expected_kg) if expected_kg > TOLERANCE else 0
        elif treatment == "Manual Deduction":
            if manual_deduction > full_value + TOLERANCE:
                frappe.throw(_("Manual Deduction cannot exceed the full returned value for {0}.").format(row.item))
            deduction = manual_deduction
            missing_kg = max(expected_kg - actual_value_kg, 0) if actual_value_kg > TOLERANCE else 0
        else:
            missing_kg = max(expected_kg - actual_value_kg, 0) if actual_value_kg > TOLERANCE else 0

        row.custom_nkt_expected_kg = _qty(expected_kg)
        row.custom_nkt_missing_kg = _qty(missing_kg)
        row.custom_nkt_value_deduction = _money(deduction)
        row.credit_amount = _money(max(full_value - deduction, 0))

        # Physical Fraction receipt remains Encoder-controlled. Actual kg is a
        # single operator-facing quantity on Encoder and is copied to fraction_kg.
        if doc.side == "Cashier":
            row.classification = ""
            row.fraction_kg = 0
        else:
            if not row.classification:
                frappe.throw(
                    _("Select Return Stock As for {0}. "
                      "The system will not assume Saleable stock.").format(row.item)
                )
            if row.classification == "Damaged" and not row.damaged_item:
                frappe.throw(_("Item {0} has no designated Damaged Item.").format(row.item))
            if row.classification == "Fraction":
                if not row.fraction_item:
                    frappe.throw(_("Item {0} has no designated Fraction Item.").format(row.item))
                if abs(flt(row.quantity) - 1.0) > TOLERANCE:
                    frappe.throw(
                        _("Enter each partial sack as a separate return row. "
                          "Fraction Return Qty must be 1 for {0}.").format(row.item)
                    )
                if flt(row.fraction_kg) <= TOLERANCE:
                    frappe.throw(_("Actual kg Returned is required for Fraction item {0}.").format(row.item))
                max_kg = flt(row.quantity) * standard_kg
                if standard_kg <= TOLERANCE:
                    frappe.throw(_("Standard Sack Weight is required on Item {0}.").format(row.item))
                if flt(row.fraction_kg) > max_kg + TOLERANCE:
                    frappe.throw(_("Actual kg Returned exceeds the returned sack weight for {0}.").format(row.item))
                if treatment == "Deduct Missing kg":
                    if abs(flt(row.fraction_kg) - actual_value_kg) > 0.01:
                        frappe.throw(
                            _("Encoder Actual kg Returned must be the same quantity used for the "
                              "Deduct Missing kg calculation on {0}.").format(row.item)
                        )
            if row.classification not in {"Saleable","Damaged","Fraction","Rejected"}:
                frappe.throw(_("Select a valid inventory classification for {0}.").format(row.item))

            if (
                actual_value_kg > TOLERANCE
                and expected_kg > TOLERANCE
                and actual_value_kg < expected_kg - TOLERANCE
                and row.classification in {"Saleable","Damaged"}
            ):
                frappe.throw(
                    _(
                        "Actual kg Returned is only {0} kg out of {1} kg for {2}. "
                        "A partial-weight return cannot be posted as a whole {3} sack. "
                        "Choose Fraction (or Rejected if no stock is accepted)."
                    ).format(
                        actual_value_kg,
                        expected_kg,
                        row.item,
                        row.classification,
                    )
                )

        # Operational loss/tally: if physically fewer kg came back than expected
        # and the customer deduction is smaller than the proportional missing
        # value, the difference is borne by the business. This is an audit/tally
        # amount only; it does not create a payroll deduction or GL posting here.
        physical_kg = (
            flt(row.fraction_kg)
            if doc.side == "Encoder" and row.classification == "Fraction"
            else actual_value_kg
        )
        theoretical_missing_value = 0.0
        if expected_kg > TOLERANCE and physical_kg > TOLERANCE:
            physical_missing = max(expected_kg - physical_kg, 0)
            theoretical_missing_value = full_value * (physical_missing / expected_kg)
        row.custom_nkt_business_absorbed_value = _money(
            max(theoretical_missing_value - deduction, 0)
        )

        total_credit += row.credit_amount

    doc.return_credit = _money(total_credit)


def _prepare_new_rows(doc):
    total = 0.0
    rows = doc.get("new_items") or []
    if doc.transaction_type == "Exchange" and not rows:
        frappe.throw(_("An Exchange requires at least one NEW ORDER item."))
    if doc.transaction_type == "Return" and rows:
        frappe.throw(_("A pure Return must not contain NEW ORDER items."))

    returned_items = {r.item: r for r in doc.get("returned_items") or []}
    for row in rows:
        ctx = _get_item_context(row.item)
        qty = flt(row.quantity)
        if qty <= TOLERANCE:
            frappe.throw(_("NEW ORDER quantity must be greater than zero for {0}.").format(row.item))

        row.item_name = ctx.item_name
        row.uom = ctx.stock_uom

        same_item = row.item in returned_items
        default_rate = (
            flt(returned_items[row.item].original_rate)
            if same_item
            else _standard_rate(row.item)
        )
        requested_rate = flt(row.rate)

        if requested_rate > TOLERANCE:
            rate = requested_rate
        else:
            rate = default_rate

        if rate <= TOLERANCE:
            frappe.throw(_("Enter a valid selling rate for NEW ORDER item {0}.").format(row.item))

        if same_item and abs(rate - flt(returned_items[row.item].original_rate)) <= TOLERANCE:
            row.rate_source = "Original Sale Rate"
        elif abs(rate - _standard_rate(row.item)) <= TOLERANCE:
            row.rate_source = "Current Selling Rate"
        else:
            row.rate_source = "Manual Rate"

        row.rate = rate
        row.amount = qty * rate
        total += row.amount

        if doc.side == "Cashier":
            row.source_warehouse = ""
        else:
            if not row.source_warehouse:
                frappe.throw(_("Encoder must select Source Warehouse for NEW ORDER item {0}.").format(row.item))

    doc.new_order_value = _money(total)


def _prior_return_credit(side, old_cashier_sale, old_customer_order, exclude_name=None):
    filters = [
        "docstatus=1",
        "side=%s",
        "old_cashier_sale=%s",
        "old_customer_order=%s",
    ]
    params = [side, old_cashier_sale, old_customer_order]
    if exclude_name:
        filters.append("name!=%s")
        params.append(exclude_name)
    rows = frappe.db.sql(
        f"""SELECT COALESCE(SUM(return_credit),0)
            FROM `tabNKT Return Exchange Declaration`
            WHERE {' AND '.join(filters)}""",
        tuple(params),
    )
    return flt(rows[0][0] if rows else 0)


def _prepare_customer_payments(doc):
    rows = doc.get("settlement_payments") or []
    if not rows:
        frappe.throw(_("Complete the Payment Settlement for the amount the Customer owes."))

    running = 0.0
    account_total = 0.0
    actual_money_total = 0.0
    cash_count = 0

    for idx, row in enumerate(rows, start=1):
        apply_payment_row_card_fields(row)
        method = normalize_payment_method(row.payment_method)
        ensure_card_posting_allowed(method, "Card Return/Exchange settlement")
        amount = flt(row.amount)
        reference = (row.reference_number or "").strip()
        provider = (row.bank_or_provider or "").strip()

        if method not in SETTLEMENT_METHODS:
            frappe.throw(_("Unsupported settlement method on row {0}: {1}").format(idx, method or "(blank)"))
        if amount <= TOLERANCE:
            frappe.throw(_("Settlement amount must be greater than zero on row {0}.").format(idx))

        if method == "Cash":
            cash_count += 1
            if cash_count > 1:
                frappe.throw(_("Only one Cash row is allowed in the Return/Exchange settlement."))
            if doc.side == "Cashier":
                tendered = flt(row.cash_tendered)
                if tendered + TOLERANCE < amount:
                    frappe.throw(_("Cash Tendered is less than the Cash amount on settlement row {0}.").format(idx))
                row.change_amount = _money(max(tendered - amount, 0))
            else:
                row.cash_tendered = 0
                row.change_amount = 0
            row.reference_number = ""
            row.bank_or_provider = ""
            row.check_date = None
        elif method == "Account":
            row.cash_tendered = 0
            row.change_amount = 0
            row.reference_number = ""
            row.bank_or_provider = ""
            row.check_date = None
            account_total += amount
        elif method == "Check":
            row.cash_tendered = 0
            row.change_amount = 0
            if not reference:
                frappe.throw(_("Check Number is required on settlement row {0}.").format(idx))
            if not provider:
                frappe.throw(_("Issuing Bank is required for Check on settlement row {0}.").format(idx))
            if not row.check_date:
                frappe.throw(_("Check Date is required on settlement row {0}.").format(idx))
            actual_money_total += row_collected_amount(row)
        else:
            row.cash_tendered = 0
            row.change_amount = 0
            if method in REFERENCE_METHODS and not reference:
                frappe.throw(_("Reference Number is required for {0} on settlement row {1}.").format(method, idx))
            actual_money_total += row_collected_amount(row)

        if method == "Cash":
            actual_money_total += row_collected_amount(row)
        running += amount

    if abs(running - flt(doc.customer_pays)) > 0.01:
        frappe.throw(
            _("Payment Settlement total {0} must equal the Customer Pays amount {1}.").format(
                frappe.format_value(running, {"fieldtype": "Currency"}),
                frappe.format_value(doc.customer_pays, {"fieldtype": "Currency"}),
            )
        )

    doc.charge_to_account = _money(account_total)
    if account_total > TOLERANCE and actual_money_total > TOLERANCE:
        doc.customer_pays_mode = "Split Settlement"
    elif account_total > TOLERANCE:
        doc.customer_pays_mode = "Charge Difference to Account"
    else:
        doc.customer_pays_mode = "Pay Now"

    # These legacy fields are used only for money going BACK to the customer.
    doc.settlement_method = ""
    doc.settlement_reference = ""


def _prepare_financials(doc, detail):
    source_total = max(flt(detail.get("total")), 0)
    source_money = max(flt(detail.get("money_basis")), 0)
    source_account = max(flt(detail.get("account_basis")), 0)

    basis_total = source_money + source_account
    if source_total <= TOLERANCE:
        frappe.throw(_("OLD ORDER has no valid value basis."))
    if basis_total <= TOLERANCE:
        source_account = source_total
        basis_total = source_total

    if abs(basis_total - source_total) > TOLERANCE:
        source_account = max(source_total - source_money, 0)

    prior_credit = _prior_return_credit(
        doc.side, doc.old_cashier_sale, doc.old_customer_order,
        doc.name if not doc.is_new() else None,
    )
    prior_ratio = min(max(prior_credit / source_total, 0), 1)
    remaining_money = max(source_money * (1 - prior_ratio), 0)
    remaining_account = max(source_account * (1 - prior_ratio), 0)

    credit = flt(doc.return_credit)
    money_share = source_money / source_total if source_total else 0
    return_money = min(credit * money_share, remaining_money)
    return_account = min(max(credit - return_money, 0), remaining_account)
    missing = max(credit - return_money - return_account, 0)
    if missing > TOLERANCE:
        return_account += missing

    doc.real_money_basis_remaining = _money(remaining_money)
    doc.account_credit_basis_remaining = _money(remaining_account)
    doc.return_money_basis = _money(return_money)
    doc.return_account_basis = _money(return_account)

    new_value = flt(doc.new_order_value)
    doc.customer_pays = _money(max(new_value - credit, 0))
    difference_back = _money(max(credit - new_value, 0))

    doc.refund_money = 0
    doc.account_adjustment_amount = 0
    doc.customer_credit_amount = 0
    doc.credit_adjustment = 0
    doc.charge_to_account = 0

    destination = doc.settlement_destination or "None"

    if doc.customer_pays > TOLERANCE:
        doc.settlement_destination = "None"
        _prepare_customer_payments(doc)
    else:
        # No amount is owed by the customer; payment grid must be empty.
        doc.set("settlement_payments", [])
        doc.customer_pays_mode = "None"
        doc.charge_to_account = 0

        if difference_back <= TOLERANCE:
            doc.settlement_destination = "None"
            doc.settlement_method = ""
            doc.settlement_reference = ""
        elif destination == "Refund Money":
            doc.refund_money = _money(min(difference_back, return_money))
            remaining = _money(difference_back - doc.refund_money)
            doc.account_adjustment_amount = _money(min(remaining, return_account))
            doc.customer_credit_amount = _money(remaining - doc.account_adjustment_amount)

            # If no actual money can be refunded, do not pretend a refund method exists.
            if doc.refund_money <= TOLERANCE:
                doc.settlement_method = ""
                doc.settlement_reference = ""
            else:
                if not doc.settlement_method:
                    frappe.throw(_("Refund Money Method is required for the actual refund."))
                if doc.settlement_method not in MONEY_METHODS:
                    frappe.throw(_("Invalid Refund Money Method."))
                if doc.settlement_method in REFERENCE_METHODS and not (doc.settlement_reference or "").strip():
                    frappe.throw(_("Reference Number is required for the selected Refund Money Method."))
        elif destination == "Account Adjustment":
            doc.account_adjustment_amount = _money(min(difference_back, return_account))
            doc.customer_credit_amount = _money(difference_back - doc.account_adjustment_amount)
            doc.settlement_method = ""
            doc.settlement_reference = ""
        elif destination == "Customer Credit":
            doc.customer_credit_amount = difference_back
            doc.settlement_method = ""
            doc.settlement_reference = ""
        else:
            frappe.throw(_("Choose Refund Money, Customer Credit, or Account Adjustment for the amount due back."))

    doc.credit_adjustment = _money(
        flt(doc.account_adjustment_amount) + flt(doc.customer_credit_amount)
    )

    if flt(doc.refund_money) > flt(doc.return_money_basis) + TOLERANCE:
        frappe.throw(_("Actual refund cannot exceed the real-money basis of the returned value."))

    if abs(
        flt(doc.refund_money)
        + flt(doc.account_adjustment_amount)
        + flt(doc.customer_credit_amount)
        - difference_back
    ) > TOLERANCE:
        frappe.throw(_("Refund/Credit settlement does not tally to the amount due back."))



def prepare_declaration(doc):
    if not doc.side:
        return
    _assert_side(doc.side)

    primary_offline = bool(doc.flags.get("nkt_primary_offline_materialization"))
    if doc.is_new() or doc.docstatus == 0:
        if not primary_offline:
            doc.entry_user = frappe.session.user
            if not doc.entry_datetime:
                doc.entry_datetime = now_datetime()
            doc.business_date = _current_business_date()
        else:
            if not doc.entry_user or not doc.entry_datetime or not doc.business_date:
                frappe.throw(
                    _("Primary offline Return/Exchange materialization is missing immutable entry identity/time.")
                )

    if not doc.customer:
        frappe.throw(_("Customer is required."))

    detail = _populate_source(doc)
    if (
        not primary_offline
        and getdate(doc.business_date) != _current_business_date()
    ):
        frappe.throw(_("Return/Exchange entries must use the current live business date."))

    _prepare_return_rows(doc, detail)
    _prepare_new_rows(doc)
    _prepare_financials(doc, detail)
    doc.match_key = build_match_key(doc)
    if doc.docstatus == 0:
        doc.reconciliation_status = "Unmatched"
        doc.matched_declaration = None
        doc.exact_candidate_count = 0


def validate_declaration(doc):
    _assert_side(doc.side)
    if doc.entry_user != frappe.session.user and not (set(frappe.get_roles()) & ADMIN_ROLES):
        frappe.throw(_("Entered By must be the logged-in user."), frappe.PermissionError)

    if doc.match_key:
        duplicate = frappe.db.get_value(
            DOCTYPE,
            {
                "docstatus": 1,
                "side": doc.side,
                "business_date": doc.business_date,
                "customer": doc.customer,
                "match_key": doc.match_key,
                "name": ["!=", doc.name],
            },
            "name",
        )
        if duplicate:
            frappe.throw(
                _("This exact {0} Return/Exchange was already submitted as {1}. Use My Recent instead of submitting it again.").format(
                    doc.side, duplicate
                )
            )

    if doc.transaction_type not in {"Return","Exchange"}:
        frappe.throw(_("Select Return or Exchange."))

    if doc.side == "Cashier":
        for row in doc.get("returned_items") or []:
            if row.classification:
                frappe.throw(_("Cashier does not control inventory classification."))
        for row in doc.get("new_items") or []:
            if row.source_warehouse:
                frappe.throw(_("Cashier does not control the NEW ORDER source warehouse."))
    else:
        if not doc.return_warehouse:
            frappe.throw(_("Encoder must select the Return Receiving Warehouse."))


def _exact_candidates(doc):
    opposite = "Encoder" if doc.side == "Cashier" else "Cashier"
    return frappe.get_all(
        DOCTYPE,
        filters={
            "docstatus": 1,
            "side": opposite,
            "business_date": doc.business_date,
            "customer": doc.customer,
            "match_key": doc.match_key,
            "reconciliation_status": ["in", ["Unmatched","Ambiguous"]],
        },
        pluck="name",
        order_by="creation asc",
    )


def _set_match(a_name, b_name):
    for name, other in ((a_name,b_name),(b_name,a_name)):
        frappe.db.set_value(
            DOCTYPE, name,
            {
                "reconciliation_status":"Matched",
                "matched_declaration":other,
                "exact_candidate_count":1,
            },
            update_modified=False,
        )

    from nkt_operations.nkt_store_operations.features.returns.posting import (
        finalize_matched_pair,
    )
    finalize_matched_pair(a_name, b_name)


def try_match(name):
    doc = frappe.get_doc(DOCTYPE, name)
    if doc.docstatus != 1 or doc.reconciliation_status == "Matched":
        return
    candidates = _exact_candidates(doc)
    count = len(candidates)
    if count == 1:
        _set_match(doc.name, candidates[0])
    elif count > 1:
        frappe.db.set_value(
            DOCTYPE, doc.name,
            {"reconciliation_status":"Ambiguous","exact_candidate_count":count},
            update_modified=False,
        )
    else:
        frappe.db.set_value(
            DOCTYPE, doc.name,
            {"reconciliation_status":"Unmatched","exact_candidate_count":0},
            update_modified=False,
        )


def submit_declaration(doc):
    # Cashier and Encoder are operationally independent.
    # Each side posts ONLY its own authoritative effects immediately.
    # Exact matching is reconciliation afterward, not permission to post.
    from nkt_operations.nkt_store_operations.features.returns.posting import (
        post_independent_declaration,
    )
    post_independent_declaration(doc.name)
    try_match(doc.name)


def cancel_declaration(doc):
    controlled_reversal = bool(doc.flags.get("nkt_controlled_reversal"))

    if doc.posting_status == "Posted" and not controlled_reversal:
        frappe.throw(
            _("A posted Return/Exchange cannot be cancelled directly because its independent stock/money/order effects are already operational. Use the controlled reversal workflow.")
        )

    other = doc.matched_declaration
    if other and frappe.db.exists(DOCTYPE, other):
        frappe.db.set_value(
            DOCTYPE, other,
            {
                "reconciliation_status":"Unmatched",
                "matched_declaration":None,
                "exact_candidate_count":0,
            },
            update_modified=False,
        )
    doc.db_set("reconciliation_status","Cancelled",update_modified=False)
    if controlled_reversal:
        doc.db_set("posting_status","Cancelled",update_modified=False)


@frappe.whitelist()
def build_draft(side, source_name):
    side_label = "Cashier" if (side or "").lower()=="cashier" else "Encoder"
    _assert_side(side_label)
    detail = _source_detail(side_label.lower(), source_name)
    old_cash, old_order, detail = _source_pair(side_label, source_name)

    return {
        "version": VERSION,
        "side": side_label,
        "company": frappe.db.get_value("NKT Customer Order", old_order, "company"),
        "business_date": str(_current_business_date()),
        "customer": detail.get("customer"),
        "customer_name": detail.get("customer_name"),
        "old_cashier_sale": old_cash,
        "old_customer_order": old_order,
        "source_generation": cint(detail.get("generation")),
        "real_money_basis": _money(detail.get("money_basis")),
        "account_credit_basis": _money(detail.get("account_basis")),
        "items": detail.get("items") or [],
    }


def _doc_from_payload(side, payload):
    side_label = "Cashier" if (side or "").lower()=="cashier" else "Encoder"
    _assert_side(side_label)
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    payload = payload or {}

    source_name = payload.get("source_name")
    draft = build_draft(side_label.lower(), source_name)

    doc = frappe.new_doc(DOCTYPE)
    doc.side = side_label
    doc.company = draft["company"]
    doc.business_date = _current_business_date()
    doc.entry_datetime = now_datetime()
    doc.entry_user = frappe.session.user
    doc.customer = draft["customer"]
    doc.customer_name = draft["customer_name"]
    doc.old_cashier_sale = draft["old_cashier_sale"]
    doc.old_customer_order = draft["old_customer_order"]
    doc.source_generation = draft["source_generation"]
    doc.transaction_type = payload.get("transaction_type") or "Return"
    doc.settlement_destination = payload.get("settlement_destination") or "None"
    doc.customer_pays_mode = "None"
    doc.settlement_method = payload.get("settlement_method") or ""
    doc.settlement_reference = payload.get("settlement_reference") or ""
    doc.return_warehouse = payload.get("return_warehouse") if side_label=="Encoder" else None

    for row in payload.get("settlement_payments") or []:
        doc.append("settlement_payments", {
            "payment_method": row.get("payment_method") or row.get("method"),
            "amount": row.get("amount"),
            "cash_tendered": row.get("cash_tendered") if side_label=="Cashier" else 0,
            "change_amount": row.get("change_amount") if side_label=="Cashier" else 0,
            "reference_number": row.get("reference_number") or row.get("reference") or "",
            "bank_or_provider": row.get("bank_or_provider") or row.get("provider") or "",
            "check_date": row.get("check_date") or None,
        })
    doc.notes = payload.get("notes") or ""

    for row in payload.get("returned_items") or []:
        actual_kg = row.get("actual_kg_returned") or row.get("value_kg_received") or 0
        doc.append("returned_items", {
            "item": row.get("item"),
            "quantity": row.get("quantity"),
            "original_source_warehouse": row.get("original_source_warehouse"),
            "classification": row.get("classification") if side_label=="Encoder" else "",
            "fraction_kg": actual_kg if side_label=="Encoder" and row.get("classification")=="Fraction" else 0,
            "custom_nkt_return_value_treatment": row.get("return_value_treatment") or "Full Value",
            "custom_nkt_value_kg_received": actual_kg,
            "custom_nkt_manual_deduction": row.get("manual_deduction") or 0,
        })

    for row in payload.get("new_items") or []:
        doc.append("new_items", {
            "item": row.get("item"),
            "quantity": row.get("quantity"),
            "rate": row.get("rate"),
            "source_warehouse": row.get("source_warehouse") if side_label=="Encoder" else "",
        })
    return doc


def _primary_offline_local_db_datetime(value, label):
    """
    Convert immutable offset-aware Store-Edge time to the equivalent
    Asia/Manila local *naive* datetime required by Frappe/MariaDB Datetime
    fields. The original offset-aware value remains preserved in the immutable
    Primary journal/payload hash; this conversion is only for canonical DB
    document fields.
    """
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(raw)
    except Exception as exc:
        frappe.throw(_(f"{label} is not a valid datetime."))
    if dt.tzinfo is not None:
        dt = dt.astimezone(ZoneInfo("Asia/Manila")).replace(tzinfo=None)
    return dt


def build_primary_offline_declaration(
    normalized,
    *,
    event_uuid,
    payload_sha256,
    physical_settled_at,
):
    """
    Build the canonical C7 declaration from a preserved immutable Store-Edge
    intent without converting its physical date/time into today's online date.

    This helper does not insert/submit. The Primary materializer owns locking,
    user context, transaction scope, idempotency, and final submit.
    """
    if not isinstance(normalized, dict):
        frappe.throw(_("The saved Return/Exchange details could not be verified. Refresh the screen and try again. If the problem continues, ask an administrator for help."))

    side = normalized.get("side")
    if side not in {"Cashier", "Encoder"}:
        frappe.throw(_("Preserved Return/Exchange side is invalid."))

    doc = frappe.new_doc(DOCTYPE)
    doc.side = side
    doc.company = normalized.get("company")
    doc.business_date = getdate(normalized.get("business_date"))
    doc.entry_datetime = _primary_offline_local_db_datetime(
        normalized.get("entry_datetime"),
        "Preserved Return/Exchange entry time",
    )
    doc.entry_user = normalized.get("entry_user")
    doc.customer = normalized.get("customer")
    doc.old_cashier_sale = normalized.get("old_cashier_sale")
    doc.old_customer_order = normalized.get("old_customer_order")
    doc.source_generation = cint(normalized.get("source_generation") or 0)
    doc.transaction_type = normalized.get("transaction_type") or "Return"
    doc.settlement_destination = normalized.get("settlement_destination") or "None"
    doc.customer_pays_mode = "None"
    doc.settlement_method = normalized.get("settlement_method") or ""
    doc.settlement_reference = normalized.get("settlement_reference") or ""
    doc.return_warehouse = (
        normalized.get("return_warehouse") if side == "Encoder" else None
    )
    doc.notes = normalized.get("notes") or ""
    doc.custom_nkt_submit_request_id = normalized.get("submit_request_id")
    doc.custom_nkt_offline_event_uuid = event_uuid
    doc.custom_nkt_offline_payload_sha256 = payload_sha256
    doc.custom_nkt_offline_cashier_shift = (
        normalized.get("cashier_shift") if side == "Cashier" else None
    )
    doc.custom_nkt_offline_physical_settled_at = _primary_offline_local_db_datetime(
        physical_settled_at,
        "Preserved Return/Exchange settled time",
    )

    for row in normalized.get("settlement_payments") or []:
        doc.append(
            "settlement_payments",
            {
                "payment_method": row.get("payment_method"),
                "amount": row.get("amount"),
                "cash_tendered": row.get("cash_tendered") if side == "Cashier" else 0,
                "change_amount": 0,
                "reference_number": row.get("reference_number") or "",
                "bank_or_provider": row.get("bank_or_provider") or "",
                "check_date": row.get("check_date") or None,
            },
        )

    for row in normalized.get("returned_items") or []:
        actual_kg = row.get("actual_kg_returned") or 0
        doc.append(
            "returned_items",
            {
                "item": row.get("item"),
                "quantity": row.get("quantity"),
                "original_source_warehouse": row.get("original_source_warehouse"),
                "classification": row.get("classification") if side == "Encoder" else "",
                "fraction_kg": (
                    actual_kg
                    if side == "Encoder" and row.get("classification") == "Fraction"
                    else 0
                ),
                "custom_nkt_return_value_treatment": row.get("return_value_treatment")
                or "Full Value",
                "custom_nkt_value_kg_received": actual_kg,
                "custom_nkt_manual_deduction": row.get("manual_deduction") or 0,
            },
        )

    for row in normalized.get("new_items") or []:
        doc.append(
            "new_items",
            {
                "item": row.get("item"),
                "quantity": row.get("quantity"),
                "rate": row.get("rate"),
                "source_warehouse": (
                    row.get("source_warehouse") if side == "Encoder" else ""
                ),
            },
        )

    doc.flags.nkt_primary_offline_materialization = True
    doc.flags.ignore_permissions = True
    return doc


@frappe.whitelist()
def preview_payload(side, payload):
    doc = _doc_from_payload(side, payload)
    prepare_declaration(doc)
    validate_declaration(doc)
    return {
        "version": VERSION,
        "side": doc.side,
        "transaction_type": doc.transaction_type,
        "return_credit": _money(doc.return_credit),
        "new_order_value": _money(doc.new_order_value),
        "customer_pays": _money(doc.customer_pays),
        "customer_pays_mode": doc.customer_pays_mode or "None",
        "charge_to_account": _money(doc.charge_to_account),
        "refund_money": _money(doc.refund_money),
        "account_adjustment_amount": _money(doc.account_adjustment_amount),
        "customer_credit_amount": _money(doc.customer_credit_amount),
        "credit_adjustment": _money(doc.credit_adjustment),
        "return_money_basis": _money(doc.return_money_basis),
        "return_account_basis": _money(doc.return_account_basis),
        "real_money_basis_remaining": _money(doc.real_money_basis_remaining),
        "account_credit_basis_remaining": _money(doc.account_credit_basis_remaining),
        "settlement_payments": [
            {
                "payment_method": r.payment_method,
                "amount": _money(r.amount),
                "cash_tendered": _money(r.cash_tendered),
                "change_amount": _money(r.change_amount),
                "reference_number": r.reference_number or "",
                "bank_or_provider": r.bank_or_provider or "",
                "check_date": str(r.check_date or ""),
            }
            for r in (doc.get("settlement_payments") or [])
        ],
        "settlement_status": (
            "PAYMENT OK"
            if flt(doc.customer_pays) > TOLERANCE
            else ("REFUND OK" if flt(doc.refund_money) > TOLERANCE else "SETTLEMENT OK")
        ),
        "returned_items": [
            {
                "item":r.item,
                "quantity":flt(r.quantity),
                "original_rate":flt(r.original_rate),
                "credit_amount":flt(r.credit_amount),
                "classification":r.classification,
                "fraction_kg":flt(r.fraction_kg),
                "return_value_treatment":r.get("custom_nkt_return_value_treatment") or "Full Value",
                "actual_kg_returned":flt(r.get("custom_nkt_value_kg_received")),
                "expected_kg":flt(r.get("custom_nkt_expected_kg")),
                "missing_kg":flt(r.get("custom_nkt_missing_kg")),
                "value_deduction":flt(r.get("custom_nkt_value_deduction")),
                "manual_deduction":flt(r.get("custom_nkt_manual_deduction")),
                "business_absorbed_value":flt(r.get("custom_nkt_business_absorbed_value")),
            }
            for r in doc.returned_items
        ],
        "new_items": [
            {
                "item":r.item,
                "quantity":flt(r.quantity),
                "rate":flt(r.rate),
                "amount":flt(r.amount),
                "rate_source":r.rate_source,
                "source_warehouse":r.source_warehouse,
            }
            for r in doc.new_items
        ],
    }


@frappe.whitelist()
def submit_from_payload(side, payload):
    side_label = "Cashier" if (side or "").lower() == "cashier" else "Encoder"
    _assert_side(side_label)
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    payload = payload or {}

    request_id = _normalize_submit_request_id(payload.get("submit_request_id"))

    # Fast replay path. A retry of an already-completed browser request returns
    # the original declaration rather than creating another business event.
    existing = _existing_request_submission(request_id, side_label)
    if existing:
        return _submission_result(existing, replayed=True)

    # Resolve and then lock the immutable OLD pair in one deterministic order.
    # The second same-lineage request waits here and then re-runs validation
    # against the quantity/basis consumed by the first request.
    source_name = payload.get("source_name")
    draft = build_draft(side_label.lower(), source_name)
    _lock_submission_source(draft.get("old_cashier_sale"), draft.get("old_customer_order"))

    # Re-check after acquiring the source lock. This is the critical replay
    # check for simultaneous double-F12/network retry requests.
    existing = _existing_request_submission(request_id, side_label)
    if existing:
        return _submission_result(existing, replayed=True)

    doc = _doc_from_payload(side_label.lower(), payload)
    doc.custom_nkt_submit_request_id = request_id
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    doc.submit()
    doc.reload()
    return _submission_result(doc, replayed=False)


@frappe.whitelist()
def get_own_recent(side, limit=30):
    side_label = "Cashier" if (side or "").lower()=="cashier" else "Encoder"
    _assert_side(side_label)
    filters = {"side":side_label,"entry_user":frappe.session.user}
    return frappe.get_all(
        DOCTYPE, filters=filters,
        fields=["name","entry_datetime","customer_name","transaction_type","return_credit","new_order_value",
                "customer_pays","customer_pays_mode","refund_money","account_adjustment_amount",
                "customer_credit_amount","credit_adjustment","reconciliation_status","matched_declaration",
                "posting_status","new_cashier_sale","new_customer_order"],
        order_by="entry_datetime desc", limit_page_length=min(cint(limit) or 30,100),
    )


def _ensure_server_script_permissions():
    # Parent stays Admin-only through normal Desk. Frontline uses whitelisted APIs above.
    return True


@frappe.whitelist()
def install():
    frappe.set_user("Administrator")
    if not frappe.db.exists("DocType", DOCTYPE):
        frappe.throw(_("Return/Exchange setup is incomplete. Please ask an administrator to finish the system setup before continuing."))

    c71 = frappe.get_attr("nkt_operations.nkt_store_operations.features.returns.service.verify")()
    c63 = frappe.get_attr("nkt_operations.nkt_store_operations.features.cashier.reconciliation.verify")()
    if not c71.get("passed") or not c63.get("passed"):
        frappe.throw(_("Accepted C7.1/C6 baseline is not passing."))

    create_custom_fields(
        {
            "NKT Return Exchange Declaration": [
                {
                    "fieldname":"custom_nkt_submit_request_id",
                    "label":"Submit Request ID",
                    "fieldtype":"Data",
                    "length":140,
                    "read_only":1,
                    "hidden":1,
                    "no_copy":1,
                    "unique":1,
                    "insert_after":"entry_user",
                },
            ],
            "NKT Return Exchange Returned Item": [
                {
                    "fieldname":"custom_nkt_return_value_treatment",
                    "label":"Return Value Treatment",
                    "fieldtype":"Select",
                    "options":"Full Value\nDeduct Missing kg\nManual Deduction",
                    "default":"Full Value",
                    "insert_after":"fraction_kg",
                },
                {
                    "fieldname":"custom_nkt_value_kg_received",
                    "label":"Actual kg Returned",
                    "fieldtype":"Float",
                    "precision":"3",
                    "insert_after":"custom_nkt_return_value_treatment",
                },
                {
                    "fieldname":"custom_nkt_manual_deduction",
                    "label":"Manual Deduction",
                    "fieldtype":"Currency",
                    "insert_after":"custom_nkt_value_kg_received",
                },
                {
                    "fieldname":"custom_nkt_expected_kg",
                    "label":"Expected kg",
                    "fieldtype":"Float",
                    "precision":"3",
                    "read_only":1,
                    "insert_after":"custom_nkt_manual_deduction",
                },
                {
                    "fieldname":"custom_nkt_missing_kg",
                    "label":"Missing kg",
                    "fieldtype":"Float",
                    "precision":"3",
                    "read_only":1,
                    "insert_after":"custom_nkt_expected_kg",
                },
                {
                    "fieldname":"custom_nkt_value_deduction",
                    "label":"Return Value Deduction",
                    "fieldtype":"Currency",
                    "read_only":1,
                    "insert_after":"custom_nkt_missing_kg",
                },
                {
                    "fieldname":"custom_nkt_business_absorbed_value",
                    "label":"Business Absorbed Value",
                    "fieldtype":"Currency",
                    "read_only":1,
                    "insert_after":"custom_nkt_value_deduction",
                },
            ]
        },
        ignore_validate=True,
        update=True,
    )
    frappe.clear_cache(doctype="NKT Return Exchange Declaration")
    frappe.clear_cache(doctype="NKT Return Exchange Returned Item")

    _ensure_server_script_permissions()
    frappe.db.commit()
    frappe.clear_cache()
    return {
        "version":VERSION,
        "installed":True,
        "doctype":DOCTYPE,
        "posting_enabled":True,
        "business_records_created":False,
    }


@frappe.whitelist()
def verify():
    errors=[]
    for dt in (DOCTYPE,"NKT Return Exchange Returned Item","NKT Return Exchange New Item","NKT Return Exchange Settlement Payment"):
        if not frappe.db.exists("DocType",dt):
            errors.append("Missing DocType: "+dt)

    if frappe.db.exists("DocType", DOCTYPE):
        request_df = frappe.get_meta(DOCTYPE).get_field("custom_nkt_submit_request_id")
        if not request_df:
            errors.append("Return/Exchange declaration missing Submit Request ID idempotency field.")
        elif not cint(request_df.unique):
            errors.append("Submit Request ID field must remain unique.")

    submit_source = inspect.getsource(submit_from_payload)
    for control in (
        "_normalize_submit_request_id",
        "_lock_submission_source",
        "_existing_request_submission",
    ):
        if control not in submit_source:
            errors.append("C7.12 submit hard-control missing: "+control)
    if "idempotent_replay" not in inspect.getsource(_submission_result):
        errors.append("C7.12 replay result marker is missing.")
    if "FOR UPDATE" not in inspect.getsource(_lock_submission_source):
        errors.append("C7.12 source-lineage row locking is missing.")

    if frappe.db.exists("DocType", "NKT Return Exchange Settlement Payment"):
        pm = frappe.get_meta("NKT Return Exchange Settlement Payment").get_field("payment_method")
        options = (pm.options or "").splitlines() if pm else []
        for needed in ("Cash","Check","GCash","Maya","Card","Bank Transfer","Online","Account"):
            if needed not in options:
                errors.append("Settlement Payment missing method: "+needed)
        if "Credit Card" in options:
            errors.append("Legacy Credit Card option must be replaced by Card.")

    if frappe.db.exists("DocType", "NKT Return Exchange Returned Item"):
        rmeta = frappe.get_meta("NKT Return Exchange Returned Item")
        for needed in (
            "custom_nkt_return_value_treatment",
            "custom_nkt_value_kg_received",
            "custom_nkt_manual_deduction",
            "custom_nkt_expected_kg",
            "custom_nkt_missing_kg",
            "custom_nkt_value_deduction",
            "custom_nkt_business_absorbed_value",
        ):
            if not rmeta.has_field(needed):
                errors.append("Returned Item missing Fraction value field: "+needed)

    prep_source = inspect.getsource(_prepare_return_rows)
    if 'row.classification = "Saleable"' in prep_source:
        errors.append("Encoder return classification still silently defaults to Saleable.")
    if "partial-weight return cannot be posted" not in prep_source:
        errors.append("Partial-weight whole-sack classification guard is missing.")

    if frappe.db.exists("DocType", "NKT Return Exchange New Item"):
        rate_source = frappe.get_meta("NKT Return Exchange New Item").get_field("rate_source")
        if not rate_source or "Manual Rate" not in (rate_source.options or "").splitlines():
            errors.append("NKT Return Exchange New Item is missing Manual Rate source.")

    c71=frappe.get_attr("nkt_operations.nkt_store_operations.features.returns.service.verify")()
    c61=frappe.get_attr("nkt_operations.nkt_store_operations.features.cashier.shift_report.verify")()
    c62=frappe.get_attr("nkt_operations.nkt_store_operations.features.cashier.encoder_zout.verify")()
    c63=frappe.get_attr("nkt_operations.nkt_store_operations.features.cashier.reconciliation.verify")()
    if not c71.get("passed"): errors.append("C7.1 regression.")
    if not c61.get("passed"): errors.append("C6.1 regression.")
    if not c62.get("passed"): errors.append("C6.2 regression.")
    if not c63.get("passed"): errors.append("C6.3 regression.")

    mapping_count=frappe.db.sql(
        """SELECT COUNT(*) FROM `tabItem`
           WHERE COALESCE(nkt_damaged_item,'')!=''
              OR COALESCE(nkt_fraction_item,'')!=''"""
    )[0][0]
    if not mapping_count:
        errors.append("No designated Damaged/Fraction mappings found.")

    sample={}
    for side, source in (
        ("Cashier", frappe.db.get_value("NKT Cashier Sale", {"docstatus":1}, "name", order_by="creation desc")),
        ("Encoder", frappe.db.get_value("NKT Customer Order", {"docstatus":1}, "name", order_by="creation desc")),
    ):
        if source:
            try:
                old_cash, old_order, detail=_source_pair(side,source)
                sample[side.lower()]={
                    "source":source,
                    "old_cashier_sale":old_cash,
                    "old_customer_order":old_order,
                    "customer":detail.get("customer"),
                    "money_basis":_money(detail.get("money_basis")),
                    "account_basis":_money(detail.get("account_basis")),
                    "generation":cint(detail.get("generation")),
                    "items":len(detail.get("items") or []),
                }
            except Exception as exc:
                errors.append(f"{side} source-pair preview failed: {exc}")

    if not callable(globals().get("preview_payload")):
        errors.append("C7.4 preview_payload endpoint missing.")
    if not callable(globals().get("submit_from_payload")):
        errors.append("C7.4 submit_from_payload endpoint missing.")

    return {
        "version":VERSION,
        "posting_enabled":True,
        "legacy_return_backend_not_used_for_new_c7_posting":True,
        "software_owner_approval_required_by_c7_3":False,
        "software_physical_receipt_confirmation_required_by_c7_3":False,
        "damaged_fraction_mapping_count":int(mapping_count),
        "sample":sample,
        "c7_1_regression_passed":bool(c71.get("passed")),
        "c6_1_regression_passed":bool(c61.get("passed")),
        "c6_2_regression_passed":bool(c62.get("passed")),
        "c6_3_regression_passed":bool(c63.get("passed")),
        "errors":errors,
        "passed":not errors,
    }
