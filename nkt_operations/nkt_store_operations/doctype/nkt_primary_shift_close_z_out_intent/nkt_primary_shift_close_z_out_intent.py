from __future__ import annotations
import json
import re
import uuid
import frappe
from frappe.model.document import Document

_SHA = re.compile(r"^[0-9a-f]{64}$")
IMMUTABLE_FIELDS = (
    "event_uuid", "event_family", "event_action", "edge_identity",
    "origin_device", "origin_user", "company", "actor",
    "event_business_date", "settled_at", "primary_shift_name",
    "envelope_sha256", "payload_sha256",
    "canonical_envelope_json", "canonical_payload_json",
    "preservation_state", "primary_ack_uuid", "primary_preserved_at",
)

class NKTPrimaryShiftCloseZOutIntent(Document):
    def validate(self):
        for field in ("event_uuid", "primary_ack_uuid"):
            try:
                self.set(field, str(uuid.UUID(str(self.get(field)))))
            except Exception as exc:
                raise frappe.ValidationError(f"{field} is invalid.") from exc

        if self.preservation_state != "Preserved":
            frappe.throw("Primary Shift Close / Z-Out intent must remain Preserved.")
        if self.materialization_state not in (
            "Pending Canonical Materialization",
            "Canonical Materialized",
            "Reconciliation Review Required",
        ):
            frappe.throw("Primary Shift Close / Z-Out materialization state is invalid.")
        if self.reconciliation_status not in (
            "Not Reconciled", "Matched", "Difference Found", "Review Required"
        ):
            frappe.throw("Primary Shift Close / Z-Out reconciliation status is invalid.")

        for field in ("envelope_sha256", "payload_sha256"):
            value = str(self.get(field) or "").lower()
            if not _SHA.fullmatch(value):
                frappe.throw(f"{field} must be 64 lowercase hexadecimal characters.")
            self.set(field, value)

        for field in ("canonical_envelope_json", "canonical_payload_json"):
            try:
                parsed = json.loads(str(self.get(field) or ""))
            except Exception as exc:
                raise frappe.ValidationError(f"{field} must contain valid JSON.") from exc
            self.set(field, json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False))

        old = None if self.is_new() else self.get_doc_before_save()
        if old:
            changed = [
                f for f in IMMUTABLE_FIELDS
                if (old.get(f) or None) != (self.get(f) or None)
            ]
            if changed:
                frappe.throw(
                    "Immutable Primary Shift Close / Z-Out preserved intent cannot be changed: "
                    + ", ".join(changed)
                )
