import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt, get_datetime, now_datetime
from nkt_operations.nkt_store_operations.features.security.role_hierarchy import (
    has_nkt_authority,
)


TOLERANCE = 0.005
ACTIVE_STATUSES = ("Open",)
DENOMINATION_FIELDS = {
    "bill_1000_qty": 1000.0,
    "bill_500_qty": 500.0,
    "bill_200_qty": 200.0,
    "bill_100_qty": 100.0,
    "bill_50_qty": 50.0,
    "bill_20_qty": 20.0,
    "coin_20_qty": 20.0,
    "coin_10_qty": 10.0,
    "coin_5_qty": 5.0,
    "coin_1_qty": 1.0,
    "coin_025_qty": 0.25,
}


class NKTCashierShift(Document):
    def before_validate(self):
        if self.is_new():
            self.cashier = frappe.session.user
            if not self.status:
                self.status = "Not Opened"

        self.calculate_totals()

    def validate(self):
        self.validate_location()
        self.validate_operator_locked()
        self.validate_open_shift_uniqueness()
        self.validate_opened_fields_locked()
        self.validate_count_fields()

    def before_submit(self):
        if self.status != "Turned Over - Awaiting Review":
            frappe.throw(
                _(
                    "Record and turn over the denomination count before "
                    "the shift can be marked Reviewed / OK."
                )
            )

        if not cint(self.blind_count_confirmed):
            frappe.throw(_("Denomination count has not been recorded."))

        if not self.approved_by or not self.approved_on:
            frappe.throw(_("Administrator review is required."))

        counted_total = self.get_denomination_total()
        if abs(counted_total - flt(self.actual_cash_count)) > TOLERANCE:
            frappe.throw(
                _(
                    "Recorded denomination total does not match "
                    "Actual Cash."
                )
            )

        self.calculate_totals()
        self.actual_cash_count = counted_total
        self.over_short = counted_total - flt(self.expected_cash)
        if abs(flt(self.over_short)) > TOLERANCE and not (self.count_notes or "").strip():
            frappe.throw(_("An explanation is required for a cash overage or shortage."))

        self.status = "Reviewed / Closed"
        self.turnover_status = "Reviewed / OK"
        self.turnover_amount = counted_total
        self.turnover_confirmed_by = self.approved_by
        self.turnover_confirmed_on = self.approved_on
        self.closed_by = self.approved_by
        self.closed_on = self.approved_on

    def before_cancel(self):
        movement_count = frappe.db.count(
            "NKT Cashier Movement",
            {
                "cashier_shift": self.name,
                "docstatus": 1,
            },
        )

        if movement_count:
            frappe.throw(
                _(
                    "Cancel the submitted cashier movements "
                    "before cancelling this shift."
                )
            )

    def on_cancel(self):
        self.db_set("status", "Cancelled", update_modified=False)

    def validate_location(self):
        warehouse = frappe.db.get_value(
            "Warehouse",
            self.settlement_location,
            ["is_group", "disabled", "company"],
            as_dict=True,
        )

        if not warehouse:
            frappe.throw(_("Settlement Location was not found."))
        if warehouse.is_group:
            frappe.throw(_("Settlement Location must be a leaf warehouse."))
        if warehouse.disabled:
            frappe.throw(_("Settlement Location is disabled."))
        if warehouse.company != self.company:
            frappe.throw(_("Settlement Location belongs to another company."))

    def validate_cash_register(self):
        register = frappe.db.get_value(
            "NKT Cash Register",
            self.cash_register,
            ["company", "settlement_location", "disabled"],
            as_dict=True,
        )

        if not register:
            frappe.throw(_("Cash Register / Drawer was not found."))
        if cint(register.disabled):
            frappe.throw(_("Cash Register / Drawer is disabled."))
        if register.company != self.company:
            frappe.throw(_("Cash Register belongs to another company."))
        if register.settlement_location != self.settlement_location:
            frappe.throw(
                _(
                    "Cash Register does not belong to the selected "
                    "Settlement Location."
                )
            )

    def validate_operator_locked(self):
        previous = self.get_doc_before_save()
        if previous and previous.cashier != self.cashier:
            frappe.throw(_("Shift Operator cannot be changed."))

    def validate_opened_fields_locked(self):
        previous = self.get_doc_before_save()
        if not previous or previous.status == "Not Opened":
            return

        locked_fields = (
            "company",
            "settlement_location",
            "cashier",
            "opening_cash",
            "shift_start",
        )
        for fieldname in locked_fields:
            current_value = self.get(fieldname)
            previous_value = previous.get(fieldname)

            if fieldname == "shift_start":
                current_value = get_datetime(current_value) if current_value else None
                previous_value = get_datetime(previous_value) if previous_value else None
            elif fieldname == "opening_cash":
                current_value = flt(current_value)
                previous_value = flt(previous_value)
            else:
                current_value = cstr(current_value or "")
                previous_value = cstr(previous_value or "")

            if current_value != previous_value:
                frappe.throw(
                    _("{0} cannot be changed after opening the shift.").format(
                        self.meta.get_label(fieldname)
                    )
                )

    def validate_open_shift_uniqueness(self):
        if self.docstatus != 0 or self.status not in ACTIVE_STATUSES:
            return

        user_shift = frappe.db.get_value(
            "NKT Cashier Shift",
            {
                "name": ["!=", self.name or ""],
                "docstatus": 0,
                "status": ["in", ACTIVE_STATUSES],
                "cashier": self.cashier,
            },
            "name",
        )
        if user_shift:
            frappe.throw(
                _(
                    "Shift Operator {0} already has active shift {1}."
                ).format(self.cashier, user_shift)
            )

    def validate_count_fields(self):
        if flt(self.opening_cash) < -TOLERANCE:
            frappe.throw(_("Opening Cash cannot be negative."))

        for fieldname in DENOMINATION_FIELDS:
            quantity = cint(self.get(fieldname) or 0)
            if quantity < 0:
                frappe.throw(
                    _("{0} cannot be negative.").format(
                        self.meta.get_label(fieldname)
                    )
                )

        if cint(self.blind_count_confirmed):
            counted_total = self.get_denomination_total()
            self.actual_cash_count = counted_total
            self.over_short = counted_total - flt(self.expected_cash)
        else:
            self.actual_cash_count = 0
            self.over_short = 0

    def get_denomination_total(self):
        return sum(
            cint(self.get(fieldname) or 0) * value
            for fieldname, value in DENOMINATION_FIELDS.items()
        )

    def calculate_totals(self):
        if not self.name or self.is_new():
            cash_in = cash_out = non_cash_in = non_cash_out = 0
        else:
            rows = frappe.db.sql(
                """
                SELECT
                    COALESCE(SUM(CASE
                        WHEN affects_cash_drawer = 1
                         AND direction = 'In'
                        THEN amount ELSE 0 END), 0) AS cash_in,
                    COALESCE(SUM(CASE
                        WHEN affects_cash_drawer = 1
                         AND direction = 'Out'
                        THEN amount ELSE 0 END), 0) AS cash_out,
                    COALESCE(SUM(CASE
                        WHEN affects_cash_drawer = 0
                         AND direction = 'In'
                        THEN amount ELSE 0 END), 0) AS non_cash_in,
                    COALESCE(SUM(CASE
                        WHEN affects_cash_drawer = 0
                         AND direction = 'Out'
                        THEN amount ELSE 0 END), 0) AS non_cash_out
                FROM `tabNKT Cashier Movement`
                WHERE cashier_shift = %s
                  AND docstatus = 1
                """,
                self.name,
                as_dict=True,
            )[0]
            cash_in = flt(rows.cash_in)
            cash_out = flt(rows.cash_out)
            non_cash_in = flt(rows.non_cash_in)
            non_cash_out = flt(rows.non_cash_out)

        self.total_cash_in = cash_in
        self.total_cash_out = cash_out
        self.total_non_cash_in = non_cash_in
        self.total_non_cash_out = non_cash_out
        self.expected_cash = flt(self.opening_cash) + cash_in - cash_out

        if cint(self.blind_count_confirmed):
            self.actual_cash_count = self.get_denomination_total()
            self.over_short = (
                flt(self.actual_cash_count) - flt(self.expected_cash)
            )

    def refresh_totals_to_db(self):
        if not self.name:
            return

        self.calculate_totals()
        values = {
            "total_cash_in": self.total_cash_in,
            "total_cash_out": self.total_cash_out,
            "total_non_cash_in": self.total_non_cash_in,
            "total_non_cash_out": self.total_non_cash_out,
            "expected_cash": self.expected_cash,
            "actual_cash_count": self.actual_cash_count,
            "over_short": self.over_short,
        }
        frappe.db.set_value(
            "NKT Cashier Shift",
            self.name,
            values,
            update_modified=False,
        )


