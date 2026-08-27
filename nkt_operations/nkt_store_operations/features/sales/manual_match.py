from __future__ import annotations

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import now_datetime
from frappe.utils.password import check_password

from nkt_operations.nkt_store_operations.features.sales.matching import (
    MATCHED_STATUSES,
    _complete_match,
    basket_summary_text,
    payment_summary_text,
)


DIRECT_AUTHORITY_ROLES = {"NKT OWNER", "NKT ADMINISTRATOR"}
CLIENT_SCRIPT_NAME = "NKT Customer Order Manual Match Resolution V1.3"


def _client_script() -> str:
    return r'''
(function () {
    const METHOD_ROOT = "nkt_operations.nkt_store_operations.features.sales.manual_match";

    const esc = (value) => {
        const text = value === null || value === undefined ? "" : String(value);
        return frappe.utils && frappe.utils.escape_html
            ? frappe.utils.escape_html(text)
            : text.replace(/[&<>\"']/g, (char) => ({
                "&": "&amp;", "<": "&lt;", ">": "&gt;",
                "\"": "&quot;", "'": "&#039;"
            }[char]));
    };

    function candidate_table(candidates) {
        const rows = candidates.map((candidate) => `
            <tr>
                <td><strong>${esc(candidate.name)}</strong></td>
                <td>${esc(candidate.sale_datetime || candidate.creation)}</td>
                <td>${esc(candidate.cashier)}</td>
                <td>${esc(candidate.cashier_shift)}</td>
                <td>${esc(candidate.linked_payment_receipt)}</td>
                <td style="text-align:right">${format_currency(candidate.grand_total || 0)}</td>
                <td>${esc(candidate.basket_summary)}</td>
                <td>${esc(candidate.payment_summary)}</td>
            </tr>
        `).join("");

        return `
            <div style="overflow-x:auto; max-height:320px;">
                <table class="table table-bordered table-sm">
                    <thead>
                        <tr>
                            <th>Cashier Sale</th>
                            <th>Sale Time</th>
                            <th>Cashier</th>
                            <th>Shift</th>
                            <th>Payment Receipt</th>
                            <th>Total</th>
                            <th>Basket</th>
                            <th>Payment</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
            <p class="text-muted small">
                Compare these records with the retained handwritten slips. The system will link only the selected Cashier Sale and its existing Payment Receipt. No stock or cashier movement will be created.
            </p>
        `;
    }

    function open_manual_match_dialog(frm) {
        frappe.call({
            method: `${METHOD_ROOT}.get_manual_match_candidates`,
            type: "POST",
            args: { customer_order: frm.doc.name },
            freeze: true,
            freeze_message: __("Loading exact cashier candidates...")
        }).then((response) => {
            const payload = response.message || {};
            const candidates = payload.candidates || [];
            if (!candidates.length) {
                frappe.msgprint(__("No exact same-customer cashier candidates are currently available."));
                return;
            }

            const dialog = new frappe.ui.Dialog({
                title: __("Resolve Ambiguous Cashier Match"),
                size: "extra-large",
                fields: [
                    {
                        fieldname: "candidate_details",
                        fieldtype: "HTML",
                        options: candidate_table(candidates)
                    },
                    {
                        fieldname: "cashier_sale",
                        fieldtype: "Select",
                        label: __("Confirmed Cashier Sale"),
                        options: candidates.map((candidate) => candidate.name).join("\n"),
                        reqd: 1
                    },
                    {
                        fieldname: "reason",
                        fieldtype: "Small Text",
                        label: __("Resolution Reason / Handwritten Slip Check"),
                        reqd: 1,
                        description: __("State how the retained handwritten slip established the correct pair.")
                    },
                    { fieldtype: "Section Break", label: __("Authority") },
                    {
                        fieldname: "authorized_user",
                        fieldtype: "Link",
                        options: "User",
                        label: __("Authorized User"),
                        description: __("Leave blank when the logged-in user is Administrator, NKT OWNER, or NKT ADMINISTRATOR.")
                    },
                    {
                        fieldname: "authorized_password",
                        fieldtype: "Password",
                        label: __("Authorized User Password"),
                        description: __("Required only when a lower-authority user requests the resolution.")
                    }
                ],
                primary_action_label: __("Link Selected Pair"),
                primary_action(values) {
                    frappe.call({
                        method: `${METHOD_ROOT}.resolve_ambiguous_match`,
                        type: "POST",
                        args: {
                            customer_order: frm.doc.name,
                            cashier_sale: values.cashier_sale,
                            reason: values.reason,
                            authorized_user: values.authorized_user || null,
                            authorized_password: values.authorized_password || null
                        },
                        freeze: true,
                        freeze_message: __("Linking the selected cashier and encoder records...")
                    }).then((result) => {
                        dialog.hide();
                        const data = result.message || {};
                        frappe.msgprint({
                            title: __("Ambiguous Match Resolved"),
                            indicator: "green",
                            message: __("Customer Order {0} was linked to Cashier Sale {1} using Payment Receipt {2}.", [
                                data.customer_order || frm.doc.name,
                                data.cashier_sale || values.cashier_sale,
                                data.payment_receipt || ""
                            ])
                        });
                        frm.reload_doc();
                    });
                }
            });
            dialog.show();
        });
    }

    frappe.ui.form.on("NKT Customer Order", {
        refresh(frm) {
            if (
                frm.doc.docstatus === 1
                && frm.doc.cashier_reconciliation_status === "Ambiguous"
                && !frm.doc.matched_cashier_sale
            ) {
                frm.add_custom_button(
                    __("Resolve Ambiguous Match"),
                    () => open_manual_match_dialog(frm),
                    __("Reconciliation")
                );
            }
        }
    });
})();
'''


