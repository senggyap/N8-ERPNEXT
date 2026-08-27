from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from nkt_operations.nkt_store_operations.features.oil.controls import (
    require_owner_admin,
)

TOLERANCE = 0.000001


class NKTOilOperationsSettings(Document):
    def validate(self):
        require_owner_admin()

        if abs(flt(self.nominal_kg_per_container) - 17.0) > TOLERANCE:
            frappe.throw(_("Nominal Palm Oil content is locked at exactly 17 Kg per container."))

        if self.company and self.combined_bulk_warehouse:
            wh = frappe.db.get_value(
                "Warehouse",
                self.combined_bulk_warehouse,
                ["company", "is_group", "disabled"],
                as_dict=True,
            )
            if not wh:
                frappe.throw(_("Combined bulk Warehouse does not exist."))
            if str(wh.company or "") != str(self.company or ""):
                frappe.throw(_("Combined bulk Warehouse belongs to another Company."))
            if int(wh.is_group or 0):
                frappe.throw(_("Combined bulk Warehouse must be a leaf Warehouse."))
            if int(wh.disabled or 0):
                frappe.throw(_("Combined bulk Warehouse is disabled."))

        self._validate_item(self.palm_olein_item, expected_uom="Kg", label="Palm Olein")
        self._validate_item(self.empty_container_item, expected_uom="Nos", label="Empty Container")
        self._validate_item(self.finished_palm_oil_item, expected_uom="Nos", label="Finished Palm Oil")

        if self.finished_palm_oil_item:
            weight, uom = frappe.db.get_value(
                "Item",
                self.finished_palm_oil_item,
                ["weight_per_unit", "weight_uom"],
            ) or (0, None)
            if abs(flt(weight) - 17.0) > TOLERANCE or str(uom or "") != "Kg":
                frappe.throw(
                    _("Finished Palm Oil Item must have Weight Per Unit = 17 and Weight UOM = Kg.")
                )

    def _validate_item(self, item_code, *, expected_uom, label):
        if not item_code:
            return
        row = frappe.db.get_value(
            "Item",
            item_code,
            ["stock_uom", "is_stock_item", "disabled"],
            as_dict=True,
        )
        if not row:
            frappe.throw(_("{0} Item does not exist.").format(label))
        if not int(row.is_stock_item or 0):
            frappe.throw(_("{0} must be a stock Item.").format(label))
        if int(row.disabled or 0):
            frappe.throw(_("{0} Item is disabled.").format(label))
        if str(row.stock_uom or "") != expected_uom:
            frappe.throw(
                _("{0} Stock UOM must be {1}.").format(label, expected_uom)
            )
