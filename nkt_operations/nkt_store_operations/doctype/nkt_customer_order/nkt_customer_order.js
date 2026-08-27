// NKT_C15F_R8A_SUPPORT_RECONCILIATION_GUARD
function nkt_r8a_can_retry_reconciliation() {
    const roles = frappe.user_roles || [];
    return frappe.session.user === "Administrator"
        || ["System Manager", "NKT OWNER", "NKT ADMINISTRATOR"].some((role) => roles.includes(role));
}


function nkt_calculate_declared_payments(frm) {
    let total = 0, cash = 0, non_cash = 0, account = 0;
    (frm.doc.declared_payments || []).forEach((row) => {
        const amount = flt(row.amount);
        total += amount;
        if (row.payment_method === "Cash") cash += amount;
        else if (row.payment_method === "Account") account += amount;
        else non_cash += amount;
    });
    frm.set_value("declared_payment_total", total);
    frm.set_value("declared_cash", cash);
    frm.set_value("declared_non_cash", non_cash);
    frm.set_value("declared_account", account);
}

function calculate_order(frm) {
	let total_quantity = 0;
	let grand_total = 0;

	(frm.doc.items || []).forEach((row) => {
		const quantity = flt(row.quantity);
		const standard_rate = flt(row.standard_rate);
		const adjustment = flt(row.price_adjustment);

		row.final_rate = standard_rate + adjustment;
		row.amount = quantity * row.final_rate;

		total_quantity += quantity;
		grand_total += row.amount;
	});

	frm.set_value("total_quantity", total_quantity);
	frm.set_value("grand_total", grand_total);
	frm.refresh_field("items");
}


async function load_item_details(frm, cdt, cdn) {
    const row = locals[cdt][cdn];
    if (!row.item) return;

    const r = await frappe.call({
        method: "nkt_operations.nkt_store_operations.doctype.nkt_customer_order.nkt_customer_order.get_encoder_item_context",
        args: {item_code: row.item}
    });
    const item = r.message || {};

    await frappe.model.set_value(cdt, cdn, "item_name", item.item_name || "");
    await frappe.model.set_value(cdt, cdn, "uom", item.stock_uom || "");
    await frappe.model.set_value(cdt, cdn, "standard_rate", flt(item.standard_rate || 0));

    if (!row.source_warehouse && frm.doc.default_warehouse) {
        await frappe.model.set_value(cdt, cdn, "source_warehouse", frm.doc.default_warehouse);
    }

    calculate_order(frm);
}


frappe.ui.form.on("NKT Customer Order", {
	setup(frm) {
		frm.set_query("item", "items", () => ({
			filters: {
				disabled: 0,
				is_stock_item: 1,
				is_sales_item: 1,
				nkt_stock_form: "Saleable Sack"
			}
		}));

		frm.set_query("default_warehouse", () => ({
			filters: {
				company: frm.doc.company,
				is_group: 0
			}
		}));

		frm.set_query("source_warehouse", "items", () => ({
			filters: {
				company: frm.doc.company,
				is_group: 0
			}
		}));
	},

	refresh(frm) {
		if (frm.is_new()) {
			frm.set_value("encoder", frappe.session.user);

			if (!frm.doc.status) {
				frm.set_value("status", "Draft");
			}
		}

		calculate_order(frm);
		nkt_calculate_declared_payments(frm);
		if (nkt_r8a_can_retry_reconciliation() && frm.doc.docstatus === 1 && ["Unmatched", "Ambiguous"].includes(frm.doc.cashier_reconciliation_status)) {
			frm.add_custom_button(__("Retry Cashier Reconciliation"), () => {
				frappe.call({
					method: "nkt_operations.nkt_store_operations.features.sales.matching.retry_match",
					type: "POST",
					args: {customer_order: frm.doc.name},
					freeze: true,
					callback: () => frm.reload_doc()
				});
			});
		}
	},

	default_warehouse(frm) {
		(frm.doc.items || []).forEach((row) => {
			if (!row.source_warehouse) {
				frappe.model.set_value(
					row.doctype,
					row.name,
					"source_warehouse",
					frm.doc.default_warehouse
				);
			}
		});
	},

	validate(frm) {
		calculate_order(frm);
		nkt_calculate_declared_payments(frm);
	},

	items_remove(frm) {
		calculate_order(frm);
	}
});


