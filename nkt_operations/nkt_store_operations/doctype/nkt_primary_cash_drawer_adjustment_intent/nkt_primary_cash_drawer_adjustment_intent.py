from __future__ import annotations

import re
import uuid

import frappe
from frappe.model.document import Document
from frappe.utils import flt

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMMUTABLE_FIELDS = (
    "event_uuid","event_family","event_action","origin_device","origin_user",
    "operational_context","business_date","settled_at","client_created_at",
    "cashier_shift","company","settlement_location","cashier","adjustment_type",
    "direction","amount","party_name","purpose","supporting_document",
    "denomination_total","denominations_json","envelope_sha256","payload_sha256",
    "canonical_envelope_json","canonical_payload_json","preservation_state",
)


class NKTPrimaryCashDrawerAdjustmentIntent(Document):
    def validate(self):
        try:
            self.event_uuid = str(uuid.UUID(str(self.event_uuid)))
        except Exception as exc:
            raise frappe.ValidationError("Cash Drawer Adjustment Intent UUID is invalid.") from exc

        for field in ("envelope_sha256","payload_sha256"):
            value = str(self.get(field) or "").lower()
            if not _SHA256_RE.fullmatch(value):
                frappe.throw(f"{field} must be 64 lowercase hexadecimal characters.")
            self.set(field, value)

        if self.adjustment_type not in (
            "Petty Cash Release","Petty Cash Return","Cash Drop",
            "Advance / Mid-Shift Deposit","Other Cash In","Other Cash Out",
        ):
            frappe.throw("Cash Drawer Adjustment Intent type is invalid.")
        if self.direction not in ("In","Out"):
            frappe.throw("Cash Drawer Adjustment Intent direction is invalid.")
        if flt(self.amount) <= 0:
            frappe.throw("Cash Drawer Adjustment Intent amount must be greater than zero.")
        if self.preservation_state != "Preserved":
            frappe.throw("Cash Drawer Adjustment Intent preservation state is invalid.")
        if self.downstream_state not in (
            "Awaiting Cash Drawer Materialization",
            "Cash Drawer Materialized",
            "Materialization Conflict",
        ):
            frappe.throw("Cash Drawer Adjustment Intent downstream state is invalid.")

        old = None if self.is_new() else self.get_doc_before_save()
        if old:
            changed = [
                field for field in IMMUTABLE_FIELDS
                if (old.get(field) or None) != (self.get(field) or None)
            ]
            if changed:
                frappe.throw(
                    "Immutable Primary Cash Drawer Adjustment Intent cannot be changed: "
                    + ", ".join(changed)
                )
