import hashlib
import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, get_datetime, now_datetime
from frappe.utils.password import check_password

from erpnext.stock.doctype.delivery_note.delivery_note import (
    get_returned_qty_map,
    make_sales_return,
)

from nkt_operations.nkt_store_operations.features.security.role_hierarchy import (
    has_nkt_authority,
)
from nkt_operations.nkt_store_operations.features.payments_accounts.cash_ledger import (
    cancel_source_cashier_movements,
    create_cashier_movement,
    get_open_shift_for_user,
    validate_cashier_shift,
)


TOLERANCE = 0.005

SETTLEMENT_TYPES = {
    'No Credit',
    'Cash Refund',
    'Customer Credit',
    'Account Adjustment',
    'Same-Item Exchange',
    'Different-Item Exchange',
}

EXCHANGE_TYPES = {
    'Same-Item Exchange',
    'Different-Item Exchange',
}


class NKTCustomerReturn(Document):
    def before_validate(self):
        self.set_defaults()

        if self.warehouse_release:
            self.sync_header_from_release()
            self.set_default_return_warehouse()

            if not self.get('items'):
                self.load_items_from_release()

            self.refresh_return_quantities()
            self.refresh_item_mappings()

        self.calculate_summary()
        self.set_cashier_shift_defaults()
        self.invalidate_stale_receipt_confirmation()
        self.invalidate_stale_approval()

    def validate(self):
        self.validate_source_release()
        self.validate_return_warehouse()
        self.validate_return_rows()
        self.validate_classification()
        self.validate_replacement_items()
        self.validate_settlement()

    def before_submit(self):
        self.lock_source_documents()
        self.validate_source_release()
        self.validate_return_warehouse()
        self.refresh_return_quantities()
        self.refresh_item_mappings()
        self.calculate_summary()
        self.set_cashier_shift_defaults()
        self.validate_return_rows()
        self.validate_classification()
        self.validate_replacement_items()
        self.validate_settlement()
        self.validate_physical_receipt()
        self.validate_approval()

        accepted = flt(self.total_accepted_sacks)
        rejected = flt(self.total_rejected_sacks)

        if accepted <= TOLERANCE:
            self.return_status = 'Not Accepted'
        elif rejected > TOLERANCE:
            self.return_status = 'Partially Accepted'
        else:
            self.return_status = 'Received'

    def on_submit(self):
        self.create_return_delivery_note()
        self.create_classification_stock_entries()
        self.create_settlement_cashier_movement()

    def on_cancel(self):
        self.cancel_settlement_cashier_movement()
        self.cancel_classification_stock_entries()
        self.cancel_return_delivery_note()

        self.db_set(
            'return_status',
            'Cancelled',
            update_modified=False,
        )

    def set_defaults(self):
        if not self.return_datetime:
            self.return_datetime = now_datetime()

        if not self.received_by:
            self.received_by = frappe.session.user

        if not self.return_status:
            self.return_status = 'Draft'

        if not self.warehouse_receipt_status:
            self.warehouse_receipt_status = 'Pending Receipt'

        if not self.settlement_type:
            self.settlement_type = 'No Credit'

        if not self.settlement_cashier:
            self.settlement_cashier = frappe.session.user

        if not self.approval_status:
            self.approval_status = 'Pending Approval'

    def get_source_release(self):
        if not self.warehouse_release:
            frappe.throw(
                _("Warehouse Release is required.")
            )

        source = frappe.get_doc(
            "NKT Warehouse Release",
            self.warehouse_release,
        )

        if source.docstatus != 1:
            frappe.throw(
                _("The Warehouse Release must be submitted.")
            )

        if source.release_status != "Released":
            frappe.throw(
                _(
                    "Warehouse Release {0} is not released. "
                    "Current status: {1}."
                ).format(
                    source.name,
                    source.release_status,
                )
            )

        if not source.delivery_note:
            frappe.throw(
                _(
                    "Warehouse Release {0} has no linked "
                    "Delivery Note."
                ).format(source.name)
            )

        delivery_note = frappe.db.get_value(
            "Delivery Note",
            source.delivery_note,
            [
                "docstatus",
                "company",
                "customer",
                "is_return",
            ],
            as_dict=True,
        )

        if not delivery_note:
            frappe.throw(
                _("The original Delivery Note was not found.")
            )

        if delivery_note.docstatus != 1:
            frappe.throw(
                _("The original Delivery Note must be submitted.")
            )

        if delivery_note.is_return:
            frappe.throw(
                _(
                    "The linked Delivery Note is already "
                    "a return document."
                )
            )

        if delivery_note.company != source.company:
            frappe.throw(
                _(
                    "The Warehouse Release and Delivery Note "
                    "belong to different companies."
                )
            )

        if delivery_note.customer != source.customer:
            frappe.throw(
                _(
                    "The Warehouse Release and Delivery Note "
                    "belong to different customers."
                )
            )

        return source

    def sync_header_from_release(self):
        source = self.get_source_release()

        protected_values = {
            "company": source.company,
            "customer": source.customer,
            "customer_order": source.customer_order,
            "original_delivery_note": source.delivery_note,
        }

        for fieldname, source_value in protected_values.items():
            existing_value = self.get(fieldname)

            if (
                existing_value
                and existing_value != source_value
            ):
                frappe.throw(
                    _(
                        "{0} does not match the selected "
                        "Warehouse Release."
                    ).format(
                        self.meta.get_label(fieldname)
                    )
                )

            self.set(fieldname, source_value)

        self.customer_name = source.customer_name

        return source

    def validate_source_release(self):
        self.sync_header_from_release()

    def set_default_return_warehouse(self):
        if (
            self.return_warehouse
            or not self.warehouse_release
        ):
            return

        source = self.get_source_release()

        source_warehouses = {
            row.source_warehouse
            for row in source.get("items") or []
            if (
                flt(row.release_quantity) > TOLERANCE
                and row.source_warehouse
            )
        }

        if len(source_warehouses) == 1:
            self.return_warehouse = next(
                iter(source_warehouses)
            )

    def validate_return_warehouse(self):
        if not self.return_warehouse:
            frappe.throw(
                _("Return Receiving Warehouse is required.")
            )

        warehouse = frappe.db.get_value(
            "Warehouse",
            self.return_warehouse,
            [
                "is_group",
                "disabled",
                "company",
            ],
            as_dict=True,
        )

        if not warehouse:
            frappe.throw(
                _("The return receiving warehouse does not exist.")
            )

        if warehouse.is_group:
            frappe.throw(
                _(
                    "Select a leaf warehouse, not a "
                    "warehouse group."
                )
            )

        if warehouse.disabled:
            frappe.throw(
                _("The return receiving warehouse is disabled.")
            )

        if (
            warehouse.company
            and warehouse.company != self.company
        ):
            frappe.throw(
                _(
                    "The return receiving warehouse belongs "
                    "to a different company."
                )
            )

    def lock_source_documents(self):
        if not self.warehouse_release:
            return

        source_delivery_note = frappe.db.get_value(
            "NKT Warehouse Release",
            self.warehouse_release,
            "delivery_note",
        )

        frappe.db.sql(
            """
            SELECT name
            FROM `tabNKT Warehouse Release`
            WHERE name = %s
            FOR UPDATE
            """,
            self.warehouse_release,
        )

        if source_delivery_note:
            frappe.db.sql(
                """
                SELECT name
                FROM `tabDelivery Note`
                WHERE name = %s
                FOR UPDATE
                """,
                source_delivery_note,
            )

    def match_delivery_note_items(self, source):
        delivery_note = frappe.get_doc(
            "Delivery Note",
            source.delivery_note,
        )

        available_items = [
            item
            for item in delivery_note.get("items") or []
            if flt(item.qty) > TOLERANCE
        ]

        used_names = set()
        mapping = {}

        for release_row in source.get("items") or []:
            release_quantity = flt(
                release_row.release_quantity
            )

            if release_quantity <= TOLERANCE:
                continue

            matched_item = None

            for item in available_items:
                if item.name in used_names:
                    continue

                if (
                    item.item_code == release_row.item
                    and item.warehouse
                    == release_row.source_warehouse
                    and abs(
                        flt(item.qty) - release_quantity
                    ) <= TOLERANCE
                ):
                    matched_item = item
                    break

            if not matched_item:
                for item in available_items:
                    if item.name in used_names:
                        continue

                    if (
                        item.item_code == release_row.item
                        and item.warehouse
                        == release_row.source_warehouse
                    ):
                        matched_item = item
                        break

            if not matched_item:
                frappe.throw(
                    _(
                        "Could not match release row {0} "
                        "to its original Delivery Note item."
                    ).format(release_row.idx)
                )

            used_names.add(matched_item.name)
            mapping[release_row.name] = matched_item

        return mapping

    def get_item_mapping(self, item_code):
        return frappe.db.get_value(
            "Item",
            item_code,
            [
                "stock_uom",
                "nkt_stock_form",
                "nkt_standard_sack_weight_kg",
                "nkt_damaged_item",
                "nkt_fraction_item",
            ],
            as_dict=True,
        ) or frappe._dict()

    def set_row_item_mapping(self, row):
        mapping = self.get_item_mapping(row.item)

        row.standard_sack_weight_kg = flt(
            mapping.nkt_standard_sack_weight_kg
        )
        row.damaged_item = mapping.nkt_damaged_item
        row.fraction_item = mapping.nkt_fraction_item

    def load_items_from_release(self):
        source = self.sync_header_from_release()

        delivery_item_map = (
            self.match_delivery_note_items(source)
        )

        returned_qty_map = (
            get_returned_qty_map(source.delivery_note)
            or {}
        )

        self.set("items", [])

        for release_row in source.get("items") or []:
            release_quantity = flt(
                release_row.release_quantity
            )

            if release_quantity <= TOLERANCE:
                continue

            delivery_item = delivery_item_map.get(
                release_row.name
            )

            if not delivery_item:
                continue

            previously_returned = max(
                flt(
                    returned_qty_map.get(
                        delivery_item.name,
                        0,
                    )
                ),
                0,
            )

            available_to_return = max(
                release_quantity - previously_returned,
                0,
            )

            if available_to_return <= TOLERANCE:
                continue

            row = self.append("items", {})

            row.warehouse_release_item = release_row.name
            row.delivery_note_item = delivery_item.name
            row.item = release_row.item
            row.item_name = release_row.item_name
            row.released_quantity = release_quantity
            row.previously_returned_quantity = (
                previously_returned
            )
            row.available_return_quantity = (
                available_to_return
            )
            row.return_quantity = 0
            row.saleable_sacks = 0
            row.repairable_sacks = 0
            row.opened_sacks = 0
            row.accepted_fraction_kg = 0
            row.rejected_sacks = 0
            row.accepted_sacks = 0
            row.uom = release_row.uom
            row.original_source_warehouse = (
                release_row.source_warehouse
            )
            row.original_rate = flt(delivery_item.rate)
            self.set_row_item_mapping(row)

    def refresh_return_quantities(self):
        if not self.get("items"):
            return

        source = self.sync_header_from_release()

        release_rows = {
            row.name: row
            for row in source.get("items") or []
        }

        delivery_item_map = (
            self.match_delivery_note_items(source)
        )

        returned_qty_map = (
            get_returned_qty_map(source.delivery_note)
            or {}
        )

        for row in self.get("items") or []:
            if not row.warehouse_release_item:
                frappe.throw(
                    _(
                        "Return row {0} is not linked to "
                        "an original release item."
                    ).format(row.idx)
                )

            release_row = release_rows.get(
                row.warehouse_release_item
            )

            if not release_row:
                frappe.throw(
                    _(
                        "The original release item for "
                        "row {0} was not found."
                    ).format(row.idx)
                )

            delivery_item = delivery_item_map.get(
                release_row.name
            )

            if not delivery_item:
                frappe.throw(
                    _(
                        "The original Delivery Note item "
                        "for row {0} was not found."
                    ).format(row.idx)
                )

            release_quantity = flt(
                release_row.release_quantity
            )

            previously_returned = max(
                flt(
                    returned_qty_map.get(
                        delivery_item.name,
                        0,
                    )
                ),
                0,
            )

            available_to_return = max(
                release_quantity - previously_returned,
                0,
            )

            row.delivery_note_item = delivery_item.name
            row.item = release_row.item
            row.item_name = release_row.item_name
            row.released_quantity = release_quantity
            row.previously_returned_quantity = (
                previously_returned
            )
            row.available_return_quantity = (
                available_to_return
            )
            row.uom = release_row.uom
            row.original_source_warehouse = (
                release_row.source_warehouse
            )
            row.original_rate = flt(delivery_item.rate)

    def refresh_item_mappings(self):
        for row in self.get("items") or []:
            self.set_row_item_mapping(row)

    def calculate_summary(self):
        totals = {
            'physical': 0,
            'accepted': 0,
            'saleable': 0,
            'repairable': 0,
            'opened': 0,
            'rejected': 0,
            'fraction_kg': 0,
            'gross_value': 0,
            'replacement_value': 0,
        }

        for row in self.get('items') or []:
            saleable = max(flt(row.saleable_sacks), 0)
            repairable = max(flt(row.repairable_sacks), 0)
            opened = max(flt(row.opened_sacks), 0)
            rejected = max(flt(row.rejected_sacks), 0)
            fraction_kg = max(
                flt(row.accepted_fraction_kg),
                0,
            )
            accepted = saleable + repairable + opened
            whole_sacks = saleable + repairable
            original_rate = max(flt(row.original_rate), 0)
            standard_weight = flt(
                row.standard_sack_weight_kg
            )

            full_value = whole_sacks * original_rate
            fraction_value = 0

            if (
                fraction_kg > TOLERANCE
                and standard_weight > TOLERANCE
            ):
                fraction_value = (
                    fraction_kg
                    / standard_weight
                    * original_rate
                )

            gross_value = full_value + fraction_value

            row.accepted_sacks = accepted
            row.whole_sacks_accepted = whole_sacks
            row.full_sack_return_value = full_value
            row.fraction_return_value = fraction_value
            row.gross_return_value = gross_value

            totals['physical'] += max(
                flt(row.return_quantity),
                0,
            )
            totals['accepted'] += accepted
            totals['saleable'] += saleable
            totals['repairable'] += repairable
            totals['opened'] += opened
            totals['rejected'] += rejected
            totals['fraction_kg'] += fraction_kg
            totals['gross_value'] += gross_value

        for row in self.get('replacement_items') or []:
            qty = max(flt(row.quantity), 0)
            rate = max(flt(row.rate), 0)
            row.amount = qty * rate
            totals['replacement_value'] += row.amount

        deductions = (
            max(flt(self.labor_charge), 0)
            + max(flt(self.packaging_deduction), 0)
            + max(flt(self.handling_deduction), 0)
            + max(flt(self.other_deduction), 0)
        )

        calculated_credit = max(
            totals['gross_value'] - deductions,
            0,
        )

        self.total_return_quantity = totals['physical']
        self.total_accepted_sacks = totals['accepted']
        self.total_saleable_sacks = totals['saleable']
        self.total_repairable_sacks = totals['repairable']
        self.total_opened_sacks = totals['opened']
        self.total_rejected_sacks = totals['rejected']
        self.total_fraction_kg = totals['fraction_kg']
        self.gross_return_value = totals['gross_value']
        self.total_deductions = deductions
        self.calculated_return_credit = calculated_credit
        self.settlement_amount = (
            0
            if self.settlement_type == 'No Credit'
            else calculated_credit
        )
        self.replacement_value = totals['replacement_value']

        self.customer_pays = 0
        self.refund_due = 0
        self.customer_credit_due = 0

        if self.settlement_type == 'Cash Refund':
            self.refund_due = self.settlement_amount

        elif self.settlement_type in {
            'Customer Credit',
            'Account Adjustment',
        }:
            self.customer_credit_due = self.settlement_amount

        elif self.settlement_type in EXCHANGE_TYPES:
            self.customer_pays = max(
                flt(self.replacement_value)
                - flt(self.settlement_amount),
                0,
            )
            self.refund_due = max(
                flt(self.settlement_amount)
                - flt(self.replacement_value),
                0,
            )

            if self.difference_payment_method in {
                'Customer Credit',
                'Account Adjustment',
            }:
                self.customer_credit_due = self.refund_due
                self.refund_due = 0

    def require_whole_sacks(self, value, label, row_idx):
        value = flt(value)

        if value < -TOLERANCE:
            frappe.throw(
                _(
                    "{0} cannot be negative on row {1}."
                ).format(label, row_idx)
            )

        if abs(value - round(value)) > TOLERANCE:
            frappe.throw(
                _(
                    "{0} must be a whole number on row {1}."
                ).format(label, row_idx)
            )

    def validate_return_rows(self):
        if not self.get("items"):
            frappe.throw(
                _(
                    "No accepted quantity remains available "
                    "for return."
                )
            )

        has_physical_quantity = False
        seen_release_rows = set()

        for row in self.get("items") or []:
            physical_quantity = flt(row.return_quantity)
            available_quantity = flt(
                row.available_return_quantity
            )

            if row.warehouse_release_item in seen_release_rows:
                frappe.throw(
                    _(
                        "Original release item is duplicated "
                        "on row {0}."
                    ).format(row.idx)
                )

            seen_release_rows.add(
                row.warehouse_release_item
            )

            self.require_whole_sacks(
                physical_quantity,
                _("Physical Sacks Presented"),
                row.idx,
            )

            if physical_quantity > TOLERANCE:
                has_physical_quantity = True

            if (
                physical_quantity
                > available_quantity + TOLERANCE
            ):
                frappe.throw(
                    _(
                        "Physical Sacks Presented on row {0} "
                        "exceeds the available quantity of {1}."
                    ).format(
                        row.idx,
                        available_quantity,
                    )
                )

        if not has_physical_quantity:
            frappe.throw(
                _(
                    "Enter Physical Sacks Presented greater "
                    "than zero."
                )
            )

    def validate_classification(self):
        for row in self.get("items") or []:
            physical = flt(row.return_quantity)
            saleable = flt(row.saleable_sacks)
            repairable = flt(row.repairable_sacks)
            opened = flt(row.opened_sacks)
            rejected = flt(row.rejected_sacks)
            fraction_kg = flt(row.accepted_fraction_kg)

            for value, label in (
                (saleable, _("Saleable Sacks")),
                (repairable, _("Repairable / Rebag Sacks")),
                (opened, _("Opened Sacks")),
                (rejected, _("Rejected Sacks")),
            ):
                self.require_whole_sacks(
                    value,
                    label,
                    row.idx,
                )

            classified = (
                saleable
                + repairable
                + opened
                + rejected
            )

            if abs(classified - physical) > TOLERANCE:
                frappe.throw(
                    _(
                        "Row {0}: Saleable + Repairable + "
                        "Opened + Rejected must equal the "
                        "Physical Sacks Presented quantity "
                        "of {1}."
                    ).format(row.idx, physical)
                )

            if fraction_kg < -TOLERANCE:
                frappe.throw(
                    _(
                        "Accepted Fraction Weight cannot be "
                        "negative on row {0}."
                    ).format(row.idx)
                )

            if opened > TOLERANCE:
                self.validate_opened_sack_mapping(row)

                if fraction_kg <= TOLERANCE:
                    frappe.throw(
                        _(
                            "Enter the actual accepted fraction "
                            "weight for the opened sack(s) on "
                            "row {0}."
                        ).format(row.idx)
                    )

                maximum_weight = (
                    opened
                    * flt(row.standard_sack_weight_kg)
                )

                if fraction_kg > maximum_weight + TOLERANCE:
                    frappe.throw(
                        _(
                            "Accepted fraction weight on row {0} "
                            "cannot exceed {1} kg for {2} "
                            "opened sack(s)."
                        ).format(
                            row.idx,
                            maximum_weight,
                            opened,
                        )
                    )
            elif fraction_kg > TOLERANCE:
                frappe.throw(
                    _(
                        "Accepted Fraction Weight requires at "
                        "least one Opened Sack on row {0}."
                    ).format(row.idx)
                )

            if repairable > TOLERANCE:
                self.validate_repairable_mapping(row)

            row.accepted_sacks = (
                saleable + repairable + opened
            )

    def validate_repairable_mapping(self, row):
        base = self.get_item_mapping(row.item)

        if base.nkt_stock_form != "Saleable Sack":
            frappe.throw(
                _(
                    "Item {0} must be configured as an NKT "
                    "Saleable Sack before repairable returns "
                    "can be accepted."
                ).format(row.item)
            )

        if not row.damaged_item:
            frappe.throw(
                _(
                    "Item {0} has no linked Damaged Item."
                ).format(row.item)
            )

        damaged = frappe.db.get_value(
            "Item",
            row.damaged_item,
            [
                "disabled",
                "stock_uom",
                "nkt_stock_form",
                "nkt_base_saleable_item",
            ],
            as_dict=True,
        )

        if (
            not damaged
            or damaged.disabled
            or damaged.nkt_stock_form != "Damaged Sack"
            or damaged.nkt_base_saleable_item != row.item
            or damaged.stock_uom != row.uom
        ):
            frappe.throw(
                _(
                    "The linked Damaged Item for {0} is invalid."
                ).format(row.item)
            )

    def validate_opened_sack_mapping(self, row):
        base = self.get_item_mapping(row.item)

        if base.nkt_stock_form != "Saleable Sack":
            frappe.throw(
                _(
                    "Item {0} must be configured as an NKT "
                    "Saleable Sack before opened returns can "
                    "be accepted."
                ).format(row.item)
            )

        if flt(row.standard_sack_weight_kg) <= TOLERANCE:
            frappe.throw(
                _(
                    "Item {0} has no valid Standard Sack "
                    "Weight."
                ).format(row.item)
            )

        if not row.fraction_item:
            frappe.throw(
                _(
                    "Item {0} has no linked Fraction Item."
                ).format(row.item)
            )

        fraction = frappe.db.get_value(
            "Item",
            row.fraction_item,
            [
                "disabled",
                "stock_uom",
                "nkt_stock_form",
                "nkt_base_saleable_item",
            ],
            as_dict=True,
        )

        if (
            not fraction
            or fraction.disabled
            or fraction.stock_uom != "Kg"
            or fraction.nkt_stock_form != "Fraction Stock"
            or fraction.nkt_base_saleable_item != row.item
        ):
            frappe.throw(
                _(
                    "The linked Fraction Item for {0} is invalid."
                ).format(row.item)
            )

    def validate_settlement(self):
        settlement_type = self.settlement_type or ''

        if settlement_type not in SETTLEMENT_TYPES:
            frappe.throw(
                _('Select a valid Settlement Type.')
            )

        for fieldname in (
            'labor_charge',
            'packaging_deduction',
            'handling_deduction',
            'other_deduction',
        ):
            if flt(self.get(fieldname)) < -TOLERANCE:
                frappe.throw(
                    _('{0} cannot be negative.').format(
                        self.meta.get_label(fieldname)
                    )
                )

        if (
            flt(self.total_deductions)
            > flt(self.gross_return_value) + TOLERANCE
        ):
            frappe.throw(
                _(
                    'Total deductions cannot exceed the '
                    'Gross Return Value.'
                )
            )

        if (
            flt(self.other_deduction) > TOLERANCE
            and not (
                self.other_deduction_reason or ''
            ).strip()
        ):
            frappe.throw(
                _('Other Deduction Reason is required.')
            )

        if not (self.settlement_reason or '').strip():
            frappe.throw(
                _('Settlement Reason is required.')
            )

        replacement_rows = self.get('replacement_items') or []

        if settlement_type in EXCHANGE_TYPES:
            if not replacement_rows:
                frappe.throw(
                    _(
                        'Add at least one Replacement Item '
                        'for an exchange.'
                    )
                )

            if (
                flt(self.customer_pays) > TOLERANCE
                or flt(self.refund_due) > TOLERANCE
            ) and not self.difference_payment_method:
                frappe.throw(
                    _(
                        'Select the Exchange Difference Method.'
                    )
                )
        elif replacement_rows:
            frappe.throw(
                _(
                    'Replacement Items are allowed only for '
                    'Same-Item or Different-Item Exchange.'
                )
            )

        if settlement_type == 'No Credit':
            if flt(self.settlement_amount) > TOLERANCE:
                frappe.throw(
                    _(
                        'Approved Return Credit must be zero '
                        'for No Credit.'
                    )
                )

        movement = self.get_settlement_movement_spec()

        if movement:
            validate_cashier_shift(
                cashier_shift=self.cashier_shift,
                company=self.company,
                settlement_location=self.settlement_location,
                cashier=self.settlement_cashier,
                require_open=True,
            )

    def set_cashier_shift_defaults(self):
        movement = self.get_settlement_movement_spec()

        if not movement:
            return

        if not self.settlement_cashier:
            self.settlement_cashier = frappe.session.user

        if not self.cashier_shift:
            shift = get_open_shift_for_user(
                company=self.company,
                user=self.settlement_cashier,
                settlement_location=self.settlement_location,
            )

            if shift:
                self.cashier_shift = shift.name
                self.settlement_location = (
                    shift.settlement_location
                )

        if self.cashier_shift:
            shift = frappe.db.get_value(
                'NKT Cashier Shift',
                self.cashier_shift,
                [
                    'settlement_location',
                    'cashier',
                ],
                as_dict=True,
            )

            if shift:
                self.settlement_location = (
                    shift.settlement_location
                )
                self.settlement_cashier = shift.cashier

    def validate_replacement_items(self):
        replacement_rows = self.get('replacement_items') or []
        original_items = {
            row.item
            for row in self.get('items') or []
            if flt(row.return_quantity) > TOLERANCE
        }

        for row in replacement_rows:
            if not row.item:
                frappe.throw(
                    _(
                        'Replacement Item is required on row {0}.'
                    ).format(row.idx)
                )

            item = frappe.db.get_value(
                'Item',
                row.item,
                [
                    'disabled',
                    'is_stock_item',
                    'stock_uom',
                    'item_name',
                ],
                as_dict=True,
            )

            if not item or item.disabled or not item.is_stock_item:
                frappe.throw(
                    _(
                        'Replacement Item {0} is not an active '
                        'stock item.'
                    ).format(row.item)
                )

            if flt(row.quantity) <= TOLERANCE:
                frappe.throw(
                    _(
                        'Replacement Quantity must be greater '
                        'than zero on row {0}.'
                    ).format(row.idx)
                )

            if flt(row.rate) < -TOLERANCE:
                frappe.throw(
                    _(
                        'Replacement Rate cannot be negative '
                        'on row {0}.'
                    ).format(row.idx)
                )

            row.item_name = item.item_name
            row.uom = item.stock_uom
            row.amount = flt(row.quantity) * flt(row.rate)

            if not row.source_warehouse:
                frappe.throw(
                    _(
                        'Replacement Source Warehouse is '
                        'required on row {0}.'
                    ).format(row.idx)
                )

            warehouse = frappe.db.get_value(
                'Warehouse',
                row.source_warehouse,
                ['is_group', 'disabled', 'company'],
                as_dict=True,
            )

            if (
                not warehouse
                or warehouse.is_group
                or warehouse.disabled
                or warehouse.company != self.company
            ):
                frappe.throw(
                    _(
                        'Replacement Source Warehouse is '
                        'invalid on row {0}.'
                    ).format(row.idx)
                )

            if (
                self.settlement_type == 'Same-Item Exchange'
                and row.item not in original_items
            ):
                frappe.throw(
                    _(
                        'Same-Item Exchange replacement {0} '
                        'was not part of the original returned '
                        'items.'
                    ).format(row.item)
                )

    def get_receipt_signature(self):
        rows = []

        for row in self.get('items') or []:
            rows.append(
                {
                    'warehouse_release_item':
                        row.warehouse_release_item,
                    'item': row.item,
                    'physical': round(
                        flt(row.return_quantity),
                        6,
                    ),
                    'saleable': round(
                        flt(row.saleable_sacks),
                        6,
                    ),
                    'repairable': round(
                        flt(row.repairable_sacks),
                        6,
                    ),
                    'opened': round(
                        flt(row.opened_sacks),
                        6,
                    ),
                    'fraction_kg': round(
                        flt(row.accepted_fraction_kg),
                        6,
                    ),
                    'rejected': round(
                        flt(row.rejected_sacks),
                        6,
                    ),
                }
            )

        rows.sort(
            key=lambda row: (
                row.get('warehouse_release_item') or ''
            )
        )

        payload = {
            'warehouse_release': self.warehouse_release,
            'return_warehouse': self.return_warehouse,
            'items': rows,
        }

        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(',', ':'),
            default=str,
        ).encode('utf-8')

        return hashlib.sha256(encoded).hexdigest()

    def clear_receipt_confirmation(self):
        self.warehouse_receipt_status = 'Pending Receipt'
        self.warehouse_receipt_confirmed_by = None
        self.warehouse_receipt_confirmed_on = None
        self.warehouse_receipt_remarks = None
        self.warehouse_receipt_signature = None

    def invalidate_stale_receipt_confirmation(self):
        if self.docstatus != 0:
            return

        if self.warehouse_receipt_status not in {
            'Received',
            'Overridden',
        }:
            return

        if (
            not self.warehouse_receipt_signature
            or self.warehouse_receipt_signature
            != self.get_receipt_signature()
        ):
            self.clear_receipt_confirmation()

    def validate_physical_receipt(self):
        if self.warehouse_receipt_status not in {
            'Received',
            'Overridden',
        }:
            frappe.throw(
                _(
                    'The selected Return Receiving Warehouse '
                    'must confirm the physical goods before '
                    'approval and submission.'
                )
            )

        if (
            not self.warehouse_receipt_confirmed_by
            or not self.warehouse_receipt_confirmed_on
            or not self.warehouse_receipt_signature
        ):
            frappe.throw(
                _(
                    'The physical warehouse receipt record '
                    'is incomplete.'
                )
            )

        if (
            self.warehouse_receipt_signature
            != self.get_receipt_signature()
        ):
            frappe.throw(
                _(
                    'Physical return details changed after '
                    'warehouse confirmation. Confirm receipt '
                    'again.'
                )
            )

    def get_settlement_movement_spec(self):
        if self.settlement_type == 'Cash Refund':
            amount = flt(self.refund_due)
            if amount > TOLERANCE:
                return {
                    'movement_type': 'Customer Return Refund',
                    'direction': 'Out',
                    'payment_method': 'Cash',
                    'amount': amount,
                }

        if self.settlement_type in EXCHANGE_TYPES:
            if self.difference_payment_method in {
                'Customer Credit',
                'Account Adjustment',
            }:
                return None

            if flt(self.customer_pays) > TOLERANCE:
                return {
                    'movement_type':
                        'Exchange Difference Collected',
                    'direction': 'In',
                    'payment_method':
                        self.difference_payment_method,
                    'amount': flt(self.customer_pays),
                }

            if flt(self.refund_due) > TOLERANCE:
                return {
                    'movement_type':
                        'Exchange Difference Refunded',
                    'direction': 'Out',
                    'payment_method':
                        self.difference_payment_method,
                    'amount': flt(self.refund_due),
                }

        return None

    def create_settlement_cashier_movement(self):
        movement = self.get_settlement_movement_spec()

        if not movement:
            return

        cash_movement = create_cashier_movement(
            company=self.company,
            posting_datetime=self.return_datetime,
            cashier_shift=self.cashier_shift,
            settlement_location=self.settlement_location,
            cashier=self.settlement_cashier,
            movement_type=movement['movement_type'],
            direction=movement['direction'],
            payment_method=movement['payment_method'],
            amount=movement['amount'],
            source_doctype=self.doctype,
            source_name=self.name,
            source_row='settlement',
            customer=self.customer,
            reference_number=self.settlement_reference,
            remarks=(
                f'Created automatically from '
                f'NKT Customer Return {self.name}. '
                f'{self.settlement_reason}'
            ),
        )

        if cash_movement:
            self.db_set(
                'cashier_movement',
                cash_movement.name,
                update_modified=False,
            )

    def cancel_settlement_cashier_movement(self):
        cancel_source_cashier_movements(
            self.doctype,
            self.name,
        )

    def get_approval_signature(self):
        return_datetime = get_datetime(
            self.return_datetime
        )

        items = []

        for row in self.get('items') or []:
            items.append(
                {
                    'warehouse_release_item':
                        row.warehouse_release_item,
                    'delivery_note_item':
                        row.delivery_note_item,
                    'item': row.item,
                    'physical': round(
                        flt(row.return_quantity), 6
                    ),
                    'saleable': round(
                        flt(row.saleable_sacks), 6
                    ),
                    'repairable': round(
                        flt(row.repairable_sacks), 6
                    ),
                    'opened': round(
                        flt(row.opened_sacks), 6
                    ),
                    'fraction_kg': round(
                        flt(row.accepted_fraction_kg), 6
                    ),
                    'rejected': round(
                        flt(row.rejected_sacks), 6
                    ),
                    'gross_value': round(
                        flt(row.gross_return_value), 6
                    ),
                    'damaged_item': row.damaged_item,
                    'fraction_item': row.fraction_item,
                }
            )

        items.sort(
            key=lambda item: (
                item.get('warehouse_release_item') or ''
            )
        )

        replacements = []

        for row in self.get('replacement_items') or []:
            replacements.append(
                {
                    'item': row.item,
                    'quantity': round(
                        flt(row.quantity), 6
                    ),
                    'rate': round(flt(row.rate), 6),
                    'amount': round(flt(row.amount), 6),
                    'source_warehouse':
                        row.source_warehouse,
                }
            )

        replacements.sort(
            key=lambda item: (
                item.get('item') or '',
                item.get('source_warehouse') or '',
            )
        )

        payload = {
            'warehouse_release': self.warehouse_release,
            'customer': self.customer,
            'return_datetime': (
                return_datetime.strftime(
                    '%Y-%m-%d %H:%M:%S'
                )
                if return_datetime
                else ''
            ),
            'return_warehouse': self.return_warehouse,
            'warehouse_receipt_status':
                self.warehouse_receipt_status,
            'warehouse_receipt_signature':
                self.warehouse_receipt_signature,
            'settlement_type': self.settlement_type,
            'gross_return_value': round(
                flt(self.gross_return_value), 6
            ),
            'labor_charge': round(
                flt(self.labor_charge), 6
            ),
            'packaging_deduction': round(
                flt(self.packaging_deduction), 6
            ),
            'handling_deduction': round(
                flt(self.handling_deduction), 6
            ),
            'other_deduction': round(
                flt(self.other_deduction), 6
            ),
            'other_deduction_reason': (
                self.other_deduction_reason or ''
            ).strip(),
            'settlement_amount': round(
                flt(self.settlement_amount), 6
            ),
            'replacement_value': round(
                flt(self.replacement_value), 6
            ),
            'customer_pays': round(
                flt(self.customer_pays), 6
            ),
            'refund_due': round(
                flt(self.refund_due), 6
            ),
            'difference_payment_method':
                self.difference_payment_method,
            'settlement_location':
                self.settlement_location,
            'settlement_cashier':
                self.settlement_cashier,
            'cashier_shift': self.cashier_shift,
            'settlement_reference': (
                self.settlement_reference or ''
            ).strip(),
            'settlement_reason': (
                self.settlement_reason or ''
            ).strip(),
            'return_reason': (
                self.return_reason or ''
            ).strip(),
            'remarks': (self.remarks or '').strip(),
            'items': items,
            'replacement_items': replacements,
        }

        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(',', ':'),
            default=str,
        ).encode('utf-8')

        return hashlib.sha256(encoded).hexdigest()

    def clear_approval(self):
        self.approval_status = "Pending Approval"
        self.approved_by = None
        self.approved_on = None
        self.approval_reason = None
        self.approval_signature = None

    def invalidate_stale_approval(self):
        if self.docstatus != 0:
            return

        if self.approval_status != "Approved":
            return

        current_signature = self.get_approval_signature()

        if (
            not self.approval_signature
            or self.approval_signature
            != current_signature
        ):
            self.clear_approval()

    def validate_approval(self):
        if self.approval_status != "Approved":
            frappe.throw(
                _(
                    "Owner or Administrator approval is "
                    "required before submission."
                )
            )

        if (
            not self.approved_by
            or not self.approved_on
            or not self.approval_reason
            or not self.approval_signature
        ):
            frappe.throw(
                _("The return approval record is incomplete.")
            )

        if (
            self.approval_signature
            != self.get_approval_signature()
        ):
            frappe.throw(
                _(
                    "Return classification or settlement "
                    "changed after approval. Request approval "
                    "again."
                )
            )

        if not has_nkt_authority(
            10,
            self.approved_by,
        ):
            frappe.throw(
                _(
                    "The approving user no longer has NKT "
                    "Owner or NKT Administrator authority."
                )
            )

    def create_return_delivery_note(self):
        accepted_total = flt(self.total_accepted_sacks)

        if accepted_total <= TOLERANCE:
            return

        if self.return_delivery_note:
            return

        return_document = make_sales_return(
            self.original_delivery_note
        )

        return_document.set_posting_time = 1

        return_datetime = get_datetime(
            self.return_datetime
        )

        return_document.posting_date = (
            return_datetime.date()
        )
        return_document.posting_time = (
            return_datetime.time()
        )

        return_document.issue_credit_note = 0

        mapped_items = {
            item.dn_detail: item
            for item in return_document.get("items") or []
            if item.dn_detail
        }

        selected_items = []

        for row in self.get("items") or []:
            accepted_quantity = (
                flt(row.saleable_sacks)
                + flt(row.repairable_sacks)
                + flt(row.opened_sacks)
            )

            if accepted_quantity <= TOLERANCE:
                continue

            mapped_item = mapped_items.get(
                row.delivery_note_item
            )

            if not mapped_item:
                frappe.throw(
                    _(
                        "Delivery Note item for return "
                        "row {0} is no longer available."
                    ).format(row.idx)
                )

            mapped_item.qty = -accepted_quantity
            mapped_item.warehouse = self.return_warehouse

            conversion_factor = flt(
                mapped_item.conversion_factor
            ) or 1

            mapped_item.stock_qty = (
                mapped_item.qty * conversion_factor
            )

            mapped_item.amount = (
                mapped_item.qty
                * flt(mapped_item.rate)
            )

            mapped_item.base_amount = (
                mapped_item.qty
                * flt(mapped_item.base_rate)
            )

            mapped_item.net_amount = (
                mapped_item.qty
                * flt(
                    mapped_item.net_rate
                    or mapped_item.rate
                )
            )

            mapped_item.base_net_amount = (
                mapped_item.qty
                * flt(
                    mapped_item.base_net_rate
                    or mapped_item.base_rate
                )
            )

            selected_items.append(mapped_item)

        if not selected_items:
            return

        return_document.set("items", selected_items)
        return_document.set("packed_items", [])

        source_note = (
            "Created automatically from "
            f"NKT Customer Return {self.name}."
        )

        source_note += (
            "\nAccepted sacks: "
            f"{flt(self.total_accepted_sacks)}"
        )
        source_note += (
            "\nRejected sacks: "
            f"{flt(self.total_rejected_sacks)}"
        )
        source_note += (
            "\nManual settlement: "
            f"{self.settlement_type} - "
            f"{flt(self.settlement_amount)}"
        )
        source_note += (
            "\nSettlement is recorded in the NKT return "
            "and is not automatically posted to accounting."
        )

        if self.return_reason:
            source_note += (
                f"\nReturn reason: {self.return_reason}"
            )

        if self.remarks:
            source_note += f"\nRemarks: {self.remarks}"

        return_document.remarks = source_note

        return_document.flags.ignore_permissions = True
        return_document.insert()
        return_document.submit()

        self.db_set(
            "return_delivery_note",
            return_document.name,
            update_modified=False,
        )

    def get_difference_account(self):
        account = frappe.db.get_value(
            "Company",
            self.company,
            "stock_adjustment_account",
        )

        if not account:
            frappe.throw(
                _(
                    "Set the Stock Adjustment Account in "
                    "Company {0}."
                ).format(self.company)
            )

        return account

    def get_repack_stock_entry_type(self):
        stock_entry_type = frappe.db.get_value(
            "Stock Entry Type",
            {
                "purpose": "Repack",
                "is_standard": 1,
            },
            "name",
        )

        if not stock_entry_type:
            frappe.throw(
                _(
                    "No standard Stock Entry Type exists "
                    "for Repack."
                )
            )

        return stock_entry_type

    def create_repack_entry(
        self,
        row,
        source_qty,
        output_item,
        output_qty,
        action_label,
    ):
        posting_datetime = get_datetime(
            self.return_datetime
        )

        source_uom = frappe.db.get_value(
            "Item",
            row.item,
            "stock_uom",
        )
        output_uom = frappe.db.get_value(
            "Item",
            output_item,
            "stock_uom",
        )

        if not source_uom or not output_uom:
            frappe.throw(
                _("Stock UOM could not be determined.")
            )

        difference_account = self.get_difference_account()

        stock_entry = frappe.get_doc(
            {
                "doctype": "Stock Entry",
                "company": self.company,
                "purpose": "Repack",
                "stock_entry_type":
                    self.get_repack_stock_entry_type(),
                "set_posting_time": 1,
                "posting_date": posting_datetime.date(),
                "posting_time": posting_datetime.time(),
                "remarks": (
                    f"{action_label} created automatically "
                    f"from NKT Customer Return {self.name}."
                ),
                "items": [
                    {
                        "item_code": row.item,
                        "qty": source_qty,
                        "uom": source_uom,
                        "stock_uom": source_uom,
                        "conversion_factor": 1,
                        "s_warehouse": self.return_warehouse,
                        "expense_account": difference_account,
                        "allow_zero_valuation_rate": 1,
                    },
                    {
                        "item_code": output_item,
                        "qty": output_qty,
                        "uom": output_uom,
                        "stock_uom": output_uom,
                        "conversion_factor": 1,
                        "t_warehouse": self.return_warehouse,
                        "expense_account": difference_account,
                        "allow_zero_valuation_rate": 1,
                        "is_finished_item": 1,
                    },
                ],
            }
        )

        stock_entry.flags.ignore_permissions = True
        stock_entry.insert()
        stock_entry.submit()

        return stock_entry

    def create_classification_stock_entries(self):
        for row in self.get("items") or []:
            repairable = flt(row.repairable_sacks)
            opened = flt(row.opened_sacks)

            if repairable > TOLERANCE:
                repair_entry = self.create_repack_entry(
                    row=row,
                    source_qty=repairable,
                    output_item=row.damaged_item,
                    output_qty=repairable,
                    action_label=(
                        "Customer-return repair/rebag "
                        "classification"
                    ),
                )

                row.db_set(
                    "repair_stock_entry",
                    repair_entry.name,
                    update_modified=False,
                )

            if opened > TOLERANCE:
                fraction_entry = self.create_repack_entry(
                    row=row,
                    source_qty=opened,
                    output_item=row.fraction_item,
                    output_qty=flt(
                        row.accepted_fraction_kg
                    ),
                    action_label=(
                        "Customer-return opened-sack "
                        "fraction recovery"
                    ),
                )

                row.db_set(
                    "fraction_stock_entry",
                    fraction_entry.name,
                    update_modified=False,
                )

    def cancel_classification_stock_entries(self):
        entry_names = []

        for row in self.get("items") or []:
            for fieldname in (
                "fraction_stock_entry",
                "repair_stock_entry",
            ):
                entry_name = row.get(fieldname)

                if entry_name and entry_name not in entry_names:
                    entry_names.append(entry_name)

        for entry_name in entry_names:
            stock_entry = frappe.get_doc(
                "Stock Entry",
                entry_name,
            )

            if stock_entry.docstatus == 1:
                stock_entry.flags.ignore_permissions = True
                stock_entry.cancel()

    def cancel_return_delivery_note(self):
        if not self.return_delivery_note:
            return

        delivery_note = frappe.get_doc(
            "Delivery Note",
            self.return_delivery_note,
        )

        if delivery_note.docstatus == 1:
            delivery_note.flags.ignore_permissions = True
            delivery_note.cancel()


