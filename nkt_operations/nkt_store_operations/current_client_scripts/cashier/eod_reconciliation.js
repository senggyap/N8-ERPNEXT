/* NKT CURRENT CLIENT SCRIPT — NKT EOD Reconciliation — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT EOD Reconciliation Controls V2.0C.6.3 ===== */

frappe.ui.form.on("NKT EOD Reconciliation", {
    async onload(frm) {
        if (frm.is_new()) {
            const r = await frappe.call({
                method: "nkt_operations.nkt_store_operations.features.cashier.reconciliation.get_defaults"
            });
            const d = r.message || {};
            if (!frm.doc.company && d.company) await frm.set_value("company", d.company);
            if (!frm.doc.business_date && d.business_date) await frm.set_value("business_date", d.business_date);
        }
    },

    refresh(frm) {
        if (frm.doc.docstatus === 0) {
            frm.set_intro(
                __("Compare Cashier close(s) and finalized Z-Out(s). Document variances here; do not change either source to force a match."),
                "blue"
            );
            if (!frm.is_new()) {
                frm.add_custom_button(__("Refresh Reconciliation"), async () => {
                    const r = await frappe.call({
                        method: "nkt_operations.nkt_store_operations.features.cashier.reconciliation.refresh_preview",
                        type: "POST",
                        args: {name: frm.doc.name},
                        freeze: true,
                        freeze_message: __("Refreshing EOD reconciliation...")
                    });
                    await frm.reload_doc();
                    const ready = ((r.message || {}).readiness || {}).ready_to_finalize;
                    frappe.show_alert({
                        message: ready ? __("Close data is ready for review.") : __("Close is incomplete. Review open shifts / missing Z-Outs."),
                        indicator: ready ? "green" : "orange"
                    });
                }, __("EOD"));
            }
        } else if (frm.doc.docstatus === 1) {
            frm.set_intro(__("Management reconciliation finalized."), "green");
            frm.add_custom_button(__("Print Reconciliation"), () => {
                const url = `/printview?doctype=${encodeURIComponent(frm.doctype)}&name=${encodeURIComponent(frm.doc.name)}&format=${encodeURIComponent("NKT EOD Reconciliation")}&no_letterhead=1&_lang=en`;
                window.open(url, "_blank");
            }, __("EOD"));
        }
    }
});

/* ===== END SOURCE: NKT EOD Reconciliation Controls V2.0C.6.3 ===== */
