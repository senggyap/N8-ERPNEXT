/* NKT CURRENT CLIENT SCRIPT — NKT Customer Order Item — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT Saleable Item Warning V1.8 - NKT Customer Order Item ===== */

frappe.ui.form.on("NKT Customer Order Item", {
    item(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (!row.item) return;
        frappe.call({
            method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.get_item_saleability',
            args: {item: row.item},
            callback: r => {
                if (r.message && !r.message.allowed) {
                    frappe.msgprint({
                        title: __('Item Not Allowed for Ordinary Sale'),
                        indicator: 'red',
                        message: r.message.message
                    });
                    frappe.model.set_value(cdt, cdn, 'item', null);
                }
            }
        });
    }
});

/* ===== END SOURCE: NKT Saleable Item Warning V1.8 - NKT Customer Order Item ===== */