@frappe.whitelist()
def get_return_data(
    warehouse_release,
    customer_return=None,
):
    source = frappe.get_doc(
        "NKT Warehouse Release",
        warehouse_release,
    )

    source.check_permission("read")

    return_doc = frappe.new_doc(
        "NKT Customer Return"
    )

    if customer_return:
        return_doc.name = customer_return

    return_doc.warehouse_release = warehouse_release
    return_doc.load_items_from_release()
    return_doc.set_default_return_warehouse()
    return_doc.calculate_summary()

    return {
        "company": return_doc.company,
        "customer": return_doc.customer,
        "customer_name": return_doc.customer_name,
        "customer_order": return_doc.customer_order,
        "warehouse_release":
            return_doc.warehouse_release,
        "original_delivery_note":
            return_doc.original_delivery_note,
        "default_return_warehouse":
            return_doc.return_warehouse,
        "items": [
            {
                "warehouse_release_item":
                    row.warehouse_release_item,
                "delivery_note_item":
                    row.delivery_note_item,
                "item": row.item,
                "item_name": row.item_name,
                "released_quantity":
                    row.released_quantity,
                "previously_returned_quantity":
                    row.previously_returned_quantity,
                "available_return_quantity":
                    row.available_return_quantity,
                "return_quantity": 0,
                "saleable_sacks": 0,
                "repairable_sacks": 0,
                "opened_sacks": 0,
                "accepted_fraction_kg": 0,
                "rejected_sacks": 0,
                "accepted_sacks": 0,
                "uom": row.uom,
                "original_source_warehouse":
                    row.original_source_warehouse,
                "original_rate": row.original_rate,
                "standard_sack_weight_kg":
                    row.standard_sack_weight_kg,
                "damaged_item": row.damaged_item,
                "fraction_item": row.fraction_item,
            }
            for row in return_doc.get("items") or []
        ],
    }


