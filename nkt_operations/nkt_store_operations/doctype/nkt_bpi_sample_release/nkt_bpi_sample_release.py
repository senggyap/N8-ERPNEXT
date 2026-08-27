from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, now, nowdate

TOL = 0.000001


class NKTBPISampleRelease(Document):
    def validate(self):
        if getdate(self.sample_date) != getdate(nowdate()):
            frappe.throw(_("BPI Sample Date must be the current business date."))

        if flt(self.physical_sample_qty) <= TOL:
            frappe.throw(_("Physical Sample Qty Released must be greater than zero."))

        item = frappe.db.get_value(
            "Item", self.item_code, ["stock_uom", "disabled"], as_dict=True
        )
        if not item or item.disabled:
            frappe.throw(_("Select an active Item."))
        self.sample_uom = item.stock_uom

        wh = frappe.db.get_value(
            "Warehouse", self.warehouse, ["company", "is_group", "disabled"], as_dict=True
        )
        if not wh or wh.disabled or wh.is_group:
            frappe.throw(_("Select an active non-group Warehouse."))
        if wh.company != self.company:
            frappe.throw(_("Warehouse must belong to Company {0}.").format(self.company))

        if self.source_supplier_receiving:
            src = frappe.db.get_value(
                "NKT Supplier Receiving",
                self.source_supplier_receiving,
                ["supplier", "company", "purchase_order"],
                as_dict=True,
            )
            if not src:
                frappe.throw(_("Linked Supplier Arrival does not exist."))
            if src.supplier != self.supplier:
                frappe.throw(_("BPI Sample Supplier must match the linked Supplier Arrival."))
            if src.company != self.company:
                frappe.throw(_("BPI Sample Company must match the linked Supplier Arrival."))
            self.purchase_order = src.purchase_order

        # Financial responsibility is a management-side quantity decision.
        # It is intentionally separate from the physical sample release.
        chargeable = flt(self.supplier_chargeable_qty)
        physical = flt(self.physical_sample_qty)

        if chargeable < -TOL:
            frappe.throw(_("Qty Chargeable to Supplier cannot be negative."))
        if chargeable - physical > TOL:
            frappe.throw(_("Qty Chargeable to Supplier cannot exceed Physical Sample Qty Released."))

        self.nkt_absorbed_qty = max(physical - chargeable, 0)

        if chargeable <= TOL:
            if self.management_notes or self.get_db_value("supplier_chargeable_qty"):
                self.charge_status = "Not Chargeable"
            else:
                self.charge_status = "Pending Management Decision"
        elif abs(chargeable - physical) <= TOL:
            self.charge_status = "Fully Chargeable"
        else:
            self.charge_status = "Partially Chargeable"

        if self.is_new():
            self.status = "Draft"

    def before_save(self):
        if not self.released_by:
            self.released_by = frappe.session.user
        if not self.released_at:
            self.released_at = now()

        if self.charge_status == "Pending Management Decision":
            self.status = "Physical Release Recorded"
        else:
            self.status = "Charge Decision Recorded"


    def before_submit(self):
        # Production physical release: full Physical Sample Qty leaves stock.
        # Supplier-chargeable quantity does not control stock deduction.
        ste = _create_underlying_stock_issue(self)
        self.underlying_stock_entry = ste.name
        self.stock_posting_status = "Posted"

    def before_cancel(self):
        frappe.throw(_(
            "Posted BPI / Regulatory Sample Release cannot be cancelled directly. "
            "Use a controlled stock correction/reversal process."
        ))

    def before_update_after_submit(self):
        # Physical identity is immutable after Submit because only the restricted
        # management decision fields are allow_on_submit. Management may decide
        # later whether 0%, part, or all of the physical sample is supplier-borne.
        if not self.underlying_stock_entry or self.stock_posting_status != "Posted":
            frappe.throw(_(
                "Submitted BPI Sample Release must remain linked to its posted Stock Issue."
            ))


