import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class NKTCustomerAdvance(Document):
    # NKT_C15F_R6C_SUBMITTED_CUSTOMER_ADVANCE_MONEY_IMMUTABILITY
    # Submitted monetary state is maintained only by audited NKT application/reversal machinery.
    # Frappe calls this hook specifically for an update to an already-submitted document.
    def before_update_after_submit(self):
        stored = frappe.db.get_value(
            "NKT Customer Advance",
            self.name,
            [
                "original_advance_amount",
                "applied_amount",
                "available_advance_amount",
            ],
            as_dict=True,
        )
        if not stored:
            frappe.throw(_("The submitted Customer Advance no longer exists."))

        changed = []
        labels = {
            "original_advance_amount": _("Original Advance Amount"),
            "applied_amount": _("Applied Amount"),
            "available_advance_amount": _("Available Advance Amount"),
        }
        for fieldname in labels:
            if abs(flt(self.get(fieldname)) - flt(stored.get(fieldname))) > 0.005:
                changed.append(labels[fieldname])

        if changed:
            frappe.throw(
                _(
                    "Submitted Customer Advance monetary state is system-controlled. "
                    "Use the audited Customer Advance Application / reversal workflow; "
                    "direct post-submit edits are not allowed. Changed field(s): {0}."
                ).format(", ".join(changed))
            )

    def before_validate(self):
        if not self.posting_datetime:
            self.posting_datetime = now_datetime()

        self.calculate_balance_and_status()

    def validate(self):
        self.validate_amounts()
        self.validate_source_receipt()

    def before_submit(self):
        self.calculate_balance_and_status()

    def before_cancel(self):
        if flt(self.applied_amount) > 0.005:
            frappe.throw(
                _(
                    "This customer advance has already been used. "
                    "It cannot be cancelled."
                )
            )

        self.advance_status = "Cancelled"

    def calculate_balance_and_status(self):
        original_amount = flt(self.original_advance_amount)
        applied_amount = flt(self.applied_amount)

        self.available_advance_amount = max(
            original_amount - applied_amount,
            0,
        )

        if self.docstatus == 2:
            self.advance_status = "Cancelled"
        elif self.available_advance_amount <= 0.005:
            self.advance_status = "Fully Used"
        elif applied_amount > 0.005:
            self.advance_status = "Partially Used"
        else:
            self.advance_status = "Available"

    def validate_amounts(self):
        original_amount = flt(self.original_advance_amount)
        applied_amount = flt(self.applied_amount)

        if original_amount <= 0:
            frappe.throw(
                _("Original Advance Amount must be greater than zero.")
            )

        if applied_amount < 0:
            frappe.throw(
                _("Applied Amount cannot be negative.")
            )

        if applied_amount > original_amount + 0.005:
            frappe.throw(
                _(
                    "Applied Amount cannot exceed the "
                    "Original Advance Amount."
                )
            )

    def validate_source_receipt(self):
        # C7.8.5 RETURN CREDIT EARLY EXIT
        if self.get("custom_nkt_credit_origin") == "Return Credit":
            source_return = self.get("custom_nkt_source_return_exchange")
            if not source_return:
                frappe.throw(_("NKT Source Return / Exchange is required for Return Credit."))
            if not frappe.db.exists("NKT Return Exchange Declaration", source_return):
                frappe.throw(_("The NKT Source Return / Exchange does not exist."))
            self.source_payment_receipt = None
            return
        if self.get("custom_nkt_credit_origin") == "Return Credit":
            if not self.get("custom_nkt_source_return_exchange"):
                frappe.throw(_("NKT Source Return / Exchange is required for Return Credit."))
        elif self.get("custom_nkt_credit_origin") != "Return Credit" and (not self.source_payment_receipt):
            frappe.throw(_("Source Payment Receipt is required."))

        receipt = frappe.db.get_value(
            "NKT Payment Receipt",
            self.source_payment_receipt,
            [
                "docstatus",
                "company",
                "customer",
                "customer_advance_amount",
            ],
            as_dict=True,
        )

        if self.get("custom_nkt_credit_origin") != "Return Credit" and (not receipt):
            frappe.throw(
                _("The Source Payment Receipt does not exist.")
            )

        if receipt.docstatus != 1:
            frappe.throw(
                _("The Source Payment Receipt must be submitted.")
            )

        if receipt.company != self.company:
            frappe.throw(
                _("The receipt belongs to a different company.")
            )

        if receipt.customer != self.customer:
            frappe.throw(
                _("The receipt belongs to a different customer.")
            )

        if abs(
            flt(receipt.customer_advance_amount)
            - flt(self.original_advance_amount)
        ) > 0.005:
            frappe.throw(
                _(
                    "The advance amount does not match the "
                    "approved overpayment."
                )
            )