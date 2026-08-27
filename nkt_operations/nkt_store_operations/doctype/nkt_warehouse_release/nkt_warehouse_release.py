import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import (
    cint,
    flt,
    get_datetime,
    now_datetime,
)


class NKTWarehouseRelease(Document):
    def before_validate(self):
        self.set_defaults()

        if (
            self.customer_order
            and not self.get("items")
        ):
            self.load_items_from_order()

        self.refresh_release_quantities()
        self.calculate_summary()

    def validate(self):
        self.validate_customer_order(for_submit=False)
        self.validate_release_rows()
        self.validate_available_stock()

        from nkt_operations.nkt_store_operations.features.inventory.order_fulfillment import (
            validate_warehouse_release_document,
        )
        validate_warehouse_release_document(self, for_submit=False)

    def before_submit(self):
        self.refresh_release_quantities()
        self.calculate_summary()

        from nkt_operations.nkt_store_operations.features.inventory.order_fulfillment import (
            validate_warehouse_release_document,
        )
        validate_warehouse_release_document(self, for_submit=True)

        self.validate_customer_order(for_submit=True)
        self.validate_release_rows()
        self.validate_available_stock()

        if self.flags.get("nkt_c15c_preserve_offline_release"):
            if not self.release_datetime or not self.released_by:
                frappe.throw(
                    _(
                        "Trusted offline release materialization is missing "
                        "the original physical release time/operator."
                    )
                )
        else:
            self.release_datetime = now_datetime()
            self.released_by = frappe.session.user

        self.release_status = "Released"

    def on_submit(self):
        self.create_stock_entry()

        from nkt_operations.nkt_store_operations.features.inventory.order_fulfillment import (
            apply_warehouse_release_reservations,
            sync_next_warehouse_release,
        )
        apply_warehouse_release_reservations(self.name)
        self.update_customer_order_release_status()
        sync_next_warehouse_release(
            self.customer_order,
            self.custom_nkt_source_warehouse,
        )

    def on_cancel(self):
        self.cancel_stock_entry()

        from nkt_operations.nkt_store_operations.features.inventory.order_fulfillment import (
            restore_warehouse_release_reservations,
            sync_next_warehouse_release,
        )
        restore_warehouse_release_reservations(self.name)
        self.release_status = "Cancelled"
        self.update_customer_order_release_status()
        sync_next_warehouse_release(
            self.customer_order,
            self.custom_nkt_source_warehouse,
        )

    def set_defaults(self):
        if not self.release_datetime:
            self.release_datetime = now_datetime()

        if not self.released_by:
            self.released_by = frappe.session.user

        if not self.release_status:
            self.release_status = "Draft"

    def validate_customer_order(self, for_submit=False):
        if not self.customer_order:
            frappe.throw(_("Customer Order is required."))

        order = frappe.db.get_value(
            "NKT Customer Order",
            self.customer_order,
            [
                "docstatus",
                "status",
                "company",
                "customer",
                "requires_admin_confirmation",
                "admin_confirmation_status",
            ],
            as_dict=True,
        )

        if not order:
            frappe.throw(_("The selected Customer Order does not exist."))

        if order.docstatus != 1:
            frappe.throw(_("The Customer Order must be submitted."))

        if order.company != self.company:
            frappe.throw(_("The Customer Order belongs to a different company."))

        if order.customer != self.customer:
            frappe.throw(_("The Customer Order belongs to a different customer."))

        if not for_submit:
            return

        allowed_statuses = {"Ready for Release", "Partially Released"}
        if order.status not in allowed_statuses:
            frappe.throw(
                _(
                    "Customer Order {0} is not ready for release. "
                    "Current status: {1}."
                ).format(self.customer_order, order.status)
            )

        if (
            order.requires_admin_confirmation
            and order.admin_confirmation_status != "Confirmed"
        ):
            pass  # NKT_C4_1_2_RELEASE_NO_PREAPPROVAL: physical warehouse release posts independently; Admin review is post-release audit only.


    def get_order(self):
        return frappe.get_doc(
            "NKT Customer Order",
            self.customer_order,
        )

    def get_previously_released_map(self):
        if not self.customer_order:
            return {}

        rows = frappe.db.sql(
            """
            SELECT
                item.customer_order_item,
                COALESCE(
                    SUM(item.release_quantity),
                    0
                ) AS released_quantity

            FROM `tabNKT Warehouse Release Item` item

            INNER JOIN `tabNKT Warehouse Release` release_doc
                ON release_doc.name = item.parent
                AND item.parenttype =
                    'NKT Warehouse Release'
                AND item.parentfield = 'items'

            WHERE release_doc.customer_order = %s
                AND release_doc.docstatus = 1
                AND release_doc.name != %s

            GROUP BY item.customer_order_item
            """,
            (
                self.customer_order,
                self.name or "",
            ),
            as_dict=True,
        )

        return {
            row.customer_order_item:
                flt(row.released_quantity)
            for row in rows
            if row.customer_order_item
        }

    def load_items_from_order(self):
        from nkt_operations.nkt_store_operations.features.inventory.order_fulfillment import (
            load_release_items_from_order,
        )
        load_release_items_from_order(self)

    def refresh_release_quantities(self):
        if (
            not self.customer_order
            or not self.get("items")
        ):
            return

        order = self.get_order()

        order_rows = {
            row.name: row
            for row in (order.get("items") or [])
        }

        previously_released = (
            self.get_previously_released_map()
        )

        for row in self.items:
            order_row = order_rows.get(
                row.customer_order_item
            )

            if not order_row:
                frappe.throw(
                    _(
                        "The original Customer Order item "
                        "for release row {0} was not found."
                    ).format(row.idx)
                )

            ordered_quantity = flt(
                order_row.quantity
            )

            released_quantity = flt(
                previously_released.get(
                    order_row.name,
                    0,
                )
            )

            remaining_quantity = max(
                ordered_quantity - released_quantity,
                0,
            )

            row.item = order_row.item
            row.item_name = order_row.item_name
            row.ordered_quantity = ordered_quantity
            row.previously_released_quantity = (
                released_quantity
            )
            row.remaining_quantity = (
                remaining_quantity
            )
            row.uom = order_row.uom
            row.source_warehouse = (
                order_row.source_warehouse
            )

    def calculate_summary(self):
        total_release_quantity = 0
        is_partial_release = 0

        for row in self.get("items") or []:
            release_quantity = flt(
                row.release_quantity
            )

            remaining_quantity = flt(
                row.remaining_quantity
            )

            total_release_quantity += (
                release_quantity
            )

            if (
                remaining_quantity > 0.005
                and release_quantity
                < remaining_quantity - 0.005
            ):
                is_partial_release = 1

        self.total_release_quantity = (
            total_release_quantity
        )

        self.is_partial_release = (
            is_partial_release
        )

    def validate_release_rows(self):
        if not self.get("items"):
            frappe.throw(
                _("At least one release item is required.")
            )

        has_positive_release = False

        for row in self.items:
            release_quantity = flt(
                row.release_quantity
            )

            remaining_quantity = flt(
                row.remaining_quantity
            )

            if release_quantity < 0:
                frappe.throw(
                    _(
                        "Release Quantity cannot be negative "
                        "on row {0}."
                    ).format(row.idx)
                )

            if release_quantity > 0.005:
                has_positive_release = True

            if (
                release_quantity
                > remaining_quantity + 0.005
            ):
                frappe.throw(
                    _(
                        "Release Quantity on row {0} exceeds "
                        "the remaining quantity of {1}."
                    ).format(
                        row.idx,
                        remaining_quantity,
                    )
                )

            if (
                release_quantity > 0.005
                and not row.source_warehouse
            ):
                frappe.throw(
                    _(
                        "Source Warehouse is missing "
                        "on row {0}."
                    ).format(row.idx)
                )

        if not has_positive_release:
            frappe.throw(
                _(
                    "Enter a Release Quantity greater "
                    "than zero."
                )
            )

    def validate_available_stock(self):
        allow_negative_stock = cint(
            frappe.db.get_single_value(
                "Stock Settings",
                "allow_negative_stock",
            )
        )

        for row in self.get("items") or []:
            release_quantity = flt(
                row.release_quantity
            )

            if release_quantity <= 0.005:
                continue

            available_quantity = flt(
                frappe.db.get_value(
                    "Bin",
                    {
                        "item_code": row.item,
                        "warehouse": row.source_warehouse,
                    },
                    "actual_qty",
                )
            )

            if (
                release_quantity
                <= available_quantity + 0.005
            ):
                continue

            # Respect ERPNext Stock Settings.
            # When negative stock is enabled, allow the
            # Delivery Note to proceed.
            if allow_negative_stock:
                continue

            frappe.throw(
                _(
                    "Insufficient stock for {0} in {1}. "
                    "Available: {2}, requested: {3}."
                ).format(
                    row.item,
                    row.source_warehouse,
                    available_quantity,
                    release_quantity,
                )
            )

    def create_stock_entry(self):
        from nkt_operations.nkt_store_operations.features.inventory.order_fulfillment import (
            create_warehouse_release_material_issue,
        )

        stock_entry = create_warehouse_release_material_issue(self.name)
        self.db_set(
            "custom_nkt_stock_entry",
            stock_entry,
            update_modified=False,
        )

    def cancel_stock_entry(self):
        from nkt_operations.nkt_store_operations.features.inventory.order_fulfillment import (
            cancel_warehouse_release_material_issue,
        )

        cancel_warehouse_release_material_issue(self.name)

    def update_customer_order_release_status(self):
        from nkt_operations.nkt_store_operations.features.inventory.order_fulfillment import (
            update_customer_order_fulfillment_status,
        )
        update_customer_order_fulfillment_status(self.customer_order)


@frappe.whitelist()
def get_release_data(customer_order, warehouse_release=None):
    from nkt_operations.nkt_store_operations.features.inventory.order_fulfillment import (
        get_release_data as controlled_get_release_data,
    )
    return controlled_get_release_data(
        customer_order=customer_order,
        warehouse_release=warehouse_release,
    )
