# Copyright (c) 2026, NKT Grains Trading
# C10G Admin Transfer Reconciliation / Discrepancy Review
from __future__ import annotations

import math

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import make_autoname
from frappe.utils import cint, flt, getdate, now_datetime, today

OPEN = "Open"
UNDER_REVIEW = "Under Review"
RESOLVED = "Resolved"
ALLOWED_REVIEW_STATUSES = {OPEN, UNDER_REVIEW, RESOLVED}
ALLOWED_TRANSFER_STATUSES = {"In Transit", "Partially Arrived", "Completed", "Discrepancy"}
ISSUE_TYPES = {"Short on Arrival", "Damaged in Transit", "Wet in Transit", "Busted Packaging", "Other"}
RESPONSIBILITY_VALUES = {
    "Pending Investigation", "Warehouse Source", "Warehouse Destination",
    "Internal Trucking", "NKT Internal", "Other",
}
REVIEW_ROLES = {"NKT ADMINISTRATOR", "NKT OWNER"}
TOL = 0.0000001


def _is_reviewer() -> bool:
    if frappe.session.user == "Administrator":
        return True
    return bool(REVIEW_ROLES.intersection(set(frappe.get_roles())))


def _require_reviewer():
    if not _is_reviewer():
        frappe.throw(
            _("Only NKT Admin/Owner may perform Internal Transfer discrepancy reconciliation."),
            frappe.PermissionError,
        )


