const T = {
    repair: "Repair / Rebag Damaged Sack",
    recover: "Recover Damaged Contents to Fraction",
    pack: "Pack Fraction into Saleable Sacks",
    complete: "Complete Underweight Damaged Sack",
    dd: "Dispose Damaged Stock",
    df: "Dispose Fraction Stock"
};

function is_disposal(frm) {
    return [T.dd, T.df].includes(frm.doc.recovery_type);
}

function is_conversion(frm) {
    return [T.repair,T.recover,T.pack,T.complete].includes(frm.doc.recovery_type);
}

function reset_approval(frm) {
    if (is_conversion(frm)) {
        frm.doc.approval_status = "Not Required";
        if (frm.doc.docstatus === 0) frm.doc.status = "Draft";
        frm.doc.approved_by = "";
        frm.doc.approved_on = "";
        frm.doc.approval_reason = "";
        frm.doc.approval_signature = "";
        frm.refresh_fields([
            "approval_status","status","approved_by","approved_on",
            "approval_reason","approval_signature"
        ]);
        return;
    }
    if (frm.doc.approval_status !== "Approved") return;
    frm.set_value("approval_status", "Pending Approval");
    frm.set_value("status", "Pending Approval");
    frm.set_value("approved_by", "");
    frm.set_value("approved_on", "");
    frm.set_value("approval_reason", "");
    frm.set_value("approval_signature", "");
    frappe.show_alert({
        message: __("Recovery changed. Approval is required again."),
        indicator: "orange"
    });
}

function calculate(frm) {
    let expected = 0;
    let loss = 0;
    if (frm.doc.recovery_type === T.recover) {
        expected = flt(frm.doc.damaged_sacks_consumed)
            * flt(frm.doc.standard_sack_weight_kg);
        loss = Math.max(expected - flt(frm.doc.fraction_kg_produced), 0);
    } else if (frm.doc.recovery_type === T.complete) {
        expected = flt(frm.doc.saleable_sacks_produced)
            * flt(frm.doc.standard_sack_weight_kg);
        loss = Math.max(
            expected - flt(frm.doc.measured_damaged_contents_kg)
            - flt(frm.doc.fraction_kg_consumed), 0
        );
    } else if (frm.doc.recovery_type === T.pack) {
        const required = flt(frm.doc.saleable_sacks_produced)
            * flt(frm.doc.standard_sack_weight_kg);
        if (Math.abs(flt(frm.doc.fraction_kg_consumed) - required) > 0.005) {
            frm.doc.fraction_kg_consumed = required;
            frm.refresh_field("fraction_kg_consumed");
        }
    }
    frm.set_value("expected_weight_kg", expected);
    frm.set_value("recorded_loss_kg", loss);
}

function visibility(frm) {
    const a = frm.doc.recovery_type;
    const map = {
        damaged_sacks_consumed: [T.repair,T.recover,T.complete,T.dd].includes(a),
        measured_damaged_contents_kg: a === T.complete,
        fraction_kg_consumed: [T.pack,T.complete,T.df].includes(a),
        saleable_sacks_produced: [T.repair,T.pack,T.complete].includes(a),
        fraction_kg_produced: a === T.recover,
        expected_weight_kg: [T.recover,T.complete].includes(a),
        recorded_loss_kg: [T.recover,T.complete].includes(a),
        available_fraction_kg: a === T.pack,
        max_saleable_sacks: a === T.pack,
        target_warehouse: Boolean(a) && !is_disposal(frm),
        disposal_expense_account: is_disposal(frm)
    };
    Object.entries(map).forEach(([field, show]) => frm.toggle_display(field, show));
    frm.toggle_reqd("target_warehouse", Boolean(a) && !is_disposal(frm));
    frm.toggle_reqd("disposal_expense_account", is_disposal(frm));
}

function clear_qty(frm) {
    ["damaged_sacks_consumed","measured_damaged_contents_kg","fraction_kg_consumed",
     "saleable_sacks_produced","fraction_kg_produced"].forEach(
        field => frm.set_value(field, 0)
    );
    calculate(frm);
}