@frappe.whitelist()
def open_shift(cashier_shift):
    shift = frappe.get_doc("NKT Cashier Shift", cashier_shift)
    shift.check_permission("write")

    if shift.docstatus != 0 or shift.status != "Not Opened":
        frappe.throw(_("Only a Not Opened draft shift can be opened."))

    if shift.cashier != frappe.session.user:
        frappe.throw(
            _("Only the recorded Shift Operator can open this shift.")
        )

    shift.validate_location()
    shift.status = "Open"
    shift.shift_start = now_datetime()
    shift.validate_open_shift_uniqueness()
    shift.flags.ignore_permissions = True
    shift.save()

    return {
        "name": shift.name,
        "status": shift.status,
        "cashier": shift.cashier,
        "shift_start": shift.shift_start,
    }


@frappe.whitelist()
def record_cash_count(cashier_shift, denominations, count_notes=None):
    shift = frappe.get_doc("NKT Cashier Shift", cashier_shift)
    shift.check_permission("write")

    if shift.docstatus != 0 or shift.status != "Open":
        frappe.throw(_("Only an Open shift can be counted and turned over."))

    if shift.cashier != frappe.session.user:
        frappe.throw(
            _("Only the Shift Operator can enter the cash count.")
        )

    if isinstance(denominations, str):
        denominations = json.loads(denominations or "{}")
    denominations = denominations or {}

    for fieldname in DENOMINATION_FIELDS:
        quantity = cint(denominations.get(fieldname) or 0)
        if quantity < 0:
            frappe.throw(_("Denomination quantities cannot be negative."))
        shift.set(fieldname, quantity)

    count_notes = (count_notes or "").strip()
    shift.calculate_totals()
    counted_total = shift.get_denomination_total()
    difference = counted_total - flt(shift.expected_cash)
    if abs(difference) > TOLERANCE and not count_notes:
        frappe.throw(_("An explanation is required for a cash overage or shortage."))

    shift.count_notes = count_notes
    shift.blind_count_confirmed = 1
    shift.count_locked_by = frappe.session.user
    shift.count_locked_on = now_datetime()
    shift.status = "Turned Over - Awaiting Review"
    shift.shift_end = now_datetime()
    shift.calculate_totals()
    shift.actual_cash_count = shift.get_denomination_total()
    shift.over_short = (
        flt(shift.actual_cash_count) - flt(shift.expected_cash)
    )
    shift.turnover_status = "Turned Over - Awaiting Review"
    shift.turnover_amount = shift.actual_cash_count
    shift.flags.ignore_permissions = True
    shift.save()

    return {
        "status": shift.status,
        "actual_cash_count": shift.actual_cash_count,
        "expected_cash": shift.expected_cash,
        "over_short": shift.over_short,
    }


