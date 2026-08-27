const NKT_EXCHANGE_TYPES = [
    'Same-Item Exchange',
    'Different-Item Exchange'
];


function is_exchange(frm) {
    return NKT_EXCHANGE_TYPES.includes(
        frm.doc.settlement_type
    );
}


function calculate_return_summary(frm) {
    let physical = 0;
    let accepted = 0;
    let saleable = 0;
    let repairable = 0;
    let opened = 0;
    let rejected = 0;
    let fractionKg = 0;
    let grossValue = 0;
    let replacementValue = 0;

    (frm.doc.items || []).forEach((row) => {
        const rowSaleable = Math.max(
            flt(row.saleable_sacks), 0
        );
        const rowRepairable = Math.max(
            flt(row.repairable_sacks), 0
        );
        const rowOpened = Math.max(
            flt(row.opened_sacks), 0
        );
        const rowRejected = Math.max(
            flt(row.rejected_sacks), 0
        );
        const rowFraction = Math.max(
            flt(row.accepted_fraction_kg), 0
        );
        const whole = rowSaleable + rowRepairable;
        const rowAccepted = whole + rowOpened;
        const rate = Math.max(
            flt(row.original_rate), 0
        );
        const standardWeight = flt(
            row.standard_sack_weight_kg
        );
        const fullValue = whole * rate;
        const fractionValue =
            rowFraction > 0 && standardWeight > 0
                ? rowFraction / standardWeight * rate
                : 0;
        const rowGross = fullValue + fractionValue;

        row.accepted_sacks = rowAccepted;
        row.whole_sacks_accepted = whole;
        row.full_sack_return_value = fullValue;
        row.fraction_return_value = fractionValue;
        row.gross_return_value = rowGross;

        physical += Math.max(
            flt(row.return_quantity), 0
        );
        accepted += rowAccepted;
        saleable += rowSaleable;
        repairable += rowRepairable;
        opened += rowOpened;
        rejected += rowRejected;
        fractionKg += rowFraction;
        grossValue += rowGross;
    });

    (frm.doc.replacement_items || []).forEach(
        (row) => {
            row.amount = Math.max(
                flt(row.quantity), 0
            ) * Math.max(flt(row.rate), 0);
            replacementValue += row.amount;
        }
    );

    const totalDeductions =
        Math.max(flt(frm.doc.labor_charge), 0)
        + Math.max(
            flt(frm.doc.packaging_deduction), 0
        )
        + Math.max(
            flt(frm.doc.handling_deduction), 0
        )
        + Math.max(
            flt(frm.doc.other_deduction), 0
        );

    const calculatedCredit = Math.max(
        grossValue - totalDeductions,
        0
    );

    const approvedCredit =
        frm.doc.settlement_type === 'No Credit'
            ? 0
            : calculatedCredit;

    let customerPays = 0;
    let refundDue = 0;
    let creditDue = 0;

    if (frm.doc.settlement_type === 'Cash Refund') {
        refundDue = approvedCredit;
    } else if (
        [
            'Customer Credit',
            'Account Adjustment'
        ].includes(frm.doc.settlement_type)
    ) {
        creditDue = approvedCredit;
    } else if (is_exchange(frm)) {
        customerPays = Math.max(
            replacementValue - approvedCredit,
            0
        );
        refundDue = Math.max(
            approvedCredit - replacementValue,
            0
        );

        if ([
            'Customer Credit',
            'Account Adjustment'
        ].includes(frm.doc.difference_payment_method)) {
            creditDue = refundDue;
            refundDue = 0;
        }
    }

    const values = {
        total_return_quantity: physical,
        total_accepted_sacks: accepted,
        total_saleable_sacks: saleable,
        total_repairable_sacks: repairable,
        total_opened_sacks: opened,
        total_rejected_sacks: rejected,
        total_fraction_kg: fractionKg,
        gross_return_value: grossValue,
        total_deductions: totalDeductions,
        calculated_return_credit: calculatedCredit,
        settlement_amount: approvedCredit,
        replacement_value: replacementValue,
        customer_pays: customerPays,
        refund_due: refundDue,
        customer_credit_due: creditDue
    };

    Object.entries(values).forEach(
        ([fieldname, value]) => {
            frm.set_value(fieldname, value);
        }
    );

    frm.refresh_field('items');
    frm.refresh_field('replacement_items');
}


