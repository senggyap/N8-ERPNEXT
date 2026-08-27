import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, get_datetime, now_datetime
from frappe.utils.password import check_password

from nkt_operations.nkt_store_operations.features.security.role_hierarchy import has_nkt_authority
from nkt_operations.nkt_store_operations.features.payments_accounts.cash_ledger import (
    cancel_source_cashier_movements,
    create_cashier_movement,
    get_open_shift_for_user,
    validate_cashier_shift,
)
from nkt_operations.nkt_store_operations.features.payments_accounts.card_surcharge import (
    apply_payment_row_card_fields,
    ensure_card_posting_allowed,
    row_collected_amount,
)


REFERENCE_METHODS = {
    "GCash",
    "Maya",
    "Card",
    "Bank Transfer",
    "Online",
}


OVERRIDE_AUDIT_FIELDS = (
    "overpayment_override_approved",
    "overpayment_override_approved_amount",
    "overpayment_override_approved_by",
    "overpayment_override_approved_on",
    "overpayment_override_reason",
)


class NKTPaymentReceipt(Document):
    def before_validate(self):
        self.set_defaults()
        self.apply_payment_rules()
        self.calculate_totals()
        self.set_cashier_shift_defaults()
        self.load_order_balance()
        self.calculate_overpayment_control()

    def validate(self):
        self.validate_primary_tender_binding()
        self.validate_customer_order()
        self.validate_payment_rows()
        self.validate_duplicate_references()
        self.validate_override_field_security()
        self.validate_cashier_shift_link()

    def before_submit(self):
        self.apply_primary_tender_defaults()
        self.apply_payment_rules()
        self.calculate_totals()
        self.set_cashier_shift_defaults()
        self.load_order_balance()
        self.calculate_overpayment_control()
        self.validate_order_is_submitted()
        self.validate_overpayment_for_submit()
        self.validate_cashier_shift_link()
        self.validate_primary_tender_binding()

        self.receipt_status = 'Completed'

    def on_submit(self):
        if self.is_primary_tender_materialization():
            # C15C.10B authority boundary:
            # preserve the canonical Payment Receipt only.
            # Cashier Movement (10C), Customer Receivable/Advance/order effects (10D),
            # and warehouse/stock effects (10E) remain deferred.
            return

        self.create_customer_advance()
        self.update_linked_order_payment_summary()
        self.create_cashier_movements()

    def before_cancel(self):
        if self.is_primary_tender_materialization():
            frappe.throw(
                _(
                    "A Payment Receipt materialized from an immutable Cashier Tender "
                    "cannot be cancelled through the ordinary Payment Receipt workflow."
                )
            )
        self.validate_customer_advance_can_cancel()

    def on_cancel(self):
        self.cancel_cashier_movements()
        self.cancel_customer_advance()
        self.update_linked_order_payment_summary()

    def is_primary_tender_materialization(self):
        return bool(self.get("source_primary_tender_intent"))

    def get_primary_tender_source(self):
        source = str(self.get("source_primary_tender_intent") or "").strip()
        if not source:
            return None

        journal = frappe.db.get_value(
            "NKT Primary Cashier Tender Intent",
            source,
            [
                "name",
                "event_uuid",
                "origin_user",
                "company",
                "customer",
                "cashier_shift",
                "settlement_location",
                "settled_at",
                "payload_sha256",
                "canonical_payload_json",
                "preservation_state",
            ],
            as_dict=True,
        )
        if not journal:
            frappe.throw(_("Source Primary Cashier Tender Intent was not found."))
        if str(journal.preservation_state or "") != "Preserved":
            frappe.throw(_("Source Cashier Tender is not in Preserved state."))
        return journal

    def apply_primary_tender_defaults(self):
        journal = self.get_primary_tender_source()
        if not journal:
            return

        self.company = journal.company
        self.customer = journal.customer
        self.receipt_datetime = journal.settled_at
        self.received_by = journal.origin_user
        self.encoded_by = journal.origin_user
        self.cashier_shift = journal.cashier_shift
        self.settlement_location = journal.settlement_location
        self.payment_purpose = "Cashier Sale Payment"
        self.customer_order = None
        self.source_cashier_sale = None
        self.allocation_status = "Unallocated - Awaiting Encoder"
        self.source_tender_payload_sha256 = journal.payload_sha256
        self.downstream_effects_state = "Deferred - C15C.10C/10D"

    def validate_primary_tender_binding(self):
        journal = self.get_primary_tender_source()
        if not journal:
            return

        # Re-apply authoritative immutable context before comparing rows.
        self.apply_primary_tender_defaults()

        if str(self.source_tender_payload_sha256 or "") != str(journal.payload_sha256 or ""):
            frappe.throw(_("Payment Receipt Tender payload hash does not match its Primary Tender."))

        try:
            payload = frappe.parse_json(journal.canonical_payload_json or "{}")
        except Exception as exc:
            raise frappe.ValidationError(
                _("Primary Tender canonical payload is invalid.")
            ) from exc

        from nkt_operations.nkt_store_operations.features.payments_accounts.internal.cashier_tender_intent import (
            _normalize_cashier_tender_intent_payload,
        )

        normalized = _normalize_cashier_tender_intent_payload(payload)
        expected = [
            row
            for row in (normalized.get("payments") or [])
            if row.get("payment_method") not in {"Account", "Return Credit"}
            and flt(row.get("amount")) > 0.005
        ]

        actual = list(self.get("payments") or [])
        if len(actual) != len(expected):
            frappe.throw(_("Payment Receipt rows do not match the immutable Cashier Tender."))

        def text(value):
            return str(value or "").strip()

        for idx, (row, exp) in enumerate(zip(actual, expected), start=1):
            mismatch = []
            if text(row.payment_method) != text(exp.get("payment_method")):
                mismatch.append("payment_method")
            for fieldname in (
                "amount",
                "card_surcharge",
                "collected_amount",
                "cash_tendered",
                "change_amount",
            ):
                if abs(flt(row.get(fieldname)) - flt(exp.get(fieldname))) > 0.01:
                    mismatch.append(fieldname)
            for fieldname in (
                "reference_number",
                "bank_or_provider",
                "check_number",
                "check_date",
                "remarks",
            ):
                if text(row.get(fieldname)) != text(exp.get(fieldname)):
                    mismatch.append(fieldname)
            if mismatch:
                frappe.throw(
                    _(
                        "Payment Receipt row {0} conflicts with immutable Cashier Tender fields: {1}."
                    ).format(idx, ", ".join(mismatch))
                )

    def set_defaults(self):
        if self.is_primary_tender_materialization():
            self.apply_primary_tender_defaults()

        if not self.receipt_datetime:
            self.receipt_datetime = now_datetime()

        if not self.received_by:
            self.received_by = frappe.session.user

        if not self.encoded_by:
            self.encoded_by = frappe.session.user

        if not self.receipt_status:
            self.receipt_status = "Draft"

        if (
            self.source_cashier_sale
            or self.is_primary_tender_materialization()
        ) and not self.payment_purpose:
            self.payment_purpose = "Cashier Sale Payment"

        if not self.allocation_status:
            if self.source_cashier_sale and self.customer_order:
                self.allocation_status = "Allocated to Encoder Order"
            elif self.source_cashier_sale or self.is_primary_tender_materialization():
                self.allocation_status = "Unallocated - Awaiting Encoder"
            elif self.payment_purpose == "Account Collection":
                self.allocation_status = "Account Collection"
            else:
                self.allocation_status = "Direct Order Payment"

    def has_shift_movement_rows(self):
        return any(
            flt(row.amount) > 0.005
            and row.payment_method != 'Account'
            for row in self.get('payments') or []
        )

    def set_cashier_shift_defaults(self):
        if self.is_primary_tender_materialization():
            self.apply_primary_tender_defaults()
            return

        if self.source_cashier_sale:
            sale = frappe.db.get_value(
                "NKT Cashier Sale",
                self.source_cashier_sale,
                ["cashier", "cashier_shift", "settlement_location"],
                as_dict=True,
            )
            if not sale:
                frappe.throw(_("Source Cashier Sale was not found."))
            self.received_by = sale.cashier
            self.cashier_shift = sale.cashier_shift
            self.settlement_location = sale.settlement_location
            return

        if not self.has_shift_movement_rows():
            self.cashier_shift = None
            self.settlement_location = None
            return

        cashier_user = self.received_by or frappe.session.user
        shift = get_open_shift_for_user(
            company=self.company,
            user=cashier_user,
        )

        if not shift:
            frappe.throw(
                _(
                    "{0} must have exactly one Open Cashier Shift before "
                    "receiving cash, checks, or other payments."
                ).format(cashier_user)
            )

        # The active shift is system-assigned. Users cannot select another
        # employee's shift or an older shift.
        self.cashier_shift = shift.name
        self.settlement_location = shift.settlement_location

    def validate_cashier_shift_link(self):
        if not self.has_shift_movement_rows():
            return

        validate_cashier_shift(
            cashier_shift=self.cashier_shift,
            company=self.company,
            settlement_location=self.settlement_location,
            cashier=self.received_by,
            require_open=False
            if (self.source_cashier_sale or self.is_primary_tender_materialization())
            else True,
        )

    def create_cashier_movements(self):
        if self.source_cashier_sale or self.is_primary_tender_materialization():
            return

        movement_type = (
            'Customer Order Payment'
            if self.payment_purpose == 'Order Payment'
            else 'Customer Account Collection'
        )

        for row in self.get('payments') or []:
            if (
                flt(row.amount) <= 0.005
                or row.payment_method == 'Account'
            ):
                continue

            reference = (
                row.reference_number
                or row.check_number
                or ''
            )

            create_cashier_movement(
                company=self.company,
                posting_datetime=self.receipt_datetime,
                cashier_shift=self.cashier_shift,
                settlement_location=self.settlement_location,
                cashier=self.received_by,
                movement_type=movement_type,
                direction='In',
                payment_method=row.payment_method,
                amount=row_collected_amount(row),
                settlement_amount=row.amount,
                card_surcharge=flt(row.get("card_surcharge")),
                source_doctype=self.doctype,
                source_name=self.name,
                source_row=row.name,
                customer=self.customer,
                reference_number=reference,
                remarks=(
                    f'Created automatically from '
                    f'{self.doctype} {self.name}.'
                ),
            )

    def cancel_cashier_movements(self):
        if self.source_cashier_sale or self.is_primary_tender_materialization():
            return

        cancel_source_cashier_movements(
            self.doctype,
            self.name,
        )

    def apply_payment_rules(self):
        for row in self.get("payments") or []:
            apply_payment_row_card_fields(row)
            method = row.payment_method

            row.verification_status = "Not Required"

            if method == "Cash":
                row.affects_cash_drawer = 1
                if flt(row.cash_tendered) <= 0.005 and flt(row.amount) > 0.005:
                    row.cash_tendered = flt(row.amount)
                row.change_amount = max(
                    flt(row.cash_tendered) - flt(row.amount),
                    0,
                )
            else:
                row.affects_cash_drawer = 0
                row.cash_tendered = 0
                row.change_amount = 0

    def calculate_totals(self):
        total_payment = 0
        total_cash = 0
        total_non_cash = 0
        total_account_charge = 0
        card_surcharge_total = 0
        total_collected = 0

        for row in self.get("payments") or []:
            amount = flt(row.amount)
            total_payment += amount
            card_surcharge_total += flt(row.get("card_surcharge"))

            if row.payment_method not in {"Account", "Return Credit"}:
                total_collected += row_collected_amount(row)

            if row.payment_method == "Cash":
                total_cash += amount
            elif row.payment_method == "Account":
                total_account_charge += amount
            else:
                total_non_cash += amount

        self.total_payment = total_payment
        self.total_cash = total_cash
        self.total_non_cash = total_non_cash
        self.total_account_charge = total_account_charge
        if self.meta.has_field("card_surcharge_total"):
            self.card_surcharge_total = card_surcharge_total
        if self.meta.has_field("total_collected"):
            self.total_collected = total_collected
        self.verification_required = 0

    def load_order_balance(self):
        if not self.customer_order:
            self.order_total = 0
            self.previously_applied = 0
            self.amount_due_before_receipt = 0
            self.remaining_balance = 0
            return

        balance = get_order_balance(
            self.customer_order,
            self.name,
        )

        self.order_total = balance["order_total"]
        self.previously_applied = balance["previously_applied"]
        self.amount_due_before_receipt = (
            balance["amount_due_before_receipt"]
        )

        self.remaining_balance = max(
            flt(self.amount_due_before_receipt)
            - flt(self.total_payment),
            0,
        )

    def calculate_overpayment_control(self):
        is_order_payment = (
            self.payment_purpose == "Order Payment"
            and self.customer_order
        )

        if is_order_payment:
            excess = max(
                flt(self.total_payment)
                - flt(self.amount_due_before_receipt),
                0,
            )
        else:
            excess = 0

        self.overpayment_amount = excess
        self.overpayment_override_required = (
            1 if excess > 0.005 else 0
        )

        approval_matches = (
            cint(self.overpayment_override_approved)
            and abs(
                flt(
                    self.overpayment_override_approved_amount
                )
                - excess
            )
            <= 0.005
        )

        if (
            excess <= 0.005
            or (
                cint(self.overpayment_override_approved)
                and not approval_matches
            )
        ):
            self.clear_stale_override()
            approval_matches = False

        self.customer_advance_amount = (
            excess if approval_matches else 0
        )

    def clear_stale_override(self):
        has_existing_approval = any(
            [
                cint(self.overpayment_override_approved),
                flt(
                    self.overpayment_override_approved_amount
                ),
                self.overpayment_override_approved_by,
                self.overpayment_override_approved_on,
                self.overpayment_override_reason,
            ]
        )

        if has_existing_approval:
            self.flags.clearing_stale_override = True

        self.overpayment_override_approved = 0
        self.overpayment_override_approved_amount = 0
        self.overpayment_override_approved_by = None
        self.overpayment_override_approved_on = None
        self.overpayment_override_reason = None

    def validate_customer_order(self):
        if not self.customer:
            frappe.throw(_("Customer is required."))

        if self.payment_purpose == "Cashier Sale Payment":
            if not (
                self.source_cashier_sale
                or self.is_primary_tender_materialization()
            ):
                frappe.throw(
                    _(
                        "Source Cashier Sale or preserved Primary Cashier Tender "
                        "is required for a Cashier Sale Payment."
                    )
                )
            # The cashier records payment before the encoder may have posted
            # the official order. Allocation happens later during matching.
            if not self.customer_order:
                return

        if (
            self.payment_purpose == "Order Payment"
            and not self.customer_order
            and not self.source_cashier_sale
        ):
            frappe.throw(
                _("Customer Order is required for an Order Payment.")
            )

        if not self.customer_order:
            return

        order = frappe.db.get_value(
            "NKT Customer Order",
            self.customer_order,
            ["customer", "company"],
            as_dict=True,
        )

        if not order:
            frappe.throw(
                _("The selected customer order does not exist.")
            )

        if order.customer != self.customer:
            frappe.throw(
                _("The selected order belongs to a different customer.")
            )

        if order.company != self.company:
            frappe.throw(
                _("The selected order belongs to a different company.")
            )

    def validate_payment_rows(self):
        if not self.get("payments"):
            frappe.throw(
                _("At least one payment row is required.")
            )

        for row in self.payments:
            ensure_card_posting_allowed(row.payment_method, "Card Payment Receipt")
            method = row.payment_method
            amount = flt(row.amount)

            if not method:
                frappe.throw(
                    _(
                        "Payment Method is required on row {0}."
                    ).format(row.idx)
                )

            if amount <= 0:
                frappe.throw(
                    _(
                        "Amount must be greater than zero "
                        "on row {0}."
                    ).format(row.idx)
                )

            if method == "Cash":
                if flt(row.cash_tendered) < amount:
                    frappe.throw(
                        _(
                            "Cash Tendered cannot be less than "
                            "the payment amount on row {0}."
                        ).format(row.idx)
                    )

            elif method == "Check":
                if not row.bank_or_provider:
                    frappe.throw(
                        _(
                            "Bank is required on row {0}."
                        ).format(row.idx)
                    )

                if not row.check_number:
                    frappe.throw(
                        _(
                            "Check Number is required on row {0}."
                        ).format(row.idx)
                    )

                if not row.check_date:
                    frappe.throw(
                        _(
                            "Check Date is required on row {0}."
                        ).format(row.idx)
                    )

            elif method in REFERENCE_METHODS:
                if not row.reference_number:
                    frappe.throw(
                        _(
                            "Reference Number is required for "
                            "{0} on row {1}."
                        ).format(method, row.idx)
                    )

            elif method == "Account":
                pass

            else:
                frappe.throw(
                    _(
                        "Unsupported payment method: {0}."
                    ).format(method)
                )

    def validate_duplicate_references(self):
        # C2.3.1: Payment Receipt is the authoritative hard duplicate guard for
        # physical incoming Checks. Informal non-check references may repeat.
        receipt_name = self.name or ""
        seen_checks = set()
        for row in self.get("payments") or []:
            if row.payment_method != "Check":
                continue
            check_number = (row.check_number or row.reference_number or "").strip()
            provider = (row.bank_or_provider or "").strip()
            if not check_number or not provider:
                continue
            key = (
                "".join(provider.lower().split()),
                "".join(check_number.lower().split()),
            )
            if key in seen_checks:
                frappe.throw(_("Duplicate physical Check in this Payment Receipt: {0} / {1}.").format(provider, check_number))
            seen_checks.add(key)
            existing = frappe.db.sql(
                """
                SELECT pd.parent AS payment_receipt
                FROM `tabNKT Payment Detail` pd
                INNER JOIN `tabNKT Payment Receipt` pr ON pr.name = pd.parent
                WHERE pd.parenttype = 'NKT Payment Receipt'
                  AND pr.docstatus < 2
                  AND pr.customer = %s
                  AND pd.payment_method = 'Check'
                  AND REPLACE(LOWER(TRIM(COALESCE(pd.bank_or_provider, ''))), ' ', '') = %s
                  AND REPLACE(LOWER(TRIM(COALESCE(NULLIF(pd.check_number, ''), pd.reference_number, ''))), ' ', '') = %s
                  AND pd.parent != %s
                LIMIT 1
                """,
                (self.customer, key[0], key[1], receipt_name),
                as_dict=True,
            )
            if existing:
                frappe.throw(
                    _("Incoming Check {0} from {1} has already been recorded for this customer in Payment Receipt {2}.").format(
                        check_number,
                        provider,
                        existing[0].payment_receipt,
                    )
                )
    def validate_override_field_security(self):
        previous = self.get_doc_before_save()

        if not previous:
            has_manual_override_values = any(
                [
                    cint(self.overpayment_override_approved),
                    flt(
                        self.overpayment_override_approved_amount
                    ),
                    self.overpayment_override_approved_by,
                    self.overpayment_override_approved_on,
                    self.overpayment_override_reason,
                ]
            )

            if (
                has_manual_override_values
                and not self.flags.get(
                    "overpayment_override_authorized"
                )
            ):
                frappe.throw(
                    _(
                        "Overpayment approval can only be entered "
                        "through the Admin Overpayment Override."
                    )
                )

            return

        def normalize_override_value(fieldname, value):
            if fieldname == "overpayment_override_approved":
                return cint(value)

            if (
                fieldname
                == "overpayment_override_approved_amount"
            ):
                return round(flt(value), 6)

            if fieldname == "overpayment_override_approved_on":
                return get_datetime(value) if value else None

            return str(value or "").strip()

        approval_fields_changed = any(
            normalize_override_value(
                fieldname,
                previous.get(fieldname),
            )
            != normalize_override_value(
                fieldname,
                self.get(fieldname),
            )
            for fieldname in OVERRIDE_AUDIT_FIELDS
        )

        allowed_change = (
            self.flags.get(
                "overpayment_override_authorized"
            )
            or self.flags.get("clearing_stale_override")
        )

        if approval_fields_changed and not allowed_change:
            frappe.throw(
                _(
                    "Overpayment approval fields cannot be "
                    "changed manually."
                )
            )
            
    def validate_overpayment_for_submit(self):
        excess = flt(self.overpayment_amount)

        if excess <= 0.005:
            return

        if not cint(self.overpayment_override_approved):
            frappe.throw(
                _(
                    "Payment exceeds the order balance by "
                    "₱{0:,.2f}. Admin overpayment override "
                    "is required before submission."
                ).format(excess)
            )

        approved_amount = flt(
            self.overpayment_override_approved_amount
        )

        if abs(approved_amount - excess) > 0.005:
            frappe.throw(
                _(
                    "The payment changed after the Admin "
                    "override. A new override is required."
                )
            )

        approving_user = (
            self.overpayment_override_approved_by
        )

        if (
            not approving_user
            or not self.overpayment_override_approved_on
            or not self.overpayment_override_reason
        ):
            frappe.throw(
                _("The Admin override record is incomplete.")
            )

        if not has_nkt_authority(10, approving_user):
            frappe.throw(
                _(
                    "The approving user no longer has "
                    "NKT Owner or NKT Administrator "
                    "authority."
                )
            )

    def validate_order_is_submitted(self):
        if (
            self.payment_purpose != "Order Payment"
            or not self.customer_order
        ):
            return

        order_docstatus = frappe.db.get_value(
            "NKT Customer Order",
            self.customer_order,
            "docstatus",
        )

        if cint(order_docstatus) != 1:
            frappe.throw(
                _(
                    "Submit the Customer Order before submitting "
                    "its Payment Receipt."
                )
            )

    def create_customer_advance(self):
        advance_amount = flt(self.customer_advance_amount)

        if advance_amount <= 0.005:
            return

        existing_advance = frappe.db.get_value(
            "NKT Customer Advance",
            {
                "source_payment_receipt": self.name,
            },
            "name",
        )

        if existing_advance:
            return

        advance = frappe.get_doc(
            {
                "doctype": "NKT Customer Advance",
                "company": self.company,
                "posting_datetime":
                    self.receipt_datetime or now_datetime(),
                "customer": self.customer,
                "source_payment_receipt": self.name,
                "source_customer_order": self.customer_order,
                "original_advance_amount": advance_amount,
                "applied_amount": 0,
                "available_advance_amount": advance_amount,
                "advance_status": "Available",
                "approved_by":
                    self.overpayment_override_approved_by,
                "approved_on":
                    self.overpayment_override_approved_on,
                "approval_reason":
                    self.overpayment_override_reason,
                "remarks":
                    "Created automatically from approved "
                    "payment overpayment.",
            }
        )

        advance.flags.ignore_permissions = True
        advance.insert()
        advance.submit()

    def validate_customer_advance_can_cancel(self):
        advance = frappe.db.get_value(
            "NKT Customer Advance",
            {
                "source_payment_receipt": self.name,
                "docstatus": 1,
            },
            [
                "name",
                "applied_amount",
            ],
            as_dict=True,
        )

        if (
            advance
            and flt(advance.applied_amount) > 0.005
        ):
            frappe.throw(
                _(
                    "Customer Advance {0} has already been used. "
                    "This receipt cannot be cancelled."
                ).format(advance.name)
            )

    def cancel_customer_advance(self):
        advance_name = frappe.db.get_value(
            "NKT Customer Advance",
            {
                "source_payment_receipt": self.name,
                "docstatus": 1,
            },
            "name",
        )

        if not advance_name:
            return

        advance = frappe.get_doc(
            "NKT Customer Advance",
            advance_name,
        )

        advance.flags.ignore_permissions = True
        advance.cancel()

    def update_linked_order_payment_summary(self):
        if not self.customer_order:
            return

        summary = frappe.db.sql(
            """
            SELECT
                COALESCE(
                    SUM(
                        total_payment
                        - COALESCE(customer_advance_amount, 0)
                        - COALESCE(total_account_charge, 0)
                    ),
                    0
                ) AS actual_paid,

                COALESCE(
                    SUM(total_account_charge),
                    0
                ) AS account_applied,

                COALESCE(
                    SUM(
                        total_payment
                        - COALESCE(customer_advance_amount, 0)
                    ),
                    0
                ) AS total_settled

            FROM `tabNKT Payment Receipt`

            WHERE customer_order = %s
              AND docstatus = 1
            """,
            self.customer_order,
            as_dict=True,
        )[0]

        order = frappe.db.get_value(
            "NKT Customer Order",
            self.customer_order,
            [
                "grand_total",
                "requires_admin_confirmation",
                "admin_confirmation_status",
                "declared_account",
                "payment_arrangement",
                "custom_nkt_return_credit_applied",
            ],
            as_dict=True,
        )

        if not order:
            frappe.throw(
                _("The linked Customer Order was not found.")
            )

        # V2.0C.5.2 advance applications included in order payment summary
        advance_applied = 0
        if frappe.db.exists("DocType", "NKT Customer Advance Application"):
            advance_applied = flt(
                frappe.db.sql(
                    """
                    SELECT COALESCE(SUM(applied_amount), 0)
                    FROM `tabNKT Customer Advance Application`
                    WHERE customer_order = %s
                      AND docstatus = 1
                      AND application_status = 'Applied'
                    """,
                    self.customer_order,
                )[0][0]
            )

        actual_paid = max(
            flt(summary.actual_paid) + advance_applied,
            0,
        )
        # Account is not money received, so it is not copied into the
        # cash-basis Payment Receipt. The official encoder order supplies
        # the account portion after reconciliation.
        account_applied = max(
            flt(summary.account_applied),
            flt(order.declared_account),
            0,
        )

        return_credit_applied = max(
            flt(order.get("custom_nkt_return_credit_applied")),
            0,
        )

        total_settled = max(
            actual_paid + account_applied + return_credit_applied,
            0,
        )

        remaining_balance = max(
            flt(order.grand_total) - total_settled,
            0,
        )

        if total_settled <= 0.005:
            payment_status = "Unpaid"

        elif remaining_balance > 0.005:
            payment_status = "Partially Paid"

        elif account_applied > 0.005:
            payment_status = "Charged to Account"

        else:
            payment_status = "Paid"

        payment_method_rows = frappe.db.sql(
            """
            SELECT DISTINCT detail.payment_method

            FROM `tabNKT Payment Receipt` receipt

            INNER JOIN `tabNKT Payment Detail` detail
                ON detail.parent = receipt.name
               AND detail.parenttype = 'NKT Payment Receipt'
               AND detail.parentfield = 'payments'

            WHERE receipt.customer_order = %s
              AND receipt.docstatus = 1
              AND detail.amount > 0
            """,
            self.customer_order,
            as_dict=True,
        )

        payment_methods = sorted(
            {
                row.payment_method
                for row in payment_method_rows
                if row.payment_method
            }
        )

        if order.payment_arrangement:
            payment_arrangement = order.payment_arrangement
        elif len(payment_methods) == 1:
            payment_arrangement = (
                "Other Online"
                if payment_methods[0] == "Online"
                else payment_methods[0]
            )
        elif len(payment_methods) > 1:
            payment_arrangement = "Split Payment"
        else:
            payment_arrangement = None

        requires_confirmation = cint(
            order.requires_admin_confirmation
        )

        confirmation_completed = (
            order.admin_confirmation_status == "Confirmed"
        )

        if (
            requires_confirmation
            and not confirmation_completed
        ):
            order_status = "Pending Admin Confirmation"

        elif payment_status == "Unpaid":
            order_status = "Awaiting Payment"

        elif payment_status == "Partially Paid":
            order_status = "Partially Paid"

        elif payment_status == "Charged to Account":
            order_status = "Pending Credit Control"

        else:
            order_status = "Ready for Release"

        values = {
            "amount_paid": actual_paid,
            "amount_due": remaining_balance,
            "payment_status": payment_status,
            "status": order_status,
        }

        if payment_arrangement:
            values["payment_arrangement"] = (
                payment_arrangement
            )

        frappe.db.set_value(
            "NKT Customer Order",
            self.customer_order,
            values,
            update_modified=False,
        )

