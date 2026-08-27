from contextlib import contextmanager
from contextvars import ContextVar

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from nkt_operations.nkt_store_operations.features.payments_accounts.card_surcharge import (
    card_surcharge_amount,
    ensure_card_posting_allowed,
    normalize_payment_method,
)


TOLERANCE = 0.005


# C15C.10C narrow process-local bridge. This context is set only by
# create_cashier_movement() after it has verified a submitted Tender-derived
# Payment Receipt row. It lets the existing Cashier Movement controller call
# validate_cashier_shift(require_open=True) without losing the authoritative
# Store Edge observation merely because Primary receives it after shift close.
# Ordinary callers never receive this context and therefore retain the original
# Open-shift requirement.
_C15C10C_OBSERVED_TENDER_SHIFT_CONTEXT = ContextVar(
    "nkt_c15c10c_observed_tender_shift_context", default=None
)


@contextmanager
def _observed_tender_shift_validation_context(*, cashier_shift, company, settlement_location, cashier):
    token = _C15C10C_OBSERVED_TENDER_SHIFT_CONTEXT.set(
        {
            "cashier_shift": cashier_shift,
            "company": company,
            "settlement_location": settlement_location,
            "cashier": cashier,
        }
    )
    try:
        yield
    finally:
        _C15C10C_OBSERVED_TENDER_SHIFT_CONTEXT.reset(token)


def _observed_tender_shift_context_matches(*, cashier_shift, company, settlement_location, cashier):
    ctx = _C15C10C_OBSERVED_TENDER_SHIFT_CONTEXT.get()
    if not ctx:
        return False
    return (
        ctx.get("cashier_shift") == cashier_shift
        and ctx.get("company") == company
        and (not settlement_location or ctx.get("settlement_location") == settlement_location)
        and (not cashier or ctx.get("cashier") == cashier)
    )


# C15C.10F narrow process-local bridge for a Cash Drawer Adjustment that was
# physically accepted at Store Edge while its shift was Open, durably preserved
# at Primary, and only materialized after that shift later closed.
#
# Unlike a document flag by itself, this context is opened only by the canonical
# Cash Drawer Adjustment posting hook after it has revalidated the exact
# preserved event/journal/source/amount binding. The Cashier Movement controller
# still calls validate_cashier_shift(require_open=True); this context is the
# narrowly-scoped evidence that the physical event was valid while Open.
_C15C10F_PRESERVED_CASH_DRAWER_SHIFT_CONTEXT = ContextVar(
    "nkt_c15c10f_preserved_cash_drawer_shift_context", default=None
)


@contextmanager
def _preserved_cash_drawer_shift_validation_context(
    *,
    cashier_shift,
    company,
    settlement_location,
    cashier,
):
    token = _C15C10F_PRESERVED_CASH_DRAWER_SHIFT_CONTEXT.set(
        {
            "cashier_shift": cashier_shift,
            "company": company,
            "settlement_location": settlement_location,
            "cashier": cashier,
        }
    )
    try:
        yield
    finally:
        _C15C10F_PRESERVED_CASH_DRAWER_SHIFT_CONTEXT.reset(token)


def _preserved_cash_drawer_shift_context_matches(
    *,
    cashier_shift,
    company,
    settlement_location,
    cashier,
):
    ctx = _C15C10F_PRESERVED_CASH_DRAWER_SHIFT_CONTEXT.get()
    if not ctx:
        return False
    return (
        ctx.get("cashier_shift") == cashier_shift
        and ctx.get("company") == company
        and (not settlement_location or ctx.get("settlement_location") == settlement_location)
        and (not cashier or ctx.get("cashier") == cashier)
    )


# C15C.10I narrow process-local bridge for a Return/Exchange that was
# physically accepted at Store Edge while its original Cashier Shift was valid,
# then materialized at Primary after that historical shift closed.
_C15C10I_PRESERVED_RETURN_EXCHANGE_SHIFT_CONTEXT = ContextVar(
    "nkt_c15c10i_preserved_return_exchange_shift_context", default=None
)


@contextmanager
def preserved_return_exchange_shift_validation_context(
    *,
    cashier_shift,
    company,
    settlement_location,
    cashier,
):
    token = _C15C10I_PRESERVED_RETURN_EXCHANGE_SHIFT_CONTEXT.set(
        {
            "cashier_shift": cashier_shift,
            "company": company,
            "settlement_location": settlement_location,
            "cashier": cashier,
        }
    )
    try:
        yield
    finally:
        _C15C10I_PRESERVED_RETURN_EXCHANGE_SHIFT_CONTEXT.reset(token)