frappe.ui.form.on("NKT Customer Order Item", {
	item(frm, cdt, cdn) {
		load_item_details(frm, cdt, cdn);
	},

	quantity(frm) {
		calculate_order(frm);
	},

	price_adjustment(frm) {
		calculate_order(frm);
	},

	final_rate(frm) {
		calculate_order(frm);
	},

	items_add(frm, cdt, cdn) {
		if (frm.doc.default_warehouse) {
			frappe.model.set_value(
				cdt,
				cdn,
				"source_warehouse",
				frm.doc.default_warehouse
			);
		}
	}
});

// NKT_WAREHOUSE_OVERRIDE_BUTTON_V3
frappe.ui.form.on("NKT Customer Order", {
    refresh(frm) {
        const needs_override =
            frm.doc.docstatus === 1
            && frm.doc.requires_admin_confirmation
            && frm.doc.admin_confirmation_status
                !== "Confirmed"
            && frm.doc.status !== "Released";

        if (!needs_override) {
            return;
        }

        frm.add_custom_button(
            __("Owner/Admin Warehouse Override"),
            () => {
                const direct_approval =
                    frappe.session.user === "Administrator"
                    || frappe.user.has_role("NKT OWNER")
                    || frappe.user.has_role(
                        "NKT ADMINISTRATOR"
                    );

                const fields = direct_approval
                    ? [
                        {
                            fieldname: "reason",
                            fieldtype: "Small Text",
                            label: __("Override Reason"),
                            reqd: 1,
                            description: __(
                                "Explain why stock is being "
                                + "withdrawn from the "
                                + "restricted warehouse."
                            )
                        }
                    ]
                    : [
                        {
                            fieldname: "admin_user",
                            fieldtype: "Data",
                            label: __(
                                "Owner or Administrator "
                                + "Username"
                            ),
                            reqd: 1
                        },
                        {
                            fieldname: "admin_password",
                            fieldtype: "Password",
                            label: __(
                                "Owner or Administrator "
                                + "Password"
                            ),
                            reqd: 1
                        },
                        {
                            fieldname: "reason",
                            fieldtype: "Small Text",
                            label: __("Override Reason"),
                            reqd: 1,
                            description: __(
                                "Explain why stock is being "
                                + "withdrawn from the "
                                + "restricted warehouse."
                            )
                        }
                    ];

                const title = direct_approval
                    ? __(
                        "Approve Warehouse Override as {0}",
                        [frappe.session.user]
                    )
                    : __(
                        "Owner/Admin Warehouse Override"
                    );

                frappe.prompt(
                    fields,
                    (values) => {
                        frappe.call({
                            method:
                                "nkt_operations."
                                + "nkt_store_operations."
                                + "doctype."
                                + "nkt_customer_order."
                                + "nkt_customer_order."
                                + "approve_warehouse_withdrawal",
                            type: "POST",
                            args: {
                                customer_order:
                                    frm.doc.name,
                                admin_user:
                                    values.admin_user || "",
                                admin_password:
                                    values.admin_password || "",
                                reason: values.reason
                            },
                            freeze: true,
                            freeze_message: __(
                                "Recording warehouse "
                                + "override approval..."
                            ),
                            callback(r) {
                                if (!r.message) {
                                    return;
                                }

                                frappe.show_alert({
                                    message: __(
                                        "Warehouse withdrawal "
                                        + "approved by {0}.",
                                        [
                                            r.message
                                                .admin_confirmed_by
                                            || r.message
                                                .confirmed_by
                                        ]
                                    ),
                                    indicator: "green"
                                });

                                frm.reload_doc();
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
});


frappe.ui.form.on("NKT Declared Payment", {
    payment_method(frm) { nkt_calculate_declared_payments(frm); },
    amount(frm) { nkt_calculate_declared_payments(frm); },
    declared_payments_add(frm) { nkt_calculate_declared_payments(frm); },
    declared_payments_remove(frm) { nkt_calculate_declared_payments(frm); }
});