def install_schema():
    custom_fields = {
        "NKT Customer Order": [
            {
                "fieldname": "custom_nkt_manual_match_section",
                "label": "Manual Match Resolution Audit",
                "fieldtype": "Section Break",
                "insert_after": "cashier_reconciled_on",
                "collapsible": 1,
            },
            {
                "fieldname": "custom_nkt_match_resolution_status",
                "label": "Manual Match Resolution Status",
                "fieldtype": "Data",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_manual_match_section",
            },
            {
                "fieldname": "custom_nkt_match_requested_by",
                "label": "Resolution Requested By",
                "fieldtype": "Link",
                "options": "User",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_match_resolution_status",
            },
            {
                "fieldname": "custom_nkt_match_resolved_by",
                "label": "Resolution Authorized By",
                "fieldtype": "Link",
                "options": "User",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_match_requested_by",
            },
            {
                "fieldname": "custom_nkt_match_resolved_on",
                "label": "Resolution Date and Time",
                "fieldtype": "Datetime",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_match_resolved_by",
            },
            {
                "fieldname": "custom_nkt_match_resolution_reason",
                "label": "Resolution Reason",
                "fieldtype": "Small Text",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_match_resolved_on",
            },
        ],
        "NKT Cashier Sale": [
            {
                "fieldname": "custom_nkt_manual_match_section",
                "label": "Manual Match Resolution Audit",
                "fieldtype": "Section Break",
                "insert_after": "reconciled_on",
                "collapsible": 1,
            },
            {
                "fieldname": "custom_nkt_match_resolution_status",
                "label": "Manual Match Resolution Status",
                "fieldtype": "Data",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_manual_match_section",
            },
            {
                "fieldname": "custom_nkt_match_requested_by",
                "label": "Resolution Requested By",
                "fieldtype": "Link",
                "options": "User",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_match_resolution_status",
            },
            {
                "fieldname": "custom_nkt_match_resolved_by",
                "label": "Resolution Authorized By",
                "fieldtype": "Link",
                "options": "User",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_match_requested_by",
            },
            {
                "fieldname": "custom_nkt_match_resolved_on",
                "label": "Resolution Date and Time",
                "fieldtype": "Datetime",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_match_resolved_by",
            },
            {
                "fieldname": "custom_nkt_match_resolution_reason",
                "label": "Resolution Reason",
                "fieldtype": "Small Text",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_match_resolved_on",
            },
        ],
    }
    create_custom_fields(custom_fields, ignore_validate=True, update=True)
    _install_client_script()
    frappe.clear_cache(doctype="NKT Customer Order")
    frappe.clear_cache(doctype="NKT Cashier Sale")
    return {"installed": True, "client_script": CLIENT_SCRIPT_NAME}