function clear_return_approval(frm) {
    if (frm.doc.approval_status !== 'Approved') {
        return;
    }

    frm.set_value(
        'approval_status',
        'Pending Approval'
    );
    frm.set_value('approved_by', '');
    frm.set_value('approved_on', '');
    frm.set_value('approval_reason', '');
    frm.set_value('approval_signature', '');

    frappe.show_alert({
        message: __(
            'Return details changed. Approval is required again.'
        ),
        indicator: 'orange'
    });
}


function clear_receipt_confirmation(frm) {
    if (![
        'Received',
        'Overridden'
    ].includes(frm.doc.warehouse_receipt_status)) {
        return;
    }

    frm.set_value(
        'warehouse_receipt_status',
        'Pending Receipt'
    );
    frm.set_value(
        'warehouse_receipt_confirmed_by',
        ''
    );
    frm.set_value(
        'warehouse_receipt_confirmed_on',
        ''
    );
    frm.set_value(
        'warehouse_receipt_remarks',
        ''
    );
    frm.set_value(
        'warehouse_receipt_signature',
        ''
    );
    clear_return_approval(frm);
}


function clear_return_source(frm) {
    frm.set_value('warehouse_release', '');
    frm.set_value('customer_order', '');
    frm.set_value('original_delivery_note', '');
    frm.set_value('customer_name', '');
    frm.clear_table('items');
    clear_receipt_confirmation(frm);
    clear_return_approval(frm);
    calculate_return_summary(frm);
}


function load_warehouse_release(frm) {
    if (!frm.doc.warehouse_release) {
        frm.clear_table('items');
        calculate_return_summary(frm);
        return;
    }

    frappe.call({
        method:
            'nkt_operations.nkt_store_operations.'
            + 'doctype.nkt_customer_return.'
            + 'nkt_customer_return.get_return_data',
        args: {
            warehouse_release:
                frm.doc.warehouse_release,
            customer_return: frm.doc.name
        },
        freeze: true,
        freeze_message: __('Loading original sale items...'),
        callback(r) {
            if (!r.message) {
                return;
            }

            const data = r.message;

            if (
                frm.doc.customer
                && frm.doc.customer !== data.customer
            ) {
                frappe.msgprint(
                    __('The release belongs to a different customer.')
                );
                clear_return_source(frm);
                return;
            }

            frm.set_value('company', data.company);
            frm.set_value('customer', data.customer);
            frm.set_value(
                'customer_name',
                data.customer_name
            );
            frm.set_value(
                'customer_order',
                data.customer_order
            );
            frm.set_value(
                'original_delivery_note',
                data.original_delivery_note
            );
            frm.set_value(
                'return_warehouse',
                data.default_return_warehouse || ''
            );

            frm.clear_table('items');

            (data.items || []).forEach((sourceRow) => {
                const row = frm.add_child('items');
                Object.assign(row, sourceRow);
            });

            frm.refresh_field('items');
            clear_receipt_confirmation(frm);
            clear_return_approval(frm);
            calculate_return_summary(frm);
        }
    });
}


function confirm_physical_receipt(frm) {
    if (frm.is_new() || frm.is_dirty()) {
        frappe.msgprint(
            __('Save the return before confirming physical receipt.')
        );
        return;
    }

    frappe.prompt(
        [
            {
                fieldname: 'remarks',
                fieldtype: 'Small Text',
                label: __('Receipt Confirmation Remarks')
            }
        ],
        (values) => {
            frappe.call({
                method:
                    'nkt_operations.nkt_store_operations.'
                    + 'doctype.nkt_customer_return.'
                    + 'nkt_customer_return.'
                    + 'confirm_customer_return_receipt',
                type: 'POST',
                args: {
                    customer_return: frm.doc.name,
                    remarks: values.remarks || ''
                },
                freeze: true,
                freeze_message: __(
                    'Confirming physical warehouse receipt...'
                ),
                callback(r) {
                    if (!r.message) {
                        return;
                    }

                    frappe.show_alert({
                        message: __(
                            'Physical receipt confirmed by {0}.',
                            [r.message.confirmed_by]
                        ),
                        indicator: 'green'
                    });
                    frm.reload_doc();
                }
            });
        },
        __('Confirm Physical Receipt'),
        __('Confirm')
    );
}


