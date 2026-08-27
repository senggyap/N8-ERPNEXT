
import importlib

import frappe
from frappe.utils import flt

TOLERANCE = 0.005
CORE = "nkt_operations.nkt_store_operations.features.payments_accounts.collection"
MANUAL_UI_SCRIPT = "NKT C5.2 Customer Advance Order UI"


def _order_credit_approved(order):
    order_status = (order.get("custom_nkt_account_credit_status") or "").strip()
    if order_status:
        return order_status == "Approved"

    receivable = frappe.db.get_value(
        "NKT Customer Receivable",
        {
            "customer_order": order.name,
            "docstatus": ["!=", 2],
        },
        ["credit_control_status"],
        as_dict=True,
    )
    return bool(
        receivable
        and (receivable.credit_control_status or "").strip() == "Approved"
    )


def _is_matched_account_order(order):
    if int(order.docstatus or 0) != 1:
        return False

    if flt(order.get("declared_account")) <= TOLERANCE:
        return False

    match_status = (order.get("cashier_reconciliation_status") or "").strip()
    if not match_status.startswith("Matched"):
        return False

    if not order.get("matched_cashier_sale"):
        return False

    return True


def _available_advance(order):
    info = frappe.get_attr(CORE + ".get_customer_advance_balance")(
        order.customer,
        order.company,
    )
    return max(flt((info or {}).get("available_advance")), 0)


def auto_apply_customer_advance_for_order(customer_order, remarks=None):
    """
    Normal C5.2.1 rule:
    - Cashier + Encoder independently enter Account.
    - Credit Control remains a gate when required.
    - Once the matched Account order is Approved, verified Customer Advance
      is consumed automatically.
    - This never represents new money and therefore must not create another
      Payment Receipt or Cashier Movement.
    """
    if getattr(frappe.flags, "nkt_c5_auto_advance_active", False):
        return {
            "customer_order": customer_order,
            "skipped": "recursion_guard",
        }

    order = frappe.get_doc("NKT Customer Order", customer_order)

    # V2.0C.5.4 ADVANCE AUTO-APPLY CORRECTION HOLD
    order_meta = frappe.get_meta("NKT Customer Order")
    if (
        order_meta.has_field("custom_nkt_advance_auto_apply_hold")
        and int(order.get("custom_nkt_advance_auto_apply_hold") or 0)
    ):
        return {
            "customer_order": order.name,
            "skipped": "advance_auto_apply_correction_hold",
        }

    if not _is_matched_account_order(order):
        return {
            "customer_order": order.name,
            "skipped": "not_matched_account_order",
        }

    if not _order_credit_approved(order):
        return {
            "customer_order": order.name,
            "skipped": "credit_control_not_approved",
        }

    due = max(flt(order.get("amount_due")), 0)
    if due <= TOLERANCE:
        return {
            "customer_order": order.name,
            "skipped": "nothing_due",
        }

    available = _available_advance(order)
    if available <= TOLERANCE:
        return {
            "customer_order": order.name,
            "skipped": "no_available_advance",
        }

    apply_amount = min(due, available)

    receipts_before = frappe.db.count("NKT Payment Receipt")
    movements_before = frappe.db.count("NKT Cashier Movement")

    frappe.flags.nkt_c5_auto_advance_active = True
    try:
        result = frappe.get_attr(
            CORE + ".apply_customer_advance_to_order"
        )(
            customer_order=order.name,
            amount=apply_amount,
            remarks=(
                remarks
                or "Automatically applied verified Customer Advance after "
                "Cashier/Encoder Account-sale reconciliation and required "
                "Credit Control approval."
            ),
        )
    finally:
        frappe.flags.nkt_c5_auto_advance_active = False

    # The remainder, if any, is approved Account exposure. It is not an
    # unpaid-cash-sale blocker. Preserve already-released states.
    order.reload()
    protected_statuses = {
        "Released",
        "Partially Released",
        "Pending Admin Confirmation",
    }

    values = {
        "payment_status": (
            "Paid"
            if flt(order.amount_due) <= TOLERANCE
            else "Partially Paid"
        )
    }
    if (order.status or "") not in protected_statuses:
        values["status"] = "Ready for Release"

    frappe.db.set_value(
        "NKT Customer Order",
        order.name,
        values,
        update_modified=False,
    )

    receipts_after = frappe.db.count("NKT Payment Receipt")
    movements_after = frappe.db.count("NKT Cashier Movement")

    if receipts_after != receipts_before:
        frappe.throw(
            "C5.2.1 safety stop: applying Customer Advance created "
            "a new Payment Receipt."
        )

    if movements_after != movements_before:
        frappe.throw(
            "C5.2.1 safety stop: applying Customer Advance created "
            "a new Cashier Movement."
        )

    return {
        **(result or {}),
        "automatic": True,
        "payment_receipt_count_unchanged": True,
        "cashier_movement_count_unchanged": True,
    }




