from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class NKTSupplierCommercialItem(Document):
    def validate(self):
        self.commercial_description = (self.commercial_description or "").strip()
        if not self.commercial_description:
            frappe.throw(_("Supplier SOA / Commercial Item Description is required."))

        existing = frappe.db.exists(
            "NKT Supplier Commercial Item",
            {
                "supplier": self.supplier,
                "item_code": self.item_code,
                "name": ["!=", self.name or ""],
            },
        )
        if existing:
            frappe.throw(_(
                "A restricted Supplier Commercial Item mapping already exists "
                "for Supplier {0} and Item {1}: {2}"
            ).format(self.supplier, self.item_code, existing))
