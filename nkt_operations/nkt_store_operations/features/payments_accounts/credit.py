from __future__ import annotations

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import add_days, flt, getdate, now_datetime, today
from frappe.utils.password import check_password


TOLERANCE = 0.005
RECEIVABLE_DOCTYPE = "NKT Customer Receivable"
CLIENT_SCRIPT_NAME = "NKT Account Credit Control V1.4"
CREDIT_CONTROLLER_ROLE = "NKT Credit Controller"
DIRECT_AUTHORITY_ROLES = {
    "NKT OWNER",
    "NKT ADMINISTRATOR",
    CREDIT_CONTROLLER_ROLE,
}


def install_schema():
    _ensure_credit_controller_role()
    _ensure_receivable_doctype()
    _ensure_receivable_v16_fields()

    custom_fields = {
        "Customer": [
            {
                "fieldname": "custom_nkt_account_credit_section",
                "label": "NKT Account Credit",
                "fieldtype": "Section Break",
                "insert_after": "customer_group",
                "collapsible": 1,
            },
            {
                "fieldname": "custom_nkt_allow_account_sales",
                "label": "Allow Account Sales",
                "fieldtype": "Check",
                "default": "0",
                "insert_after": "custom_nkt_account_credit_section",
                "description": (
                    "Only approved customers may use Account as a sale payment method."
                ),
            },
            {
                "fieldname": "custom_nkt_credit_status",
                "label": "Credit Status",
                "fieldtype": "Select",
                "options": "\nActive\nOn Hold\nSuspended",
                "default": "On Hold",
                "insert_after": "custom_nkt_allow_account_sales",
            },
            {
                "fieldname": "custom_nkt_credit_limit",
                "label": "Credit Limit",
                "fieldtype": "Currency",
                "default": "0",
                "insert_after": "custom_nkt_credit_status",
            },
            {
                "fieldname": "custom_nkt_credit_terms_days",
                "label": "Credit Terms (Days)",
                "fieldtype": "Int",
                "default": "0",
                "insert_after": "custom_nkt_credit_limit",
            },
            {
                "fieldname": "custom_nkt_require_manual_account_approval",
                "label": "Require Manual Approval for Every Account Sale",
                "fieldtype": "Check",
                "default": "0",
                "insert_after": "custom_nkt_credit_terms_days",
                "description": "Enable only for customers whose every account sale must be reviewed by credit control.",
            },
            {
                "fieldname": "custom_nkt_auto_approval_limit",
                "label": "Automatic Approval Limit per Order",
                "fieldtype": "Currency",
                "default": "0",
                "insert_after": "custom_nkt_require_manual_account_approval",
                "description": "Zero means there is no separate per-order cap. The overall customer credit limit still applies.",
            },
            {
                "fieldname": "custom_nkt_current_account_balance",
                "label": "Current Account Balance",
                "fieldtype": "Currency",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_auto_approval_limit",
            },
            {
                "fieldname": "custom_nkt_available_credit",
                "label": "Available Credit",
                "fieldtype": "Currency",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_current_account_balance",
            },
        ],
        "NKT Customer Order": [
            {
                "fieldname": "custom_nkt_account_control_section",
                "label": "Account Credit Control",
                "fieldtype": "Section Break",
                "insert_after": "amount_due",
                "collapsible": 1,
            },
            {
                "fieldname": "custom_nkt_customer_receivable",
                "label": "Customer Receivable",
                "fieldtype": "Link",
                "options": RECEIVABLE_DOCTYPE,
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_account_control_section",
            },
            {
                "fieldname": "custom_nkt_account_credit_status",
                "label": "Account Credit Status",
                "fieldtype": "Select",
                "options": "\nNot Required\nPending Approval\nApproved\nRejected\nCancelled",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_customer_receivable",
            },
            {
                "fieldname": "custom_nkt_account_approval_mode",
                "label": "Approval Mode",
                "fieldtype": "Select",
                "options": "\nNot Required\nAutomatic\nManual",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_account_credit_status",
            },
            {
                "fieldname": "custom_nkt_account_review_reason",
                "label": "Why Manual Review Is Required",
                "fieldtype": "Small Text",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_account_approval_mode",
            },
            {
                "fieldname": "custom_nkt_account_due_date",
                "label": "Account Due Date",
                "fieldtype": "Date",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_account_review_reason",
            },
            {
                "fieldname": "custom_nkt_account_approved_by",
                "label": "Account Approved By",
                "fieldtype": "Link",
                "options": "User",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_account_due_date",
            },
            {
                "fieldname": "custom_nkt_account_approved_on",
                "label": "Account Approved On",
                "fieldtype": "Datetime",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_account_approved_by",
            },
            {
                "fieldname": "custom_nkt_account_approval_reason",
                "label": "Account Approval Reason",
                "fieldtype": "Small Text",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_nkt_account_approved_on",
            },
        ],
    }
    create_custom_fields(custom_fields, ignore_validate=True, update=True)
    _install_client_script()
    _backfill_customer_credit_totals()
    _reevaluate_pending_account_sales()
    frappe.clear_cache(doctype="Customer")
    frappe.clear_cache(doctype="NKT Customer Order")
    frappe.clear_cache(doctype=RECEIVABLE_DOCTYPE)
    return {
        "installed": True,
        "receivable_doctype": RECEIVABLE_DOCTYPE,
        "client_script": CLIENT_SCRIPT_NAME,
    }


