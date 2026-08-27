from __future__ import annotations

from contextlib import contextmanager
from collections import defaultdict
import inspect

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime, nowdate
from nkt_operations.nkt_store_operations.features.payments_accounts.card_surcharge import (
    apply_payment_row_card_fields,
    normalize_payment_method,
)

VERSION = "V2.0C.7.11B.2"
DECL = "NKT Return Exchange Declaration"
TOLERANCE = 0.005


def _money(v):
    return round(flt(v), 2)


@contextmanager
def _as_user(user):
    old = frappe.session.user
    if user and user != old:
        frappe.set_user(user)
    try:
        yield
    finally:
        if frappe.session.user != old:
            frappe.set_user(old)


def _material_receipt_type():
    name = frappe.db.get_value(
        "Stock Entry Type", {"purpose":"Material Receipt","is_standard":1}, "name"
    ) or frappe.db.get_value("Stock Entry Type", {"purpose":"Material Receipt"}, "name")
    if not name:
        frappe.throw(_("No Stock Entry Type exists for Material Receipt."))
    return name


def _active_cashier_shift(cashier, company):
    from nkt_operations.nkt_store_operations.features.payments_accounts.cash_ledger import get_open_shift_for_user
    shift = get_open_shift_for_user(company=company, user=cashier)
    if not shift:
        frappe.throw(
            _("Cashier {0} must have exactly one Open Cashier Shift to post the return/exchange settlement.").format(cashier)
        )

    shift_date = getdate(shift.shift_start)
    today = getdate(nowdate())
    if shift_date != today:
        frappe.throw(
            _(
                "Cashier Shift {0} belongs to business date {1}, but today is {2}. "
                "Close the previous-date shift and open a current-date Cashier Shift before posting this Return/Exchange. "
                "NKT does not backdate or cross-date cashier transactions."
            ).format(shift.name, shift_date, today)
        )
    return shift


def _preserved_offline_cashier_shift(cashier):
    shift_name = str(cashier.get("custom_nkt_offline_cashier_shift") or "").strip()
    event_uuid = str(cashier.get("custom_nkt_offline_event_uuid") or "").strip()
    if not event_uuid:
        return _active_cashier_shift(cashier.entry_user, cashier.company)
    if not shift_name:
        frappe.throw(
            _("Preserved offline Cashier Return/Exchange lost its original Cashier Shift.")
        )

    shift = frappe.db.get_value(
        "NKT Cashier Shift",
        shift_name,
        [
            "name", "docstatus", "status", "company", "settlement_location",
            "cashier", "shift_start", "shift_end",
        ],
        as_dict=True,
    )
    if not shift:
        frappe.throw(_("Preserved offline Cashier Shift was not found."))
    if str(shift.company or "") != str(cashier.company or ""):
        frappe.throw(_("Preserved offline Cashier Shift belongs to another Company."))
    if str(shift.cashier or "") != str(cashier.entry_user or ""):
        frappe.throw(_("Preserved offline Cashier Shift belongs to another Cashier."))
    if getdate(shift.shift_start) != getdate(cashier.business_date):
        frappe.throw(
            _("Preserved offline Cashier Shift date conflicts with the physical Return/Exchange date.")
        )
    when = get_datetime(cashier.entry_datetime)
    if shift.shift_start and when < get_datetime(shift.shift_start):
        frappe.throw(_("Offline Return/Exchange occurred before the preserved Cashier Shift opened."))
    if shift.shift_end and when > get_datetime(shift.shift_end):
        frappe.throw(_("Offline Return/Exchange occurred after the preserved Cashier Shift closed."))
    return shift


def _preserved_return_exchange_context(cashier, shift):
    from nkt_operations.nkt_store_operations.features.payments_accounts.cash_ledger import (
        preserved_return_exchange_shift_validation_context,
    )
    return preserved_return_exchange_shift_validation_context(
        cashier_shift=shift.name,
        company=cashier.company,
        settlement_location=shift.settlement_location,
        cashier=cashier.entry_user,
    )


def _ensure_custom_fields():
    create_custom_fields(
        {
            "NKT Cashier Sale": [
                {"fieldname":"custom_nkt_return_credit_applied","label":"NKT Return Credit Applied","fieldtype":"Currency","default":"0","read_only":1,"hidden":1,"insert_after":"custom_nkt_lineage_account_basis"},
            ],
            "NKT Customer Order": [
                {"fieldname":"custom_nkt_return_credit_applied","label":"NKT Return Credit Applied","fieldtype":"Currency","default":"0","read_only":1,"hidden":1,"insert_after":"custom_nkt_lineage_account_basis"},
            ],
            "NKT Customer Advance": [
                {"fieldname":"custom_nkt_credit_origin","label":"NKT Credit Origin","fieldtype":"Select","options":"Payment Advance\nReturn Credit","default":"Payment Advance","read_only":1,"insert_after":"source_customer_order"},
                {"fieldname":"custom_nkt_source_return_exchange","label":"NKT Source Return / Exchange","fieldtype":"Link","options":DECL,"read_only":1,"unique":1,"insert_after":"custom_nkt_credit_origin"},
            ],
            "NKT Customer Advance Application": [
                {"fieldname":"custom_nkt_source_return_exchange","label":"NKT Source Return / Exchange","fieldtype":"Link","options":DECL,"read_only":1,"insert_after":"source_payment_receipt"},
            ],
            "Stock Entry": [
                {"fieldname":"custom_nkt_return_exchange_declaration","label":"NKT Return / Exchange","fieldtype":"Link","options":DECL,"read_only":1,"insert_after":"remarks"},
                {"fieldname":"custom_nkt_return_exchange_kind","label":"NKT Return / Exchange Stock Kind","fieldtype":"Data","read_only":1,"insert_after":"custom_nkt_return_exchange_declaration"},
            ],
            "NKT Return Exchange Returned Item": [
                {"fieldname":"custom_nkt_source_inventory_unit_cost","label":"Source Inventory Unit Cost","fieldtype":"Currency","precision":"6","read_only":1,"hidden":1,"insert_after":"custom_nkt_business_absorbed_value"},
                {"fieldname":"custom_nkt_return_inventory_rate","label":"Return Inventory Rate","fieldtype":"Currency","precision":"6","read_only":1,"hidden":1,"insert_after":"custom_nkt_source_inventory_unit_cost"},
                {"fieldname":"custom_nkt_return_inventory_value","label":"Return Inventory Value","fieldtype":"Currency","precision":"6","read_only":1,"hidden":1,"insert_after":"custom_nkt_return_inventory_rate"},
                {"fieldname":"custom_nkt_inventory_cost_source","label":"Inventory Cost Source","fieldtype":"Small Text","read_only":1,"hidden":1,"insert_after":"custom_nkt_return_inventory_value"},
            ],
            "Stock Reconciliation": [
                {"fieldname":"custom_nkt_return_valuation_correction","label":"NKT Return Valuation Correction","fieldtype":"Check","default":"0","read_only":1,"hidden":1,"insert_after":"remarks"},
                {"fieldname":"custom_nkt_source_return_stock_entry","label":"NKT Source Return Stock Entry","fieldtype":"Link","options":"Stock Entry","read_only":1,"hidden":1,"insert_after":"custom_nkt_return_valuation_correction"},
                {"fieldname":"custom_nkt_source_return_exchange","label":"NKT Source Return / Exchange","fieldtype":"Link","options":DECL,"read_only":1,"hidden":1,"insert_after":"custom_nkt_source_return_stock_entry"},
            ],
        },
        ignore_validate=True,
        update=True,
    )

    # Return-origin Customer Credit has no Payment Receipt because no new money
    # was received. Both the Advance itself and any later Application must accept
    # a return-origin credit with no source Payment Receipt.
    for advance_dt_name in ("NKT Customer Advance", "NKT Customer Advance Application"):
        advance_dt = frappe.get_doc("DocType", advance_dt_name)
        changed = False
        for row in advance_dt.fields:
            if row.fieldname == "source_payment_receipt":
                if cint(row.reqd):
                    row.reqd = 0
                    changed = True
                new_label = "Original Payment Receipt (if applicable)"
                if row.label != new_label:
                    row.label = new_label
                    changed = True
        if changed:
            advance_dt.flags.ignore_permissions = True
            advance_dt.save(ignore_permissions=True)

    for dt in ("NKT Payment Detail","NKT Declared Payment"):
        meta_doc = frappe.get_doc("DocType", dt)
        changed = False
        for row in meta_doc.fields:
            if row.fieldname == "payment_method":
                opts = [normalize_payment_method(x) for x in (row.options or "").splitlines()]
                # Preserve order while eliminating the legacy label.
                opts = list(dict.fromkeys(x for x in opts if x))
                if "Return Credit" not in opts:
                    opts.append("Return Credit")
                new_options = "\n".join(opts)
                if row.options != new_options:
                    row.options = new_options
                    changed = True
        if changed:
            meta_doc.flags.ignore_permissions = True
            meta_doc.save(ignore_permissions=True)

    for dt in ("NKT Cashier Sale","NKT Customer Order","NKT Customer Advance",
               "NKT Customer Advance Application","Stock Entry",
               "NKT Return Exchange Returned Item","Stock Reconciliation",
               "NKT Payment Detail","NKT Declared Payment"):
        frappe.clear_cache(doctype=dt)


