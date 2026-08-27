from __future__ import annotations

import uuid

import frappe
from frappe.model.document import Document
from frappe.utils import flt

IMMUTABLE_FIELDS = (
    "event_uuid","cashier_shift","company","settlement_location","cashier",
    "adjustment_type","direction","amount","signed_cash_effect","business_date",
)


class NKTEdgeCashDrawerAdjustmentProjection(Document):
    def validate(self):
        try:
            self.event_uuid = str(uuid.UUID(str(self.event_uuid)))
        except Exception as exc:
            raise frappe.ValidationError("Event UUID must be a valid UUID.") from exc

        if self.adjustment_type not in (
            "Petty Cash Release","Petty Cash Return","Cash Drop",
            "Advance / Mid-Shift Deposit","Other Cash In","Other Cash Out",
        ):
            frappe.throw("Edge cash-drawer projection adjustment type is invalid.")
        if self.direction not in ("In","Out"):
            frappe.throw("Edge cash-drawer projection direction is invalid.")
        if flt(self.amount) <= 0:
            frappe.throw("Edge cash-drawer projection amount must be greater than zero.")
        expected = flt(self.amount) if self.direction == "In" else -flt(self.amount)
        if abs(flt(self.signed_cash_effect) - expected) > 0.000001:
            frappe.throw("Edge cash-drawer projection signed effect is invalid.")
        if self.projection_state not in (
            "Pending Edge","Awaiting Primary","Primary Preserved","Primary Cash Materialized","Finalized"
        ):
            frappe.throw("Edge cash-drawer projection state is invalid.")
        if self.projection_state in ("Primary Cash Materialized","Finalized"):
            if (
                not self.materialization_ack_uuid
                or not self.materialized_adjustment
                or not self.materialized_movement
                or not self.primary_materialized_at
            ):
                frappe.throw(
                    "Primary cash materialization binding is required for this projection state."
                )
        if self.projection_state == "Finalized" and not self.finalized_at:
            frappe.throw("Finalized At is required for a finalized projection.")

        old = None if self.is_new() else self.get_doc_before_save()
        if old:
            changed = [
                field for field in IMMUTABLE_FIELDS
                if (old.get(field) or None) != (self.get(field) or None)
            ]
            if changed:
                frappe.throw(
                    "Immutable Edge cash-drawer projection cannot be changed: "
                    + ", ".join(changed)
                )
