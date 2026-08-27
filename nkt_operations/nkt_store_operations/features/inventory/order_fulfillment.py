from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.custom.doctype.property_setter.property_setter import make_property_setter
from frappe.utils import cint, flt, get_datetime, now_datetime


TOLERANCE = 0.005
NORMAL_SALE_FORM = "Saleable Sack"
DAMAGED_FORM = "Damaged Sack"
FRACTION_FORM = "Fraction Stock"
IMMEDIATE_KIND = "Immediate Store Deduction"
RELEASE_KIND = "Warehouse Release"
NKT_ORDER_VOUCHER_TYPE = "NKT Customer Order"


def install_schema():
    """Install/update V1.2 fields and enable standard ERPNext reservation support."""
    custom_fields = {
        "Warehouse": [
            {
                "fieldname": "custom_nkt_immediate_sale_deduction",
                "label": "NKT Immediate Sale Deduction",
                "fieldtype": "Check",
                "default": "0",
                "insert_after": "custom_requires_nkt_admin_approval",
                "description": (
                    "When enabled, encoder submission immediately creates a "
                    "Material Issue and deducts physical stock."
                ),
            },
            {
                "fieldname": "custom_nkt_release_authorizer_user",
                "label": "NKT Release Authorizer User",
                "fieldtype": "Link",
                "options": "User",
                "insert_after": "custom_nkt_immediate_sale_deduction",
                "description": (
                    "Optional strict control. If set, restricted-warehouse orders "
                    "must have been confirmed by this exact user before release."
                ),
            },
        ],
        "NKT Customer Order": [
            {
                "fieldname": "custom_nkt_retail_stock_entry",
                "label": "Immediate Stock Material Issue",
                "fieldtype": "Link",
                "options": "Stock Entry",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_retail_delivery_note",
            },
            {
                "fieldname": "custom_nkt_fulfillment_status",
                "label": "Inventory Fulfillment Status",
                "fieldtype": "Data",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_retail_stock_entry",
            },
            {
                "fieldname": "custom_nkt_external_reserved_qty",
                "label": "External Reserved Quantity",
                "fieldtype": "Float",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_fulfillment_status",
            },
            {
                "fieldname": "custom_nkt_external_released_qty",
                "label": "External Released Quantity",
                "fieldtype": "Float",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_external_reserved_qty",
            },
        ],
        "NKT Customer Order Item": [
            {
                "fieldname": "custom_nkt_stock_reservation_entry",
                "label": "Stock Reservation Entry",
                "fieldtype": "Link",
                "options": "Stock Reservation Entry",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "source_warehouse",
            },
            {
                "fieldname": "custom_nkt_reserved_qty",
                "label": "Reserved Quantity",
                "fieldtype": "Float",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_stock_reservation_entry",
            },
            {
                "fieldname": "custom_nkt_released_qty",
                "label": "Released Quantity",
                "fieldtype": "Float",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_reserved_qty",
            },
        ],
        "NKT Warehouse Release": [
            {
                "fieldname": "custom_nkt_source_warehouse",
                "label": "Source Warehouse",
                "fieldtype": "Link",
                "options": "Warehouse",
                "read_only": 1,
                "in_list_view": 1,
                "no_copy": 1,
                "insert_after": "customer_order",
            },
            {
                "fieldname": "custom_nkt_mother_release_reference",
                "label": "Release Authorization Reference",
                "fieldtype": "Data",
                "unique": 1,
                "in_list_view": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_source_warehouse",
                "description": (
                    "Unique reference from the authorized release instruction. "
                    "Required before submission."
                ),
            },
            {
                "fieldname": "custom_nkt_driver_name",
                "label": "Driver Name",
                "fieldtype": "Data",
                "insert_after": "custom_nkt_mother_release_reference",
            },
            {
                "fieldname": "custom_nkt_plate_number",
                "label": "Plate Number",
                "fieldtype": "Data",
                "insert_after": "custom_nkt_driver_name",
            },
            {
                "fieldname": "custom_nkt_authorized_by",
                "label": "Authorized By",
                "fieldtype": "Link",
                "options": "User",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_plate_number",
            },
            {
                "fieldname": "custom_nkt_authorized_on",
                "label": "Authorized On",
                "fieldtype": "Datetime",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_authorized_by",
            },
            {
                "fieldname": "custom_nkt_authorized_load_summary",
                "label": "Authorized Load Summary",
                "fieldtype": "Small Text",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_authorized_on",
            },
            {
                "fieldname": "custom_nkt_reservation_status",
                "label": "Reservation Status",
                "fieldtype": "Data",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "release_status",
            },
            {
                "fieldname": "custom_nkt_reservation_applied",
                "label": "Reservation Consumption Applied",
                "fieldtype": "Check",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_reservation_status",
            },
            {
                "fieldname": "custom_nkt_stock_entry",
                "label": "Stock Material Issue",
                "fieldtype": "Link",
                "options": "Stock Entry",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "delivery_note",
            },
        ],
        "NKT Warehouse Release Item": [
            {
                "fieldname": "custom_nkt_stock_reservation_entry",
                "label": "Stock Reservation Entry",
                "fieldtype": "Link",
                "options": "Stock Reservation Entry",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "source_warehouse",
            },
            {
                "fieldname": "custom_nkt_reservation_outstanding_qty",
                "label": "Reservation Outstanding Quantity",
                "fieldtype": "Float",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_stock_reservation_entry",
            },
            {
                "fieldname": "custom_nkt_reservation_consumed_qty",
                "label": "Reservation Quantity Consumed",
                "fieldtype": "Float",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_reservation_outstanding_qty",
            },
        ],
        "Stock Entry": [
            {
                "fieldname": "custom_nkt_customer_order",
                "label": "NKT Customer Order",
                "fieldtype": "Link",
                "options": "NKT Customer Order",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "stock_entry_type",
            },
            {
                "fieldname": "custom_nkt_warehouse_release",
                "label": "NKT Warehouse Release",
                "fieldtype": "Link",
                "options": "NKT Warehouse Release",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_customer_order",
            },
            {
                "fieldname": "custom_nkt_fulfillment_kind",
                "label": "NKT Fulfillment Kind",
                "fieldtype": "Select",
                "options": f"\n{IMMEDIATE_KIND}\n{RELEASE_KIND}",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_warehouse_release",
            },
        ],
    }
    create_custom_fields(custom_fields, update=True)
    _ensure_nkt_sre_voucher_type()

    stock_settings_meta = frappe.get_meta("Stock Settings")
    if stock_settings_meta.has_field("enable_stock_reservation"):
        frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", 1)

    legacy_field = frappe.db.get_value(
        "Custom Field",
        {"dt": "NKT Customer Order", "fieldname": "custom_nkt_retail_delivery_note"},
        "name",
    )
    if legacy_field:
        frappe.db.set_value(
            "Custom Field",
            legacy_field,
            {
                "label": "Legacy Immediate Delivery Note",
                "hidden": 1,
                "description": "Deprecated by NKT Order Fulfillment V1.1.",
            },
            update_modified=False,
        )

    damaged_items = set(
        frappe.get_all(
            "Item", filters={"nkt_damaged_item": ["is", "set"]}, pluck="nkt_damaged_item"
        )
    )
    fraction_items = set(
        frappe.get_all(
            "Item", filters={"nkt_fraction_item": ["is", "set"]}, pluck="nkt_fraction_item"
        )
    )
    for row in frappe.get_all(
        "Item",
        fields=["name", "nkt_stock_form", "disabled", "is_stock_item", "is_sales_item"],
    ):
        code = row.name or ""
        wanted = None
        if code in damaged_items or code.lower().startswith("[damaged]"):
            wanted = DAMAGED_FORM
        elif code in fraction_items or code.lower().startswith("[fraction]"):
            wanted = FRACTION_FORM
        elif (
            not row.nkt_stock_form
            and not cint(row.disabled)
            and cint(row.is_stock_item)
            and cint(row.is_sales_item)
        ):
            wanted = NORMAL_SALE_FORM
        if wanted and row.nkt_stock_form != wanted:
            frappe.db.set_value("Item", row.name, "nkt_stock_form", wanted, update_modified=False)

    retail_warehouses = frappe.get_all(
        "Warehouse",
        filters={"warehouse_name": "NKT Retail Store", "is_group": 0},
        pluck="name",
    )
    for warehouse in retail_warehouses:
        frappe.db.set_value(
            "Warehouse",
            warehouse,
            "custom_nkt_immediate_sale_deduction",
            1,
            update_modified=False,
        )

    restricted_without_authorizer = frappe.get_all(
        "Warehouse",
        filters={
            "is_group": 0,
            "custom_requires_nkt_admin_approval": 1,
            "custom_nkt_release_authorizer_user": ["is", "not set"],
        },
        pluck="name",
    )
    frappe.clear_cache()
    frappe.db.commit()
    return {
        "retail_warehouses_configured": retail_warehouses,
        "restricted_warehouses_without_specific_authorizer": restricted_without_authorizer,
        "message": "NKT V1.2 reservation and controlled release schema installed.",
    }