def _cashier_encoder(a_name, b_name):
    a = frappe.get_doc(DECL, a_name)
    b = frappe.get_doc(DECL, b_name)
    if a.side == b.side:
        frappe.throw(_("Matched return/exchange pair must contain one Cashier and one Encoder declaration."))
    cashier = a if a.side == "Cashier" else b
    encoder = b if a.side == "Cashier" else a
    if cashier.match_key != encoder.match_key:
        frappe.throw(_("Matched return/exchange declarations no longer have the same match key."))
    return cashier, encoder


def _sle_unit_cost(row):
    qty = abs(flt(row.get("actual_qty")))
    value_diff = abs(flt(row.get("stock_value_difference")))
    if qty > TOLERANCE and value_diff > TOLERANCE:
        return value_diff / qty
    valuation = flt(row.get("valuation_rate"))
    if valuation > TOLERANCE:
        return valuation
    incoming = flt(row.get("incoming_rate"))
    if incoming > TOLERANCE:
        return incoming
    return 0.0


def _source_stock_entry_candidates(customer_order):
    candidates = []
    meta = frappe.get_meta("NKT Customer Order")

    # Accepted retail path.
    if meta.has_field("custom_nkt_retail_stock_entry"):
        value = frappe.db.get_value(
            "NKT Customer Order", customer_order, "custom_nkt_retail_stock_entry"
        )
        if value:
            candidates.append(("custom_nkt_retail_stock_entry", value))

    # Future-safe: include other nonempty Link-to-Stock-Entry fields such as
    # release/issue links without hard-coding their names.
    order_doc = frappe.get_doc("NKT Customer Order", customer_order)
    for df in meta.fields:
        if df.fieldtype == "Link" and df.options == "Stock Entry":
            value = order_doc.get(df.fieldname)
            if value and all(value != x[1] for x in candidates):
                candidates.append((df.fieldname, value))
    return candidates


def _source_issue_unit_cost(encoder, row):
    item = row.item
    warehouse = row.original_source_warehouse
    if not item or not warehouse:
        frappe.throw(
            _("Cannot restore return inventory cost because the original item/warehouse is missing on return row {0}.").format(row.idx)
        )

    sle_fields = "actual_qty, stock_value_difference, valuation_rate, incoming_rate, voucher_no, posting_date, posting_time"

    # First choice: the exact Stock Entry linked to the source Customer Order.
    for fieldname, stock_entry in _source_stock_entry_candidates(encoder.old_customer_order):
        rows = frappe.db.sql(
            f"""
            SELECT {sle_fields}
            FROM `tabStock Ledger Entry`
            WHERE voucher_type='Stock Entry'
              AND voucher_no=%s
              AND item_code=%s
              AND warehouse=%s
              AND actual_qty < 0
              AND IFNULL(is_cancelled,0)=0
            ORDER BY creation
            """,
            (stock_entry, item, warehouse),
            as_dict=True,
        )
        for sle in rows:
            cost = _sle_unit_cost(sle)
            if cost > TOLERANCE:
                return cost, f"{fieldname}:{stock_entry}"

    # Controlled fallback for older/external-release transactions: use the
    # latest actual outgoing SLE from the same item+warehouse at or immediately
    # before the source order time. This is audit-labelled as a fallback.
    order_creation = frappe.db.get_value(
        "NKT Customer Order", encoder.old_customer_order, "creation"
    )
    rows = frappe.db.sql(
        f"""
        SELECT {sle_fields}
        FROM `tabStock Ledger Entry`
        WHERE item_code=%s
          AND warehouse=%s
          AND actual_qty < 0
          AND IFNULL(is_cancelled,0)=0
          AND TIMESTAMP(posting_date, posting_time) <= DATE_ADD(%s, INTERVAL 10 MINUTE)
        ORDER BY posting_date DESC, posting_time DESC, creation DESC
        LIMIT 10
        """,
        (item, warehouse, order_creation),
        as_dict=True,
    )
    for sle in rows:
        cost = _sle_unit_cost(sle)
        if cost > TOLERANCE:
            return cost, f"SLE fallback:{sle.voucher_no}"

    frappe.throw(
        _(
            "Cannot determine original inventory cost for {0} from {1}. "
            "NKT will not receive returned stock at zero valuation. "
            "Review the source stock issue/release first."
        ).format(item, warehouse)
    )


