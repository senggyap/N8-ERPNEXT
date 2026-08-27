
import frappe
from frappe import _
from frappe.utils import flt

TOLERANCE = 0.005

PRIVILEGED_ROLES = {
    "System Manager",
    "NKT OWNER",
    "NKT ADMINISTRATOR",
    "NKT Credit Controller",
}

FRONTLINE_ROLES = {
    "NKT Encoder",
    "NKT Cashier",
}

ENCODER_OLD_SCRIPT = "NKT Encoder Fast Screen V2.0C.3"
ENCODER_NEW_SCRIPT = "NKT Encoder Fast Screen V2.0C.5.5 Role Safe Receivable"

CUSTOMER_SCRIPT = "NKT C5.5 Customer Exposure Visibility"
ORDER_SCRIPT = "NKT C5.5 Encoder Order Internal Visibility"
RECEIVABLE_SCRIPT = "NKT C5.5 Encoder Receivable Internal Visibility"

CUSTOMER = "TEST - ACCOUNT CUSTOMER"


def _roles(user=None):
    return set(frappe.get_roles(user or frappe.session.user))


def _is_privileged(user=None):
    user = user or frappe.session.user
    return user == "Administrator" or bool(
        _roles(user).intersection(PRIVILEGED_ROLES)
    )


def _is_frontline(user=None):
    return bool(_roles(user).intersection(FRONTLINE_ROLES))


def _require_customer_visibility_access():
    if _is_privileged() or _is_frontline():
        return
    frappe.throw(_("Not permitted."), frappe.PermissionError)


def _customer_name(customer):
    return (
        frappe.db.get_value("Customer", customer, "customer_name")
        or customer
    )


def _receivable_totals(customer):
    if not frappe.db.exists("DocType", "NKT Customer Receivable"):
        return frappe._dict(
            official_receivable=0,
            pending_internal=0,
            total_operational_exposure=0,
        )

    row = frappe.db.sql(
        """
        SELECT
            COALESCE(SUM(
                CASE
                    WHEN r.credit_control_status = 'Approved'
                     AND COALESCE(r.status, '') <> 'Cancelled'
                     AND COALESCE(r.outstanding_amount, 0) > 0
                    THEN r.outstanding_amount
                    ELSE 0
                END
            ), 0) AS official_receivable,

            COALESCE(SUM(
                CASE
                    WHEN r.credit_control_status = 'Pending Approval'
                     AND COALESCE(r.status, '') <> 'Cancelled'
                     AND COALESCE(r.outstanding_amount, 0) > 0
                     AND EXISTS (
                        SELECT 1
                        FROM `tabNKT Customer Order` o
                        WHERE o.name = r.customer_order
                          AND o.docstatus = 1
                          AND COALESCE(o.matched_cashier_sale, '') <> ''
                          AND COALESCE(
                                o.cashier_reconciliation_status,
                                ''
                              ) LIKE 'Matched%%'
                     )
                    THEN r.outstanding_amount
                    ELSE 0
                END
            ), 0) AS pending_internal
        FROM `tabNKT Customer Receivable` r
        WHERE r.customer = %s
          AND r.docstatus <> 2
        """,
        customer,
        as_dict=True,
    )[0]

    official = flt(row.official_receivable)
    pending = flt(row.pending_internal)

    return frappe._dict(
        official_receivable=official,
        pending_internal=pending,
        total_operational_exposure=official + pending,
    )


def _available_customer_advance(customer):
    if not frappe.db.exists("DocType", "NKT Customer Advance"):
        return 0.0

    return flt(
        frappe.db.sql(
            """
            SELECT COALESCE(SUM(available_advance_amount), 0)
            FROM `tabNKT Customer Advance`
            WHERE customer = %s
              AND docstatus = 1
              AND COALESCE(available_advance_amount, 0) > 0
            """,
            customer,
        )[0][0]
    )


