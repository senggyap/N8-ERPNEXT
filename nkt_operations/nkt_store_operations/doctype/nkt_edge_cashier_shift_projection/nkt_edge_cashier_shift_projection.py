from __future__ import annotations
import json
import uuid
import frappe
from frappe.model.document import Document

IMMUTABLE_IDENTITY_FIELDS = (
    "shift_reference", "edge_shift_uuid", "primary_shift_name", "source_kind",
    "company", "settlement_location", "cashier", "shift_business_date",
    "shift_start", "opening_cash", "open_event_uuid",
)

class NKTEdgeCashierShiftProjection(Document):
    def validate(self):
        try:
            self.edge_shift_uuid = str(uuid.UUID(str(self.edge_shift_uuid)))
        except Exception as exc:
            raise frappe.ValidationError("Edge Shift UUID is invalid.") from exc

        if self.source_kind not in ("Opened Offline at Edge", "Primary Shift Adopted at Edge"):
            frappe.throw("Store Edge Cashier Shift source kind is invalid.")
        if self.local_status not in ("Open", "Closed"):
            frappe.throw("Store Edge Cashier Shift local status is invalid.")

        for field in ("denominations_json", "provisional_summary_json"):
            if self.get(field):
                try:
                    parsed = json.loads(str(self.get(field)))
                except Exception as exc:
                    raise frappe.ValidationError(f"{field} must contain valid JSON.") from exc
                self.set(field, json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False))

        old = None if self.is_new() else self.get_doc_before_save()
        if old:
            changed = [
                f for f in IMMUTABLE_IDENTITY_FIELDS
                if (old.get(f) or None) != (self.get(f) or None)
            ]
            if changed:
                frappe.throw(
                    "Immutable Store Edge Cashier Shift identity cannot be changed: "
                    + ", ".join(changed)
                )
            if old.local_status == "Closed" and self.local_status != "Closed":
                frappe.throw("A physically closed Store Edge Cashier Shift cannot be reopened locally.")
            if old.close_event_uuid and self.close_event_uuid != old.close_event_uuid:
                frappe.throw("Cashier Shift Close Event UUID is immutable after physical close.")