def _install_client_script():
    existing = frappe.db.exists("Client Script", CLIENT_SCRIPT_NAME)
    values = {
        "dt": "NKT Customer Order",
        "view": "Form",
        "enabled": 1,
        "script": _client_script(),
    }
    if existing:
        doc = frappe.get_doc("Client Script", CLIENT_SCRIPT_NAME)
        doc.update(values)
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc(
            {
                "doctype": "Client Script",
                "name": CLIENT_SCRIPT_NAME,
                **values,
            }
        )
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)


def _is_direct_authority(user: str) -> bool:
    if user == "Administrator":
        return True
    return bool(DIRECT_AUTHORITY_ROLES.intersection(frappe.get_roles(user)))


def _get_authorizing_user(authorized_user=None, authorized_password=None) -> str:
    requester = frappe.session.user
    if _is_direct_authority(requester):
        return requester

    authorized_user = (authorized_user or "").strip()
    authorized_password = authorized_password or ""
    if not authorized_user or not authorized_password:
        frappe.throw(
            _(
                "This user cannot resolve an ambiguous match directly. Enter "
                "Administrator, NKT OWNER, or NKT ADMINISTRATOR credentials."
            )
        )

    enabled = frappe.db.get_value("User", authorized_user, "enabled")
    if not enabled:
        frappe.throw(_("Authorized User {0} is disabled or does not exist.").format(authorized_user))
    if not _is_direct_authority(authorized_user):
        frappe.throw(
            _(
                "User {0} does not have NKT OWNER or NKT ADMINISTRATOR authority."
            ).format(authorized_user)
        )
    return check_password(authorized_user, authorized_password)


def _exact_candidate_filters(order):
    return {
        "docstatus": 1,
        "company": order.company,
        "business_date": order.order_date,
        "customer": order.customer,
        "nkt_basket_fingerprint": order.nkt_basket_fingerprint,
        "nkt_payment_fingerprint": order.nkt_payment_fingerprint,
        "reconciliation_status": ["in", ["Unmatched", "Ambiguous", ""]],
    }


def _candidate_rows(order):
    rows = frappe.get_all(
        "NKT Cashier Sale",
        filters=_exact_candidate_filters(order),
        fields=[
            "name",
            "sale_datetime",
            "creation",
            "cashier",
            "cashier_shift",
            "customer",
            "customer_name",
            "grand_total",
            "linked_payment_receipt",
            "reconciliation_status",
            "matched_customer_order",
        ],
        order_by="sale_datetime asc, creation asc",
    )
    result = []
    for row in rows:
        sale = frappe.get_doc("NKT Cashier Sale", row.name)
        data = dict(row)
        data["basket_summary"] = basket_summary_text(sale.get("items") or [])
        data["payment_summary"] = payment_summary_text(sale.get("payments") or [])
        result.append(data)
    return result


@frappe.whitelist()
def get_manual_match_candidates(customer_order):
    order = frappe.get_doc("NKT Customer Order", customer_order)
    order.check_permission("read")
    if order.docstatus != 1:
        frappe.throw(_("The Customer Order must be submitted."))
    if order.matched_cashier_sale or order.cashier_reconciliation_status in MATCHED_STATUSES:
        frappe.throw(_("Customer Order {0} is already reconciled.").format(order.name))
    return {
        "customer_order": order.name,
        "customer": order.customer,
        "customer_name": order.customer_name,
        "grand_total": order.grand_total,
        "candidates": _candidate_rows(order),
    }


