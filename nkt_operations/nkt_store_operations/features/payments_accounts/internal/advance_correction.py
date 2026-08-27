
import frappe
from frappe import _
from frappe.utils import flt, now_datetime

TOLERANCE = 0.005

APPLICATION = "NKT Customer Advance Application"
ADVANCE = "NKT Customer Advance"
ORDER = "NKT Customer Order"
RECEIVABLE = "NKT Customer Receivable"

AUTHORIZED_ROLES = {
    "System Manager",
    "NKT OWNER",
    "NKT ADMINISTRATOR",
    "NKT Credit Controller",
}

APP_SCRIPT = "NKT C5.4 Advance Application Correction"
ORDER_SCRIPT = "NKT C5.4 Advance Correction Order Tools"

AUTO_MODULE = (
    "nkt_operations.nkt_store_operations.features.payments_accounts.internal.auto_advance"
)
CORE_MODULE = (
    "nkt_operations.nkt_store_operations.features.payments_accounts.collection"
)


def _require_authority():
    user = frappe.session.user

    if user == "Administrator":
        return

    roles = set(frappe.get_roles(user))

    if not roles.intersection(AUTHORIZED_ROLES):
        frappe.throw(
            _(
                "Only Owner, Administrator, System Manager, or "
                "NKT Credit Controller may correct Customer Advance "
                "applications."
            ),
            frappe.PermissionError,
        )


def _has_field(doctype, fieldname):
    return frappe.get_meta(doctype).has_field(fieldname)


def _lock(doctype, name):
    frappe.db.sql(
        f"SELECT name FROM `tab{doctype}` WHERE name=%s FOR UPDATE",
        name,
    )


def _status_for_receivable(outstanding, amount_paid):
    if outstanding <= TOLERANCE:
        return "Paid"
    if amount_paid > TOLERANCE:
        return "Partially Paid"
    return "Open"


def _payment_status_for_order(amount_paid, amount_due, is_account):
    if amount_due <= TOLERANCE:
        return "Paid"

    if amount_paid > TOLERANCE:
        return "Partially Paid"

    return "Charged to Account" if is_account else "Unpaid"


def _restore_advance(advance_name, amount):
    _lock(ADVANCE, advance_name)
    advance = frappe.get_doc(ADVANCE, advance_name)

    current_applied = flt(advance.applied_amount)
    original = flt(advance.original_advance_amount)

    if current_applied + TOLERANCE < amount:
        frappe.throw(
            _(
                "Customer Advance {0} currently shows only {1} applied, "
                "which is less than the {2} being reversed."
            ).format(
                advance.name,
                frappe.format_value(
                    current_applied,
                    {"fieldtype": "Currency"},
                ),
                frappe.format_value(
                    amount,
                    {"fieldtype": "Currency"},
                ),
            )
        )

    new_applied = max(current_applied - amount, 0)
    new_available = max(original - new_applied, 0)

    if new_available <= TOLERANCE:
        advance_status = "Fully Used"
    elif new_applied > TOLERANCE:
        advance_status = "Partially Used"
    else:
        advance_status = "Available"

    frappe.db.set_value(
        ADVANCE,
        advance.name,
        {
            "applied_amount": new_applied,
            "available_advance_amount": new_available,
            "advance_status": advance_status,
        },
        update_modified=False,
    )

    return frappe._dict(
        name=advance.name,
        applied_amount=new_applied,
        available_advance_amount=new_available,
        advance_status=advance_status,
    )