@frappe.whitelist()
def lock_blind_count(cashier_shift, denominations, count_notes=None):
    """Backward-compatible alias for clients installed before Shift v4."""
    return record_cash_count(cashier_shift, denominations, count_notes)


@frappe.whitelist()
def get_shift_review_mode():
    return {
        "can_review": has_nkt_authority(10, frappe.session.user),
        "current_user": frappe.session.user,
    }


@frappe.whitelist()
def mark_shift_reviewed(cashier_shift, reviewed_ok=0, review_note=None):
    if not cint(reviewed_ok):
        frappe.throw(_("Tick Reviewed and OK before confirming."))

    current_user = frappe.session.user
    if not has_nkt_authority(10, current_user):
        frappe.throw(
            _("Only NKT Owner, NKT Administrator, or Administrator can review a shift.")
        )

    shift = frappe.get_doc("NKT Cashier Shift", cashier_shift)
    if shift.docstatus != 0 or shift.status != "Turned Over - Awaiting Review":
        frappe.throw(_("This shift is not awaiting administrator review."))

    shift.approval_reason = (review_note or "").strip()
    shift.approved_by = current_user
    shift.approved_on = now_datetime()
    shift.flags.ignore_permissions = True
    shift.submit()

    return {
        "name": shift.name,
        "status": shift.status,
        "approved_by": current_user,
        "actual_cash_count": shift.actual_cash_count,
        "expected_cash": shift.expected_cash,
        "over_short": shift.over_short,
    }


# Backward-compatible aliases for clients installed before Shift v5.
@frappe.whitelist()
def get_shift_approval_mode():
    mode = get_shift_review_mode()
    return {
        "direct_approval": mode["can_review"],
        "current_user": mode["current_user"],
    }


@frappe.whitelist()
def approve_and_close_shift(
    cashier_shift,
    approval_reason=None,
    admin_user=None,
    admin_password=None,
):
    return mark_shift_reviewed(
        cashier_shift=cashier_shift,
        reviewed_ok=1,
        review_note=approval_reason,
    )


@frappe.whitelist()
def refresh_shift_totals(cashier_shift):
    shift = frappe.get_doc("NKT Cashier Shift", cashier_shift)
    shift.check_permission("read")
    shift.refresh_totals_to_db()
    return frappe.db.get_value(
        "NKT Cashier Shift",
        cashier_shift,
        [
            "total_cash_in",
            "total_cash_out",
            "total_non_cash_in",
            "total_non_cash_out",
            "expected_cash",
            "actual_cash_count",
            "over_short",
        ],
        as_dict=True,
    )
