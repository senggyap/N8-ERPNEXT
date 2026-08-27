from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


PROTECTED_FIELDS = (
    "pin_salt",
    "pin_hash",
    "pin_iterations",
    "pin_configured",
)


class NKTSellingPriceAuthorizer(Document):
    def validate(self):
        if not self.user or not frappe.db.exists("User", self.user):
            frappe.throw(_("Select an existing User."))

        internal = bool(self.flags.get("nkt_manager_pin_internal"))
        if internal:
            return

        # Human/UI saves may create an inactive shell record, but credential
        # activation/change must always go through the server PIN setter.
        if self.is_new():
            if cint(self.can_authorize_selling_price_adjustments) or cint(self.pin_configured):
                frappe.throw(_("Use Set / Change 5-Digit PIN to activate this authorizer."))
            return

        old = frappe.db.get_value(
            self.doctype,
            self.name,
            ["user", "can_authorize_selling_price_adjustments", *PROTECTED_FIELDS],
            as_dict=True,
        )
        if not old:
            return
        if str(old.user or "") != str(self.user or ""):
            frappe.throw(_("The User on a selling-price authorizer cannot be changed. Create a new inactive record instead."))

        for fieldname in PROTECTED_FIELDS:
            if str(old.get(fieldname) or "") != str(self.get(fieldname) or ""):
                frappe.throw(_("Protected Manager PIN credential fields cannot be edited directly."))

        if not cint(old.can_authorize_selling_price_adjustments) and cint(self.can_authorize_selling_price_adjustments):
            frappe.throw(_("Use Set / Change 5-Digit PIN to enable selling-price authorization."))

    def on_update(self):
        if frappe.get_meta("User").has_field("custom_nkt_can_authorize_selling_price_adjustments"):
            frappe.db.set_value(
                "User",
                self.user,
                "custom_nkt_can_authorize_selling_price_adjustments",
                1 if cint(self.can_authorize_selling_price_adjustments) else 0,
                update_modified=False,
            )
