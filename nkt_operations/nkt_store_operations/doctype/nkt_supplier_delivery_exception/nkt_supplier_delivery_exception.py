from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

TOL = 0.000001
ALLOWED_ROLES = {"NKT Purchasing", "NKT ADMINISTRATOR", "NKT OWNER", "System Manager"}


def _has_role():
    if frappe.session.user == "Administrator":
        return True
    return bool(ALLOWED_ROLES.intersection(set(frappe.get_roles(frappe.session.user))))


class NKTSupplierDeliveryException(Document):
    def validate(self):
        if not _has_role():
            frappe.throw(_("You are not authorized to manage supplier delivery claims."), frappe.PermissionError)

        if not self.supplier_receiving:
            frappe.throw(_("NKT Supplier Receiving is required."))

        receiving = frappe.db.get_value(
            "NKT Supplier Receiving",
            self.supplier_receiving,
            [
                "company", "supplier", "purchase_order", "bill_of_lading_no",
                "supplier_dr_no", "supplier_delivery_reference", "delivery_vehicle",
                "plate_number", "internal_vehicle_no", "vehicle_operator",
                "underlying_purchase_receipt",
            ],
            as_dict=True,
        )
        if not receiving:
            frappe.throw(_("Linked NKT Supplier Receiving does not exist."))

        # Identity is server-owned from the receiving event.
        self.company = receiving.company
        self.supplier = receiving.supplier
        self.purchase_order = receiving.purchase_order
        self.bill_of_lading_no = receiving.bill_of_lading_no
        self.supplier_dr_no = receiving.supplier_dr_no
        self.other_supplier_reference = receiving.supplier_delivery_reference
        self.delivery_vehicle = receiving.delivery_vehicle
        self.plate_number = receiving.plate_number
        self.internal_vehicle_no = receiving.internal_vehicle_no
        self.vehicle_operator = receiving.vehicle_operator
        self.receiving_posting_reference = receiving.underlying_purchase_receipt

        if not self.items:
            frappe.throw(_("At least one physical exception item is required."))

        seen = set()
        total_claimed = 0.0
        total_agreed = 0.0
        for row in self.items:
            key = (row.purchase_order_item, row.issue_type)
            if key in seen:
                frappe.throw(_("Duplicate exception type {0} for the same Purchase Order item.").format(row.issue_type))
            seen.add(key)

            if flt(row.issue_qty) <= TOL:
                frappe.throw(_("Issue Qty must be greater than zero."))
            if flt(row.supplier_claimable_qty) < -TOL or flt(row.supplier_claimable_qty) - flt(row.issue_qty) > TOL:
                frappe.throw(_("Supplier Claimable Qty must be between zero and Issue Qty."))

            if row.responsibility in ("Trucker", "NKT / Internal") and flt(row.supplier_claimable_qty) > TOL:
                frappe.throw(_(
                    "Supplier Claimable Qty must be zero when responsibility is {0}. "
                    "Trucker/NKT liability is separate from the supplier claim."
                ).format(row.responsibility))

            total_claimed += flt(row.claimed_amount)
            total_agreed += flt(row.agreed_deduction_amount)

        self.gross_claim_amount = total_claimed
        self.agreed_supplier_deduction_amount = total_agreed
