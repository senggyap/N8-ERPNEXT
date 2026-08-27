frappe.ui.form.on("NKT Trucker Payment", {
    refresh(frm) {
        frm.set_intro(
            __("RESTRICTED FAST SCREEN — Trucker SOA payment control only. Draft/Prepared/Signed reservations do not reduce outstanding; only Released payment does. Outgoing check identity is company-wide across Supplier and Trucker payment registers."),
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

        if (
            is_management &&
            frm.doc.payment_method === "Management-Issued Check" &&
            frm.doc.check_replacement_for &&
            frm.doc.payment_status === "Draft"
        ) {
            call_action(
                "Link Replacement",
                "nkt_operations.nkt_store_operations.doctype.nkt_trucker_payment.nkt_trucker_payment.link_trucker_replacement_check"
            );
        }

        if (is_management && frm.doc.payment_status === "Draft") {
            call_action(
                frm.doc.payment_method === "Management-Issued Check"
                    ? "Approve Check"
                    : "Approve Payment",
                "nkt_operations.nkt_store_operations.doctype.nkt_trucker_payment.nkt_trucker_payment.approve_trucker_payment"
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
                "nkt_operations.nkt_store_operations.doctype.nkt_trucker_payment.nkt_trucker_payment.sign_trucker_check"
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
                "nkt_operations.nkt_store_operations.doctype.nkt_trucker_payment.nkt_trucker_payment.release_trucker_payment",
                "Release this trucker payment? Once Released, it reduces the operational Trucker SOA outstanding balance."
            );
        }

        if (
            frm.doc.payment_method === "Management-Issued Check" &&
            frm.doc.check_status === "Released"
        ) {
            call_action(
                "Mark Deposited",
                "nkt_operations.nkt_store_operations.doctype.nkt_trucker_payment.nkt_trucker_payment.mark_trucker_check_deposited"
            );
            call_action(
                "Mark Cleared",
                "nkt_operations.nkt_store_operations.doctype.nkt_trucker_payment.nkt_trucker_payment.mark_trucker_check_cleared"
            );
        }

        if (
            frm.doc.payment_method === "Management-Issued Check" &&
            frm.doc.check_status === "Deposited"
        ) {
            call_action(
                "Mark Cleared",
                "nkt_operations.nkt_store_operations.doctype.nkt_trucker_payment.nkt_trucker_payment.mark_trucker_check_cleared"
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
                "nkt_operations.nkt_store_operations.doctype.nkt_trucker_payment.nkt_trucker_payment.cancel_trucker_payment",
                "Cancel this trucker payment/check? Its active Trucker SOA reservation/payment effect will be removed according to the proven lifecycle rules."
            );
        }

        if (
            is_management &&
            frm.doc.payment_method === "Management-Issued Check" &&
            ["Prepared", "Signed", "Released", "Deposited"].includes(frm.doc.check_status)
        ) {
            call_action(
                "Mark Stale",
                "nkt_operations.nkt_store_operations.doctype.nkt_trucker_payment.nkt_trucker_payment.mark_trucker_check_stale"
            );
        }

        frm.add_custom_button(__("Carrier Balance"), () => {
            frappe.call({
                method: "nkt_operations.nkt_store_operations.doctype.nkt_trucker_payment.nkt_trucker_payment.get_trucker_payment_balance",
                args: {
                    company: frm.doc.company,
                    carrier_account: frm.doc.carrier_account
                },
                callback: (r) => {
                    const b = r.message || {};
                    frappe.msgprint({
                        title: __("Operational Carrier Balance"),
                        message: `
                            <b>Trucker SOA Net Payable:</b> ${format_currency(b.net_payable_total || 0)}<br>
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
        frm.toggle_display(
            "reference_section",
            is_bank || frm.doc.payment_method === "Other"
        );
    },

    carrier_account(frm) {
        if (!frm.doc.carrier_account) return;

        frm.set_query("trucker_soa", "allocations", () => ({
            filters: {
                carrier_account: frm.doc.carrier_account,
                company: frm.doc.company,
                status: "Finalized"
            }
        }));
    }
});

frappe.ui.form.on("NKT Trucker Payment Allocation", {
    trucker_soa(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (!row.trucker_soa) return;

        frappe.db.get_value(
            "NKT Trucker SOA",
            row.trucker_soa,
            ["statement_date", "net_payable"],
            (r) => {
                if (!r) return;
                frappe.model.set_value(
                    cdt, cdn, "soa_statement_date", r.statement_date || ""
                );
                frappe.model.set_value(
                    cdt, cdn, "soa_net_payable", r.net_payable || 0
                );
            }
        );
    }
});
