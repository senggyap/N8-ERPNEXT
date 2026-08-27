import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

ALLOWED_TRANSITIONS = {
    "Draft": {"Scheduled", "In Progress", "Completed", "Cancelled"},
    "Scheduled": {"In Progress", "Completed", "Cancelled"},
    "In Progress": {"Completed", "Cancelled"},
    "Completed": set(),
    "Cancelled": set(),
}

class NKTTruckingVehicleMaintenance(Document):
    def validate(self):
        self._guard_locked_history()
        self._fill_vehicle_snapshot()
        self._validate_lineage()
        self._validate_cost()
        self._validate_status_transition()
        self._stamp_completion()

    def _guard_locked_history(self):
        if self.is_new():
            return
        old_status = frappe.db.get_value(self.doctype, self.name, "status")
        if old_status in ("Completed", "Cancelled"):
            frappe.throw(f"{old_status} maintenance records are locked. Create a controlled follow-up/correction record rather than rewriting history.")

    def _vehicle_values(self):
        if not self.vehicle:
            return "Unclassified", None
        values = frappe.db.get_value(
            "NKT Vehicle", self.vehicle, ["custom_fleet_ownership", "plate_number"], as_dict=True
        ) or {}
        return values.get("custom_fleet_ownership") or "Unclassified", values.get("plate_number")

    def _fill_vehicle_snapshot(self):
        ownership, plate = self._vehicle_values()
        self.fleet_ownership_snapshot = ownership
        if plate:
            self.plate_no = plate

    def _validate_lineage(self):
        if self.source_trip:
            trip = frappe.get_doc("NKT Trucking Trip", self.source_trip)
            if trip.vehicle and self.vehicle != trip.vehicle:
                frappe.throw(f"Related Trip {trip.name} uses vehicle {trip.vehicle}; maintenance Vehicle must match that trip.")
            self.driver_name_snapshot = trip.driver_name
        elif self.source_incident:
            inc = frappe.get_doc("NKT Trucking Incident", self.source_incident)
            if inc.vehicle and self.vehicle != inc.vehicle:
                frappe.throw(f"Related Incident {inc.name} uses vehicle {inc.vehicle}; maintenance Vehicle must match that incident.")
            if inc.driver_name_snapshot:
                self.driver_name_snapshot = inc.driver_name_snapshot
            if inc.source_trip and not self.source_trip:
                self.source_trip = inc.source_trip
        else:
            self.driver_name_snapshot = None

    def _validate_cost(self):
        if flt(self.actual_cost) < 0:
            frappe.throw("Actual Cost cannot be negative.")
        if self.linked_trucking_expense and not frappe.db.exists("NKT Trucking Expense", self.linked_trucking_expense):
            frappe.throw("Linked Trucking Expense does not exist.")
        self.accounting_posting_status = "Operational Only — No GL Posting"

    def _validate_status_transition(self):
        if self.is_new():
            return
        old = frappe.db.get_value(self.doctype, self.name, "status")
        if old and self.status != old and self.status not in ALLOWED_TRANSITIONS.get(old, set()):
            frappe.throw(f"Invalid maintenance status change: {old} → {self.status}.")

    def _stamp_completion(self):
        if self.status == "Completed" and not self.completed_at:
            self.completed_at = now_datetime()
            self.completed_by_user = frappe.session.user

    def on_trash(self):
        if self.status != "Draft":
            frappe.throw("Only Draft maintenance records may be deleted.")