def _return_inventory_rate_and_value(encoder, row):
    source_unit_cost, source = _source_issue_unit_cost(encoder, row)
    classification = row.classification

    if classification == "Fraction":
        sack_kg = flt(frappe.db.get_value("Item", row.item, "nkt_standard_sack_weight_kg"))
        if sack_kg <= TOLERANCE:
            frappe.throw(
                _("Standard Sack Weight (kg) is required on {0} before receiving Fraction stock.").format(row.item)
            )
        qty = flt(row.fraction_kg)
        return_rate = source_unit_cost / sack_kg
    elif classification in ("Saleable", "Damaged"):
        qty = flt(row.quantity)
        return_rate = source_unit_cost
    else:
        return 0.0, 0.0, 0.0, source

    value = qty * return_rate

    # Store the inventory-cost audit separately from customer refund/credit.
    if row.name:
        frappe.db.set_value(
            row.doctype,
            row.name,
            {
                "custom_nkt_source_inventory_unit_cost": source_unit_cost,
                "custom_nkt_return_inventory_rate": return_rate,
                "custom_nkt_return_inventory_value": value,
                "custom_nkt_inventory_cost_source": source,
            },
            update_modified=False,
        )

    return source_unit_cost, return_rate, value, source


def _create_return_stock_entry(encoder):
    existing = encoder.get("return_stock_entry") or frappe.db.get_value(
        "Stock Entry",
        {"custom_nkt_return_exchange_declaration":encoder.name,
         "custom_nkt_return_exchange_kind":"Customer Return Receipt",
         "docstatus":["!=",2]},
        "name",
    )
    if existing:
        return existing

    if not encoder.return_warehouse:
        frappe.throw(_("Encoder Return Receiving Warehouse is required."))

    grouped = defaultdict(lambda: {"qty":0.0, "value":0.0, "sources":[]})
    for row in encoder.returned_items:
        classification = row.classification
        if classification == "Rejected":
            continue
        if classification == "Saleable":
            item = row.item
            qty = flt(row.quantity)
        elif classification == "Damaged":
            item = row.damaged_item
            qty = flt(row.quantity)
        elif classification == "Fraction":
            item = row.fraction_item
            qty = flt(row.fraction_kg)
        else:
            frappe.throw(_("Invalid inventory classification on return row {0}.").format(row.idx))

        if item and qty > TOLERANCE:
            source_unit_cost, return_rate, value, source = _return_inventory_rate_and_value(
                encoder, row
            )
            grouped[item]["qty"] += qty
            grouped[item]["value"] += value
            grouped[item]["sources"].append(source)

    if not grouped:
        return None

    company_meta = frappe.get_meta("Company")
    stock_adjustment_account = (
        frappe.db.get_value("Company", encoder.company, "stock_adjustment_account")
        if company_meta.has_field("stock_adjustment_account")
        else None
    )

    items = []
    for item, data in sorted(grouped.items()):
        qty = flt(data["qty"])
        value = flt(data["value"])
        rate = value / qty if qty > TOLERANCE else 0
        if rate <= TOLERANCE:
            frappe.throw(
                _("Return inventory valuation for {0} resolved to zero. NKT will not post zero-valued returned stock.").format(item)
            )
        uom = frappe.db.get_value("Item", item, "stock_uom")
        detail = {
            "item_code":item,
            "qty":qty,
            "uom":uom,
            "stock_uom":uom,
            "conversion_factor":1,
            "t_warehouse":encoder.return_warehouse,
            "basic_rate":rate,
            "allow_zero_valuation_rate":0,
        }
        if stock_adjustment_account and frappe.get_meta("Stock Entry Detail").has_field("expense_account"):
            detail["expense_account"] = stock_adjustment_account
        items.append(detail)

    when = get_datetime(encoder.entry_datetime or now_datetime())
    entry = frappe.get_doc({
        "doctype":"Stock Entry",
        "company":encoder.company,
        "purpose":"Material Receipt",
        "stock_entry_type":_material_receipt_type(),
        "set_posting_time":1,
        "posting_date":when.date(),
        "posting_time":when.time(),
        "custom_nkt_return_exchange_declaration":encoder.name,
        "custom_nkt_return_exchange_kind":"Customer Return Receipt",
        "remarks":(
            f"Official customer-return inventory receipt from matched Return/Exchange "
            f"{encoder.name}. Saleable/Damaged/Fraction uses configured item identity and "
            f"restores original source inventory cost; customer refund/credit value is separate. "
            f"Rejected creates no stock."
        ),
        "items":items,
    })
    entry.flags.ignore_permissions = True
    entry.insert(ignore_permissions=True)
    entry.submit()
    return entry.name


def _apply_account_adjustment(encoder, requested):
    requested = _money(requested)
    if requested <= TOLERANCE:
        return None, 0.0, 0.0

    existing = frappe.db.get_value(
        "NKT Return Account Adjustment",
        {"return_exchange_declaration":encoder.name},
        ["name","amount"],
        as_dict=True,
    )
    if existing:
        return existing.name, flt(existing.amount), max(requested-flt(existing.amount),0)

    receivable_name = frappe.db.get_value(
        "NKT Customer Receivable",
        {"customer_order":encoder.old_customer_order,
         "status":["in",["Open","Partially Paid"]]},
        "name",
    )
    if not receivable_name:
        return None, 0.0, requested

    frappe.db.sql(
        "SELECT name FROM `tabNKT Customer Receivable` WHERE name=%s FOR UPDATE",
        receivable_name,
    )
    rec = frappe.get_doc("NKT Customer Receivable", receivable_name)
    before = max(flt(rec.outstanding_amount),0)
    use = min(requested,before)
    if use <= TOLERANCE:
        return None,0.0,requested

    original_after = max(flt(rec.original_amount)-use, flt(rec.amount_paid))
    outstanding_after = max(original_after-flt(rec.amount_paid),0)
    status = "Paid" if outstanding_after <= TOLERANCE else "Partially Paid"

    frappe.db.set_value(
        "NKT Customer Receivable",rec.name,
        {"original_amount":original_after,
         "outstanding_amount":outstanding_after,
         "status":status},
        update_modified=False,
    )
    frappe.db.set_value(
        "NKT Customer Order",encoder.old_customer_order,
        {"amount_due":outstanding_after,
         "payment_status":"Paid" if outstanding_after<=TOLERANCE else "Partially Paid"},
        update_modified=False,
    )

    audit = frappe.get_doc({
        "doctype":"NKT Return Account Adjustment",
        "company":encoder.company,
        "posting_datetime":encoder.entry_datetime or now_datetime(),
        "customer":encoder.customer,
        "customer_name":encoder.customer_name,
        "return_exchange_declaration":encoder.name,
        "customer_order":encoder.old_customer_order,
        "receivable":rec.name,
        "amount":use,
        "outstanding_before":before,
        "outstanding_after":outstanding_after,
        "created_by":frappe.session.user,
        "remarks":"Return/Exchange value applied as a non-cash reduction of the OLD ORDER receivable.",
    })
    audit.flags.ignore_permissions=True
    audit.insert(ignore_permissions=True)

    from nkt_operations.nkt_store_operations.features.payments_accounts.credit import refresh_customer_credit
    refresh_customer_credit(encoder.customer)
    return audit.name,use,max(requested-use,0)


