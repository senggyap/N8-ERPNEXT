
import frappe
from frappe.utils import flt

TOLERANCE = 0.005

CUSTOMER = "TEST - ACCOUNT CUSTOMER"
COMPANY = "NKT (Dev)"

ORDER = "NKT-ORD-00048"
RECEIVABLE = "NKT-REC-00010"

ADVANCE_1 = "NKT-ADV-00003"
ADVANCE_2 = "NKT-ADV-00004"

PAYMENT_1 = "NKT-PAY-00050"
PAYMENT_2 = "NKT-PAY-00051"

MOVEMENT_1 = "NKT-MOV-00050"
MOVEMENT_2 = "NKT-MOV-00051"

APPLICATION_REVERSED_TEST = "NKT-ADV-APP-00005"
APPLICATION_ACTIVE_2 = "NKT-ADV-APP-00006"

C5_2_VERIFY = (
    "nkt_operations.nkt_store_operations."
    "nkt_c5_2_auto_advance.verify"
)
C5_3_VERIFY = (
    "nkt_operations.nkt_store_operations."
    "nkt_c5_3_advance_statement.verify"
)
C5_4_VERIFY = (
    "nkt_operations.nkt_store_operations."
    "nkt_c5_4_advance_correction.verify"
)


def _eq(value, expected):
    return abs(flt(value) - flt(expected)) <= TOLERANCE


def _get(doctype, name, fields):
    if not frappe.db.exists(doctype, name):
        return None

    meta = frappe.get_meta(doctype)
    safe_fields = [
        fieldname
        for fieldname in fields
        if fieldname == "name" or meta.has_field(fieldname)
    ]

    if "name" not in safe_fields:
        safe_fields.insert(0, "name")

    return frappe.db.get_value(
        doctype,
        name,
        safe_fields,
        as_dict=True,
    )


def _application(name):
    return _get(
        "NKT Customer Advance Application",
        name,
        [
            "name",
            "customer_advance",
            "source_payment_receipt",
            "customer_order",
            "applied_amount",
            "application_status",
            "custom_nkt_reversed_on",
            "custom_nkt_reversed_by",
            "custom_nkt_reversal_reason",
        ],
    )


