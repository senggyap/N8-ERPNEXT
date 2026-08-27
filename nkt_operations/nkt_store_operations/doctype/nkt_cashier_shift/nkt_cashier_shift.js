const NKT_SHIFT_METHOD =
    'nkt_operations.nkt_store_operations.doctype.'
    + 'nkt_cashier_shift.nkt_cashier_shift.';

const DENOMINATIONS = [
    {fieldname: 'bill_1000_qty', label: '₱1,000 Bills', value: 1000},
    {fieldname: 'bill_500_qty', label: '₱500 Bills', value: 500},
    {fieldname: 'bill_200_qty', label: '₱200 Bills', value: 200},
    {fieldname: 'bill_100_qty', label: '₱100 Bills', value: 100},
    {fieldname: 'bill_50_qty', label: '₱50 Bills', value: 50},
    {fieldname: 'bill_20_qty', label: '₱20 Bills', value: 20},
    {fieldname: 'coin_20_qty', label: '₱20 Coins', value: 20},
    {fieldname: 'coin_10_qty', label: '₱10 Coins', value: 10},
    {fieldname: 'coin_5_qty', label: '₱5 Coins', value: 5},
    {fieldname: 'coin_1_qty', label: '₱1 Coins', value: 1},
    {fieldname: 'coin_025_qty', label: '₱0.25 Coins', value: 0.25}
];

const DENOMINATION_FIELDS = DENOMINATIONS.map((row) => row.fieldname);

function money(value) {
    return format_currency(flt(value || 0), 'PHP');
}

function set_shift_field_access(frm) {
    const opened = frm.doc.status !== 'Not Opened';
    [
        'company',
        'settlement_location',
        'opening_cash'
    ].forEach((fieldname) => {
        frm.set_df_property(fieldname, 'read_only', opened ? 1 : 0);
    });

    const show_system_totals = opened || frm.doc.docstatus === 1;
    [
        'movement_totals_section',
        'total_cash_in',
        'total_cash_out',
        'total_non_cash_in',
        'total_non_cash_out',
        'expected_cash'
    ].forEach((fieldname) => {
        frm.toggle_display(fieldname, show_system_totals);
    });

    const counted =
        frm.doc.status === 'Turned Over - Awaiting Review'
        || frm.doc.docstatus === 1;

    [
        'count_section',
        'blind_count_confirmed',
        'actual_cash_count',
        'over_short',
        'count_notes',
        'count_locked_by',
        'count_locked_on',
        'turnover_section',
        'turnover_status',
        'turnover_amount',
        'turnover_confirmed_by',
        'turnover_confirmed_on',
        'denomination_section',
        ...DENOMINATION_FIELDS,
        'approval_section',
        'approval_reason',
        'approved_by',
        'approved_on',
        'closed_by',
        'closed_on'
    ].forEach((fieldname) => {
        frm.toggle_display(fieldname, counted);
    });
}

function print_shift_report(frm, trigger_print = true) {
    const params = new URLSearchParams({
        doctype: 'NKT Cashier Shift',
        name: frm.doc.name,
        format: 'NKT Cashier Shift Report',
        no_letterhead: '0',
        _lang: frappe.boot.lang || 'en'
    });
    if (trigger_print) {
        params.set('trigger_print', '1');
    }
    window.open(`/printview?${params.toString()}`, '_blank');
}

