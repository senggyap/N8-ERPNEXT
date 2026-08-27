from frappe.model.document import Document

from nkt_operations.nkt_store_operations.features.cashier.reconciliation import (
    finalize_reconciliation,
    validate_reconciliation,
)


class NKTEODReconciliation(Document):
    def validate(self):
        validate_reconciliation(self)
        if self.docstatus == 0:
            self.status = "Draft"

    def before_submit(self):
        finalize_reconciliation(self)

    def on_cancel(self):
        self.db_set("status", "Cancelled", update_modified=False)
