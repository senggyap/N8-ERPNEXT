from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

import frappe
from frappe import _
from frappe.utils import flt

CARD_METHOD = "Card"
LEGACY_CARD_METHOD = "Credit Card"
CARD_SURCHARGE_RATE = Decimal("0.02")
CENT = Decimal("0.01")
CARD_PRODUCTION_UNLOCKED = True


def normalize_payment_method(value):
    method = (value or "").strip()
    return CARD_METHOD if method == LEGACY_CARD_METHOD else method




def ensure_card_posting_allowed(payment_method, context="Card payment"):
    method = normalize_payment_method(payment_method)
    if method != CARD_METHOD:
        return
    if CARD_PRODUCTION_UNLOCKED or getattr(frappe.flags, "nkt_card_acceptance_fixture", False):
        return
    frappe.throw(
        _("{0} remains locked pending the controlled Card production-unlock phase. "
          "The exact 2% surcharge is installed for acceptance testing only; Maya has no surcharge.").format(context)
    )


def money_decimal(value):
    return Decimal(str(flt(value or 0))).quantize(CENT, rounding=ROUND_HALF_UP)


def card_surcharge_amount(base_amount, payment_method):
    if normalize_payment_method(payment_method) != CARD_METHOD:
        return 0.0
    base = money_decimal(base_amount)
    return float((base * CARD_SURCHARGE_RATE).quantize(CENT, rounding=ROUND_HALF_UP))


def collected_amount(base_amount, payment_method):
    base = money_decimal(base_amount)
    surcharge = Decimal(str(card_surcharge_amount(base, payment_method)))
    return float((base + surcharge).quantize(CENT, rounding=ROUND_HALF_UP))


def row_card_surcharge(row):
    method = normalize_payment_method(
        row.get("payment_method") if isinstance(row, dict) else getattr(row, "payment_method", None)
    )
    amount = row.get("amount") if isinstance(row, dict) else getattr(row, "amount", 0)
    return card_surcharge_amount(amount, method)


def row_collected_amount(row):
    method = normalize_payment_method(
        row.get("payment_method") if isinstance(row, dict) else getattr(row, "payment_method", None)
    )
    amount = row.get("amount") if isinstance(row, dict) else getattr(row, "amount", 0)
    return collected_amount(amount, method)


def apply_payment_row_card_fields(row):
    method = normalize_payment_method(
        row.get("payment_method") if isinstance(row, dict) else getattr(row, "payment_method", None)
    )
    amount = row.get("amount") if isinstance(row, dict) else getattr(row, "amount", 0)
    surcharge = card_surcharge_amount(amount, method)
    collected = collected_amount(amount, method)

    if isinstance(row, dict):
        row["payment_method"] = method
        row["card_surcharge"] = surcharge
        row["collected_amount"] = collected
    else:
        row.payment_method = method
        if getattr(row, "meta", None) and row.meta.has_field("card_surcharge"):
            row.card_surcharge = surcharge
        if getattr(row, "meta", None) and row.meta.has_field("collected_amount"):
            row.collected_amount = collected

    return {
        "payment_method": method,
        "settlement_amount": float(money_decimal(amount)),
        "card_surcharge": surcharge,
        "collected_amount": collected,
    }
