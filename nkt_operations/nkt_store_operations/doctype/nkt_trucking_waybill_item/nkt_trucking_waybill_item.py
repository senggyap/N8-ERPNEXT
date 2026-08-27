from frappe.model.document import Document
from frappe.utils import flt

class NKTTruckingWaybillItem(Document):
    def validate(self):
        self.line_total = flt(self.qty) * flt(self.unit_price)
