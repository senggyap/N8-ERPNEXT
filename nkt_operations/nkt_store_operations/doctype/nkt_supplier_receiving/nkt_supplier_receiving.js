frappe.ui.form.on("NKT Supplier Receiving", {
    refresh(frm) {
        frm.set_intro(
            __("RECEIVE ITEMS — record what physically arrived. Purchase price, payable, Supplier/Trucker responsibility and accounting stay outside this Encoder screen."),
            "blue"
        );

        const roles = frappe.user_roles || [];
        const frontline = roles.includes("NKT Encoder") || roles.includes("NKT Warehouse");

        // Secondary details stay out of the fast path.
        frm.doc.__show_receiving_more = false;
        frm.toggle_display(
            ["bill_of_lading_no", "supplier_delivery_reference", "driver_name", "supporting_document", "receiving_notes"],
            false
        );

        if (frm.doc.docstatus === 0) {
            frm.add_custom_button(__("More Details"), () => {
                frm.doc.__show_receiving_more = !frm.doc.__show_receiving_more;
                frm.toggle_display(
                    ["bill_of_lading_no", "supplier_delivery_reference", "driver_name", "supporting_document", "receiving_notes"],
                    frm.doc.__show_receiving_more
                );
            });

            if (frm.is_new() && !frm.doc.purchase_order) {
                frm.add_custom_button(__("Load Expected Delivery"), () => {
                    frappe.call({
                        method: "nkt_operations.nkt_store_operations.doctype.nkt_supplier_receiving.nkt_supplier_receiving.get_open_purchase_orders",
                        freeze: true,
                        freeze_message: __("Loading expected deliveries..."),
                        callback(r) {
                            const rows = r.message || [];
                            if (!rows.length) {
                                frappe.msgprint(__("There are no open expected deliveries."));
                                return;
                            }

                            const options = rows.map(x =>
                                `${x.name} | ${x.supplier} | ${x.transaction_date || ""}`
                            );

                            const d = new frappe.ui.Dialog({
                                title: __("Load Expected Delivery"),
                                fields: [{
                                    fieldname: "po_display",
                                    fieldtype: "Select",
                                    label: __("Expected Delivery"),
                                    options,
                                    reqd: 1,
                                    description: __("Internal PO number is shown only to identify the correct expected delivery. No price or payable data is exposed.")
                                }],
                                primary_action_label: __("Load"),
                                primary_action(values) {
                                    const selected = (values.po_display || "").split(" | ")[0];
                                    d.hide();
                                    nkt_load_expected_delivery(frm, selected);
                                }
                            });
                            d.show();
                        }
                    });
                });
            }

            // One operational action: set the hidden physical confirmation and Submit.
            frm.page.set_primary_action(__("Receive Items"), async () => {
                if (!frm.doc.purchase_order) {
                    frappe.msgprint(__("Load the Expected Delivery first."));
                    return;
                }
                if (!frm.doc.supplier_dr_no && !frm.doc.bill_of_lading_no && !frm.doc.supplier_delivery_reference) {
                    frappe.msgprint(__("Enter DR No. or use More Details to enter BL/Other Delivery Reference."));
                    return;
                }
                if (!frm.doc.internal_vehicle_no && !frm.doc.plate_number && !frm.doc.delivery_vehicle) {
                    frappe.msgprint(__("Enter Van / Truck No. or Plate No."));
                    return;
                }
                if (!(frm.doc.items || []).length) {
                    frappe.msgprint(__("Load the Expected Delivery first."));
                    return;
                }

                const missing_qty = (frm.doc.items || []).some(row => flt(row.delivered_qty) < 0);
                if (missing_qty) {
                    frappe.msgprint(__("Quantity Received cannot be negative."));
                    return;
                }

                await frm.set_value("physical_quantities_confirmed", 1);
                await frm.save("Submit");
            });
        }

        if (frontline) {
            // Explicitly keep technical/commercial lineage out of normal frontline operation.
            frm.toggle_display(
                [
                    "movement_type", "company", "supplier", "purchase_order",
                    "receiving_date", "receiving_time", "vehicle_operator",
                    "total_expected_qty", "total_delivered_qty", "total_accepted_qty",
                    "total_damaged_qty", "total_other_rejected_qty", "total_rejected_qty",
                    "total_shortage_qty", "total_overdelivery_qty",
                    "physical_quantities_confirmed", "posting_status",
                    "underlying_purchase_receipt", "posted_by", "posted_at"
                ],
                false
            );
        }
    },

    delivery_vehicle(frm) {
        if (!frm.doc.delivery_vehicle) {
            frm.set_value("vehicle_operator", "");
            return;
        }
        frappe.call({
            method: "nkt_operations.nkt_store_operations.doctype.nkt_supplier_receiving.nkt_supplier_receiving.get_vehicle_identity",
            args: { vehicle: frm.doc.delivery_vehicle },
            callback(r) {
                const v = r.message;
                if (!v) return;
                frm.set_value("plate_number", v.plate_number || "");
                frm.set_value("internal_vehicle_no", v.internal_vehicle_no || "");
                frm.set_value("vehicle_operator", v.operator_name || "");
            }
        });
    }
});