function approve_customer_return(frm) {
    if (frm.is_new() || frm.is_dirty()) {
        frappe.msgprint(
            __('Save all return details before approval.')
        );
        return;
    }

    frappe.call({
        method:
            'nkt_operations.nkt_store_operations.'
            + 'doctype.nkt_customer_return.'
            + 'nkt_customer_return.'
            + 'get_customer_return_approval_mode',
        callback(r) {
            if (!r.message) {
                return;
            }

            const direct = r.message.direct_approval;
            const fields = direct
                ? [
                    {
                        fieldname: 'approval_reason',
                        fieldtype: 'Small Text',
                        label: __('Approval Reason'),
                        reqd: 1
                    }
                ]
                : [
                    {
                        fieldname: 'admin_user',
                        fieldtype: 'Data',
                        label: __(
                            'Owner or Administrator Username'
                        ),
                        reqd: 1
                    },
                    {
                        fieldname: 'admin_password',
                        fieldtype: 'Password',
                        label: __(
                            'Owner or Administrator Password'
                        ),
                        reqd: 1
                    },
                    {
                        fieldname: 'approval_reason',
                        fieldtype: 'Small Text',
                        label: __('Approval Reason'),
                        reqd: 1
                    }
                ];

            frappe.prompt(
                fields,
                (values) => {
                    frappe.call({
                        method:
                            'nkt_operations.'
                            + 'nkt_store_operations.'
                            + 'doctype.nkt_customer_return.'
                            + 'nkt_customer_return.'
                            + 'approve_customer_return',
                        type: 'POST',
                        args: {
                            customer_return: frm.doc.name,
                            approval_reason:
                                values.approval_reason,
                            admin_user:
                                values.admin_user || '',
                            admin_password:
                                values.admin_password || ''
                        },
                        freeze: true,
                        freeze_message: __(
                            'Recording return approval...'
                        ),
                        callback(result) {
                            if (!result.message) {
                                return;
                            }

                            frappe.show_alert({
                                message: __(
                                    'Customer return approved by {0}.',
                                    [result.message.approved_by]
                                ),
                                indicator: 'green'
                            });
                            frm.reload_doc();
                        }
                    });
                },
                direct
                    ? __(
                        'Approve as {0}',
                        [r.message.current_user]
                    )
                    : __('Owner/Admin Approval'),
                __('Approve')
            );
        }
    });
}


function update_settlement_ui(frm) {
    const exchange = is_exchange(frm);
    const hasMovement =
        frm.doc.settlement_type === 'Cash Refund'
        || (
            exchange
            && ![
                'Customer Credit',
                'Account Adjustment'
            ].includes(frm.doc.difference_payment_method)
            && (
                flt(frm.doc.customer_pays) > 0.005
                || flt(frm.doc.refund_due) > 0.005
            )
        );

    frm.toggle_display('replacement_section', exchange);
    frm.toggle_display('replacement_items', exchange);
    frm.toggle_display('replacement_value', exchange);
    frm.toggle_display('difference_payment_method', exchange);
    frm.toggle_display('customer_pays', exchange);
    frm.toggle_display('refund_due',
        frm.doc.settlement_type === 'Cash Refund'
        || exchange
    );
    frm.toggle_display(
        'customer_credit_due',
        [
            'Customer Credit',
            'Account Adjustment'
        ].includes(frm.doc.settlement_type)
        || (
            exchange
            && [
                'Customer Credit',
                'Account Adjustment'
            ].includes(frm.doc.difference_payment_method)
        )
    );

    [
        'cashier_settlement_section',
        'settlement_location',
        'settlement_cashier',
        'cashier_shift'
    ].forEach((fieldname) => {
        frm.toggle_display(fieldname, hasMovement);
    });

    frm.toggle_reqd('cashier_shift', hasMovement);
    frm.toggle_reqd('settlement_location', hasMovement);
    frm.toggle_reqd('settlement_cashier', hasMovement);

    frm.toggle_display(
        'other_deduction_reason',
        flt(frm.doc.other_deduction) > 0.005
    );
}