@frappe.whitelist()
def get_customer_return_approval_mode():
    current_user = frappe.session.user

    return {
        "direct_approval": has_nkt_authority(
            10,
            current_user,
        ),
        "current_user": current_user,
    }


@frappe.whitelist(methods=["POST"])
def approve_customer_return(
    customer_return,
    approval_reason,
    admin_user=None,
    admin_password=None,
):
    approval_reason = (
        approval_reason or ""
    ).strip()

    if not approval_reason:
        frappe.throw(
            _("Approval Reason is required.")
        )

    return_doc = frappe.get_doc(
        "NKT Customer Return",
        customer_return,
    )

    return_doc.check_permission("write")

    if return_doc.docstatus != 0:
        frappe.throw(
            _("Only a saved Draft return can be approved.")
        )

    return_doc.set_defaults()
    return_doc.sync_header_from_release()
    return_doc.set_default_return_warehouse()
    return_doc.refresh_return_quantities()
    return_doc.refresh_item_mappings()
    return_doc.calculate_summary()
    return_doc.validate()
    return_doc.validate_physical_receipt()
    return_doc.save()
    return_doc.reload()

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

        if not cint(enabled):
            frappe.throw(
                _("The approving account is disabled.")
            )

        if not has_nkt_authority(
            10,
            authenticated_user,
        ):
            frappe.throw(
                _(
                    "The supplied user does not have NKT "
                    "Owner or NKT Administrator authority."
                )
            )

    return_doc.reload()
    return_doc.refresh_return_quantities()
    return_doc.refresh_item_mappings()
    return_doc.calculate_summary()

    values = {
        "approval_status": "Approved",
        "approved_by": authenticated_user,
        "approved_on": now_datetime(),
        "approval_reason": approval_reason,
        "approval_signature":
            return_doc.get_approval_signature(),
    }

    frappe.db.set_value(
        "NKT Customer Return",
        return_doc.name,
        values,
        update_modified=True,
    )

    return_doc.add_comment(
        "Info",
        _(
            "Customer return classification and manual "
            "settlement approved by {0}. Reason: {1}"
        ).format(
            authenticated_user,
            approval_reason,
        ),
    )

    return {
        "customer_return": return_doc.name,
        "approval_status": "Approved",
        "approved_by": authenticated_user,
    }