def _reverse_source_order(application, amount, reason):
    order_name = application.customer_order

    if not order_name or not frappe.db.exists(ORDER, order_name):
        return {
            "customer_order": order_name,
            "receivable": None,
            "order_updated": False,
        }

    _lock(ORDER, order_name)
    order = frappe.get_doc(ORDER, order_name)

    receivable = frappe.db.get_value(
        RECEIVABLE,
        {
            "customer_order": order.name,
            "docstatus": ["!=", 2],
            "credit_control_status": "Approved",
        },
        [
            "name",
            "original_amount",
            "amount_paid",
            "outstanding_amount",
            "status",
        ],
        as_dict=True,
    )

    receivable_name = None

    if receivable:
        receivable_name = receivable.name
        _lock(RECEIVABLE, receivable.name)
        receivable = frappe.get_doc(RECEIVABLE, receivable.name)

        if flt(receivable.amount_paid) + TOLERANCE < amount:
            frappe.throw(
                _(
                    "Receivable {0} no longer contains enough applied "
                    "amount to reverse this Customer Advance application."
                ).format(receivable.name)
            )

        new_receivable_paid = max(
            flt(receivable.amount_paid) - amount,
            0,
        )
        new_outstanding = min(
            flt(receivable.original_amount),
            flt(receivable.outstanding_amount) + amount,
        )

        frappe.db.set_value(
            RECEIVABLE,
            receivable.name,
            {
                "amount_paid": new_receivable_paid,
                "outstanding_amount": new_outstanding,
                "status": _status_for_receivable(
                    new_outstanding,
                    new_receivable_paid,
                ),
            },
            update_modified=False,
        )

    if flt(order.amount_paid) + TOLERANCE < amount:
        frappe.throw(
            _(
                "Customer Order {0} no longer contains enough settled "
                "amount to reverse this Customer Advance application."
            ).format(order.name)
        )

    new_order_paid = max(flt(order.amount_paid) - amount, 0)
    new_order_due = max(flt(order.amount_due) + amount, 0)

    is_account = flt(order.get("declared_account")) > TOLERANCE

    values = {
        "amount_paid": new_order_paid,
        "amount_due": new_order_due,
        "payment_status": _payment_status_for_order(
            new_order_paid,
            new_order_due,
            is_account,
        ),
    }

    protected_statuses = {
        "Released",
        "Partially Released",
    }

    if (order.status or "") not in protected_statuses:
        credit_status = (
            order.get("custom_nkt_account_credit_status") or ""
        ).strip()

        if is_account and credit_status == "Approved":
            values["status"] = "Ready for Release"
        elif new_order_due > TOLERANCE:
            values["status"] = "Awaiting Payment"

    if _has_field(ORDER, "custom_nkt_advance_auto_apply_hold"):
        values["custom_nkt_advance_auto_apply_hold"] = 1

    if _has_field(ORDER, "custom_nkt_advance_hold_reason"):
        values["custom_nkt_advance_hold_reason"] = (
            "Advance application correction: " + reason
        )

    frappe.db.set_value(
        ORDER,
        order.name,
        values,
        update_modified=False,
    )

    frappe.get_doc(ORDER, order.name).add_comment(
        "Info",
        _(
            "Customer Advance application {0} was reversed by {1}. "
            "Amount restored to advance: {2}. Reason: {3}. "
            "Advance auto-apply is held on this order until an "
            "authorized reviewer releases it."
        ).format(
            application.name,
            frappe.session.user,
            frappe.format_value(
                amount,
                {"fieldtype": "Currency"},
            ),
            reason,
        ),
    )

    return {
        "customer_order": order.name,
        "receivable": receivable_name,
        "order_updated": True,
    }


def _mark_application_reversed(application, reason):
    now = now_datetime()

    values = {
        "application_status": "Reversed",
    }

    optional_values = {
        "custom_nkt_reversed_on": now,
        "custom_nkt_reversed_by": frappe.session.user,
        "custom_nkt_reversal_reason": reason,
    }

    for fieldname, value in optional_values.items():
        if _has_field(APPLICATION, fieldname):
            values[fieldname] = value

    frappe.db.set_value(
        APPLICATION,
        application.name,
        values,
        update_modified=False,
    )

    return now


