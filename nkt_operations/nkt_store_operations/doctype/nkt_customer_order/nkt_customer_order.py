import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, now_datetime, today
from frappe.utils.password import check_password

from nkt_operations.nkt_store_operations.features.security.role_hierarchy import has_nkt_authority
from nkt_operations.nkt_store_operations.features.inventory.order_fulfillment import (
    cancel_customer_order_fulfillment,
    process_customer_order_fulfillment,
    validate_normal_sale_item,
)
from nkt_operations.nkt_store_operations.features.sales.matching import (
    build_basket_fingerprint,
    build_payment_fingerprint,
    build_warehouse_fingerprint,
    try_match_customer_order,
    unlink_customer_order,
)
from nkt_operations.nkt_store_operations.features.payments_accounts.card_surcharge import (
    apply_payment_row_card_fields,
    ensure_card_posting_allowed,
    row_collected_amount,
)
from nkt_operations.nkt_store_operations.features.payments_accounts.credit import (
    cancel_customer_order_receivable,
    process_customer_order_receivable,
    validate_customer_order_account_credit,
)
from nkt_operations.nkt_store_operations.features.reports_history.receipt_support import (
    ensure_customer_receipt_record,
)


class NKTCustomerOrder(Document):
    def before_validate(self):
        self.set_defaults()
        self.populate_item_details()
        self.calculate_totals()
        self.apply_declared_payment_rules()
        self.calculate_declared_payment_totals()
        self.update_reconciliation_fingerprints()
        self.set_admin_confirmation_requirement()

    def validate(self):
        self.validate_customer()
        self.validate_items()
        self.validate_declared_payments()
        self.validate_warehouses()
        validate_customer_order_account_credit(self)

    def before_submit(self):
        if (
            self.requires_admin_confirmation
            and self.admin_confirmation_status != "Confirmed"
        ):
            self.status = "Pending Admin Confirmation"

        elif self.account_sale:
            self.status = "Pending Credit Control"

        else:
            self.status = "Awaiting Payment"

    def on_submit(self):
        # C15E: preserve the customer-facing Trust Receipt account-balance
        # snapshot before this order can create its own new receivable.
        ensure_customer_receipt_record(self.name)
        try_match_customer_order(self.name)
        process_customer_order_receivable(self.name)
        if not self.flags.get("nkt_c15c_defer_fulfillment"):
            process_customer_order_fulfillment(self.name)

    def on_cancel(self):
        cancel_customer_order_fulfillment(self.name)
        unlink_customer_order(self.name)
        cancel_customer_order_receivable(self.name)

    def set_defaults(self):
        if not self.order_date:
            self.order_date = today()

        # The official encoder is always the logged-in account that prepares
        # the draft. It is never manually selected.
        if (
            (self.is_new() or self.docstatus == 0)
            and not self.flags.get("nkt_c15c_preserve_offline_encoder")
        ):
            self.encoder = frappe.session.user

        if not self.status:
            self.status = "Draft"

        if not self.admin_confirmation_status:
            self.admin_confirmation_status = "Not Required"

        if not self.payment_status:
            self.payment_status = "Unpaid"

        self.amount_paid = flt(self.amount_paid)

    def populate_item_details(self):
        for row in self.get("items") or []:
            if not row.item:
                continue

            item = frappe.db.get_value(
                "Item",
                row.item,
                ["item_name", "stock_uom"],
                as_dict=True,
            )

            if not item:
                frappe.throw(
                    _("Item {0} does not exist.").format(row.item)
                )

            row.item_name = item.item_name
            row.uom = item.stock_uom

            if (
                not row.source_warehouse
                and self.default_warehouse
            ):
                row.source_warehouse = self.default_warehouse

            standard_rate = frappe.db.get_value(
                "Item Price",
                {
                    "item_code": row.item,
                    "price_list": "Standard Selling",
                    "selling": 1,
                },
                "price_list_rate",
            )

            row.standard_rate = flt(standard_rate)

    def calculate_totals(self):
        total_quantity = 0
        grand_total = 0

        for row in self.get("items") or []:
            quantity = flt(row.quantity)
            standard_rate = flt(row.standard_rate)
            price_adjustment = flt(row.price_adjustment)
            special_rate = flt(row.get("custom_nkt_authorized_special_rate"))

            row.quantity = quantity
            row.standard_rate = standard_rate
            # NKT_MANAGER_PIN_ENCODER_RATE_MP1
            # Encoder does not enter a Manager PIN. This hidden field only lets
            # the independent Encoder reproduce a legitimate special Cashier
            # rate so exact matching remains possible.
            row.final_rate = (
                special_rate
                if special_rate > 0.005
                else standard_rate + price_adjustment
            )
            row.amount = quantity * row.final_rate

            total_quantity += quantity
            grand_total += row.amount

        self.total_quantity = total_quantity
        self.grand_total = grand_total

        # Before any submitted payment exists, the full order
        # amount is still due.
        if (
            self.docstatus == 0
            and self.payment_status == "Unpaid"
            and flt(self.amount_paid) <= 0.005
        ):
            self.amount_due = grand_total

    def apply_declared_payment_rules(self):
        methods = []
        for row in self.get("declared_payments") or []:
            apply_payment_row_card_fields(row)
            # C15C.10D R4A: during controlled offline Draft materialization/
            # hydration only, preserve Account / Return Credit as NON-MONEY
            # at the child-row level as well as in order-level totals.
            # Normal online drafts keep the existing legacy behavior because
            # this server-only flag is absent outside the C15C materializers.
            if (
                self.flags.get("nkt_c15c_preserve_offline_encoder")
                and row.payment_method in {"Account", "Return Credit"}
                and row.meta.has_field("collected_amount")
            ):
                row.collected_amount = 0
            if row.payment_method:
                methods.append(row.payment_method)
        self.account_sale = 1 if "Account" in methods else 0
        if len(set(methods)) == 1:
            method = methods[0]
            self.payment_arrangement = "Other Online" if method == "Online" else ("On-Account" if method == "Account" else method)
        elif len(set(methods)) > 1:
            self.payment_arrangement = "Split Payment"

    def calculate_declared_payment_totals(self):
        total = cash = non_cash = account = card_surcharge = collected = 0
        for row in self.get("declared_payments") or []:
            amount = flt(row.amount)
            total += amount
            card_surcharge += flt(row.get("card_surcharge"))
            if row.payment_method not in {"Account", "Return Credit"}:
                collected += row_collected_amount(row)
            if row.payment_method == "Cash":
                cash += amount
            elif row.payment_method == "Account":
                account += amount
            elif row.payment_method == "Return Credit":
                pass
            else:
                non_cash += amount
        self.declared_payment_total = total
        self.declared_cash = cash
        self.declared_non_cash = non_cash
        self.declared_account = account
        if self.meta.has_field("declared_card_surcharge_total"):
            self.declared_card_surcharge_total = card_surcharge
        if self.meta.has_field("declared_total_collected"):
            self.declared_total_collected = collected
        if self.meta.has_field("custom_nkt_return_credit_applied"):
            self.custom_nkt_return_credit_applied = sum(
                flt(row.amount)
                for row in (self.get("declared_payments") or [])
                if row.payment_method == "Return Credit"
            )

    def update_reconciliation_fingerprints(self):
        self.nkt_basket_fingerprint = build_basket_fingerprint(self.get("items") or [])
        self.nkt_payment_fingerprint = build_payment_fingerprint(self.get("declared_payments") or [])
        self.nkt_warehouse_fingerprint = build_warehouse_fingerprint(self.get("items") or [])
        if not self.cashier_reconciliation_status:
            self.cashier_reconciliation_status = "Unmatched"

    def validate_declared_payments(self):
        if int(self.docstatus or 0) == 0 and self.flags.get("nkt_c15c_offline_intent_materialization") and str(self.get("custom_nkt_fast_request_id") or "").strip() and not (self.get("declared_payments") or []): return  # C15C.9H server-only transient materializer guard
        if not self.get("declared_payments"):
            frappe.throw(_("The encoder must record at least one declared payment method."))
        seen_checks = set()
        noncheck_reference_methods = {"GCash", "Maya", "Card", "Bank Transfer", "Online"}
        for row in self.declared_payments:
            ensure_card_posting_allowed(row.payment_method, "Card Encoder declaration")
            if not row.payment_method:
                frappe.throw(_("Payment Method is required on declared-payment row {0}.").format(row.idx))
            if flt(row.amount) <= 0:
                frappe.throw(_("Declared payment amount must be greater than zero on row {0}.").format(row.idx))
            if row.payment_method == "Check":
                check_number = (row.get("custom_nkt_check_number") or row.reference_number or "").strip()
                provider = (row.bank_or_provider or "").strip()
                if not check_number:
                    frappe.throw(_("Check Number is required on declared-payment row {0}.").format(row.idx))
                if not provider:
                    frappe.throw(_("Issuing Bank is required for Check on declared-payment row {0}.").format(row.idx))
                key = (
                    "".join(provider.lower().split()),
                    "".join(check_number.lower().split()),
                )
                if key in seen_checks:
                    frappe.throw(_("Duplicate physical Check in the encoder payment rows: {0} / {1}.").format(provider, check_number))
                seen_checks.add(key)
            elif row.payment_method in noncheck_reference_methods:
                reference = (row.reference_number or "").strip()
                if not reference:
                    frappe.throw(_("Reference Number is required for {0} on declared-payment row {1}.").format(row.payment_method, row.idx))
                # C2.3.1: informal e-payment references are audit text, not uniqueness keys.
                # Repeats are allowed even inside one order because staff may record only a suffix.
        if abs(flt(self.declared_payment_total) - flt(self.grand_total)) > 0.005:
            frappe.throw(_("Encoder declared-payment total must equal the order total. Order total: {0}; declared total: {1}.").format(frappe.format_value(self.grand_total, {"fieldtype": "Currency"}), frappe.format_value(self.declared_payment_total, {"fieldtype": "Currency"})))
    def get_restricted_warehouses(self):
        warehouse_names = sorted(
            {
                row.source_warehouse
                for row in (self.get("items") or [])
                if row.source_warehouse
            }
        )

        if not warehouse_names:
            return set()

        restricted_warehouses = frappe.get_all(
            "Warehouse",
            filters={
                "name": ["in", warehouse_names],
                "custom_requires_nkt_admin_approval": 1,
            },
            pluck="name",
        )

        return set(restricted_warehouses)

    def set_admin_confirmation_requirement(self):
        restricted_warehouses = (
            self.get_restricted_warehouses()
        )

        requires_confirmation = bool(
            restricted_warehouses
        )

        self.requires_admin_confirmation = int(
            requires_confirmation
        )

        if requires_confirmation:
            if (
                self.admin_confirmation_status
                != "Confirmed"
            ):
                self.admin_confirmation_status = "Pending"

        else:
            self.admin_confirmation_status = "Not Required"

            # Clear old approval information when none of the
            # selected warehouses requires approval.
            if self.docstatus == 0:
                self.admin_confirmed_by = None
                self.admin_confirmed_on = None
                self.admin_confirmation_remarks = None

    def validate_customer(self):
        if not self.customer:
            frappe.throw(_("Customer is required."))

    def validate_items(self):
        if not self.get("items"):
            frappe.throw(
                _("At least one item is required.")
            )

        for row in self.items:
            validate_normal_sale_item(row.item, row.idx)
            if not row.item:
                frappe.throw(
                    _(
                        "Item is required on row {0}."
                    ).format(row.idx)
                )

            if flt(row.quantity) <= 0:
                frappe.throw(
                    _(
                        "Quantity must be greater than zero "
                        "on row {0}."
                    ).format(row.idx)
                )

            if not row.uom:
                frappe.throw(
                    _(
                        "UOM is required on row {0}."
                    ).format(row.idx)
                )

            if not row.source_warehouse:
                frappe.throw(
                    _(
                        "Source Warehouse is required "
                        "on row {0}."
                    ).format(row.idx)
                )

            if flt(row.standard_rate) <= 0:
                frappe.throw(
                    _(
                        "No Standard Selling price was found "
                        "for item {0}."
                    ).format(row.item)
                )

            if flt(row.final_rate) < 0:
                frappe.throw(
                    _(
                        "Final Rate cannot be negative "
                        "on row {0}."
                    ).format(row.idx)
                )

    def validate_warehouses(self):
        warehouses = {self.default_warehouse}

        for row in self.get("items") or []:
            warehouses.add(row.source_warehouse)

        for warehouse_name in filter(
            None,
            warehouses,
        ):
            warehouse = frappe.db.get_value(
                "Warehouse",
                warehouse_name,
                [
                    "company",
                    "is_group",
                ],
                as_dict=True,
            )

            if not warehouse:
                frappe.throw(
                    _(
                        "Warehouse {0} does not exist."
                    ).format(warehouse_name)
                )

            if warehouse.is_group:
                frappe.throw(
                    _(
                        "Warehouse {0} is a group warehouse."
                    ).format(warehouse_name)
                )

            if warehouse.company != self.company:
                frappe.throw(
                    _(
                        "Warehouse {0} does not belong "
                        "to company {1}."
                    ).format(
                        warehouse_name,
                        self.company,
                    )
                )


