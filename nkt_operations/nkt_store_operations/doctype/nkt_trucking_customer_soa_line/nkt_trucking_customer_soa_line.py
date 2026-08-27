from frappe.model.document import Document
from frappe.utils import flt

class NKTTruckingCustomerSOALine(Document):
    def validate(self):
        if not self.manual_amount_override:
            self.amount = flt(self.qty) * flt(self.rate)
