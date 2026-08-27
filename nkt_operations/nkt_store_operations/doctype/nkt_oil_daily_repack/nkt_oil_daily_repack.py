from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, nowdate

from nkt_operations.nkt_store_operations.features.oil.controls import (
    get_oil_settings,
    require_owner_admin,
)

TOLERANCE = 0.000001
TECHNICAL_DAILY_POSTING_TIME = "00:00:01"


class NKTOilDailyRepack(Document):
    def before_validate(self):
        require_owner_admin()
        self._apply_settings()
        self._calculate()
        if not self.encoded_by:
            self.encoded_by = frappe.session.user
        if not self.status:
            self.status = "Draft"

    def validate(self):
        require_owner_admin()
        self._validate_date()
        self._validate_quantities()
        self._validate_one_report_per_day()
        self._capture_pre_encoding_shortfall()

    def before_submit(self):
        require_owner_admin()
        self._apply_settings()
        self._calculate()
        self._validate_date()
        self._validate_quantities()
        self._validate_one_report_per_day()

    def on_submit(self):
        require_owner_admin()
        entry = self._create_repack_stock_entry()
        post_qty = self._bin_qty(self.finished_palm_oil_item)
        self.db_set(
            {
                "stock_entry": entry.name,
                "post_repack_finished_actual_qty": post_qty,
                "status": "Posted",
            },
            update_modified=False,
        )

    def _apply_settings(self):
        settings = get_oil_settings()
        self.company = settings.company
        self.combined_bulk_warehouse = settings.combined_bulk_warehouse
        self.palm_olein_item = settings.palm_olein_item
        self.empty_container_item = settings.empty_container_item
        self.finished_palm_oil_item = settings.finished_palm_oil_item
        self.nominal_kg_per_container = 17.0

    def _calculate(self):
        containers = int(self.finished_containers or 0)
        spillage = flt(self.reported_spillage_kg)
        self.nominal_finished_kg = flt(containers * 17.0)
        self.bulk_consumption_kg = flt(self.nominal_finished_kg + spillage)
        self.empty_containers_consumed = containers

    def _validate_date(self):
        if not self.repacking_date:
            frappe.throw(_("Physical Repacking Date is required."))
        if getdate(self.repacking_date) > getdate(nowdate()):
            frappe.throw(_("Physical Repacking Date cannot be in the future."))

    def _validate_quantities(self):
        if int(self.finished_containers or 0) <= 0:
            frappe.throw(_("Finished 17 Kg Containers must be greater than zero."))
        if flt(self.reported_spillage_kg) < -TOLERANCE:
            frappe.throw(_("Reported Spillage / Loss cannot be negative."))
        expected = int(self.finished_containers or 0) * 17.0 + flt(self.reported_spillage_kg)
        if abs(flt(self.bulk_consumption_kg) - expected) > TOLERANCE:
            frappe.throw(_("Palm Olein consumption calculation is inconsistent."))
        if int(self.empty_containers_consumed or 0) != int(self.finished_containers or 0):
            frappe.throw(_("Empty-container consumption must equal finished-container quantity."))

    def _validate_one_report_per_day(self):
        existing = frappe.get_all(
            self.doctype,
            filters={
                "company": self.company,
                "repacking_date": self.repacking_date,
                "docstatus": ["!=", 2],
                "name": ["!=", self.name or ""],
            },
            pluck="name",
            limit_page_length=2,
        )
        if existing:
            frappe.throw(
                _("A Daily Oil Repack report already exists for {0}: {1}. "
                  "Use one consolidated report for the day's physical repacking.")
                .format(self.repacking_date, existing[0])
            )

    def _bin_qty(self, item_code):
        return flt(
            frappe.db.get_value(
                "Bin",
                {"item_code": item_code, "warehouse": self.combined_bulk_warehouse},
                "actual_qty",
            )
        )

    def _capture_pre_encoding_shortfall(self):
        actual = self._bin_qty(self.finished_palm_oil_item)
        self.pre_encoding_finished_actual_qty = actual
        self.pre_encoding_release_shortfall_qty = max(-actual, 0.0)

    def _create_repack_stock_entry(self):
        if self.stock_entry:
            frappe.throw(_("This Daily Oil Repack already has a Stock Entry."))

        doc = frappe.get_doc(
            {
                "doctype": "Stock Entry",
                "company": self.company,
                "stock_entry_type": "Repack",
                "purpose": "Repack",
                "posting_date": self.repacking_date,
                "posting_time": TECHNICAL_DAILY_POSTING_TIME,
                "set_posting_time": 1,
                "items": [
                    {
                        "item_code": self.palm_olein_item,
                        "s_warehouse": self.combined_bulk_warehouse,
                        "qty": flt(self.bulk_consumption_kg),
                        "uom": "Kg",
                        "stock_uom": "Kg",
                        "conversion_factor": 1,
                    },
                    {
                        "item_code": self.empty_container_item,
                        "s_warehouse": self.combined_bulk_warehouse,
                        "qty": int(self.empty_containers_consumed),
                        "uom": "Nos",
                        "stock_uom": "Nos",
                        "conversion_factor": 1,
                    },
                    {
                        "item_code": self.finished_palm_oil_item,
                        "t_warehouse": self.combined_bulk_warehouse,
                        "qty": int(self.finished_containers),
                        "uom": "Nos",
                        "stock_uom": "Nos",
                        "conversion_factor": 1,
                    },
                ],
            }
        )
        if doc.meta.has_field("remarks"):
            doc.remarks = (
                f"NKT Oil Daily Repack {self.name}; physical repacking date "
                f"{self.repacking_date}; reported spillage {flt(self.reported_spillage_kg):.6f} Kg. "
                "00:00:01 is a technical daily-ledger ordering time, not a claimed physical clock time."
            )
        doc.insert(ignore_permissions=True)
        doc.submit()
        return doc