def _apply_restored_advance_to_target(
    source_application,
    target_order,
    amount,
    reason,
):
    if not target_order:
        return None

    if target_order == source_application.customer_order:
        frappe.throw(
            _(
                "Reverse and Reapply must use a different Customer Order. "
                "Use Release Advance Auto-Apply Hold later if you truly "
                "intend to put the advance back on the same order."
            )
        )

    if not frappe.db.exists(ORDER, target_order):
        frappe.throw(_("Target Customer Order does not exist."))

    target = frappe.get_doc(ORDER, target_order)

    if target.docstatus != 1:
        frappe.throw(_("Target Customer Order must be submitted."))

    if target.company != source_application.company:
        frappe.throw(
            _("Target Customer Order belongs to a different company.")
        )

    if target.customer != source_application.customer:
        frappe.throw(
            _("Target Customer Order belongs to a different customer.")
        )

    if flt(target.get("declared_account")) <= TOLERANCE:
        frappe.throw(
            _(
                "Target Customer Order must be an Account sale for "
                "a controlled advance reapplication."
            )
        )

    if (
        target.get("custom_nkt_account_credit_status") or ""
    ).strip() != "Approved":
        frappe.throw(
            _("Target Account sale must be Credit Control Approved.")
        )

    if not (
        target.get("cashier_reconciliation_status") or ""
    ).startswith("Matched"):
        frappe.throw(
            _(
                "Target Account sale must already be matched between "
                "Cashier and Encoder."
            )
        )

    target_due = max(flt(target.amount_due), 0)

    if target_due <= TOLERANCE:
        frappe.throw(_("Target Customer Order has no amount due."))

    apply_amount = min(amount, target_due)

    if _has_field(ORDER, "custom_nkt_advance_auto_apply_hold"):
        frappe.db.set_value(
            ORDER,
            target.name,
            "custom_nkt_advance_auto_apply_hold",
            0,
            update_modified=False,
        )

    result = frappe.get_attr(
        CORE_MODULE + ".apply_customer_advance_to_order"
    )(
        customer_order=target.name,
        amount=apply_amount,
        remarks=(
            f"C5.4 controlled reapplication after reversing "
            f"{source_application.name}. Reason: {reason}"
        ),
    )

    frappe.get_doc(ORDER, target.name).add_comment(
        "Info",
        _(
            "Customer Advance was intentionally reapplied here as part "
            "of correction of application {0}. Amount requested: {1}. "
            "Reason: {2}."
        ).format(
            source_application.name,
            frappe.format_value(
                apply_amount,
                {"fieldtype": "Currency"},
            ),
            reason,
        ),
    )

    return {
        "target_customer_order": target.name,
        "requested_amount": apply_amount,
        "result": result,
    }


@frappe.whitelist()
def reverse_advance_application(
    application,
    reason,
    reapply_to_order=None,
):
    _require_authority()

    reason = (reason or "").strip()

    if not reason:
        frappe.throw(_("Correction reason is required."))

    if not frappe.db.exists(APPLICATION, application):
        frappe.throw(_("Customer Advance Application does not exist."))

    _lock(APPLICATION, application)
    app = frappe.get_doc(APPLICATION, application)

    if app.docstatus != 1:
        frappe.throw(
            _("Only submitted Customer Advance Applications can be corrected.")
        )

    if (app.application_status or "").strip() != "Applied":
        frappe.throw(
            _(
                "Only an Applied Customer Advance Application can be "
                "reversed. Current status: {0}."
            ).format(app.application_status or "")
        )

    amount = max(flt(app.applied_amount), 0)

    if amount <= TOLERANCE:
        frappe.throw(_("Application amount must be greater than zero."))

    receipts_before = frappe.db.count("NKT Payment Receipt")
    movements_before = frappe.db.count("NKT Cashier Movement")

    try:
        advance_after = _restore_advance(
            app.customer_advance,
            amount,
        )

        source_result = _reverse_source_order(
            app,
            amount,
            reason,
        )

        reversed_on = _mark_application_reversed(
            app,
            reason,
        )

        from nkt_operations.nkt_store_operations.features.payments_accounts.credit import (
            refresh_customer_credit,
        )

        refresh_customer_credit(app.customer)

        reapply_result = _apply_restored_advance_to_target(
            app,
            reapply_to_order,
            amount,
            reason,
        )

        refresh_customer_credit(app.customer)

        receipts_after = frappe.db.count("NKT Payment Receipt")
        movements_after = frappe.db.count("NKT Cashier Movement")

        if receipts_after != receipts_before:
            frappe.throw(
                _(
                    "C5.4 safety stop: correction created a new "
                    "Payment Receipt."
                )
            )

        if movements_after != movements_before:
            frappe.throw(
                _(
                    "C5.4 safety stop: correction created a new "
                    "Cashier Movement."
                )
            )

        frappe.db.commit()

        return {
            "application": app.name,
            "application_status": "Reversed",
            "reversed_on": reversed_on,
            "reversed_by": frappe.session.user,
            "reason": reason,
            "source": source_result,
            "advance_after_reversal": advance_after,
            "reapplication": reapply_result,
            "payment_receipt_count_unchanged": True,
            "cashier_movement_count_unchanged": True,
        }

    except Exception:
        frappe.db.rollback()
        raise


