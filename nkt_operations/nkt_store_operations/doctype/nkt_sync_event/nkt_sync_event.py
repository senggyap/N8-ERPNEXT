from __future__ import annotations

import hashlib
import re
import uuid

import frappe
from frappe.model.document import Document
from frappe.utils import getdate, get_datetime, now

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

IMMUTABLE_FIELDS = (
    "event_uuid",
    "event_family",
    "event_action",
    "operational_context",
    "origin_device",
    "origin_user",
    "business_date",
    "settled_at",
    "client_created_at",
    "payload_sha256",
    "legacy_request_id",
)


def _valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except Exception:
        return False


class NKTSyncEvent(Document):
    def before_insert(self):
        if not self.server_received_at:
            self.server_received_at = now()
        if not self.sync_state:
            self.sync_state = "Received"

    def validate(self):
        if not _valid_uuid(self.event_uuid):
            frappe.throw("Event UUID must be a valid UUID.")
        if not _SHA256_RE.fullmatch(str(self.payload_sha256 or "").lower()):
            frappe.throw("Canonical Payload SHA-256 must be exactly 64 lowercase hexadecimal characters.")
        if self.primary_ack_uuid and not _valid_uuid(self.primary_ack_uuid):
            frappe.throw("Primary ACK UUID must be a valid UUID when present.")

        settled = get_datetime(self.settled_at)
        if getdate(self.business_date) != settled.date():
            frappe.throw("Business Date must match the Asia/Manila business date of Business / Settled Time.")

        old = None if self.is_new() else self.get_doc_before_save()
        if not old:
            return

        changed = [
            field for field in IMMUTABLE_FIELDS
            if (old.get(field) or None) != (self.get(field) or None)
        ]
        if changed:
            frappe.throw("Immutable sync-event identity cannot be changed: " + ", ".join(changed))

        old_ack = str(old.get("primary_ack_uuid") or "").strip()
        new_ack = str(self.get("primary_ack_uuid") or "").strip()
        if old_ack and old_ack != new_ack:
            frappe.throw("Primary ACK UUID is immutable once bound to the sync event.")
