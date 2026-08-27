import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

LOCKED = {"Liquidation Submitted", "Settled", "Cancelled"}

class NKTTruckingDriverCashAdvance(Document):
    def validate(self):
        self._guard_history()
        if flt(self.amount) <= 0:
            frappe.throw("Advance Amount must be greater than zero.")

        self.driver_name = (self.driver_name or "").strip()
        if not self.driver_name:
            frappe.throw("Driver is required.")

        if self.scope == "Trip-Specific":
            if not self.source_trip:
                frappe.throw("Trip-Specific cash advance requires a Source Trip.")
            trip = frappe.get_doc("NKT Trucking Trip", self.source_trip)
            if (trip.driver_name or "").strip() != self.driver_name:
                frappe.throw(
                    f"Cash advance Driver must match the actual Driver on {trip.name}: {trip.driver_name}."
                )
            if trip.vehicle:
                if self.vehicle and self.vehicle != trip.vehicle:
                    frappe.throw(f"Cash advance vehicle must match Source Trip {trip.name}.")
                self.vehicle = trip.vehicle
            self.fleet_ownership_snapshot = trip.fleet_ownership_snapshot or "Unclassified"
        elif self.scope == "General ENT Operations":
            if self.source_trip:
                frappe.throw("General ENT Operations advance must not carry one Source Trip. Use liquidation lines to allocate actual trip costs.")
            if self.vehicle:
                self.fleet_ownership_snapshot = (
                    frappe.db.get_value("NKT Vehicle", self.vehicle, "custom_fleet_ownership") or "Unclassified"
                )
            else:
                self.fleet_ownership_snapshot = "Unclassified"
        else:
            frappe.throw("Invalid Advance Scope.")

        if self.is_new():
            if self.status not in ("Draft", "Released"):
                frappe.throw("A new cash advance may start only as Draft or Released.")
        if self.status == "Released":
            if not self.released_by_user:
                self.released_by_user = frappe.session.user
            if not self.released_at:
                self.released_at = now_datetime()
            if not flt(self.outstanding_amount):
                self.outstanding_amount = flt(self.amount)
        elif self.status == "Draft":
            self.outstanding_amount = flt(self.amount)

        self.accounting_posting_status = "Operational Only — No GL Posting"

    def _guard_history(self):
        if self.is_new():
            return
        old = self.get_doc_before_save()
        if not old:
            return
        old_status = old.status
        if old_status in LOCKED:
            frappe.throw(
                f"{old_status} cash advances are locked. Complete/correct them through the linked liquidation workflow."
            )
        if old_status == "Draft" and self.status not in ("Draft", "Released", "Cancelled"):
            frappe.throw(f"Invalid cash advance status change: {old_status} → {self.status}.")
        if old_status == "Released" and self.status != "Released":
            frappe.throw("Released cash advances are controlled by Driver Liquidation and cannot be manually re-statused.")

    def on_trash(self):
        if self.status != "Draft":
            frappe.throw("Only Draft cash advances may be deleted.")
