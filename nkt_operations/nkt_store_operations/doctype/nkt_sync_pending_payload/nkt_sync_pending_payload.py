from __future__ import annotations

import json
import re
import uuid

import frappe
from frappe.model.document import Document

from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import canonical_payload_hash

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMMUTABLE = ("event_uuid","event_family","payload_sha256","payload_json")


def _valid_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except Exception:
        return False


class NKTSyncPendingPayload(Document):
    def validate(self):
        if not _valid_uuid(self.event_uuid):
            frappe.throw("Event UUID must be a valid UUID.")
        digest=str(self.payload_sha256 or "").lower()
        if not _SHA256_RE.fullmatch(digest):
            frappe.throw("Payload SHA-256 must be 64 lowercase hexadecimal characters.")

        try:
            payload=json.loads(self.payload_json or "")
        except Exception as exc:
            raise frappe.ValidationError("Pending payload JSON is invalid.") from exc

        if canonical_payload_hash(payload) != digest:
            frappe.throw("Pending payload hash does not match payload JSON.")

        old=None if self.is_new() else self.get_doc_before_save()
        if old:
            changed=[f for f in IMMUTABLE if (old.get(f) or None)!=(self.get(f) or None)]
            if changed:
                frappe.throw("Immutable pending payload cannot be changed: "+", ".join(changed))