def _ensure_nkt_sre_voucher_type():
    meta = frappe.get_meta("Stock Reservation Entry")
    field = meta.get_field("voucher_type")
    options = [line.strip() for line in (field.options or "").splitlines() if line.strip()]
    if NKT_ORDER_VOUCHER_TYPE in options:
        return
    options.append(NKT_ORDER_VOUCHER_TYPE)
    make_property_setter(
        "Stock Reservation Entry",
        "voucher_type",
        "options",
        "\n" + "\n".join(options),
        "Text",
    )
    frappe.clear_cache(doctype="Stock Reservation Entry")


def validate_normal_sale_item(item_code, row_index=None):
    if not item_code:
        return
    item = frappe.db.get_value(
        "Item",
        item_code,
        ["disabled", "is_stock_item", "is_sales_item", "nkt_stock_form"],
        as_dict=True,
    )
    if not item:
        frappe.throw(_("Item {0} does not exist.").format(item_code))
    valid = (
        not cint(item.disabled)
        and cint(item.is_stock_item)
        and cint(item.is_sales_item)
        and item.nkt_stock_form == NORMAL_SALE_FORM
    )
    if not valid:
        row_text = _(" on row {0}").format(row_index) if row_index else ""
        frappe.throw(
            _(
                "Item {0}{1} is not allowed in a normal cashier/encoder sale. "
                "Only active 'Saleable Sack' items may be sold here. Use the "
                "Returns or Stock Recovery workflow for damaged/fraction stock."
            ).format(item_code, row_text)
        )


def _warehouse_is_immediate(warehouse):
    return bool(
        cint(
            frappe.db.get_value(
                "Warehouse", warehouse, "custom_nkt_immediate_sale_deduction"
            )
        )
    )


def _material_issue_type():
    name = frappe.db.get_value(
        "Stock Entry Type", {"purpose": "Material Issue", "is_standard": 1}, "name"
    ) or frappe.db.get_value("Stock Entry Type", {"purpose": "Material Issue"}, "name")
    if not name:
        frappe.throw(_("No Stock Entry Type exists for Material Issue."))
    return name


def _posting_datetime(doc):
    return get_datetime(
        doc.get("release_datetime")
        or doc.get("modified")
        or doc.get("creation")
        or now_datetime()
    )


def _stock_entry_item(item_code, quantity, uom, warehouse):
    return {
        "item_code": item_code,
        "qty": flt(quantity),
        "uom": uom,
        "stock_uom": uom,
        "conversion_factor": 1,
        "s_warehouse": warehouse,
    }


def _create_material_issue(
    *,
    company,
    posting_datetime,
    items,
    customer_order=None,
    warehouse_release=None,
    fulfillment_kind,
    remarks,
):
    if not items:
        frappe.throw(_("At least one Material Issue item is required."))
    stock_entry = frappe.get_doc(
        {
            "doctype": "Stock Entry",
            "company": company,
            "purpose": "Material Issue",
            "stock_entry_type": _material_issue_type(),
            "posting_date": posting_datetime.date(),
            "posting_time": posting_datetime.time(),
            "set_posting_time": 1,
            "custom_nkt_customer_order": customer_order,
            "custom_nkt_warehouse_release": warehouse_release,
            "custom_nkt_fulfillment_kind": fulfillment_kind,
            "remarks": remarks,
            "items": items,
        }
    )
    stock_entry.flags.ignore_permissions = True
    stock_entry.insert(ignore_permissions=True)
    stock_entry.submit()
    return stock_entry.name