function denomination_dialog(frm) {
    const expected_cash = flt(frm.doc.expected_cash || 0);
    let dialog;

    const denomination_fields = [];
    DENOMINATIONS.forEach((row, index) => {
        if (index === 0 || index === 3 || index === 6 || index === 9) {
            denomination_fields.push({
                fieldtype: 'Section Break',
                label: index < 6 ? __('Bills') : __('Coins')
            });
        } else {
            denomination_fields.push({fieldtype: 'Column Break'});
        }
        denomination_fields.push({
            fieldtype: 'Int',
            fieldname: row.fieldname,
            label: __(row.label),
            default: cint(frm.doc[row.fieldname] || 0),
            non_negative: 1
        });
    });

    dialog = new frappe.ui.Dialog({
        title: __('Cash Count by Denomination'),
        fields: [
            {
                fieldtype: 'HTML',
                fieldname: 'summary'
            },
            ...denomination_fields,
            {
                fieldtype: 'Section Break',
                label: __('Difference Explanation')
            },
            {
                fieldtype: 'Small Text',
                fieldname: 'count_notes',
                label: __('Cash Difference Explanation / Closing Notes'),
                default: frm.doc.count_notes || '',
                description: __('Required whenever the denomination count does not equal Expected Cash.')
            }
        ],
        primary_action_label: __('Record Turnover and Print'),
        primary_action(values) {
            const denominations = {};
            let actual_cash = 0;
            DENOMINATIONS.forEach((row) => {
                const quantity = cint(values[row.fieldname] || 0);
                denominations[row.fieldname] = quantity;
                actual_cash += quantity * row.value;
            });
            const difference = actual_cash - expected_cash;
            const notes = (values.count_notes || '').trim();

            if (Math.abs(difference) > 0.004 && !notes) {
                frappe.msgprint({
                    title: __('Explanation Required'),
                    indicator: 'orange',
                    message: __('Enter an explanation for the cash overage or shortage before recording the count.')
                });
                return;
            }

            frappe.confirm(
                __('Record this count?<br><br>Expected Cash: {0}<br>Actual Count: {1}<br>Over / (Short): {2}', [
                    money(expected_cash),
                    money(actual_cash),
                    money(difference)
                ]),
                () => {
                    dialog.disable_primary_action();
                    frappe.call({
                        method: NKT_SHIFT_METHOD + 'record_cash_count',
                        args: {
                            cashier_shift: frm.doc.name,
                            denominations: JSON.stringify(denominations),
                            count_notes: notes
                        },
                        freeze: true,
                        freeze_message: __('Recording cash count...'),
                        callback(r) {
                            if (!r.exc) {
                                dialog.hide();
                                frappe.show_alert({
                                    message: __('Shift closed. Opening the final Shift Report for printing.'),
                                    indicator: 'green'
                                });
                                print_shift_report(frm, true);
                                frm.reload_doc();
                            } else {
                                dialog.enable_primary_action();
                            }
                        }
                    });
                }
            );
        }
    });

    const update_summary = () => {
        let actual_cash = 0;
        DENOMINATIONS.forEach((row) => {
            actual_cash += cint(dialog.get_value(row.fieldname) || 0) * row.value;
        });
        const difference = actual_cash - expected_cash;
        const indicator = Math.abs(difference) < 0.005
            ? 'green'
            : (difference < 0 ? 'red' : 'orange');
        const difference_label = difference < 0 ? __('Short') : __('Over');
        dialog.fields_dict.summary.$wrapper.html(`
            <div class="alert alert-${indicator === 'green' ? 'success' : (indicator === 'red' ? 'danger' : 'warning')}">
                <div style="display:flex; gap:24px; flex-wrap:wrap; font-size:15px;">
                    <div><strong>${__('System Expected Cash')}</strong><br>${money(expected_cash)}</div>
                    <div><strong>${__('Denomination Count')}</strong><br>${money(actual_cash)}</div>
                    <div><strong>${difference_label}</strong><br>${money(Math.abs(difference))}</div>
                </div>
            </div>
        `);
    };

    dialog.show();
    DENOMINATION_FIELDS.forEach((fieldname) => {
        const field = dialog.fields_dict[fieldname];
        if (field && field.$input) {
            field.$input.on('input change', update_summary);
        }
    });
    update_summary();
}

function review_dialog(frm) {
    const dialog = new frappe.ui.Dialog({
        title: __('Administrator Shift Review'),
        fields: [
            {
                fieldtype: 'Check',
                fieldname: 'reviewed_ok',
                label: __('Reviewed and OK'),
                default: 1
            },
            {
                fieldtype: 'Small Text',
                fieldname: 'review_note',
                label: __('Review Note'),
                description: __('Optional when everything is correct. Use this for observations or follow-up items.')
            }
        ],
        primary_action_label: __('Mark Reviewed / OK'),
        primary_action(values) {
            if (!cint(values.reviewed_ok)) {
                frappe.msgprint(__('Tick Reviewed and OK before confirming.'));
                return;
            }
            dialog.disable_primary_action();
            frappe.call({
                method: NKT_SHIFT_METHOD + 'mark_shift_reviewed',
                args: {
                    cashier_shift: frm.doc.name,
                    reviewed_ok: values.reviewed_ok,
                    review_note: values.review_note || ''
                },
                freeze: true,
                freeze_message: __('Recording administrator review...'),
                callback(result) {
                    if (!result.exc) {
                        dialog.hide();
                        frappe.show_alert({
                            message: __('Shift marked Reviewed / OK.'),
                            indicator: 'green'
                        });
                        frm.reload_doc();
                    } else {
                        dialog.enable_primary_action();
                    }
                }
            });
        }
    });
    dialog.show();
}