@frappe.whitelist()
def release_order_advance_hold(
    customer_order,
    reason,
    apply_now=1,
):
    _require_authority()

    reason = (reason or "").strip()

    if not reason:
        frappe.throw(_("Reason is required to release the hold."))

    if not frappe.db.exists(ORDER, customer_order):
        frappe.throw(_("Customer Order does not exist."))

    order = frappe.get_doc(ORDER, customer_order)

    if not _has_field(ORDER, "custom_nkt_advance_auto_apply_hold"):
        frappe.throw(_("Advance Auto-Apply Hold field is not installed."))

    if not int(order.get("custom_nkt_advance_auto_apply_hold") or 0):
        return {
            "customer_order": order.name,
            "already_released": True,
        }

    frappe.db.set_value(
        ORDER,
        order.name,
        {
            "custom_nkt_advance_auto_apply_hold": 0,
            "custom_nkt_advance_hold_reason": (
                f"Hold released by {frappe.session.user}: {reason}"
            ),
        },
        update_modified=False,
    )

    result = None

    if int(apply_now or 0):
        result = frappe.get_attr(
            AUTO_MODULE + ".auto_apply_customer_advance_for_order"
        )(
            order.name
        )

    frappe.get_doc(ORDER, order.name).add_comment(
        "Info",
        _(
            "Customer Advance auto-apply hold released by {0}. "
            "Apply now: {1}. Reason: {2}."
        ).format(
            frappe.session.user,
            "Yes" if int(apply_now or 0) else "No",
            reason,
        ),
    )

    frappe.db.commit()

    return {
        "customer_order": order.name,
        "hold_released": True,
        "apply_now": bool(int(apply_now or 0)),
        "application_result": result,
    }


def _ensure_custom_fields():
    from frappe.custom.doctype.custom_field.custom_field import (
        create_custom_fields,
    )

    fields = {
        APPLICATION: [
            {
                "fieldname": "custom_nkt_c5_4_reversal_section",
                "label": "Controlled Advance Correction",
                "fieldtype": "Section Break",
                "insert_after": "remarks",
                "read_only": 1,
            },
            {
                "fieldname": "custom_nkt_reversed_on",
                "label": "Reversed On",
                "fieldtype": "Datetime",
                "insert_after": "custom_nkt_c5_4_reversal_section",
                "read_only": 1,
                "allow_on_submit": 1,
            },
            {
                "fieldname": "custom_nkt_reversed_by",
                "label": "Reversed By",
                "fieldtype": "Link",
                "options": "User",
                "insert_after": "custom_nkt_reversed_on",
                "read_only": 1,
                "allow_on_submit": 1,
            },
            {
                "fieldname": "custom_nkt_reversal_reason",
                "label": "Reversal Reason",
                "fieldtype": "Small Text",
                "insert_after": "custom_nkt_reversed_by",
                "read_only": 1,
                "allow_on_submit": 1,
            },
        ],
        ORDER: [
            {
                "fieldname": "custom_nkt_advance_auto_apply_hold",
                "label": "Advance Auto-Apply Hold",
                "fieldtype": "Check",
                "default": "0",
                "read_only": 1,
                "hidden": 1,
                "allow_on_submit": 1,
            },
            {
                "fieldname": "custom_nkt_advance_hold_reason",
                "label": "Advance Auto-Apply Hold Reason",
                "fieldtype": "Small Text",
                "read_only": 1,
                "hidden": 1,
                "allow_on_submit": 1,
                "insert_after": "custom_nkt_advance_auto_apply_hold",
            },
        ],
    }

    create_custom_fields(
        fields,
        update=True,
        ignore_validate=True,
    )

    frappe.clear_cache(doctype=APPLICATION)
    frappe.clear_cache(doctype=ORDER)


def _ensure_application_permissions():
    if not frappe.db.exists("DocType", APPLICATION):
        return

    doc = frappe.get_doc("DocType", APPLICATION)
    existing = {
        row.role: row
        for row in (doc.permissions or [])
    }

    changed = False

    for role in (
        "System Manager",
        "NKT OWNER",
        "NKT ADMINISTRATOR",
        "NKT Credit Controller",
    ):
        if role not in existing:
            row = doc.append("permissions", {})
            row.role = role
            row.read = 1
            row.write = 1
            row.report = 1
            row.export = 1
            row.print = 1
            changed = True
        else:
            row = existing[role]
            desired = {
                "read": 1,
                "write": 1,
                "report": 1,
                "export": 1,
                "print": 1,
            }
            for fieldname, value in desired.items():
                if int(getattr(row, fieldname, 0) or 0) != value:
                    setattr(row, fieldname, value)
                    changed = True

    if changed:
        doc.flags.ignore_permissions = True
        doc.save()