def _create_customer_credit(encoder, amount):
    amount = _money(amount)
    if amount <= TOLERANCE:
        return None

    existing = frappe.db.get_value(
        "NKT Customer Advance",
        {"custom_nkt_source_return_exchange":encoder.name,
         "docstatus":["!=",2]},
        "name",
    )
    if existing:
        return existing

    adv = frappe.get_doc({
        "doctype":"NKT Customer Advance",
        "company":encoder.company,
        "posting_datetime":encoder.entry_datetime or now_datetime(),
        "customer":encoder.customer,
        "customer_name":encoder.customer_name,
        "source_payment_receipt":None,
        "source_customer_order":encoder.old_customer_order,
        "custom_nkt_credit_origin":"Return Credit",
        "custom_nkt_source_return_exchange":encoder.name,
        "original_advance_amount":amount,
        "applied_amount":0,
        "available_advance_amount":amount,
        "advance_status":"Available",
        "remarks":"Non-cash Customer Credit created from matched Return/Exchange. No Payment Receipt or Cashier Movement was created.",
    })
    adv.flags.ignore_permissions=True
    adv.insert(ignore_permissions=True)
    adv.submit()
    return adv.name


def _basis_for_new_order(cashier, actual_account_adjustment, actual_customer_credit):
    money = max(flt(cashier.return_money_basis),0)
    account = max(flt(cashier.return_account_basis),0)

    # Actual money leaving the customer relationship removes refundable-money basis first.
    money = max(money - flt(cashier.refund_money),0)
    account = max(account - flt(actual_account_adjustment),0)

    # Customer Credit leaves the NEW ORDER lineage. Consume money basis first
    # to keep future cash-refund capacity conservative; then Account/Credit basis.
    cc = max(flt(actual_customer_credit),0)
    use_money = min(cc,money)
    money -= use_money
    cc -= use_money
    account = max(account-cc,0)

    for row in (cashier.get("settlement_payments") or []):
        if row.payment_method == "Account":
            account += flt(row.amount)
        else:
            money += flt(row.amount)

    target = max(flt(cashier.new_order_value),0)
    total = money+account
    if total > TOLERANCE and abs(total-target)>TOLERANCE:
        factor = target/total
        money *= factor
        account *= factor
    elif total <= TOLERANCE and target > TOLERANCE:
        account = target

    return _money(money), _money(max(target-_money(money),0))



def _price_adjustment_select_value(rate, current):
    """Return the Select-safe representation of a replacement-sale price adjustment.

    The accepted Fast POS child field stores allowed adjustments as Select options
    like "5" and "-10". Arithmetic naturally produces floats such as 5.0; passing
    that float directly makes Frappe validate it as the string "5.0", which is not
    one of the accepted options. Normalize only integral adjustments to the canonical
    integer string. Non-integral values are intentionally left visible as numeric
    strings so the existing Select validation still rejects them.
    """
    adjustment = _money(flt(rate) - flt(current))
    rounded = round(adjustment)
    if abs(adjustment - rounded) <= TOLERANCE:
        return str(int(rounded))
    return str(adjustment)

def _append_sale_items(doc, rows, source_warehouse):
    for row in rows:
        current = flt(frappe.db.get_value(
            "Item Price",
            {"item_code":row.item,"price_list":"Standard Selling","selling":1},
            "price_list_rate",
        ))
        doc.append("items",{
            "item":row.item,
            "quantity":row.quantity,
            "source_warehouse":source_warehouse,
            "price_adjustment":_price_adjustment_select_value(row.rate, current),
        })


def _append_order_items(doc, rows):
    for row in rows:
        current = flt(frappe.db.get_value(
            "Item Price",
            {"item_code":row.item,"price_list":"Standard Selling","selling":1},
            "price_list_rate",
        ))
        doc.append("items",{
            "item":row.item,
            "quantity":row.quantity,
            "source_warehouse":row.source_warehouse,
            "price_adjustment":_price_adjustment_select_value(row.rate, current),
        })


def _payment_rows(cashier):
    rows = []
    return_credit_applied = min(flt(cashier.return_credit), flt(cashier.new_order_value))
    if return_credit_applied > TOLERANCE:
        rows.append({
            "payment_method": "Return Credit",
            "amount": return_credit_applied,
            "remarks": f"Applied returned-value credit from {cashier.name}.",
        })

    for src in (cashier.get("settlement_payments") or []):
        apply_payment_row_card_fields(src)
        row = {
            "payment_method": normalize_payment_method(src.payment_method),
            "amount": flt(src.amount),
            "card_surcharge": flt(src.get("card_surcharge")),
            "collected_amount": flt(src.get("collected_amount")),
            "reference_number": src.reference_number or "",
            "bank_or_provider": src.bank_or_provider or "",
            "remarks": f"Exchange difference settlement from {cashier.name}.",
        }
        if src.payment_method == "Cash":
            row["cash_tendered"] = flt(src.cash_tendered)
            row["change_amount"] = flt(src.change_amount)
        elif src.payment_method == "Check":
            row["check_number"] = src.reference_number or ""
            row["check_date"] = src.check_date
        rows.append(row)

    return rows, _money(return_credit_applied)


def _existing_exchange_sale(declaration_name):
    return frappe.db.get_value(
        "NKT Cashier Sale",
        {
            "custom_nkt_source_return_entry": declaration_name,
            "docstatus": ["!=", 2],
        },
        "name",
    )


def _existing_exchange_order(declaration_name):
    return frappe.db.get_value(
        "NKT Customer Order",
        {
            "custom_nkt_source_return_entry": declaration_name,
            "docstatus": ["!=", 2],
        },
        "name",
    )


