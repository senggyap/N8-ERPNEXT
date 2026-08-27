from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import now


class NKTUserSecurityState(Document):
    def before_insert(self):
        if not self.policy_version:
            self.policy_version = 1

    def validate(self):
        if not self.user or not frappe.db.exists("User", self.user):
            frappe.throw("A valid User is required.")

        old = None if self.is_new() else self.get_doc_before_save()
        if old and old.user != self.user:
            frappe.throw("User is immutable after this security-state record is created.")

        actor = frappe.session.user or "Administrator"
        timestamp = now()

        if not old:
            if self.status == "Restricted":
                self.restricted_at = self.restricted_at or timestamp
                self.restricted_by = self.restricted_by or actor
            return

        if old.status != self.status:
            self.policy_version = max(int(old.policy_version or 1) + 1, 2)
            if self.status == "Restricted":
                self.restricted_at = timestamp
                self.restricted_by = actor
            elif self.status == "Active":
                self.restored_at = timestamp
                self.restored_by = actor