def _ensure_credit_controller_role():
    if frappe.db.exists("Role", CREDIT_CONTROLLER_ROLE):
        return
    role = frappe.get_doc(
        {
            "doctype": "Role",
            "role_name": CREDIT_CONTROLLER_ROLE,
            "desk_access": 1,
        }
    )
    role.flags.ignore_permissions = True
    role.insert(ignore_permissions=True)


def _ensure_receivable_doctype():
    if frappe.db.exists("DocType", RECEIVABLE_DOCTYPE):
        return

    permissions = [
        {
            "role": "System Manager",
            "read": 1,
            "write": 1,
            "create": 1,
            "delete": 1,
            "report": 1,
            "export": 1,
            "print": 1,
            "email": 1,
            "share": 1,
        },
        {
            "role": "NKT OWNER",
            "read": 1,
            "write": 1,
            "create": 1,
            "report": 1,
            "export": 1,
            "print": 1,
            "email": 1,
            "share": 1,
        },
        {
            "role": "NKT ADMINISTRATOR",
            "read": 1,
            "write": 1,
            "create": 1,
            "report": 1,
            "export": 1,
            "print": 1,
            "email": 1,
            "share": 1,
        },
        {
            "role": CREDIT_CONTROLLER_ROLE,
            "read": 1,
            "write": 1,
            "report": 1,
            "export": 1,
            "print": 1,
            "share": 1,
        },
        {
            "role": "NKT Encoder",
            "read": 1,
            "report": 1,
            "print": 1,
        },
    ]

    fields = [
        {"fieldname": "account_details", "label": "Account Details", "fieldtype": "Section Break"},
        {
            "fieldname": "company",
            "label": "Company",
            "fieldtype": "Link",
            "options": "Company",
            "reqd": 1,
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "customer",
            "label": "Customer",
            "fieldtype": "Link",
            "options": "Customer",
            "reqd": 1,
            "read_only": 1,
            "in_list_view": 1,
            "search_index": 1,
        },
        {
            "fieldname": "customer_name",
            "label": "Customer Name",
            "fieldtype": "Data",
            "read_only": 1,
        },
        {
            "fieldname": "customer_order",
            "label": "Customer Order",
            "fieldtype": "Link",
            "options": "NKT Customer Order",
            "reqd": 1,
            "read_only": 1,
            "in_list_view": 1,
            "search_index": 1,
            "unique": 1,
        },
        {
            "fieldname": "posting_date",
            "label": "Posting Date",
            "fieldtype": "Date",
            "reqd": 1,
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "due_date",
            "label": "Due Date",
            "fieldtype": "Date",
            "reqd": 1,
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "source_encoder",
            "label": "Encoder",
            "fieldtype": "Link",
            "options": "User",
            "read_only": 1,
        },
        {"fieldname": "amounts_section", "label": "Amounts", "fieldtype": "Section Break"},
        {
            "fieldname": "original_amount",
            "label": "Original Account Amount",
            "fieldtype": "Currency",
            "reqd": 1,
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "amount_paid",
            "label": "Amount Paid",
            "fieldtype": "Currency",
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "outstanding_amount",
            "label": "Outstanding Amount",
            "fieldtype": "Currency",
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "status",
            "label": "Receivable Status",
            "fieldtype": "Select",
            "options": "Open\nPartially Paid\nPaid\nCancelled",
            "read_only": 1,
            "in_list_view": 1,
        },
        {"fieldname": "credit_control_section", "label": "Credit Control", "fieldtype": "Section Break"},
        {
            "fieldname": "credit_control_status",
            "label": "Credit Control Status",
            "fieldtype": "Select",
            "options": "Pending Approval\nApproved\nRejected\nCancelled",
            "read_only": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "approved_by",
            "label": "Approved By",
            "fieldtype": "Link",
            "options": "User",
            "read_only": 1,
        },
        {
            "fieldname": "approved_on",
            "label": "Approved On",
            "fieldtype": "Datetime",
            "read_only": 1,
        },
        {
            "fieldname": "approval_reason",
            "label": "Approval Reason",
            "fieldtype": "Small Text",
            "read_only": 1,
        },
        {
            "fieldname": "remarks",
            "label": "Remarks",
            "fieldtype": "Small Text",
        },
    ]

    doc = frappe.get_doc(
        {
            "doctype": "DocType",
            "name": RECEIVABLE_DOCTYPE,
            "module": "NKT Store Operations",
            "custom": 1,
            "autoname": "NKT-REC-.#####",
            "track_changes": 1,
            "allow_rename": 0,
            "fields": fields,
            "permissions": permissions,
            "sort_field": "creation",
            "sort_order": "DESC",
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)


def _ensure_receivable_v16_fields():
    if not frappe.db.exists("DocType", RECEIVABLE_DOCTYPE):
        return
    doc = frappe.get_doc("DocType", RECEIVABLE_DOCTYPE)
    existing = {row.fieldname for row in (doc.get("fields") or []) if row.fieldname}
    fields = [
        {
            "fieldname": "approval_mode",
            "label": "Approval Mode",
            "fieldtype": "Select",
            "options": "\nAutomatic\nManual",
            "read_only": 1,
            "insert_after": "credit_control_status",
        },
        {
            "fieldname": "review_reason",
            "label": "Manual Review Reason",
            "fieldtype": "Small Text",
            "read_only": 1,
            "insert_after": "approval_mode",
        },
    ]
    changed = False
    for field in fields:
        if field["fieldname"] not in existing:
            doc.append("fields", field)
            changed = True
    if changed:
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)


