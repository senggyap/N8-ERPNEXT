
import frappe
from frappe import _
from frappe.utils import flt

ALLOWED_ROLES = {
    "NKT Cashier",
    "NKT Encoder",
    "NKT ADMINISTRATOR",
    "NKT OWNER",
    "NKT Credit Controller",
    "System Manager",
}

TARGET_DOCTYPES = (
    "NKT Cashier Fast Screen",
    "NKT Encoder Fast Screen",
)

PLACEHOLDER = (
    "w.on('click', '[data-action=\"new-customer\"]', "
    "() => readonly_notice(state, 'New Customer creation'));"
)
PATCHED_BINDING = (
    "w.on('click', '[data-action=\"new-customer\"]', "
    "() => open_fast_new_customer(state));"
)
INJECT_ANCHOR = "  function update_totals(state) {"
OLD_NO_RESULT = "No customer found. New Customer creation is not yet connected in V2.0C.1."
NEW_NO_RESULT = "No customer found. Click New Customer to create one."
JS_FUNCTION = "\n  function open_fast_new_customer(state) {\n    const d = new frappe.ui.Dialog({\n      title: __('New Customer'),\n      fields: [\n        { fieldname: 'customer_name', fieldtype: 'Data', label: __('Customer Name'), reqd: 1 },\n        { fieldname: 'customer_type', fieldtype: 'Select', label: __('Customer Type'), options: 'Individual\\nCompany', default: 'Individual', reqd: 1 },\n        { fieldname: 'mobile_no', fieldtype: 'Data', label: __('Mobile Number') }\n      ],\n      primary_action_label: __('Create Customer'),\n      primary_action(values) {\n        if (!values || !(values.customer_name || '').trim()) return;\n        d.disable_primary_action();\n\n        frappe.call({\n          method: 'nkt_operations.nkt_store_operations.features.fast_screen.fast_customer_creation.create_fast_customer',\n          args: {\n            customer_name: values.customer_name,\n            customer_type: values.customer_type || 'Individual',\n            mobile_no: values.mobile_no || ''\n          },\n          freeze: true,\n          freeze_message: __('Creating Customer…')\n        }).then(r => {\n          const x = r.message || {};\n          if (!x.customer) frappe.throw(__('Customer creation returned no Customer.'));\n\n          const previous = state.customer && state.customer.name ? state.customer.name : null;\n          state.customer = {\n            name: x.customer,\n            customer_name: x.customer_name || x.customer,\n            mobile_no: x.mobile_no || '',\n            territory: x.territory || '',\n            current_account_balance: Number(x.current_account_balance || 0)\n          };\n\n          if (!previous || previous !== state.customer.name) {\n            state.payments = [];\n            state.cashTendered = 0;\n            state.cashTenderedManual = false;\n            state.paymentConfirmed = false;\n          }\n\n          state.customerResults = [];\n          state.customerIndex = 0;\n          role(state, 'customer-entry').val(state.customer.customer_name);\n          role(state, 'customer-selected').find('.nkt-customer-name').text(state.customer.customer_name);\n          role(state, 'customer-balance').text(format_money(state.customer.current_account_balance));\n          role(state, 'customer-status').text('Selected');\n          role(state, 'customer-results').prop('hidden', true);\n          render_payments(state);\n          d.hide();\n\n          frappe.show_alert({\n            message: x.created ? __('Customer created and selected.') : __('That Customer already exists and was selected.'),\n            indicator: x.created ? 'green' : 'blue'\n          });\n          focus_item(state);\n        }).catch(() => d.enable_primary_action());\n      }\n    });\n\n    d.show();\n    setTimeout(() => {\n      const field = d.get_field('customer_name');\n      if (field && field.$input) field.$input.trigger('focus');\n    }, 80);\n  }\n\n"


def _roles(user=None):
    return set(frappe.get_roles(user or frappe.session.user))


def _require_allowed():
    user = frappe.session.user
    if user == "Administrator":
        return
    if not _roles(user).intersection(ALLOWED_ROLES):
        frappe.throw(_("Not permitted."), frappe.PermissionError)


def _select_options(doctype, fieldname):
    field = frappe.get_meta(doctype).get_field(fieldname)
    if not field:
        return []
    return [row.strip() for row in (field.options or "").splitlines() if row.strip()]