frappe.ui.form.on('NKT Customer Return', {
    setup(frm) {
        frm.set_query('warehouse_release', () => {
            const filters = {
                docstatus: 1,
                release_status: 'Released'
            };

            if (frm.doc.customer) {
                filters.customer = frm.doc.customer;
            } else {
                filters.customer =
                    '__NO_CUSTOMER_SELECTED__';
            }

            return {filters};
        });

        [
            'return_warehouse',
            'settlement_location'
        ].forEach((fieldname) => {
            frm.set_query(fieldname, () => {
                const filters = {
                    is_group: 0,
                    disabled: 0
                };

                if (frm.doc.company) {
                    filters.company = frm.doc.company;
                }

                return {filters};
            });
        });

        frm.set_query('cashier_shift', () => {
            const filters = {
                docstatus: 0,
                status: 'Open',
                cashier:
                    frm.doc.settlement_cashier
                    || frappe.session.user
            };

            if (frm.doc.company) {
                filters.company = frm.doc.company;
            }

            if (frm.doc.settlement_location) {
                filters.settlement_location =
                    frm.doc.settlement_location;
            }

            return {filters};
        });
    },

    refresh(frm) {
        if (frm.is_new()) {
            if (!frm.doc.return_datetime) {
                frm.set_value(
                    'return_datetime',
                    frappe.datetime.now_datetime()
                );
            }

            if (!frm.doc.received_by) {
                frm.set_value(
                    'received_by',
                    frappe.session.user
                );
            }

            if (!frm.doc.settlement_cashier) {
                frm.set_value(
                    'settlement_cashier',
                    frappe.session.user
                );
            }
        }

        calculate_return_summary(frm);
        update_settlement_ui(frm);

        if (
            frm.doc.docstatus === 0
            && !frm.is_new()
            && ![
                'Received',
                'Overridden'
            ].includes(frm.doc.warehouse_receipt_status)
        ) {
            frm.add_custom_button(
                __('Confirm Physical Receipt'),
                () => confirm_physical_receipt(frm),
                __('Actions')
            );
        }

        if (
            frm.doc.docstatus === 0
            && !frm.is_new()
            && frm.doc.approval_status !== 'Approved'
        ) {
            frm.add_custom_button(
                __('Owner/Admin Approve Return'),
                () => approve_customer_return(frm),
                __('Actions')
            );
        }

        if (frm.doc.approval_status === 'Approved') {
            frm.set_intro(
                __(
                    'Return and settlement approved by {0}.',
                    [frm.doc.approved_by]
                ),
                'green'
            );
        } else if (
            [
                'Received',
                'Overridden'
            ].includes(frm.doc.warehouse_receipt_status)
        ) {
            frm.set_intro(
                __('Physical receipt confirmed; management approval is pending.'),
                'blue'
            );
        } else if (frm.doc.docstatus === 0) {
            frm.set_intro(
                __('Physical warehouse receipt must be confirmed before approval.'),
                'orange'
            );
        }

        if (
            frm.doc.docstatus === 1
            && frm.doc.return_delivery_note
        ) {
            frm.add_custom_button(
                __('Open Return Delivery Note'),
                () => frappe.set_route(
                    'Form',
                    'Delivery Note',
                    frm.doc.return_delivery_note
                ),
                __('View')
            );
        }

        if (
            frm.doc.docstatus === 1
            && frm.doc.cashier_movement
        ) {
            frm.add_custom_button(
                __('Open Cashier Movement'),
                () => frappe.set_route(
                    'Form',
                    'NKT Cashier Movement',
                    frm.doc.cashier_movement
                ),
                __('View')
            );
        }
    },

    customer(frm) {
        if (frm.doc.warehouse_release) {
            clear_return_source(frm);
        }
    },

    warehouse_release(frm) {
        load_warehouse_release(frm);
    },

    return_warehouse(frm) {
        clear_receipt_confirmation(frm);
    },

    return_datetime(frm) {
        clear_return_approval(frm);
    },

    settlement_type(frm) {
        if (!is_exchange(frm)) {
            frm.clear_table('replacement_items');
        }
        calculate_return_summary(frm);
        update_settlement_ui(frm);
        clear_return_approval(frm);
    },

    labor_charge(frm) {
        calculate_return_summary(frm);
        clear_return_approval(frm);
    },

    packaging_deduction(frm) {
        calculate_return_summary(frm);
        clear_return_approval(frm);
    },

    handling_deduction(frm) {
        calculate_return_summary(frm);
        clear_return_approval(frm);
    },

    other_deduction(frm) {
        calculate_return_summary(frm);
        update_settlement_ui(frm);
        clear_return_approval(frm);
    },

    other_deduction_reason(frm) {
        clear_return_approval(frm);
    },

    difference_payment_method(frm) {
        clear_return_approval(frm);
    },

    settlement_location(frm) {
        clear_return_approval(frm);
    },

    settlement_cashier(frm) {
        clear_return_approval(frm);
    },

    cashier_shift(frm) {
        if (!frm.doc.cashier_shift) {
            clear_return_approval(frm);
            return;
        }

        frappe.db.get_value(
            'NKT Cashier Shift',
            frm.doc.cashier_shift,
            ['settlement_location', 'cashier']
        ).then((result) => {
            const values = result.message || {};
            frm.set_value(
                'settlement_location',
                values.settlement_location || ''
            );
            frm.set_value(
                'settlement_cashier',
                values.cashier || ''
            );
        });
        clear_return_approval(frm);
    },

    settlement_reference(frm) {
        clear_return_approval(frm);
    },

    settlement_reason(frm) {
        clear_return_approval(frm);
    },

    return_reason(frm) {
        clear_return_approval(frm);
    },

    remarks(frm) {
        clear_return_approval(frm);
    },

    validate(frm) {
        calculate_return_summary(frm);
    },

    items_remove(frm) {
        clear_receipt_confirmation(frm);
        calculate_return_summary(frm);
    },

    replacement_items_remove(frm) {
        calculate_return_summary(frm);
        clear_return_approval(frm);
    }
});