def _legacy_immediate_delivery_notes(order_name):
    if not frappe.db.has_column("Delivery Note", "custom_nkt_customer_order"):
        return []
    return frappe.get_all(
        "Delivery Note",
        filters={
            "custom_nkt_customer_order": order_name,
            "custom_nkt_fulfillment_kind": IMMEDIATE_KIND,
            "docstatus": ["!=", 2],
        },
        pluck="name",
    )


def _cancel_or_delete_delivery_note(name):
    if not name or not frappe.db.exists("Delivery Note", name):
        return
    doc = frappe.get_doc("Delivery Note", name)
    doc.flags.ignore_permissions = True
    if doc.docstatus == 1:
        doc.cancel()
    elif doc.docstatus == 0:
        frappe.delete_doc("Delivery Note", name, ignore_permissions=True, force=True)


def _create_immediate_stock_entry(order, rows):
    existing = frappe.db.get_value(
        "Stock Entry",
        {
            "custom_nkt_customer_order": order.name,
            "custom_nkt_fulfillment_kind": IMMEDIATE_KIND,
            "docstatus": ["!=", 2],
        },
        "name",
    )
    if existing:
        entry = frappe.get_doc("Stock Entry", existing)
        if entry.docstatus == 0:
            entry.flags.ignore_permissions = True
            entry.submit()
        frappe.db.set_value(
            "NKT Customer Order",
            order.name,
            "custom_nkt_retail_stock_entry",
            existing,
            update_modified=False,
        )
        return existing

    legacy = _legacy_immediate_delivery_notes(order.name)
    if legacy:
        frappe.throw(
            _(
                "Legacy Delivery Note {0} still exists for Customer Order {1}. "
                "Run repair_order before retrying fulfillment."
            ).format(", ".join(legacy), order.name)
        )

    items = [
        _stock_entry_item(row.item, row.quantity, row.uom, row.source_warehouse)
        for row in rows
        if flt(row.quantity) > TOLERANCE
    ]
    name = _create_material_issue(
        company=order.company,
        posting_datetime=_posting_datetime(order),
        items=items,
        customer_order=order.name,
        fulfillment_kind=IMMEDIATE_KIND,
        remarks=(
            "Created automatically when encoder submitted "
            f"NKT Customer Order {order.name}. Official immediate Retail Store deduction."
        ),
    )
    frappe.db.set_value(
        "NKT Customer Order",
        order.name,
        "custom_nkt_retail_stock_entry",
        name,
        update_modified=False,
    )
    return name


def _active_reservation_for_row(order_name, row_name):
    return frappe.db.get_value(
        "Stock Reservation Entry",
        {
            "voucher_type": NKT_ORDER_VOUCHER_TYPE,
            "voucher_no": order_name,
            "voucher_detail_no": row_name,
            "docstatus": 1,
        },
        "name",
    )


def _reservation_outstanding(sre):
    return max(
        flt(sre.reserved_qty)
        - flt(sre.delivered_qty)
        - flt(sre.transferred_qty)
        - flt(sre.consumed_qty),
        0,
    )


def _create_external_reservation(order, row):
    existing = _active_reservation_for_row(order.name, row.name)
    if existing:
        _refresh_order_row_reservation_fields(row.name, existing)
        return existing

    draft = frappe.db.get_value(
        "Stock Reservation Entry",
        {
            "voucher_type": NKT_ORDER_VOUCHER_TYPE,
            "voucher_no": order.name,
            "voucher_detail_no": row.name,
            "docstatus": 0,
        },
        "name",
    )
    if draft:
        sre = frappe.get_doc("Stock Reservation Entry", draft)
        sre.flags.ignore_permissions = True
        sre.submit()
        _refresh_order_row_reservation_fields(row.name, sre.name)
        return sre.name

    from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
        get_available_qty_to_reserve,
    )

    item = frappe.db.get_value(
        "Item",
        row.item,
        ["stock_uom", "has_serial_no", "has_batch_no"],
        as_dict=True,
    )
    stock_uom = item.stock_uom or row.uom
    qty = flt(row.quantity)
    available = flt(get_available_qty_to_reserve(row.item, row.source_warehouse))
    if available + TOLERANCE < qty:
        frappe.throw(
            _(
                "Cannot reserve {0} {1} of {2} in {3}. Available to reserve: {4}. "
                "The encoder order was not completed because this would oversell external stock."
            ).format(qty, stock_uom, row.item, row.source_warehouse, available)
        )

    sre = frappe.get_doc(
        {
            "doctype": "Stock Reservation Entry",
            "voucher_type": NKT_ORDER_VOUCHER_TYPE,
            "voucher_no": order.name,
            "voucher_detail_no": row.name,
            "voucher_qty": qty,
            "available_qty": available,
            "reserved_qty": qty,
            "item_code": row.item,
            "warehouse": row.source_warehouse,
            "stock_uom": stock_uom,
            "company": order.company,
            "has_serial_no": cint(item.has_serial_no),
            "has_batch_no": cint(item.has_batch_no),
            "reservation_based_on": "Qty",
        }
    )
    sre.flags.ignore_permissions = True
    sre.insert(ignore_permissions=True)
    sre.submit()
    _refresh_order_row_reservation_fields(row.name, sre.name)
    return sre.name