@frappe.whitelist()
def get_operational_customer_receivable(customer):
    _require_customer_visibility_access()

    if not frappe.db.exists("Customer", customer):
        frappe.throw(_("Customer does not exist."))

    totals = _receivable_totals(customer)

    return {
        "customer": customer,
        "customer_name": _customer_name(customer),
        "customer_receivable": totals.total_operational_exposure,
    }


@frappe.whitelist()
def get_customer_receivable_visibility(customer):
    _require_customer_visibility_access()

    if not frappe.db.exists("Customer", customer):
        frappe.throw(_("Customer does not exist."))

    totals = _receivable_totals(customer)

    if not _is_privileged():
        return {
            "customer": customer,
            "customer_name": _customer_name(customer),
            "customer_receivable": totals.total_operational_exposure,
            "visibility": "operational_only",
        }

    return {
        "customer": customer,
        "customer_name": _customer_name(customer),
        "total_operational_exposure": totals.total_operational_exposure,
        "official_receivable": totals.official_receivable,
        "pending_internal": totals.pending_internal,
        "available_customer_advance": _available_customer_advance(customer),
        "visibility": "owner_control",
    }


def _ensure_client_script(name, dt, script):
    if frappe.db.exists("Client Script", name):
        doc = frappe.get_doc("Client Script", name)
        doc.dt = dt
        doc.view = "Form"
        doc.enabled = 1
        doc.script = script
        doc.flags.ignore_permissions = True
        doc.save()
        return doc.name

    doc = frappe.new_doc("Client Script")
    doc.name = name
    doc.dt = dt
    doc.view = "Form"
    doc.enabled = 1
    doc.script = script
    doc.flags.ignore_permissions = True
    doc.insert()
    return doc.name


def _build_encoder_script():
    source_name = None

    if frappe.db.exists("Client Script", ENCODER_NEW_SCRIPT):
        source_name = ENCODER_NEW_SCRIPT
    elif frappe.db.exists("Client Script", ENCODER_OLD_SCRIPT):
        source_name = ENCODER_OLD_SCRIPT

    if not source_name:
        frappe.throw(
            _(
                "Cannot install C5.5: accepted Encoder Fast Screen "
                "V2.0C.3 Client Script was not found."
            )
        )

    source = frappe.db.get_value(
        "Client Script",
        source_name,
        "script",
    ) or ""

    if source_name == ENCODER_OLD_SCRIPT:
        required_tokens = [
            "Current Account Balance:",
            "current_account_balance",
            "function choose_customer",
            "function render_customer_results",
        ]
        missing = [
            token for token in required_tokens if token not in source
        ]
        if missing:
            frappe.throw(
                _(
                    "C5.5 safety stop: Encoder Fast Screen source no "
                    "longer matches accepted V2.0C.3 anchors: {0}"
                ).format(", ".join(missing))
            )

    patched = source.replace(
        "nktFastEncoderV20C3",
        "nktFastEncoderV20C55RoleSafe",
    )
    patched = patched.replace(
        "V2.0C.3 LIVE — RECONCILIATION + CONTROL PARITY",
        "V2.0C.5.5 LIVE — ROLE-SAFE CUSTOMER RECEIVABLE",
    )
    patched = patched.replace(
        "Current Account Balance:",
        "Customer Receivable:",
    )
    patched = patched.replace(
        "Balance ${format_money(x.current_account_balance)}",
        "Receivable ${format_money(x.current_account_balance)}",
    )

    if "Customer Receivable:" not in patched:
        frappe.throw(
            _("C5.5 safety stop: Encoder receivable label was not patched.")
        )

    _ensure_client_script(
        ENCODER_NEW_SCRIPT,
        "NKT Encoder Fast Screen",
        patched,
    )

    frappe.db.sql(
        """
        UPDATE `tabClient Script`
        SET enabled = CASE WHEN name = %s THEN 1 ELSE 0 END
        WHERE dt = 'NKT Encoder Fast Screen'
          AND name LIKE 'NKT Encoder Fast Screen%%'
        """,
        ENCODER_NEW_SCRIPT,
    )

    return ENCODER_NEW_SCRIPT


