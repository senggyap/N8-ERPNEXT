import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

ALLOWED_TRANSITIONS = {
    "Draft": {"Requested", "Cancelled"},
    "Requested": {"Scheduled", "Cancelled"},
    "Scheduled": {"Dispatched", "Cancelled"},
    "Dispatched": {"In Transit", "Delivered", "Cancelled"},
    "In Transit": {"Delivered", "Cancelled"},
    "Delivered": {"Closed"},
    "Closed": set(),
    "Cancelled": set(),
}

ASSIGNMENT_REQUIRED = {"Scheduled", "Dispatched", "In Transit", "Delivered", "Closed"}
HISTORY_LOCKED = {"Dispatched", "In Transit", "Delivered", "Closed"}

class NKTTruckingTrip(Document):
    def validate(self):
        self._guard_closed_history()
        if self.job_type == "External Customer" and not self.customer:
            frappe.throw("Customer is required for an External Customer trucking trip.")
        if not self.origin or not self.destination:
            frappe.throw("Origin and Destination are required.")
        if not self.driver_name:
            frappe.throw("Actual Driver is required for every trucking trip. Relief/substitute drivers must be recorded on the trip itself.")
        self._set_fleet_and_carrier_snapshot()
        self._validate_paperwork()
        self._set_incentive_state()
        self._set_carrier_payable_state()
        self._validate_status_transition()
        self._stamp_status_time()

    def _vehicle_values(self):
        if not self.vehicle:
            return "Unclassified", None
        values = frappe.db.get_value(
            "NKT Vehicle", self.vehicle, ["custom_fleet_ownership", "related_supplier"], as_dict=True
        ) or {}
        return values.get("custom_fleet_ownership") or "Unclassified", values.get("related_supplier")

    def _set_fleet_and_carrier_snapshot(self):
        ownership, carrier = self._vehicle_values()
        if self.status in ASSIGNMENT_REQUIRED and not self.vehicle:
            frappe.throw("Truck / Vehicle is required once a trucking trip is Scheduled or later.")
        if self.status in ASSIGNMENT_REQUIRED and ownership == "Unclassified":
            frappe.throw("Classify the selected vehicle as ENT-Owned or External Carrier before the trip can be Scheduled/Dispatched.")
        if self.status in ASSIGNMENT_REQUIRED and ownership == "External Carrier" and not carrier:
            frappe.throw("External Carrier vehicles require Related Supplier on the NKT Vehicle master so carrier SOA/payables can identify who must be paid.")

        old_status = None if self.is_new() else frappe.db.get_value(self.doctype, self.name, "status")
        old_snapshot = None if self.is_new() else frappe.db.get_value(self.doctype, self.name, "fleet_ownership_snapshot")
        old_carrier = None if self.is_new() else frappe.db.get_value(self.doctype, self.name, "carrier_account_snapshot")
        if old_status in HISTORY_LOCKED and old_snapshot:
            old_vehicle = frappe.db.get_value(self.doctype, self.name, "vehicle")
            if old_vehicle and self.vehicle and self.vehicle != old_vehicle:
                frappe.throw("Vehicle assignment is locked after dispatch. Use a controlled correction rather than rewriting trip history.")
            self.fleet_ownership_snapshot = old_snapshot
            self.carrier_account_snapshot = old_carrier
        else:
            self.fleet_ownership_snapshot = ownership
            self.carrier_account_snapshot = carrier if ownership == "External Carrier" else None

        self.driver_incentive_applicable = 1 if self.fleet_ownership_snapshot == "ENT-Owned" else 0

    def _validate_paperwork(self):
        old_complete = 0 if self.is_new() else int(frappe.db.get_value(self.doctype, self.name, "paperwork_complete") or 0)
        old_returned = 0 if self.is_new() else int(frappe.db.get_value(self.doctype, self.name, "container_returned") or 0)
        if (self.container_return_required or self.eir_required) and not (self.container_no or "").strip():
            frappe.throw("Container No. is required when Container Return or EIR control applies.")
        if self.container_returned and not old_returned and not self.container_returned_at:
            event_time = getattr(self.flags, "nkt_c15c10l_event_datetime", None) or now_datetime()
            self.container_returned_at = event_time
        if old_complete and not self.paperwork_complete:
            frappe.throw("Completed trip paperwork cannot be silently reopened. Use a controlled correction/audit path.")
        if self.paperwork_complete:
            if self.status not in ("Delivered", "Closed"):
                frappe.throw("All Required Papers Complete may be confirmed only after the trip is Delivered.")
            if self.container_return_required and not self.container_returned:
                frappe.throw("Container Return is required before trip paperwork can be marked complete.")
            if self.eir_required and not (self.eir_no or "").strip():
                frappe.throw("EIR No. is required before trip paperwork can be marked complete.")
            if not old_complete:
                preserved_user = getattr(self.flags, "nkt_c15c10l_origin_user", None)
                preserved_time = getattr(self.flags, "nkt_c15c10l_event_datetime", None)
                self.paperwork_verified_by = preserved_user or frappe.session.user
                self.paperwork_verified_at = preserved_time or now_datetime()

    def _set_incentive_state(self):
        applicable = bool(self.driver_incentive_applicable)
        if not applicable:
            self.driver_incentive_eligible = 0
            self.incentive_tracking_status = "Not Applicable"
            return

        # Paid/Prepared state is authoritative over the derived readiness state.
        if int(self.incentive_paid or 0):
            self.driver_incentive_eligible = 0
            self.incentive_tracking_status = "Paid"
            return
        if self.incentive_batch and frappe.db.exists("NKT Driver Incentive Batch", self.incentive_batch):
            batch_status = frappe.db.get_value("NKT Driver Incentive Batch", self.incentive_batch, "status")
            if batch_status == "Paid":
                self.driver_incentive_eligible = 0
                self.incentive_tracking_status = "Paid"
                return
            if batch_status == "Prepared":
                self.driver_incentive_eligible = 0
                self.incentive_tracking_status = "Included in Prepared Batch"
                return

        if self.status not in ("Delivered", "Closed"):
            self.driver_incentive_eligible = 0
            self.incentive_tracking_status = "Waiting for Delivery"
        elif not self.paperwork_complete:
            self.driver_incentive_eligible = 0
            self.incentive_tracking_status = "Waiting for Papers"
        else:
            self.driver_incentive_eligible = 1
            self.incentive_tracking_status = "Ready for Weekly Payout"

    def _set_carrier_payable_state(self):
        if self.fleet_ownership_snapshot != "External Carrier":
            self.carrier_payable_status = "Not Applicable"
        elif self.status not in ("Delivered", "Closed"):
            self.carrier_payable_status = "Waiting for Delivery"
        else:
            # Carrier rate/payable remains outside the Trip. C9 Trucker SOA is the restricted payable ledger.
            self.carrier_payable_status = "Ready for Trucker SOA"

    def _validate_status_transition(self):
        if self.is_new():
            return
        old = frappe.db.get_value(self.doctype, self.name, "status")
        if old and self.status != old and self.status not in ALLOWED_TRANSITIONS.get(old, set()):
            frappe.throw(f"Invalid trucking trip status change: {old} → {self.status}.")

    def _stamp_status_time(self):
        stamp = getattr(self.flags, "nkt_c15c10l_event_datetime", None) or now_datetime()
        mapping = {
            "Requested": "requested_at",
            "Dispatched": "dispatched_at",
            "In Transit": "in_transit_at",
            "Delivered": "delivered_at",
            "Closed": "closed_at",
        }
        field = mapping.get(self.status)
        if field and not self.get(field):
            self.set(field, stamp)

    def _guard_closed_history(self):
        if self.is_new():
            return
        old_status = frappe.db.get_value(self.doctype, self.name, "status")
        if old_status in ("Closed", "Cancelled"):
            frappe.throw(f"{old_status} trucking trips are locked. Use a controlled correction in a later C14 phase rather than rewriting history.")

    def on_trash(self):
        if self.status != "Draft":
            frappe.throw("Only Draft trucking trips may be deleted.")