def _application_client_script():
    return r'''
frappe.ui.form.on("NKT Customer Advance Application", {
    refresh(frm) {
        const allowed = new Set([
            "System Manager",
            "NKT OWNER",
            "NKT ADMINISTRATOR",
            "NKT Credit Controller"
        ]);

        const authorized = (frappe.user_roles || []).some(
            role => allowed.has(role)
        ) || frappe.session.user === "Administrator";

        if (
            !authorized ||
            frm.doc.docstatus !== 1 ||
            frm.doc.application_status !== "Applied"
        ) {
            return;
        }

        frm.add_custom_button(
            __("Reverse / Correct Advance"),
            () => {
                const d = new frappe.ui.Dialog({
                    title: __("Correct Customer Advance Application"),
                    fields: [
                        {
                            fieldname: "reason",
                            label: __("Correction Reason"),
                            fieldtype: "Small Text",
                            reqd: 1
                        },
                        {
                            fieldname: "reapply",
                            label: __("Reapply to Different Account Order"),
                            fieldtype: "Check",
                            default: 0
                        },
                        {
                            fieldname: "target_order",
                            label: __("Target Customer Order"),
                            fieldtype: "Link",
                            options: "NKT Customer Order",
                            depends_on: "eval:doc.reapply==1",
                            mandatory_depends_on: "eval:doc.reapply==1",
                            get_query() {
                                return {
                                    filters: {
                                        customer: frm.doc.customer,
                                        company: frm.doc.company,
                                        docstatus: 1
                                    }
                                };
                            }
                        }
                    ],
                    primary_action_label: __("Apply Correction"),
                    primary_action(values) {
                        d.hide();

                        frappe.call({
                            method:
                                "nkt_operations.nkt_store_operations." +
                                "nkt_c5_4_advance_correction." +
                                "reverse_advance_application",
                            type: "POST",
                            args: {
                                application: frm.doc.name,
                                reason: values.reason,
                                reapply_to_order:
                                    values.reapply
                                        ? values.target_order
                                        : null
                            },
                            freeze: true,
                            freeze_message: __("Applying controlled correction..."),
                            callback(r) {
                                if (r.message) {
                                    frappe.msgprint({
                                        title: __("Advance Correction Completed"),
                                        indicator: "green",
                                        message:
                                            __("Application {0} is now Reversed.", [
                                                frm.doc.name
                                            ]) +
                                            "<br>" +
                                            __("No new Payment Receipt or Cashier Movement was created.")
                                    });
                                    frm.reload_doc();
                                }
                            }
                        });
                    }
                });

                d.show();
            },
            __("Actions")
        );
    }
});
'''


def _order_client_script():
    return r'''
frappe.ui.form.on("NKT Customer Order", {
    refresh(frm) {
        const allowed = new Set([
            "System Manager",
            "NKT OWNER",
            "NKT ADMINISTRATOR",
            "NKT Credit Controller"
        ]);

        const authorized = (frappe.user_roles || []).some(
            role => allowed.has(role)
        ) || frappe.session.user === "Administrator";

        if (!authorized || frm.is_new()) {
            return;
        }

        frm.add_custom_button(
            __("Advance Applications"),
            () => {
                frappe.route_options = {
                    customer_order: frm.doc.name
                };
                frappe.set_route(
                    "List",
                    "NKT Customer Advance Application"
                );
            },
            __("Payment")
        );

        if (
            frm.doc.docstatus === 1 &&
            Number(frm.doc.custom_nkt_advance_auto_apply_hold || 0) === 1
        ) {
            frm.add_custom_button(
                __("Release Advance Auto-Apply Hold"),
                () => {
                    frappe.prompt(
                        [
                            {
                                fieldname: "reason",
                                label: __("Reason"),
                                fieldtype: "Small Text",
                                reqd: 1
                            },
                            {
                                fieldname: "apply_now",
                                label: __("Apply Available Advance Now"),
                                fieldtype: "Check",
                                default: 0,
                                description:
                                    __("Leave unchecked if you only want to remove the correction hold.")
                            }
                        ],
                        values => {
                            frappe.call({
                                method:
                                    "nkt_operations.nkt_store_operations." +
                                    "nkt_c5_4_advance_correction." +
                                    "release_order_advance_hold",
                                type: "POST",
                                args: {
                                    customer_order: frm.doc.name,
                                    reason: values.reason,
                                    apply_now: values.apply_now ? 1 : 0
                                },
                                freeze: true,
                                callback(r) {
                                    if (r.message) {
                                        frm.reload_doc();
                                    }
                                }
                            });
                        },
                        __("Release Advance Hold"),
                        __("Release")
                    );
                },
                __("Payment")
            );
        }
    }
});
'''


