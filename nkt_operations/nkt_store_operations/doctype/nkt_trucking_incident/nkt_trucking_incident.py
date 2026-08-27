import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

ALLOWED_TRANSITIONS = {
    "Open": {"Under Review", "Resolved", "Closed"},
    "Under Review": {"Resolved", "Closed"},
    "Resolved": {"Closed"},
    "Closed": set(),
}

class NKTTruckingIncident(Document):
    def validate(self):
        self._guard_closed_history()
        self._fill_trip_vehicle_snapshot()
        self._validate_costs_and_claim()
        self._validate_status_transition()
        self._stamp_audit()

    def _guard_closed_history(self):
        if self.is_new():
            return
        old_status = frappe.db.get_value(self.doctype, self.name, "status")
        if old_status == "Closed":
            frappe.throw("Closed trucking incidents are locked. Create a controlled follow-up/correction record rather than rewriting incident history.")

    def _vehicle_values(self):
        if not self.vehicle:
            return "Unclassified", None
        values = frappe.db.get_value(
            "NKT Vehicle", self.vehicle, ["custom_fleet_ownership", "plate_number"], as_dict=True
        ) or {}
        return values.get("custom_fleet_ownership") or "Unclassified", values.get("plate_number")

    def _fill_trip_vehicle_snapshot(self):
        if self.source_trip:
            trip = frappe.get_doc("NKT Trucking Trip", self.source_trip)
            if trip.vehicle:
                if self.vehicle and self.vehicle != trip.vehicle:
                    frappe.throw(f"Related Trip {trip.name} uses vehicle {trip.vehicle}; Incident Vehicle must match the trip.")
                self.vehicle = trip.vehicle
            self.driver_name_snapshot = trip.driver_name
            self.helper_name_snapshot = trip.helper_name
            self.destination_snapshot = trip.destination
            self.dr_no_snapshot = trip.dr_no
            self.eir_no_snapshot = trip.eir_no
        if not self.vehicle:
            frappe.throw("Truck / Vehicle is required.")
        ownership, plate = self._vehicle_values()
        self.fleet_ownership_snapshot = ownership
        self.plate_no = plate

    def _validate_costs_and_claim(self):
        if flt(self.estimated_cost) < 0 or flt(self.actual_cost) < 0:
            frappe.throw("Estimated Cost and Actual Cost cannot be negative.")
        if self.claim_status in ("Filed", "Settled") and not (self.claim_reference or "").strip():
            frappe.throw("Claim Reference is required when Claim Status is Filed or Settled.")
        if self.status in ("Resolved", "Closed") and not (self.resolution_notes or "").strip():
            frappe.throw("Resolution / Final Notes are required before an incident can be Resolved or Closed.")
        if self.linked_trucking_expense and not frappe.db.exists("NKT Trucking Expense", self.linked_trucking_expense):
            frappe.throw("Linked Trucking Expense does not exist.")
        self.accounting_posting_status = "Operational Only — No GL Posting"

    def _validate_status_transition(self):
        if self.is_new():
            return
        old = frappe.db.get_value(self.doctype, self.name, "status")
        if old and self.status != old and self.status not in ALLOWED_TRANSITIONS.get(old, set()):
            frappe.throw(f"Invalid incident status change: {old} → {self.status}.")

    def _stamp_audit(self):
        now = now_datetime()
        if not self.reported_by_user:
            self.reported_by_user = frappe.session.user
        if not self.reported_at:
            self.reported_at = now
        if self.status in ("Resolved", "Closed") and not self.resolved_at:
            self.resolved_at = now
            self.resolved_by_user = frappe.session.user
        if self.status == "Closed" and not self.closed_at:
            self.closed_at = now
            self.closed_by_user = frappe.session.user

    def on_trash(self):
        if self.status != "Open":
            frappe.throw("Only Open incident records may be deleted.")
