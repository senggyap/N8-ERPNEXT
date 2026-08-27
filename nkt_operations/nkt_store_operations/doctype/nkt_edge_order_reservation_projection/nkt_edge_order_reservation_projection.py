from __future__ import annotations

import re
import uuid

import frappe
from frappe.model.document import Document

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMMUTABLE_FIELDS = (
    "reservation_key",
    "event_uuid",
    "line_no",
    "item_code",
    "warehouse",
    "reserved_qty",
    "business_date",
)


def _valid_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except Exception:
        return False


class NKTEdgeOrderReservationProjection(Document):
    def validate(self):
        if not _SHA256_RE.fullmatch(str(self.reservation_key or "").lower()):
            frappe.throw("Reservation Key must be a SHA-256 value.")
        if not _valid_uuid(self.event_uuid):
            frappe.throw("Event UUID must be a valid UUID.")
        if int(self.line_no or 0) < 1:
            frappe.throw("Line No. must be one or greater.")
        if not str(self.item_code or "").strip():
            frappe.throw("Item Code is required.")
        if not str(self.warehouse or "").strip():
            frappe.throw("Warehouse is required.")
        if float(self.reserved_qty or 0) <= 0:
            frappe.throw("Reserved Qty must be greater than zero.")
        if self.projection_state not in ("Pending Edge", "Awaiting Primary"):
            frappe.throw("Projection State is invalid.")

        old = None if self.is_new() else self.get_doc_before_save()
        if old:
            changed = [
                field for field in IMMUTABLE_FIELDS
                if (old.get(field) or None) != (self.get(field) or None)
            ]
            if changed:
                frappe.throw(
                    "Immutable Edge order reservation projection cannot be changed: "
                    + ", ".join(changed)
                )
