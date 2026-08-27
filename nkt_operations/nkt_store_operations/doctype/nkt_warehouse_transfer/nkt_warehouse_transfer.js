(() => {
    const API = "nkt_operations.nkt_store_operations.features.inventory.warehouse_transfer_fast_sync";
    const REQUEST_PREFIX = "nkt_wh_transfer_request_v1";

    frappe.ui.form.on("NKT Warehouse Transfer", {
        setup(frm) {
            frm.set_query("source_warehouse", () => ({
                filters: { company: frm.doc.company || "", is_group: 0, disabled: 0 }
            }));
            frm.set_query("destination_warehouse", () => ({
                filters: { company: frm.doc.company || "", is_group: 0, disabled: 0 }
            }));
            frm.set_query("item_code", "items", () => ({
                filters: { is_stock_item: 1, disabled: 0 }
            }));
        },

        refresh(frm) {
            if (!frm.is_new()) {
                frm.set_df_property("transfer_date", "read_only", 1);
            }

            const posted = !frm.is_new() && frm.doc.status !== "Draft";
            if (posted) {
                [
                    "company", "internal_dr_no", "source_warehouse", "destination_warehouse",
                    "vehicle_or_unit_no", "plate_no", "driver_name", "items", "notes"
                ].forEach((fieldname) => frm.set_df_property(fieldname, "read_only", 1));
            }

            add_print_button(frm);

            if (frm.is_new()) return;

            frappe.call({
                method: `${API}.get_transfer_action_state`,
                args: {
                    transfer_name: frm.doc.name,
                    device_id: bound_device_id()
                }
            }).then((r) => {
                const state = r.message || {};
                add_physical_action_buttons(frm, state);
            }).catch((err) => {
                console.warn("Warehouse Transfer action state unavailable", err);
            });
        }
    });

    function add_print_button(frm) {
        const allowed =
            !frm.is_new() &&
            (
                frappe.session.user === "Administrator" ||
                [
                    "NKT Encoder",
                    "NKT Warehouse",
                    "NKT ADMINISTRATOR",
                    "NKT OWNER"
                ].some((role) => frappe.user.has_role(role))
            );

        if (!allowed) return;
        frm.add_custom_button(__("Print Transfer DR"), () => {
            frm.meta.default_print_format = "NKT Internal Transfer DR";
            frm.print_doc();
        }, __("Warehouse Transfer"));
    }

    function add_physical_action_buttons(frm, state) {
        if (state.can_release) {
            frm.add_custom_button(__("Release"), () => {
                frappe.confirm(
                    __(
                        "Record the physical Release from {0}? The goods will be treated as in transit until destination Arrival is recorded.",
                        [frm.doc.source_warehouse]
                    ),
                    () => confirm_release(frm)
                );
            }, __("Warehouse Transfer"));
        }

        if (state.can_arrive && (state.arrival_rows || []).length) {
            frm.add_custom_button(__("Receive / Arrive"), () => {
                prompt_arrival(frm, state.arrival_rows || []);
            }, __("Warehouse Transfer"));
        }

        if (state.physical_release_already_recorded && frm.doc.status === "Draft") {
            frm.dashboard.set_headline_alert(
                __("Physical Release already recorded."),
                "blue"
            );
        } else if (state.arrival_rebase_pending) {
            frm.dashboard.set_headline_alert(
                __("Previous physical Arrival already recorded."),
                "blue"
            );
        }
    }

    function confirm_release(frm) {
        const requestId = get_or_create_request_id("dispatch", frm.doc.name);
        frappe.call({
            method: `${API}.confirm_source_dispatch`,
            args: {
                transfer_name: frm.doc.name,
                request_id: requestId,
                device_id: bound_device_id()
            },
            freeze: true,
            freeze_message: __("Recording physical Release...")
        }).then((r) => {
            const result = r.message || {};
            clear_request_id("dispatch", frm.doc.name, requestId);
            frappe.show_alert({
                message: result.replayed
                    ? __("Physical Release was already recorded.")
                    : __("Physical Release recorded."),
                indicator: "green"
            }, 7);
            frm.reload_doc();
        }).catch((err) => {
            show_error(err);
        });
    }

    function prompt_arrival(frm, rows) {
        const fields = [
            {
                fieldtype: "HTML",
                fieldname: "arrival_help",
                options: `<div class="text-muted" style="margin-bottom:8px;">
                    ${__("Enter only the quantity physically received now. Partial arrivals are allowed; any balance remains in transit.")}
                </div>`
            }
        ];

        rows.forEach((row, i) => {
            fields.push({
                fieldtype: "Float",
                fieldname: `arrival_qty_${i}`,
                label: `${row.item_code} — ${__("Remaining")} ${format_number(row.remaining_qty)} ${row.uom || ""}`,
                reqd: 1,
                default: row.remaining_qty,
                description: `${row.item_name || row.item_code} | ${__("Released")}: ${format_number(row.released_qty)} | ${__("Already Recorded")}: ${format_number(row.effective_arrived_qty)}`
            });
        });

        frappe.prompt(
            fields,
            (values) => {
                const quantities = {};
                rows.forEach((row, i) => {
                    quantities[row.item_code] = flt(values[`arrival_qty_${i}`]);
                });

                frappe.confirm(
                    __("Record this physical destination Arrival now?"),
                    () => confirm_arrival(frm, quantities)
                );
            },
            __("Record Physical Arrival"),
            __("Continue")
        );
    }

    function confirm_arrival(frm, quantities) {
        const requestId = get_or_create_request_id("arrival", frm.doc.name);
        frappe.call({
            method: `${API}.confirm_destination_arrival`,
            args: {
                transfer_name: frm.doc.name,
                arrival_quantities: JSON.stringify(quantities),
                request_id: requestId,
                device_id: bound_device_id()
            },
            freeze: true,
            freeze_message: __("Recording physical Arrival...")
        }).then((r) => {
            const result = r.message || {};
            clear_request_id("arrival", frm.doc.name, requestId);

            let message;
            if (result.replayed) {
                message = __("Physical Arrival was already recorded.");
            } else if (Number(result.remaining_quantity || 0) > 0.0000001) {
                message = __("Partial physical Arrival recorded; remaining quantity stays in transit.");
            } else {
                message = __("Physical Arrival recorded.");
            }

            frappe.show_alert({
                message,
                indicator: "green"
            }, 8);
            frm.reload_doc();
        }).catch((err) => {
            show_error(err);
        });
    }

    function request_storage_key(action, transferName) {
        return `${REQUEST_PREFIX}:${action}:${transferName}`;
    }

    function get_or_create_request_id(action, transferName) {
        const key = request_storage_key(action, transferName);
        try {
            const existing = String(window.localStorage.getItem(key) || "").trim();
            if (existing) return existing;
            const next = uuid();
            window.localStorage.setItem(key, next);
            return next;
        } catch (_) {
            return uuid();
        }
    }

    function clear_request_id(action, transferName, requestId) {
        const key = request_storage_key(action, transferName);
        try {
            const stored = String(window.localStorage.getItem(key) || "").trim();
            if (!stored || stored === requestId) {
                window.localStorage.removeItem(key);
            }
        } catch (_) {}
    }

    function bound_device_id() {
        try {
            return String(window.localStorage.getItem("nkt_device_id") || "").trim();
        } catch (_) {
            return "";
        }
    }

    function uuid() {
        if (window.crypto?.randomUUID) return window.crypto.randomUUID();
        return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
            const r = Math.random() * 16 | 0;
            const v = c === "x" ? r : (r & 3 | 8);
            return v.toString(16);
        });
    }

    function show_error(err) {
        const message =
            err?.message ||
            err?.exc ||
            __("Unable to record the warehouse transfer action.");
        frappe.msgprint({
            title: __("Warehouse Transfer"),
            indicator: "red",
            message
        });
    }
})();