def _client_script() -> str:
    return r'''
(function () {
    const ROOT = "nkt_operations.nkt_store_operations.features.payments_accounts.credit";

    function approve_account_sale(frm) {
        const dialog = new frappe.ui.Dialog({
            title: __("Approve Exception Account Sale"),
            fields: [
                {
                    fieldname: "reason",
                    fieldtype: "Small Text",
                    label: __("Approval Reason"),
                    reqd: 1,
                    description: __("State the credit check performed and why the account sale is approved.")
                },
                {fieldtype: "Section Break", label: __("Authority")},
                {
                    fieldname: "authorized_user",
                    fieldtype: "Link",
                    options: "User",
                    label: __("Authorized User"),
                    description: __("Leave blank when logged in as Administrator, NKT OWNER, NKT ADMINISTRATOR, or NKT Credit Controller.")
                },
                {
                    fieldname: "authorized_password",
                    fieldtype: "Password",
                    label: __("Authorized User Password")
                }
            ],
            primary_action_label: __("Approve Exception Account Sale"),
            primary_action(values) {
                frappe.call({
                    method: `${ROOT}.approve_account_sale`,
                    type: "POST",
                    args: {
                        customer_order: frm.doc.name,
                        reason: values.reason,
                        authorized_user: values.authorized_user || null,
                        authorized_password: values.authorized_password || null
                    },
                    freeze: true,
                    freeze_message: __("Approving account sale...")
                }).then(() => {
                    dialog.hide();
                    frm.reload_doc();
                });
            }
        });
        dialog.show();
    }

    frappe.ui.form.on("NKT Customer Order", {
        refresh(frm) {
            if (frm.doc.custom_nkt_customer_receivable) {
                frm.add_custom_button(__("Open Receivable"), () => {
                    frappe.set_route("Form", "NKT Customer Receivable", frm.doc.custom_nkt_customer_receivable);
                }, __("Account"));
            }

            if (
                frm.doc.docstatus === 1
                && frm.doc.account_sale
                && frm.doc.matched_cashier_sale
                && frm.doc.custom_nkt_account_credit_status === "Pending Approval"
            ) {
                frm.add_custom_button(__("Re-evaluate Credit Rules"), async () => {
                    await frappe.call({
                        method: `${ROOT}.reevaluate_account_sale`,
                        type: "POST",
                        args: {customer_order: frm.doc.name},
                        freeze: true,
                        freeze_message: __("Checking automatic approval rules...")
                    });
                    frm.reload_doc();
                }, __("Account"));
                frm.add_custom_button(__("Approve Exception Account Sale"), () => approve_account_sale(frm), __("Account"));
                if (frm.doc.custom_nkt_account_review_reason) {
                    frm.dashboard.set_headline_alert(
                        __("Manual credit review required: {0}", [frm.doc.custom_nkt_account_review_reason]),
                        "orange"
                    );
                }
            }
        }
    });
})();
'''