def get_status_after_warehouse_check(
    order,
    requires_approval,
    confirmation_status,
):
    # Never downgrade an already released transaction.
    if order.status == "Released":
        return "Released"

    if (
        requires_approval
        and confirmation_status != "Confirmed"
    ):
        return "Pending Admin Confirmation"

    payment_status = order.payment_status or "Unpaid"

    if payment_status == "Paid":
        return "Ready for Release"

    if payment_status == "Partially Paid":
        return "Partially Paid"

    if payment_status == "Charged to Account":
        return "Pending Credit Control"

    return "Awaiting Payment"


@frappe.whitelist()
def refresh_warehouse_approval(customer_order):
    """
    Recalculate warehouse approval for an existing order.

    This is useful for orders created before the Warehouse
    approval checkbox was introduced.
    """
    order = frappe.get_doc(
        "NKT Customer Order",
        customer_order,
    )

    restricted_warehouses = (
        order.get_restricted_warehouses()
    )

    requires_approval = (
        1 if restricted_warehouses else 0
    )

    if requires_approval:
        if (
            order.admin_confirmation_status
            == "Confirmed"
        ):
            confirmation_status = "Confirmed"
        else:
            confirmation_status = "Pending"

    else:
        confirmation_status = "Not Required"

    order_status = get_status_after_warehouse_check(
        order,
        requires_approval,
        confirmation_status,
    )

    values = {
        "requires_admin_confirmation":
            requires_approval,
        "admin_confirmation_status":
            confirmation_status,
        "status":
            order_status,
    }

    if not requires_approval:
        values.update(
            {
                "admin_confirmed_by": None,
                "admin_confirmed_on": None,
                "admin_confirmation_remarks": None,
            }
        )

    frappe.db.set_value(
        "NKT Customer Order",
        order.name,
        values,
        update_modified=True,
    )

    frappe.db.commit()

    return {
        "requires_admin_confirmation":
            requires_approval,
        "restricted_warehouses":
            sorted(restricted_warehouses),
        "admin_confirmation_status":
            confirmation_status,
        "status":
            order_status,
    }