class NKTWarehouseTransferDiscrepancy(Document):
    """Factual internal-transfer discrepancy + later Admin/Owner audit review.

    C10G does NOT change stock, release/arrival Stock Entries, Supplier/Trucker
    payables, payroll, or sales. Warehouse remains the factual recorder.
    Admin/Owner may only review/classify the existing facts and resolve the
    discrepancy record.
    """

    def autoname(self):
        self.name = make_autoname("NKT-WTD-.#####")

    def before_insert(self):
        self._validate_creator_role()
        self.reported_by = frappe.session.user
        self.reported_at = now_datetime()
        self.status = OPEN

    def validate(self):
        before = self.get_doc_before_save()
        self._validate_review_lifecycle(before)
        self._validate_discrepancy_date()
        transfer_rows = self._load_transfer_context()
        self._validate_items(transfer_rows, before)
        self._validate_immutable_audit_fields(before)
        self._validate_factual_history_boundary(before)

    def _validate_creator_role(self):
        if frappe.session.user == "Administrator":
            return
        if "NKT Warehouse" not in frappe.get_roles():
            frappe.throw(
                _("Only NKT Warehouse may create an Internal Transfer Discrepancy record."),
                frappe.PermissionError,
            )

    def _validate_review_lifecycle(self, before):
        status = (self.status or OPEN).strip()
        if status not in ALLOWED_REVIEW_STATUSES:
            frappe.throw(_("Invalid Internal Transfer Discrepancy review status."))
        self.status = status

        if not before:
            if status != OPEN:
                frappe.throw(_("A new Internal Transfer Discrepancy must start Open."))
            for fieldname in ("reviewed_by", "reviewed_at", "resolution_notes"):
                if self.get(fieldname):
                    frappe.throw(
                        _("{0} is server-owned Admin review data.").format(
                            self.meta.get_label(fieldname) or fieldname
                        )
                    )
            return

        old_status = (before.status or OPEN).strip()
        reviewer = _is_reviewer()

        # C10G.1 hardening: Resolved is a final audit state. The UI already
        # disables Save, but the backend must independently block API/server
        # edits too. Transition INTO Resolved still works because before.status
        # is Under Review during that save.
        if old_status == RESOLVED:
            frappe.throw(
                _("A Resolved Internal Transfer discrepancy is audit-locked and cannot be edited.")
            )

        # Warehouse can maintain factual content while the record is Open only.
        if not reviewer:
            if old_status != OPEN or status != OPEN:
                frappe.throw(
                    _("Warehouse factual editing is locked once Admin/Owner review begins."),
                    frappe.PermissionError,
                )
            for fieldname in ("reviewed_by", "reviewed_at", "resolution_notes"):
                if self.get(fieldname) != before.get(fieldname):
                    frappe.throw(
                        _("{0} is reserved for Admin/Owner review.").format(
                            self.meta.get_label(fieldname) or fieldname
                        ),
                        frappe.PermissionError,
                    )
            return

        # Admin/Owner review is strictly forward-only.
        allowed = {
            (OPEN, OPEN),
            (OPEN, UNDER_REVIEW),
            (UNDER_REVIEW, UNDER_REVIEW),
            (UNDER_REVIEW, RESOLVED),
            (RESOLVED, RESOLVED),
        }
        if (old_status, status) not in allowed:
            frappe.throw(
                _("Internal Transfer discrepancy review cannot move from {0} to {1}.").format(
                    frappe.bold(old_status), frappe.bold(status)
                )
            )

        if status == OPEN:
            # Admin/Owner may inspect Open records, but must Start Review before classification.
            if self.reviewed_by or self.reviewed_at or self.resolution_notes:
                frappe.throw(_("Start Admin Review before entering review/resolution data."))
            return

        # Server owns reviewer identity/time from first transition into review.
        if old_status == OPEN and status == UNDER_REVIEW:
            self.reviewed_by = frappe.session.user
            self.reviewed_at = now_datetime()
        else:
            self.reviewed_by = before.reviewed_by
            self.reviewed_at = before.reviewed_at

        if status == RESOLVED:
            if not (self.resolution_notes or "").strip():
                frappe.throw(_("Resolution Notes are required before resolving an Internal Transfer discrepancy."))

    def _validate_discrepancy_date(self):
        if not self.discrepancy_date:
            frappe.throw(_("Discrepancy / Business Date is required."))
        d = getdate(self.discrepancy_date)
        current = getdate(today())
        if d > current:
            frappe.throw(_("Discrepancy / Business Date cannot be in the future."))
        before = self.get_doc_before_save()
        if self.flags.in_insert:
            if d != current:
                frappe.throw(_("Frontline Internal Transfer Discrepancy records must use the current live business date."))
        elif before and getdate(before.discrepancy_date) != d:
            frappe.throw(_("Discrepancy / Business Date is historical audit identity and cannot be changed after creation."))

    def _load_transfer_context(self):
        if not self.warehouse_transfer:
            frappe.throw(_("Warehouse Transfer is required."))
        transfer = frappe.db.get_value(
            "NKT Warehouse Transfer",
            self.warehouse_transfer,
            ["company", "transfer_date", "internal_transfer_no", "source_warehouse", "destination_warehouse", "status", "outgoing_stock_entry"],
            as_dict=True,
        )
        if not transfer:
            frappe.throw(_("Warehouse Transfer {0} does not exist.").format(frappe.bold(self.warehouse_transfer)))
        if transfer.status not in ALLOWED_TRANSFER_STATUSES:
            frappe.throw(
                _("Warehouse Transfer {0} status {1} cannot accept a physical discrepancy record.").format(
                    frappe.bold(self.warehouse_transfer), frappe.bold(transfer.status or "(blank)")
                )
            )
        if not transfer.outgoing_stock_entry:
            frappe.throw(_("Warehouse Transfer must have a physical source Release before a discrepancy can be recorded."))

        self.company = transfer.company
        self.internal_transfer_no = transfer.internal_transfer_no or self.warehouse_transfer
        self.source_warehouse = transfer.source_warehouse
        self.destination_warehouse = transfer.destination_warehouse
        self.outgoing_stock_entry = transfer.outgoing_stock_entry

        rows = frappe.get_all(
            "NKT Warehouse Transfer Item",
            filters={"parent": self.warehouse_transfer, "parenttype": "NKT Warehouse Transfer", "parentfield": "items"},
            fields=["name", "item_code", "item_name", "uom", "released_qty"],
            order_by="idx asc",
            limit_page_length=500,
        )
        return {r.item_code: r for r in rows}

    def _validate_items(self, transfer_rows, before):
        if not self.items:
            frappe.throw(_("At least one discrepancy item is required."))

        reviewer = _is_reviewer()
        seen = set()
        for row in self.items:
            if not row.item_code:
                frappe.throw(_("Row {0}: Item is required.").format(row.idx))
            if row.item_code not in transfer_rows:
                frappe.throw(
                    _("Row {0}: Item {1} is not part of Warehouse Transfer {2}.").format(
                        row.idx, frappe.bold(row.item_code), frappe.bold(self.warehouse_transfer)
                    )
                )
            if row.issue_type not in ISSUE_TYPES:
                frappe.throw(_("Row {0}: a valid Issue Type is required.").format(row.idx))
            key = (row.item_code, row.issue_type)
            if key in seen:
                frappe.throw(
                    _("Row {0}: duplicate {1} issue for item {2}. Use one row per item/issue type in this discrepancy record.").format(
                        row.idx, frappe.bold(row.issue_type), frappe.bold(row.item_code)
                    )
                )
            seen.add(key)

            tr = transfer_rows[row.item_code]
            row.transfer_item_row = tr.name
            row.item_name = tr.item_name or frappe.db.get_value("Item", row.item_code, "item_name")
            row.uom = tr.uom or frappe.db.get_value("Item", row.item_code, "stock_uom")

            qty = flt(row.discrepancy_qty)
            released = flt(tr.released_qty)
            if not math.isfinite(qty) or qty <= 0:
                frappe.throw(_("Row {0}: Discrepancy Qty must be greater than zero.").format(row.idx))
            if qty - released > TOL:
                frappe.throw(
                    _("Row {0}: Discrepancy Qty {1} cannot exceed released quantity {2} for item {3}.").format(
                        row.idx, qty, released, frappe.bold(row.item_code)
                    )
                )
            if not row.uom:
                frappe.throw(_("Row {0}: transfer item has no UOM.").format(row.idx))
            must_whole = cint(frappe.db.get_value("UOM", row.uom, "must_be_whole_number") or 0)
            if must_whole and abs(qty - round(qty)) > TOL:
                frappe.throw(
                    _("Row {0}: Discrepancy Qty must be a whole number for UOM {1}.").format(
                        row.idx, frappe.bold(row.uom)
                    )
                )

            responsibility = (row.responsibility or "Pending Investigation").strip()
            if responsibility not in RESPONSIBILITY_VALUES:
                frappe.throw(_("Row {0}: invalid Responsibility value.").format(row.idx))

            if not before or self.status == OPEN:
                if responsibility != "Pending Investigation":
                    frappe.throw(
                        _("Row {0}: Responsibility remains Pending Investigation until Admin/Owner starts review.").format(row.idx)
                    )
                row.responsibility = "Pending Investigation"
            else:
                if not reviewer:
                    frappe.throw(_("Only Admin/Owner may classify discrepancy responsibility."), frappe.PermissionError)

            if row.issue_type == "Other" and not (row.operational_notes or "").strip():
                frappe.throw(_("Row {0}: Operational / Condition Notes are required for Other discrepancy.").format(row.idx))

        if before and self.status == RESOLVED:
            pending = [r.idx for r in self.items if (r.responsibility or "").strip() == "Pending Investigation"]
            if pending:
                frappe.throw(
                    _("Resolve requires a Responsibility classification for every discrepancy row. Pending rows: {0}.").format(
                        ", ".join(str(x) for x in pending)
                    )
                )

    def _validate_immutable_audit_fields(self, before):
        if not before:
            return
        for fieldname in (
            "warehouse_transfer", "company", "internal_transfer_no", "source_warehouse", "destination_warehouse",
            "outgoing_stock_entry", "reported_by", "reported_at",
        ):
            if self.get(fieldname) != before.get(fieldname):
                frappe.throw(
                    _("{0} is audit-owned and cannot be changed after discrepancy creation.").format(
                        self.meta.get_label(fieldname) or fieldname
                    )
                )

    def _validate_factual_history_boundary(self, before):
        if not before or not _is_reviewer():
            return

        # Admin/Owner review may not rewrite the factual record.
        factual_header = ("discrepancy_date", "evidence", "notes")
        for fieldname in factual_header:
            if self.get(fieldname) != before.get(fieldname):
                frappe.throw(
                    _("Admin/Owner review cannot modify factual field {0}.").format(
                        self.meta.get_label(fieldname) or fieldname
                    )
                )

        old_rows = {r.name: r for r in before.items}
        new_rows = {r.name: r for r in self.items if r.name}
        if set(old_rows) != set(new_rows):
            frappe.throw(_("Admin/Owner review cannot add or remove factual discrepancy rows."))

        factual_child_fields = (
            "transfer_item_row", "item_code", "item_name", "uom",
            "issue_type", "discrepancy_qty", "operational_notes",
        )
        for name, old in old_rows.items():
            new = new_rows[name]
            for fieldname in factual_child_fields:
                if new.get(fieldname) != old.get(fieldname):
                    frappe.throw(
                        _("Admin/Owner review cannot modify factual discrepancy row {0} field {1}.").format(
                            old.idx, fieldname
                        )
                    )


@frappe.whitelist()
def start_admin_review(name: str):
    _require_reviewer()
    doc = frappe.get_doc("NKT Warehouse Transfer Discrepancy", name)
    if doc.status != OPEN:
        frappe.throw(_("Only an Open discrepancy can start Admin Review."))
    doc.status = UNDER_REVIEW
    doc.save()
    return {
        "name": doc.name,
        "status": doc.status,
        "reviewed_by": doc.reviewed_by,
        "reviewed_at": doc.reviewed_at,
    }


@frappe.whitelist()
def resolve_admin_review(name: str):
    _require_reviewer()
    doc = frappe.get_doc("NKT Warehouse Transfer Discrepancy", name)
    if doc.status != UNDER_REVIEW:
        frappe.throw(_("Only a discrepancy Under Review can be resolved."))
    doc.status = RESOLVED
    doc.save()
    return {
        "name": doc.name,
        "status": doc.status,
        "reviewed_by": doc.reviewed_by,
        "reviewed_at": doc.reviewed_at,
    }
