
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate, now_datetime

from nkt_operations.nkt_store_operations.features.payments_accounts.cash_ledger import (
    cancel_source_cashier_movements,
    create_cashier_movement,
    get_open_shift_for_user,
    validate_cashier_shift,
)
from nkt_operations.nkt_store_operations.features.security.role_hierarchy import has_nkt_authority
from nkt_operations.nkt_store_operations.features.sales.matching import (
    build_basket_fingerprint,
    build_payment_fingerprint,
    build_warehouse_fingerprint,
    ensure_cash_basis_payment_receipt,
    try_match_cashier_sale,
    unmatch_cashier_sale,
)
from nkt_operations.nkt_store_operations.features.inventory.order_fulfillment import validate_normal_sale_item
from nkt_operations.nkt_store_operations.features.payments_accounts.card_surcharge import (
    apply_payment_row_card_fields,
    ensure_card_posting_allowed,
    row_collected_amount,
)

REFERENCE_METHODS = {"GCash", "Maya", "Card", "Bank Transfer", "Online"}
TOLERANCE = 0.005
from nkt_operations.nkt_store_operations.features.payments_accounts.credit import (
    validate_cashier_sale_account_credit,
)


class NKTCashierSale(Document):
    def before_validate(self):
        self.set_defaults()
        self.assign_active_shift()
        self.populate_item_details()
        self.calculate_totals()
        self.apply_payment_rules()
        self.calculate_payment_totals()
        self.update_fingerprints()

    def validate(self):
        self.validate_cashier_user()
        self.validate_customer()
        self.validate_items()
        self.validate_price_authorization()
        self.validate_warehouses()
        self.validate_payment_rows()
        self.validate_payment_total()
        self.validate_duplicate_references()
        self.validate_shift()
        validate_cashier_sale_account_credit(self)

    def before_submit(self):
        self.before_validate()
        self.validate()
        self.status = "Submitted - Unmatched"
        self.reconciliation_status = "Unmatched"

    def on_submit(self):
        # C15C.10D controlled offline Tender materialization happens only
        # after 10B Payment Receipt and 10C Cashier Movement are authoritative.
        # Never duplicate those effects in this server-only path.
        if not self.flags.get("nkt_c15c_existing_tender_effects"):
            self.create_cashier_movements()
            receipt_name = ensure_cash_basis_payment_receipt(self.name)
            if receipt_name:
                self.db_set("linked_payment_receipt", receipt_name, update_modified=False)
        try_match_cashier_sale(self.name)

    def on_cancel(self):
        # C7.13B controlled reversal preserves historical Cashier Movements.
        # The reversal engine posts opposite movements into the CURRENT open
        # shift instead of rewriting/cancelling a closed historical shift.
        controlled_reversal = bool(self.flags.get("nkt_controlled_reversal"))
        if not controlled_reversal:
            cancel_source_cashier_movements(self.doctype, self.name)
        unmatch_cashier_sale(self.name)
        self.db_set("status", "Cancelled", update_modified=False)
        self.db_set("reconciliation_status", "Cancelled", update_modified=False)

    def set_defaults(self):
        if not self.sale_datetime:
            self.sale_datetime = now_datetime()
        if (
            (self.is_new() or self.docstatus == 0)
            and not self.flags.get("nkt_c15c_preserve_offline_cashier")
        ):
            self.cashier = frappe.session.user
        if not self.status:
            self.status = "Draft"
        if not self.reconciliation_status:
            self.reconciliation_status = "Unmatched"
        if self.customer:
            self.customer_name = frappe.db.get_value("Customer", self.customer, "customer_name")

    def validate_cashier_user(self):
        current_user = frappe.session.user
        if self.flags.get("nkt_c15c_preserve_offline_cashier"):
            origin_roles = set(frappe.get_roles(self.cashier))
            if not has_nkt_authority(10, self.cashier) and "NKT Cashier" not in origin_roles:
                frappe.throw(_("Offline Tender origin is not an authorized NKT Cashier."), frappe.PermissionError)
            return
        if self.cashier != current_user and not has_nkt_authority(10, current_user):
            frappe.throw(_("The cashier must be the logged-in user."), frappe.PermissionError)
        roles = set(frappe.get_roles(current_user))
        if not has_nkt_authority(10, current_user) and "NKT Cashier" not in roles:
            frappe.throw(_("Only an NKT Cashier can create a cashier-side sale."), frappe.PermissionError)

    def assign_active_shift(self):
        if self.flags.get("nkt_c15c_preserve_offline_cashier"):
            return
        # The cashier never selects these values. Their one Open shift controls
        # company, business date, settlement location and default warehouse.
        shift = get_open_shift_for_user(
            company=self.company or None,
            user=self.cashier or frappe.session.user,
        )
        if not shift and not self.company:
            shift = get_open_shift_for_user(user=self.cashier or frappe.session.user)
        if not shift:
            frappe.throw(
                _("{0} must have exactly one Open Cashier Shift before encoding a cashier sale.").format(
                    self.cashier or frappe.session.user
                )
            )

        self.company = shift.company
        self.cashier = shift.cashier
        self.cashier_shift = shift.name
        self.settlement_location = shift.settlement_location
        self.default_warehouse = shift.settlement_location
        self.business_date = getdate(shift.shift_start)

    def validate_shift(self):
        validate_cashier_shift(
            cashier_shift=self.cashier_shift,
            company=self.company,
            settlement_location=self.settlement_location,
            cashier=self.cashier,
            require_open=not bool(self.flags.get("nkt_c15c_preserve_offline_cashier")),
        )

    def populate_item_details(self):
        for row in self.get("items") or []:
            if not row.item:
                continue
            item = frappe.db.get_value("Item", row.item, ["item_name", "stock_uom"], as_dict=True)
            if not item:
                frappe.throw(_("Item {0} does not exist.").format(row.item))
            row.item_name = item.item_name
            row.uom = item.stock_uom
            if not row.source_warehouse and self.default_warehouse:
                row.source_warehouse = self.default_warehouse
            standard_rate = frappe.db.get_value(
                "Item Price",
                {"item_code": row.item, "price_list": "Standard Selling", "selling": 1},
                "price_list_rate",
            )
            row.standard_rate = flt(standard_rate)

    def calculate_totals(self):
        total_quantity = 0
        grand_total = 0
        for row in self.get("items") or []:
            row.quantity = flt(row.quantity)
            row.standard_rate = flt(row.standard_rate)
            # price_adjustment is a Select field whose accepted values are
            # canonical strings: "0", "-5", "-10", "-15", "-20",
            # "5", "10", "15", "20". Use a numeric local variable for
            # arithmetic but do NOT overwrite the Select value with a float
            # such as -5.0, which Frappe correctly rejects as not in options.
            price_adjustment = flt(row.price_adjustment)
            special_rate = flt(row.get("custom_nkt_authorized_special_rate"))
            row.final_rate = (
                special_rate
                if special_rate > TOLERANCE
                else row.standard_rate + price_adjustment
            )
            row.amount = row.quantity * row.final_rate
            total_quantity += row.quantity
            grand_total += row.amount
        self.total_quantity = total_quantity
        self.grand_total = grand_total

    def apply_payment_rules(self):
        for row in self.get("payments") or []:
            apply_payment_row_card_fields(row)
            if (
                self.flags.get("nkt_c15c_preserve_offline_cashier")
                and row.payment_method in {"Account", "Return Credit"}
                and row.meta.has_field("collected_amount")
            ):
                row.collected_amount = 0
            row.verification_status = "Not Required"
            if row.payment_method == "Cash":
                row.affects_cash_drawer = 1
                if flt(row.cash_tendered) <= TOLERANCE and flt(row.amount) > TOLERANCE:
                    row.cash_tendered = flt(row.amount)
                row.change_amount = max(flt(row.cash_tendered) - flt(row.amount), 0)
            else:
                row.affects_cash_drawer = 0
                row.cash_tendered = 0
                row.change_amount = 0

    def calculate_payment_totals(self):
        total = cash = non_cash = account = card_surcharge = collected = 0
        for row in self.get("payments") or []:
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
        self.total_payment = total
        self.total_cash = cash
        self.total_non_cash = non_cash
        self.total_account_charge = account
        if self.meta.has_field("card_surcharge_total"):
            self.card_surcharge_total = card_surcharge
        if self.meta.has_field("total_collected"):
            self.total_collected = collected
        if self.meta.has_field("custom_nkt_return_credit_applied"):
            self.custom_nkt_return_credit_applied = sum(
                flt(row.amount)
                for row in (self.get("payments") or [])
                if row.payment_method == "Return Credit"
            )

    def update_fingerprints(self):
        self.nkt_basket_fingerprint = build_basket_fingerprint(self.get("items") or [])
        self.nkt_payment_fingerprint = build_payment_fingerprint(self.get("payments") or [])
        self.nkt_warehouse_fingerprint = build_warehouse_fingerprint(self.get("items") or [])

    def validate_customer(self):
        if not self.customer:
            frappe.throw(_("Customer is required."))

    def validate_items(self):
        if not self.get("items"):
            frappe.throw(_("At least one item is required."))
        for row in self.items:
            validate_normal_sale_item(row.item, row.idx)
            if not row.item:
                frappe.throw(_("Item is required on row {0}.").format(row.idx))
            if flt(row.quantity) <= 0:
                frappe.throw(_("Quantity must be greater than zero on row {0}.").format(row.idx))
            if not row.uom:
                frappe.throw(_("UOM is required on row {0}.").format(row.idx))
            if not row.source_warehouse:
                frappe.throw(_("Source Warehouse is required on row {0}.").format(row.idx))
            if flt(row.standard_rate) <= 0:
                frappe.throw(_("No Standard Selling price was found for item {0}.").format(row.item))
            if flt(row.final_rate) < 0:
                frappe.throw(_("Final Rate cannot be negative on row {0}.").format(row.idx))

    def validate_price_authorization(self):
        # NKT_MANAGER_PIN_CASHIER_CONTROLLER_MP1
        # The Fast Screen supplies transient signed evidence through doc.flags;
        # an ordinary form/API caller cannot persist or forge that flag.
        from nkt_operations.nkt_store_operations import manager_authorization as nkt_manager_pin

        nkt_manager_pin.validate_document_authorization_evidence(self)

    def validate_warehouses(self):
        warehouses = {self.default_warehouse}
        warehouses.update(row.source_warehouse for row in (self.get("items") or []) if row.source_warehouse)
        for warehouse_name in filter(None, warehouses):
            warehouse = frappe.db.get_value("Warehouse", warehouse_name, ["company", "is_group"], as_dict=True)
            if not warehouse:
                frappe.throw(_("Warehouse {0} does not exist.").format(warehouse_name))
            if warehouse.is_group:
                frappe.throw(_("Warehouse {0} is a group warehouse.").format(warehouse_name))
            if warehouse.company != self.company:
                frappe.throw(_("Warehouse {0} does not belong to company {1}.").format(warehouse_name, self.company))

    def validate_payment_rows(self):
        if not self.get("payments"):
            frappe.throw(_("At least one payment row is required."))
        for row in self.payments:
            ensure_card_posting_allowed(row.payment_method, "Card Cashier Sale")
            method = row.payment_method
            amount = flt(row.amount)
            if not method:
                frappe.throw(_("Payment Method is required on row {0}.").format(row.idx))
            if amount <= 0:
                frappe.throw(_("Amount must be greater than zero on row {0}.").format(row.idx))
            if method == "Cash" and flt(row.cash_tendered) < amount:
                frappe.throw(_("Cash Tendered cannot be less than the payment amount on row {0}.").format(row.idx))
            if method == "Check" and not (row.check_number or row.reference_number):
                frappe.throw(_("Check Number is required on row {0}.").format(row.idx))
            if method in REFERENCE_METHODS and not row.reference_number:
                frappe.throw(_("Reference Number is required for {0} on row {1}.").format(method, row.idx))

    def validate_payment_total(self):
        if abs(flt(self.total_payment) - flt(self.grand_total)) > TOLERANCE:
            frappe.throw(_("Cashier payment total must equal the sale total. Sale total: {0}; payment total: {1}.").format(frappe.format_value(self.grand_total, {"fieldtype": "Currency"}), frappe.format_value(self.total_payment, {"fieldtype": "Currency"})))

    def validate_duplicate_references(self):
        # C2.3.1: only physical incoming Checks are uniqueness-controlled.
        # Informal GCash/Maya/Bank Transfer/Online references may repeat.
        seen_checks = set()
        for row in self.get("payments") or []:
            if row.payment_method != "Check":
                continue
            check_number = (row.check_number or row.reference_number or "").strip()
            provider = (row.bank_or_provider or "").strip()
            if not check_number:
                continue
            if not provider:
                frappe.throw(_("Issuing Bank is required for Check on row {0}.").format(row.idx))
            key = (
                "".join(provider.lower().split()),
                "".join(check_number.lower().split()),
            )
            if key in seen_checks:
                frappe.throw(_("Duplicate physical Check in this Cashier Sale: {0} / {1}.").format(provider, check_number))
            seen_checks.add(key)
            duplicate = frappe.db.sql(
                """
                SELECT pd.parent AS payment_receipt, pr.source_cashier_sale
                FROM `tabNKT Payment Detail` pd
                INNER JOIN `tabNKT Payment Receipt` pr ON pr.name = pd.parent
                WHERE pd.parenttype = 'NKT Payment Receipt'
                  AND pr.docstatus = 1
                  AND pr.customer = %s
                  AND pd.payment_method = 'Check'
                  AND REPLACE(LOWER(TRIM(COALESCE(pd.bank_or_provider, ''))), ' ', '') = %s
                  AND REPLACE(LOWER(TRIM(COALESCE(NULLIF(pd.check_number, ''), pd.reference_number, ''))), ' ', '') = %s
                  AND pd.parent != %s
                LIMIT 1
                """,
                (
                    self.customer,
                    key[0],
                    key[1],
                    self.linked_payment_receipt or "",
                ),
                as_dict=True,
            )
            if duplicate:
                hit = duplicate[0]
                frappe.throw(
                    _("Incoming Check {0} from {1} is already recorded for this customer in Payment Receipt {2}{3}.").format(
                        check_number,
                        provider,
                        hit.payment_receipt,
                        " / Cashier Sale " + hit.source_cashier_sale if hit.source_cashier_sale else "",
                    )
                )
    def create_cashier_movements(self):
        for row in self.get("payments") or []:
            if flt(row.amount) <= TOLERANCE or row.payment_method in {"Account", "Return Credit"}:
                continue
            create_cashier_movement(
                company=self.company,
                posting_datetime=self.sale_datetime,
                cashier_shift=self.cashier_shift,
                settlement_location=self.settlement_location,
                cashier=self.cashier,
                movement_type="Customer Order Payment",
                direction="In",
                payment_method=row.payment_method,
                amount=row_collected_amount(row),
                settlement_amount=row.amount,
                card_surcharge=flt(row.get("card_surcharge")),
                source_doctype=self.doctype,
                source_name=self.name,
                source_row=row.name,
                customer=self.customer,
                reference_number=row.reference_number or row.check_number or "",
                remarks=f"Cashier-side sale receipt {self.name}.",
            )


@frappe.whitelist()
def get_active_cashier_context(company=None):
    user = frappe.session.user
    shift = get_open_shift_for_user(company=company or None, user=user)
    if not shift and not company:
        shift = get_open_shift_for_user(user=user)
    if not shift:
        return {}
    return {
        "company": shift.company,
        "cashier_shift": shift.name,
        "settlement_location": shift.settlement_location,
        "default_warehouse": shift.settlement_location,
        "business_date": str(getdate(shift.shift_start)),
    }


@frappe.whitelist()
def get_cashier_item_context(item_code):
    user = frappe.session.user
    roles = set(frappe.get_roles(user))
    if not has_nkt_authority(10, user) and "NKT Cashier" not in roles:
        frappe.throw(_("Only an NKT Cashier can retrieve cashier-sale pricing."), frappe.PermissionError)

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
