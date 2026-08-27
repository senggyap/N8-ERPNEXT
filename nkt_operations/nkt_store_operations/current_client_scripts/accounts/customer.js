/* NKT CURRENT CLIENT SCRIPT — Customer — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT Customer Account History V1.7 ===== */

frappe.ui.form.on('Customer', {
    refresh(frm) {
        if (frm.is_new()) return;
        frm.add_custom_button(__('Create Statement of Account'), () => {
            frappe.route_options = { customer: frm.doc.name };
            frappe.new_doc('NKT Customer Statement');
        }, __('NKT Account'));

        frm.add_custom_button(__('View Receivables'), () => {
            frappe.set_route('List', 'NKT Customer Receivable', { customer: frm.doc.name });
        }, __('NKT Account'));

        frm.add_custom_button(__('Statement History'), () => {
            frappe.set_route('List', 'NKT Customer Statement', { customer: frm.doc.name });
        }, __('NKT Account'));
    }
});

/* ===== END SOURCE: NKT Customer Account History V1.7 ===== */

/* ===== SOURCE: NKT Customer Account Controls V1.8 ===== */

frappe.ui.form.on('Customer', {
    refresh(frm) {
        if (frm.is_new()) return;
        const allowed = frappe.user.has_role('System Manager') ||
            frappe.user.has_role('NKT OWNER') ||
            frappe.user.has_role('NKT ADMINISTRATOR') ||
            frappe.user.has_role('NKT Credit Controller');
        if (!allowed) return;

        frm.add_custom_button(__('Refresh Aging Control'), () => {
            frappe.call({
                method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.refresh_customer_account_control',
                args: {customer: frm.doc.name},
                freeze: true,
                callback: () => frm.reload_doc()
            });
        }, __('NKT Account'));

        frm.add_custom_button(__('Set Overdue Hold Threshold'), () => {
            frappe.prompt([{fieldname:'days', label:__('Days Overdue (0 disables)'), fieldtype:'Int', reqd:1, default: frm.doc.custom_nkt_max_overdue_days || 0}],
                values => frappe.call({
                    method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.set_overdue_hold_threshold',
                    args: {customer: frm.doc.name, days: values.days},
                    freeze: true,
                    callback: () => frm.reload_doc()
                }), __('Overdue Hold Threshold'));
        }, __('NKT Account'));

        if (frm.doc.custom_nkt_manual_account_hold) {
            frm.add_custom_button(__('Clear Manual Hold'), () => {
                frappe.prompt([{fieldname:'reason', label:__('Reason'), fieldtype:'Small Text', reqd:1}],
                    values => frappe.call({
                        method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.clear_customer_account_hold',
                        args: {customer: frm.doc.name, reason: values.reason},
                        freeze: true,
                        callback: () => frm.reload_doc()
                    }), __('Clear Account Hold'));
            }, __('NKT Account'));
        } else {
            frm.add_custom_button(__('Place Manual Hold'), () => {
                frappe.prompt([{fieldname:'reason', label:__('Reason'), fieldtype:'Small Text', reqd:1}],
                    values => frappe.call({
                        method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.set_customer_account_hold',
                        args: {customer: frm.doc.name, reason: values.reason},
                        freeze: true,
                        callback: () => frm.reload_doc()
                    }), __('Place Account Hold'));
            }, __('NKT Account'));
        }
    }
});

/* ===== END SOURCE: NKT Customer Account Controls V1.8 ===== */

/* ===== SOURCE: NKT C5.5 Customer Exposure Visibility ===== */

frappe.ui.form.on("Customer", {
    refresh(frm) {
        const roles = new Set(frappe.user_roles || []);

        const privileged =
            frappe.session.user === "Administrator" ||
            roles.has("System Manager") ||
            roles.has("NKT OWNER") ||
            roles.has("NKT ADMINISTRATOR") ||
            roles.has("NKT Credit Controller");

        const encoder = roles.has("NKT Encoder") && !privileged;

        const hideIfPresent = (fieldname, hidden) => {
            if (frm.fields_dict[fieldname]) {
                frm.set_df_property(fieldname, "hidden", hidden ? 1 : 0);
            }
        };

        if (encoder) {
            if (frm.fields_dict.custom_nkt_current_account_balance) {
                frm.set_df_property(
                    "custom_nkt_current_account_balance",
                    "label",
                    __("Customer Receivable")
                );
            }

            [
                "custom_nkt_require_manual_account_approval",
                "custom_nkt_auto_approval_limit"
            ].forEach(f => hideIfPresent(f, true));

            return;
        }

        if (!privileged || frm.is_new()) return;

        if (frm.fields_dict.custom_nkt_current_account_balance) {
            frm.set_df_property(
                "custom_nkt_current_account_balance",
                "label",
                __("Operational Exposure")
            );
        }

        frappe.call({
            method:
                "nkt_operations.nkt_store_operations." +
                "nkt_c5_5_role_safe_receivable." +
                "get_customer_receivable_visibility",
            args: { customer: frm.doc.name },
            freeze: false
        }).then(r => {
            const x = r.message || {};
            if (x.visibility !== "owner_control") return;

            const money = value =>
                format_currency(
                    Number(value || 0),
                    frappe.defaults.get_default("currency") || "PHP"
                );

            frm.dashboard.add_indicator(
                __("Total Exposure: {0}", [
                    money(x.total_operational_exposure)
                ]),
                "blue"
            );

            frm.dashboard.add_indicator(
                __("Official Receivable: {0}", [
                    money(x.official_receivable)
                ]),
                "green"
            );

            frm.dashboard.add_indicator(
                __("Pending Internal: {0}", [
                    money(x.pending_internal)
                ]),
                Number(x.pending_internal || 0) > 0
                    ? "orange"
                    : "green"
            );

            frm.dashboard.add_indicator(
                __("Available Advance: {0}", [
                    money(x.available_customer_advance)
                ]),
                Number(x.available_customer_advance || 0) > 0
                    ? "blue"
                    : "grey"
            );
        });
    }
});

/* ===== END SOURCE: NKT C5.5 Customer Exposure Visibility ===== */

/* ===== SOURCE: NKT Customer 360 Navigation C12D ===== */
frappe.ui.form.on("Customer", {
  refresh(frm) {
    if (frm.is_new()) return;

    const roles = new Set(frappe.user_roles || []);
    const allowed =
      frappe.session.user === "Administrator" ||
      roles.has("NKT OWNER") ||
      roles.has("NKT ADMINISTRATOR") ||
      roles.has("NKT Credit Controller");

    if (!allowed) return;

    frm.add_custom_button(__("Customer 360"), () => {
      frappe.route_options = { customer: frm.doc.name };
      frappe.set_route("query-report", "NKT Customer 360");
    }, __("NKT Account"));
  }
});

/* ===== END SOURCE: NKT Customer 360 Navigation C12D ===== */