frappe.ui.form.on('NKT Cashier Shift', {
    setup(frm) {
        frm.set_query('settlement_location', () => {
            const filters = {is_group: 0, disabled: 0};
            if (frm.doc.company) {
                filters.company = frm.doc.company;
            }
            return {filters};
        });
    },

    onload(frm) {
        if (frm.is_new() && !frm.doc.cashier) {
            frm.set_value('cashier', frappe.session.user);
        }
    },

    refresh(frm) {
        set_shift_field_access(frm);

        if (frm.doc.docstatus === 0 && frm.doc.status === 'Not Opened') {
            frm.set_intro(
                __('Enter the fresh opening cash for this operator, then click Open Shift.'),
                'blue'
            );
            frm.add_custom_button(__('Open Shift'), async () => {
                if (frm.is_new() || frm.is_dirty()) {
                    await frm.save();
                }
                frappe.call({
                    method: NKT_SHIFT_METHOD + 'open_shift',
                    args: {cashier_shift: frm.doc.name},
                    freeze: true,
                    freeze_message: __('Opening shift...'),
                    callback(r) {
                        if (!r.exc) {
                            frm.reload_doc();
                        }
                    }
                });
            }).addClass('btn-primary');
        }

        if (
            frm.doc.docstatus === 0
            && frm.doc.status === 'Open'
            && frm.doc.cashier === frappe.session.user
        ) {
            frm.page.clear_primary_action();
            frm.set_intro(
                __('Expected Cash is visible. Count the money by denomination, explain any difference, turn it over, and print the Shift Report.'),
                'green'
            );
            frm.add_custom_button(
                __('Close Shift & Print Final Report'),
                async () => {
                    await frappe.call({
                        method: NKT_SHIFT_METHOD + 'refresh_shift_totals',
                        args: {cashier_shift: frm.doc.name}
                    });
                    await frm.reload_doc();
                    denomination_dialog(frm);
                }
            ).addClass('btn-primary');
        }

        if (
            frm.doc.docstatus === 0
            && frm.doc.status === 'Turned Over - Awaiting Review'
        ) {
            frm.page.clear_primary_action();
            frm.set_intro(
                __('Cash was counted, turned over, and this shift is operationally closed. A new shift may now be opened. Administrator review may be completed later.'),
                'orange'
            );

            frm.add_custom_button(
                __('Print Shift Report'),
                () => print_shift_report(frm, true)
            ).addClass('btn-primary');

            frappe.call({
                method: NKT_SHIFT_METHOD + 'get_shift_review_mode',
                callback(r) {
                    if (r.message && r.message.can_review) {
                        frm.add_custom_button(
                            __('Mark Reviewed / OK'),
                            () => review_dialog(frm),
                            __('Actions')
                        );
                    }
                }
            });
        }

        if (
            frm.doc.docstatus === 1
            || frm.doc.status === 'Reviewed / Closed'
            || frm.doc.status === 'Closed'
        ) {
            frm.set_intro(
                __('Shift is reviewed, closed, and retained for audit.'),
                'green'
            );
            frm.add_custom_button(
                __('Print Shift Report'),
                () => print_shift_report(frm, true)
            ).addClass('btn-primary');
        }

        if (!frm.is_new() && frm.doc.status === 'Open') {
            frm.add_custom_button(
                __('Refresh Movement Totals'),
                () => {
                    frappe.call({
                        method: NKT_SHIFT_METHOD + 'refresh_shift_totals',
                        args: {cashier_shift: frm.doc.name},
                        callback() {
                            frm.reload_doc();
                        }
                    });
                },
                __('Actions')
            );
        }
    },

    company(frm) {
        frm.set_value('settlement_location', '');
    }
});