def _refresh_order_row_reservation_fields(row_name, sre_name=None):
    if not row_name or not frappe.db.exists("NKT Customer Order Item", row_name):
        return
    if not sre_name:
        parent = frappe.db.get_value("NKT Customer Order Item", row_name, "parent")
        sre_name = _active_reservation_for_row(parent, row_name)
    reserved = released = 0
    if sre_name and frappe.db.exists("Stock Reservation Entry", sre_name):
        data = frappe.db.get_value(
            "Stock Reservation Entry",
            sre_name,
            ["reserved_qty", "delivered_qty", "docstatus"],
            as_dict=True,
        )
        if data and data.docstatus == 1:
            reserved = flt(data.reserved_qty)
            released = flt(data.delivered_qty)
        else:
            sre_name = None
    frappe.db.set_value(
        "NKT Customer Order Item",
        row_name,
        {
            "custom_nkt_stock_reservation_entry": sre_name,
            "custom_nkt_reserved_qty": reserved,
            "custom_nkt_released_qty": released,
        },
        update_modified=False,
    )


def _submitted_release_map(order_name):
    rows = frappe.db.sql(
        """
        SELECT
            item.customer_order_item,
            COALESCE(SUM(item.release_quantity), 0) AS released_qty
        FROM `tabNKT Warehouse Release Item` item
        INNER JOIN `tabNKT Warehouse Release` rel
            ON rel.name = item.parent
            AND item.parenttype = 'NKT Warehouse Release'
            AND item.parentfield = 'items'
        WHERE rel.customer_order = %s
          AND rel.docstatus = 1
        GROUP BY item.customer_order_item
        """,
        order_name,
        as_dict=True,
    )
    return {row.customer_order_item: flt(row.released_qty) for row in rows}


def _remaining_rows_for_warehouse(order, warehouse):
    released_map = _submitted_release_map(order.name)
    result = []
    for row in order.get("items") or []:
        if row.source_warehouse != warehouse or _warehouse_is_immediate(warehouse):
            continue
        ordered = flt(row.quantity)
        released = min(flt(released_map.get(row.name)), ordered)
        remaining = max(ordered - released, 0)
        if remaining <= TOLERANCE:
            continue
        sre_name = _active_reservation_for_row(order.name, row.name)
        if not sre_name:
            frappe.throw(
                _(
                    "External order row {0} has no active Stock Reservation Entry. "
                    "Run repair_order for {1}."
                ).format(row.idx, order.name)
            )
        sre = frappe.get_doc("Stock Reservation Entry", sre_name)
        result.append(
            frappe._dict(
                order_row=row,
                ordered=ordered,
                released=released,
                remaining=remaining,
                reservation=sre_name,
                reservation_outstanding=_reservation_outstanding(sre),
            )
        )
    return result


def _sync_draft_release(order, warehouse):
    remaining_rows = _remaining_rows_for_warehouse(order, warehouse)
    drafts = frappe.get_all(
        "NKT Warehouse Release",
        filters={
            "customer_order": order.name,
            "custom_nkt_source_warehouse": warehouse,
            "docstatus": 0,
        },
        order_by="creation asc",
        pluck="name",
    )

    if not remaining_rows:
        for draft_name in drafts:
            frappe.delete_doc(
                "NKT Warehouse Release", draft_name, ignore_permissions=True, force=True
            )
        return None

    keep = drafts[0] if drafts else None
    for duplicate in drafts[1:]:
        frappe.delete_doc(
            "NKT Warehouse Release", duplicate, ignore_permissions=True, force=True
        )

    if keep:
        release = frappe.get_doc("NKT Warehouse Release", keep)
        previous_qty = {
            row.customer_order_item: flt(row.release_quantity)
            for row in (release.get("items") or [])
        }
    else:
        release = frappe.get_doc(
            {
                "doctype": "NKT Warehouse Release",
                "company": order.company,
                "customer_order": order.name,
                "customer": order.customer,
                "customer_name": order.customer_name,
                "custom_nkt_source_warehouse": warehouse,
                "release_datetime": now_datetime(),
                "released_by": frappe.session.user,
                "release_status": "Draft",
                "custom_nkt_reservation_status": "Reserved - Awaiting Release",
                "remarks": (
                    "Generated automatically from encoder Customer Order "
                    f"{order.name}. Physical stock remains in the warehouse until submission."
                ),
            }
        )
        previous_qty = {}

    release.company = order.company
    release.customer = order.customer
    release.customer_name = order.customer_name
    release.customer_order = order.name
    release.custom_nkt_source_warehouse = warehouse
    release.release_status = "Draft"
    release.custom_nkt_reservation_status = "Reserved - Awaiting Release"
    release.set("items", [])
    for data in remaining_rows:
        row = data.order_row
        old_qty = previous_qty.get(row.name)
        proposed = data.remaining if old_qty is None or old_qty <= TOLERANCE else old_qty
        release.append(
            "items",
            {
                "customer_order_item": row.name,
                "item": row.item,
                "item_name": row.item_name,
                "ordered_quantity": data.ordered,
                "previously_released_quantity": data.released,
                "remaining_quantity": data.remaining,
                "release_quantity": min(proposed, data.remaining),
                "uom": row.uom,
                "source_warehouse": warehouse,
                "custom_nkt_stock_reservation_entry": data.reservation,
                "custom_nkt_reservation_outstanding_qty": data.reservation_outstanding,
            },
        )

    release.flags.ignore_permissions = True
    if release.is_new():
        release.insert(ignore_permissions=True)
    else:
        release.save(ignore_permissions=True)
    return release.name


def _external_warehouses(order):
    return sorted(
        {
            row.source_warehouse
            for row in (order.get("items") or [])
            if row.source_warehouse and not _warehouse_is_immediate(row.source_warehouse)
        }
    )


def process_customer_order_fulfillment(order_name):
    """Deduct immediate stock, reserve external stock, and maintain one draft per warehouse."""
    order = frappe.get_doc("NKT Customer Order", order_name)
    if order.docstatus != 1:
        return {"skipped": "Customer Order is not submitted."}

    immediate_rows = []
    external_rows = defaultdict(list)
    for row in order.get("items") or []:
        if flt(row.quantity) <= TOLERANCE:
            continue
        if _warehouse_is_immediate(row.source_warehouse):
            immediate_rows.append(row)
        else:
            external_rows[row.source_warehouse].append(row)

    stock_entry = None
    if immediate_rows:
        stock_entry = _create_immediate_stock_entry(order, immediate_rows)

    reservations = []
    releases = []
    for warehouse, rows in sorted(external_rows.items()):
        for row in rows:
            reservations.append(_create_external_reservation(order, row))
        release_name = _sync_draft_release(order, warehouse)
        if release_name:
            releases.append(release_name)

    status = update_customer_order_fulfillment_status(order.name)
    return {
        "customer_order": order.name,
        "immediate_stock_entry": stock_entry,
        "stock_reservations": reservations,
        "warehouse_releases": releases,
        "fulfillment_status": status,
    }


