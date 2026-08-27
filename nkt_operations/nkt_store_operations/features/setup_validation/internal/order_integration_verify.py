import frappe
from frappe.utils import flt

TOLERANCE = 0.005
ORDER = "NKT-ORD-00049"
RECEIVABLE = "NKT-REC-00011"
ADVANCE = "NKT-ADV-00004"
CUSTOMER = "TEST - ACCOUNT CUSTOMER"


def _eq(value, expected):
    return abs(flt(value) - flt(expected)) <= TOLERANCE


def verify():
    """
    Read-only one-time integration verifier for the accepted ORD-00049
    Credit Control -> automatic Customer Advance scenario.

    Global Payment Receipt / Cashier Movement counts are reported but are
    intentionally not frozen here. Compare them with the pre-approval
    baseline captured immediately before approval.
    """
    order = frappe.db.get_value(
        "NKT Customer Order",
        ORDER,
        [
            "name",
            "customer",
            "grand_total",
            "declared_account",
            "amount_paid",
            "amount_due",
            "payment_status",
            "status",
            "cashier_reconciliation_status",
            "matched_cashier_sale",
            "custom_nkt_account_credit_status",
        ],
        as_dict=True,
    )

    receivable = frappe.db.get_value(
        "NKT Customer Receivable",
        RECEIVABLE,
        [
            "name",
            "customer_order",
            "original_amount",
            "amount_paid",
            "outstanding_amount",
            "status",
            "credit_control_status",
        ],
        as_dict=True,
    )

    advance = frappe.db.get_value(
        "NKT Customer Advance",
        ADVANCE,
        [
            "name",
            "customer",
            "source_payment_receipt",
            "original_advance_amount",
            "applied_amount",
            "available_advance_amount",
            "advance_status",
        ],
        as_dict=True,
    )

    apps = frappe.get_all(
        "NKT Customer Advance Application",
        filters={
            "customer_order": ORDER,
            "application_status": "Applied",
            "docstatus": 1,
        },
        fields=[
            "name",
            "customer_advance",
            "source_payment_receipt",
            "applied_amount",
        ],
        order_by="creation asc",
    )
    app_total = sum(flt(row.applied_amount) for row in apps)

    c5_final = frappe.get_attr(
        "nkt_operations.nkt_store_operations."
        "nkt_c5_final_acceptance.run"
    )()
    c55 = frappe.get_attr(
        "nkt_operations.nkt_store_operations."
        "nkt_c5_5_role_safe_receivable.verify"
    )()
    c56 = frappe.get_attr(
        "nkt_operations.nkt_store_operations."
        "nkt_c5_6_fast_customer_creation.verify"
    )()

    owner = c55.get("owner_view") or {}
    encoder = c55.get("encoder_view") or {}

    checks = {
        "order_exists": bool(order),
        "order_approved": (
            bool(order)
            and order.custom_nkt_account_credit_status == "Approved"
        ),
        "order_remains_matched": (
            bool(order)
            and (order.cashier_reconciliation_status or "").startswith(
                "Matched"
            )
            and order.matched_cashier_sale == "NKT-CASH-00043"
        ),
        "order_due_is_7750": (
            bool(order) and _eq(order.amount_due, 7750)
        ),
        "receivable_exists": bool(receivable),
        "receivable_approved": (
            bool(receivable)
            and receivable.credit_control_status == "Approved"
        ),
        "receivable_outstanding_is_7750": (
            bool(receivable)
            and _eq(receivable.outstanding_amount, 7750)
        ),
        "advance_00004_fully_used": (
            bool(advance)
            and _eq(advance.original_advance_amount, 2000)
            and _eq(advance.applied_amount, 2000)
            and _eq(advance.available_advance_amount, 0)
            and advance.advance_status == "Fully Used"
        ),
        "ord49_active_advance_application_total_is_950": (
            _eq(app_total, 950)
        ),
        "ord49_application_uses_advance_00004": any(
            row.customer_advance == ADVANCE
            and _eq(row.applied_amount, 950)
            for row in apps
        ),
        "owner_official_receivable_is_7750": _eq(
            owner.get("official_receivable"), 7750
        ),
        "owner_pending_internal_is_zero": _eq(
            owner.get("pending_internal"), 0
        ),
        "owner_operational_exposure_is_7750": _eq(
            owner.get("total_operational_exposure"), 7750
        ),
        "encoder_operational_receivable_is_7750": _eq(
            encoder.get("customer_receivable"), 7750
        ),
        "c5_final_still_passes": bool(c5_final.get("passed")),
        "c5_5_still_passes": bool(c55.get("passed")),
        "c5_6_still_passes": bool(c56.get("passed")),
    }

    errors = [
        name for name, passed in checks.items() if not passed
    ]

    return {
        "version": "V2.0C.5.6-ORD49-INTEGRATION-VERIFY",
        "order": order,
        "receivable": receivable,
        "advance": advance,
        "active_advance_applications": apps,
        "payment_receipt_count": frappe.db.count(
            "NKT Payment Receipt"
        ),
        "cashier_movement_count": frappe.db.count(
            "NKT Cashier Movement"
        ),
        "advance_application_count": frappe.db.count(
            "NKT Customer Advance Application"
        ),
        "owner_view": owner,
        "encoder_view": encoder,
        "checks": checks,
        "errors": errors,
        "passed": not errors,
    }