def _preserved_return_exchange_shift_context_matches(
    *,
    cashier_shift,
    company,
    settlement_location,
    cashier,
):
    ctx = _C15C10I_PRESERVED_RETURN_EXCHANGE_SHIFT_CONTEXT.get()
    if not ctx:
        return False
    return (
        ctx.get("cashier_shift") == cashier_shift
        and ctx.get("company") == company
        and (not settlement_location or ctx.get("settlement_location") == settlement_location)
        and (not cashier or ctx.get("cashier") == cashier)
    )


def validate_cashier_shift(
    cashier_shift,
    company,
    settlement_location=None,
    cashier=None,
    cash_register=None,
    require_open=True,
):
    if not cashier_shift:
        frappe.throw(_("Cashier Shift is required."))

    shift = frappe.db.get_value(
        "NKT Cashier Shift",
        cashier_shift,
        [
            "docstatus",
            "status",
            "company",
            "settlement_location",
            "cash_register",
            "cashier",
        ],
        as_dict=True,
    )

    if not shift:
        frappe.throw(_("Cashier Shift was not found."))

    if require_open and (
        cint(shift.docstatus) != 0 or shift.status != "Open"
    ):
        observed_tender = _observed_tender_shift_context_matches(
            cashier_shift=cashier_shift,
            company=company,
            settlement_location=settlement_location,
            cashier=cashier,
        )
        preserved_cash_drawer = _preserved_cash_drawer_shift_context_matches(
            cashier_shift=cashier_shift,
            company=company,
            settlement_location=settlement_location,
            cashier=cashier,
        )
        preserved_return_exchange = _preserved_return_exchange_shift_context_matches(
            cashier_shift=cashier_shift,
            company=company,
            settlement_location=settlement_location,
            cashier=cashier,
        )
        if not (observed_tender or preserved_cash_drawer or preserved_return_exchange):
            frappe.throw(
                _("Cashier Shift {0} is not open.").format(cashier_shift)
            )

    if shift.company != company:
        frappe.throw(_("Cashier Shift belongs to another company."))

    if settlement_location and shift.settlement_location != settlement_location:
        frappe.throw(
            _(
                "Cashier Shift location does not match the selected "
                "Settlement Location."
            )
        )

    if cashier and shift.cashier != cashier:
        frappe.throw(
            _("Cashier Shift belongs to {0}, not {1}.").format(
                shift.cashier, cashier
            )
        )

    return shift


def get_open_shift_for_user(
    company=None,
    user=None,
    settlement_location=None,
):
    user = user or frappe.session.user

    filters = {
        "docstatus": 0,
        "status": "Open",
        "cashier": user,
    }

    if company:
        filters["company"] = company

    if settlement_location:
        filters["settlement_location"] = settlement_location

    shifts = frappe.get_all(
        "NKT Cashier Shift",
        filters=filters,
        fields=[
            "name",
            "company",
            "settlement_location",
            "cash_register",
            "cashier",
            "shift_start",
        ],
        order_by="shift_start desc",
        limit=2,
    )

    if len(shifts) == 1:
        return shifts[0]

    return None