function classification_changed(frm) {
    clear_receipt_confirmation(frm);
    calculate_return_summary(frm);
}


frappe.ui.form.on('NKT Customer Return Item', {
    return_quantity(frm, cdt, cdn) {
        const row = locals[cdt][cdn];

        if (
            flt(row.return_quantity)
            > flt(row.available_return_quantity)
        ) {
            frappe.show_alert({
                message: __(
                    'Physical sacks presented exceeds the available quantity.'
                ),
                indicator: 'orange'
            });
        }

        const classified =
            flt(row.saleable_sacks)
            + flt(row.repairable_sacks)
            + flt(row.opened_sacks)
            + flt(row.rejected_sacks);

        if (classified <= 0.005) {
            row.saleable_sacks = flt(
                row.return_quantity
            );
        }

        classification_changed(frm);
    },

    saleable_sacks(frm) {
        classification_changed(frm);
    },

    repairable_sacks(frm) {
        classification_changed(frm);
    },

    opened_sacks(frm) {
        classification_changed(frm);
    },

    accepted_fraction_kg(frm) {
        classification_changed(frm);
    },

    rejected_sacks(frm) {
        classification_changed(frm);
    }
});


frappe.ui.form.on(
    'NKT Customer Return Replacement Item',
    {
        item(frm, cdt, cdn) {
            const row = locals[cdt][cdn];

            if (!row.item) {
                return;
            }

            frappe.call({
                method:
                    'nkt_operations.nkt_store_operations.'
                    + 'doctype.nkt_customer_return.'
                    + 'nkt_customer_return.'
                    + 'get_replacement_item_defaults',
                args: {
                    item_code: row.item,
                    customer_return: frm.doc.name
                },
                callback(r) {
                    if (!r.message) {
                        return;
                    }

                    row.item_name = r.message.item_name;
                    row.uom = r.message.uom;
                    row.rate = r.message.rate;
                    row.rate_source =
                        r.message.rate_source;

                    if (!row.source_warehouse) {
                        row.source_warehouse =
                            frm.doc.settlement_location
                            || frm.doc.return_warehouse;
                    }

                    calculate_return_summary(frm);
                    clear_return_approval(frm);
                }
            });
        },

        quantity(frm) {
            calculate_return_summary(frm);
            clear_return_approval(frm);
        },

        rate(frm, cdt, cdn) {
            const row = locals[cdt][cdn];
            row.rate_source = 'Manual';
            calculate_return_summary(frm);
            clear_return_approval(frm);
        },

        source_warehouse(frm) {
            clear_return_approval(frm);
        }
    }
);
