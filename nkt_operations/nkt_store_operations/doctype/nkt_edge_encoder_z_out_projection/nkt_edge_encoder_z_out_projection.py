from __future__ import annotations
import hashlib
import json
import uuid
import frappe
from frappe.model.document import Document

IMMUTABLE_FIELDS = (
    "edge_zout_uuid", "event_uuid", "company", "encoder", "business_date",
    "start_datetime", "effective_end_datetime", "finalized_on",
    "snapshot_json", "snapshot_sha256",
)

class NKTEdgeEncoderZOutProjection(Document):
    def validate(self):
        for field in ("edge_zout_uuid", "event_uuid"):
            try:
                self.set(field, str(uuid.UUID(str(self.get(field)))))
            except Exception as exc:
                raise frappe.ValidationError(f"{field} is invalid.") from exc

        try:
            parsed = json.loads(str(self.snapshot_json or ""))
        except Exception as exc:
            raise frappe.ValidationError("Official Z-Out Snapshot must contain valid JSON.") from exc
        canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if digest != str(self.snapshot_sha256 or "").lower():
            frappe.throw("Official Z-Out Snapshot hash mismatch.")
        self.snapshot_json = canonical
        self.snapshot_sha256 = digest

        if self.sync_state not in ("Pending Edge", "Primary Preserved"):
            frappe.throw("Official offline Z-Out sync state is invalid.")

        old = None if self.is_new() else self.get_doc_before_save()
        if old:
            changed = [
                f for f in IMMUTABLE_FIELDS
                if (old.get(f) or None) != (self.get(f) or None)
            ]
            if changed:
                frappe.throw(
                    "Official offline Encoder Z-Out snapshot is immutable after finalization: "
                    + ", ".join(changed)
                )
