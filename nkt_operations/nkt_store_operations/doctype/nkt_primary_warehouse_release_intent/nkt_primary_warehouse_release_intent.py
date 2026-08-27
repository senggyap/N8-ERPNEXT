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
    "warehouse_release","customer_order","company","customer","source_warehouse",
    "release_reference","driver_name","plate_number","total_release_quantity",
    "envelope_sha256","payload_sha256","canonical_envelope_json",
    "canonical_payload_json","preservation_state",
)


def _uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError("Warehouse Release Intent UUID is invalid.") from exc


class NKTPrimaryWarehouseReleaseIntent(Document):
    def validate(self):
        self.event_uuid = _uuid(self.event_uuid)
        for field in ("envelope_sha256","payload_sha256"):
            value = str(self.get(field) or "").lower()
            if not _SHA256_RE.fullmatch(value):
                frappe.throw(f"{field} must be 64 lowercase hexadecimal characters.")
            self.set(field, value)
        if flt(self.total_release_quantity) <= 0:
            frappe.throw("Total Release Quantity must be greater than zero.")
        if self.preservation_state != "Preserved":
            frappe.throw("Warehouse Release Intent preservation state is invalid.")
        if self.downstream_state not in (
            "Awaiting Physical Stock Materialization",
            "Physical Stock Materialized",
            "Materialization Conflict",
        ):
            frappe.throw("Warehouse Release Intent downstream state is invalid.")

        if self.downstream_state == "Physical Stock Materialized":
            for field in (
                "materialized_warehouse_release",
                "materialized_stock_entry",
                "materialization_ack_uuid",
                "materialization_ack_sha256",
                "materialization_ack_json",
            ):
                if not str(self.get(field) or "").strip():
                    frappe.throw(f"{field} is required after physical-stock materialization.")
            try:
                uuid.UUID(str(self.materialization_ack_uuid))
            except Exception as exc:
                raise frappe.ValidationError("Materialization ACK UUID is invalid.") from exc
            ack_hash = str(self.materialization_ack_sha256 or "").lower()
            if not _SHA256_RE.fullmatch(ack_hash):
                frappe.throw("Materialization ACK SHA-256 is invalid.")
            self.materialization_ack_sha256 = ack_hash

        old = None if self.is_new() else self.get_doc_before_save()
        if old:
            changed = [
                f for f in IMMUTABLE_FIELDS
                if (old.get(f) or None) != (self.get(f) or None)
            ]
            if changed:
                frappe.throw(
                    "Immutable Primary Warehouse Release Intent cannot be changed: "
                    + ", ".join(changed)
                )
