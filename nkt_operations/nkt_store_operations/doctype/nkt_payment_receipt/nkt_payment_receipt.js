function approval_matches_overpayment(frm, overpayment) {
	return (
		Number(frm.doc.overpayment_override_approved || 0) === 1 &&
		Math.abs(
			flt(frm.doc.overpayment_override_approved_amount) -
			flt(overpayment)
		) <= 0.005
	);
}


function clear_local_overpayment_approval(frm) {
	frm.set_value("overpayment_override_approved", 0);
	frm.set_value("overpayment_override_approved_amount", 0);
	frm.set_value("overpayment_override_approved_by", "");
	frm.set_value("overpayment_override_approved_on", "");
	frm.set_value("overpayment_override_reason", "");
}


function setup_overpayment_override_button(frm) {
    frm.remove_custom_button(
        __("Owner/Admin Overpayment Override"),
        __("Actions")
    );

    frm.remove_custom_button(
        __("Admin Overpayment Override"),
        __("Actions")
    );

    const overpayment = flt(
        frm.doc.overpayment_amount
    );

    const approval_valid =
        approval_matches_overpayment(
            frm,
            overpayment
        );

    if (
        frm.doc.docstatus !== 0
        || frm.is_new()
        || overpayment <= 0.005
        || approval_valid
    ) {
        return;
    }

    frm.add_custom_button(
        __("Owner/Admin Overpayment Override"),
        () => {
            if (frm.is_dirty()) {
                frappe.msgprint(
                    __(
                        "Save the overpaid receipt as "
                        + "Draft before requesting "
                        + "the override."
                    )
                );
                return;
            }

            const direct_approval =
                frappe.session.user === "Administrator"
                || frappe.user.has_role("NKT OWNER")
                || frappe.user.has_role(
                    "NKT ADMINISTRATOR"
                );

            const fields = direct_approval
                ? [
                    {
                        label: __("Reason"),
                        fieldname: "reason",
                        fieldtype: "Small Text",
                        reqd: 1
                    }
                ]
                : [
                    {
                        label: __(
                            "Owner or Administrator Username"
                        ),
                        fieldname: "admin_user",
                        fieldtype: "Data",
                        reqd: 1
                    },
                    {
                        label: __(
                            "Owner or Administrator Password"
                        ),
                        fieldname: "admin_password",
                        fieldtype: "Password",
                        reqd: 1
                    },
                    {
                        label: __("Reason"),
                        fieldname: "reason",
                        fieldtype: "Small Text",
                        reqd: 1
                    }
                ];

            const title = direct_approval
                ? __(
                    "Approve Overpayment as {0}",
                    [frappe.session.user]
                )
                : __(
                    "Owner/Admin Overpayment Override"
                );

            frappe.prompt(
                fields,
                (values) => {
                    frappe.call({
                        method:
                            "nkt_operations."
                            + "nkt_store_operations."
                            + "doctype."
                            + "nkt_payment_receipt."
                            + "nkt_payment_receipt."
                            + "approve_overpayment",
                        type: "POST",
                        args: {
                            payment_receipt:
                                frm.doc.name,
                            admin_user:
                                values.admin_user || "",
                            admin_password:
                                values.admin_password || "",
                            reason: values.reason
                        },
                        freeze: true,
                        freeze_message: __(
                            "Recording overpayment approval..."
                        ),
                        callback(r) {
                            if (
                                r.message
                                && r.message.approved
                            ) {
                                frappe.show_alert({
                                    message: __(
                                        "Overpayment "
                                        + "override approved."
                                    ),
                                    indicator: "green"
                                });

                                frm.reload_doc();
                            }
                        }
                    });
                },
                title,
                __("Approve")
            );
        },
        __("Actions")
    );
}


