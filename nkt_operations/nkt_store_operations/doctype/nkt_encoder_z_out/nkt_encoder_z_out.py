import hashlib
import json

import frappe
from frappe.model.document import Document

from nkt_operations.nkt_store_operations.features.cashier.encoder_zout import (
    finalize_zout_document,
    validate_zout_document,
)


class NKTEncoderZOut(Document):
    def before_insert(self):
        if not self.encoder:
            import frappe
            self.encoder = frappe.session.user

    def validate(self):
        validate_zout_document(self)
        if self.docstatus == 0:
            self.status = "Draft"

    def before_submit(self):
        if getattr(self.flags, "nkt_c15c10k_preserved_offline_zout", False):
            validate_zout_document(self)
            try:
                data = json.loads(str(self.snapshot_json or ""))
            except Exception as exc:
                raise frappe.ValidationError(
                    "Preserved official offline Z-Out snapshot is invalid."
                ) from exc
            canonical = json.dumps(
                data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
            )
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if digest != str(self.snapshot_hash or "").lower():
                frappe.throw("Preserved official offline Z-Out snapshot hash mismatch.")
            self.snapshot_json = canonical
            self.snapshot_hash = digest
            self.status = "Finalized"
            return

        finalize_zout_document(self)
        self.status = "Finalized"

    def on_cancel(self):
        self.db_set("status", "Cancelled", update_modified=False)