function load_pack_capacity(frm) {
    if (
        frm.doc.recovery_type !== T.pack
        || !frm.doc.base_saleable_item
        || !frm.doc.source_warehouse
    ) {
        frm.doc.available_fraction_kg = 0;
        frm.doc.max_saleable_sacks = 0;
        frm.refresh_fields(["available_fraction_kg","max_saleable_sacks"]);
        return;
    }
    frappe.call({
        method: "nkt_operations.nkt_store_operations.doctype.nkt_stock_recovery.nkt_stock_recovery.get_pack_capacity",
        args: {
            item_code: frm.doc.base_saleable_item,
            warehouse: frm.doc.source_warehouse
        },
        callback(r) {
            if (!r.message) return;
            frm.doc.available_fraction_kg = flt(r.message.available_fraction_kg);
            frm.doc.max_saleable_sacks = cint(r.message.max_saleable_sacks);
            frm.refresh_fields(["available_fraction_kg","max_saleable_sacks"]);
            check_pack_max(frm, false);
        }
    });
}

function check_pack_max(frm, noisy=true) {
    if (frm.doc.recovery_type !== T.pack) return true;
    const requested = flt(frm.doc.saleable_sacks_produced);
    const maximum = cint(frm.doc.max_saleable_sacks);
    if (requested > maximum && requested > 0) {
        if (noisy) {
            frappe.msgprint({
                title: __("Too Many Sacks"),
                indicator: "red",
                message: __(
                    "Maximum from the current Fraction balance is {0} sack(s). "
                    + "Available: {1} kg. Enter {0} or less.",
                    [maximum, flt(frm.doc.available_fraction_kg)]
                )
            });
        }
        return false;
    }
    return true;
}

function load_mapping(frm) {
    if (!frm.doc.base_saleable_item) return;
    frappe.call({
        method: "nkt_operations.nkt_store_operations.doctype.nkt_stock_recovery.nkt_stock_recovery.get_item_mapping",
        args: {item_code: frm.doc.base_saleable_item},
        callback(r) {
            if (!r.message) return;
            frm.set_value("damaged_item", r.message.damaged_item);
            frm.set_value("fraction_item", r.message.fraction_item);
            frm.set_value("standard_sack_weight_kg", r.message.standard_sack_weight_kg);
            calculate(frm);
            load_pack_capacity(frm);
        }
    });
}

function submit_recovery_approval(frm, values) {
    frappe.call({
        method: "nkt_operations.nkt_store_operations."
            + "doctype.nkt_stock_recovery."
            + "nkt_stock_recovery.approve_stock_recovery",
        type: "POST",
        args: {
            stock_recovery: frm.doc.name,
            approval_reason: values.approval_reason,
            admin_user: values.admin_user || "",
            admin_password: values.admin_password || ""
        },
        freeze: true,
        freeze_message: __(
            "Recording recovery approval..."
        ),
        callback(r) {
            if (!r.message) return;

            frappe.show_alert({
                message: __(
                    "Approved by {0}.",
                    [r.message.approved_by]
                ),
                indicator: "green"
            });

            frm.reload_doc();
        }
    });
}


function approve(frm) {
    if (frm.is_new() || frm.is_dirty()) {
        frappe.msgprint(
            __("Save all changes before approval.")
        );
        return;
    }

    frappe.call({
        method: "nkt_operations.nkt_store_operations."
            + "doctype.nkt_stock_recovery."
            + "nkt_stock_recovery."
            + "get_stock_recovery_approval_mode",
        callback(r) {
            if (!r.message) return;

            const direct = r.message.direct_approval;

            const fields = direct
                ? [
                    {
                        fieldname: "approval_reason",
                        fieldtype: "Small Text",
                        label: __("Approval Reason"),
                        reqd: 1
                    }
                ]
                : [
                    {
                        fieldname: "admin_user",
                        fieldtype: "Data",
                        label: __(
                            "Owner/Admin Username"
                        ),
                        reqd: 1
                    },
                    {
                        fieldname: "admin_password",
                        fieldtype: "Password",
                        label: __(
                            "Owner/Admin Password"
                        ),
                        reqd: 1
                    },
                    {
                        fieldname: "approval_reason",
                        fieldtype: "Small Text",
                        label: __("Approval Reason"),
                        reqd: 1
                    }
                ];

            const title = direct
                ? __(
                    "Approve as {0}",
                    [r.message.current_user]
                )
                : __("Owner/Admin Approval");

            frappe.prompt(
                fields,
                values => {
                    submit_recovery_approval(
                        frm,
                        values
                    );
                },
                title,
                __("Approve")
            );
        }
    });
}