@frappe.whitelist()
def refresh_order_payment_summary(customer_order):
    receipts = frappe.get_all(
        "NKT Payment Receipt",
        filters={
            "customer_order": customer_order,
            "docstatus": 1,
        },
        fields=["name"],
        order_by="modified desc",
        limit=1,
    )

    if not receipts:
        frappe.throw(
            _("No submitted Payment Receipt was found.")
        )

    receipt = frappe.get_doc(
        "NKT Payment Receipt",
        receipts[0].name,
    )

    receipt.update_linked_order_payment_summary()
    frappe.db.commit()

    return frappe.db.get_value(
        "NKT Customer Order",
        customer_order,
        [
            "status",
            "payment_arrangement",
            "payment_status",
            "amount_paid",
            "amount_due",
        ],
        as_dict=True,
    )

 
@frappe.whitelist(methods=["POST"])
def approve_overpayment(
    payment_receipt,
    admin_user=None,
    admin_password=None,
    reason=None,
):
    reason = (reason or "").strip()

    if not reason:
        frappe.throw(_("Override Reason is required."))

    doc = frappe.get_doc(
        "NKT Payment Receipt",
        payment_receipt,
        for_update=True,
    )

    doc.check_permission("write")

    if doc.docstatus != 0:
        frappe.throw(
            _("Only a Draft receipt can receive an override.")
        )

    doc.apply_payment_rules()
    doc.calculate_totals()
    doc.load_order_balance()
    doc.calculate_overpayment_control()

    excess = flt(doc.overpayment_amount)

    if excess <= 0.005:
        frappe.throw(
            _("This receipt currently has no overpayment.")
        )

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

        user_enabled = frappe.db.get_value(
            "User",
            authenticated_user,
            "enabled",
        )

        if not user_enabled:
            frappe.throw(
                _(
                    "The selected Owner or Administrator "
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
                    "to approve an overpayment."
                )
            )

    doc.overpayment_override_approved = 1
    doc.overpayment_override_approved_amount = excess
    doc.overpayment_override_approved_by = (
        authenticated_user
    )
    doc.overpayment_override_approved_on = now_datetime()
    doc.overpayment_override_reason = reason
    doc.customer_advance_amount = excess

    doc.flags.overpayment_override_authorized = True
    doc.save()

    return {
        "approved": True,
        "approved_by": authenticated_user,
        "approved_amount": excess,
    }


