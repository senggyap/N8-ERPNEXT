
import frappe
from frappe import _
from frappe.utils import flt

MARKER = "NKT-C5.2-CUSTOMER-ADVANCE-ORDER-UI"
SCRIPT_NAME = "NKT C5.2 Customer Advance Order UI"
ORDER_DOCTYPE = "NKT Customer Order"
CORE = "nkt_operations.nkt_store_operations.features.payments_accounts.collection"

CLIENT_SCRIPT = r"""
// NKT-C5.2-CUSTOMER-ADVANCE-ORDER-UI
(() => {
    const METHOD_ROOT =
        "nkt_operations.nkt_store_operations.features.payments_accounts.internal.advance_ui";

    const permitted = () => {
        const roles = frappe.user_roles || [];
        return (
            frappe.session.user === "Administrator" ||
            roles.includes("System Manager") ||
            roles.includes("NKT Encoder")
        );
    };

    const money = (value) => format_currency(flt(value || 0));

    const open_advance_dialog = (frm, context) => {
        const available = flt(context.available_advance || 0);
        const due = flt(context.amount_due || 0);
        const maximum = Math.min(available, due);

        const dialog = new frappe.ui.Dialog({
            title: __("Apply Customer Advance"),
            fields: [
                {
                    fieldtype: "HTML",
                    fieldname: "summary",
                    options:
                        `<div class="mb-3">
                            <div><b>${__("Customer")}</b>: ${frappe.utils.escape_html(context.customer || "")}</div>
                            <div><b>${__("Order Due")}</b>: ${money(due)}</div>
                            <div><b>${__("Available Advance")}</b>: ${money(available)}</div>
                        </div>`
                },
                {
                    fieldtype: "Currency",
                    fieldname: "amount",
                    label: __("Amount to Apply"),
                    reqd: 1,
                    default: maximum
                },
                {
                    fieldtype: "Small Text",
                    fieldname: "remarks",
                    label: __("Remarks"),
                    default: "Applied available customer advance to this order."
                }
            ],
            primary_action_label: __("Apply Advance"),
            primary_action: async (values) => {
                const amount = flt(values.amount || 0);

                if (amount <= 0) {
                    frappe.msgprint(__("Amount must be greater than zero."));
                    return;
                }

                if (amount > maximum + 0.005) {
                    frappe.msgprint(
                        __("Amount cannot exceed the order due or available customer advance.")
                    );
                    return;
                }

                dialog.disable_primary_action();

                try {
                    const response = await frappe.call({
                        method: `${METHOD_ROOT}.apply_order_advance`,
                        type: "POST",
                        args: {
                            customer_order: frm.doc.name,
                            amount,
                            remarks: values.remarks || ""
                        },
                        freeze: true,
                        freeze_message: __("Applying customer advance...")
                    });

                    const result = response.message || {};
                    dialog.hide();

                    frappe.show_alert({
                        message: __(
                            "Applied {0}. Remaining order due: {1}. Remaining customer advance: {2}.",
                            [
                                money(result.applied_amount),
                                money(result.remaining_order_due),
                                money(result.available_advance_after)
                            ]
                        ),
                        indicator: "green"
                    });

                    await frm.reload_doc();
                } finally {
                    dialog.enable_primary_action();
                }
            }
        });

        dialog.show();
    };

    frappe.ui.form.on("NKT Customer Order", {
        refresh: async (frm) => {
            frm.remove_custom_button(
                __("Apply Customer Advance"),
                __("Payment")
            );

            if (
                !permitted() ||
                frm.is_new() ||
                cint(frm.doc.docstatus) !== 1 ||
                flt(frm.doc.amount_due || 0) <= 0.005
            ) {
                return;
            }

            try {
                const response = await frappe.call({
                    method: `${METHOD_ROOT}.get_order_advance_context`,
                    args: { customer_order: frm.doc.name },
                    silent: true
                });

                const context = response.message || {};
                if (flt(context.available_advance || 0) <= 0.005) {
                    return;
                }

                frm.add_custom_button(
                    __("Apply Customer Advance"),
                    () => open_advance_dialog(frm, context),
                    __("Payment")
                );
            } catch (error) {
                console.debug("NKT customer advance context unavailable", error);
            }
        }
    });
})();
"""


