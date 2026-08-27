frappe.ui.form.on("NKT Warehouse Transfer Discrepancy", {
    refresh(frm) {
        const roles = frappe.user_roles || [];
        const is_reviewer =
            frappe.session.user === "Administrator" ||
            roles.includes("NKT ADMINISTRATOR") ||
            roles.includes("NKT OWNER");

        const is_open = frm.doc.status === "Open";
        const is_under_review = frm.doc.status === "Under Review";
        const is_resolved = frm.doc.status === "Resolved";

        if (is_reviewer && !frm.is_new()) {
            const factual_fields = [
                "warehouse_transfer",
                "discrepancy_date",
                "evidence",
                "notes",
                "items",
            ];
            factual_fields.forEach((fieldname) => {
                frm.set_df_property(fieldname, "read_only", 1);
            });

            frm.set_df_property("resolution_notes", "read_only", !is_under_review);

            if (frm.fields_dict.items && frm.fields_dict.items.grid) {
                frm.fields_dict.items.grid.update_docfield_property(
                    "responsibility",
                    "read_only",
                    !is_under_review
                );
            }

            if (is_open) {
                frm.add_custom_button(__("Start Admin Review"), () => {
                    frappe.call({
                        method:
                            "nkt_operations.nkt_store_operations.doctype.nkt_warehouse_transfer_discrepancy.nkt_warehouse_transfer_discrepancy.start_admin_review",
                        args: { name: frm.doc.name },
                        freeze: true,
                        freeze_message: __("Starting Admin review..."),
                        callback(r) {
                            if (!r.exc) {
                                frm.reload_doc();
                            }
                        },
                    });
                }, __("Admin Review"));
            }

            if (is_under_review) {
                frm.add_custom_button(__("Resolve"), async () => {
                    if (frm.is_dirty()) {
                        await frm.save();
                    }
                    frappe.call({
                        method:
                            "nkt_operations.nkt_store_operations.doctype.nkt_warehouse_transfer_discrepancy.nkt_warehouse_transfer_discrepancy.resolve_admin_review",
                        args: { name: frm.doc.name },
                        freeze: true,
                        freeze_message: __("Resolving discrepancy review..."),
                        callback(r) {
                            if (!r.exc) {
                                frm.reload_doc();
                            }
                        },
                    });
                }, __("Admin Review"));
            }
        }

        if (is_resolved) {
            frm.disable_save();
        }
    },
});