@frappe.whitelist()
def get_order_balance(
    customer_order,
    payment_receipt=None,
):
    order = frappe.db.get_value(
        "NKT Customer Order",
        customer_order,
        ["grand_total"],
        as_dict=True,
    )

    if not order:
        frappe.throw(_("Customer Order was not found."))

    previous = frappe.db.sql(
        """
        SELECT COALESCE(
    		SUM(
        		total_payment
        		- COALESCE(customer_advance_amount, 0)
    		),
    		0
)
        FROM `tabNKT Payment Receipt`
        WHERE customer_order = %s
          AND docstatus = 1
          AND name != %s
        """,
        (
            customer_order,
            payment_receipt or "",
        ),
    )[0][0]

    order_total = flt(order.grand_total)
    # V2.0C.5.2 advance applications included in get_order_balance
    advance_applied = 0
    if frappe.db.exists("DocType", "NKT Customer Advance Application"):
        advance_applied = flt(
            frappe.db.sql(
                """
                SELECT COALESCE(SUM(applied_amount), 0)
                FROM `tabNKT Customer Advance Application`
                WHERE customer_order = %s
                  AND docstatus = 1
                  AND application_status = 'Applied'
                """,
                customer_order,
            )[0][0]
        )

    previously_applied = flt(previous) + advance_applied
    return {
        "order_total": order_total,
        "previously_applied": previously_applied,
        "amount_due_before_receipt": max(
            order_total - previously_applied,
            0,
        ),
    }
