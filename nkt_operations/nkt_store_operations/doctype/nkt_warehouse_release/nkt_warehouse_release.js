function calculate_release_summary(frm) {
	let total_release_quantity = 0;
	let is_partial_release = 0;

	(frm.doc.items || []).forEach((row) => {
		const release_quantity = flt(row.release_quantity);
		const remaining_quantity = flt(row.remaining_quantity);

		total_release_quantity += release_quantity;

		if (
			remaining_quantity > 0 &&
			release_quantity < remaining_quantity
		) {
			is_partial_release = 1;
		}
	});

	frm.set_value(
		"total_release_quantity",
		total_release_quantity
	);

	frm.set_value(
		"is_partial_release",
		is_partial_release
	);
}


function clear_release_items(frm) {
	frm.clear_table("items");
	frm.set_value("total_release_quantity", 0);
	frm.set_value("is_partial_release", 0);
	frm.refresh_field("items");
}


function load_customer_order(frm) {
	if (!frm.doc.customer_order) {
		clear_release_items(frm);
		return;
	}

	frappe.call({
		method:
			"nkt_operations.nkt_store_operations.doctype." +
			"nkt_warehouse_release.nkt_warehouse_release." +
			"get_release_data",

		args: {
			customer_order: frm.doc.customer_order,
			warehouse_release: frm.doc.name
		},

		freeze: true,
		freeze_message: __("Loading order items..."),

		callback(r) {
			if (!r.message) {
				return;
			}

			const data = r.message;

			if (
				frm.doc.customer &&
				frm.doc.customer !== data.customer
			) {
				frappe.msgprint(
					__(
						"The selected order belongs to a different customer."
					)
				);

				frm.set_value("customer_order", "");
				clear_release_items(frm);
				return;
			}

			if (!frm.doc.customer) {
				frm.set_value(
					"customer",
					data.customer
				);
			}

			frm.set_value("company", data.company);
			frm.set_value(
				"customer_name",
				data.customer_name
			);

			frm.clear_table("items");

			(data.items || []).forEach((source_row) => {
				const row = frm.add_child("items");

				row.customer_order_item =
					source_row.customer_order_item;

				row.item = source_row.item;
				row.item_name = source_row.item_name;

				row.ordered_quantity =
					source_row.ordered_quantity;

				row.previously_released_quantity =
					source_row.previously_released_quantity;

				row.remaining_quantity =
					source_row.remaining_quantity;

				row.release_quantity =
					source_row.remaining_quantity;

				row.uom = source_row.uom;

				row.source_warehouse =
					source_row.source_warehouse;
			});

			frm.refresh_field("items");
			calculate_release_summary(frm);
		}
	});
}


frappe.ui.form.on("NKT Warehouse Release", {
	setup(frm) {
		frm.set_query("customer_order", () => {
			const filters = {
				docstatus: 1,
				status: [
					"in",
					[
						"Ready for Release",
						"Partially Released"
					]
				]
			};

			if (frm.doc.customer) {
				filters.customer = frm.doc.customer;
			} else {
				filters.customer = "";
			}

			return {
				filters: filters
			};
		});
	},

	refresh(frm) {
		if (frm.is_new()) {
			if (!frm.doc.release_datetime) {
				frm.set_value(
					"release_datetime",
					frappe.datetime.now_datetime()
				);
			}

			if (!frm.doc.released_by) {
				frm.set_value(
					"released_by",
					frappe.session.user
				);
			}

			if (!frm.doc.release_status) {
				frm.set_value(
					"release_status",
					"Draft"
				);
			}
		}

		frm.toggle_enable(
			"customer",
			frm.doc.docstatus === 0
		);

		frm.toggle_enable(
			"customer_order",
			frm.doc.docstatus === 0
		);

		calculate_release_summary(frm);
	},

	customer(frm) {
		if (frm.doc.customer_order) {
			frm.set_value("customer_order", "");
		}

		clear_release_items(frm);
	},

	customer_order(frm) {
		load_customer_order(frm);
	},

	validate(frm) {
		calculate_release_summary(frm);
	},

	items_remove(frm) {
		calculate_release_summary(frm);
	}
});


frappe.ui.form.on("NKT Warehouse Release Item", {
	release_quantity(frm) {
		calculate_release_summary(frm);
	}
});

// C15E — later-known customer receipt Plate/DR completion.
// This updates the separate Customer Receipt Record, never the original sale.
frappe.ui.form.on("NKT Warehouse Release", {
    refresh(frm) {
        if (!frm.doc.customer_order) return;
        const roles = frappe.user_roles || [];
        const allowed = roles.some(r =>
            ["NKT Warehouse", "NKT OWNER", "NKT ADMINISTRATOR", "System Manager"].includes(r)
        );
        if (!allowed) return;

        frm.add_custom_button(__("Receipt Plate / DR References"), () => {
            const d = new frappe.ui.Dialog({
                title: __("Customer Receipt Delivery References"),
                fields: [
                    {
                        fieldname: "plate_reference",
                        fieldtype: "Data",
                        label: __("Plate / Vehicle Reference"),
                        default: frm.doc.custom_nkt_plate_number || ""
                    },
                    {
                        fieldname: "dr_reference",
                        fieldtype: "Data",
                        label: __("DR / Delivery Reference"),
                        default: frm.doc.custom_nkt_mother_release_reference || ""
                    },
                    {
                        fieldname: "reason",
                        fieldtype: "Small Text",
                        label: __("Reason / Source Note"),
                        reqd: 1,
                        default: __("Reference confirmed from warehouse/delivery paperwork.")
                    }
                ],
                primary_action_label: __("Save References"),
                primary_action(values) {
                    frappe.call({
                        method: "nkt_operations.nkt_store_operations.features.reports_history.receipt_support.update_customer_receipt_references",
                        type: "POST",
                        args: {
                            customer_order: frm.doc.customer_order,
                            warehouse_release: frm.doc.name,
                            plate_reference: values.plate_reference || "",
                            dr_reference: values.dr_reference || "",
                            reason: values.reason
                        },
                        freeze: true,
                        callback(r) {
                            if (r.message) {
                                frappe.show_alert({message: __("Customer receipt references saved."), indicator: "green"});
                                d.hide();
                            }
                        }
                    });
                }
            });
            d.show();
        }, __("Print / Customer Receipt"));
    }
});