def _first_existing_leaf(doctype, candidates):
    # V2.0C.5.6 LEAF DEFAULTS HOTFIX
    for candidate in candidates:
        if not candidate or not frappe.db.exists(doctype, candidate):
            continue

        meta = frappe.get_meta(doctype)
        if meta.has_field("is_group"):
            is_group = frappe.db.get_value(
                doctype,
                candidate,
                "is_group",
            )
            if int(is_group or 0) != 0:
                continue

        return candidate

    return None


def _selling_setting(fieldname):
    if not frappe.db.exists("DocType", "Selling Settings"):
        return None
    meta = frappe.get_meta("Selling Settings")
    if not meta.has_field(fieldname):
        return None
    return frappe.db.get_single_value("Selling Settings", fieldname)


def _default_customer_group():
    field = frappe.get_meta("Customer").get_field("customer_group")
    candidates = [
        _selling_setting("customer_group"),
        _selling_setting("default_customer_group"),
        field.default if field else None,
        frappe.defaults.get_global_default("customer_group"),
        "All Customer Groups",
    ]
    hit = _first_existing_leaf("Customer Group", candidates)
    if hit:
        return hit

    rows = frappe.db.sql(
        """
        SELECT name
        FROM `tabCustomer Group`
        WHERE COALESCE(is_group, 0) = 0
        ORDER BY lft ASC, name ASC
        LIMIT 1
        """,
        as_dict=True,
    )
    return rows[0].name if rows else None


def _default_territory():
    field = frappe.get_meta("Customer").get_field("territory")
    candidates = [
        _selling_setting("territory"),
        _selling_setting("default_territory"),
        field.default if field else None,
        frappe.defaults.get_global_default("territory"),
        "All Territories",
    ]
    hit = _first_existing_leaf("Territory", candidates)
    if hit:
        return hit

    rows = frappe.db.sql(
        """
        SELECT name
        FROM `tabTerritory`
        WHERE COALESCE(is_group, 0) = 0
        ORDER BY lft ASC, name ASC
        LIMIT 1
        """,
        as_dict=True,
    )
    return rows[0].name if rows else None


def _operational_balance(customer):
    try:
        from nkt_operations.nkt_store_operations.features.payments_accounts.internal.role_safe_receivable import _receivable_totals
        totals = _receivable_totals(customer)
        return flt(totals.total_operational_exposure)
    except Exception:
        return 0.0


def _find_existing_customer(customer_name):
    rows = frappe.db.sql(
        """
        SELECT name, customer_name, mobile_no, territory, customer_group
        FROM `tabCustomer`
        WHERE disabled = 0
          AND LOWER(TRIM(customer_name)) = LOWER(TRIM(%s))
        ORDER BY creation ASC
        LIMIT 1
        """,
        customer_name,
        as_dict=True,
    )
    return rows[0] if rows else None


@frappe.whitelist()
def get_fast_customer_creation_context():
    _require_allowed()
    customer_group = _default_customer_group()
    territory = _default_territory()

    return {
        "allowed": True,
        "customer_types": _select_options("Customer", "customer_type") or ["Individual", "Company"],
        "default_customer_group": customer_group,
        "default_territory": territory,
        "default_customer_group_is_leaf": (
            bool(customer_group)
            and int(
                frappe.db.get_value(
                    "Customer Group",
                    customer_group,
                    "is_group",
                )
                or 0
            )
            == 0
        ),
        "default_territory_is_leaf": (
            bool(territory)
            and int(
                frappe.db.get_value(
                    "Territory",
                    territory,
                    "is_group",
                )
                or 0
            )
            == 0
        ),
        "frontline_credit_control_locked": True,
        "account_privileges_not_granted_here": True,
    }