def _customer_client_script():
    return r"""
frappe.ui.form.on("Customer", {
    refresh(frm) {
        const roles = new Set(frappe.user_roles || []);

        const privileged =
            frappe.session.user === "Administrator" ||
            roles.has("System Manager") ||
            roles.has("NKT OWNER") ||
            roles.has("NKT ADMINISTRATOR") ||
            roles.has("NKT Credit Controller");

        const encoder = roles.has("NKT Encoder") && !privileged;

        const hideIfPresent = (fieldname, hidden) => {
            if (frm.fields_dict[fieldname]) {
                frm.set_df_property(fieldname, "hidden", hidden ? 1 : 0);
            }
        };

        if (encoder) {
            if (frm.fields_dict.custom_nkt_current_account_balance) {
                frm.set_df_property(
                    "custom_nkt_current_account_balance",
                    "label",
                    __("Customer Receivable")
                );
            }

            [
                "custom_nkt_require_manual_account_approval",
                "custom_nkt_auto_approval_limit"
            ].forEach(f => hideIfPresent(f, true));

            return;
        }

        if (!privileged || frm.is_new()) return;

        if (frm.fields_dict.custom_nkt_current_account_balance) {
            frm.set_df_property(
                "custom_nkt_current_account_balance",
                "label",
                __("Operational Exposure")
            );
        }

        frappe.call({
            method:
                "nkt_operations.nkt_store_operations." +
                "nkt_c5_5_role_safe_receivable." +
                "get_customer_receivable_visibility",
            args: { customer: frm.doc.name },
            freeze: false
        }).then(r => {
            const x = r.message || {};
            if (x.visibility !== "owner_control") return;

            const money = value =>
                format_currency(
                    Number(value || 0),
                    frappe.defaults.get_default("currency") || "PHP"
                );

            frm.dashboard.add_indicator(
                __("Total Exposure: {0}", [
                    money(x.total_operational_exposure)
                ]),
                "blue"
            );

            frm.dashboard.add_indicator(
                __("Official Receivable: {0}", [
                    money(x.official_receivable)
                ]),
                "green"
            );

            frm.dashboard.add_indicator(
                __("Pending Internal: {0}", [
                    money(x.pending_internal)
                ]),
                Number(x.pending_internal || 0) > 0
                    ? "orange"
                    : "green"
            );

            frm.dashboard.add_indicator(
                __("Available Advance: {0}", [
                    money(x.available_customer_advance)
                ]),
                Number(x.available_customer_advance || 0) > 0
                    ? "blue"
                    : "grey"
            );
        });
    }
});
"""


def _order_client_script():
    return r"""
frappe.ui.form.on("NKT Customer Order", {
    refresh(frm) {
        const roles = new Set(frappe.user_roles || []);

        const privileged =
            frappe.session.user === "Administrator" ||
            roles.has("System Manager") ||
            roles.has("NKT OWNER") ||
            roles.has("NKT ADMINISTRATOR") ||
            roles.has("NKT Credit Controller");

        const encoder = roles.has("NKT Encoder") && !privileged;

        if (!encoder) return;

        [
            "requires_admin_confirmation",
            "admin_confirmation_status",
            "admin_confirmed_by",
            "admin_confirmed_on",
            "admin_confirmation_remarks",
            "cashier_reconciliation_section",
            "cashier_reconciliation_status",
            "matched_cashier_sale",
            "cashier_reconciliation_warning",
            "cashier_reconciled_on",
            "custom_nkt_manual_match_section",
            "custom_nkt_match_resolution_status",
            "custom_nkt_match_requested_by",
            "custom_nkt_match_resolved_by",
            "custom_nkt_match_resolved_on",
            "custom_nkt_match_resolution_reason",
            "custom_nkt_account_control_section",
            "custom_nkt_customer_receivable",
            "custom_nkt_account_credit_status",
            "custom_nkt_account_approval_mode",
            "custom_nkt_account_review_reason",
            "custom_nkt_account_approved_by",
            "custom_nkt_account_approved_on",
            "custom_nkt_account_approval_reason"
        ].forEach(fieldname => {
            if (frm.fields_dict[fieldname]) {
                frm.set_df_property(fieldname, "hidden", 1);
            }
        });
    }
});
"""


