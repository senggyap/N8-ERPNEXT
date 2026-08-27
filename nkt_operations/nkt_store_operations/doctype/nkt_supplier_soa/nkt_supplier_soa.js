frappe.ui.form.on("NKT Supplier SOA", {
    refresh(frm) {
        frm.set_intro(
            __("RESTRICTED FAST SCREEN — Build NKT's supplier payable statement from delivery, damage/shortage and BPI sample lines. This is not a Purchase Invoice or Payment Entry."),
            "blue"
        );

        // Keep source lineage and notes secondary. Statement lines and totals stay central.
        frm.toggle_display("notes_section", !frm.is_new());

        if (!frm.is_new()) {
            const roles = frappe.user_roles || [];
            const is_management = roles.some(r =>
                ["NKT ADMINISTRATOR", "NKT OWNER", "Administrator"].includes(r)
            );
            const is_operator = is_management || roles.includes("NKT Purchasing");

            const run_action = (label, method, confirm_text=null) => {
                const execute = () => frappe.call({
                    method,
                    args: { name: frm.doc.name },
                    freeze: true,
                    callback: () => frm.reload_doc()
                });

                frm.add_custom_button(__(label), () => {
                    if (confirm_text) {
                        frappe.confirm(__(confirm_text), execute);
                    } else {
                        execute();
                    }
                }, __("Statement Actions"));
            };

            if (is_operator && frm.doc.status === "Draft" && (frm.doc.lines || []).length) {
                run_action(
                    "Prepare Statement",
                    "nkt_operations.nkt_store_operations.doctype.nkt_supplier_soa.nkt_supplier_soa.prepare_supplier_soa",
                    "Prepare this Supplier SOA? It will freeze for management review and will still NOT be payable until Finalized."
                );
            }

            if (is_operator && frm.doc.status === "Prepared") {
                run_action(
                    "Return to Draft",
                    "nkt_operations.nkt_store_operations.doctype.nkt_supplier_soa.nkt_supplier_soa.return_supplier_soa_to_draft",
                    "Return this Prepared Supplier SOA to Draft for correction?"
                );
            }

            if (is_management && frm.doc.status === "Prepared") {
                run_action(
                    "Finalize Statement",
                    "nkt_operations.nkt_store_operations.doctype.nkt_supplier_soa.nkt_supplier_soa.finalize_supplier_soa",
                    "Finalize this Supplier SOA? Finalized means frozen payable truth and makes it eligible for payment allocation."
                );
            }

            if (is_management && frm.doc.status === "Finalized") {
                frm.add_custom_button(__("Supersede Statement"), () => {
                    frappe.prompt(
                        [{
                            fieldname: "reason",
                            fieldtype: "Small Text",
                            label: __("Supersede Reason"),
                            reqd: 1
                        }],
                        values => {
                            frappe.call({
                                method: "nkt_operations.nkt_store_operations.doctype.nkt_supplier_soa.nkt_supplier_soa.supersede_supplier_soa",
                                args: {
                                    name: frm.doc.name,
                                    reason: values.reason
                                },
                                freeze: true,
                                callback: () => frm.reload_doc()
                            });
                        },
                        __("Supersede Supplier SOA"),
                        __("Supersede")
                    );
                }, __("Statement Actions"));
            }
        }

        frm.set_df_property("soa_format", "read_only", 1);
        frm.set_df_property("bl_no", "read_only", 1);
        frm.toggle_display("bl_no", frm.doc.soa_format === "Rice - Date + BL / Van");

        if (frm.doc.docstatus === 0 && !frm.doc.lines?.length) {
            frm.add_custom_button(__("Auto Build Statement(s)"), async () => {
                const required = ["company", "supplier", "period_start", "period_end"];
                const missing = required.filter(f => !frm.doc[f]);
                if (missing.length) {
                    frappe.msgprint(__("Set Company, Supplier, Period Start and Period End first."));
                    return;
                }

                if (frm.is_new()) {
                    await frm.save();
                }

                const r = await frappe.call({
                    method: "nkt_operations.nkt_store_operations.doctype.nkt_supplier_soa.nkt_supplier_soa.auto_build_supplier_soas",
                    args: { name: frm.doc.name },
                    freeze: true,
                    freeze_message: __("Automatically separating Supplier SOA sources...")
                });

                await frm.reload_doc();

                const data = r.message || {};
                const companions = data.companion_soas || [];
                let msg = __("Supplier SOA format resolved automatically to {0}.", [frm.doc.soa_format || ""]);
                if (companions.length) {
                    msg += "<br><b>" + __("Separate companion SOA(s) created:") + "</b> " + companions.join(", ");
                }
                frappe.msgprint(msg);
            });
        }
    }
});

frappe.ui.form.on("NKT Supplier SOA Line", {
    item_code(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (!row.item_code || !frm.doc.supplier) return;

        frappe.db.get_value(
            "NKT Supplier Commercial Item",
            { supplier: frm.doc.supplier, item_code: row.item_code, status: "Active" },
            "commercial_description",
            (r) => {
                if (r && r.commercial_description) {
                    frappe.model.set_value(cdt, cdn, "commercial_item_description", r.commercial_description);
                }
            }
        );
    }
});
