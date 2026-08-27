import hashlib
import json
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt, get_datetime, now_datetime

from nkt_operations.nkt_store_operations.features.payments_accounts.card_surcharge import (
    normalize_payment_method,
    row_card_surcharge,
    row_collected_amount,
)

TOLERANCE = 0.005
MATCHED_STATUSES = {
    "Matched",
    "Matched with Customer Warning",
    "Matched with Warehouse Warning",
    "Matched with Customer and Warehouse Warning",
}


def _r(value, places=4):
    return round(flt(value), places)


def _clean(value):
    return " ".join((value or "").strip().lower().split())


def _row_rate(row):
    rate = flt(getattr(row, "final_rate", None))
    if not rate:
        rate = flt(getattr(row, "standard_rate", 0)) + flt(getattr(row, "price_adjustment", 0))
    return rate


def build_basket(items):
    grouped = defaultdict(lambda: {"qty": 0.0, "amount": 0.0})
    for row in items or []:
        item = getattr(row, "item", None) or getattr(row, "item_code", None)
        if not item:
            continue
        uom = getattr(row, "uom", None) or ""
        rate = _row_rate(row)
        qty = flt(getattr(row, "quantity", None) or getattr(row, "qty", 0))
        key = (item, uom, _r(rate, 4))
        grouped[key]["qty"] += qty
        grouped[key]["amount"] += qty * rate
    return [
        {"item": key[0], "uom": key[1], "rate": key[2], "qty": _r(value["qty"], 6), "amount": _r(value["amount"], 2)}
        for key, value in sorted(grouped.items())
    ]


def build_warehouse_allocation(items):
    grouped = defaultdict(float)
    for row in items or []:
        item = getattr(row, "item", None) or getattr(row, "item_code", None)
        if not item:
            continue
        uom = getattr(row, "uom", None) or ""
        warehouse = getattr(row, "source_warehouse", None) or ""
        rate = _row_rate(row)
        qty = flt(getattr(row, "quantity", None) or getattr(row, "qty", 0))
        key = (item, uom, _r(rate, 4), warehouse)
        grouped[key] += qty
    return [
        {"item": key[0], "uom": key[1], "rate": key[2], "warehouse": key[3], "qty": _r(qty, 6)}
        for key, qty in sorted(grouped.items())
    ]


def build_payment_rows(payments):
    grouped = defaultdict(float)
    detailed = []
    for row in payments or []:
        method = normalize_payment_method(getattr(row, "payment_method", None))
        amount = flt(getattr(row, "amount", 0))
        if not method or amount <= TOLERANCE:
            continue
        if method in {"Cash", "Account"}:
            grouped[method] += amount
            continue
        reference = getattr(row, "check_number", None) or getattr(row, "reference_number", None) or ""
        detailed.append({
            "method": method,
            "amount": _r(amount, 2),
            "reference": _clean(reference),
        })
    result = [{"method": method, "amount": _r(amount, 2), "reference": ""} for method, amount in sorted(grouped.items())]
    result.extend(sorted(detailed, key=lambda d: (d["method"], d["reference"], d["amount"])))
    return result


def build_payment_summary(payments):
    grouped = defaultdict(float)
    for row in payments or []:
        method = normalize_payment_method(getattr(row, "payment_method", None))
        amount = flt(getattr(row, "amount", 0))
        if method and amount > TOLERANCE:
            grouped[method] += amount
    return [{"method": method, "amount": _r(amount, 2)} for method, amount in sorted(grouped.items())]