def _install_client_script():
    values = {
        "dt": "NKT Customer Order",
        "view": "Form",
        "enabled": 1,
        "script": _client_script(),
    }
    if frappe.db.exists("Client Script", CLIENT_SCRIPT_NAME):
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


def _account_amount_from_order(order) -> float:
    return max(flt(order.get("declared_account")), 0)


def _account_amount_from_cashier_sale(sale) -> float:
    return sum(
        max(flt(row.amount), 0)
        for row in (sale.get("payments") or [])
        if row.payment_method == "Account"
    )


def _customer_settings(customer: str):
    values = frappe.db.get_value(
        "Customer",
        customer,
        [
            "customer_name",
            "custom_nkt_allow_account_sales",
            "custom_nkt_credit_status",
            "custom_nkt_credit_limit",
            "custom_nkt_credit_terms_days",
            "custom_nkt_require_manual_account_approval",
            "custom_nkt_auto_approval_limit",
        ],
        as_dict=True,
    )
    if not values:
        frappe.throw(_("Customer {0} does not exist.").format(customer))
    return values


def get_customer_credit_snapshot(customer: str, exclude_order: str | None = None):
    settings = _customer_settings(customer)
    conditions = ["customer = %s", "status IN ('Open', 'Partially Paid')"]
    params = [customer]
    if exclude_order:
        conditions.append("customer_order != %s")
        params.append(exclude_order)

    outstanding = flt(
        frappe.db.sql(
            f"""
            SELECT COALESCE(SUM(outstanding_amount), 0)
            FROM `tab{RECEIVABLE_DOCTYPE}`
            WHERE {' AND '.join(conditions)}
            """,
            tuple(params),
        )[0][0]
    )
    credit_limit = max(flt(settings.custom_nkt_credit_limit), 0)
    available_credit = max(credit_limit - outstanding, 0)
    return {
        "customer": customer,
        "customer_name": settings.customer_name,
        "allow_account_sales": int(settings.custom_nkt_allow_account_sales or 0),
        "credit_status": settings.custom_nkt_credit_status or "On Hold",
        "credit_limit": credit_limit,
        "credit_terms_days": max(int(settings.custom_nkt_credit_terms_days or 0), 0),
        "require_manual_approval": int(settings.custom_nkt_require_manual_account_approval or 0),
        "auto_approval_limit": max(flt(settings.custom_nkt_auto_approval_limit), 0),
        "current_balance": outstanding,
        "available_credit": available_credit,
    }


def _lock_customer_credit(customer: str):
    frappe.db.sql(
        "SELECT name FROM `tabCustomer` WHERE name = %s FOR UPDATE",
        customer,
    )


def _validate_customer_account_enabled(customer: str):
    settings = _customer_settings(customer)
    if not settings.custom_nkt_allow_account_sales:
        frappe.throw(
            _(
                "Customer {0} is not enabled for account sales. Enable Allow Account Sales on the Customer record first."
            ).format(settings.customer_name or customer)
        )
    return settings


def _overdue_receivables(customer: str, exclude_order: str | None = None):
    conditions = [
        "customer = %s",
        "status IN ('Open', 'Partially Paid')",
        "outstanding_amount > %s",
        "due_date < %s",
    ]
    params = [customer, TOLERANCE, getdate(today())]
    if exclude_order:
        conditions.append("customer_order != %s")
        params.append(exclude_order)
    return frappe.db.sql(
        f"""
        SELECT name, customer_order, due_date, outstanding_amount
        FROM `tab{RECEIVABLE_DOCTYPE}`
        WHERE {' AND '.join(conditions)}
        ORDER BY due_date ASC, creation ASC
        """,
        tuple(params),
        as_dict=True,
    )


