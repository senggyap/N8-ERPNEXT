from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, now, nowdate

from nkt_operations.nkt_store_operations.doctype.nkt_supplier_payment.nkt_supplier_payment import (
    _get_supplier_payment_advance_balance,
    _get_supplier_soa_operational_balance,
    _payment_counts_as_released,
)

TOL = 0.000001
MANAGEMENT_ROLES = {"NKT OWNER", "NKT ADMINISTRATOR", "Administrator"}


def _require_management():
    if frappe.session.user == "Administrator":
        return
    if not set(frappe.get_roles(frappe.session.user)).intersection(MANAGEMENT_ROLES):
        frappe.throw(
            _("Only NKT Owner / Administrator may apply a released Supplier Advance."),
            frappe.PermissionError,
        )


class NKTSupplierAdvanceApplication(Document):
    def before_validate(self):
        _require_management()
        self._load_payment_identity()
        self._calculate_and_validate()

    def validate(self):
        _require_management()
        self._load_payment_identity()
        self._calculate_and_validate()

    def before_submit(self):
        _require_management()
        self._load_payment_identity()
        self._calculate_and_validate()
        self.applied_by = frappe.session.user
        self.applied_at = now()

    def on_submit(self):
        payment = frappe.get_doc("NKT Supplier Payment", self.supplier_payment)
        payment.add_comment(
            "Info",
            _(
                "Supplier advance application {0} posted for {1}. "
                "Applied now: {2}; remaining unapplied advance: {3}."
            ).format(
                self.name,
                self.supplier,
                frappe.format_value(self.total_applied, {"fieldtype": "Currency"}),
                frappe.format_value(self.remaining_advance_after, {"fieldtype": "Currency"}),
            ),
        )
        for row in self.allocations:
            soa = frappe.get_doc("NKT Supplier SOA", row.supplier_soa)
            soa.add_comment(
                "Info",
                _(
                    "Supplier advance {0} from Payment {1} applied: {2}."
                ).format(
                    self.name,
                    self.supplier_payment,
                    frappe.format_value(row.allocated_amount, {"fieldtype": "Currency"}),
                ),
            )

    def on_cancel(self):
        _require_management()
        payment = frappe.get_doc("NKT Supplier Payment", self.supplier_payment)
        payment.add_comment(
            "Info",
            _("Supplier advance application {0} cancelled; its statement allocations no longer reduce outstanding.").format(self.name),
        )

    def on_trash(self):
        if self.docstatus != 0:
            frappe.throw(_("Only a Draft Supplier Advance Application may be deleted."))

    def _load_payment_identity(self):
        if not self.supplier_payment:
            return
        payment = frappe.get_doc("NKT Supplier Payment", self.supplier_payment)
        self.company = payment.company
        self.supplier = payment.supplier
        self.original_payment_amount = flt(payment.payment_amount)

    def _calculate_and_validate(self):
        if not self.supplier_payment:
            frappe.throw(_("Released Supplier Advance Payment is required."))

        payment = frappe.get_doc("NKT Supplier Payment", self.supplier_payment)

        if not _payment_counts_as_released(payment):
            frappe.throw(
                _("Supplier Payment {0} must be an operationally Released payment before its unapplied amount can be used as an advance.").format(payment.name)
            )

        if not self.application_date:
            frappe.throw(_("Application Date is required."))
        if getdate(self.application_date) > getdate(nowdate()):
            frappe.throw(_("Application Date cannot be in the future."))
        if getdate(self.application_date) < getdate(payment.payment_date):
            frappe.throw(_("Advance Application Date cannot be before the original Supplier Payment date."))

        exclude = self.name if (self.name and not self.is_new()) else None
        advance = _get_supplier_payment_advance_balance(
            payment.name,
            exclude_advance_application=exclude,
        )

        self.original_payment_amount = advance.payment_amount
        self.original_unallocated_advance = advance.original_unallocated_advance
        self.previously_applied_advance = advance.applied_later_amount
        self.available_advance_before = advance.available_advance_balance

        if flt(self.available_advance_before) <= TOL:
            frappe.throw(_("Supplier Payment {0} has no remaining unapplied advance.").format(payment.name))

        if not self.allocations:
            frappe.throw(_("Choose at least one finalized Supplier SOA to receive the advance."))

        seen = set()
        total = 0.0
        for row in self.allocations:
            if row.supplier_soa in seen:
                frappe.throw(_("Supplier SOA {0} is listed more than once.").format(row.supplier_soa))
            seen.add(row.supplier_soa)

            soa = frappe.db.get_value(
                "NKT Supplier SOA",
                row.supplier_soa,
                ["company", "supplier", "statement_date", "net_payable", "status"],
                as_dict=True,
            )
            if not soa:
                frappe.throw(_("Supplier SOA {0} does not exist.").format(row.supplier_soa))
            if soa.company != payment.company or soa.supplier != payment.supplier:
                frappe.throw(_("Supplier SOA {0} does not belong to the same Company/Supplier as the advance.").format(row.supplier_soa))
            if soa.status != "Finalized":
                frappe.throw(_("Supplier SOA {0} must be Finalized before advance application.").format(row.supplier_soa))

            balance = _get_supplier_soa_operational_balance(
                row.supplier_soa,
                exclude_advance_application=exclude,
            )
            amount = flt(row.allocated_amount)
            if amount <= TOL:
                frappe.throw(_("Advance Amount Applied must be greater than zero on every row."))
            if amount - flt(balance.available_to_allocate) > TOL:
                frappe.throw(
                    _("Supplier SOA {0} has only {1} available to allocate.").format(
                        row.supplier_soa,
                        frappe.format_value(balance.available_to_allocate, {"fieldtype": "Currency"}),
                    )
                )

            row.soa_statement_date = soa.statement_date
            row.soa_net_payable = soa.net_payable
            row.available_before = balance.available_to_allocate
            total += amount

        if total - flt(self.available_advance_before) > TOL:
            frappe.throw(
                _("This application exceeds the remaining Supplier Advance by {0}.").format(
                    frappe.format_value(total - flt(self.available_advance_before), {"fieldtype": "Currency"})
                )
            )

        self.total_applied = total
        self.remaining_advance_after = max(flt(self.available_advance_before) - total, 0)
