/* NKT CURRENT CLIENT SCRIPT — NKT Customer Receivable — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT Receivable Collection Shortcut V1.5 ===== */

frappe.ui.form.on("NKT Customer Receivable", {
    refresh(frm) {
        if (["Open", "Partially Paid"].includes(frm.doc.status) && flt(frm.doc.outstanding_amount) > 0) {
            frm.add_custom_button(__("New Account Payment Verification"), () => {
                frappe.new_doc("NKT Encoder Account Allocation", {
                    company: frm.doc.company,
                    customer: frm.doc.customer,
                    customer_name: frm.doc.customer_name,
                    referenced_customer_order: frm.doc.customer_order,
                    allocation_date: frappe.datetime.get_today()
                });
            }, __("Collection"));
        }
    }
});

/* ===== END SOURCE: NKT Receivable Collection Shortcut V1.5 ===== */

/* ===== SOURCE: NKT C5.5 Encoder Receivable Internal Visibility ===== */

frappe.ui.form.on("NKT Customer Receivable", {
    refresh(frm) {
        const roles = new Set(frappe.user_roles || []);

        const privileged =
            frappe.session.user === "Administrator" ||
            roles.has("System Manager") ||
            roles.has("NKT OWNER") ||
            roles.has("NKT ADMINISTRATOR") ||
            roles.has("NKT Credit Controller");

        const encoder = roles.has("NKT Encoder") && !privileged;

        if (!encoder) return;

        [
            "credit_control_section",
            "credit_control_status",
            "approved_by",
            "approved_on",
            "approval_reason",
            "approval_mode",
            "review_reason"
        ].forEach(fieldname => {
            if (frm.fields_dict[fieldname]) {
                frm.set_df_property(fieldname, "hidden", 1);
            }
        });
    }
});

/* ===== END SOURCE: NKT C5.5 Encoder Receivable Internal Visibility ===== */
