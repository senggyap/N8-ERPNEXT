from __future__ import annotations

import json
import re
import uuid

import frappe
from frappe.model.document import Document

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMMUTABLE_FIELDS = (
    "event_uuid",
    "event_family",
    "event_action",
    "submit_request_id",
    "origin_device",
    "origin_user",
    "operational_context",
    "company",
    "warehouse",
    "business_date",
    "settled_at",
    "count_datetime",
    "client_created_at",
    "counted_by",
    "entry_role",
    "count_reason",
    "physical_count_reference",
    "operator_notes",
    "item_count",
    "envelope_sha256",
    "payload_sha256",
    "canonical_envelope_json",
    "canonical_payload_json",
    "preservation_state",
    "primary_ack_uuid",
    "primary_preserved_at",
)


def _uuid(value, label):
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


class NKTPrimaryPhysicalInventoryCountIntent(Document):
    def validate(self):
        self.event_uuid = _uuid(self.event_uuid, "Event UUID")
        self.primary_ack_uuid = _uuid(self.primary_ack_uuid, "Primary ACK UUID")

        if self.event_family != "NKT Physical Inventory Count Intent":
            frappe.throw("Primary Physical Inventory journal family is invalid.")
        if self.event_action != "record_physical_inventory_count":
            frappe.throw("Primary Physical Inventory journal action is invalid.")
        if self.preservation_state != "Preserved":
            frappe.throw("Primary Physical Inventory intent must remain Preserved.")
        if self.downstream_state not in (
            "Awaiting Physical Inventory Reconciliation",
            "Materialized",
            "Conflict",
        ):
            frappe.throw("Primary Physical Inventory downstream state is invalid.")

        if self.materialization_decision not in (
            "Pending",
            "Posted",
            "No Variance",
            "Fresh Recount Required",
            "Manual Primary Review Required",
        ):
            frappe.throw("Primary Physical Inventory materialization decision is invalid.")

        if self.downstream_state == "Materialized" and not self.materialized_adjustment:
            frappe.throw(
                "Materialized Primary Physical Inventory intent must link its NKT Physical Inventory Adjustment."
            )

        for field in ("envelope_sha256", "payload_sha256"):
            value = str(self.get(field) or "").lower()
            if not _SHA256_RE.fullmatch(value):
                frappe.throw(f"{field} must be 64 lowercase hexadecimal characters.")
            self.set(field, value)

        for field in ("canonical_envelope_json", "canonical_payload_json"):
            try:
                parsed = json.loads(str(self.get(field) or ""))
            except Exception as exc:
                raise frappe.ValidationError(f"{field} must contain valid JSON.") from exc
            self.set(
                field,
                json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            )

        old = None if self.is_new() else self.get_doc_before_save()
        if old:
            changed = [
                field for field in IMMUTABLE_FIELDS
                if (old.get(field) or None) != (self.get(field) or None)
            ]
            if changed:
                frappe.throw(
                    "Immutable Primary Physical Inventory count intent cannot be changed: "
                    + ", ".join(changed)
                )