def _require_encoder_authority():
    user = frappe.session.user
    if user == "Administrator":
        return

    roles = set(frappe.get_roles(user))
    if {"NKT Encoder", "System Manager"} & roles:
        return

    frappe.throw(
        _("Only the Encoder or System Manager may apply customer advance to an order."),
        frappe.PermissionError,
    )


@frappe.whitelist()
def get_order_advance_context(customer_order):
    _require_encoder_authority()

    order = frappe.db.get_value(
        ORDER_DOCTYPE,
        customer_order,
        [
            "name",
            "company",
            "customer",
            "customer_name",
            "grand_total",
            "amount_paid",
            "amount_due",
            "payment_status",
            "status",
            "docstatus",
        ],
        as_dict=True,
    )

    if not order:
        frappe.throw(_("Customer Order {0} was not found.").format(customer_order))

    if int(order.docstatus or 0) != 1:
        return {
            **order,
            "available_advance": 0,
            "can_apply": False,
        }

    advance_info = frappe.get_attr(
        CORE + ".get_customer_advance_balance"
    )(
        order.customer,
        order.company,
    )

    available = flt((advance_info or {}).get("available_advance"))

    return {
        **order,
        "available_advance": available,
        "can_apply": (
            flt(order.amount_due) > 0.005
            and available > 0.005
        ),
    }


@frappe.whitelist(methods=["POST"])
def apply_order_advance(customer_order, amount, remarks=None):
    _require_encoder_authority()

    return frappe.get_attr(
        CORE + ".apply_customer_advance_to_order"
    )(
        customer_order=customer_order,
        amount=amount,
        remarks=remarks,
    )


def _find_existing_client_script():
    if frappe.db.exists("Client Script", SCRIPT_NAME):
        return SCRIPT_NAME

    rows = frappe.get_all(
        "Client Script",
        filters={"dt": ORDER_DOCTYPE},
        fields=["name", "script"],
        limit_page_length=100,
    )
    for row in rows:
        if MARKER in (row.script or ""):
            return row.name

    return None


def install():
    existing = _find_existing_client_script()

    if existing:
        doc = frappe.get_doc("Client Script", existing)
    else:
        doc = frappe.new_doc("Client Script")
        doc.name = SCRIPT_NAME

    meta = frappe.get_meta("Client Script")

    if meta.has_field("dt"):
        doc.dt = ORDER_DOCTYPE

    if meta.has_field("view"):
        doc.view = "Form"

    if meta.has_field("script_type"):
        options = (meta.get_field("script_type").options or "").splitlines()
        if "Form" in options:
            doc.script_type = "Form"

    if meta.has_field("enabled"):
        doc.enabled = 1

    if meta.has_field("module"):
        doc.module = "NKT Store Operations"

    doc.script = CLIENT_SCRIPT
    doc.flags.ignore_permissions = True

    if doc.is_new():
        doc.insert(ignore_permissions=True)
    else:
        doc.save(ignore_permissions=True)

    frappe.db.commit()
    frappe.clear_cache(doctype=ORDER_DOCTYPE)

    return {
        "installed": True,
        "client_script": doc.name,
        "marker": MARKER,
        "doctype": ORDER_DOCTYPE,
    }


def verify():
    existing = _find_existing_client_script()

    row = None
    if existing:
        row = frappe.db.get_value(
            "Client Script",
            existing,
            ["name", "dt", "enabled", "script"],
            as_dict=True,
        )

    return {
        "client_script_installed": bool(existing),
        "client_script": existing,
        "doctype": row.dt if row else None,
        "enabled": int(row.enabled or 0) if row and hasattr(row, "enabled") else None,
        "marker_present": bool(row and MARKER in (row.script or "")),
        "backend_apply_method": bool(
            frappe.get_attr(CORE + ".apply_customer_advance_to_order")
        ),
        "passed": bool(
            row
            and row.dt == ORDER_DOCTYPE
            and MARKER in (row.script or "")
        ),
    }