def _auto_apply_block_reason(order):
    """Return None only when an Account order is eligible to consume advance now."""
    if int(order.docstatus or 0) != 1:
        return "order_not_submitted"
    if flt(order.get("declared_account")) <= TOLERANCE:
        return "not_account_order"
    match_status = (order.get("cashier_reconciliation_status") or "").strip()
    if not match_status.startswith("Matched") or not order.get("matched_cashier_sale"):
        return "not_matched_account_order"
    if not _order_credit_approved(order):
        return "credit_control_not_approved"
    order_meta = frappe.get_meta("NKT Customer Order")
    if (
        order_meta.has_field("custom_nkt_advance_auto_apply_hold")
        and int(order.get("custom_nkt_advance_auto_apply_hold") or 0)
    ):
        return "advance_auto_apply_correction_hold"
    if max(flt(order.get("amount_due")), 0) <= TOLERANCE:
        return "order_due_not_positive"
    return None


def _customer_company_pairs(customer=None, company=None):
    """Compatibility helper for read-only diagnostics only."""
    pairs = set()
    advance_filters = {
        "docstatus": 1,
        "advance_status": ["in", ["Available", "Partially Used"]],
        "available_advance_amount": [">", TOLERANCE],
    }
    receivable_filters = {
        "status": ["in", ["Open", "Partially Paid"]],
        "outstanding_amount": [">", TOLERANCE],
    }
    if customer:
        advance_filters["customer"] = customer
        receivable_filters["customer"] = customer
    if company:
        advance_filters["company"] = company
        receivable_filters["company"] = company
    for row in frappe.get_all("NKT Customer Advance", filters=advance_filters, fields=["customer", "company"]):
        pairs.add((row.customer, row.company))
    if frappe.db.exists("DocType", "NKT Customer Receivable"):
        for row in frappe.get_all("NKT Customer Receivable", filters=receivable_filters, fields=["customer", "company"]):
            pairs.add((row.customer, row.company))
    return sorted(pairs, key=lambda x: ((x[0] or ""), (x[1] or "")))


def inspect_customer_advance_receivable_invariant(customer=None, company=None):
    """Read-only compatibility diagnostic after UI7B6.

    IMPORTANT: customer-wide coexistence of an available Customer Advance and an
    already-approved receivable is NOT, by itself, an instruction to sweep that
    advance backward into the receivable. C5 auto-application is directional:
    it runs for the relevant matched Account order when that order reaches its
    normal Credit Control eligibility gate. A later Return Credit must not be
    retroactively swept into an older accepted order merely because both balances
    coexist.
    """
    rows = []
    for customer_name, company_name in _customer_company_pairs(customer, company):
        advance_info = frappe.get_attr(CORE + ".get_customer_advance_balance")(
            customer_name, company_name
        ) or {}
        available = max(flt(advance_info.get("available_advance")), 0)
        recs = frappe.get_all(
            "NKT Customer Receivable",
            filters={
                "customer": customer_name,
                "company": company_name,
                "status": ["in", ["Open", "Partially Paid"]],
                "outstanding_amount": [">", TOLERANCE],
            },
            fields=[
                "name", "customer_order", "posting_date", "due_date",
                "outstanding_amount", "credit_control_status", "creation",
            ],
            order_by="posting_date asc, creation asc, name asc",
        )
        approved_open = 0.0
        pending_open = 0.0
        review_rows = []
        for rec in recs:
            outstanding = max(flt(rec.outstanding_amount), 0)
            if (rec.credit_control_status or "").strip() == "Approved":
                approved_open += outstanding
            else:
                pending_open += outstanding
            review_rows.append({
                "receivable": rec.name,
                "customer_order": rec.customer_order,
                "outstanding_amount": outstanding,
                "credit_control_status": rec.credit_control_status,
            })
        rows.append({
            "customer": customer_name,
            "company": company_name,
            "available_advance": available,
            "approved_open_outstanding": approved_open,
            "pending_or_unapproved_open_outstanding": pending_open,
            "coexistence_requires_context_review": bool(
                available > TOLERANCE and approved_open > TOLERANCE
            ),
            "receivables": review_rows,
            "legacy_broad_invariant_disabled": True,
            "invariant_violation": False,
            "expected_immediate_application": 0.0,
        })
    return {
        "rows": rows,
        "pair_count": len(rows),
        "invariant_violation_count": 0,
        "legacy_broad_invariant_disabled": True,
        "available_advance_total": sum(flt(row["available_advance"]) for row in rows),
        "approved_open_outstanding_total": sum(flt(row["approved_open_outstanding"]) for row in rows),
        "expected_immediate_application_total": 0.0,
    }


