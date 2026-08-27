import re

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import (
    create_custom_fields,
)
from frappe.utils import flt

from nkt_operations.nkt_store_operations.features.security.role_hierarchy import (
    require_nkt_authority,
)


def install_item_mapping_fields():
    custom_fields = {
        "Item": [
            {
                "fieldname": "nkt_stock_structure_section",
                "label": "NKT Damaged and Fraction Stock",
                "fieldtype": "Section Break",
                "insert_after": "stock_uom",
                "collapsible": 1,
            },
            {
                "fieldname": "nkt_stock_form",
                "label": "NKT Stock Form",
                "fieldtype": "Select",
                "options": (
                    "\n"
                    "Saleable Sack\n"
                    "Damaged Sack\n"
                    "Fraction Stock"
                ),
                "insert_after": "nkt_stock_structure_section",
                "in_standard_filter": 1,
            },
            {
                "fieldname": "nkt_standard_sack_weight_kg",
                "label": "Standard Sack Weight (kg)",
                "fieldtype": "Float",
                "precision": "3",
                "insert_after": "nkt_stock_form",
                "depends_on": (
                    'eval:doc.nkt_stock_form=="Saleable Sack"'
                ),
            },
            {
                "fieldname": "nkt_damaged_item",
                "label": "Linked Damaged Item",
                "fieldtype": "Link",
                "options": "Item",
                "insert_after": "nkt_standard_sack_weight_kg",
                "depends_on": (
                    'eval:doc.nkt_stock_form=="Saleable Sack"'
                ),
            },
            {
                "fieldname": "nkt_fraction_item",
                "label": "Linked Fraction Item",
                "fieldtype": "Link",
                "options": "Item",
                "insert_after": "nkt_damaged_item",
                "depends_on": (
                    'eval:doc.nkt_stock_form=="Saleable Sack"'
                ),
            },
            {
                "fieldname": "nkt_base_saleable_item",
                "label": "Base Saleable Item",
                "fieldtype": "Link",
                "options": "Item",
                "insert_after": "nkt_fraction_item",
                "depends_on": (
                    'eval:doc.nkt_stock_form=="Damaged Sack"'
                    ' || doc.nkt_stock_form=="Fraction Stock"'
                ),
            },
        ]
    }

    create_custom_fields(
        custom_fields,
        update=True,
    )

    frappe.db.commit()
    frappe.clear_cache()

    return {"status": "installed"}


def get_default_fraction_code(item_code):
    item_code = (item_code or "").strip()

    # Removes a final weight suffix such as:
    # [25kg], [25 kg], (25kg), or (25 kg)
    clean_name = re.sub(
        r"\s*[\[(]\s*\d+(?:\.\d+)?\s*kg\s*[\])]\s*$",
        "",
        item_code,
        flags=re.IGNORECASE,
    ).strip()

    if not clean_name:
        clean_name = item_code

    return f"[Fraction] {clean_name}"


def validate_item_code(item_code, label):
    item_code = (item_code or "").strip()

    if not item_code:
        frappe.throw(
            _("{0} is required.").format(label)
        )

    if len(item_code) > 140:
        frappe.throw(
            _(
                "{0} cannot exceed 140 characters."
            ).format(label)
        )

    return item_code


def create_related_item(
    base_item,
    item_code,
    stock_form,
    stock_uom,
):
    existing_name = frappe.db.exists(
        "Item",
        item_code,
    )

    if existing_name:
        related_item = frappe.get_doc(
            "Item",
            existing_name,
        )

        related_item.check_permission("write")

        if not related_item.is_stock_item:
            frappe.throw(
                _(
                    "Existing Item {0} is not a stock item."
                ).format(related_item.name)
            )

        if related_item.stock_uom != stock_uom:
            frappe.throw(
                _(
                    "Existing Item {0} uses Stock UOM {1}, "
                    "but {2} is required."
                ).format(
                    related_item.name,
                    related_item.stock_uom,
                    stock_uom,
                )
            )

        if (
            related_item.nkt_stock_form
            and related_item.nkt_stock_form
            != stock_form
        ):
            frappe.throw(
                _(
                    "Existing Item {0} is already marked "
                    "as {1}."
                ).format(
                    related_item.name,
                    related_item.nkt_stock_form,
                )
            )

        if (
            related_item.nkt_base_saleable_item
            and related_item.nkt_base_saleable_item
            != base_item.name
        ):
            frappe.throw(
                _(
                    "Existing Item {0} is already linked "
                    "to another saleable item."
                ).format(related_item.name)
            )

        changed = False

        if related_item.nkt_stock_form != stock_form:
            related_item.nkt_stock_form = stock_form
            changed = True

        if (
            related_item.nkt_base_saleable_item
            != base_item.name
        ):
            related_item.nkt_base_saleable_item = (
                base_item.name
            )
            changed = True

        if changed:
            related_item.save()

        return related_item

    if not frappe.has_permission(
        "Item",
        ptype="create",
    ):
        frappe.throw(
            _(
                "You do not have permission to create Item masters."
            ),
            frappe.PermissionError,
        )

    values = {
        "doctype": "Item",
        "item_code": item_code,
        "item_name": item_code,
        "item_group": base_item.item_group,
        "stock_uom": stock_uom,
        "is_stock_item": 1,
        "disabled": 0,
        "nkt_stock_form": stock_form,
        "nkt_base_saleable_item": base_item.name,
        "description": (
            f"{stock_form} linked to "
            f"{base_item.item_code}."
        ),
    }

    item_meta = frappe.get_meta("Item")

    for fieldname in (
        "brand",
        "country_of_origin",
        "customs_tariff_number",
    ):
        if (
            item_meta.has_field(fieldname)
            and base_item.get(fieldname)
        ):
            values[fieldname] = base_item.get(
                fieldname
            )

    related_item = frappe.get_doc(values)
    related_item.insert()

    return related_item


