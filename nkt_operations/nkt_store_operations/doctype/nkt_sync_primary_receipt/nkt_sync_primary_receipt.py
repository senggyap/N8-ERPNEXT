from __future__ import annotations

import re
import uuid

import frappe
from frappe.model.document import Document

_SHA256_RE=re.compile(r"^[0-9a-f]{64}$")
IMMUTABLE=("event_uuid","event_family","primary_ack_uuid","envelope_sha256","payload_sha256","result_code","canonical_doctype","canonical_name","materialization_state")


def _uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError("Receipt UUID value is invalid.") from exc


class NKTSyncPrimaryReceipt(Document):
    def validate(self):
        self.event_uuid=_uuid(self.event_uuid)
        self.primary_ack_uuid=_uuid(self.primary_ack_uuid)

        for field in ("envelope_sha256","payload_sha256"):
            value=str(self.get(field) or "").lower()
            if not _SHA256_RE.fullmatch(value):
                frappe.throw(f"{field} must be 64 lowercase hexadecimal characters.")
            self.set(field,value)

        if self.result_code != "Committed":
            frappe.throw("Primary sync receipt result must be Committed.")

        if self.materialization_state not in (
            "Technical Receipt Only",
            "Canonical Draft Materialized",
            "Tender Intent Preserved",
            "Encoder Settlement Intent Preserved",
            "Warehouse Release Intent Preserved",
            "Cash Drawer Adjustment Intent Preserved",
            "Cash Drawer Adjustment Intent Preserved",
            "Warehouse Transfer Dispatch Intent Preserved",
            "Warehouse Transfer Arrival Intent Preserved",
            "Supplier Receiving Intent Preserved",
            "Return Exchange Intent Preserved",
            "Physical Inventory Count Intent Preserved",
            "Cashier Shift Open Intent Preserved",
            "Cashier Shift Close Intent Preserved",
            "Encoder Z-Out Finalization Intent Preserved",
            "Trucking Trip Lifecycle Intent Preserved",
        ):
            frappe.throw("Primary receipt materialization state is invalid.")

        if self.materialization_state in (
            "Canonical Draft Materialized",
            "Tender Intent Preserved",
            "Encoder Settlement Intent Preserved",
            "Warehouse Release Intent Preserved",
            "Warehouse Transfer Dispatch Intent Preserved",
            "Warehouse Transfer Arrival Intent Preserved",
            "Supplier Receiving Intent Preserved",
            "Return Exchange Intent Preserved",
            "Physical Inventory Count Intent Preserved",
            "Cashier Shift Open Intent Preserved",
            "Cashier Shift Close Intent Preserved",
            "Encoder Z-Out Finalization Intent Preserved",
            "Trucking Trip Lifecycle Intent Preserved",
        ):
            if not str(self.canonical_doctype or "").strip():
                frappe.throw("Canonical DocType is required for a preserved/materialized receipt.")
            if not str(self.canonical_name or "").strip():
                frappe.throw("Canonical Name is required for a preserved/materialized receipt.")

        old=None if self.is_new() else self.get_doc_before_save()
        if old:
            changed=[f for f in IMMUTABLE if (old.get(f) or None)!=(self.get(f) or None)]
            if changed:
                frappe.throw("Immutable Primary receipt cannot be changed: "+", ".join(changed))
