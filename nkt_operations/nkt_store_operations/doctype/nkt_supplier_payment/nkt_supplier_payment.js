frappe.ui.form.on("NKT Supplier Payment", {
    refresh(frm) {
        frm.set_intro(
            __("RESTRICTED FAST SCREEN — supplier payment/check control only. Prepared/Signed reserves the SOA; only Released payment reduces operational outstanding. No Cashier movement or Payment Entry is created."),
            "blue"
        );

        frm.trigger("payment_method");
        frm.toggle_display("audit_section", !frm.is_new());

        if (frm.is_new()) return;

        const roles = frappe.user_roles || [];
        const is_management = roles.some(r =>
            ["NKT ADMINISTRATOR", "NKT OWNER", "Administrator"].includes(r)
        );
        const is_operator = is_management || roles.includes("NKT Purchasing");
        if (!is_operator) return;

        const call_action = (label, method, confirm_text=null) => {
            const run = () => frappe.call({
                method,
                args: { name: frm.doc.name },
                freeze: true,
                callback: () => frm.reload_doc()
            });

            frm.add_custom_button(__(label), () => {
                if (confirm_text) {
                    frappe.confirm(__(confirm_text), run);
                } else {
                    run();
                }
            }, __("Payment Actions"));
        };

        if (is_management && frm.doc.payment_status === "Draft") {
            call_action(
                frm.doc.payment_method === "Management-Issued Check" ? "Approve Check" : "Approve Payment",
                "nkt_operations.nkt_store_operations.doctype.nkt_supplier_payment.nkt_supplier_payment.approve_supplier_payment"
            );
        }

        if (
            is_management &&
            frm.doc.payment_method === "Management-Issued Check" &&
            frm.doc.payment_status === "Prepared" &&
            frm.doc.check_status === "Prepared"
        ) {
            call_action(
                "Sign Check",
                "nkt_operations.nkt_store_operations.doctype.nkt_supplier_payment.nkt_supplier_payment.sign_supplier_check"
            );
        }

        if (
            (
                frm.doc.payment_method === "Management-Issued Check" &&
                frm.doc.check_status === "Signed"
            ) ||
            (
                frm.doc.payment_method !== "Management-Issued Check" &&
                frm.doc.payment_status === "Prepared"
            )
        ) {
            call_action(
                "Release Payment",
                "nkt_operations.nkt_store_operations.doctype.nkt_supplier_payment.nkt_supplier_payment.release_supplier_payment",
                "Release this supplier payment? Once Released, it reduces the operational Supplier SOA outstanding balance."
            );
        }

        if (
            frm.doc.payment_method === "Management-Issued Check" &&
            frm.doc.check_status === "Released"
        ) {
            call_action(
                "Mark Deposited",
                "nkt_operations.nkt_store_operations.doctype.nkt_supplier_payment.nkt_supplier_payment.mark_supplier_check_deposited"
            );
            call_action(
                "Mark Cleared",
                "nkt_operations.nkt_store_operations.doctype.nkt_supplier_payment.nkt_supplier_payment.mark_supplier_check_cleared"
            );
        }

        if (
            frm.doc.payment_method === "Management-Issued Check" &&
            frm.doc.check_status === "Deposited"
        ) {
            call_action(
                "Mark Cleared",
                "nkt_operations.nkt_store_operations.doctype.nkt_supplier_payment.nkt_supplier_payment.mark_supplier_check_cleared"
            );
        }

        if (
            is_management &&
            (
                (
                    frm.doc.payment_method === "Management-Issued Check" &&
                    ["Prepared", "Signed", "Released", "Deposited"].includes(frm.doc.check_status)
                ) ||
                (
                    frm.doc.payment_method !== "Management-Issued Check" &&
                    ["Draft", "Prepared"].includes(frm.doc.payment_status)
                )
            )
        ) {
            call_action(
                "Cancel Payment",
                "nkt_operations.nkt_store_operations.doctype.nkt_supplier_payment.nkt_supplier_payment.cancel_supplier_payment",
                "Cancel this supplier payment/check? Its active SOA reservation/payment effect will be removed according to the proven lifecycle rules."
            );
        }

        if (
            is_management &&
            frm.doc.payment_method === "Management-Issued Check" &&
            ["Prepared", "Signed", "Released", "Deposited"].includes(frm.doc.check_status)
        ) {
            call_action(
                "Mark Stale",
                "nkt_operations.nkt_store_operations.doctype.nkt_supplier_payment.nkt_supplier_payment.mark_supplier_check_stale"
            );
        }

        if (
            is_management &&
            frm.doc.payment_method === "Management-Issued Check" &&
            frm.doc.check_replacement_for &&
            frm.doc.payment_status === "Draft"
        ) {
            call_action(
                "Link Replacement",
                "nkt_operations.nkt_store_operations.doctype.nkt_supplier_payment.nkt_supplier_payment.link_supplier_replacement_check"
            );
        }

        if (is_management && frm.doc.payment_status === "Released") {
            frappe.call({
                method: "nkt_operations.nkt_store_operations.doctype.nkt_supplier_payment.nkt_supplier_payment.get_supplier_advance_balance",
                args: { name: frm.doc.name },
                callback: (r) => {
                    const b = r.message || {};
                    if (flt(b.available_advance_balance || 0) <= 0.005) return;
                    frm.add_custom_button(__("Apply Remaining Advance"), () => {
                        frappe.new_doc("NKT Supplier Advance Application", {
                            supplier_payment: frm.doc.name,
                            company: frm.doc.company,
                            supplier: frm.doc.supplier,
                            original_payment_amount: frm.doc.payment_amount,
                            original_unallocated_advance: b.original_unallocated_advance || 0,
                            previously_applied_advance: b.applied_later_amount || 0,
                            available_advance_before: b.available_advance_balance || 0
                        });
                    }, __("Payment Actions"));
                }
            });
        }

        frm.add_custom_button(__("Supplier Balance"), () => {
            frappe.call({
                method: "nkt_operations.nkt_store_operations.doctype.nkt_supplier_payment.nkt_supplier_payment.get_supplier_payment_balance",
                args: {
                    company: frm.doc.company,
                    supplier: frm.doc.supplier
                },
                callback: (r) => {
                    const b = r.message || {};
                    frappe.msgprint({
                        title: __("Operational Supplier Balance"),
                        message: `
                            <b>SOA Net Payable:</b> ${format_currency(b.net_payable_total || 0)}<br>
                            <b>Released Paid:</b> ${format_currency(b.released_paid_total || 0)}<br>
                            <b>Outstanding:</b> ${format_currency(b.outstanding_total || 0)}<br>
                            <b>Available to Allocate:</b> ${format_currency(b.available_to_allocate_total || 0)}
                        `
                    });
                }
            });
        }, __("View"));
    },

    payment_method(frm) {
        const is_check = frm.doc.payment_method === "Management-Issued Check";
        const is_bank = frm.doc.payment_method === "Bank Transfer";

        frm.toggle_display("check_section", is_check);
        frm.toggle_display("reference_section", is_bank || frm.doc.payment_method === "Other");
    },

    supplier(frm) {
        if (!frm.doc.supplier) return;
        frm.set_query("supplier_soa", "allocations", () => ({
            filters: {
                supplier: frm.doc.supplier,
                company: frm.doc.company,
                status: "Finalized"
            }
        }));
    }
});

frappe.ui.form.on("NKT Supplier Payment Allocation", {
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
