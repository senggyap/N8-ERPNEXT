from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, nowdate

TOL = 0.000001


class NKTTruckingJob(Document):
    def validate(self):
        if self.job_type != "Supplier Arrival Haul":
            frappe.throw(_("C9G foundation supports Supplier Arrival Haul only."))

        if getdate(self.job_date) != getdate(nowdate()):
            frappe.throw(_("Trucking Job Date must be the current business date."))

        self._snapshot_supplier_arrival()

    def _snapshot_supplier_arrival(self):
        if not self.source_supplier_receiving:
            frappe.throw(_("Supplier Arrival is required."))

        receiving = frappe.get_doc("NKT Supplier Receiving", self.source_supplier_receiving)

        if int(receiving.docstatus or 0) != 1 or receiving.posting_status != "Posted":
            frappe.throw(_("Supplier Arrival must be submitted and Posted before creating a Trucking Job."))

        existing = frappe.db.exists(
            "NKT Trucking Job",
            {
                "source_supplier_receiving": receiving.name,
                "name": ["!=", self.name or ""],
            },
        )
        if existing:
            frappe.throw(_(
                "Supplier Arrival {0} already has Trucking Job {1}."
            ).format(receiving.name, existing))

        self.company = receiving.company
        self.job_date = receiving.receiving_date
        self.source_supplier = receiving.supplier
        self.receiving_warehouse = receiving.receiving_warehouse

        self.bl_no = receiving.bill_of_lading_no
        self.dr_no = receiving.supplier_dr_no or receiving.supplier_delivery_reference
        self.other_supplier_reference = receiving.supplier_delivery_reference

        self.delivery_vehicle = receiving.delivery_vehicle
        self.plate_number = receiving.plate_number
        self.internal_vehicle_no = receiving.internal_vehicle_no
        self.vehicle_operator = receiving.vehicle_operator
        self.driver_name = receiving.driver_name
        self.physical_source_locked = 1

        # Default Carrier/Trucker Account from remembered vehicle when available.
        if self.delivery_vehicle and not self.carrier_account:
            carrier = frappe.db.get_value(
                "NKT Vehicle", self.delivery_vehicle, "related_supplier"
            )
            if carrier:
                self.carrier_account = carrier

        # Physical facts are copied from the Supplier Arrival and are not manually
        # re-encoded. This is operational lineage only; no trucking rate/payable.
        self.set("items", [])
        for source in receiving.items:
            row = self.append("items", {})
            row.item_code = source.item_code
            row.item_name = source.item_name
            row.purchase_order_item = source.purchase_order_item
            row.uom = source.uom
            row.expected_qty = flt(source.expected_qty)
            row.hauled_qty = flt(source.delivered_qty)
            row.accepted_qty = flt(source.accepted_qty)
            row.damaged_qty = flt(source.damaged_qty)
            row.other_rejected_qty = flt(source.other_rejected_qty)
            row.shortlanded_qty = flt(source.shortage_qty)
            row.condition_summary = source.condition_reason or source.condition_classification

        if not self.items:
            frappe.throw(_("Supplier Arrival has no physical item rows."))

        if not self.is_new():
            old = self.get_doc_before_save()
            if old and old.source_supplier_receiving != self.source_supplier_receiving:
                frappe.throw(_("Trucking Job source Supplier Arrival cannot be changed."))

    def before_save(self):
        self.job_status = "Recorded"
