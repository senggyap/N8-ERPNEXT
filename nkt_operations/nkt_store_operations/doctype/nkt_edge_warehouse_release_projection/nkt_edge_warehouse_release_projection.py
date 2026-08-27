from __future__ import annotations

import re
import uuid

import frappe
from frappe.model.document import Document
from frappe.utils import flt

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMMUTABLE_FIELDS = (
    "projection_key",
    "event_uuid",
    "line_no",
    "warehouse_release",
    "warehouse_release_item",
    "customer_order",
    "item_code",
    "warehouse",
    "released_qty",
    "release_reference",
    "business_date",
)


def _valid_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except Exception:
        return False


class NKTEdgeWarehouseReleaseProjection(Document):
    def validate(self):
        self.projection_key = str(self.projection_key or "").lower()
        if not _SHA256_RE.fullmatch(self.projection_key):
            frappe.throw("Projection Key must be a SHA-256 value.")
        if not _valid_uuid(self.event_uuid):
            frappe.throw("Event UUID must be a valid UUID.")
        if int(self.line_no or 0) < 1:
            frappe.throw("Line No. must be one or greater.")
        for field in (
            "warehouse_release", "warehouse_release_item", "customer_order",
            "item_code", "warehouse", "release_reference",
        ):
            if not str(self.get(field) or "").strip():
                frappe.throw(f"{field} is required.")
        if flt(self.released_qty) <= 0:
            frappe.throw("Released Qty must be greater than zero.")
        if self.projection_state not in (
            "Pending Edge", "Awaiting Primary", "Primary Preserved",
            "Primary Stock Materialized", "Finalized"
        ):
            frappe.throw("Projection State is invalid.")

        if self.projection_state in ("Primary Stock Materialized", "Finalized"):
            if not _valid_uuid(self.materialization_ack_uuid):
                frappe.throw("Materialization ACK UUID must be a valid UUID.")
            ack_hash = str(self.materialization_ack_sha256 or "").lower()
            if not _SHA256_RE.fullmatch(ack_hash):
                frappe.throw("Materialization ACK SHA-256 is invalid.")
            if not str(self.primary_stock_entry or "").strip():
                frappe.throw("Primary Stock Entry is required after materialization.")
            if flt(self.primary_post_actual_qty) < 0:
                frappe.throw("Primary Post Actual Qty cannot be negative.")
            if not self.primary_materialized_at:
                frappe.throw("Primary Materialized At is required after materialization.")
        if self.projection_state == "Finalized" and not self.finalized_at:
            frappe.throw("Finalized At is required for a finalized Edge projection.")

        old = None if self.is_new() else self.get_doc_before_save()
        if old:
            changed = [
                field for field in IMMUTABLE_FIELDS
                if (old.get(field) or None) != (self.get(field) or None)
            ]
            if changed:
                frappe.throw(
                    "Immutable Edge warehouse-release projection cannot be changed: "
                    + ", ".join(changed)
                )