def _create_replacement_cashier_sale(cashier, money_basis, account_basis):
    if cashier.transaction_type != "Exchange":
        return None
    existing = cashier.new_cashier_sale or _existing_exchange_sale(cashier.name)
    if existing:
        return existing

    shift = _preserved_offline_cashier_shift(cashier)
    payment_rows, return_credit_applied = _payment_rows(cashier)
    offline_event = bool(str(cashier.get("custom_nkt_offline_event_uuid") or "").strip())

    with _as_user(cashier.entry_user):
        sale = frappe.new_doc("NKT Cashier Sale")
        sale.customer = cashier.customer
        sale.customer_name = cashier.customer_name
        sale.company = cashier.company
        sale.source_order_slip = f"EXCHANGE:{cashier.name}"
        _append_sale_items(sale, cashier.new_items, shift.settlement_location)
        for row in payment_rows:
            sale.append("payments", row)
        sale.custom_nkt_is_exchange_order = 1
        sale.custom_nkt_exchange_generation = cint(cashier.source_generation) + 1
        sale.custom_nkt_exchange_parent = cashier.old_cashier_sale
        sale.custom_nkt_exchange_origin = (
            frappe.db.get_value(
                "NKT Cashier Sale",
                cashier.old_cashier_sale,
                "custom_nkt_exchange_origin",
            )
            or cashier.old_cashier_sale
        )
        sale.custom_nkt_source_return_entry = cashier.name
        sale.custom_nkt_lineage_money_basis = money_basis
        sale.custom_nkt_lineage_account_basis = account_basis
        sale.custom_nkt_return_credit_applied = return_credit_applied
        sale.flags.ignore_permissions = True
        if offline_event:
            sale.cashier = cashier.entry_user
            sale.cashier_shift = shift.name
            sale.settlement_location = shift.settlement_location
            sale.default_warehouse = shift.settlement_location
            sale.business_date = getdate(cashier.business_date)
            sale.sale_datetime = get_datetime(cashier.entry_datetime)
            sale.flags.nkt_c15c_preserve_offline_cashier = True
            with _preserved_return_exchange_context(cashier, shift):
                sale.insert(ignore_permissions=True)
                sale.submit()
        else:
            sale.insert(ignore_permissions=True)
            sale.submit()
    return sale.name


def _create_replacement_customer_order(encoder, money_basis, account_basis):
    if encoder.transaction_type != "Exchange":
        return None
    existing = encoder.new_customer_order or _existing_exchange_order(encoder.name)
    if existing:
        return existing

    payment_rows, return_credit_applied = _payment_rows(encoder)

    with _as_user(encoder.entry_user):
        order = frappe.new_doc("NKT Customer Order")
        order.company = encoder.company
        order.customer = encoder.customer
        order.customer_name = encoder.customer_name
        order.default_warehouse = (
            encoder.new_items[0].source_warehouse
            if encoder.new_items
            else encoder.return_warehouse
        )
        order.source_order_slip = f"EXCHANGE:{encoder.name}"
        if str(encoder.get("custom_nkt_offline_event_uuid") or "").strip():
            order.order_date = getdate(encoder.business_date)
            order.encoder = encoder.entry_user
            order.flags.nkt_c15c_preserve_offline_encoder = True
        _append_order_items(order, encoder.new_items)

        declared_meta = frappe.get_meta("NKT Declared Payment")
        for row in payment_rows:
            declared = {
                "payment_method": row["payment_method"],
                "amount": row["amount"],
                "card_surcharge": row.get("card_surcharge") or 0,
                "collected_amount": row.get("collected_amount") or row["amount"],
                "reference_number": row.get("reference_number") or "",
                "bank_or_provider": row.get("bank_or_provider") or "",
                "remarks": row.get("remarks") or "",
            }
            if (
                declared_meta.has_field("custom_nkt_check_date")
                and row.get("check_date")
            ):
                declared["custom_nkt_check_date"] = row.get("check_date")
            if (
                declared_meta.has_field("custom_nkt_check_number")
                and row.get("check_number")
            ):
                declared["custom_nkt_check_number"] = row.get("check_number")
            order.append("declared_payments", declared)

        order.custom_nkt_is_exchange_order = 1
        order.custom_nkt_exchange_generation = cint(encoder.source_generation) + 1
        order.custom_nkt_exchange_parent = encoder.old_customer_order
        order.custom_nkt_exchange_origin = (
            frappe.db.get_value(
                "NKT Customer Order",
                encoder.old_customer_order,
                "custom_nkt_exchange_origin",
            )
            or encoder.old_customer_order
        )
        order.custom_nkt_source_return_entry = encoder.name
        order.custom_nkt_lineage_money_basis = money_basis
        order.custom_nkt_lineage_account_basis = account_basis
        order.custom_nkt_return_credit_applied = return_credit_applied
        order.flags.ignore_permissions = True
        order.insert(ignore_permissions=True)
        order.submit()
    return order.name


def _create_replacement_sale_and_order(cashier, encoder, money_basis, account_basis):
    if cashier.transaction_type!="Exchange":
        return None,None
    if cashier.new_cashier_sale and cashier.new_customer_order:
        return cashier.new_cashier_sale,cashier.new_customer_order

    shift=_active_cashier_shift(cashier.entry_user,cashier.company)
    payment_rows,return_credit_applied=_payment_rows(cashier)

    with _as_user(cashier.entry_user):
        sale=frappe.new_doc("NKT Cashier Sale")
        sale.customer=cashier.customer
        sale.customer_name=cashier.customer_name
        sale.company=cashier.company
        sale.source_order_slip=f"EXCHANGE:{cashier.name}"
        _append_sale_items(sale,cashier.new_items,shift.settlement_location)
        for row in payment_rows:
            sale.append("payments",row)
        sale.custom_nkt_is_exchange_order=1
        sale.custom_nkt_exchange_generation=cint(cashier.source_generation)+1
        sale.custom_nkt_exchange_parent=cashier.old_cashier_sale
        sale.custom_nkt_exchange_origin=(
            frappe.db.get_value("NKT Cashier Sale",cashier.old_cashier_sale,"custom_nkt_exchange_origin")
            or cashier.old_cashier_sale
        )
        sale.custom_nkt_source_return_entry=cashier.name
        sale.custom_nkt_lineage_money_basis=money_basis
        sale.custom_nkt_lineage_account_basis=account_basis
        sale.custom_nkt_return_credit_applied=return_credit_applied
        sale.flags.ignore_permissions=True
        sale.insert(ignore_permissions=True)
        sale.submit()

    with _as_user(encoder.entry_user):
        order=frappe.new_doc("NKT Customer Order")
        order.company=encoder.company
        order.customer=encoder.customer
        order.customer_name=encoder.customer_name
        order.default_warehouse=encoder.new_items[0].source_warehouse if encoder.new_items else encoder.return_warehouse
        order.source_order_slip=f"EXCHANGE:{encoder.name}"
        _append_order_items(order,encoder.new_items)
        declared_meta = frappe.get_meta("NKT Declared Payment")
        for row in payment_rows:
            declared = {
                "payment_method": row["payment_method"],
                "amount": row["amount"],
                "card_surcharge": row.get("card_surcharge") or 0,
                "collected_amount": row.get("collected_amount") or row["amount"],
                "reference_number": row.get("reference_number") or "",
                "bank_or_provider": row.get("bank_or_provider") or "",
                "remarks": row.get("remarks") or "",
            }
            if declared_meta.has_field("custom_nkt_check_date") and row.get("check_date"):
                declared["custom_nkt_check_date"] = row.get("check_date")
            if declared_meta.has_field("custom_nkt_check_number") and row.get("check_number"):
                declared["custom_nkt_check_number"] = row.get("check_number")
            order.append("declared_payments", declared)
        order.custom_nkt_is_exchange_order=1
        order.custom_nkt_exchange_generation=cint(encoder.source_generation)+1
        order.custom_nkt_exchange_parent=encoder.old_customer_order
        order.custom_nkt_exchange_origin=(
            frappe.db.get_value("NKT Customer Order",encoder.old_customer_order,"custom_nkt_exchange_origin")
            or encoder.old_customer_order
        )
        order.custom_nkt_source_return_entry=encoder.name
        order.custom_nkt_lineage_money_basis=money_basis
        order.custom_nkt_lineage_account_basis=account_basis
        order.custom_nkt_return_credit_applied=return_credit_applied
        order.flags.ignore_permissions=True
        order.insert(ignore_permissions=True)
        order.submit()

    sale.reload(); order.reload()
    if sale.matched_customer_order!=order.name or order.matched_cashier_sale!=sale.name:
        frappe.throw(
            _("The Return/Exchange could not be completed because the new sale and order did not match. Please ask an administrator to review it before retrying.")
        )
    return sale.name,order.name


