frappe.ui.form.on("NKT BPI Sample Release", {
    refresh(frm) {
        frm.set_intro(
            __("FAST SCREEN: record the physical BPI/sample release first. The full physical quantity leaves stock; supplier-charge quantity is a separate management decision."),
            "blue"
        );

        // Keep the normal operational screen compact.
        const can_manage_charge = frappe.user_roles.some(
            r => ["NKT Purchasing", "NKT ADMINISTRATOR", "NKT OWNER", "System Manager"].includes(r)
        );
        frm.toggle_display("management_section", can_manage_charge);
        frm.toggle_display("control_section", !frm.is_new());

        if (frm.doc.docstatus === 1 && frm.doc.stock_posting_status === "Posted") {
            frm.set_intro(
                __("PHYSICAL SAMPLE POSTED. Stock used the full Physical Sample Qty. Purchasing/Management may set the supplier-chargeable quantity separately."),
                "green"
            );
        }
    },

    source_supplier_receiving(frm) {
        if (!frm.doc.source_supplier_receiving) return;
        frappe.db.get_value(
            "NKT Supplier Receiving",
            frm.doc.source_supplier_receiving,
            ["company", "supplier", "purchase_order"],
            (r) => {
                if (!r) return;
                frm.set_value("company", r.company || "");
                frm.set_value("supplier", r.supplier || "");
                frm.set_value("purchase_order", r.purchase_order || "");
            }
        );
    }
});