function nkt_load_expected_delivery(frm, purchase_order) {
    frappe.call({
        method: "nkt_operations.nkt_store_operations.doctype.nkt_supplier_receiving.nkt_supplier_receiving.load_purchase_order",
        args: { purchase_order },
        freeze: true,
        freeze_message: __("Loading item(s) to receive..."),
        callback(r) {
            const data = r.message;
            if (!data) return;

            frm.set_value("purchase_order", data.purchase_order);
            frm.set_value("supplier", data.supplier);
            frm.set_value("company", data.company);
            frm.clear_table("items");

            let suggested = null;
            for (const x of (data.items || [])) {
                const row = frm.add_child("items");
                row.item_code = x.item_code;
                row.item_name = x.item_name;
                row.purchase_order_item = x.purchase_order_item;
                row.uom = x.uom;
                row.expected_qty = x.expected_qty;

                // IMPORTANT: do not assume the expected quantity physically arrived.
                row.delivered_qty = 0;
                row.accepted_qty = 0;
                row.damaged_qty = 0;
                row.other_rejected_qty = 0;
                row.rejected_qty = 0;
                row.shortage_qty = x.expected_qty;
                row.overdelivery_qty = 0;
                row.condition_classification = "Normal";

                if (!suggested && x.suggested_warehouse) {
                    suggested = x.suggested_warehouse;
                }
            }

            if (suggested && !frm.doc.receiving_warehouse) {
                frm.set_value("receiving_warehouse", suggested);
            }

            frm.refresh_field("items");
            nkt_recalc_receive_items(frm);

            frappe.show_alert({
                message: __("Expected delivery loaded. Enter the actual Quantity Received using the Item UOM."),
                indicator: "green"
            });
        }
    });
}

function nkt_recalc_receive_items(frm) {
    const totals = {
        expected: 0, delivered: 0, accepted: 0,
        damaged: 0, rejected: 0, shortage: 0, overdelivery: 0
    };

    for (const row of (frm.doc.items || [])) {
        const expected = flt(row.expected_qty);
        const delivered = Math.max(flt(row.delivered_qty), 0);
        const damaged = Math.max(flt(row.damaged_qty), 0);
        const other = Math.max(flt(row.other_rejected_qty), 0);
        const rejected = damaged + other;
        const accepted = Math.max(delivered - rejected, 0);
        const shortage = Math.max(expected - delivered, 0);
        const over = Math.max(delivered - expected, 0);

        frappe.model.set_value(row.doctype, row.name, "accepted_qty", accepted);
        frappe.model.set_value(row.doctype, row.name, "rejected_qty", rejected);
        frappe.model.set_value(row.doctype, row.name, "shortage_qty", shortage);
        frappe.model.set_value(row.doctype, row.name, "overdelivery_qty", over);

        if (rejected <= 0) {
            frappe.model.set_value(row.doctype, row.name, "condition_classification", "Normal");
            frappe.model.set_value(row.doctype, row.name, "rejected_warehouse", "");
            frappe.model.set_value(row.doctype, row.name, "condition_reason", "");
        } else if (!row.condition_classification || row.condition_classification === "Normal") {
            frappe.model.set_value(row.doctype, row.name, "condition_classification", "Damaged");
        }

        totals.expected += expected;
        totals.delivered += delivered;
        totals.accepted += accepted;
        totals.damaged += damaged;
        totals.rejected += rejected;
        totals.shortage += shortage;
        totals.overdelivery += over;
    }

    frm.set_value("total_expected_qty", totals.expected);
    frm.set_value("total_delivered_qty", totals.delivered);
    frm.set_value("total_accepted_qty", totals.accepted);
    frm.set_value("total_damaged_qty", totals.damaged);
    frm.set_value("total_other_rejected_qty", 0);
    frm.set_value("total_rejected_qty", totals.rejected);
    frm.set_value("total_shortage_qty", totals.shortage);
    frm.set_value("total_overdelivery_qty", totals.overdelivery);
}

frappe.ui.form.on("NKT Supplier Receiving Item", {
    delivered_qty(frm) { nkt_recalc_receive_items(frm); },
    damaged_qty(frm) { nkt_recalc_receive_items(frm); },
    condition_classification(frm) { nkt_recalc_receive_items(frm); },
    items_remove(frm) { nkt_recalc_receive_items(frm); }
});