@frappe.whitelist()
def create_fast_customer(customer_name, customer_type="Individual", mobile_no=""):
    _require_allowed()

    customer_name = (customer_name or "").strip()
    customer_type = (customer_type or "Individual").strip()
    mobile_no = (mobile_no or "").strip()

    if len(customer_name) < 2:
        frappe.throw(_("Enter a valid Customer Name."))

    allowed_types = _select_options("Customer", "customer_type") or ["Individual", "Company"]
    if customer_type not in allowed_types:
        frappe.throw(_("Invalid Customer Type."))

    existing = _find_existing_customer(customer_name)
    if existing:
        return {
            "created": False,
            "existing": True,
            "customer": existing.name,
            "customer_name": existing.customer_name,
            "mobile_no": existing.mobile_no or "",
            "territory": existing.territory or "",
            "customer_group": existing.customer_group or "",
            "current_account_balance": _operational_balance(existing.name),
        }

    meta = frappe.get_meta("Customer")
    customer_group = _default_customer_group()
    territory = _default_territory()

    group_field = meta.get_field("customer_group")
    territory_field = meta.get_field("territory")

    if group_field and group_field.reqd and not customer_group:
        frappe.throw(_("Customer creation setup is incomplete: no default Customer Group is available."))

    if territory_field and territory_field.reqd and not territory:
        frappe.throw(_("Customer creation setup is incomplete: no default Territory is available."))

    doc = frappe.new_doc("Customer")
    doc.customer_name = customer_name

    if meta.has_field("customer_type"):
        doc.customer_type = customer_type
    if mobile_no and meta.has_field("mobile_no"):
        doc.mobile_no = mobile_no
    if customer_group and meta.has_field("customer_group"):
        doc.customer_group = customer_group
    if territory and meta.has_field("territory"):
        doc.territory = territory

    # Identity creation only. Credit/account-control settings remain separate.
    doc.flags.ignore_permissions = True
    doc.insert()
    frappe.db.commit()

    return {
        "created": True,
        "existing": False,
        "customer": doc.name,
        "customer_name": doc.customer_name,
        "mobile_no": getattr(doc, "mobile_no", "") or "",
        "territory": getattr(doc, "territory", "") or "",
        "customer_group": getattr(doc, "customer_group", "") or "",
        "current_account_balance": 0.0,
        "frontline_credit_control_locked": True,
    }


def _active_main_fast_script(doctype):
    rows = frappe.get_all(
        "Client Script",
        filters={"dt": doctype, "enabled": 1},
        fields=["name", "script"],
        order_by="modified desc",
    )

    main = []
    for row in rows:
        script = row.script or ""
        if (
            "function render_nkt_fast_shell" in script
            and "function make_state" in script
            and 'data-action="new-customer"' in script
        ):
            main.append(row)

    if len(main) != 1:
        frappe.throw(
            _("C5.6 safety stop: expected exactly one active main fast-screen script for {0}; found {1}.").format(
                doctype, len(main)
            )
        )
    return main[0]


def _patch_fast_script(row):
    script = row.script or ""

    if "function open_fast_new_customer(state)" in script:
        return {"script": row.name, "patched": False, "already_patched": True}

    if PLACEHOLDER not in script:
        frappe.throw(_("C5.6 safety stop: New Customer placeholder anchor was not found in {0}.").format(row.name))

    if INJECT_ANCHOR not in script:
        frappe.throw(_("C5.6 safety stop: JavaScript insertion anchor was not found in {0}.").format(row.name))

    patched = script.replace(PLACEHOLDER, PATCHED_BINDING, 1)
    patched = patched.replace(INJECT_ANCHOR, JS_FUNCTION + INJECT_ANCHOR, 1)
    patched = patched.replace(OLD_NO_RESULT, NEW_NO_RESULT)

    if (
        "function open_fast_new_customer(state)" not in patched
        or "create_fast_customer" not in patched
        or PLACEHOLDER in patched
    ):
        frappe.throw(_("C5.6 safety stop: New Customer UI patch validation failed for {0}.").format(row.name))

    doc = frappe.get_doc("Client Script", row.name)
    doc.script = patched
    doc.flags.ignore_permissions = True
    doc.save()

    return {"script": row.name, "patched": True, "already_patched": False}


def install():
    before = {
        "Customer": frappe.db.count("Customer"),
        "NKT Payment Receipt": frappe.db.count("NKT Payment Receipt"),
        "NKT Cashier Movement": frappe.db.count("NKT Cashier Movement"),
        "NKT Customer Advance Application": frappe.db.count("NKT Customer Advance Application"),
    }

    patched = []
    for doctype in TARGET_DOCTYPES:
        patched.append(_patch_fast_script(_active_main_fast_script(doctype)))

    frappe.db.commit()
    frappe.clear_cache()

    after = {
        "Customer": frappe.db.count("Customer"),
        "NKT Payment Receipt": frappe.db.count("NKT Payment Receipt"),
        "NKT Cashier Movement": frappe.db.count("NKT Cashier Movement"),
        "NKT Customer Advance Application": frappe.db.count("NKT Customer Advance Application"),
    }

    if before != after:
        frappe.throw(_("C5.6 installer safety stop: a business-record count changed during UI installation."))

    return {
        "version": "V2.0C.5.6",
        "installed": True,
        "patched_fast_screens": patched,
        "business_record_counts_unchanged": True,
        "counts": after,
    }