@frappe.whitelist(methods=['POST'])
def confirm_customer_return_receipt(
    customer_return,
    remarks=None,
):
    return_doc = frappe.get_doc(
        'NKT Customer Return',
        customer_return,
    )
    return_doc.check_permission('write')

    if return_doc.docstatus != 0:
        frappe.throw(
            _('Only a saved Draft return can be confirmed.')
        )

    current_user = frappe.session.user
    roles = set(frappe.get_roles(current_user))

    if (
        not has_nkt_authority(10, current_user)
        and 'NKT Warehouse' not in roles
    ):
        frappe.throw(
            _(
                'Only NKT Warehouse, NKT Owner, or NKT '
                'Administrator may confirm physical receipt.'
            ),
            frappe.PermissionError,
        )

    return_doc.set_defaults()
    return_doc.sync_header_from_release()
    return_doc.set_default_return_warehouse()
    return_doc.refresh_return_quantities()
    return_doc.refresh_item_mappings()
    return_doc.calculate_summary()
    return_doc.validate_return_warehouse()
    return_doc.validate_return_rows()
    return_doc.validate_classification()

    status = (
        'Overridden'
        if has_nkt_authority(10, current_user)
        and 'NKT Warehouse' not in roles
        else 'Received'
    )

    values = {
        'warehouse_receipt_status': status,
        'warehouse_receipt_confirmed_by': current_user,
        'warehouse_receipt_confirmed_on': now_datetime(),
        'warehouse_receipt_remarks': (
            remarks or ''
        ).strip(),
        'warehouse_receipt_signature':
            return_doc.get_receipt_signature(),
        'approval_status': 'Pending Approval',
        'approved_by': '',
        'approved_on': None,
        'approval_reason': '',
        'approval_signature': '',
    }

    frappe.db.set_value(
        'NKT Customer Return',
        return_doc.name,
        values,
        update_modified=True,
    )

    return_doc.add_comment(
        'Info',
        _(
            'Physical return received at {0} and confirmed '
            'by {1}. {2}'
        ).format(
            return_doc.return_warehouse,
            current_user,
            (remarks or '').strip(),
        ),
    )

    return {
        'customer_return': return_doc.name,
        'warehouse_receipt_status': status,
        'confirmed_by': current_user,
    }


@frappe.whitelist()
def get_replacement_item_defaults(
    item_code,
    customer_return=None,
):
    item = frappe.db.get_value(
        'Item',
        item_code,
        ['item_name', 'stock_uom', 'disabled'],
        as_dict=True,
    )

    if not item or item.disabled:
        frappe.throw(_('Replacement Item is disabled or missing.'))

    original_rate = None

    if customer_return:
        return_doc = frappe.get_doc(
            'NKT Customer Return',
            customer_return,
        )
        return_doc.check_permission('read')

        for row in return_doc.get('items') or []:
            if row.item == item_code:
                original_rate = flt(row.original_rate)
                break

    if original_rate is not None:
        rate = original_rate
        rate_source = 'Original Sale Rate'
    else:
        rate = frappe.db.get_value(
            'Item Price',
            {
                'item_code': item_code,
                'price_list': 'Standard Selling',
                'selling': 1,
            },
            'price_list_rate',
        ) or 0
        rate_source = (
            'Current Selling Rate'
            if flt(rate) > TOLERANCE
            else 'Manual'
        )

    return {
        'item_name': item.item_name,
        'uom': item.stock_uom,
        'rate': flt(rate),
        'rate_source': rate_source,
    }