@frappe.whitelist(methods=["POST"])
def create_or_link_stock_items(
    saleable_item,
    standard_sack_weight_kg,
    damaged_item_code=None,
    fraction_item_code=None,
):
    require_nkt_authority(
        10,
        action=(
            "create or link damaged and fraction "
            "Item masters"
        ),
    )

    saleable_item = (saleable_item or "").strip()

    if not saleable_item:
        frappe.throw(
            _("Saleable Item is required.")
        )

    base_item = frappe.get_doc(
        "Item",
        saleable_item,
    )

    base_item.check_permission("write")

    if not base_item.is_stock_item:
        frappe.throw(
            _(
                "The selected saleable Item must be "
                "a stock item."
            )
        )

    if base_item.disabled:
        frappe.throw(
            _("The selected saleable Item is disabled.")
        )

    if base_item.nkt_stock_form in (
        "Damaged Sack",
        "Fraction Stock",
    ):
        frappe.throw(
            _(
                "A damaged or fraction Item cannot be "
                "used as the base saleable Item."
            )
        )

    if (
        getattr(base_item, "has_serial_no", 0)
        or getattr(base_item, "has_batch_no", 0)
    ):
        frappe.throw(
            _(
                "Automatic creation for serialized or "
                "batch-controlled Items is not yet enabled."
            )
        )

    standard_weight = flt(
        standard_sack_weight_kg
    )

    if standard_weight <= 0:
        frappe.throw(
            _(
                "Standard Sack Weight must be "
                "greater than zero."
            )
        )

    damaged_item_code = validate_item_code(
        damaged_item_code
        or base_item.nkt_damaged_item
        or f"[Damaged] {base_item.item_code}",
        _("Damaged Item Code"),
    )

    fraction_item_code = validate_item_code(
        fraction_item_code
        or base_item.nkt_fraction_item
        or get_default_fraction_code(
            base_item.item_code
        ),
        _("Fraction Item Code"),
    )

    if damaged_item_code == base_item.name:
        frappe.throw(
            _(
                "Damaged Item cannot be the same "
                "as the saleable Item."
            )
        )

    if fraction_item_code == base_item.name:
        frappe.throw(
            _(
                "Fraction Item cannot be the same "
                "as the saleable Item."
            )
        )

    if damaged_item_code == fraction_item_code:
        frappe.throw(
            _(
                "Damaged Item and Fraction Item "
                "must be different."
            )
        )

    if not frappe.db.exists("UOM", "Kg"):
        frappe.throw(
            _(
                "The Kg UOM does not exist. Create the "
                "Kg UOM before creating fraction Items."
            )
        )

    damaged_item = create_related_item(
        base_item=base_item,
        item_code=damaged_item_code,
        stock_form="Damaged Sack",
        stock_uom=base_item.stock_uom,
    )

    fraction_item = create_related_item(
        base_item=base_item,
        item_code=fraction_item_code,
        stock_form="Fraction Stock",
        stock_uom="Kg",
    )

    base_item.nkt_stock_form = "Saleable Sack"
    base_item.nkt_standard_sack_weight_kg = (
        standard_weight
    )
    base_item.nkt_damaged_item = damaged_item.name
    base_item.nkt_fraction_item = fraction_item.name

    base_item.save()

    frappe.db.commit()

    return {
        "saleable_item": base_item.name,
        "damaged_item": damaged_item.name,
        "damaged_stock_uom": damaged_item.stock_uom,
        "fraction_item": fraction_item.name,
        "fraction_stock_uom": fraction_item.stock_uom,
        "standard_sack_weight_kg": standard_weight,
    }
