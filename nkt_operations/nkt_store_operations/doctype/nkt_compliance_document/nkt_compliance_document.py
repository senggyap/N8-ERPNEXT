from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, getdate, now_datetime, today


TRACKED_FIELDS = (
    "document_number",
    "issue_date",
    "expiry_date",
    "renewal_due_date",
    "document_file",
    "record_state",
)


def calculate_compliance_status(doc, as_of_date=None):
    as_of = getdate(as_of_date or today())
    record_state = (doc.get("record_state") or "Active").strip()

    if record_state == "Superseded":
        return {
            "status": "Superseded",
            "status_as_of": as_of,
            "days_to_due": None,
            "reference_date": None,
        }

    if record_state == "Cancelled":
        return {
            "status": "Cancelled",
            "status_as_of": as_of,
            "days_to_due": None,
            "reference_date": None,
        }

    reference_date = doc.get("renewal_due_date") or doc.get("expiry_date")
    if not reference_date:
        return {
            "status": "No Expiry",
            "status_as_of": as_of,
            "days_to_due": None,
            "reference_date": None,
        }

    due = getdate(reference_date)
    days = (due - as_of).days
    reminder_days = max(cint(doc.get("reminder_days_before") or 0), 0)

    if days < 0:
        status = "Expired"
    elif days <= reminder_days:
        status = "Expiring Soon"
    else:
        status = "Active"

    return {
        "status": status,
        "status_as_of": as_of,
        "days_to_due": days,
        "reference_date": due,
    }


class NKTComplianceDocument(Document):
    def before_validate(self):
        self._apply_defaults_and_validate_dates()
        self._apply_status()

    def before_save(self):
        if not self.is_new():
            self._capture_controlled_change_history()

    def _apply_defaults_and_validate_dates(self):
        if not self.record_state:
            self.record_state = "Active"

        self.reminder_days_before = max(cint(self.reminder_days_before or 0), 0)

        if self.expiry_date and not self.renewal_due_date:
            self.renewal_due_date = self.expiry_date

        if self.issue_date and self.expiry_date:
            if getdate(self.issue_date) > getdate(self.expiry_date):
                frappe.throw(_("Issue Date cannot be after Expiry Date."))

        if self.renewal_due_date and self.expiry_date:
            if getdate(self.renewal_due_date) > getdate(self.expiry_date):
                frappe.throw(_("Renewal Due Date cannot be after Expiry Date."))

        if self.branch and self.company:
            branch_company = frappe.db.get_value("Branch", self.branch, "company")
            if branch_company and branch_company != self.company:
                frappe.throw(_("Selected Branch does not belong to Company {0}.").format(self.company))

        if self.warehouse and self.company:
            warehouse_company = frappe.db.get_value("Warehouse", self.warehouse, "company")
            if warehouse_company and warehouse_company != self.company:
                frappe.throw(_("Selected Warehouse / Site does not belong to Company {0}.").format(self.company))

    def _apply_status(self):
        status = calculate_compliance_status(self)
        self.compliance_status = status["status"]
        self.status_as_of = status["status_as_of"]
        self.days_to_due = status["days_to_due"]

    def _capture_controlled_change_history(self):
        old = self.get_doc_before_save()
        if not old:
            return

        changes = {}
        for fieldname in TRACKED_FIELDS:
            previous = old.get(fieldname)
            new = self.get(fieldname)
            if self._normalized(previous) != self._normalized(new):
                changes[fieldname] = {
                    "previous": previous,
                    "new": new,
                }

        if not changes:
            self.change_note = None
            return

        note = (self.change_note or "").strip()
        if not note:
            labels = ", ".join(
                self.meta.get_label(fieldname) or fieldname
                for fieldname in changes
            )
            frappe.throw(
                _(
                    "Renewal / Change Note is required because controlled document fields changed: {0}."
                ).format(labels)
            )

        change_type = self._classify_change(changes)

        self.append(
            "renewal_history",
            {
                "changed_on": now_datetime(),
                "changed_by": frappe.session.user,
                "change_type": change_type,
                "change_note": note,
                "previous_document_number": old.document_number,
                "new_document_number": self.document_number,
                "previous_issue_date": old.issue_date,
                "new_issue_date": self.issue_date,
                "previous_expiry_date": old.expiry_date,
                "new_expiry_date": self.expiry_date,
                "previous_renewal_due_date": old.renewal_due_date,
                "new_renewal_due_date": self.renewal_due_date,
                "previous_document_file": old.document_file,
                "new_document_file": self.document_file,
                "previous_record_state": old.record_state,
                "new_record_state": self.record_state,
            },
        )

        # The permanent note lives in the appended history row.
        self.change_note = None

    @staticmethod
    def _classify_change(changes):
        if set(changes) == {"record_state"}:
            return "Lifecycle Change"

        renewal_fields = {
            "document_number",
            "expiry_date",
            "renewal_due_date",
            "document_file",
        }
        if set(changes) & renewal_fields:
            return "Renewal"

        return "Correction / Update"

    @staticmethod
    def _normalized(value):
        if value is None:
            return ""
        return str(value).strip()
