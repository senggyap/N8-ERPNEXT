from __future__ import annotations

import uuid

import frappe
from frappe.model.document import Document
from frappe.utils import now


POLICY_FIELDS = ("status", "assigned_user", "operational_context", "device_class")


def _valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except Exception:
        return False


class NKTDeviceRegistry(Document):
    def before_insert(self):
        if not self.registered_at:
            self.registered_at = now()
        if not self.registered_by:
            self.registered_by = frappe.session.user or "Administrator"
        if not self.policy_version:
            self.policy_version = 1

    def validate(self):
        if not self.is_new():
            previous_status = frappe.db.get_value(self.doctype, self.name, "status")
            terminal_statuses = {"Revoked", "Lost/Stolen", "Retired"}
            if previous_status in terminal_statuses and self.status not in terminal_statuses:
                frappe.throw(
                    "Terminal device identity cannot be restored or re-trusted. Enroll recovered or replacement hardware with a new device identity.",
                    frappe.PermissionError,
                )
        if not _valid_uuid(self.device_id):
            frappe.throw("Device ID must be a valid UUID.")

        old = None if self.is_new() else self.get_doc_before_save()
        if not old:
            return

        if old.device_id != self.device_id:
            frappe.throw("Device ID is immutable after registration.")

        changed = any((old.get(field) or None) != (self.get(field) or None) for field in POLICY_FIELDS)
        if changed:
            self.policy_version = max(int(old.policy_version or 1) + 1, 2)

        if old.status != self.status:
            actor = frappe.session.user or "Administrator"
            timestamp = now()
            if self.status == "Restricted":
                self.restricted_at = timestamp
                self.restricted_by = actor
            elif self.status in ("Revoked", "Lost/Stolen"):
                self.revoked_at = timestamp
                self.revoked_by = actor
