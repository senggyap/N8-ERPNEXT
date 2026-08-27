frappe.ui.form.on("NKT Trucker Adjustment", {
    refresh(frm) {
        frm.set_intro(
            __("RESTRICTED FAST SCREEN — physical issues come from the linked Supplier Arrival/Exception. Load them once, then management decides trucking responsibility and any deduction. Supplier and trucker money remain separate."),
            "blue"
        );
        frm.toggle_display("notes_section", !frm.is_new());

        if (frm.doc.docstatus === 0 && frm.doc.trucking_job && !(frm.doc.items || []).length) {
            frm.add_custom_button(__("Load Physical Issues"), () => {
                frappe.call({
                    method: "nkt_operations.nkt_store_operations.doctype.nkt_trucker_adjustment.nkt_trucker_adjustment.get_trucker_adjustment_source_payload",
                    args: { trucking_job: frm.doc.trucking_job },
                    freeze: true,
                    freeze_message: __("Loading physical delivery issues..."),
                    callback: (r) => {
                        const data = r.message;
                        if (!data) return;

                        [
                            "company", "carrier_account", "source_supplier_receiving",
                            "source_supplier_exception", "job_date", "dr_no",
                            "plate_number", "internal_vehicle_no", "vehicle_operator"
                        ].forEach(fieldname => {
                            if (data[fieldname] !== undefined) {
                                frm.set_value(fieldname, data[fieldname]);
                            }
                        });

                        frm.clear_table("items");
                        (data.items || []).forEach(values => {
                            const row = frm.add_child("items");
                            Object.assign(row, values);
                        });
                        frm.refresh_field("items");

                        frappe.show_alert({
                            message: __("Physical issues loaded. Responsibility and deductions remain unset."),
                            indicator: "green"
                        });
                    }
                });
            });
        }
    }
});