def _manual_review_reasons(order, snapshot):
    reasons = []
    account_amount = _account_amount_from_order(order)
    if snapshot["credit_status"] != "Active":
        reasons.append(_("Customer credit status is {0}.").format(snapshot["credit_status"]))
    if snapshot["credit_limit"] <= TOLERANCE:
        reasons.append(_("Customer has no positive credit limit."))
    elif account_amount > snapshot["available_credit"] + TOLERANCE:
        reasons.append(
            _("Account amount exceeds available credit by {0}.").format(
                frappe.format_value(
                    account_amount - snapshot["available_credit"],
                    {"fieldtype": "Currency"},
                )
            )
        )
    if snapshot["require_manual_approval"]:
        reasons.append(_("Customer is configured for manual approval on every account sale."))
    if snapshot["auto_approval_limit"] > TOLERANCE and account_amount > snapshot["auto_approval_limit"] + TOLERANCE:
        reasons.append(
            _("Order exceeds the customer's automatic-approval limit of {0}.").format(
                frappe.format_value(snapshot["auto_approval_limit"], {"fieldtype": "Currency"})
            )
        )
    overdue = _overdue_receivables(order.customer, exclude_order=order.name)
    if overdue:
        reasons.append(
            _("Customer has {0} overdue account balance(s); oldest due date is {1}.").format(
                len(overdue), frappe.format_value(overdue[0].due_date, {"fieldtype": "Date"})
            )
        )
    return reasons


def validate_customer_order_account_credit(order):
    account_amount = _account_amount_from_order(order)
    if account_amount <= TOLERANCE:
        if order.meta.has_field("custom_nkt_account_credit_status"):
            order.custom_nkt_account_credit_status = "Not Required"
            order.custom_nkt_account_approval_mode = "Not Required"
            order.custom_nkt_account_review_reason = ""
            order.custom_nkt_account_due_date = None
        return

    if not order.customer:
        frappe.throw(_("Customer is required before Account payment can be used."))

    settings = _validate_customer_account_enabled(order.customer)
    due_date = add_days(getdate(order.order_date), max(int(settings.custom_nkt_credit_terms_days or 0), 0))
    if order.meta.has_field("custom_nkt_account_credit_status"):
        if order.custom_nkt_account_credit_status not in {"Approved", "Rejected"}:
            order.custom_nkt_account_credit_status = "Pending Approval"
            order.custom_nkt_account_approval_mode = ""
        order.custom_nkt_account_due_date = due_date


def validate_cashier_sale_account_credit(sale):
    account_amount = _account_amount_from_cashier_sale(sale)
    if account_amount <= TOLERANCE:
        return
    if not sale.customer:
        frappe.throw(_("Customer is required before Account payment can be used."))
    _validate_customer_account_enabled(sale.customer)


def _set_pending_manual_review(order, receivable_name: str, reasons):
    reason_text = " ".join(str(reason).strip() for reason in reasons if str(reason).strip())
    frappe.db.set_value(
        RECEIVABLE_DOCTYPE,
        receivable_name,
        {
            "credit_control_status": "Pending Approval",
            "approved_by": None,
            "approved_on": None,
            "approval_reason": reason_text,
            "approval_mode": "",
            "review_reason": reason_text,
        },
        update_modified=False,
    )
    frappe.db.set_value(
        "NKT Customer Order",
        order.name,
        {
            "custom_nkt_account_credit_status": "Pending Approval",
            "custom_nkt_account_approval_mode": "",
            "custom_nkt_account_review_reason": reason_text,
            "custom_nkt_account_approved_by": None,
            "custom_nkt_account_approved_on": None,
            "custom_nkt_account_approval_reason": "",
            "status": "Pending Credit Control",
        },
        update_modified=False,
    )
    return {"approved": False, "manual_review_required": True, "reasons": reasons}