def _fingerprint(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_basket_fingerprint(items):
    return _fingerprint(build_basket(items))


def build_warehouse_fingerprint(items):
    return _fingerprint(build_warehouse_allocation(items))


def build_payment_fingerprint(payments):
    return _fingerprint(build_payment_rows(payments))


def basket_summary_text(items):
    return "; ".join(f"{row['item']} {row['qty']:g} {row['uom']} @ {row['rate']:.2f}" for row in build_basket(items))


def warehouse_summary_text(items):
    return "; ".join(
        f"{row['item']} {row['qty']:g} {row['uom']} from {row['warehouse'] or '[No Warehouse]'}"
        for row in build_warehouse_allocation(items)
    )


def payment_summary_text(payments):
    parts = []
    for row in build_payment_rows(payments):
        ref = f" [{row['reference']}]" if row.get("reference") else ""
        parts.append(f"{row['method']}: {row['amount']:.2f}{ref}")
    return ", ".join(parts)


def _same_customer(cashier_sale, order):
    return bool(cashier_sale.customer and order.customer and cashier_sale.customer == order.customer)


def _candidate_time(row, field):
    return get_datetime(row.get(field) or row.get("creation"))


def _select_candidate(source_doc, candidates, source_time_field, candidate_time_field, source_slip=None):
    if not candidates:
        return None, ""

    source_customer = source_doc.get("customer")
    same_customer_candidates = [
        candidate
        for candidate in candidates
        if source_customer
        and candidate.get("customer")
        and candidate.get("customer") == source_customer
    ]

    if not same_customer_candidates:
        candidate_names = ", ".join(
            sorted(
                {
                    candidate.get("customer_name")
                    or candidate.get("customer")
                    or "[No Customer]"
                    for candidate in candidates
                }
            )
        )
        return None, _(
            "Exact item/payment candidate(s) exist, but the Customer record "
            "does not match. Automatic matching was blocked. Candidate "
            "customer(s): {0}."
        ).format(candidate_names)

    slip = (source_slip or "").strip()
    if slip:
        slip_matches = [
            candidate
            for candidate in same_customer_candidates
            if (candidate.get("source_order_slip") or "").strip() == slip
        ]
        if len(slip_matches) == 1:
            return slip_matches[0], ""
        if len(slip_matches) > 1:
            return None, _(
                "More than one exact same-customer candidate uses "
                "Source Order Slip {0}."
            ).format(slip)

    if len(same_customer_candidates) == 1:
        return same_customer_candidates[0], ""

    return None, _(
        "Several exact same-customer item/payment candidates exist. "
        "Automatic matching was blocked; manually pair the retained "
        "handwritten slips."
    )


def _mark_cashier_status(name, status, warning=None):
    frappe.db.set_value(
        "NKT Cashier Sale",
        name,
        {
            "reconciliation_status": status,
            "status": status if status != "Unmatched" else "Submitted - Unmatched",
            "reconciliation_warning": warning or "",
        },
        update_modified=False,
    )


def _mark_order_status(name, status, warning=None):
    frappe.db.set_value(
        "NKT Customer Order",
        name,
        {"cashier_reconciliation_status": status, "cashier_reconciliation_warning": warning or ""},
        update_modified=False,
    )


def _paid_rows(sale):
    return [
        row for row in (sale.get("payments") or [])
        if normalize_payment_method(row.payment_method) not in {"Account", "Return Credit"}
        and flt(row.amount) > TOLERANCE
    ]


def ensure_cash_basis_payment_receipt(cashier_sale):
    """Create the cashier's official cash-basis payment record immediately.

    The encoder order may not exist yet. Account portions are deliberately not
    copied because a charge to account is not money received.
    """
    sale = frappe.get_doc("NKT Cashier Sale", cashier_sale)
    existing = frappe.db.get_value(
        "NKT Payment Receipt",
        {"source_cashier_sale": sale.name, "docstatus": ["!=", 2]},
        "name",
    )
    if existing:
        if sale.linked_payment_receipt != existing:
            frappe.db.set_value("NKT Cashier Sale", sale.name, "linked_payment_receipt", existing, update_modified=False)
        return existing

    rows = _paid_rows(sale)
    if not rows:
        return None

    receipt = frappe.get_doc({
        "doctype": "NKT Payment Receipt",
        "company": sale.company,
        "receipt_datetime": sale.sale_datetime,
        "payment_purpose": "Cashier Sale Payment",
        "customer": sale.customer,
        "customer_name": sale.customer_name,
        "received_by": sale.cashier,
        "encoded_by": sale.cashier,
        "source_cashier_sale": sale.name,
        "allocation_status": "Unallocated - Awaiting Encoder",
        "remarks": f"Cash-basis payment created immediately from Cashier Sale {sale.name}; awaiting encoder-order allocation.",
    })
    for row in rows:
        receipt.append("payments", {
            "payment_method": normalize_payment_method(row.payment_method),
            "amount": row.amount,
            "card_surcharge": row_card_surcharge(row),
            "collected_amount": row_collected_amount(row),
            "cash_tendered": row.cash_tendered if normalize_payment_method(row.payment_method) == "Cash" else 0,
            "reference_number": row.reference_number,
            "reference_datetime": row.reference_datetime,
            "bank_or_provider": row.bank_or_provider,
            "check_number": row.check_number,
            "check_date": row.check_date,
            "verification_status": "Not Required",
            "affects_cash_drawer": 1 if row.payment_method == "Cash" else 0,
            "remarks": row.remarks,
        })
    receipt.flags.ignore_permissions = True
    receipt.insert(ignore_permissions=True)
    receipt.submit()
    frappe.db.set_value("NKT Cashier Sale", sale.name, "linked_payment_receipt", receipt.name, update_modified=False)
    return receipt.name


def _apply_account_only_order_summary(order):
    """Finalize the official encoder status for a fully charged account sale."""
    account_amount = max(flt(order.declared_account), 0)
    return_credit = max(flt(order.get("custom_nkt_return_credit_applied")), 0)
    remaining = max(flt(order.grand_total) - account_amount - return_credit, 0)
    if account_amount > TOLERANCE and remaining <= TOLERANCE:
        payment_status = "Charged to Account"
        status = "Pending Credit Control"
    elif account_amount > TOLERANCE:
        payment_status = "Partially Paid"
        status = "Partially Paid"
    elif return_credit > TOLERANCE and remaining <= TOLERANCE:
        payment_status = "Paid"
        status = "Ready for Release"
    else:
        payment_status = "Unpaid"
        status = "Awaiting Payment"
    if order.requires_admin_confirmation and order.admin_confirmation_status != "Confirmed":
        status = "Pending Admin Confirmation"
    frappe.db.set_value(
        "NKT Customer Order",
        order.name,
        {
            "amount_paid": 0,
            "amount_due": remaining if account_amount <= TOLERANCE else account_amount,
            "payment_status": payment_status,
            "status": status,
        },
        update_modified=False,
    )


def _link_receipt_to_order(sale, order):
    receipt_name = ensure_cash_basis_payment_receipt(sale.name)
    if not receipt_name:
        # Fully charged-to-account sales correctly have no cash-basis receipt.
        _apply_account_only_order_summary(order)
        return None

    receipt = frappe.get_doc("NKT Payment Receipt", receipt_name)
    if receipt.customer_order and receipt.customer_order != order.name:
        frappe.throw(_("Payment Receipt {0} is already allocated to another encoder order.").format(receipt.name))

    frappe.db.set_value(
        "NKT Payment Receipt",
        receipt.name,
        {
            "customer_order": order.name,
            "official_customer": order.customer,
            "official_customer_name": order.customer_name,
            "allocation_status": "Allocated to Encoder Order",
        },
        update_modified=False,
    )
    receipt.reload()
    receipt.update_linked_order_payment_summary()
    return receipt.name


def try_match_cashier_sale(cashier_sale):
    sale = frappe.get_doc("NKT Cashier Sale", cashier_sale)
    if sale.docstatus != 1 or sale.reconciliation_status in MATCHED_STATUSES:
        return
    candidates = frappe.get_all(
        "NKT Customer Order",
        filters={
            "docstatus": 1,
            "company": sale.company,
            "order_date": sale.business_date,
            "nkt_basket_fingerprint": sale.nkt_basket_fingerprint,
            "nkt_payment_fingerprint": sale.nkt_payment_fingerprint,
            "cashier_reconciliation_status": ["in", ["Unmatched", "Ambiguous", ""]],
        },
        fields=["name", "customer", "customer_name", "source_order_slip", "creation", "grand_total", "payment_status"],
        order_by="creation asc",
    )
    selected, warning = _select_candidate(sale.as_dict(), candidates, "sale_datetime", "creation", sale.source_order_slip)
    if not selected:
        # C5.5 NO-EXACT-MATCH STATUS HOTFIX
        # Ambiguous is reserved for multiple exact SAME-CUSTOMER
        # candidates. Wrong-customer fingerprint collisions are
        # No Exact Match / Unmatched.
        warning_text = str(warning or "")
        ambiguous_same_customer = (
            "Several exact same-customer" in warning_text
            or "More than one exact same-customer" in warning_text
        )
        status = "Ambiguous" if ambiguous_same_customer else "Unmatched"
        _mark_cashier_status(sale.name, status, warning)
        if warning and status == "Ambiguous":
            for c in candidates:
                _mark_order_status(c.name, "Ambiguous", warning)
        return
    _complete_match(sale.name, selected.name)


def try_match_customer_order(customer_order):
    order = frappe.get_doc("NKT Customer Order", customer_order)
    if order.docstatus != 1 or order.cashier_reconciliation_status in MATCHED_STATUSES:
        return
    candidates = frappe.get_all(
        "NKT Cashier Sale",
        filters={
            "docstatus": 1,
            "company": order.company,
            "business_date": order.order_date,
            "nkt_basket_fingerprint": order.nkt_basket_fingerprint,
            "nkt_payment_fingerprint": order.nkt_payment_fingerprint,
            "reconciliation_status": ["in", ["Unmatched", "Ambiguous"]],
        },
        fields=["name", "customer", "customer_name", "source_order_slip", "sale_datetime", "creation", "grand_total"],
        order_by="sale_datetime asc",
    )
    selected, warning = _select_candidate(order.as_dict(), candidates, "creation", "sale_datetime", order.source_order_slip)
    if not selected:
        # C5.5 NO-EXACT-MATCH STATUS HOTFIX
        # Ambiguous is reserved for multiple exact SAME-CUSTOMER
        # candidates. Wrong-customer fingerprint collisions are
        # No Exact Match / Unmatched.
        warning_text = str(warning or "")
        ambiguous_same_customer = (
            "Several exact same-customer" in warning_text
            or "More than one exact same-customer" in warning_text
        )
        status = "Ambiguous" if ambiguous_same_customer else "Unmatched"
        _mark_order_status(order.name, status, warning)
        if warning and status == "Ambiguous":
            for c in candidates:
                _mark_cashier_status(c.name, "Ambiguous", warning)
        return
    _complete_match(selected.name, order.name)


def _complete_match(cashier_sale, customer_order):
    sale = frappe.get_doc("NKT Cashier Sale", cashier_sale)
    order = frappe.get_doc("NKT Customer Order", customer_order)
    if sale.nkt_basket_fingerprint != order.nkt_basket_fingerprint or sale.nkt_payment_fingerprint != order.nkt_payment_fingerprint:
        frappe.throw(_("The cashier and encoder item/payment baskets are not identical."))

    if not _same_customer(sale, order):
        frappe.throw(
            _(
                "Cashier Sale {0} and Customer Order {1} belong to different "
                "Customer records. Matching is blocked until the correct pair "
                "is selected."
            ).format(sale.name, order.name)
        )

    receipt_name = _link_receipt_to_order(sale, order)
    same_warehouse = (
        sale.nkt_warehouse_fingerprint
        == order.nkt_warehouse_fingerprint
    )
    warnings = []
    if not same_warehouse:
        warnings.append(
            _(
                "Cashier and encoder source-warehouse allocations differ. "
                "The encoder order remains the official inventory/release "
                "record; verify the retained handwritten slip during EOD "
                "reconciliation."
            )
        )

    status = "Matched" if same_warehouse else "Matched with Warehouse Warning"
    warning = " ".join(warnings)
    now = now_datetime()
    frappe.db.set_value("NKT Cashier Sale", sale.name, {
        "status": status,
        "reconciliation_status": status,
        "matched_customer_order": order.name,
        "linked_payment_receipt": receipt_name,
        "reconciliation_warning": warning,
        "reconciled_on": now,
    }, update_modified=False)
    frappe.db.set_value("NKT Customer Order", order.name, {
        "cashier_reconciliation_status": status,
        "matched_cashier_sale": sale.name,
        "cashier_reconciliation_warning": warning,
        "cashier_reconciled_on": now,
    }, update_modified=False)
    sale.add_comment("Info", _("Matched to encoder Customer Order {0}. {1}").format(order.name, warning))
    order.add_comment("Info", _("Matched to Cashier Sale {0}. {1}").format(sale.name, warning))

    # V2.0C.5.2.1 auto-apply verified Customer Advance after sale match
    from nkt_operations.nkt_store_operations.features.payments_accounts.internal.auto_advance import (
        auto_apply_customer_advance_for_order,
    )
    auto_apply_customer_advance_for_order(order.name)


def unmatch_cashier_sale(cashier_sale):
    """Used when the cashier sale itself is cancelled.

    The cash-basis receipt and shift movement must be reversed together.
    """
    sale = frappe.get_doc("NKT Cashier Sale", cashier_sale)
    order_name = sale.matched_customer_order
    receipt_name = sale.linked_payment_receipt
    if receipt_name and frappe.db.exists("NKT Payment Receipt", receipt_name):
        receipt = frappe.get_doc("NKT Payment Receipt", receipt_name)
        if receipt.docstatus == 1:
            receipt.flags.ignore_permissions = True
            receipt.cancel()
    if order_name and frappe.db.exists("NKT Customer Order", order_name):
        frappe.db.set_value("NKT Customer Order", order_name, {
            "cashier_reconciliation_status": "Unmatched",
            "matched_cashier_sale": None,
            "cashier_reconciliation_warning": "Cashier sale was cancelled; encoder order requires review.",
            "cashier_reconciled_on": None,
        }, update_modified=False)


def unlink_customer_order(customer_order):
    """Unlink a cancelled encoder order without erasing money actually received."""
    order = frappe.get_doc("NKT Customer Order", customer_order)
    sale_name = order.matched_cashier_sale
    if not sale_name or not frappe.db.exists("NKT Cashier Sale", sale_name):
        return
    sale = frappe.get_doc("NKT Cashier Sale", sale_name)
    receipt_name = sale.linked_payment_receipt
    if receipt_name and frappe.db.exists("NKT Payment Receipt", receipt_name):
        frappe.db.set_value("NKT Payment Receipt", receipt_name, {
            "customer_order": None,
            "official_customer": None,
            "official_customer_name": None,
            "allocation_status": "Unallocated - Awaiting Encoder",
        }, update_modified=False)
    frappe.db.set_value("NKT Cashier Sale", sale.name, {
        "status": "Submitted - Unmatched",
        "reconciliation_status": "Unmatched",
        "matched_customer_order": None,
        "reconciliation_warning": "Encoder order was cancelled; payment remains recorded and requires rematching or the controlled refund workflow.",
        "reconciled_on": None,
    }, update_modified=False)


def backfill_fingerprints():
    orders = frappe.get_all("NKT Customer Order", filters={"docstatus": ["<", 2]}, pluck="name")
    for name in orders:
        doc = frappe.get_doc("NKT Customer Order", name)
        frappe.db.set_value("NKT Customer Order", name, {
            "nkt_basket_fingerprint": build_basket_fingerprint(doc.get("items") or []),
            "nkt_payment_fingerprint": build_payment_fingerprint(doc.get("declared_payments") or []),
            "nkt_warehouse_fingerprint": build_warehouse_fingerprint(doc.get("items") or []),
            "cashier_reconciliation_status": doc.cashier_reconciliation_status or "Unmatched",
        }, update_modified=False)
    sales = frappe.get_all("NKT Cashier Sale", filters={"docstatus": ["<", 2]}, pluck="name")
    for name in sales:
        doc = frappe.get_doc("NKT Cashier Sale", name)
        frappe.db.set_value("NKT Cashier Sale", name, {
            "nkt_basket_fingerprint": build_basket_fingerprint(doc.get("items") or []),
            "nkt_payment_fingerprint": build_payment_fingerprint(doc.get("payments") or []),
            "nkt_warehouse_fingerprint": build_warehouse_fingerprint(doc.get("items") or []),
        }, update_modified=False)
    frappe.db.commit()
    frappe.clear_cache()
    print(f"Backfilled {len(orders)} customer orders and {len(sales)} cashier sales.")


def backfill_cash_basis_payment_receipts():
    names = frappe.get_all(
        "NKT Cashier Sale",
        filters={"docstatus": 1},
        pluck="name",
        order_by="creation asc",
    )
    created = 0
    linked = 0
    for name in names:
        before = frappe.db.get_value("NKT Cashier Sale", name, "linked_payment_receipt")
        receipt_name = ensure_cash_basis_payment_receipt(name)
        if receipt_name and not before:
            created += 1
        sale = frappe.get_doc("NKT Cashier Sale", name)
        if receipt_name and sale.matched_customer_order:
            order = frappe.get_doc("NKT Customer Order", sale.matched_customer_order)
            _link_receipt_to_order(sale, order)
            linked += 1
    frappe.db.commit()
    frappe.clear_cache()
    print(f"Cash-basis receipt backfill complete. Created/linked: {created}; allocated to matched orders: {linked}.")


@frappe.whitelist(methods=["POST"])
def retry_match(cashier_sale=None, customer_order=None):
    if cashier_sale:
        frappe.get_doc("NKT Cashier Sale", cashier_sale).check_permission("read")
        try_match_cashier_sale(cashier_sale)
        return frappe.db.get_value("NKT Cashier Sale", cashier_sale, ["reconciliation_status", "matched_customer_order", "reconciliation_warning"], as_dict=True)
    if customer_order:
        frappe.get_doc("NKT Customer Order", customer_order).check_permission("read")
        try_match_customer_order(customer_order)
        return frappe.db.get_value("NKT Customer Order", customer_order, ["cashier_reconciliation_status", "matched_cashier_sale", "cashier_reconciliation_warning"], as_dict=True)
    frappe.throw(_("Cashier Sale or Customer Order is required."))
