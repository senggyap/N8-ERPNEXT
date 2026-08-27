import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime, nowdate

from nkt_operations.nkt_store_operations.features.returns.reversal import (
    _assert_authority,
    execute_reversal,
    prepare_reversal_document,
    validate_reversal_document,
)


class NKTReturnExchangeReversal(Document):
    def before_validate(self):
        _assert_authority()
        if not self.reversal_datetime:
            self.reversal_datetime = now_datetime()
        self.business_date = nowdate()
        self.requested_by = frappe.session.user
        if not self.reversal_status:
            self.reversal_status = "Draft"
        if self.original_cashier_declaration or self.original_encoder_declaration:
            prepare_reversal_document(self)

    def validate(self):
        _assert_authority()
        validate_reversal_document(self, for_submit=False)

    def before_submit(self):
        # C7.13C production unlock. The complete C7.13B R14 rollback suite is
        # accepted; server-side blockers/confirmations/references remain the
        # authoritative safety gate.
        self.reversal_datetime = now_datetime()
        self.business_date = nowdate()
        self.requested_by = frappe.session.user
        validate_reversal_document(self, for_submit=True)

    def on_submit(self):
        execute_reversal(self)