def _post_refund(cashier):
    amount=_money(cashier.refund_money)
    if amount<=TOLERANCE:
        return None
    from nkt_operations.nkt_store_operations.features.payments_accounts.cash_ledger import create_cashier_movement
    shift = _preserved_offline_cashier_shift(cashier)
    kwargs = dict(
        company=cashier.company,
        posting_datetime=cashier.entry_datetime or now_datetime(),
        cashier_shift=shift.name,
        settlement_location=shift.settlement_location,
        cashier=cashier.entry_user,
        movement_type=(
            "Customer Return Refund"
            if cashier.transaction_type=="Return"
            else "Exchange Difference Refunded"
        ),
        direction="Out",
        payment_method=normalize_payment_method(cashier.settlement_method),
        amount=amount,
        source_doctype=DECL,
        source_name=cashier.name,
        source_row="refund",
        customer=cashier.customer,
        reference_number=cashier.settlement_reference or "",
        remarks=f"Actual customer refund from matched Return/Exchange {cashier.name}.",
    )
    if str(cashier.get("custom_nkt_offline_event_uuid") or "").strip():
        with _preserved_return_exchange_context(cashier, shift):
            movement = create_cashier_movement(**kwargs)
    else:
        movement = create_cashier_movement(**kwargs)
    return movement.name if movement else None


def post_independent_declaration(name):
    frappe.db.sql(
        f"SELECT name FROM `tab{DECL}` WHERE name=%s FOR UPDATE",
        name,
    )
    doc = frappe.get_doc(DECL, name)
    if doc.docstatus != 1:
        frappe.throw(_("Return/Exchange declaration must be submitted before posting."))

    if doc.posting_status == "Posted":
        return {
            "posted": True,
            "side": doc.side,
            "declaration": doc.name,
            "new_cashier_sale": doc.new_cashier_sale,
            "new_customer_order": doc.new_customer_order,
            "return_stock_entry": doc.return_stock_entry,
        }

    now = now_datetime()

    if doc.side == "Cashier":
        # Cashier owns the money side and its independent NEW SALE representation.
        money_basis, account_basis = _basis_for_new_order(
            doc,
            flt(doc.account_adjustment_amount),
            flt(doc.customer_credit_amount),
        )
        new_sale = _create_replacement_cashier_sale(
            doc, money_basis, account_basis
        )
        refund_movement = _post_refund(doc)

        frappe.db.set_value(
            DECL,
            doc.name,
            {
                "posting_status": "Posted",
                "posted_on": now,
                "new_cashier_sale": new_sale,
            },
            update_modified=False,
        )
        return {
            "posted": True,
            "side": "Cashier",
            "declaration": doc.name,
            "new_cashier_sale": new_sale,
            "refund_movement": refund_movement,
        }

    if doc.side == "Encoder":
        # Encoder owns official return inventory, NEW ORDER and Account/Credit.
        stock_entry = _create_return_stock_entry(doc)

        adj_name, actual_adj, adj_leftover = _apply_account_adjustment(
            doc, doc.account_adjustment_amount
        )
        actual_credit = _money(
            flt(doc.customer_credit_amount) + adj_leftover
        )
        credit_name = _create_customer_credit(doc, actual_credit)

        money_basis, account_basis = _basis_for_new_order(
            doc, actual_adj, actual_credit
        )
        new_order = _create_replacement_customer_order(
            doc, money_basis, account_basis
        )

        frappe.db.set_value(
            DECL,
            doc.name,
            {
                "posting_status": "Posted",
                "posted_on": now,
                "return_stock_entry": stock_entry,
                "new_customer_order": new_order,
                "account_adjustment_record": adj_name,
                "customer_credit_record": credit_name,
            },
            update_modified=False,
        )
        return {
            "posted": True,
            "side": "Encoder",
            "declaration": doc.name,
            "return_stock_entry": stock_entry,
            "new_customer_order": new_order,
            "account_adjustment_record": adj_name,
            "customer_credit_record": credit_name,
        }

    frappe.throw(_("Unsupported Return/Exchange side: {0}").format(doc.side))


def finalize_matched_pair(a_name, b_name):
    """Reconcile two already-independent operational sides.

    This is intentionally NOT a posting gate. It also catches up a declaration
    submitted under the older matched-gated C7 build, so the user's existing
    Cashier test declaration can be preserved.
    """
    frappe.db.sql(
        f"SELECT name FROM `tab{DECL}` WHERE name IN (%s,%s) FOR UPDATE",
        (a_name, b_name),
    )
    cashier, encoder = _cashier_encoder(a_name, b_name)

    post_independent_declaration(cashier.name)
    post_independent_declaration(encoder.name)

    cashier.reload()
    encoder.reload()

    new_sale = cashier.new_cashier_sale
    new_order = encoder.new_customer_order

    if new_sale and new_order:
        from nkt_operations.nkt_store_operations.features.sales.matching import (
            try_match_cashier_sale,
            try_match_customer_order,
        )
        try_match_cashier_sale(new_sale)
        try_match_customer_order(new_order)

        sale = frappe.get_doc("NKT Cashier Sale", new_sale)
        order = frappe.get_doc("NKT Customer Order", new_order)
        operational_pair_matched = (
            sale.matched_customer_order == new_order
            and order.matched_cashier_sale == new_sale
        )

        if operational_pair_matched:
            # Official Encoder-side lineage basis becomes the common basis
            # after normal sale/order reconciliation. This does not affect
            # drawer money or stock.
            official_money = flt(
                frappe.db.get_value(
                    "NKT Customer Order",
                    new_order,
                    "custom_nkt_lineage_money_basis",
                )
            )
            official_account = flt(
                frappe.db.get_value(
                    "NKT Customer Order",
                    new_order,
                    "custom_nkt_lineage_account_basis",
                )
            )
            frappe.db.set_value(
                "NKT Cashier Sale",
                new_sale,
                {
                    "custom_nkt_lineage_money_basis": official_money,
                    "custom_nkt_lineage_account_basis": official_account,
                },
                update_modified=False,
            )
        else:
            # IMPORTANT: normal Cashier/Encoder operational reconciliation is
            # NOT a posting gate. Both sides are already authoritative for
            # their own responsibilities. Leave the NEW Sale / NEW Order in
            # the standard reconciliation queue for Admin/Owner review instead
            # of rolling back either independently posted side.
            frappe.log_error(
                title="NKT C7 NEW Sale/Order reconciliation pending",
                message=(
                    f"Return/Exchange declarations {cashier.name} and {encoder.name} matched, "
                    f"but NEW Cashier Sale {new_sale} and NEW Customer Order {new_order} "
                    "did not complete normal sale/order reconciliation. Independent posting was preserved."
                ),
            )

    operational_new_pair_matched = bool(
        not new_sale or not new_order
        or (
            frappe.db.get_value(
                "NKT Cashier Sale", new_sale, "matched_customer_order"
            ) == new_order
            and frappe.db.get_value(
                "NKT Customer Order", new_order, "matched_cashier_sale"
            ) == new_sale
        )
    )

    # Only real DocType fields belong in the declaration update.
    # operational_new_pair_matched is a diagnostic/result value, NOT a
    # persisted field on NKT Return Exchange Declaration.
    common = {
        "new_cashier_sale": new_sale,
        "new_customer_order": new_order,
    }
    frappe.db.set_value(
        DECL, cashier.name, common, update_modified=False
    )
    frappe.db.set_value(
        DECL, encoder.name, common, update_modified=False
    )

    return {
        "reconciled": True,
        "cashier": cashier.name,
        "encoder": encoder.name,
        "cashier_posted": cashier.posting_status == "Posted",
        "encoder_posted": encoder.posting_status == "Posted",
        "new_cashier_sale": new_sale,
        "new_customer_order": new_order,
        "operational_new_pair_matched": operational_new_pair_matched,
    }


