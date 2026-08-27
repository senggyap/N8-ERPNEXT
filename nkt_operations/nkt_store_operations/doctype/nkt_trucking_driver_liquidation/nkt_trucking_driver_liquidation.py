import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

LOCKED = {"Settled", "Cancelled"}

DEFAULT_TREATMENT = {
    "Fuel": "Vehicle Operating Cost",
    "Toll / Parking": "Direct Trip Cost",
    "Trip Incidental": "Direct Trip Cost",
    "Port / Container / EIR": "Direct Trip Cost",
    "Maintenance / Repair": "Vehicle Operating Cost",
}

class NKTTruckingDriverLiquidation(Document):
    def validate(self):
        self._guard_history()
        if not self.cash_advance:
            frappe.throw("Cash Advance is required.")
        advance = frappe.get_doc("NKT Trucking Driver Cash Advance", self.cash_advance)
        if advance.status in ("Settled", "Cancelled"):
            frappe.throw(f"Cash Advance {advance.name} is already {advance.status} and cannot be liquidated.")
        if advance.status == "Draft":
            frappe.throw(f"Cash Advance {advance.name} has not been Released yet.")

        self.driver_name = advance.driver_name
        self.source_trip = advance.source_trip
        self.vehicle = advance.vehicle
        self.advance_amount = flt(advance.amount)
        if not self.company and advance.company:
            self.company = advance.company

        self._guard_one_active_liquidation()
        if not self.lines:
            frappe.throw("At least one liquidation line is required.")

        total = 0
        for row in self.lines:
            self._prepare_line(row, advance)
            if flt(row.amount) <= 0:
                frappe.throw(f"Row {row.idx}: Amount must be greater than zero.")
            total += flt(row.amount)

        self.expense_total = total
        self.unused_cash_expected = max(flt(self.advance_amount) - total, 0)
        self.reimbursement_due = max(total - flt(self.advance_amount), 0)

        if flt(self.cash_return_received) < 0 or flt(self.reimbursement_paid) < 0:
            frappe.throw("Settlement amounts cannot be negative.")

        if self.status in ("Verified", "Settled"):
            if not self.verified_by_user:
                self.verified_by_user = frappe.session.user
            if not self.verified_at:
                self.verified_at = now_datetime()

        if self.status == "Settled":
            if not self.is_new():
                old_status = frappe.db.get_value(self.doctype, self.name, "status")
                if old_status != "Verified":
                    frappe.throw("Liquidation must be Verified before it can be Settled.")
            else:
                frappe.throw("A new liquidation cannot start as Settled.")
            if abs(flt(self.cash_return_received) - flt(self.unused_cash_expected)) > 0.005:
                frappe.throw(
                    f"Cash Return Received must equal expected unused cash ({self.unused_cash_expected})."
                )
            if abs(flt(self.reimbursement_paid) - flt(self.reimbursement_due)) > 0.005:
                frappe.throw(
                    f"Additional Reimbursement Paid must equal reimbursement due ({self.reimbursement_due})."
                )
            if (flt(self.unused_cash_expected) > 0 or flt(self.reimbursement_due) > 0) and not (self.settlement_reference or "").strip():
                frappe.throw("Settlement / Receipt Reference is required when cash is returned or an additional reimbursement is paid.")
            if not self.settled_by_user:
                self.settled_by_user = frappe.session.user
            if not self.settled_at:
                self.settled_at = now_datetime()

    def _prepare_line(self, row, advance):
        if advance.scope == "Trip-Specific":
            if row.source_trip and row.source_trip != advance.source_trip:
                frappe.throw(f"Row {row.idx}: Trip-Specific advance may only liquidate against {advance.source_trip}.")
            row.source_trip = advance.source_trip
            if advance.vehicle:
                if row.vehicle and row.vehicle != advance.vehicle:
                    frappe.throw(f"Row {row.idx}: Vehicle must match the cash advance trip vehicle.")
                row.vehicle = advance.vehicle
        else:
            if row.source_trip:
                trip = frappe.get_doc("NKT Trucking Trip", row.source_trip)
                if (trip.driver_name or "").strip() != (advance.driver_name or "").strip():
                    frappe.throw(
                        f"Row {row.idx}: Source Trip {trip.name} driver does not match advance Driver {advance.driver_name}."
                    )
                if trip.vehicle:
                    if row.vehicle and row.vehicle != trip.vehicle:
                        frappe.throw(f"Row {row.idx}: Vehicle does not match Source Trip {trip.name}.")
                    row.vehicle = trip.vehicle

        if not row.profit_treatment:
            if row.expense_category == "Fuel":
                row.profit_treatment = "Direct Trip Cost" if row.source_trip else "Vehicle Operating Cost"
            elif row.expense_category in ("Toll / Parking", "Trip Incidental", "Port / Container / EIR"):
                row.profit_treatment = "Direct Trip Cost" if row.source_trip else "Vehicle Operating Cost"
            else:
                row.profit_treatment = DEFAULT_TREATMENT.get(row.expense_category)

        if row.expense_category == "Other" and not (row.memo or "").strip():
            frappe.throw(f"Row {row.idx}: Other expense requires Details / Explanation.")

        if not row.profit_treatment:
            frappe.throw(f"Row {row.idx}: Profit Treatment is required.")

    def _guard_one_active_liquidation(self):
        hits = frappe.get_all(
            "NKT Trucking Driver Liquidation",
            filters={
                "cash_advance": self.cash_advance,
                "name": ["!=", self.name or ""],
                "status": ["!=", "Cancelled"],
            },
            pluck="name",
            limit=1,
        )
        if hits:
            frappe.throw(f"Cash Advance {self.cash_advance} already has active liquidation {hits[0]}.")

    def _guard_history(self):
        if self.is_new():
            if self.status not in ("Draft", "Verified", "Cancelled"):
                frappe.throw("A new liquidation may start only as Draft, Verified, or Cancelled.")
            return
        old = self.get_doc_before_save()
        if not old:
            return
        if old.status in LOCKED:
            frappe.throw(f"{old.status} liquidations are locked. Create a controlled correction instead of editing history.")
        if old.status == "Draft":
            if self.status not in ("Draft", "Verified", "Cancelled"):
                frappe.throw(f"Invalid liquidation status change: Draft → {self.status}.")
        elif old.status == "Verified":
            if self.status not in ("Verified", "Settled"):
                frappe.throw(f"Invalid liquidation status change: Verified → {self.status}.")
            self._guard_verified_financial_history(old)

    def _guard_verified_financial_history(self, old):
        fields = ["cash_advance", "driver_name", "source_trip", "vehicle", "advance_amount", "expense_total", "unused_cash_expected", "reimbursement_due"]
        for fn in fields:
            if str(getattr(self, fn, None) or "") != str(getattr(old, fn, None) or ""):
                frappe.throw(f"Verified liquidation field {fn} is locked.")
        old_lines = [(x.expense_category, x.profit_treatment, x.source_trip, x.vehicle, flt(x.amount), x.reference_no or "", x.memo or "", x.receipt_attachment or "") for x in old.lines]
        new_lines = [(x.expense_category, x.profit_treatment, x.source_trip, x.vehicle, flt(x.amount), x.reference_no or "", x.memo or "", x.receipt_attachment or "") for x in self.lines]
        if old_lines != new_lines:
            frappe.throw("Verified liquidation expense lines are locked. Only settlement fields may be completed.")

    def on_update(self):
        if not self.cash_advance:
            return
        if self.status == "Verified":
            frappe.db.set_value(
                "NKT Trucking Driver Cash Advance",
                self.cash_advance,
                {
                    "status": "Liquidation Submitted",
                    "liquidation": self.name,
                    "outstanding_amount": flt(self.unused_cash_expected),
                },
                update_modified=True,
            )
        elif self.status == "Settled":
            frappe.db.set_value(
                "NKT Trucking Driver Cash Advance",
                self.cash_advance,
                {
                    "status": "Settled",
                    "liquidation": self.name,
                    "outstanding_amount": 0,
                },
                update_modified=True,
            )

    def on_trash(self):
        if self.status != "Draft":
            frappe.throw("Only Draft liquidations may be deleted.")