def _finalize_account_approval(order, receivable_name: str, *, mode: str, approved_by=None, reason: str):
    approved_on = now_datetime()
    frappe.db.set_value(
        RECEIVABLE_DOCTYPE,
        receivable_name,
        {
            "credit_control_status": "Approved",
            "approved_by": approved_by,
            "approved_on": approved_on,
            "approval_reason": reason,
            "approval_mode": mode,
            "review_reason": "",
        },
        update_modified=False,
    )
    frappe.db.set_value(
        "NKT Customer Order",
        order.name,
        {
            "custom_nkt_account_credit_status": "Approved",
            "custom_nkt_account_approval_mode": mode,
            "custom_nkt_account_review_reason": "",
            "custom_nkt_account_approved_by": approved_by,
            "custom_nkt_account_approved_on": approved_on,
            "custom_nkt_account_approval_reason": reason,
            "amount_due": flt(frappe.db.get_value(RECEIVABLE_DOCTYPE, receivable_name, "outstanding_amount")),
            "status": "Ready for Release",
        },
        update_modified=False,
    )
    from nkt_operations.nkt_store_operations.features.inventory.order_fulfillment import (
        update_customer_order_fulfillment_status,
    )
    update_customer_order_fulfillment_status(order.name)
    refresh_customer_credit(order.customer)
    # V2.0C.5.2.1 auto-apply Customer Advance after Credit Control approval
    from nkt_operations.nkt_store_operations.features.payments_accounts.internal.auto_advance import (
        auto_apply_customer_advance_for_order,
    )
    auto_apply_customer_advance_for_order(order.name)
    return {"approved": True, "mode": mode, "approved_on": approved_on}


def evaluate_account_sale_approval(order_name: str):
    order = frappe.get_doc("NKT Customer Order", order_name)
    account_amount = _account_amount_from_order(order)
    if account_amount <= TOLERANCE:
        return {"approved": False, "not_required": True}
    if order.docstatus != 1:
        return {"approved": False, "reason": "Order is not submitted."}
    if not order.matched_cashier_sale:
        return _set_pending_manual_review(
            order,
            order.custom_nkt_customer_receivable,
            [_("Cashier and encoder records have not yet matched.")],
        )

    snapshot = get_customer_credit_snapshot(order.customer, exclude_order=order.name)
    reasons = _manual_review_reasons(order, snapshot)
    if reasons:
        return _set_pending_manual_review(order, order.custom_nkt_customer_receivable, reasons)

    reason = _(
        "Automatically approved: active account customer, no overdue balance, within available credit, and within the configured per-order automatic-approval limit."
    )
    result = _finalize_account_approval(
        order,
        order.custom_nkt_customer_receivable,
        mode="Automatic",
        approved_by=None,
        reason=reason,
    )
    frappe.get_doc("NKT Customer Order", order.name).add_comment("Info", reason)
    frappe.get_doc(RECEIVABLE_DOCTYPE, order.custom_nkt_customer_receivable).add_comment("Info", reason)
    return result


def process_customer_order_receivable(order_name: str):
    order = frappe.get_doc("NKT Customer Order", order_name)
    account_amount = _account_amount_from_order(order)
    if account_amount <= TOLERANCE:
        frappe.db.set_value(
            "NKT Customer Order",
            order.name,
            {
                "custom_nkt_account_credit_status": "Not Required",
                "custom_nkt_account_approval_mode": "Not Required",
                "custom_nkt_account_review_reason": "",
                "custom_nkt_account_due_date": None,
                "custom_nkt_customer_receivable": None,
            },
            update_modified=False,
        )
        return None

    _lock_customer_credit(order.customer)
    settings = _validate_customer_account_enabled(order.customer)
    due_date = add_days(getdate(order.order_date), max(int(settings.custom_nkt_credit_terms_days or 0), 0))
    existing = frappe.db.get_value(
        RECEIVABLE_DOCTYPE,
        {"customer_order": order.name},
        "name",
    )
    if existing:
        receivable = frappe.get_doc(RECEIVABLE_DOCTYPE, existing)
        if receivable.status == "Cancelled":
            frappe.throw(_("Receivable {0} for this order is already cancelled.").format(existing))
        values = {
            "due_date": due_date,
            "original_amount": account_amount,
            "outstanding_amount": max(account_amount - flt(receivable.amount_paid), 0),
        }
        frappe.db.set_value(RECEIVABLE_DOCTYPE, existing, values, update_modified=False)
        receivable_name = existing
    else:
        receivable = frappe.get_doc(
            {
                "doctype": RECEIVABLE_DOCTYPE,
                "company": order.company,
                "customer": order.customer,
                "customer_name": order.customer_name,
                "customer_order": order.name,
                "posting_date": getdate(order.order_date),
                "due_date": due_date,
                "source_encoder": order.encoder,
                "original_amount": account_amount,
                "amount_paid": 0,
                "outstanding_amount": account_amount,
                "status": "Open",
                "credit_control_status": "Pending Approval",
                "remarks": f"Official internal receivable created from encoder Customer Order {order.name}.",
            }
        )
        receivable.flags.ignore_permissions = True
        receivable.insert(ignore_permissions=True)
        receivable_name = receivable.name
        order.add_comment(
            "Info",
            _("Created Customer Receivable {0} for account amount {1}.").format(
                receivable_name,
                frappe.format_value(account_amount, {"fieldtype": "Currency"}),
            ),
        )

    frappe.db.set_value(
        "NKT Customer Order",
        order.name,
        {
            "custom_nkt_customer_receivable": receivable_name,
            "custom_nkt_account_credit_status": "Pending Approval",
            "custom_nkt_account_approval_mode": "",
            "custom_nkt_account_review_reason": "",
            "custom_nkt_account_due_date": due_date,
            "amount_due": max(account_amount - flt(receivable.amount_paid), 0),
        },
        update_modified=False,
    )
    refresh_customer_credit(order.customer)
    evaluate_account_sale_approval(order.name)
    return receivable_name


