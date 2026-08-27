from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

from nkt_operations.nkt_store_operations.features.oil.controls import (
    get_oil_settings,
    require_owner_admin,
)

TOLERANCE = 0.000001
CHECKLIST_FIELDS = (
    "all_tanker_scale_receipts_entered",
    "all_daily_repack_reports_entered",
    "spillage_recovery_complete",
    "physical_combined_tanks_empty",
    "no_unencoded_paperwork",
    "owner_admin_final_confirmation",
)


class NKTOilTankClose(Document):
    def before_validate(self):
        require_owner_admin()
        settings = get_oil_settings()
        self.company = settings.company
        self.combined_bulk_warehouse = settings.combined_bulk_warehouse
        self.palm_olein_item = settings.palm_olein_item
        if not self.status:
            self.status = "Draft"

    def validate(self):
        require_owner_admin()
        self._require_checklist()

    def before_submit(self):
        require_owner_admin()
        self._require_checklist()
        self.tank_empty_datetime = now_datetime()
        self.closed_by = frappe.session.user
        self._calculate_current_variance()

    def on_submit(self):
        require_owner_admin()
        self._calculate_current_variance()
        recon = None
        if abs(flt(self.book_qty_before_close)) > TOLERANCE:
            recon = self._create_zero_reconciliation()
        self.db_set(
            {
                "stock_reconciliation": recon.name if recon else None,
                "status": "Closed",
            },
            update_modified=False,
        )

    def _require_checklist(self):
        missing = [field for field in CHECKLIST_FIELDS if not int(self.get(field) or 0)]
        if missing:
            frappe.throw(
                _("Complete every Tank Empty checklist confirmation before closing.")
            )

    def _bulk_qty(self):
        return flt(
            frappe.db.get_value(
                "Bin",
                {
                    "item_code": self.palm_olein_item,
                    "warehouse": self.combined_bulk_warehouse,
                },
                "actual_qty",
            )
        )

    def _calculate_current_variance(self):
        book = self._bulk_qty()
        self.book_qty_before_close = book
        if book > TOLERANCE:
            self.variance_type = "Shortage"
            self.variance_qty_kg = abs(book)
        elif book < -TOLERANCE:
            self.variance_type = "Surplus"
            self.variance_qty_kg = abs(book)
        else:
            self.variance_type = "Balanced"
            self.variance_qty_kg = 0.0

    def _create_zero_reconciliation(self):
        valuation_rate = flt(
            frappe.db.get_value(
                "Bin",
                {
                    "item_code": self.palm_olein_item,
                    "warehouse": self.combined_bulk_warehouse,
                },
                "valuation_rate",
            )
        )
        when = self.tank_empty_datetime or now_datetime()
        doc = frappe.get_doc(
            {
                "doctype": "Stock Reconciliation",
                "company": self.company,
                "posting_date": when.date().isoformat(),
                "posting_time": when.time().strftime("%H:%M:%S"),
                "set_posting_time": 1,
                "purpose": "Stock Reconciliation",
                "items": [
                    {
                        "item_code": self.palm_olein_item,
                        "warehouse": self.combined_bulk_warehouse,
                        "qty": 0,
                        "valuation_rate": valuation_rate,
                    }
                ],
            }
        )
        if doc.meta.has_field("remarks"):
            doc.remarks = (
                f"NKT Oil Tank Empty Close {self.name}. "
                f"System book before close: {flt(self.book_qty_before_close):.6f} Kg; "
                f"variance: {self.variance_type} {flt(self.variance_qty_kg):.6f} Kg. "
                "Physical combined tanks confirmed empty by Owner/Admin checklist."
            )
        doc.insert(ignore_permissions=True)
        doc.submit()
        return doc
