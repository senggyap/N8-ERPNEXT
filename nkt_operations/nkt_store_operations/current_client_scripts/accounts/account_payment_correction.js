/* NKT CURRENT CLIENT SCRIPT — NKT Account Payment Correction — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT Payment Correction V1.8 ===== */

frappe.ui.form.on('NKT Account Payment Correction', {
    setup(frm) {
        frm.set_query('payment_verification', () => ({filters: {status: 'Matched', allocations_posted: 1}}));
        frm.set_query('specific_customer_order', () => ({filters: {customer: frm.doc.customer, company: frm.doc.company}}));
    },
    refresh(frm) {
        if (frm.is_new()) return;
        if (frm.doc.status !== 'Applied' && frm.doc.payment_verification) {
            frm.add_custom_button(__('Load Payment Source'), () => frappe.call({
                method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.load_payment_correction_source',
                args: {correction_name: frm.doc.name, payment_verification: frm.doc.payment_verification},
                freeze: true,
                callback: () => frm.reload_doc()
            }));
            frm.add_custom_button(__('Preview Correction'), () => frappe.call({
                method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.preview_payment_correction',
                args: {correction_name: frm.doc.name},
                freeze: true,
                callback: () => frm.reload_doc()
            }));
        }
        if (frm.doc.status === 'Previewed') {
            frm.add_custom_button(__('Apply Correction'), () => {
                frappe.confirm(__('This changes only the receivable application. The existing cashier receipt and cashier movement remain unchanged. Continue?'),
                    () => frappe.call({
                        method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.apply_payment_correction',
                        args: {correction_name: frm.doc.name},
                        freeze: true,
                        callback: () => frm.reload_doc()
                    }));
            }).addClass('btn-primary');
        }
    }
});

/* ===== END SOURCE: NKT Payment Correction V1.8 ===== */
