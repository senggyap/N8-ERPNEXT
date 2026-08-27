// NKT_C15F_R8A_SUPPORT_RECONCILIATION_GUARD
function nkt_r8a_can_retry_reconciliation() {
    const roles = frappe.user_roles || [];
    return frappe.session.user === "Administrator"
        || ["System Manager", "NKT OWNER", "NKT ADMINISTRATOR"].some((role) => roles.includes(role));
}

function nkt_cashier_calculate_sale(frm) {
    let total_quantity = 0;
    let grand_total = 0;
    (frm.doc.items || []).forEach((row) => {
        row.quantity = flt(row.quantity);
        row.standard_rate = flt(row.standard_rate);
        row.price_adjustment = flt(row.price_adjustment);
        row.final_rate = row.standard_rate + row.price_adjustment;
        row.amount = row.quantity * row.final_rate;
        total_quantity += row.quantity;
        grand_total += row.amount;
    });
    frm.set_value("total_quantity", total_quantity);
    frm.set_value("grand_total", grand_total);
    frm.refresh_field("items");
}

function nkt_cashier_calculate_payments(frm) {
    let total = 0, cash = 0, non_cash = 0, account = 0;
    (frm.doc.payments || []).forEach((row) => {
        const amount = flt(row.amount);
        total += amount;
        if (row.payment_method === "Cash") {
            row.affects_cash_drawer = 1;
            if (flt(row.cash_tendered) <= 0 && amount > 0) {
                row.cash_tendered = amount;
            }
            row.change_amount = Math.max(flt(row.cash_tendered) - amount, 0);
            cash += amount;
        } else {
            row.affects_cash_drawer = 0;
            row.cash_tendered = 0;
            row.change_amount = 0;
            if (row.payment_method === "Account") account += amount;
            else non_cash += amount;
        }
    });
    frm.set_value("total_payment", total);
    frm.set_value("total_cash", cash);
    frm.set_value("total_non_cash", non_cash);
    frm.set_value("total_account_charge", account);
    frm.refresh_field("payments");
}

function nkt_apply_default_warehouse(frm) {
    (frm.doc.items || []).forEach((row) => {
        if (!row.source_warehouse && frm.doc.default_warehouse) {
            frappe.model.set_value(row.doctype, row.name, "source_warehouse", frm.doc.default_warehouse);
        }
    });
}

async function nkt_load_shift_context(frm) {
    if (!frm.is_new()) return;

    const r = await frappe.call({
        method: "nkt_operations.nkt_store_operations.doctype.nkt_cashier_sale.nkt_cashier_sale.get_active_cashier_context",
        args: {company: frm.doc.company || null}
    });

    const context = r.message || {};
    if (!context.cashier_shift) return;

    await frm.set_value("company", context.company);
    await frm.set_value("cashier", frappe.session.user);
    await frm.set_value("sale_datetime", frappe.datetime.now_datetime());
    await frm.set_value("business_date", context.business_date || frappe.datetime.get_today());
    await frm.set_value("cashier_shift", context.cashier_shift);
    await frm.set_value("settlement_location", context.settlement_location);
    await frm.set_value("default_warehouse", context.default_warehouse || context.settlement_location);
    nkt_apply_default_warehouse(frm);
}

async function nkt_cashier_load_item(frm, cdt, cdn) {
    const row = locals[cdt][cdn];
    if (!row.item) return;

    const r = await frappe.call({
        method: "nkt_operations.nkt_store_operations.doctype.nkt_cashier_sale.nkt_cashier_sale.get_cashier_item_context",
        args: {item_code: row.item}
    });
    const item = r.message || {};

    await frappe.model.set_value(cdt, cdn, "item_name", item.item_name || "");
    await frappe.model.set_value(cdt, cdn, "uom", item.stock_uom || "");
    await frappe.model.set_value(cdt, cdn, "standard_rate", flt(item.standard_rate || 0));

    if (!row.source_warehouse && frm.doc.default_warehouse) {
        await frappe.model.set_value(cdt, cdn, "source_warehouse", frm.doc.default_warehouse);
    }

    nkt_cashier_calculate_sale(frm);
}

frappe.ui.form.on("NKT Cashier Sale", {
    setup(frm) {
        const warehouse_query = () => ({filters: {company: frm.doc.company, is_group: 0}});
        frm.set_query("item", "items", () => ({filters: {
            disabled: 0,
            is_stock_item: 1,
            is_sales_item: 1,
            nkt_stock_form: "Saleable Sack"
        }}));
        frm.set_query("default_warehouse", warehouse_query);
        frm.set_query("source_warehouse", "items", warehouse_query);
    },
    refresh(frm) {
        if (frm.is_new()) {
            nkt_load_shift_context(frm);
        }
        nkt_cashier_calculate_sale(frm);
        nkt_cashier_calculate_payments(frm);
        if (frm.doc.docstatus === 1) {
            frm.add_custom_button(__("Print Cashier Receipt"), () => frm.print_doc(), __("Print"));
        }
        if (nkt_r8a_can_retry_reconciliation() && frm.doc.docstatus === 1 && ["Unmatched", "Ambiguous"].includes(frm.doc.reconciliation_status)) {
            frm.add_custom_button(__("Retry Reconciliation"), () => {
                frappe.call({
                    method: "nkt_operations.nkt_store_operations.features.sales.matching.retry_match",
                    type: "POST",
                    args: {cashier_sale: frm.doc.name},
                    freeze: true,
                    callback: () => frm.reload_doc()
                });
            });
        }
    },
    company(frm) { nkt_load_shift_context(frm); },
    default_warehouse(frm) { nkt_apply_default_warehouse(frm); },
    customer(frm) {
        if (!frm.doc.customer) return;
        frappe.db.get_value("Customer", frm.doc.customer, "customer_name").then((r) => {
            frm.set_value("customer_name", r?.message?.customer_name || "");
        });
    },
    validate(frm) {
        nkt_cashier_calculate_sale(frm);
        nkt_cashier_calculate_payments(frm);
    },
    items_remove(frm) { nkt_cashier_calculate_sale(frm); },
    payments_remove(frm) { nkt_cashier_calculate_payments(frm); }
});

frappe.ui.form.on("NKT Cashier Sale Item", {
    item(frm, cdt, cdn) { nkt_cashier_load_item(frm, cdt, cdn); },
    quantity(frm) { nkt_cashier_calculate_sale(frm); },
    price_adjustment(frm) { nkt_cashier_calculate_sale(frm); },
    items_add(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (frm.doc.default_warehouse && !row.source_warehouse) {
            frappe.model.set_value(cdt, cdn, "source_warehouse", frm.doc.default_warehouse);
        }
        nkt_cashier_calculate_sale(frm);
    }
});

frappe.ui.form.on("NKT Payment Detail", {
    payment_method(frm) { nkt_cashier_calculate_payments(frm); },
    amount(frm) { nkt_cashier_calculate_payments(frm); },
    cash_tendered(frm) { nkt_cashier_calculate_payments(frm); }
});
