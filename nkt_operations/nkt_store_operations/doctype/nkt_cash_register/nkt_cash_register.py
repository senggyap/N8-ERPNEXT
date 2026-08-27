import frappe
from frappe import _
from frappe.model.document import Document


class NKTCashRegister(Document):
    def validate(self):
        warehouse = frappe.db.get_value(
            "Warehouse",
            self.settlement_location,
            ["is_group", "disabled", "company"],
            as_dict=True,
        )

        if not warehouse:
            frappe.throw(_("Settlement Location was not found."))
        if warehouse.is_group:
            frappe.throw(_("Settlement Location must be a leaf warehouse."))
        if warehouse.disabled:
            frappe.throw(_("Settlement Location is disabled."))
        if warehouse.company != self.company:
            frappe.throw(_("Settlement Location belongs to another company."))