@frappe.whitelist(methods=["POST"])
def confirm_warehouse_withdrawal(
    customer_order,
    remarks,
):
    """
    Confirm withdrawal only when at least one selected
    warehouse is specifically marked as requiring approval.
    """
    remarks = (remarks or "").strip()

    current_user = frappe.session.user

    if (
        current_user != "Administrator"
        and "NKT Admin"
        not in frappe.get_roles(current_user)
    ):
        frappe.throw(
            _(
                "Only an NKT Admin can confirm a restricted "
                "warehouse withdrawal."
            ),
            frappe.PermissionError,
        )

    if not remarks:
        frappe.throw(
            _("Confirmation Remarks are required.")
        )

    order = frappe.get_doc(
        "NKT Customer Order",
        customer_order,
        for_update=True,
    )

    if order.docstatus != 1:
        frappe.throw(
            _(
                "The Customer Order must be submitted first."
            )
        )

    restricted_warehouses = (
        order.get_restricted_warehouses()
    )

    if not restricted_warehouses:
        frappe.throw(
            _(
                "None of the selected warehouses requires "
                "NKT Admin approval."
            )
        )

    if (
        order.admin_confirmation_status
        == "Confirmed"
    ):
        return {
            "confirmed": True,
            "status": order.status,
            "restricted_warehouses":
                sorted(restricted_warehouses),
        }

    new_status = get_status_after_warehouse_check(
        order,
        requires_approval=1,
        confirmation_status="Confirmed",
    )

    frappe.db.set_value(
        "NKT Customer Order",
        order.name,
        {
            "requires_admin_confirmation": 1,
            "admin_confirmation_status": "Confirmed",
            "admin_confirmed_by": current_user,
            "admin_confirmed_on": now_datetime(),
            "admin_confirmation_remarks": remarks,
            "status": new_status,
        },
        update_modified=True,
    )

    return {
        "confirmed": True,
        "status": new_status,
        "confirmed_by": current_user,
        "restricted_warehouses":
            sorted(restricted_warehouses),
    }