function calculate_payment_receipt(frm) {
	let total_payment = 0;
	let total_cash = 0;
	let total_non_cash = 0;
	let total_account_charge = 0;

	(frm.doc.payments || []).forEach((row) => {
		const amount = flt(row.amount);

		total_payment += amount;

		if (row.payment_method === "Cash") {
			row.affects_cash_drawer = 1;
			row.verification_status = "Not Required";
			if (flt(row.cash_tendered) <= 0 && amount > 0) {
				row.cash_tendered = amount;
			}
			row.change_amount = Math.max(
				flt(row.cash_tendered) - amount,
				0
			);

			total_cash += amount;
		} else if (row.payment_method === "Account") {
			row.affects_cash_drawer = 0;
			row.verification_status = "Not Required";
			row.change_amount = 0;

			total_account_charge += amount;
		} else {
			row.affects_cash_drawer = 0;
			row.verification_status = "Not Required";
			row.change_amount = 0;

			total_non_cash += amount;
		}
	});

	frm.set_value("total_payment", total_payment);
	frm.set_value("total_cash", total_cash);
	frm.set_value("total_non_cash", total_non_cash);
	frm.set_value(
		"total_account_charge",
		total_account_charge
	);
	frm.set_value("verification_required", 0);

	const is_order_payment =
		frm.doc.payment_purpose === "Order Payment" &&
		frm.doc.customer_order;

	const amount_due = is_order_payment
		? flt(frm.doc.amount_due_before_receipt)
		: 0;

	const remaining_balance = is_order_payment
		? Math.max(amount_due - total_payment, 0)
		: 0;

	const overpayment = is_order_payment
		? Math.max(total_payment - amount_due, 0)
		: 0;

	let approval_valid =
		approval_matches_overpayment(frm, overpayment);

	if (
		Number(frm.doc.overpayment_override_approved || 0) === 1 &&
		!approval_valid
	) {
		clear_local_overpayment_approval(frm);
		approval_valid = false;
	}

	frm.set_value(
		"remaining_balance",
		remaining_balance
	);
	frm.set_value(
		"overpayment_amount",
		overpayment
	);
	frm.set_value(
		"overpayment_override_required",
		overpayment > 0.005 ? 1 : 0
	);
	frm.set_value(
		"customer_advance_amount",
		approval_valid ? overpayment : 0
	);

	frm.refresh_field("payments");
	setup_overpayment_override_button(frm);
}


function load_order_balance(frm) {
	if (!frm.doc.customer_order) {
		frm.set_value("order_total", 0);
		frm.set_value("previously_applied", 0);
		frm.set_value(
			"amount_due_before_receipt",
			0
		);
		frm.set_value("remaining_balance", 0);

		calculate_payment_receipt(frm);
		return;
	}

	frappe.call({
		method:
			"nkt_operations.nkt_store_operations.doctype." +
			"nkt_payment_receipt.nkt_payment_receipt." +
			"get_order_balance",
		args: {
			customer_order: frm.doc.customer_order,
			payment_receipt: frm.doc.name
		},
		callback(r) {
			if (!r.message) {
				return;
			}

			frm.set_value(
				"order_total",
				r.message.order_total
			);
			frm.set_value(
				"previously_applied",
				r.message.previously_applied
			);
			frm.set_value(
				"amount_due_before_receipt",
				r.message.amount_due_before_receipt
			);

			calculate_payment_receipt(frm);
		}
	});
}


function apply_payment_method_defaults(frm, cdt, cdn) {
	const row = locals[cdt][cdn];

	if (row.payment_method === "Cash") {
		frappe.model.set_value(
			cdt,
			cdn,
			"affects_cash_drawer",
			1
		);
		if (flt(row.cash_tendered) <= 0 && flt(row.amount) > 0) {
			frappe.model.set_value(
				cdt,
				cdn,
				"cash_tendered",
				flt(row.amount)
			);
		}
		frappe.model.set_value(
			cdt,
			cdn,
			"verification_status",
			"Not Required"
		);
	} else {
		frappe.model.set_value(
			cdt,
			cdn,
			"affects_cash_drawer",
			0
		);
		frappe.model.set_value(
			cdt,
			cdn,
			"cash_tendered",
			0
		);
		frappe.model.set_value(
			cdt,
			cdn,
			"change_amount",
			0
		);
		frappe.model.set_value(
			cdt,
			cdn,
			"verification_status",
			"Not Required"
		);
	}

	calculate_payment_receipt(frm);
}


