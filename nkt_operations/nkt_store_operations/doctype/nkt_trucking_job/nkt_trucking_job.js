frappe.ui.form.on("NKT Trucking Job", {
    refresh(frm) {
        frm.set_intro(
            __("FAST SCREEN — link a posted Supplier Arrival once; BL/DR/vehicle/plate/driver/item/quantity facts are copied automatically. No trucking rate or payable amount exists on this operational screen."),
            "blue"
        );

        frm.toggle_display("control_section", !frm.is_new());
    },

    source_supplier_receiving(frm) {
        if (!frm.doc.source_supplier_receiving) return;
        // Server validation is the source of truth. Save refreshes the immutable
        // physical snapshot from the posted Supplier Arrival.
        frappe.show_alert({
            message: __("Save to copy the posted Supplier Arrival physical details."),
            indicator: "blue"
        });
    }
});