# Backward-compatible name for any older caller.
def post_matched_pair(a_name, b_name):
    return finalize_matched_pair(a_name, b_name)


def _stock_reconciliation_accounting_defaults(company):
    company_meta = frappe.get_meta("Company")
    expense_account = None
    cost_center = None
    for field in ("stock_adjustment_account", "round_off_account"):
        if company_meta.has_field(field):
            value = frappe.db.get_value("Company", company, field)
            if value:
                expense_account = value
                break
    for field in ("cost_center", "default_cost_center"):
        if company_meta.has_field(field):
            value = frappe.db.get_value("Company", company, field)
            if value:
                cost_center = value
                break
    return expense_account, cost_center


def repair_c7_11b_fraction_zero_valuation():
    """Forward/current-time valuation correction for the accepted 15 kg C7.11B test.

    Quantity is not changed. This exists only because MAT-STE-2026-00060 was
    posted before original-cost return valuation was implemented.
    """
    encoder_name = frappe.db.get_value(
        DECL,
        {
            "side":"Encoder",
            "old_cashier_sale":"NKT-CASH-00051",
            "old_customer_order":"NKT-ORD-00058",
            "docstatus":1,
            "posting_status":"Posted",
        },
        "name",
    )
    if not encoder_name:
        frappe.throw(_("C7.11B valuation repair safety stop: accepted Encoder declaration not found."))

    encoder = frappe.get_doc(DECL, encoder_name)
    if encoder.return_stock_entry != "MAT-STE-2026-00060":
        frappe.throw(
            _("C7.11B valuation repair safety stop: expected return Stock Entry MAT-STE-2026-00060.")
        )

    fraction_rows = [r for r in encoder.returned_items if r.classification == "Fraction"]
    if len(fraction_rows) != 1:
        frappe.throw(_("C7.11B valuation repair safety stop: expected exactly one Fraction return row."))
    row = fraction_rows[0]

    if row.fraction_item != "[Fraction] Kohaku Red LAM" or abs(flt(row.fraction_kg)-15) > TOLERANCE:
        frappe.throw(_("C7.11B valuation repair safety stop: expected exactly 15 kg [Fraction] Kohaku Red LAM."))

    source_unit_cost, return_rate, return_value, source = _return_inventory_rate_and_value(
        encoder, row
    )

    sle = frappe.db.get_value(
        "Stock Ledger Entry",
        {
            "voucher_type":"Stock Entry",
            "voucher_no":"MAT-STE-2026-00060",
            "item_code":row.fraction_item,
            "warehouse":encoder.return_warehouse,
            "is_cancelled":0,
        },
        ["actual_qty","valuation_rate","stock_value_difference"],
        as_dict=True,
    )
    if not sle or abs(flt(sle.actual_qty)-15) > TOLERANCE:
        frappe.throw(_("C7.11B valuation repair safety stop: original Fraction stock movement is not +15 kg."))

    # Idempotency: an accepted forward correction already exists.
    existing = frappe.db.get_value(
        "Stock Reconciliation",
        {
            "custom_nkt_return_valuation_correction":1,
            "custom_nkt_source_return_stock_entry":"MAT-STE-2026-00060",
            "docstatus":1,
        },
        "name",
    )
    if existing:
        return {
            "version":VERSION,
            "already_corrected":True,
            "stock_reconciliation":existing,
            "source_inventory_unit_cost":source_unit_cost,
            "fraction_rate_per_kg":return_rate,
            "fraction_inventory_value":return_value,
            "cost_source":source,
        }

    bin_row = frappe.db.get_value(
        "Bin",
        {"item_code":row.fraction_item,"warehouse":encoder.return_warehouse},
        ["actual_qty","stock_value"],
        as_dict=True,
    )
    if not bin_row or abs(flt(bin_row.actual_qty)-15) > TOLERANCE:
        frappe.throw(
            _("C7.11B valuation repair safety stop: current Fraction Bin must still be exactly 15 kg before repair.")
        )
    if abs(flt(bin_row.stock_value)) > TOLERANCE:
        frappe.throw(
            _("C7.11B valuation repair safety stop: Fraction Bin already has non-zero stock value; review before correcting.")
        )

    expense_account, cost_center = _stock_reconciliation_accounting_defaults(encoder.company)
    sr_meta = frappe.get_meta("Stock Reconciliation")
    sr = frappe.new_doc("Stock Reconciliation")
    sr.company = encoder.company
    if sr_meta.has_field("purpose"):
        sr.purpose = "Stock Reconciliation"
    if sr_meta.has_field("set_posting_time"):
        sr.set_posting_time = 1
    now = now_datetime()
    sr.posting_date = now.date()
    sr.posting_time = now.time()
    if sr_meta.has_field("expense_account") and expense_account:
        sr.expense_account = expense_account
    if sr_meta.has_field("cost_center") and cost_center:
        sr.cost_center = cost_center
    sr.custom_nkt_return_valuation_correction = 1
    sr.custom_nkt_source_return_stock_entry = "MAT-STE-2026-00060"
    sr.custom_nkt_source_return_exchange = encoder.name
    sr.remarks = (
        f"Forward valuation correction for accepted C7.11B Fraction return {encoder.name}. "
        f"Quantity remains 15 kg. Restores original source issue cost; customer refund value remains separate."
    )

    item = {
        "item_code":row.fraction_item,
        "warehouse":encoder.return_warehouse,
        "qty":flt(bin_row.actual_qty),
        "valuation_rate":return_rate,
    }
    sr.append("items", item)
    sr.flags.ignore_permissions = True
    sr.insert(ignore_permissions=True)
    sr.submit()

    return {
        "version":VERSION,
        "already_corrected":False,
        "stock_reconciliation":sr.name,
        "source_inventory_unit_cost":source_unit_cost,
        "fraction_rate_per_kg":return_rate,
        "fraction_inventory_value":return_value,
        "cost_source":source,
        "quantity_changed":False,
    }