frappe.ui.form.on("NKT Payment Receipt", {
	refresh(frm) {
		if (frm.is_new()) {
			if (!frm.doc.receipt_datetime) {
				frm.set_value(
					"receipt_datetime",
					frappe.datetime.now_datetime()
				);
			}

			if (!frm.doc.received_by) {
				frm.set_value(
					"received_by",
					frappe.session.user
				);
			}

			if (!frm.doc.encoded_by) {
				frm.set_value(
					"encoded_by",
					frappe.session.user
				);
			}

			if (!frm.doc.receipt_status) {
				frm.set_value(
					"receipt_status",
					"Draft"
				);
			}
		}

		load_order_balance(frm);
		calculate_payment_receipt(frm);
	},

	payment_purpose(frm) {
		load_order_balance(frm);
	},

	customer(frm) {
		frm.set_value("customer_order", "");
		load_order_balance(frm);
	},

	customer_order(frm) {
		load_order_balance(frm);
	},

	validate(frm) {
		calculate_payment_receipt(frm);
	},

	payments_remove(frm) {
		calculate_payment_receipt(frm);
	}
});


frappe.ui.form.on("NKT Payment Detail", {
	payment_method(frm, cdt, cdn) {
		apply_payment_method_defaults(frm, cdt, cdn);
	},

	amount(frm) {
		calculate_payment_receipt(frm);
	},

	cash_tendered(frm) {
		calculate_payment_receipt(frm);
	},

	payments_add(frm, cdt, cdn) {
		const row = locals[cdt][cdn];

		if (!row.payment_method) {
			frappe.model.set_value(
				cdt,
				cdn,
				"payment_method",
				"Cash"
			);
		}
	}
});


// NKT_CUSTOMER_ORDER_FILTER_V1
frappe.ui.form.on("NKT Payment Receipt", {
    setup(frm) {
        frm.set_query("customer_order", () => {
            if (
                frm.doc.payment_purpose !== "Order Payment" ||
                !frm.doc.customer
            ) {
                return {
                    filters: {
                        name: ["=", "__NO_ELIGIBLE_ORDER__"],
                    },
                };
            }

            const filters = {
                docstatus: 1,
                customer: frm.doc.customer,
                amount_due: [">", 0.005],
            };

            if (frm.doc.company) {
                filters.company = frm.doc.company;
            }

            return {
                filters: filters,
            };
        });
    },

    customer(frm) {
        if (frm.doc.customer_order) {
            frm.set_value("customer_order", "");
        }

        frm.refresh_field("customer_order");
    },

    company(frm) {
        if (frm.doc.customer_order) {
            frm.set_value("customer_order", "");
        }

        frm.refresh_field("customer_order");
    },

    payment_purpose(frm) {
        if (
            frm.doc.payment_purpose !== "Order Payment" &&
            frm.doc.customer_order
        ) {
            frm.set_value("customer_order", "");
        }

        frm.refresh_field("customer_order");
    },
});



// NKT_CASHIER_SHIFT_AUTO_ASSIGN_V2
frappe.ui.form.on('NKT Payment Receipt', {
    refresh(frm) {
        ['cashier_shift_section', 'cashier_shift', 'settlement_location', 'cashier_movement_note']
            .forEach((fieldname) => frm.toggle_display(fieldname, false));

        if (frm.is_new()) {
            frm.set_intro(
                __('The receiving user’s one Open Cashier Shift is assigned automatically when this receipt is saved.'),
                'blue'
            );
        }
    }
});