def run():
    # Strictly read-only: no insert/save/set_value/commit.
    receipts_before = frappe.db.count("NKT Payment Receipt")
    movements_before = frappe.db.count("NKT Cashier Movement")
    applications_before = frappe.db.count(
        "NKT Customer Advance Application"
    )

    c5_2 = frappe.get_attr(C5_2_VERIFY)()
    c5_3 = frappe.get_attr(C5_3_VERIFY)()
    c5_4 = frappe.get_attr(C5_4_VERIFY)()

    customer = _get(
        "Customer",
        CUSTOMER,
        [
            "name",
            "custom_nkt_current_account_balance",
            "custom_nkt_available_credit",
        ],
    )

    order = _get(
        "NKT Customer Order",
        ORDER,
        [
            "name",
            "customer",
            "company",
            "grand_total",
            "declared_account",
            "amount_paid",
            "amount_due",
            "payment_status",
            "status",
            "custom_nkt_account_credit_status",
            "cashier_reconciliation_status",
            "matched_cashier_sale",
            "custom_nkt_advance_auto_apply_hold",
        ],
    )

    receivable = _get(
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
    )

    advance_1 = _get(
        "NKT Customer Advance",
        ADVANCE_1,
        [
            "name",
            "customer",
            "company",
            "source_payment_receipt",
            "original_advance_amount",
            "applied_amount",
            "available_advance_amount",
            "advance_status",
        ],
    )

    advance_2 = _get(
        "NKT Customer Advance",
        ADVANCE_2,
        [
            "name",
            "customer",
            "company",
            "source_payment_receipt",
            "original_advance_amount",
            "applied_amount",
            "available_advance_amount",
            "advance_status",
        ],
    )

    # Use the actual live Payment Receipt field names.
    payment_1 = _get(
        "NKT Payment Receipt",
        PAYMENT_1,
        [
            "name",
            "customer",
            "total_payment",
            "receipt_status",
            "customer_advance_amount",
        ],
    )

    payment_2 = _get(
        "NKT Payment Receipt",
        PAYMENT_2,
        [
            "name",
            "customer",
            "total_payment",
            "receipt_status",
            "customer_advance_amount",
        ],
    )

    # Only existence is required for the two original movements, so avoid
    # assuming optional amount/status field names.
    movement_1 = _get(
        "NKT Cashier Movement",
        MOVEMENT_1,
        ["name"],
    )

    movement_2 = _get(
        "NKT Cashier Movement",
        MOVEMENT_2,
        ["name"],
    )

    app_05 = _application(APPLICATION_REVERSED_TEST)
    app_06 = _application(APPLICATION_ACTIVE_2)

    active_order_apps = frappe.get_all(
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

    reversed_order_apps = frappe.get_all(
        "NKT Customer Advance Application",
        filters={
            "customer_order": ORDER,
            "application_status": "Reversed",
            "docstatus": 1,
        },
        fields=["name", "applied_amount"],
        order_by="creation asc",
    )

    active_total = sum(
        flt(row.applied_amount)
        for row in active_order_apps
    )

    checks = {
        # C5.1
        "c5_1_payment_receipt_00050_exists": bool(payment_1),
        "c5_1_payment_receipt_00051_exists": bool(payment_2),
        "c5_1_cashier_movement_00050_exists": bool(movement_1),
        "c5_1_cashier_movement_00051_exists": bool(movement_2),
        "c5_1_receipt_00050_customer_correct": (
            bool(payment_1)
            and payment_1.customer == CUSTOMER
        ),
        "c5_1_receipt_00051_customer_correct": (
            bool(payment_2)
            and payment_2.customer == CUSTOMER
        ),
        "c5_1_receipt_00050_completed": (
            bool(payment_1)
            and payment_1.receipt_status == "Completed"
        ),
        "c5_1_receipt_00051_completed": (
            bool(payment_2)
            and payment_2.receipt_status == "Completed"
        ),
        "c5_1_advance_00003_original_3000": (
            bool(advance_1)
            and _eq(advance_1.original_advance_amount, 3000)
            and advance_1.source_payment_receipt == PAYMENT_1
        ),
        "c5_1_advance_00004_original_2000": (
            bool(advance_2)
            and _eq(advance_2.original_advance_amount, 2000)
            and advance_2.source_payment_receipt == PAYMENT_2
        ),

        # C5.2 / C5.2.1
        "c5_2_verifier_passed": bool(c5_2.get("passed")),
        "c5_2_order_00048_paid": (
            bool(order)
            and _eq(order.amount_paid, 2750)
            and _eq(order.amount_due, 0)
            and order.payment_status == "Paid"
            and order.custom_nkt_account_credit_status == "Approved"
            and (order.cashier_reconciliation_status or "").startswith(
                "Matched"
            )
        ),
        "c5_2_receivable_00010_paid": (
            bool(receivable)
            and _eq(receivable.amount_paid, 2750)
            and _eq(receivable.outstanding_amount, 0)
            and receivable.status == "Paid"
            and receivable.credit_control_status == "Approved"
        ),
        "c5_2_active_applications_total_2750": _eq(
            active_total,
            2750,
        ),
        "c5_2_advance_00003_fully_used": (
            bool(advance_1)
            and _eq(advance_1.available_advance_amount, 0)
            and advance_1.advance_status == "Fully Used"
        ),
        "c5_2_advance_00004_balance_consistent": (
            bool(advance_2)
            and _eq(advance_2.original_advance_amount, 2000)
            and flt(advance_2.applied_amount) + TOLERANCE >= 1050
            and flt(advance_2.applied_amount) <= 2000 + TOLERANCE
            and flt(advance_2.available_advance_amount) >= -TOLERANCE
            and _eq(
                flt(advance_2.applied_amount)
                + flt(advance_2.available_advance_amount),
                2000,
            )
            and (
                (
                    flt(advance_2.available_advance_amount)
                    <= TOLERANCE
                    and advance_2.advance_status == "Fully Used"
                )
                or (
                    flt(advance_2.available_advance_amount)
                    > TOLERANCE
                    and advance_2.advance_status == "Partially Used"
                )
            )
        ),

        # C5.3 dynamic current-state-safe checks.
        "c5_3_verifier_passed": bool(c5_3.get("passed")),
        "c5_3_historical_order_00048_still_proven": bool(
            c5_3.get("checks", {}).get(
                "order_00048_advance_reduces_ar_by_2750"
            )
        ),
        "c5_3_statement_math_consistent": bool(
            c5_3.get("checks", {}).get(
                "closing_balance_matches_statement_math"
            )
        ),
        "c5_3_available_advance_nonnegative": bool(
            c5_3.get("checks", {}).get(
                "available_advance_is_nonnegative"
            )
        ),

        # C5.4
        "c5_4_verifier_passed": bool(c5_4.get("passed")),
        "c5_4_test_reversal_audited": (
            bool(app_05)
            and app_05.application_status == "Reversed"
            and _eq(app_05.applied_amount, 1050)
            and bool(app_05.custom_nkt_reversed_on)
            and bool(app_05.custom_nkt_reversed_by)
            and bool(app_05.custom_nkt_reversal_reason)
        ),
        "c5_4_reapplication_00006_active": (
            bool(app_06)
            and app_06.application_status == "Applied"
            and app_06.customer_advance == ADVANCE_2
            and app_06.source_payment_receipt == PAYMENT_2
            and app_06.customer_order == ORDER
            and _eq(app_06.applied_amount, 1050)
        ),
        "c5_4_source_order_hold_released": (
            bool(order)
            and int(
                order.custom_nkt_advance_auto_apply_hold or 0
            )
            == 0
        ),
        "c5_4_reversed_history_retained": (
            len(reversed_order_apps) >= 3
        ),

        # V2.0C.5 FINAL VERIFIER DYNAMIC CUSTOMER STATE HOTFIX
        # Customer balance / available credit are intentionally dynamic.
        # Later legitimate Account transactions may change these fields
        # without invalidating the frozen C5 acceptance records.
        "customer_record_still_exists": bool(customer),
        "customer_balance_fields_present": (
            bool(customer)
            and customer.custom_nkt_current_account_balance is not None
            and customer.custom_nkt_available_credit is not None
        ),

        # Future-state-safe verifier rule:
        # global business-record totals are intentionally dynamic.
        # Frozen C5 acceptance is proven by the specific records above;
        # verifier read-only behavior is proven by start/end counts below.
    }

    receipts_after = frappe.db.count("NKT Payment Receipt")
    movements_after = frappe.db.count("NKT Cashier Movement")
    applications_after = frappe.db.count(
        "NKT Customer Advance Application"
    )

    checks["verifier_is_read_only_receipts"] = (
        receipts_after == receipts_before
    )
    checks["verifier_is_read_only_movements"] = (
        movements_after == movements_before
    )
    checks["verifier_is_read_only_applications"] = (
        applications_after == applications_before
    )

    errors = [
        name
        for name, passed in checks.items()
        if not passed
    ]

    return {
        "version": "V2.0C.5-FINAL-FUTURE-SAFE-R2",
        "scope": (
            "Payment on Account + Unapplied Customer Advance + "
            "Automatic Advance Application + Advance-Aware SOA + "
            "Controlled Advance Correction/Reversal"
        ),
        "component_verifiers": {
            "C5.2.1": c5_2,
            "C5.3": c5_3,
            "C5.4": c5_4,
        },
        "live_state": {
            "customer": customer,
            "order_00048": order,
            "receivable_00010": receivable,
            "payment_00050": payment_1,
            "payment_00051": payment_2,
            "advance_00003": advance_1,
            "advance_00004": advance_2,
            "active_order_applications": active_order_apps,
            "reversed_order_applications": reversed_order_apps,
            "payment_receipt_count": receipts_after,
            "cashier_movement_count": movements_after,
            "advance_application_count": applications_after,
        },
        "checks": checks,
        "errors": errors,
        "passed": not errors,
    }
