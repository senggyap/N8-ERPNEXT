from frappe.model.document import Document

from nkt_operations.nkt_store_operations.features.returns.matching import (
    prepare_declaration,
    validate_declaration,
    submit_declaration,
    cancel_declaration,
)


class NKTReturnExchangeDeclaration(Document):
    def before_validate(self):
        prepare_declaration(self)

    def validate(self):
        validate_declaration(self)

    def before_submit(self):
        prepare_declaration(self)
        validate_declaration(self)

    def on_submit(self):
        submit_declaration(self)

    def on_cancel(self):
        cancel_declaration(self)