def cancel_customer_order_receivable(order_name: str):
    receivable_name = frappe.db.get_value(
        RECEIVABLE_DOCTYPE,
        {"customer_order": order_name},
        "name",
    )
    if not receivable_name:
        return None
    receivable = frappe.get_doc(RECEIVABLE_DOCTYPE, receivable_name)
    if flt(receivable.amount_paid) > TOLERANCE:
        frappe.throw(
            _(
                "Customer Order {0} cannot be cancelled because Receivable {1} already has payments. Reverse or reallocate those collections first."
            ).format(order_name, receivable_name)
        )
    if receivable.status != "Cancelled":
        frappe.db.set_value(
            RECEIVABLE_DOCTYPE,
            receivable_name,
            {
                "status": "Cancelled",
                "credit_control_status": "Cancelled",
                "outstanding_amount": 0,
            },
            update_modified=False,
        )
    frappe.db.set_value(
        "NKT Customer Order",
        order_name,
        "custom_nkt_account_credit_status",
        "Cancelled",
        update_modified=False,
    )
    refresh_customer_credit(receivable.customer)
    return receivable_name


def refresh_customer_credit(customer: str):
    if not customer or not frappe.db.exists("Customer", customer):
        return None
    snapshot = get_customer_credit_snapshot(customer)
    frappe.db.set_value(
        "Customer",
        customer,
        {
            "custom_nkt_current_account_balance": snapshot["current_balance"],
            "custom_nkt_available_credit": snapshot["available_credit"],
        },
        update_modified=False,
    )
    return snapshot


def sync_order_receivable_balance(order_name: str):
    """Synchronize the order display balance with its official receivable.

    The Customer Order's amount_due must represent money still owed by the
    customer. Declaring a payment row as Account is not payment received.
    """
    if not order_name or not frappe.db.exists("NKT Customer Order", order_name):
        frappe.throw(_("Customer Order {0} does not exist.").format(order_name))

    order = frappe.get_doc("NKT Customer Order", order_name)
    receivable_name = order.get("custom_nkt_customer_receivable") or frappe.db.get_value(
        RECEIVABLE_DOCTYPE,
        {"customer_order": order.name, "status": ["!=", "Cancelled"]},
        "name",
    )
    if not receivable_name:
        return {
            "customer_order": order.name,
            "receivable": None,
            "amount_due": flt(order.amount_due),
        }

    receivable = frappe.get_doc(RECEIVABLE_DOCTYPE, receivable_name)
    outstanding = max(flt(receivable.outstanding_amount), 0)
    frappe.db.set_value(
        "NKT Customer Order",
        order.name,
        {
            "custom_nkt_customer_receivable": receivable.name,
            "amount_due": outstanding,
        },
        update_modified=False,
    )
    refresh_customer_credit(order.customer)
    return {
        "customer_order": order.name,
        "receivable": receivable.name,
        "amount_due": outstanding,
        "receivable_outstanding": outstanding,
    }


@frappe.whitelist()
def repair_account_order(order_name: str):
    """Idempotent forward repair for an already-created account order."""
    return sync_order_receivable_balance(order_name)


def _reevaluate_pending_account_sales():
    if not frappe.db.exists("DocType", RECEIVABLE_DOCTYPE):
        return
    names = frappe.get_all(
        "NKT Customer Order",
        filters={
            "docstatus": 1,
            "account_sale": 1,
            "custom_nkt_account_credit_status": "Pending Approval",
            "custom_nkt_customer_receivable": ["is", "set"],
        },
        pluck="name",
        order_by="creation asc",
    )
    for name in names:
        try:
            evaluate_account_sale_approval(name)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"NKT V1.6 account reevaluation failed: {name}")