frappe.ui.form.on("NKT Stock Recovery", {
    setup(frm) {
        frm.set_query("base_saleable_item", () => ({
            filters: {is_stock_item:1, disabled:0, nkt_stock_form:"Saleable Sack"}
        }));
        ["source_warehouse","target_warehouse"].forEach(field => {
            frm.set_query(field, () => {
                const filters = {is_group:0, disabled:0};
                if (frm.doc.company) filters.company = frm.doc.company;
                return {filters};
            });
        });
        frm.set_query("disposal_expense_account", () => ({
            filters: {company:frm.doc.company, is_group:0, root_type:"Expense"}
        }));
    },
    refresh(frm) {
        visibility(frm);
        calculate(frm);
        load_pack_capacity(frm);

        if (is_conversion(frm) && frm.doc.docstatus === 0) {
            frm.set_intro(
                __("Operational stock conversion — no Owner/Admin pre-approval required. Submit records the stock movement immediately."),
                "blue"
            );
        } else if (
            frm.doc.docstatus === 0 && !frm.is_new()
            && frm.doc.approval_status !== "Approved"
        ) {
            frm.add_custom_button(__("Owner/Admin Approve"), () => approve(frm), __("Actions"));
            frm.set_intro(__("Owner/Admin approval is required before disposal."), "orange");
        } else if (frm.doc.approval_status === "Approved") {
            frm.set_intro(__("Approved by {0}.", [frm.doc.approved_by]), "green");
        }
        if (frm.doc.docstatus === 1 && frm.doc.stock_entry) {
            frm.add_custom_button(__("Open Stock Entry"), () => {
                frappe.set_route("Form", "Stock Entry", frm.doc.stock_entry);
            }, __("View"));
        }
    },
    recovery_type(frm) {
        reset_approval(frm);
        clear_qty(frm);
        visibility(frm);
        if (frm.doc.recovery_type === T.pack && frm.doc.source_warehouse) {
            frm.set_value("target_warehouse", frm.doc.source_warehouse);
        }
        load_pack_capacity(frm);
    },
    source_warehouse(frm) {
        reset_approval(frm);
        if (frm.doc.source_warehouse && !is_disposal(frm)) {
            frm.set_value("target_warehouse", frm.doc.source_warehouse);
        }
        load_pack_capacity(frm);
    },
    target_warehouse(frm) { reset_approval(frm); },
    base_saleable_item(frm) { reset_approval(frm); load_mapping(frm); load_pack_capacity(frm); },
    company(frm) { reset_approval(frm); },
    disposal_expense_account(frm) { reset_approval(frm); },
    reason(frm) { reset_approval(frm); },
    remarks(frm) { reset_approval(frm); },
    recovery_datetime(frm) { reset_approval(frm); },
    validate(frm) {
        calculate(frm);
        if (!check_pack_max(frm, true)) frappe.validated = false;
    }
});

["damaged_sacks_consumed","measured_damaged_contents_kg","fraction_kg_consumed",
 "saleable_sacks_produced","fraction_kg_produced"].forEach(field => {
    frappe.ui.form.on("NKT Stock Recovery", field, frm => {
        reset_approval(frm);
        calculate(frm);
        if (field === "saleable_sacks_produced") check_pack_max(frm, true);
    });
});