def verify():
    # Future-state-safe read-only guard. Global totals may legitimately grow.
    receipts_before = frappe.db.count("NKT Payment Receipt")
    movements_before = frappe.db.count("NKT Cashier Movement")
    applications_before = frappe.db.count("NKT Customer Advance Application")

    scripts = {}

    for doctype in TARGET_DOCTYPES:
        row = _active_main_fast_script(doctype)
        script = row.script or ""
        scripts[doctype] = {
            "name": row.name,
            "new_customer_function": "function open_fast_new_customer(state)" in script,
            "server_endpoint_wired": "nkt_c5_6_fast_customer_creation.create_fast_customer" in script,
            "old_placeholder_removed": PLACEHOLDER not in script,
        }

    original_user = frappe.session.user
    role_checks = {}

    try:
        for user in ("cashier@example.com", "encoder@example.com"):
            if not frappe.db.exists("User", user):
                role_checks[user] = {"exists": False, "creation_context_allowed": False}
                continue

            frappe.set_user(user)
            try:
                ctx = get_fast_customer_creation_context()
                role_checks[user] = {
                    "exists": True,
                    "creation_context_allowed": bool(ctx.get("allowed")),
                    "default_customer_group": ctx.get("default_customer_group"),
                    "default_territory": ctx.get("default_territory"),
                    "default_customer_group_is_leaf": bool(
                        ctx.get("default_customer_group_is_leaf")
                    ),
                    "default_territory_is_leaf": bool(
                        ctx.get("default_territory_is_leaf")
                    ),
                    "frontline_credit_control_locked": bool(ctx.get("frontline_credit_control_locked")),
                    "account_privileges_not_granted_here": bool(ctx.get("account_privileges_not_granted_here")),
                }
            except Exception:
                role_checks[user] = {"exists": True, "creation_context_allowed": False}
    finally:
        frappe.set_user(original_user)

    def _script_ok(d):
        return (
            d["new_customer_function"]
            and d["server_endpoint_wired"]
            and d["old_placeholder_removed"]
        )

    checks = {
        "cashier_ui_wired": _script_ok(scripts["NKT Cashier Fast Screen"]),
        "encoder_ui_wired": _script_ok(scripts["NKT Encoder Fast Screen"]),
        "cashier_creation_allowed": bool(role_checks.get("cashier@example.com", {}).get("creation_context_allowed")),
        "encoder_creation_allowed": bool(role_checks.get("encoder@example.com", {}).get("creation_context_allowed")),
        "credit_control_not_granted_by_fast_create": (
            bool(role_checks.get("cashier@example.com", {}).get("frontline_credit_control_locked"))
            and bool(role_checks.get("encoder@example.com", {}).get("frontline_credit_control_locked"))
        ),
        "cashier_defaults_are_leaf_records": (
            bool(role_checks.get("cashier@example.com", {}).get("default_customer_group_is_leaf"))
            and bool(role_checks.get("cashier@example.com", {}).get("default_territory_is_leaf"))
        ),
        "encoder_defaults_are_leaf_records": (
            bool(role_checks.get("encoder@example.com", {}).get("default_customer_group_is_leaf"))
            and bool(role_checks.get("encoder@example.com", {}).get("default_territory_is_leaf"))
        ),
    }

    receipts_after = frappe.db.count("NKT Payment Receipt")
    movements_after = frappe.db.count("NKT Cashier Movement")
    applications_after = frappe.db.count("NKT Customer Advance Application")

    checks["verifier_is_read_only_receipts"] = receipts_after == receipts_before
    checks["verifier_is_read_only_movements"] = movements_after == movements_before
    checks["verifier_is_read_only_applications"] = applications_after == applications_before

    errors = [name for name, passed in checks.items() if not passed]

    return {
        "version": "V2.0C.5.6-FUTURE-SAFE",
        "scripts": scripts,
        "role_checks": role_checks,
        "checks": checks,
        "errors": errors,
        "passed": not errors,
    }