def _get_bpi_issue_account_and_cost_center(sample):
    """
    Resolve the standard ERPNext Material Issue accounting defaults without
    creating a dedicated BPI GL account yet.

    Preference:
      1. Item Default expense account / buying cost center for Company
      2. Company Stock Adjustment Account / Cost Center
    """
    item_default = frappe.db.get_value(
        "Item Default",
        {"parent": sample.item_code, "company": sample.company},
        ["expense_account", "buying_cost_center"],
        as_dict=True,
    ) or frappe._dict()

    company = frappe.db.get_value(
        "Company",
        sample.company,
        ["stock_adjustment_account", "cost_center"],
        as_dict=True,
    ) or frappe._dict()

    expense_account = item_default.get("expense_account") or company.get("stock_adjustment_account")
    cost_center = item_default.get("buying_cost_center") or company.get("cost_center")

    if not expense_account:
        frappe.throw(_(
            "No standard Material Issue Difference/Expense Account is configured for "
            "Item {0} / Company {1}. C9E.2 will not invent an accounting account."
        ).format(sample.item_code, sample.company))

    return expense_account, cost_center


def _create_underlying_stock_issue(sample):
    """
    PRIVATE C9E.2 BPI physical-release bridge.

    Deducts the FULL physical sample quantity from the selected releasing warehouse.
    Supplier-chargeable quantity is deliberately irrelevant to stock deduction.

    Production BPI stock posting remains locked until rollback evidence is reviewed.
    """
    if not sample.name:
        frappe.throw(_("Save the BPI Sample Release before stock posting."))

    sample.run_method("validate")

    if sample.underlying_stock_entry:
        existing = frappe.db.get_value(
            "Stock Entry",
            sample.underlying_stock_entry,
            ["name", "docstatus"],
            as_dict=True,
        )
        if existing and int(existing.docstatus or 0) == 1:
            return frappe.get_doc("Stock Entry", existing.name)
        frappe.throw(_(
            "BPI Sample Release already references Stock Entry {0}, but it is not a "
            "live submitted stock issue."
        ).format(sample.underlying_stock_entry))

    item = frappe.db.get_value(
        "Item",
        sample.item_code,
        ["has_serial_no", "has_batch_no", "stock_uom"],
        as_dict=True,
    )
    if not item:
        frappe.throw(_("Item {0} does not exist.").format(sample.item_code))

    # Rice/sample flow is not serial/batch-controlled today. Do not silently post
    # incomplete serial/batch logic.
    if item.has_serial_no or item.has_batch_no:
        frappe.throw(_(
            "C9E.2 BPI stock posting does not yet support serial/batch-controlled Item {0}."
        ).format(sample.item_code))

    expense_account, cost_center = _get_bpi_issue_account_and_cost_center(sample)

    original_user = frappe.session.user
    try:
        frappe.set_user("Administrator")

        ste = frappe.new_doc("Stock Entry")
        ste.company = sample.company
        if ste.meta.has_field("stock_entry_type"):
            ste.stock_entry_type = "Material Issue"
        ste.purpose = "Material Issue"
        ste.posting_date = sample.sample_date
        if ste.meta.has_field("set_posting_time"):
            ste.set_posting_time = 1
        if ste.meta.has_field("posting_time"):
            from frappe.utils import nowtime
            ste.posting_time = nowtime()
        ste.remarks = (
            "BPI / Regulatory Sample physical release for {0}. "
            "Physical Qty: {1}; Supplier Chargeable Qty: {2}; NKT Absorbed Qty: {3}."
        ).format(
            sample.name,
            flt(sample.physical_sample_qty),
            flt(sample.supplier_chargeable_qty),
            flt(sample.nkt_absorbed_qty),
        )

        row = ste.append("items", {})
        row.item_code = sample.item_code
        row.s_warehouse = sample.warehouse
        row.qty = flt(sample.physical_sample_qty)
        row.uom = sample.sample_uom or item.stock_uom
        row.stock_uom = item.stock_uom
        row.conversion_factor = 1
        row.expense_account = expense_account
        if cost_center:
            row.cost_center = cost_center

        ste.insert()
        ste.submit()

        sample.db_set("underlying_stock_entry", ste.name, update_modified=False)
        sample.db_set("stock_posting_status", "Posted", update_modified=False)
        sample.db_set("stock_posted_by", original_user, update_modified=False)
        sample.db_set("stock_posted_at", now(), update_modified=False)

        sample.underlying_stock_entry = ste.name
        sample.stock_posting_status = "Posted"
        sample.stock_posted_by = original_user
        sample.stock_posted_at = now()

        return ste
    finally:
        frappe.set_user(original_user)
