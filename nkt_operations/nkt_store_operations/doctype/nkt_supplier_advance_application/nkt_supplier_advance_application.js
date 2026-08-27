frappe.ui.form.on("NKT Supplier Advance Application", {
    refresh(frm) {
        frm.set_intro(
            __("OWNER/ADMIN — apply an already Released supplier payment's remaining unapplied amount to the specific finalized Supplier SOA(s) you choose. The original payment remains locked history."),
            "blue"
        );
        frm.trigger("supplier_payment");
    },

    supplier_payment(frm) {
        if (!frm.doc.supplier_payment) return;
        frappe.db.get_value(
            "NKT Supplier Payment",
            frm.doc.supplier_payment,
            ["company", "supplier", "payment_amount", "payment_status"],
            (r) => {
                if (!r) return;
                frm.set_value("company", r.company || "");
                frm.set_value("supplier", r.supplier || "");
                frm.set_value("original_payment_amount", r.payment_amount || 0);
                frm.set_query("supplier_soa", "allocations", () => ({
                    filters: {
                        company: r.company,
                        supplier: r.supplier,
                        status: "Finalized"
                    }
                }));
            }
        );
    }
});

frappe.ui.form.on("NKT Supplier Advance Application Allocation", {
    supplier_soa(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (!row.supplier_soa) return;
        frappe.db.get_value(
            "NKT Supplier SOA",
            row.supplier_soa,
            ["statement_date", "net_payable"],
            (r) => {
                if (!r) return;
                frappe.model.set_value(cdt, cdn, "soa_statement_date", r.statement_date || "");
                frappe.model.set_value(cdt, cdn, "soa_net_payable", r.net_payable || 0);
            }
        );
    }
});