def _receivable_client_script():
    return r"""
frappe.ui.form.on("NKT Customer Receivable", {
    refresh(frm) {
        const roles = new Set(frappe.user_roles || []);

        const privileged =
            frappe.session.user === "Administrator" ||
            roles.has("System Manager") ||
            roles.has("NKT OWNER") ||
            roles.has("NKT ADMINISTRATOR") ||
            roles.has("NKT Credit Controller");

        const encoder = roles.has("NKT Encoder") && !privileged;

        if (!encoder) return;

        [
            "credit_control_section",
            "credit_control_status",
            "approved_by",
            "approved_on",
            "approval_reason",
            "approval_mode",
            "review_reason"
        ].forEach(fieldname => {
            if (frm.fields_dict[fieldname]) {
                frm.set_df_property(fieldname, "hidden", 1);
            }
        });
    }
});
"""


def install():
    receipts_before = frappe.db.count("NKT Payment Receipt")
    movements_before = frappe.db.count("NKT Cashier Movement")
    applications_before = frappe.db.count(
        "NKT Customer Advance Application"
    )

    encoder_script = _build_encoder_script()

    _ensure_client_script(
        CUSTOMER_SCRIPT,
        "Customer",
        _customer_client_script(),
    )
    _ensure_client_script(
        ORDER_SCRIPT,
        "NKT Customer Order",
        _order_client_script(),
    )
    _ensure_client_script(
        RECEIVABLE_SCRIPT,
        "NKT Customer Receivable",
        _receivable_client_script(),
    )

    frappe.db.commit()
    frappe.clear_cache()

    receipts_after = frappe.db.count("NKT Payment Receipt")
    movements_after = frappe.db.count("NKT Cashier Movement")
    applications_after = frappe.db.count(
        "NKT Customer Advance Application"
    )

    if receipts_after != receipts_before:
        frappe.throw(
            _("C5.5 safety stop: Payment Receipt count changed.")
        )
    if movements_after != movements_before:
        frappe.throw(
            _("C5.5 safety stop: Cashier Movement count changed.")
        )
    if applications_after != applications_before:
        frappe.throw(
            _(
                "C5.5 safety stop: Customer Advance Application "
                "count changed."
            )
        )

    return {
        "version": "V2.0C.5.5-FUTURE-SAFE",
        "installed": True,
        "encoder_fast_script": encoder_script,
        "role_visibility_scripts": [
            CUSTOMER_SCRIPT,
            ORDER_SCRIPT,
            RECEIVABLE_SCRIPT,
        ],
        "business_records_unchanged": True,
    }


