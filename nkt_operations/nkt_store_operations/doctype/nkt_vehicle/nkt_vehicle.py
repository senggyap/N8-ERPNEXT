from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document


def normalize_plate(value):
    value = (value or "").strip().upper()
    value = re.sub(r"\s+", " ", value)
    return value


class NKTVehicle(Document):
    def validate(self):
        self.plate_number = normalize_plate(self.plate_number)
        self.internal_vehicle_no = (self.internal_vehicle_no or "").strip() or None
        self.operator_name = (self.operator_name or "").strip() or None

        if not self.plate_number:
            frappe.throw(_("Plate Number is required."))

        existing = frappe.db.exists("NKT Vehicle", {"plate_number": self.plate_number})
        if existing and existing != self.name:
            frappe.throw(_("Plate Number {0} already exists as NKT Vehicle {1}.").format(
                self.plate_number, existing
            ))
