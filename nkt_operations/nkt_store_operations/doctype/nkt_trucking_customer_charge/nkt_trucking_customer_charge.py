import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

ALLOWED = {
    "Draft": {"Ready to Bill", "Cancelled"},
    "Ready to Bill": {"Draft", "Cancelled"},
    "Billed": set(),
    "Cancelled": set(),
}

class NKTTruckingCustomerCharge(Document):
    def validate(self):
        self._guard_locked_history()
        self._validate_transition()
        self._snapshot_trip()
        self._recalculate()
        if self.status == "Ready to Bill":
            self._validate_ready()

    def _guard_locked_history(self):
        if self.is_new():
            return
        old = frappe.db.get_value(self.doctype, self.name, "status")
        if old in ("Billed", "Cancelled"):
            frappe.throw(f"{old} customer charges are locked. Create a controlled correction rather than rewriting issued history.")
        if old == "Ready to Bill":
            held = frappe.db.sql("""
                select s.name
                from `tabNKT Trucking Customer SOA` s
                inner join `tabNKT Trucking Customer SOA Line` l on l.parent=s.name
                where l.source_charge=%s and s.status='Prepared'
                limit 1
            """, self.name, as_dict=True)
            if held:
                frappe.throw(f"Customer Charge {self.name} is held on Prepared SOA {held[0].name}. Correct the SOA workflow first rather than changing the prepared billing source underneath it.")

    def _validate_transition(self):
        if self.is_new():
            if self.status not in (None, "Draft"):
                frappe.throw("A new customer charge must start in Draft.")
            return
        old = frappe.db.get_value(self.doctype, self.name, "status") or "Draft"
        if old != self.status and self.status not in ALLOWED.get(old, set()):
            frappe.throw(f"Invalid customer-charge status change: {old} → {self.status}.")

    def _snapshot_trip(self):
        if not self.source_trip:
            return
        t = frappe.get_doc("NKT Trucking Trip", self.source_trip)
        if not self.charge_date:
            self.charge_date = t.trip_date
        if not self.customer and t.job_type == "External Customer":
            self.customer = t.customer
        self.driver_name = t.driver_name
        self.vehicle = t.vehicle
        self.plate_no = t.plate_no
        self.dr_no = t.dr_no
        self.eir_no = t.eir_no
        if self.charge_type == "Backload" and not int(t.has_backload or 0):
            frappe.throw("Mark Has Backload on the source Trip before a Backload customer charge can be made Ready to Bill.")

    def _recalculate(self):
        if not int(self.manual_amount_override or 0):
            self.amount = flt(self.qty) * flt(self.rate)
        elif not (self.override_reason or "").strip():
            frappe.throw("Override Reason is required when Manual Amount Override is enabled.")

    def _validate_ready(self):
        t = frappe.get_doc("NKT Trucking Trip", self.source_trip)
        if t.status not in ("Delivered", "Closed"):
            frappe.throw("The source Trip must be Delivered or Closed before this charge can be Ready to Bill.")
        if not self.customer:
            frappe.throw("Bill To Customer is required before Ready to Bill.")
        if not self.destination:
            frappe.throw("Charge Destination is required before Ready to Bill.")
        if flt(self.amount) <= 0:
            frappe.throw("Customer-billable charge Amount must be greater than zero.")
        if not int(self.manual_amount_override or 0):
            if flt(self.qty) <= 0:
                frappe.throw("Qty must be greater than zero unless Manual Amount Override is used.")
            if flt(self.rate) < 0:
                frappe.throw("Rate cannot be negative.")

    def on_trash(self):
        if self.status != "Draft":
            frappe.throw("Only Draft customer charges may be deleted.")
