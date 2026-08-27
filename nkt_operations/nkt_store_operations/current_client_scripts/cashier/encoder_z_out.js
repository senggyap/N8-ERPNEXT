/* NKT CURRENT CLIENT SCRIPT — NKT Encoder Z-Out — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT Encoder Z-Out Controls V2.0C.6.2 ===== */

frappe.ui.form.on("NKT Encoder Z-Out", {
    async onload(frm) {
        if (frm.is_new()) {
            const r = await frappe.call({
                method: "nkt_operations.nkt_store_operations.features.cashier.encoder_zout.get_defaults"
            });
            const d = r.message || {};
            if (!frm.doc.encoder && d.encoder) await frm.set_value("encoder", d.encoder);
            if (!frm.doc.company && d.company) await frm.set_value("company", d.company);
            if (!frm.doc.business_date && d.business_date) await frm.set_value("business_date", d.business_date);
            await frm.set_value("include_inventory_appendix", 0);
        }
    },

    refresh(frm) {
        frm.set_df_property("include_inventory_appendix", "hidden", 1);
        frm.set_df_property("inventory_item_count", "hidden", 1);

        if (frm.doc.docstatus === 0) {
            frm.set_intro(__("Review totals, then Submit to finalize the Z-Out."), "blue");
            if (!frm.is_new()) {
                frm.add_custom_button(__("Refresh Totals"), async () => {
                    const r = await frappe.call({
                        method: "nkt_operations.nkt_store_operations.features.cashier.encoder_zout.refresh_preview",
                        type: "POST",
                        args: {name: frm.doc.name},
                        freeze: true,
                        freeze_message: __("Refreshing Z-Out totals...")
                    });
                    await frm.reload_doc();
                    const c = (r.message || {}).counts || {};
                    frappe.show_alert({
                        message: __("Updated: {0} sales, {1} exceptions.", [c.orders || 0, c.exceptions || 0]),
                        indicator: "green"
                    });
                }, __("Z-Out"));
            }
        }

        if (frm.doc.docstatus === 1) {
            frm.set_intro(__("Z-Out finalized."), "green");
            frm.add_custom_button(__("Print Z-Out"), () => {
                const url = `/printview?doctype=${encodeURIComponent(frm.doctype)}&name=${encodeURIComponent(frm.doc.name)}&format=${encodeURIComponent("NKT Encoder Z-Out")}&no_letterhead=1&_lang=en`;
                window.open(url, "_blank");
            }, __("Z-Out"));
        }
    }
});

/* ===== END SOURCE: NKT Encoder Z-Out Controls V2.0C.6.2 ===== */