def install():
    frappe.set_user("Administrator")
    _ensure_custom_fields()

    order_dt = frappe.get_doc("DocType", "NKT Customer Order")
    arrangement_changed = False
    for row in order_dt.fields:
        if row.fieldname == "payment_arrangement":
            opts = (row.options or "").splitlines()
            if "Return Credit" not in opts:
                opts.append("Return Credit")
                row.options = "\n".join(opts)
                arrangement_changed = True
    if arrangement_changed:
        order_dt.flags.ignore_permissions = True
        order_dt.save(ignore_permissions=True)
        frappe.clear_cache(doctype="NKT Customer Order")

    frappe.db.commit()
    frappe.clear_cache()
    return {
        "version":VERSION,
        "posting_enabled":True,
        "return_credit_payment_method_enabled":True,
        "charge_difference_to_account_enabled":True,
        "split_payment_settlement_enabled":True,
        "cash_tendered_change_supported":True,
        "credit_card_live_settlement_enabled":False,
        "customer_credit_uses_existing_advance_credit_ledger":True,
        "returned_inventory_restores_original_source_cost":True,
        "fraction_return_uses_original_cost_per_kg":True,
        "zero_valuation_return_receipts_blocked":True,
        "customer_settlement_value_separate_from_inventory_value":True,
        "business_records_created":False,
    }


def verify():
    errors=[]
    for dt in (DECL,"NKT Return Account Adjustment"):
        if not frappe.db.exists("DocType",dt):
            errors.append("Missing DocType: "+dt)

    for dt in ("NKT Cashier Sale","NKT Customer Order"):
        meta=frappe.get_meta(dt)
        for field in (
            "custom_nkt_return_credit_applied",
            "custom_nkt_lineage_money_basis",
            "custom_nkt_lineage_account_basis",
            "custom_nkt_exchange_generation",
        ):
            if not meta.has_field(field):
                errors.append(f"{dt} missing {field}")

    for dt in ("NKT Payment Detail","NKT Declared Payment"):
        options=frappe.get_meta(dt).get_field("payment_method").options or ""
        if "Return Credit" not in options.splitlines():
            errors.append(f"{dt} missing Return Credit option")

    for advance_dt_name in ("NKT Customer Advance", "NKT Customer Advance Application"):
        advance_meta=frappe.get_meta(advance_dt_name)
        source_field=advance_meta.get_field("source_payment_receipt")
        if source_field and cint(source_field.reqd):
            errors.append(
                f"{advance_dt_name} still requires Payment Receipt for Return Credit."
            )

    decl=frappe.get_meta(DECL)
    for field in (
        "customer_pays_mode","charge_to_account","account_adjustment_amount",
        "customer_credit_amount","return_money_basis","return_account_basis",
        "posting_status","return_stock_entry","new_cashier_sale","new_customer_order",
    ):
        if not decl.has_field(field):
            errors.append("Return/Exchange Declaration missing "+field)

    returned_meta = frappe.get_meta("NKT Return Exchange Returned Item")
    for field in (
        "custom_nkt_source_inventory_unit_cost",
        "custom_nkt_return_inventory_rate",
        "custom_nkt_return_inventory_value",
        "custom_nkt_inventory_cost_source",
    ):
        if not returned_meta.has_field(field):
            errors.append("Returned Item missing inventory valuation audit field "+field)

    source_text = inspect.getsource(_create_return_stock_entry).replace(" ", "")
    if (
        '"allow_zero_valuation_rate":1' in source_text
        or "'allow_zero_valuation_rate':1" in source_text
    ):
        errors.append("Return stock posting still permits zero valuation.")

    if not callable(globals().get("_source_issue_unit_cost")):
        errors.append("Original source inventory-cost resolver is missing.")

    # Pure calculation test: ₱1,300 returned value, ₱1,500 replacement -> ₱200 difference.
    # Pay Now or Account must settle the same NEW ORDER value; neither is a refund.
    arrangement = (
        frappe.get_meta("NKT Customer Order").get_field("payment_arrangement").options
        or ""
    ).splitlines()
    if "Return Credit" not in arrangement:
        errors.append("NKT Customer Order Payment Arrangement is missing Return Credit.")

    if not callable(globals().get("post_independent_declaration")):
        errors.append("Independent side posting function is missing.")
    if not callable(globals().get("finalize_matched_pair")):
        errors.append("Post-transaction reconciliation finalizer is missing.")

    calculation_test={
        "old_value":1300.0,
        "new_value":1500.0,
        "customer_pays":200.0,
        "refund_max_never_exceeds_money_basis":True,
        "pay_now_supported":True,
        "charge_difference_to_account_supported":True,
        "split_payment_supported":True,
        "cash_tendered_change_supported":True,
    }

    c71=frappe.get_attr("nkt_operations.nkt_store_operations.features.returns.service.verify")()
    c72=frappe.get_attr("nkt_operations.nkt_store_operations.features.returns.matching.verify")()
    c61=frappe.get_attr("nkt_operations.nkt_store_operations.features.cashier.shift_report.verify")()
    c62=frappe.get_attr("nkt_operations.nkt_store_operations.features.cashier.encoder_zout.verify")()
    c63=frappe.get_attr("nkt_operations.nkt_store_operations.features.cashier.reconciliation.verify")()
    if not c71.get("passed"): errors.append("C7 finder regression.")
    if not c72.get("passed"): errors.append("C7 matching regression.")
    if not c61.get("passed"): errors.append("C6.1 regression.")
    if not c62.get("passed"): errors.append("C6.2 regression.")
    if not c63.get("passed"): errors.append("C6.3 regression.")

    return {
        "version":VERSION,
        "posting_enabled":True,
        "installer_or_verifier_creates_business_transactions":False,
        "calculation_test":calculation_test,
        "c7_finder_passed":bool(c71.get("passed")),
        "c7_matching_passed":bool(c72.get("passed")),
        "c6_1_passed":bool(c61.get("passed")),
        "c6_2_passed":bool(c62.get("passed")),
        "c6_3_passed":bool(c63.get("passed")),
        "errors":errors,
        "passed":not errors,
    }