def create_cashier_movement(
    *,
    company,
    posting_datetime,
    cashier_shift,
    settlement_location,
    cashier,
    movement_type,
    direction,
    payment_method,
    amount,
    source_doctype,
    source_name,
    settlement_amount=None,
    card_surcharge=0,
    source_row=None,
    customer=None,
    reference_number=None,
    remarks=None,
    allow_closed_observed_shift=False,
    force_posted_status=False,
):
    payment_method = normalize_payment_method(payment_method)
    ensure_card_posting_allowed(payment_method, "Card Cashier Movement")
    amount = flt(amount)
    settlement_amount = flt(settlement_amount if settlement_amount is not None else amount)
    card_surcharge = flt(card_surcharge)
    allow_closed_observed_shift = bool(allow_closed_observed_shift)
    force_posted_status = bool(force_posted_status)

    if allow_closed_observed_shift:
        if source_doctype != "NKT Payment Receipt" or not source_name or not source_row:
            frappe.throw(
                _("Closed-shift observed-tender compatibility is limited to a specific Payment Receipt row.")
            )
        receipt = frappe.db.get_value(
            "NKT Payment Receipt",
            source_name,
            [
                "docstatus",
                "source_primary_tender_intent",
                "cashier_shift",
                "settlement_location",
                "received_by",
            ],
            as_dict=True,
        )
        if (
            not receipt
            or cint(receipt.docstatus) != 1
            or not receipt.source_primary_tender_intent
            or receipt.cashier_shift != cashier_shift
            or receipt.settlement_location != settlement_location
            or receipt.received_by != cashier
        ):
            frappe.throw(
                _("Closed-shift observed-tender movement is not bound to the authoritative Tender-derived Payment Receipt.")
            )
        if not frappe.db.exists(
            "NKT Payment Detail",
            {"name": source_row, "parent": source_name, "parenttype": "NKT Payment Receipt"},
        ):
            frappe.throw(_("Cashier Movement source row is not part of the authoritative Payment Receipt."))

    if payment_method != "Card" and abs(card_surcharge) > TOLERANCE:
        frappe.throw(_("Only Card movements may carry a Card Surcharge. Maya never carries the 2% surcharge."))

    if abs(amount - settlement_amount - card_surcharge) > TOLERANCE:
        frappe.throw(
            _("Cashier Movement amount must equal settlement amount plus Card surcharge.")
        )

    surcharge_enforced_types = {
        "Customer Order Payment",
        "Customer Account Collection",
        "Account Collection",
        "Exchange Difference Collected",
    }
    if payment_method == "Card" and direction == "In" and movement_type in surcharge_enforced_types:
        expected_surcharge = card_surcharge_amount(settlement_amount, "Card")
        if abs(card_surcharge - expected_surcharge) > TOLERANCE:
            frappe.throw(
                _("Card surcharge must be exactly 2% of the Card settlement amount.")
            )

    if amount <= TOLERANCE:
        return None

    if direction not in {"In", "Out"}:
        frappe.throw(_("Movement Direction must be In or Out."))

    shift = validate_cashier_shift(
        cashier_shift=cashier_shift,
        company=company,
        settlement_location=settlement_location,
        cashier=cashier,
        require_open=not allow_closed_observed_shift,
    )

    existing_filters = {
        "source_doctype": source_doctype,
        "source_name": source_name,
        "docstatus": ["!=", 2],
    }

    if source_row:
        existing_filters["source_row"] = source_row

    existing = frappe.db.get_value(
        "NKT Cashier Movement",
        existing_filters,
        "name",
    )

    if existing:
        return frappe.get_doc("NKT Cashier Movement", existing)

    movement = frappe.get_doc(
        {
            "doctype": "NKT Cashier Movement",
            "company": company,
            "posting_datetime": posting_datetime or now_datetime(),
            "cashier_shift": cashier_shift,
            "settlement_location": shift.settlement_location,
            "cash_register": shift.cash_register,
            "cashier": shift.cashier,
            "movement_type": movement_type,
            "direction": direction,
            "payment_method": payment_method,
            "amount": amount,
            "settlement_amount": settlement_amount,
            "card_surcharge": card_surcharge,
            "affects_cash_drawer": 1 if payment_method == "Cash" else 0,
            "customer": customer,
            "source_doctype": source_doctype,
            "source_name": source_name,
            "source_row": source_row,
            "reference_number": reference_number,
            "remarks": remarks,
            "status": "Posted" if force_posted_status else "Draft",
        }
    )

    movement.flags.ignore_permissions = True
    if allow_closed_observed_shift:
        movement.flags.nkt_c15c10c_observed_tender = True
        with _observed_tender_shift_validation_context(
            cashier_shift=cashier_shift,
            company=company,
            settlement_location=settlement_location,
            cashier=cashier,
        ):
            movement.insert()
            movement.flags.nkt_c15c10c_observed_tender = True
            movement.submit()
    else:
        movement.insert()
        movement.submit()

    return movement


def cancel_source_cashier_movements(source_doctype, source_name):
    names = frappe.get_all(
        "NKT Cashier Movement",
        filters={
            "source_doctype": source_doctype,
            "source_name": source_name,
            "docstatus": ["!=", 2],
        },
        pluck="name",
    )

    for name in names:
        movement = frappe.get_doc("NKT Cashier Movement", name)

        shift_status = frappe.db.get_value(
            "NKT Cashier Shift",
            movement.cashier_shift,
            ["docstatus", "status"],
            as_dict=True,
        )

        if shift_status and (
            cint(shift_status.docstatus) == 1
            or shift_status.status in {"Turned Over - Awaiting Review", "Reviewed / Closed", "Closed"}
        ):
            frappe.throw(
                _(
                    "Cashier Shift {0} has already been turned over or closed. "
                    "The source transaction is locked and "
                    "cannot be cancelled directly."
                ).format(movement.cashier_shift)
            )

        movement.flags.ignore_permissions = True

        if movement.docstatus == 1:
            movement.cancel()
        elif movement.docstatus == 0:
            movement.delete()


def refresh_shift_totals(cashier_shift):
    if not cashier_shift:
        return

    shift = frappe.get_doc("NKT Cashier Shift", cashier_shift)
    shift.refresh_totals_to_db()
