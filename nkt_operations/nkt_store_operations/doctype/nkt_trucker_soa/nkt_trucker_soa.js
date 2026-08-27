frappe.ui.form.on("NKT Trucker SOA", {
    refresh(frm) {
        frm.set_intro(
            __("RESTRICTED FAST SCREEN — choose the carrier/period, then Populate Sources. Eligible DRs are discovered automatically; management enters the hauling Rate per selected load. Rates never appear on Encoder/Warehouse trucking screens."),
            "blue"
        );
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
                    "nkt_operations.nkt_store_operations.doctype.nkt_trucker_soa.nkt_trucker_soa.prepare_trucker_soa",
                    "Prepare this Trucker SOA? It will freeze for management review and will still NOT be payable until Finalized."
                );
            }

            if (is_operator && frm.doc.status === "Prepared") {
                run_action(
                    "Return to Draft",
                    "nkt_operations.nkt_store_operations.doctype.nkt_trucker_soa.nkt_trucker_soa.return_trucker_soa_to_draft",
                    "Return this Prepared Trucker SOA to Draft for correction?"
                );
            }

            if (is_management && frm.doc.status === "Prepared") {
                run_action(
                    "Finalize Statement",
                    "nkt_operations.nkt_store_operations.doctype.nkt_trucker_soa.nkt_trucker_soa.finalize_trucker_soa",
                    "Finalize this Trucker SOA? Finalized means frozen payable truth and makes it eligible for payment allocation."
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
                                method: "nkt_operations.nkt_store_operations.doctype.nkt_trucker_soa.nkt_trucker_soa.supersede_trucker_soa",
                                args: {
                                    name: frm.doc.name,
                                    reason: values.reason
                                },
                                freeze: true,
                                callback: () => frm.reload_doc()
                            });
                        },
                        __("Supersede Trucker SOA"),
                        __("Supersede")
                    );
                }, __("Statement Actions"));
            }
        }

        if (frm.doc.docstatus !== 0 || (frm.doc.lines || []).length) return;

        frm.add_custom_button(__("Populate Sources"), () => {
            const required = ["company", "carrier_account", "period_start", "period_end"];
            const missing = required.filter(fieldname => !frm.doc[fieldname]);
            if (missing.length) {
                frappe.msgprint(
                    __("Set Company, Carrier / Trucker, Period Start and Period End first.")
                );
                return;
            }

            frappe.call({
                method: "nkt_operations.nkt_store_operations.doctype.nkt_trucker_soa.nkt_trucker_soa.get_trucker_soa_population_candidates",
                args: {
                    company: frm.doc.company,
                    carrier_account: frm.doc.carrier_account,
                    period_start: frm.doc.period_start,
                    period_end: frm.doc.period_end,
                    current_soa: frm.is_new() ? null : frm.doc.name
                },
                freeze: true,
                freeze_message: __("Finding eligible trucking jobs..."),
                callback: (r) => {
                    const data = r.message || {};
                    const eligible = data.eligible || [];

                    if (!eligible.length) {
                        let detail = __("No unused, financially-ready Trucking Job was found.");
                        if ((data.blocked || []).length) {
                            detail += "<br><br><b>" + __("Blocked:") + "</b><br>" +
                                data.blocked.map(x =>
                                    `${frappe.utils.escape_html(x.dr_no || x.trucking_job)} — ${frappe.utils.escape_html(x.reason || "")}`
                                ).join("<br>");
                        }
                        if ((data.already_used || []).length) {
                            detail += "<br><br><b>" + __("Already used:") + "</b><br>" +
                                data.already_used.map(x =>
                                    `${frappe.utils.escape_html(x.dr_no || x.trucking_job)} — ${frappe.utils.escape_html(x.existing_trucker_soa || "")}`
                                ).join("<br>");
                        }
                        frappe.msgprint(detail);
                        return;
                    }

                    const dialog = new frappe.ui.Dialog({
                        title: __("Select Hauls and Enter Restricted Rates"),
                        size: "extra-large",
                        fields: [
                            {
                                fieldname: "rate_rows",
                                fieldtype: "Table",
                                label: __("Eligible Trucking Jobs"),
                                cannot_add_rows: true,
                                in_place_edit: true,
                                data: eligible,
                                fields: [
                                    {
                                        fieldname: "include",
                                        fieldtype: "Check",
                                        label: __("Use"),
                                        in_list_view: 1,
                                        default: 1
                                    },
                                    {
                                        fieldname: "trucking_job",
                                        fieldtype: "Link",
                                        options: "NKT Trucking Job",
                                        label: __("Trucking Job"),
                                        in_list_view: 1,
                                        read_only: 1
                                    },
                                    {
                                        fieldname: "dr_no",
                                        fieldtype: "Data",
                                        label: __("DR No."),
                                        in_list_view: 1,
                                        read_only: 1
                                    },
                                    {
                                        fieldname: "plate_number",
                                        fieldtype: "Data",
                                        label: __("Plate No."),
                                        in_list_view: 1,
                                        read_only: 1
                                    },
                                    {
                                        fieldname: "hauled_qty",
                                        fieldtype: "Float",
                                        label: __("Bags"),
                                        in_list_view: 1,
                                        read_only: 1
                                    },
                                    {
                                        fieldname: "rate",
                                        fieldtype: "Currency",
                                        label: __("Rate"),
                                        in_list_view: 1,
                                        reqd: 0
                                    }
                                ]
                            },
                            {
                                fieldname: "rate_note",
                                fieldtype: "HTML",
                                options: __(
                                    "<small>Rate is a restricted statement-preparation input for each selected haul. C9H.2 does not create a universal trucking-rate master.</small>"
                                )
                            }
                        ],
                        primary_action_label: __("Build SOA Lines"),
                        primary_action(values) {
                            const selected = (values.rate_rows || []).filter(x => cint(x.include));
                            if (!selected.length) {
                                frappe.msgprint(__("Select at least one haul."));
                                return;
                            }

                            const bad = selected.filter(x => flt(x.rate) <= 0);
                            if (bad.length) {
                                frappe.msgprint(__("Enter a Rate greater than zero for every selected haul."));
                                return;
                            }

                            dialog.hide();

                            frappe.call({
                                method: "nkt_operations.nkt_store_operations.doctype.nkt_trucker_soa.nkt_trucker_soa.get_trucker_soa_population_payload",
                                args: {
                                    company: frm.doc.company,
                                    carrier_account: frm.doc.carrier_account,
                                    period_start: frm.doc.period_start,
                                    period_end: frm.doc.period_end,
                                    rate_rows: JSON.stringify(
                                        selected.map(x => ({
                                            trucking_job: x.trucking_job,
                                            rate: flt(x.rate)
                                        }))
                                    ),
                                    current_soa: frm.is_new() ? null : frm.doc.name
                                },
                                freeze: true,
                                freeze_message: __("Building Trucker SOA lines..."),
                                callback: (r2) => {
                                    const result = r2.message;
                                    if (!result) return;

                                    frm.clear_table("lines");
                                    (result.lines || []).forEach(values => {
                                        const row = frm.add_child("lines");
                                        Object.assign(row, values);
                                    });
                                    frm.refresh_field("lines");

                                    frm.set_value(
                                        "gross_haul_amount",
                                        result.totals.gross_haul_amount || 0
                                    );
                                    frm.set_value(
                                        "total_additions",
                                        result.totals.total_additions || 0
                                    );
                                    frm.set_value(
                                        "total_deductions",
                                        result.totals.total_deductions || 0
                                    );
                                    frm.set_value(
                                        "net_payable",
                                        result.totals.net_payable || 0
                                    );

                                    frappe.show_alert({
                                        message: __(
                                            "Loaded {0} trucking job(s).",
                                            [result.trucking_jobs.length]
                                        ),
                                        indicator: "green"
                                    });
                                }
                            });
                        }
                    });

                    dialog.show();

                    if ((data.blocked || []).length || (data.already_used || []).length) {
                        frappe.show_alert({
                            message: __(
                                "{0} haul(s) were excluded because they are unresolved or already used.",
                                [(data.blocked || []).length + (data.already_used || []).length]
                            ),
                            indicator: "orange"
                        });
                    }
                }
            });
        });
    }
});