def _ensure_client_script(name, dt, script):
    if frappe.db.exists("Client Script", name):
        doc = frappe.get_doc("Client Script", name)
        doc.dt = dt
        doc.view = "Form"
        doc.enabled = 1
        doc.script = script
        doc.flags.ignore_permissions = True
        doc.save()
    else:
        doc = frappe.new_doc("Client Script")
        doc.name = name
        doc.dt = dt
        doc.view = "Form"
        doc.enabled = 1
        doc.script = script
        doc.flags.ignore_permissions = True
        doc.insert()

    return doc.name


def install():
    receipts_before = frappe.db.count("NKT Payment Receipt")
    movements_before = frappe.db.count("NKT Cashier Movement")
    applications_before = frappe.db.count(APPLICATION)

    _ensure_custom_fields()
    _ensure_application_permissions()

    _ensure_client_script(
        APP_SCRIPT,
        APPLICATION,
        _application_client_script(),
    )

    _ensure_client_script(
        ORDER_SCRIPT,
        ORDER,
        _order_client_script(),
    )

    frappe.db.commit()
    frappe.clear_cache()

    receipts_after = frappe.db.count("NKT Payment Receipt")
    movements_after = frappe.db.count("NKT Cashier Movement")
    applications_after = frappe.db.count(APPLICATION)

    if receipts_after != receipts_before:
        frappe.throw(
            _("C5.4 install safety stop: Payment Receipt count changed.")
        )

    if movements_after != movements_before:
        frappe.throw(
            _("C5.4 install safety stop: Cashier Movement count changed.")
        )

    if applications_after != applications_before:
        frappe.throw(
            _(
                "C5.4 install safety stop: Customer Advance Application "
                "count changed."
            )
        )

    return {
        "version": "V2.0C.5.4",
        "installed": True,
        "client_scripts": [
            APP_SCRIPT,
            ORDER_SCRIPT,
        ],
        "payment_receipt_count_unchanged": True,
        "cashier_movement_count_unchanged": True,
        "advance_application_count_unchanged": True,
    }


def verify():
    required_app_fields = [
        "custom_nkt_reversed_on",
        "custom_nkt_reversed_by",
        "custom_nkt_reversal_reason",
    ]

    required_order_fields = [
        "custom_nkt_advance_auto_apply_hold",
        "custom_nkt_advance_hold_reason",
    ]

    app_meta = frappe.get_meta(APPLICATION)
    order_meta = frappe.get_meta(ORDER)

    field_checks = {
        fieldname: app_meta.has_field(fieldname)
        for fieldname in required_app_fields
    }

    field_checks.update(
        {
            fieldname: order_meta.has_field(fieldname)
            for fieldname in required_order_fields
        }
    )

    scripts = {}
    for name in (APP_SCRIPT, ORDER_SCRIPT):
        scripts[name] = {
            "exists": bool(frappe.db.exists("Client Script", name)),
            "enabled": int(
                frappe.db.get_value(
                    "Client Script",
                    name,
                    "enabled",
                )
                or 0
            )
            if frappe.db.exists("Client Script", name)
            else 0,
        }

    auto_fn = frappe.get_attr(
        AUTO_MODULE + ".auto_apply_customer_advance_for_order"
    )

    checks = {
        "all_custom_fields_present": all(field_checks.values()),
        "application_script_enabled": (
            scripts[APP_SCRIPT]["exists"]
            and scripts[APP_SCRIPT]["enabled"] == 1
        ),
        "order_script_enabled": (
            scripts[ORDER_SCRIPT]["exists"]
            and scripts[ORDER_SCRIPT]["enabled"] == 1
        ),
        "auto_apply_function_importable": bool(auto_fn),
        "reversal_endpoint_importable": bool(
            reverse_advance_application
        ),
        "release_hold_endpoint_importable": bool(
            release_order_advance_hold
        ),
    }

    return {
        "version": "V2.0C.5.4",
        "authorized_roles": sorted(AUTHORIZED_ROLES),
        "fields": field_checks,
        "client_scripts": scripts,
        "checks": checks,
        "business_record_counts": {
            "NKT Payment Receipt": frappe.db.count(
                "NKT Payment Receipt"
            ),
            "NKT Cashier Movement": frappe.db.count(
                "NKT Cashier Movement"
            ),
            "NKT Customer Advance Application": frappe.db.count(
                APPLICATION
            ),
        },
        "passed": all(checks.values()),
    }