def load_release_items_from_order(release):
    order = frappe.get_doc("NKT Customer Order", release.customer_order)
    warehouse = release.get("custom_nkt_source_warehouse")
    if not warehouse:
        warehouses = _external_warehouses(order)
        if len(warehouses) != 1:
            frappe.throw(
                _(
                    "Warehouse Release slips are generated automatically per warehouse. "
                    "Open the generated slip from Customer Order {0}."
                ).format(order.name)
            )
        warehouse = warehouses[0]
        release.custom_nkt_source_warehouse = warehouse

    rows = _remaining_rows_for_warehouse(order, warehouse)
    if not rows:
        frappe.throw(_("No unreleased reserved quantity remains in {0}.").format(warehouse))

    release.company = order.company
    release.customer = order.customer
    release.customer_name = order.customer_name
    release.set("items", [])
    for data in rows:
        row = data.order_row
        release.append(
            "items",
            {
                "customer_order_item": row.name,
                "item": row.item,
                "item_name": row.item_name,
                "ordered_quantity": data.ordered,
                "previously_released_quantity": data.released,
                "remaining_quantity": data.remaining,
                "release_quantity": data.remaining,
                "uom": row.uom,
                "source_warehouse": warehouse,
                "custom_nkt_stock_reservation_entry": data.reservation,
                "custom_nkt_reservation_outstanding_qty": data.reservation_outstanding,
            },
        )


def _release_load_summary(release):
    parts = []
    for row in release.get("items") or []:
        qty = flt(row.release_quantity)
        if qty > TOLERANCE:
            parts.append(f"{row.item}: {qty:g} {row.uom}")
    return "; ".join(parts)


def validate_warehouse_release_document(release, for_submit=False):
    if not release.customer_order:
        frappe.throw(_("Customer Order is required."))
    order = frappe.get_doc("NKT Customer Order", release.customer_order)
    warehouse = release.get("custom_nkt_source_warehouse")
    if not warehouse:
        frappe.throw(_("Source Warehouse is required on the generated release slip."))
    if _warehouse_is_immediate(warehouse):
        frappe.throw(
            _("Immediate-deduction warehouse {0} must not use a Warehouse Release.").format(
                warehouse
            )
        )
    if order.company != release.company or order.customer != release.customer:
        frappe.throw(_("Release company/customer does not match the Customer Order."))

    order_rows = {row.name: row for row in (order.get("items") or [])}
    released_map = _submitted_release_map(order.name)
    seen = set()
    for row in release.get("items") or []:
        if row.customer_order_item in seen:
            frappe.throw(_("Order item {0} appears more than once.").format(row.customer_order_item))
        seen.add(row.customer_order_item)
        order_row = order_rows.get(row.customer_order_item)
        if not order_row:
            frappe.throw(_("Release row {0} is not linked to this Customer Order.").format(row.idx))
        if order_row.source_warehouse != warehouse or row.source_warehouse != warehouse:
            frappe.throw(
                _(
                    "Release row {0} belongs to {1}, but this slip is only for {2}. "
                    "Cross-warehouse release rows are blocked."
                ).format(row.idx, order_row.source_warehouse, warehouse)
            )
        if row.item != order_row.item or row.uom != order_row.uom:
            frappe.throw(_("Release row {0} no longer matches the original order item.").format(row.idx))

        sre_name = row.get("custom_nkt_stock_reservation_entry") or _active_reservation_for_row(
            order.name, order_row.name
        )
        if not sre_name:
            frappe.throw(_("No active stock reservation exists for release row {0}.").format(row.idx))
        sre = frappe.get_doc("Stock Reservation Entry", sre_name)
        if (
            sre.docstatus != 1
            or sre.voucher_no != order.name
            or sre.voucher_detail_no != order_row.name
            or sre.item_code != order_row.item
            or sre.warehouse != warehouse
        ):
            frappe.throw(_("Stock reservation {0} does not belong to this row.").format(sre_name))

        outstanding = _reservation_outstanding(sre)
        already_released = min(flt(released_map.get(order_row.name)), flt(order_row.quantity))
        remaining = max(flt(order_row.quantity) - already_released, 0)
        qty = flt(row.release_quantity)
        # NKT_C15F_R4B_WAREHOUSE_RELEASE_BIN_SERIALIZATION
        frappe.db.sql(
            "SELECT name, actual_qty FROM `tabBin` WHERE item_code=%s AND warehouse=%s FOR UPDATE",
            (row.item, warehouse),
            as_dict=True,
        )
        actual_qty = flt(
            frappe.db.get_value(
                "Bin",
                {"item_code": row.item, "warehouse": warehouse},
                "actual_qty",
            )
        )
        if qty > actual_qty + TOLERANCE:
            frappe.throw(
                _(
                    "Release quantity on row {0} exceeds physical stock {1} in {2}. "
                    "NKT warehouse release does not permit negative physical stock."
                ).format(row.idx, actual_qty, warehouse)
            )
        if qty > remaining + TOLERANCE:
            frappe.throw(
                _("Release quantity on row {0} exceeds remaining order quantity {1}.").format(
                    row.idx, remaining
                )
            )
        if qty > outstanding + TOLERANCE:
            frappe.throw(
                _("Release quantity on row {0} exceeds reservation outstanding quantity {1}.").format(
                    row.idx, outstanding
                )
            )
        row.custom_nkt_stock_reservation_entry = sre.name
        row.custom_nkt_reservation_outstanding_qty = outstanding

    other_drafts = frappe.get_all(
        "NKT Warehouse Release",
        filters={
            "customer_order": order.name,
            "custom_nkt_source_warehouse": warehouse,
            "docstatus": 0,
            "name": ["!=", release.name or ""],
        },
        pluck="name",
    )
    if other_drafts:
        frappe.throw(
            _("Another draft release already exists for this order and warehouse: {0}.").format(
                ", ".join(other_drafts)
            )
        )

    if not for_submit:
        return

    reference = (release.get("custom_nkt_mother_release_reference") or "").strip().upper()
    driver = (release.get("custom_nkt_driver_name") or "").strip()
    plate = (release.get("custom_nkt_plate_number") or "").strip().upper()
    if not reference:
        frappe.throw(_("Release Authorization Reference is required before submission."))
    # C4.1.4.3: Driver and Plate are optional operational/audit details.
    # Customer pickup and other legitimate warehouse releases may have neither.

    duplicate = frappe.db.get_value(
        "NKT Warehouse Release",
        {
            "custom_nkt_mother_release_reference": reference,
            "name": ["!=", release.name],
        },
        "name",
    )
    if duplicate:
        frappe.throw(
            _("Release Authorization Reference {0} is already used by {1}.").format(
                reference, duplicate
            )
        )

    # NKT_C4_1_4_3_NO_PRE_RELEASE_ADMIN_GATE
    # Physical warehouse release is independent. Legacy Warehouse/Admin confirmation
    # configuration is retained only as historical metadata and does not gate release.
    update_customer_order_fulfillment_status(order.name)
    order.reload()
    if order.status not in {"Ready for Release", "Partially Released"}:
        frappe.throw(
            _(
                "Customer Order {0} is not ready for physical release. Current status: {1}; "
                "payment/credit and warehouse controls must be completed first."
            ).format(order.name, order.status)
        )

    release.custom_nkt_mother_release_reference = reference
    release.custom_nkt_driver_name = driver
    release.custom_nkt_plate_number = plate
    if release.flags.get("nkt_c15c_preserve_offline_release"):
        release.custom_nkt_authorized_by = release.released_by
        release.custom_nkt_authorized_on = release.release_datetime
    else:
        release.custom_nkt_authorized_by = frappe.session.user
        release.custom_nkt_authorized_on = now_datetime()
    release.custom_nkt_authorized_load_summary = _release_load_summary(release)
    release.custom_nkt_reservation_status = "Authorized for Release"


