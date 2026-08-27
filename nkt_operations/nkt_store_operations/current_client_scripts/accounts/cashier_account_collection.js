/* NKT CURRENT CLIENT SCRIPT — NKT Cashier Account Collection — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT Cashier Account Collection V1.5 ===== */

(function () {
    const ROOT = "nkt_operations.nkt_store_operations.features.payments_accounts.collection";

    function calculate(frm) {
        let total = 0;
        (frm.doc.payments || []).forEach(row => {
            const amount = flt(row.amount);
            total += amount;
            row.change_amount = row.payment_method === "Cash"
                ? Math.max(flt(row.cash_tendered) - amount, 0)
                : 0;
        });
        frm.set_value("total_payment", total);
        frm.set_value("balance_after_collection", Math.max(flt(frm.doc.previous_balance) - total, 0));
        frm.refresh_field("payments");
    }

    async function load_context(frm) {
        if (!frm.doc.company) return;
        const r = await frappe.call({method: `${ROOT}.get_cashier_context`, args: {company: frm.doc.company}});
        if (!r.message) return;
        await frm.set_value("cashier", r.message.cashier || frappe.session.user);
        await frm.set_value("cashier_shift", r.message.cashier_shift);
        await frm.set_value("business_date", r.message.business_date);
        await frm.set_value("collection_datetime", r.message.collection_datetime);
        if (r.message.settlement_location) {
            await frm.set_value("settlement_location", r.message.settlement_location);
        }
    }

    async function load_balance(frm) {
        if (!frm.doc.customer) return;
        const r = await frappe.call({method: `${ROOT}.get_customer_collection_snapshot`, args: {customer: frm.doc.customer}});
        if (!r.message) return;
        await frm.set_value("customer_name", r.message.customer_name);
        await frm.set_value("previous_balance", r.message.current_balance);
        calculate(frm);
    }

    frappe.ui.form.on("NKT Cashier Account Collection", {
        setup(frm) {
            frm.set_query("customer", () => ({
                filters: {
                    custom_nkt_allow_account_sales: 1
                }
            }));
            frm.set_query("referenced_customer_order", () => ({
                filters: {
                    customer: frm.doc.customer,
                    payment_status: ["in", ["Charged to Account", "Partially Paid"]],
                    amount_due: [">", 0]
                }
            }));
        },
        async refresh(frm) {
            if (frm.is_new()) await load_context(frm);
            calculate(frm);
            if (frm.doc.status === "Draft" && !frm.is_new()) {
                frm.add_custom_button(__("Submit Collection"), async () => {
                    await frappe.call({
                        method: `${ROOT}.submit_cashier_collection`,
                        type: "POST",
                        args: {collection: frm.doc.name},
                        freeze: true,
                        freeze_message: __("Recording cashier collection...")
                    });
                    frm.reload_doc();
                });
            }
            if (["Submitted - Unmatched", "Ambiguous", "Matched"].includes(frm.doc.status)) {
                frm.add_custom_button(__("Print Collection Receipt"), () => frm.print_doc(), __("Print"));
            }
            if (["Submitted - Unmatched", "Ambiguous"].includes(frm.doc.status)) {
                frm.add_custom_button(__("Retry Reconciliation"), async () => {
                    await frappe.call({method: `${ROOT}.retry_collection_match`, type: "POST", args: {cashier_collection: frm.doc.name}, freeze: true});
                    frm.reload_doc();
                }, __("Reconciliation"));
            }
        },
        company(frm) { if (frm.is_new()) load_context(frm); },
        customer(frm) { load_balance(frm); },
        validate(frm) { calculate(frm); },
        payments_remove(frm) { calculate(frm); }
    });

    frappe.ui.form.on("NKT Account Collection Payment", {
        payment_method(frm) { calculate(frm); },
        amount(frm) { calculate(frm); },
        cash_tendered(frm) { calculate(frm); }
    });
})();

/* ===== END SOURCE: NKT Cashier Account Collection V1.5 ===== */

/* ===== SOURCE: NKT Account Payment Receipt Button V1.7 ===== */

frappe.ui.form.on('NKT Cashier Account Collection', {
    refresh(frm) {
        if (frm.is_new() || frm.doc.status === 'Draft') return;
        frm.add_custom_button(__('Print Account Payment Receipt'), () => {
            const url = frappe.urllib.get_full_url(
                '/printview?doctype=NKT%20Cashier%20Account%20Collection&name=' +
                encodeURIComponent(frm.doc.name) +
                '&format=NKT%20Account%20Payment%20Receipt&no_letterhead=0'
            );
            window.open(url, '_blank');
        }, __('Print'));
    }
});

/* ===== END SOURCE: NKT Account Payment Receipt Button V1.7 ===== */
