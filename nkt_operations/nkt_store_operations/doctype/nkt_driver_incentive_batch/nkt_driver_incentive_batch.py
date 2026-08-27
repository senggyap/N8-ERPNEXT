import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime, flt

ALLOWED = {
    "Draft": {"Prepared", "Cancelled"},
    "Prepared": {"Draft", "Paid", "Cancelled"},
    "Paid": set(),
    "Cancelled": set(),
}

class NKTDriverIncentiveBatch(Document):
    def validate(self):
        if self.period_start and self.period_end and self.period_start > self.period_end:
            frappe.throw("Batch Period Start cannot be after Batch Period End.")
        self._validate_transition()
        self._validate_and_snapshot_lines()
        self.total_amount = sum(flt(x.incentive_amount) for x in self.lines)
        if self.status in ("Prepared", "Paid"):
            if not self.lines:
                frappe.throw("At least one eligible ENT-owned trip is required.")
            for row in self.lines:
                if flt(row.incentive_amount) <= 0:
                    frappe.throw(f"Enter the approved incentive amount for trip {row.source_trip}. C14D does not guess the incentive rate/formula.")
        if self.status == "Paid" and not self.payout_date:
            frappe.throw("Payout Date is required before an incentive batch can be marked Paid.")
        self._stamp_audit()

    def on_update(self):
        self._sync_trip_tracking()

    def _validate_transition(self):
        if self.is_new():
            if self.status != "Draft":
                frappe.throw("A new incentive batch must start in Draft.")
            return
        old = frappe.db.get_value(self.doctype, self.name, "status") or "Draft"
        if old != self.status and self.status not in ALLOWED.get(old, set()):
            frappe.throw(f"Invalid incentive batch status change: {old} → {self.status}.")

    def _validate_and_snapshot_lines(self):
        seen=set()
        for row in self.lines:
            if row.source_trip in seen:
                frappe.throw(f"Trip {row.source_trip} appears more than once in this batch.")
            seen.add(row.source_trip)
            if not frappe.db.exists("NKT Trucking Trip", row.source_trip):
                frappe.throw(f"Trucking Trip {row.source_trip} does not exist.")
            t = frappe.db.get_value("NKT Trucking Trip", row.source_trip,
                ["trip_date","driver_name","vehicle","plate_no","dr_no","eir_no","paperwork_verified_at","fleet_ownership_snapshot","driver_incentive_eligible","paperwork_complete","status","incentive_paid","incentive_batch"], as_dict=True) or {}
            if t.get("fleet_ownership_snapshot") != "ENT-Owned":
                frappe.throw(f"Trip {row.source_trip} is not an ENT-owned-truck trip. External carriers never receive ENT driver incentives.")
            if int(t.get("incentive_paid") or 0):
                frappe.throw(f"Trip {row.source_trip} is already recorded as incentive-paid.")
            if t.get("status") not in ("Delivered", "Closed") or not int(t.get("paperwork_complete") or 0):
                frappe.throw(f"Trip {row.source_trip} is not ready: delivery and all required papers/EIR must be complete first.")
            # Eligibility may be 0 when the current batch is already Prepared; the duplicate/link checks below remain authoritative.
            if self.status == "Draft" and not int(t.get("driver_incentive_eligible") or 0) and t.get("incentive_batch") != self.name:
                frappe.throw(f"Trip {row.source_trip} is not currently in Ready for Weekly Payout state.")
            self._guard_other_batch(row.source_trip)
            row.trip_date=t.get("trip_date"); row.driver_name=t.get("driver_name"); row.vehicle=t.get("vehicle")
            row.plate_no=t.get("plate_no"); row.dr_no=t.get("dr_no"); row.eir_no=t.get("eir_no"); row.paperwork_verified_at=t.get("paperwork_verified_at")

    def _guard_other_batch(self, trip):
        rows = frappe.db.sql("""
            select p.name, p.status
            from `tabNKT Driver Incentive Batch Line` l
            join `tabNKT Driver Incentive Batch` p on p.name=l.parent
            where l.source_trip=%s and p.status in ('Draft','Prepared','Paid') and p.name<>%s
            limit 1
        """, (trip, self.name or ""), as_dict=True)
        if rows:
            frappe.throw(f"Trip {trip} is already present in incentive batch {rows[0].name} ({rows[0].status}). Cancel/remove that batch first. This prevents duplicate weekly payment.")

    def _stamp_audit(self):
        old = None if self.is_new() else frappe.db.get_value(self.doctype, self.name, "status")
        if self.status == "Prepared" and old != "Prepared":
            self.prepared_by = frappe.session.user; self.prepared_at = now_datetime()
        if self.status == "Paid" and old != "Paid":
            self.paid_by = frappe.session.user; self.paid_at = now_datetime()

    def _sync_trip_tracking(self):
        now = now_datetime()
        current={x.source_trip:flt(x.incentive_amount) for x in self.lines}
        # Clear this batch link if it was returned to Draft/Cancelled after preparation.
        if self.status in ("Draft","Cancelled"):
            linked=frappe.get_all("NKT Trucking Trip", filters={"incentive_batch":self.name}, pluck="name")
            for trip in linked:
                frappe.db.set_value("NKT Trucking Trip", trip, {"incentive_batch":None,"incentive_paid":0,"incentive_paid_amount":0,"incentive_paid_at":None,"driver_incentive_eligible":1,"incentive_tracking_status":"Ready for Weekly Payout"}, update_modified=False)
            return
        if self.status == "Prepared":
            for trip in current:
                frappe.db.set_value("NKT Trucking Trip", trip, {"incentive_batch":self.name,"driver_incentive_eligible":0,"incentive_tracking_status":"Included in Prepared Batch"}, update_modified=False)
        if self.status == "Paid":
            for trip,amount in current.items():
                frappe.db.set_value("NKT Trucking Trip", trip, {"incentive_batch":self.name,"incentive_paid":1,"incentive_paid_amount":amount,"incentive_paid_at":now,"driver_incentive_eligible":0,"incentive_tracking_status":"Paid"}, update_modified=False)
