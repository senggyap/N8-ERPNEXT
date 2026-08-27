function nkt_fraction_item_default(item_code) {
    const clean_name = (item_code || "")
        .replace(
            /\s*[\[(]\s*\d+(?:\.\d+)?\s*kg\s*[\])]\s*$/i,
            ""
        )
        .trim();

    return `[Fraction] ${clean_name || item_code}`;
}


function nkt_create_stock_structure(frm) {
    if (frm.is_new()) {
        frappe.msgprint(
            __("Save the saleable Item first.")
        );
        return;
    }

    const dialog = new frappe.ui.Dialog({
        title: __("Create or Link NKT Stock Items"),

        fields: [
            {
                fieldname: "saleable_item",
                fieldtype: "Data",
                label: __("Saleable Item"),
                default: frm.doc.name,
                read_only: 1
            },
            {
                fieldname: "standard_sack_weight_kg",
                fieldtype: "Float",
                label: __("Standard Sack Weight (kg)"),
                default:
                    frm.doc.nkt_standard_sack_weight_kg
                    || 25,
                reqd: 1
            },
            {
                fieldname: "damaged_item_code",
                fieldtype: "Data",
                label: __("Damaged Item Code"),
                default:
                    frm.doc.nkt_damaged_item
                    || `[Damaged] ${frm.doc.item_code}`,
                reqd: 1
            },
            {
                fieldname: "fraction_item_code",
                fieldtype: "Data",
                label: __("Fraction Item Code"),
                default:
                    frm.doc.nkt_fraction_item
                    || nkt_fraction_item_default(
                        frm.doc.item_code
                    ),
                reqd: 1
            }
        ],

        primary_action_label: __("Create and Link"),

        primary_action(values) {
            dialog.disable_primary_action();

            frappe.call({
                method:
                    "nkt_operations.nkt_store_operations."
                    + "nkt_item_stock_mapping."
                    + "create_or_link_stock_items",

                type: "POST",

                args: values,

                freeze: true,

                freeze_message: __(
                    "Creating and linking Item masters..."
                ),

                callback(r) {
                    if (!r.message) {
                        dialog.enable_primary_action();
                        return;
                    }

                    dialog.hide();

                    frappe.msgprint({
                        title: __("NKT Stock Items Linked"),
                        indicator: "green",
                        message:
                            `<b>${__(
                                "Damaged Item"
                            )}:</b> `
                            + `${frappe.utils.escape_html(
                                r.message.damaged_item
                            )}<br>`
                            + `<b>${__(
                                "Fraction Item"
                            )}:</b> `
                            + `${frappe.utils.escape_html(
                                r.message.fraction_item
                            )}<br>`
                            + `<b>${__(
                                "Standard Weight"
                            )}:</b> `
                            + `${r.message
                                .standard_sack_weight_kg} kg`
                    });

                    frm.reload_doc();
                },

                error() {
                    dialog.enable_primary_action();
                }
            });
        }
    });

    dialog.show();
}


frappe.ui.form.on("Item", {
    refresh(frm) {
        if (
            frm.is_new()
            || !frm.doc.is_stock_item
            || ["Damaged Sack", "Fraction Stock"]
                .includes(frm.doc.nkt_stock_form)
        ) {
            return;
        }

        const button_label =
            frm.doc.nkt_damaged_item
            && frm.doc.nkt_fraction_item
                ? __("Verify NKT Stock Links")
                : __(
                    "Create/Link Damaged & Fraction Items"
                );

        frm.add_custom_button(
            button_label,
            () => nkt_create_stock_structure(frm),
            __("NKT")
        );

        if (frm.doc.nkt_damaged_item) {
            frm.add_custom_button(
                __("Open Damaged Item"),
                () => frappe.set_route(
                    "Form",
                    "Item",
                    frm.doc.nkt_damaged_item
                ),
                __("NKT")
            );
        }

        if (frm.doc.nkt_fraction_item) {
            frm.add_custom_button(
                __("Open Fraction Item"),
                () => frappe.set_route(
                    "Form",
                    "Item",
                    frm.doc.nkt_fraction_item
                ),
                __("NKT")
            );
        }
    }
});
