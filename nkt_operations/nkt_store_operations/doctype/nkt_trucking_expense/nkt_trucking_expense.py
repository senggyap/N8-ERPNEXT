import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

LOCKED = {"Verified", "Cancelled"}

DEFAULT_TREATMENT = {
    "Fuel": "Vehicle Operating Cost",
    "Driver Incentive (ENT Truck Only)": "Direct Trip Cost",
    "Helper Incentive": "Direct Trip Cost",
    "Toll / Parking": "Direct Trip Cost",
    "Trip Incidental": "Direct Trip Cost",
    "Maintenance / Repair": "Vehicle Operating Cost",
    "Payroll / Salary": "Vehicle Operating Cost",
    "Office / Admin": "Overhead",
    "Capital / Acquisition": "Capital / Excluded from Operating Profit",
}

class NKTTruckingExpense(Document):
    def validate(self):
        self._guard_locked_history()
        if not self.lines:
            frappe.throw("At least one trucking expense line is required.")
        total = 0
        for row in self.lines:
            self._prepare_line(row)
            if flt(row.amount) <= 0:
                frappe.throw(f"Row {row.idx}: Amount must be greater than zero.")
            total += flt(row.amount)
        self.grand_total = total
        if self.status == "Verified":
            if not self.verified_by_user:
                self.verified_by_user = frappe.session.user
            if not self.verified_at:
                self.verified_at = now_datetime()

    def _prepare_line(self, row):
        if row.source_trip:
            trip = frappe.get_doc("NKT Trucking Trip", row.source_trip)
            if not row.vehicle and trip.vehicle:
                row.vehicle = trip.vehicle
            if row.vehicle and trip.vehicle and row.vehicle != trip.vehicle:
                frappe.throw(f"Row {row.idx}: Expense vehicle {row.vehicle} does not match Source Trip {trip.name} vehicle {trip.vehicle}.")
            if not row.driver_name and trip.driver_name:
                row.driver_name = trip.driver_name

        # Apply a transparent default only when blank. Users can explicitly choose another
        # treatment for categories where business context genuinely differs.
        if not row.profit_treatment:
            if row.expense_category == "Fuel" and row.source_trip:
                row.profit_treatment = "Direct Trip Cost"
            elif row.expense_category == "Payroll / Salary" and not row.vehicle:
                row.profit_treatment = "Overhead"
            else:
                row.profit_treatment = DEFAULT_TREATMENT.get(row.expense_category)

        if row.expense_category == "Capital / Acquisition":
            row.profit_treatment = "Capital / Excluded from Operating Profit"

        if row.expense_category == "Driver Incentive (ENT Truck Only)":
            self._validate_driver_incentive(row)

        if row.expense_category == "Other" and not row.profit_treatment:
            frappe.throw(f"Row {row.idx}: Profit Treatment is required for Other expenses.")

    def _validate_driver_incentive(self, row):
        if not row.source_trip:
            frappe.throw(f"Row {row.idx}: Driver Incentive requires a Source Trip.")
        trip = frappe.get_doc("NKT Trucking Trip", row.source_trip)
        if not trip.vehicle:
            frappe.throw(f"Row {row.idx}: Driver Incentive requires a truck assigned to Source Trip {trip.name}.")
        ownership = trip.fleet_ownership_snapshot or frappe.db.get_value("NKT Vehicle", trip.vehicle, "custom_fleet_ownership") or "Unclassified"
        if ownership != "ENT-Owned":
            frappe.throw(f"Row {row.idx}: Driver Incentive is allowed only for an ENT-Owned truck trip. {trip.name} is {ownership}.")
        if not trip.driver_incentive_eligible:
            status = trip.incentive_tracking_status or "Not Ready"
            frappe.throw(f"Row {row.idx}: Driver Incentive for {trip.name} is not payable yet ({status}). Delivery and all required papers/EIR controls must be complete first.")
        if not row.driver_name:
            row.driver_name = trip.driver_name
        if (row.driver_name or "").strip() != (trip.driver_name or "").strip():
            frappe.throw(f"Row {row.idx}: Driver Incentive driver must match the actual Driver on Source Trip {trip.name}.")
        row.vehicle = trip.vehicle
        row.profit_treatment = "Direct Trip Cost"
        self._guard_duplicate_driver_incentive(row, trip.name)

    def _guard_duplicate_driver_incentive(self, row, trip_name):
        # A single voucher must never repeat the same trip incentive.
        matches = [r for r in self.lines if r.expense_category == "Driver Incentive (ENT Truck Only)" and r.source_trip == trip_name]
        if len(matches) > 1:
            frappe.throw(f"Driver Incentive for {trip_name} appears more than once in this expense voucher.")
        # Once another voucher is Verified, the same trip cannot be verified again.
        if self.status == "Verified":
            existing = frappe.db.sql(
                """select parent from `tabNKT Trucking Expense Line`
                   where expense_category=%s and source_trip=%s and parenttype='NKT Trucking Expense'
                     and parent<>%s""",
                ("Driver Incentive (ENT Truck Only)", trip_name, self.name or ""),
                as_dict=True,
            )
            for hit in existing:
                if frappe.db.get_value("NKT Trucking Expense", hit.parent, "status") == "Verified":
                    frappe.throw(f"Driver Incentive for {trip_name} was already verified in {hit.parent}. Duplicate incentive payment/cost is not allowed.")

    def _guard_locked_history(self):
        if self.is_new():
            return
        old_status = frappe.db.get_value(self.doctype, self.name, "status")
        if old_status in LOCKED:
            frappe.throw(f"{old_status} trucking expense vouchers are locked. Create a controlled correction instead of silently editing history.")
        if old_status == "Draft" and self.status not in ("Draft", "Verified", "Cancelled"):
            frappe.throw(f"Invalid trucking expense status change: {old_status} → {self.status}.")

    def on_trash(self):
        if self.status != "Draft":
            frappe.throw("Only Draft trucking expense vouchers may be deleted.")