# NKT_WAREHOUSE_OVERRIDE_API_V2
@frappe.whitelist(methods=["POST"])
def approve_warehouse_withdrawal(
    customer_order,
    admin_user=None,
    admin_password=None,
    reason=None,
):
    reason = (reason or "").strip()

    if not reason:
        frappe.throw(_("Override Reason is required."))

    order = frappe.get_doc(
        "NKT Customer Order",
        customer_order,
        for_update=True,
    )

    # The requesting user must at least have access to this order.
    order.check_permission("read")

    if order.docstatus != 1:
        frappe.throw(
            _("The Customer Order must be submitted first.")
        )

    if order.status == "Released":
        frappe.throw(
            _("A released Customer Order cannot be approved again.")
        )

    restricted_warehouses = (
        order.get_restricted_warehouses()
    )

    if not restricted_warehouses:
        frappe.throw(
            _(
                "This order does not use a warehouse requiring "
                "Admin confirmation."
            )
        )

    if order.admin_confirmation_status == "Confirmed":
        return {
            "customer_order": order.name,
            "status": order.status,
            "admin_confirmation_status": "Confirmed",
            "already_confirmed": 1,
        }

    current_user = frappe.session.user

    if has_nkt_authority(10, current_user):
        authenticated_user = current_user
    else:
        admin_user = (admin_user or "").strip()

        if not admin_user:
            frappe.throw(
                _(
                    "Owner or Administrator Username "
                    "is required."
                )
            )

        if not admin_password:
            frappe.throw(
                _(
                    "Owner or Administrator Password "
                    "is required."
                )
            )

        authenticated_user = check_password(
            admin_user,
            admin_password,
        )

        enabled = frappe.db.get_value(
            "User",
            authenticated_user,
            "enabled",
        )

        if not enabled:
            frappe.throw(
                _(
                    "The approving Owner or Administrator "
                    "account is disabled."
                )
            )

        if not has_nkt_authority(
            10,
            authenticated_user,
        ):
            frappe.throw(
                _(
                    "The supplied user is not authorized "
                    "to approve restricted warehouse "
                    "withdrawals."
                )
            )

    new_status = get_status_after_warehouse_check(
        order,
        True,
        "Confirmed",
    )

    values = {
        "requires_admin_confirmation": 1,
        "admin_confirmation_status": "Confirmed",
        "admin_confirmed_by": authenticated_user,
        "admin_confirmed_on": now_datetime(),
        "admin_confirmation_remarks": reason,
        "status": new_status,
    }

    frappe.db.set_value(
        "NKT Customer Order",
        order.name,
        values,
        update_modified=True,
    )

    order.add_comment(
        "Info",
        _(
            "Restricted warehouse withdrawal approved by {0}. "
            "Reason: {1}"
        ).format(
            authenticated_user,
            reason,
        ),
    )

    return {
        "customer_order": order.name,
        "status": new_status,
        "admin_confirmation_status": "Confirmed",
        "admin_confirmed_by": authenticated_user,
    }


@frappe.whitelist()
def get_encoder_item_context(item_code):
    user = frappe.session.user
    roles = set(frappe.get_roles(user))
    if not has_nkt_authority(10, user) and "NKT Encoder" not in roles:
        frappe.throw(_("Only an NKT Encoder can retrieve order pricing."), frappe.PermissionError)

    item = frappe.db.get_value(
        "Item",
        item_code,
        ["item_name", "stock_uom", "disabled"],
        as_dict=True,
    )
    if not item or item.disabled:
        frappe.throw(_("Item {0} is unavailable.").format(item_code))

    standard_rate = frappe.db.get_value(
        "Item Price",
        {
            "item_code": item_code,
            "price_list": "Standard Selling",
            "selling": 1,
        },
        "price_list_rate",
    )

    return {
        "item_name": item.item_name,
        "stock_uom": item.stock_uom,
        "standard_rate": flt(standard_rate),
    }