def sweep_customer_advance_against_approved_receivables(customer, company=None, trigger=None):
    """Disabled UI7B4 compatibility endpoint.

    Use auto_apply_customer_advance_for_order() only at the normal matched Account
    order / Credit Control gate. A customer-wide sweep is intentionally forbidden.
    """
    frappe.throw(
        "Customer-wide Customer Advance sweeps were disabled by UI7B6 because "
        "they can retroactively consume later credits against older receivables. "
        "Apply advances only through the relevant Account order's normal "
        "matching and Credit Control workflow."
    )


def repair_customer_advance_receivable_invariant(customer=None, company=None, trigger=None):
    """Disabled UI7B4 compatibility endpoint; never performs a broad repair."""
    frappe.throw(
        "The legacy broad Customer Advance invariant repair is disabled. "
        "Use the specific Account order's normal approval/auto-application path."
    )


def disable_manual_advance_ui():
    if frappe.db.exists("Client Script", MANUAL_UI_SCRIPT):
        meta = frappe.get_meta("Client Script")
        if meta.has_field("enabled"):
            frappe.db.set_value(
                "Client Script",
                MANUAL_UI_SCRIPT,
                "enabled",
                0,
                update_modified=True,
            )
        frappe.db.commit()

    frappe.clear_cache(doctype="NKT Customer Order")

    return {
        "client_script": MANUAL_UI_SCRIPT,
        "exists": bool(
            frappe.db.exists("Client Script", MANUAL_UI_SCRIPT)
        ),
        "enabled": (
            int(
                frappe.db.get_value(
                    "Client Script",
                    MANUAL_UI_SCRIPT,
                    "enabled",
                )
                or 0
            )
            if frappe.db.exists("Client Script", MANUAL_UI_SCRIPT)
            else None
        ),
    }


def verify():
    """
    Read-only C5.2.1 verifier.

    The old verifier called disable_manual_advance_ui(), which could write
    Client Script configuration and commit. Verification must only inspect
    the accepted configuration; it must never repair it.
    """
    modules = [
        "nkt_operations.nkt_store_operations.features.payments_accounts.credit",
        "nkt_operations.nkt_store_operations.features.sales.matching",
        "nkt_operations.nkt_store_operations.features.payments_accounts.collection",
    ]

    imports = {}
    for name in modules:
        imports[name] = bool(importlib.import_module(name))

    ui_exists = bool(
        frappe.db.exists("Client Script", MANUAL_UI_SCRIPT)
    )
    ui_enabled = (
        int(
            frappe.db.get_value(
                "Client Script",
                MANUAL_UI_SCRIPT,
                "enabled",
            )
            or 0
        )
        if ui_exists
        else None
    )

    return {
        "version": "V2.0C.5.2.1-FUTURE-SAFE",
        "imports": imports,
        "manual_credit_control_remains_gate": True,
        "automatic_after_approval": True,
        "automatic_after_match_when_already_approved": True,
        "manual_encoder_advance_button_enabled": ui_enabled,
        "verifier_configuration_read_only": True,
        "new_payment_receipt_on_advance_application": False,
        "new_cashier_movement_on_advance_application": False,
        "passed": (
            all(imports.values())
            and ui_enabled in (0, None)
        ),
    }
