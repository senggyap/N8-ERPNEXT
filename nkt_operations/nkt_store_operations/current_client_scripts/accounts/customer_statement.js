/* NKT CURRENT CLIENT SCRIPT — NKT Customer Statement — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT Customer Statement V1.7 ===== */

frappe.ui.form.on('NKT Customer Statement', {
    setup(frm) {
        frm.set_query('customer', () => ({ filters: { disabled: 0 } }));
    },

    onload(frm) {
        if (frm.is_new()) {
            if (!frm.doc.to_date) {
                frm.set_value('to_date', frappe.datetime.get_today());
            }
            if (!frm.doc.from_date) {
                frm.set_value('from_date', frappe.datetime.month_start());
            }
            if (!frm.doc.company) {
                frm.set_value('company', frappe.defaults.get_user_default('Company'));
            }
        }
    },

    customer(frm) {
        if (!frm.doc.customer || frm.doc.company) return;
        frappe.call({
            method: 'nkt_operations.nkt_store_operations.features.payments_accounts.statement.get_customer_default_company',
            args: { customer: frm.doc.customer },
            callback(r) {
                if (r.message) frm.set_value('company', r.message);
            }
        });
    },

    refresh(frm) {
        frm.add_custom_button(__('Generate / Refresh Statement'), () => {
            if (!frm.doc.company || !frm.doc.customer || !frm.doc.from_date || !frm.doc.to_date) {
                frappe.msgprint(__('Company, Customer, From Date, and To Date are required.'));
                return;
            }
            frappe.call({
                method: 'nkt_operations.nkt_store_operations.features.payments_accounts.statement.get_statement_data',
                args: {
                    company: frm.doc.company,
                    customer: frm.doc.customer,
                    from_date: frm.doc.from_date,
                    to_date: frm.doc.to_date
                },
                freeze: true,
                freeze_message: __('Generating statement...'),
                callback(r) {
                    const data = r.message || {};
                    const fields = [
                        'customer_name', 'currency', 'opening_balance', 'period_charges',
                        'period_payments', 'closing_balance', 'aging_current', 'aging_1_30',
                        'aging_31_60', 'aging_61_90', 'aging_over_90', 'generated_by',
                        'generated_on', 'status'
                    ];
                    fields.forEach((fieldname) => frm.set_value(fieldname, data[fieldname]));
                    frm.clear_table('lines');
                    (data.lines || []).forEach((row) => {
                        const child = frm.add_child('lines');
                        Object.keys(row).forEach((key) => child[key] = row[key]);
                    });
                    frm.refresh_field('lines');
                    frm.save().then(() => {
                        frappe.show_alert({ message: __('Statement generated.'), indicator: 'green' });
                    });
                }
            });
        }).addClass('btn-primary');

        if (!frm.is_new() && frm.doc.status === 'Generated') {
            frm.add_custom_button(__('Print Statement'), () => {
                const url = frappe.urllib.get_full_url(
                    '/printview?doctype=NKT%20Customer%20Statement&name=' +
                    encodeURIComponent(frm.doc.name) +
                    '&format=NKT%20Customer%20Statement%20of%20Account&no_letterhead=0'
                );
                window.open(url, '_blank');
            });
        }
    }
});

/* ===== END SOURCE: NKT Customer Statement V1.7 ===== */

/* ===== SOURCE: NKT Statement Delivery Audit V1.8 ===== */

frappe.ui.form.on('NKT Customer Statement', {
    refresh(frm) {
        if (frm.is_new() || frm.doc.status !== 'Generated') return;
        frm.add_custom_button(__('Record Printed Copy'), () => {
            frappe.call({
                method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.record_statement_delivery',
                args: {statement: frm.doc.name, delivery_method: 'Printed'},
                freeze: true,
                callback: () => frm.reload_doc()
            });
        }, __('Delivery Audit'));
        frm.add_custom_button(__('Record Email Sent'), () => {
            frappe.prompt([
                {fieldname:'recipient', label:__('Recipient Email'), fieldtype:'Data', reqd:1},
                {fieldname:'notes', label:__('Notes'), fieldtype:'Small Text'}
            ], values => frappe.call({
                method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.record_statement_delivery',
                args: {statement: frm.doc.name, delivery_method: 'Emailed', recipient: values.recipient, notes: values.notes},
                freeze: true,
                callback: () => frm.reload_doc()
            }), __('Record Email Sent'));
        }, __('Delivery Audit'));
        frm.add_custom_button(__('Record Hand Delivery'), () => {
            frappe.prompt([
                {fieldname:'recipient', label:__('Received By'), fieldtype:'Data', reqd:1},
                {fieldname:'notes', label:__('Notes'), fieldtype:'Small Text'}
            ], values => frappe.call({
                method: 'nkt_operations.nkt_store_operations.features.payments_accounts.receivables.record_statement_delivery',
                args: {statement: frm.doc.name, delivery_method: 'Hand Delivered', recipient: values.recipient, notes: values.notes},
                freeze: true,
                callback: () => frm.reload_doc()
            }), __('Record Hand Delivery'));
        }, __('Delivery Audit'));
    }
});

/* ===== END SOURCE: NKT Statement Delivery Audit V1.8 ===== */