def _adjust_reservation_delivery(sre_name, delta):
    row = frappe.db.sql(
        """
        SELECT reserved_qty, delivered_qty, transferred_qty, consumed_qty
        FROM `tabStock Reservation Entry`
        WHERE name = %s AND docstatus = 1
        FOR UPDATE
        """,
        sre_name,
        as_dict=True,
    )
    if not row:
        frappe.throw(_("Active Stock Reservation Entry {0} was not found.").format(sre_name))
    data = row[0]
    current = flt(data.delivered_qty)
    maximum = max(
        flt(data.reserved_qty) - flt(data.transferred_qty) - flt(data.consumed_qty), 0
    )
    new_value = current + flt(delta)
    if new_value < -TOLERANCE or new_value > maximum + TOLERANCE:
        frappe.throw(
            _("Reservation delivery update for {0} would be outside 0 to {1}.").format(
                sre_name, maximum
            )
        )
    new_value = min(max(new_value, 0), maximum)
    sre = frappe.get_doc("Stock Reservation Entry", sre_name)
    sre.db_set("delivered_qty", new_value, update_modified=False)
    sre.reload()
    sre.update_status(update_modified=False)
    sre.update_reserved_stock_in_bin()
    _refresh_order_row_reservation_fields(sre.voucher_detail_no, sre.name)


def apply_warehouse_release_reservations(release_name):
    release = frappe.get_doc("NKT Warehouse Release", release_name)
    if cint(release.get("custom_nkt_reservation_applied")):
        return
    for row in release.get("items") or []:
        qty = flt(row.release_quantity)
        if qty <= TOLERANCE:
            continue
        sre_name = row.get("custom_nkt_stock_reservation_entry")
        _adjust_reservation_delivery(sre_name, qty)
        frappe.db.set_value(
            "NKT Warehouse Release Item",
            row.name,
            "custom_nkt_reservation_consumed_qty",
            qty,
            update_modified=False,
        )
    frappe.db.set_value(
        "NKT Warehouse Release",
        release.name,
        {
            "custom_nkt_reservation_applied": 1,
            "custom_nkt_reservation_status": "Released - Reservation Reduced",
        },
        update_modified=False,
    )


def restore_warehouse_release_reservations(release_name):
    release = frappe.get_doc("NKT Warehouse Release", release_name)
    if not cint(release.get("custom_nkt_reservation_applied")):
        return
    for row in release.get("items") or []:
        consumed = flt(row.get("custom_nkt_reservation_consumed_qty"))
        if consumed <= TOLERANCE:
            continue
        _adjust_reservation_delivery(row.custom_nkt_stock_reservation_entry, -consumed)
        frappe.db.set_value(
            "NKT Warehouse Release Item",
            row.name,
            "custom_nkt_reservation_consumed_qty",
            0,
            update_modified=False,
        )
    frappe.db.set_value(
        "NKT Warehouse Release",
        release.name,
        {
            "custom_nkt_reservation_applied": 0,
            "custom_nkt_reservation_status": "Cancelled - Reservation Restored",
        },
        update_modified=False,
    )


def sync_next_warehouse_release(order_name, warehouse):
    order = frappe.get_doc("NKT Customer Order", order_name)
    return _sync_draft_release(order, warehouse)