def verify():
    # Future-state-safe read-only guard. Legitimate later business activity
    # may change global totals, so only prove this verifier itself does not.
    receipts_before = frappe.db.count("NKT Payment Receipt")
    movements_before = frappe.db.count("NKT Cashier Movement")
    applications_before = frappe.db.count("NKT Customer Advance Application")

    original_user = frappe.session.user
    owner_view = None
    encoder_view = None
    encoder_search = None

    try:
        frappe.set_user("Administrator")
        owner_view = get_customer_receivable_visibility(CUSTOMER)

        if frappe.db.exists("User", "encoder@example.com"):
            frappe.set_user("encoder@example.com")
            encoder_view = get_customer_receivable_visibility(CUSTOMER)

            from nkt_operations.nkt_store_operations.fast_screen_backend import (
                search_customers,
            )

            rows = search_customers(CUSTOMER, 5)
            encoder_search = next(
                (row for row in rows if row.name == CUSTOMER),
                None,
            )

    finally:
        frappe.set_user(original_user)

    # V2.0C.5.5 VERIFIER HOTFIX - ALLOW AUXILIARY ENCODER SCRIPTS
    # The Encoder doctype intentionally has auxiliary Client Scripts
    # (for example the accepted C4.2.1 Warehouse Change Link). Only the
    # main "NKT Encoder Fast Screen ..." family must have exactly one
    # active member.
    active_encoder_scripts = frappe.get_all(
        "Client Script",
        filters={
            "dt": "NKT Encoder Fast Screen",
            "enabled": 1,
        },
        pluck="name",
    )
    active_main_encoder_scripts = [
        name
        for name in active_encoder_scripts
        if name.startswith("NKT Encoder Fast Screen")
    ]

    expected_scripts = (
        CUSTOMER_SCRIPT,
        ORDER_SCRIPT,
        RECEIVABLE_SCRIPT,
    )

    script_checks = {
        name: bool(
            frappe.db.exists("Client Script", name)
            and int(
                frappe.db.get_value(
                    "Client Script",
                    name,
                    "enabled",
                )
                or 0
            )
            == 1
        )
        for name in expected_scripts
    }

    owner_total = flt(
        (owner_view or {}).get("total_operational_exposure")
    )
    owner_official = flt(
        (owner_view or {}).get("official_receivable")
    )
    owner_pending = flt(
        (owner_view or {}).get("pending_internal")
    )

    encoder_keys = set((encoder_view or {}).keys())

    checks = {
        "owner_has_internal_breakdown": (
            bool(owner_view)
            and owner_view.get("visibility") == "owner_control"
            and "official_receivable" in owner_view
            and "pending_internal" in owner_view
        ),
        "owner_total_equals_official_plus_pending": (
            abs(
                owner_total - (owner_official + owner_pending)
            )
            <= TOLERANCE
        ),
        "encoder_operational_only": (
            bool(encoder_view)
            and encoder_view.get("visibility") == "operational_only"
            and "customer_receivable" in encoder_view
        ),
        "encoder_internal_breakdown_not_returned": (
            "official_receivable" not in encoder_keys
            and "pending_internal" not in encoder_keys
            and "available_customer_advance" not in encoder_keys
        ),
        "encoder_search_matches_operational_total": (
            bool(encoder_search)
            and abs(
                flt(encoder_search.current_account_balance)
                - flt(encoder_view.get("customer_receivable"))
            )
            <= TOLERANCE
        ),
        "only_c5_5_main_encoder_fast_script_active": (
            active_main_encoder_scripts == [ENCODER_NEW_SCRIPT]
        ),
        "warehouse_change_helper_may_remain_active": (
            "NKT Encoder Warehouse Change Link V2.0C.4.2.1"
            in active_encoder_scripts
        ),
        "role_visibility_scripts_enabled": all(
            script_checks.values()
        ),
    }

    receipts_after = frappe.db.count("NKT Payment Receipt")
    movements_after = frappe.db.count("NKT Cashier Movement")
    applications_after = frappe.db.count("NKT Customer Advance Application")

    checks["verifier_is_read_only_receipts"] = receipts_after == receipts_before
    checks["verifier_is_read_only_movements"] = movements_after == movements_before
    checks["verifier_is_read_only_applications"] = applications_after == applications_before

    errors = [
        name for name, passed in checks.items() if not passed
    ]

    return {
        "version": "V2.0C.5.5-FUTURE-SAFE",
        "owner_view": owner_view,
        "encoder_view": encoder_view,
        "encoder_search_result": encoder_search,
        "active_encoder_scripts": active_encoder_scripts,
        "active_main_encoder_scripts": active_main_encoder_scripts,
        "role_visibility_scripts": script_checks,
        "checks": checks,
        "errors": errors,
        "passed": not errors,
    }
