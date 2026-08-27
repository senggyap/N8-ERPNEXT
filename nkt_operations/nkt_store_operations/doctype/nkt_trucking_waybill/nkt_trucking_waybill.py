import frappe
from frappe.model.document import Document
from frappe.utils import flt

class NKTTruckingWaybill(Document):
    def validate(self):
        self._fill_from_trip()
        if not self.driver_name:
            frappe.throw("Driver is required on every trucking Waybill.")
        total = 0
        for row in self.items or []:
            row.line_total = flt(row.qty) * flt(row.unit_price)
            total += flt(row.line_total)
        self.grand_total = total

    def _fill_from_trip(self):
        if not self.trip:
            return
        trip = frappe.get_doc("NKT Trucking Trip", self.trip)
        mapping = {
            "origin": trip.origin,
            "destination": trip.destination,
            "reference_dr": trip.dr_no,
            "driver_name": trip.driver_name,
            "driver_contact": trip.driver_contact,
            "plate_no": trip.plate_no,
            "departure_datetime": trip.dispatched_at,
            "arrival_datetime": trip.delivered_at,
            "company": trip.company,
        }
        for field, value in mapping.items():
            if not self.get(field) and value:
                self.set(field, value)

    def on_trash(self):
        # Waybill is supporting operational evidence. Allow deletion only while no SOA line cites it in future phases.
        return