def _backfill_customer_credit_totals():
    customers = frappe.get_all(
        RECEIVABLE_DOCTYPE,
        filters={"status": ["in", ["Open", "Partially Paid"]]},
        pluck="customer",
    ) if frappe.db.exists("DocType", RECEIVABLE_DOCTYPE) else []
    for customer in sorted(set(filter(None, customers))):
        refresh_customer_credit(customer)


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
                "This user cannot approve account sales directly. Enter Administrator, NKT OWNER, NKT ADMINISTRATOR, or NKT Credit Controller credentials."
            )
        )
    if not frappe.db.get_value("User", authorized_user, "enabled"):
        frappe.throw(_("Authorized User {0} is disabled or does not exist.").format(authorized_user))
    if not _is_direct_authority(authorized_user):
        frappe.throw(
            _("User {0} does not have account-credit approval authority.").format(
                authorized_user
            )
        )
    return check_password(authorized_user, authorized_password)


@frappe.whitelist(methods=["POST"])
def reevaluate_account_sale(customer_order):
    order = frappe.get_doc("NKT Customer Order", customer_order)
    order.check_permission("read")
    if order.docstatus != 1:
        frappe.throw(_("The Customer Order must be submitted."))
    if not order.get("custom_nkt_customer_receivable"):
        process_customer_order_receivable(order.name)
    return evaluate_account_sale_approval(order.name)


@frappe.whitelist(methods=["POST"])
def approve_account_sale(
    customer_order,
    reason,
    authorized_user=None,
    authorized_password=None,
):
    reason = " ".join((reason or "").strip().split())
    if len(reason) < 10:
        frappe.throw(_("Enter a specific credit approval reason of at least 10 characters."))

    requester = frappe.session.user
    approver = _get_authorizing_user(authorized_user, authorized_password)
    order = frappe.get_doc("NKT Customer Order", customer_order)
    order.check_permission("read")

    if order.docstatus != 1:
        frappe.throw(_("The Customer Order must be submitted."))
    account_amount = _account_amount_from_order(order)
    if account_amount <= TOLERANCE:
        frappe.throw(_("Customer Order {0} is not an account sale.").format(order.name))
    if not order.matched_cashier_sale:
        frappe.throw(
            _(
                "Cashier and encoder records must reconcile before an account sale can be approved."
            )
        )

    receivable_name = order.get("custom_nkt_customer_receivable") or frappe.db.get_value(
        RECEIVABLE_DOCTYPE,
        {"customer_order": order.name},
        "name",
    )
    if not receivable_name:
        receivable_name = process_customer_order_receivable(order.name)
    receivable = frappe.get_doc(RECEIVABLE_DOCTYPE, receivable_name)

    if receivable.credit_control_status == "Approved":
        return {
            "customer_order": order.name,
            "receivable": receivable.name,
            "approved_by": receivable.approved_by,
            "already_approved": True,
        }
    if receivable.status == "Cancelled":
        frappe.throw(_("Receivable {0} is cancelled.").format(receivable.name))

    snapshot = _customer_settings(order.customer)
    if not snapshot.custom_nkt_allow_account_sales:
        frappe.throw(_("The Customer is no longer enabled for account sales."))

    result = _finalize_account_approval(
        order,
        receivable.name,
        mode="Manual",
        approved_by=approver,
        reason=reason,
    )
    approved_on = result["approved_on"]
    message = _(
        "Account sale approved. Requested by {0}; approved by {1}. Reason: {2}"
    ).format(requester, approver, reason)
    frappe.get_doc("NKT Customer Order", order.name).add_comment("Info", message)
    frappe.get_doc(RECEIVABLE_DOCTYPE, receivable.name).add_comment("Info", message)
    refresh_customer_credit(order.customer)
    # V2.0C.5.2.1 auto-apply Customer Advance after Credit Control approval
    from nkt_operations.nkt_store_operations.features.payments_accounts.internal.auto_advance import (
        auto_apply_customer_advance_for_order,
    )
    auto_apply_customer_advance_for_order(order.name)
    return {
        "customer_order": order.name,
        "receivable": receivable.name,
        "requested_by": requester,
        "approved_by": approver,
        "approved_on": approved_on,
    }