def update_customer_order_fulfillment_status(order_name):
    order = frappe.get_doc("NKT Customer Order", order_name)
    immediate_rows = [
        row for row in (order.get("items") or []) if _warehouse_is_immediate(row.source_warehouse)
    ]
    external_rows = [
        row for row in (order.get("items") or []) if not _warehouse_is_immediate(row.source_warehouse)
    ]
    external_total = sum(flt(row.quantity) for row in external_rows)
    released_map = _submitted_release_map(order.name)
    external_released = sum(
        min(flt(released_map.get(row.name)), flt(row.quantity)) for row in external_rows
    )
    external_outstanding = max(external_total - external_released, 0)

    stock_entry = order.get("custom_nkt_retail_stock_entry")
    immediate_done = not immediate_rows or bool(
        stock_entry
        and frappe.db.get_value("Stock Entry", stock_entry, "docstatus") == 1
    )

    if stock_entry and external_total:
        fulfillment_text = (
            f"Store Stock Deducted; External Reserved {external_outstanding:g}; "
            f"Released {external_released:g}"
        )
    elif stock_entry:
        fulfillment_text = "Store Stock Deducted"
    elif external_total:
        fulfillment_text = (
            f"External Reserved {external_outstanding:g}; Released {external_released:g}"
        )
    else:
        fulfillment_text = "No Fulfillment Rows"

    new_status = order.status
    payment_status = order.payment_status or ""
    current = order.status or ""
    payment_ready = payment_status == "Paid" or (
        payment_status == "Charged to Account" and current != "Pending Credit Control"
    ) or current in {"Ready for Release", "Partially Released", "Released"}

    if external_total > TOLERANCE:
        if external_released >= external_total - TOLERANCE:
            new_status = "Released"
        elif external_released > TOLERANCE:
            new_status = "Partially Released"
        elif payment_ready:
            # C4.1.4.3: matched/settled external stock with unreleased quantity is
            # operationally ready. Admin review is post-release reconciliation/audit.
            new_status = "Ready for Release"
    elif immediate_done and payment_ready:
        new_status = "Released"

    frappe.db.set_value(
        "NKT Customer Order",
        order.name,
        {
            "custom_nkt_external_reserved_qty": external_outstanding,
            "custom_nkt_external_released_qty": external_released,
            "custom_nkt_fulfillment_status": fulfillment_text,
            "status": new_status,
        },
        update_modified=False,
    )
    return fulfillment_text


def create_warehouse_release_material_issue(release_name):
    release = frappe.get_doc("NKT Warehouse Release", release_name)
    existing = frappe.db.get_value(
        "Stock Entry",
        {
            "custom_nkt_warehouse_release": release.name,
            "custom_nkt_fulfillment_kind": RELEASE_KIND,
            "docstatus": ["!=", 2],
        },
        "name",
    )
    if existing:
        entry = frappe.get_doc("Stock Entry", existing)
        if entry.docstatus == 0:
            entry.flags.ignore_permissions = True
            entry.submit()
        release.db_set("custom_nkt_stock_entry", existing, update_modified=False)
        return existing

    items = []
    for row in release.get("items") or []:
        if flt(row.release_quantity) <= TOLERANCE:
            continue
        items.append(
            _stock_entry_item(row.item, row.release_quantity, row.uom, row.source_warehouse)
        )
    name = _create_material_issue(
        company=release.company,
        posting_datetime=_posting_datetime(release),
        items=items,
        customer_order=release.customer_order,
        warehouse_release=release.name,
        fulfillment_kind=RELEASE_KIND,
        remarks=(
            f"Created automatically from NKT Warehouse Release {release.name} "
            f"for Customer Order {release.customer_order}."
        ),
    )
    release.db_set("custom_nkt_stock_entry", name, update_modified=False)
    if release.get("delivery_note"):
        release.db_set("delivery_note", None, update_modified=False)
    return name


def cancel_warehouse_release_material_issue(release_name):
    linked = frappe.db.get_value(
        "NKT Warehouse Release", release_name, "custom_nkt_stock_entry"
    )
    if linked:
        frappe.db.set_value(
            "NKT Warehouse Release",
            release_name,
            "custom_nkt_stock_entry",
            None,
            update_modified=False,
        )
    names = frappe.get_all(
        "Stock Entry",
        filters={
            "custom_nkt_warehouse_release": release_name,
            "custom_nkt_fulfillment_kind": RELEASE_KIND,
            "docstatus": ["!=", 2],
        },
        pluck="name",
    )
    for name in names:
        entry = frappe.get_doc("Stock Entry", name)
        entry.flags.ignore_permissions = True
        if entry.docstatus == 1:
            entry.cancel()
        elif entry.docstatus == 0:
            frappe.delete_doc("Stock Entry", name, ignore_permissions=True, force=True)


def cancel_customer_order_fulfillment(order_name):
    submitted_releases = frappe.get_all(
        "NKT Warehouse Release",
        filters={"customer_order": order_name, "docstatus": 1},
        pluck="name",
    )
    if submitted_releases:
        frappe.throw(
            _(
                "Cancel submitted Warehouse Release(s) {0} before cancelling Customer Order {1}."
            ).format(", ".join(submitted_releases), order_name)
        )

    for name in frappe.get_all(
        "NKT Warehouse Release",
        filters={"customer_order": order_name, "docstatus": 0},
        pluck="name",
    ):
        frappe.delete_doc("NKT Warehouse Release", name, ignore_permissions=True, force=True)

    stock_link = frappe.db.get_value(
        "NKT Customer Order", order_name, "custom_nkt_retail_stock_entry"
    )
    if stock_link:
        frappe.db.set_value(
            "NKT Customer Order",
            order_name,
            "custom_nkt_retail_stock_entry",
            None,
            update_modified=False,
        )
    names = frappe.get_all(
        "Stock Entry",
        filters={
            "custom_nkt_customer_order": order_name,
            "custom_nkt_fulfillment_kind": IMMEDIATE_KIND,
            "docstatus": ["!=", 2],
        },
        pluck="name",
    )
    for name in names:
        entry = frappe.get_doc("Stock Entry", name)
        entry.flags.ignore_permissions = True
        if entry.docstatus == 1:
            entry.cancel()
        elif entry.docstatus == 0:
            frappe.delete_doc("Stock Entry", name, ignore_permissions=True, force=True)

    for sre_name in frappe.get_all(
        "Stock Reservation Entry",
        filters={
            "voucher_type": NKT_ORDER_VOUCHER_TYPE,
            "voucher_no": order_name,
            "docstatus": ["!=", 2],
        },
        pluck="name",
    ):
        sre = frappe.get_doc("Stock Reservation Entry", sre_name)
        if flt(sre.delivered_qty) > TOLERANCE:
            frappe.throw(
                _("Reservation {0} has released quantity and cannot be cancelled yet.").format(
                    sre.name
                )
            )
        sre.flags.ignore_permissions = True
        if sre.docstatus == 1:
            sre.cancel()
        else:
            frappe.delete_doc(
                "Stock Reservation Entry", sre.name, ignore_permissions=True, force=True
            )

    for name in _legacy_immediate_delivery_notes(order_name):
        _cancel_or_delete_delivery_note(name)


