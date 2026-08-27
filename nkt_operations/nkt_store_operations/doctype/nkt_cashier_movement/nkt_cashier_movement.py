import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

from nkt_operations.nkt_store_operations.features.payments_accounts.cash_ledger import (
    refresh_shift_totals,
    validate_cashier_shift,
)
from nkt_operations.nkt_store_operations.features.payments_accounts.card_surcharge import normalize_payment_method


TOLERANCE = 0.005


class NKTCashierMovement(Document):
    def before_validate(self):
        if not self.posting_datetime:
            self.posting_datetime = now_datetime()

        self.payment_method = normalize_payment_method(self.payment_method)
        if self.meta.has_field("settlement_amount") and flt(self.settlement_amount) <= TOLERANCE:
            self.settlement_amount = max(flt(self.amount) - flt(self.get("card_surcharge")), 0)

        self.affects_cash_drawer = 1 if self.payment_method == "Cash" else 0

        if not self.status:
            self.status = "Draft"

    def validate(self):
        if self.direction not in {"In", "Out"}:
            frappe.throw(_("Direction must be In or Out."))

        if flt(self.amount) <= TOLERANCE:
            frappe.throw(_("Amount must be greater than zero."))

        shift = validate_cashier_shift(
            cashier_shift=self.cashier_shift,
            company=self.company,
            settlement_location=self.settlement_location,
            cashier=self.cashier,
            require_open=(self.docstatus == 0),
        )

        self.cashier = shift.cashier
        self.settlement_location = shift.settlement_location
        self.cash_register = shift.cash_register

    def before_submit(self):
        validate_cashier_shift(
            cashier_shift=self.cashier_shift,
            company=self.company,
            settlement_location=self.settlement_location,
            cashier=self.cashier,
            require_open=True,
        )
        self.status = "Posted"

    def on_submit(self):
        refresh_shift_totals(self.cashier_shift)

    def before_cancel(self):
        validate_cashier_shift(
            cashier_shift=self.cashier_shift,
            company=self.company,
            settlement_location=self.settlement_location,
            cashier=self.cashier,
            require_open=True,
        )

    def on_cancel(self):
        self.db_set("status", "Cancelled", update_modified=False)
        refresh_shift_totals(self.cashier_shift)