def _validate_selected_pair(order, sale):
    if order.docstatus != 1 or sale.docstatus != 1:
        frappe.throw(_("Both the Customer Order and Cashier Sale must be submitted."))
    if order.matched_cashier_sale or order.cashier_reconciliation_status in MATCHED_STATUSES:
        frappe.throw(_("Customer Order {0} is already reconciled.").format(order.name))
    if sale.matched_customer_order or sale.reconciliation_status in MATCHED_STATUSES:
        frappe.throw(_("Cashier Sale {0} is already reconciled.").format(sale.name))

    checks = [
        (order.company == sale.company, _("Company differs.")),
        (str(order.order_date) == str(sale.business_date), _("Business date differs.")),
        (order.customer == sale.customer, _("Customer record differs.")),
        (
            order.nkt_basket_fingerprint == sale.nkt_basket_fingerprint,
            _("Item basket differs."),
        ),
        (
            order.nkt_payment_fingerprint == sale.nkt_payment_fingerprint,
            _("Payment basket differs."),
        ),
    ]
    reasons = [message for passed, message in checks if not passed]
    if reasons:
        frappe.throw(
            _("The selected records are not an exact permitted pair: {0}").format(" ".join(reasons))
        )

    receipt_name = sale.linked_payment_receipt
    if receipt_name:
        allocated_order = frappe.db.get_value(
            "NKT Payment Receipt", receipt_name, "customer_order"
        )
        if allocated_order and allocated_order != order.name:
            frappe.throw(
                _("Payment Receipt {0} is already allocated to {1}.").format(
                    receipt_name, allocated_order
                )
            )


@frappe.whitelist(methods=["POST"])
def resolve_ambiguous_match(
    customer_order,
    cashier_sale,
    reason,
    authorized_user=None,
    authorized_password=None,
):
    reason = " ".join((reason or "").strip().split())
    if len(reason) < 10:
        frappe.throw(_("Enter a specific resolution reason of at least 10 characters."))

    requester = frappe.session.user
    resolver = _get_authorizing_user(authorized_user, authorized_password)
    order = frappe.get_doc("NKT Customer Order", customer_order)
    sale = frappe.get_doc("NKT Cashier Sale", cashier_sale)
    order.check_permission("read")
    _validate_selected_pair(order, sale)

    candidate_names = [row["name"] for row in _candidate_rows(order)]
    if sale.name not in candidate_names:
        frappe.throw(_("Cashier Sale {0} is no longer an available exact candidate.").format(sale.name))

    resolved_on = now_datetime()
    _complete_match(sale.name, order.name)

    audit_values = {
        "custom_nkt_match_resolution_status": "Resolved Manually",
        "custom_nkt_match_requested_by": requester,
        "custom_nkt_match_resolved_by": resolver,
        "custom_nkt_match_resolved_on": resolved_on,
        "custom_nkt_match_resolution_reason": reason,
    }
    frappe.db.set_value(
        "NKT Customer Order", order.name, audit_values, update_modified=False
    )
    frappe.db.set_value(
        "NKT Cashier Sale", sale.name, audit_values, update_modified=False
    )

    unselected = []
    for name in candidate_names:
        if name == sale.name:
            continue
        current = frappe.db.get_value(
            "NKT Cashier Sale",
            name,
            ["reconciliation_status", "matched_customer_order"],
            as_dict=True,
        )
        if not current or current.matched_customer_order:
            continue
        if current.reconciliation_status in MATCHED_STATUSES:
            continue
        warning = _(
            "Not selected during manual resolution of Customer Order {0}. "
            "This Cashier Sale remains available for its correct encoder order."
        ).format(order.name)
        frappe.db.set_value(
            "NKT Cashier Sale",
            name,
            {
                "status": "Submitted - Unmatched",
                "reconciliation_status": "Unmatched",
                "reconciliation_warning": warning,
            },
            update_modified=False,
        )
        frappe.get_doc("NKT Cashier Sale", name).add_comment("Info", warning)
        unselected.append(name)

    audit_message = _(
        "Ambiguous cashier match manually resolved. Selected Cashier Sale {0}. "
        "Requested by {1}; authorized by {2}. Reason: {3}"
    ).format(sale.name, requester, resolver, reason)
    frappe.get_doc("NKT Customer Order", order.name).add_comment("Info", audit_message)
    frappe.get_doc("NKT Cashier Sale", sale.name).add_comment("Info", audit_message)

    receipt_name = frappe.db.get_value(
        "NKT Cashier Sale", sale.name, "linked_payment_receipt"
    )
    return {
        "customer_order": order.name,
        "cashier_sale": sale.name,
        "payment_receipt": receipt_name,
        "unselected_cashier_sales": unselected,
        "requested_by": requester,
        "authorized_by": resolver,
        "resolved_on": resolved_on,
    }