def _convert_legacy_immediate_delivery_note(order_name):
    converted = []
    legacy_names = _legacy_immediate_delivery_notes(order_name)
    has_legacy_link = frappe.db.has_column(
        "NKT Customer Order", "custom_nkt_retail_delivery_note"
    )
    linked_delivery_note = None
    if has_legacy_link:
        linked_delivery_note = frappe.db.get_value(
            "NKT Customer Order", order_name, "custom_nkt_retail_delivery_note"
        )
    for name in legacy_names:
        if has_legacy_link and linked_delivery_note == name:
            frappe.db.set_value(
                "NKT Customer Order",
                order_name,
                "custom_nkt_retail_delivery_note",
                None,
                update_modified=False,
            )
            linked_delivery_note = None
        if frappe.db.has_column("NKT Warehouse Release", "delivery_note"):
            for release_name in frappe.get_all(
                "NKT Warehouse Release", filters={"delivery_note": name}, pluck="name"
            ):
                frappe.db.set_value(
                    "NKT Warehouse Release",
                    release_name,
                    "delivery_note",
                    None,
                    update_modified=False,
                )
        _cancel_or_delete_delivery_note(name)
        converted.append(name)
    if has_legacy_link and linked_delivery_note:
        frappe.db.set_value(
            "NKT Customer Order",
            order_name,
            "custom_nkt_retail_delivery_note",
            None,
            update_modified=False,
        )
    return converted


@frappe.whitelist()
def repair_order(order_name):
    if not frappe.db.exists("NKT Customer Order", order_name):
        frappe.throw(_("Customer Order {0} does not exist.").format(order_name))
    converted = _convert_legacy_immediate_delivery_note(order_name)
    result = process_customer_order_fulfillment(order_name)
    result["cancelled_legacy_delivery_notes"] = converted
    frappe.db.commit()
    return result


@frappe.whitelist()
def repair_warehouse_release(release_name):
    release = frappe.get_doc("NKT Warehouse Release", release_name)
    legacy_dn = release.get("delivery_note")
    if legacy_dn:
        release.db_set("delivery_note", None, update_modified=False)
        _cancel_or_delete_delivery_note(legacy_dn)
    stock_entry = create_warehouse_release_material_issue(release.name)
    frappe.db.commit()
    return {
        "warehouse_release": release.name,
        "cancelled_legacy_delivery_note": legacy_dn,
        "stock_entry": stock_entry,
    }


@frappe.whitelist()
def get_release_data(customer_order, warehouse_release=None):
    order = frappe.get_doc("NKT Customer Order", customer_order)
    warehouse = None
    if warehouse_release:
        warehouse = frappe.db.get_value(
            "NKT Warehouse Release", warehouse_release, "custom_nkt_source_warehouse"
        )
    if not warehouse:
        warehouses = _external_warehouses(order)
        if len(warehouses) != 1:
            frappe.throw(
                _(
                    "This order has multiple external warehouses. Use the automatically generated "
                    "warehouse-specific release slips."
                )
            )
        warehouse = warehouses[0]
    rows = _remaining_rows_for_warehouse(order, warehouse)
    if not rows:
        frappe.throw(_("No unreleased reserved quantity remains in {0}.").format(warehouse))
    return {
        "company": order.company,
        "customer": order.customer,
        "customer_name": order.customer_name,
        "custom_nkt_source_warehouse": warehouse,
        "items": [
            {
                "customer_order_item": data.order_row.name,
                "item": data.order_row.item,
                "item_name": data.order_row.item_name,
                "ordered_quantity": data.ordered,
                "previously_released_quantity": data.released,
                "remaining_quantity": data.remaining,
                "release_quantity": data.remaining,
                "uom": data.order_row.uom,
                "source_warehouse": warehouse,
                "custom_nkt_stock_reservation_entry": data.reservation,
                "custom_nkt_reservation_outstanding_qty": data.reservation_outstanding,
            }
            for data in rows
        ],
    }


# NKT_C4_1_4_2_OPTIONAL_DISPATCH_CONTROLLER
# Binding decision: Driver and Plate Number are audit/dispatch details when known,
# not preconditions for physical warehouse release. Preserve every existing
# warehouse-release validation by calling the accepted validator. Only the two
# legacy presence checks are satisfied transiently; the document values are
# restored before returning, so blank optional fields remain blank in the record.
_nkt_c4142_original_validate_warehouse_release_document = validate_warehouse_release_document


def validate_warehouse_release_document(doc, for_submit=False):
    _nkt_c4142_optional_dispatch_values = (
        ("custom_nkt_driver_name", "__NKT_OPTIONAL_DRIVER__"),
        ("custom_nkt_plate_number", "__NKT_OPTIONAL_PLATE__"),
        ("driver_name", "__NKT_OPTIONAL_DRIVER__"),
        ("plate_number", "__NKT_OPTIONAL_PLATE__"),
    )
    restored = []
    if for_submit:
        meta = getattr(doc, "meta", None)
        for fieldname, placeholder in _nkt_c4142_optional_dispatch_values:
            has_field = bool(meta and meta.has_field(fieldname))
            if not has_field:
                continue
            original = doc.get(fieldname)
            if not (original or "").strip():
                restored.append((fieldname, original))
                doc.set(fieldname, placeholder)
    try:
        return _nkt_c4142_original_validate_warehouse_release_document(doc, for_submit=for_submit)
    finally:
        for fieldname, original in restored:
            doc.set(fieldname, original)

