from __future__ import annotations

import inspect
import json

import frappe
from frappe.utils import cint, flt

VERSION = "V2.0C.7.12A-C.1"
DECL = "NKT Return Exchange Declaration"
REQUEST_FIELD = "custom_nkt_submit_request_id"


def _business_counts():
    doctypes = [
        "NKT Return Exchange Declaration",
        "NKT Payment Receipt",
        "NKT Cashier Movement",
        "NKT Customer Advance",
        "NKT Customer Advance Application",
        "NKT Customer Receivable",
        "NKT Return Account Adjustment",
        "NKT Stock Recovery",
        "NKT Cashier Sale",
        "NKT Customer Order",
        "Stock Entry",
    ]
    out = {}
    for dt in doctypes:
        if frappe.db.exists("DocType", dt):
            out[dt] = frappe.db.count(dt)
    return out


def business_counts():
    frappe.set_user("Administrator")
    return _business_counts()


def verify():
    frappe.set_user("Administrator")
    before = _business_counts()
    errors = []
    checks = {}

    matching = frappe.get_attr("nkt_operations.nkt_store_operations.features.returns.matching.verify")()
    ui = frappe.get_attr("nkt_operations.nkt_store_operations.features.returns.service.verify")()
    posting = frappe.get_attr("nkt_operations.nkt_store_operations.features.returns.posting.verify")()
    c5 = frappe.get_attr("nkt_operations.nkt_store_operations.features.setup_validation.internal.accounts_acceptance.run")()
    c55 = frappe.get_attr("nkt_operations.nkt_store_operations.features.payments_accounts.internal.role_safe_receivable.verify")()
    c56 = frappe.get_attr("nkt_operations.nkt_store_operations.features.fast_screen.fast_customer_creation.verify")()

    checks["matching_verify_passed"] = bool(matching.get("passed"))
    checks["ui_verify_passed"] = bool(ui.get("passed"))
    checks["posting_verify_passed"] = bool(posting.get("passed"))
    checks["c5_final_passed"] = bool(c5.get("passed"))
    checks["c5_5_passed"] = bool(c55.get("passed"))
    checks["c5_6_passed"] = bool(c56.get("passed"))

    meta = frappe.get_meta(DECL)
    df = meta.get_field(REQUEST_FIELD)
    checks["request_id_field_exists"] = bool(df)
    checks["request_id_field_unique"] = bool(df and cint(df.unique))
    idx_rows = frappe.db.sql(
        "SHOW INDEX FROM `tabNKT Return Exchange Declaration` WHERE Column_name=%s",
        REQUEST_FIELD,
        as_dict=True,
    ) if df else []
    checks["request_id_db_unique_index"] = any(cint(x.get("Non_unique")) == 0 for x in idx_rows)

    import nkt_operations.nkt_store_operations.features.returns.matching as m
    import nkt_operations.nkt_store_operations.features.returns.posting as p
    import nkt_operations.nkt_store_operations.features.payments_accounts.cash_ledger as ledger

    submit_src = inspect.getsource(m.submit_from_payload)
    lock_src = inspect.getsource(m._lock_submission_source)
    return_src = inspect.getsource(m._prepare_return_rows)
    validate_src = inspect.getsource(m.validate_declaration)
    post_src = inspect.getsource(p.post_independent_declaration)
    movement_src = inspect.getsource(ledger.create_cashier_movement)

    checks["source_pair_for_update_lock"] = "FOR UPDATE" in lock_src and "NKT Customer Order" in lock_src and "NKT Cashier Sale" in lock_src
    checks["request_replay_checked_before_and_after_lock"] = submit_src.count("_existing_request_submission") >= 2
    checks["request_id_required"] = "_normalize_submit_request_id" in submit_src
    checks["over_return_guard_present"] = "exceeds remaining returnable quantity" in return_src
    checks["same_side_exact_duplicate_guard_present"] = "already submitted" in validate_src and '"side": doc.side' in validate_src and '"match_key": doc.match_key' in validate_src
    checks["independent_posting_declaration_row_lock"] = "FOR UPDATE" in post_src
    checks["cashier_movement_source_idempotency"] = "source_doctype" in movement_src and "source_name" in movement_src and "existing" in movement_src

    # Permanent anchor must not move.
    order = frappe.db.get_value(
        "NKT Customer Order", "NKT-ORD-00049",
        ["name", "customer", "amount_due", "payment_status", "status", "matched_cashier_sale"],
        as_dict=True,
    )
    rec = frappe.db.get_value(
        "NKT Customer Receivable", {"customer_order": "NKT-ORD-00049"},
        ["name", "outstanding_amount", "status"], as_dict=True,
    )
    checks["ord49_exists"] = bool(order)
    checks["ord49_amount_due_7750"] = bool(order and abs(flt(order.amount_due) - 7750.0) < 0.005)
    checks["ord49_receivable_7750"] = bool(rec and abs(flt(rec.outstanding_amount) - 7750.0) < 0.005)

    # Ensure exactly one live RX client script per screen and that both carry stable request-id logic.
    active_scripts = {}
    for dt in ("NKT Cashier Return Exchange", "NKT Encoder Return Exchange"):
        rows = frappe.get_all("Client Script", filters={"dt": dt, "enabled": 1}, fields=["name", "script"])
        active_scripts[dt] = [r.name for r in rows]
        checks[f"single_active_script::{dt}"] = len(rows) == 1
        checks[f"request_id_ui::{dt}"] = bool(rows and "submitRequestId" in (rows[0].script or "") and "submit_request_id" in (rows[0].script or ""))

    for key, ok in checks.items():
        if not ok:
            errors.append(key)

    after = _business_counts()
    count_changes = {k: [before.get(k), after.get(k)] for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)}
    if count_changes:
        errors.append("read_only_verifier_changed_business_counts")

    return {
        "version": VERSION,
        "scope": "C7.12A-C over-return / duplicate same-side / replay-concurrency hardening",
        "mode": "READ-ONLY VERIFICATION",
        "checks": checks,
        "active_client_scripts": active_scripts,
        "ord49": order,
        "ord49_receivable": rec,
        "business_counts_before": before,
        "business_counts_after": after,
        "business_count_changes": count_changes,
        "component_versions": {
            "matching": matching.get("version"),
            "ui": ui.get("version"),
            "posting": posting.get("version"),
        },
        "errors": errors,
        "passed": not errors,
    }
