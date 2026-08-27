from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Iterable, List
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import getdate, now, nowdate

from nkt_operations.nkt_store_operations.features.inventory.internal.physical_inventory_offline_intent import (
    normalize_physical_inventory_count_intent,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role
from nkt_operations.nkt_store_operations.features.inventory.physical_inventory import (
    prepare_adjustment_document,
)

FOUNDATION_VERSION = "C15C.10J-R7"
PRIMARY_JOURNAL = "NKT Primary Physical Inventory Count Intent"
PH_TZ = ZoneInfo("Asia/Manila")

MATERIALIZATION_POLICY = {
    "same_day_only": True,
    "manual_backdate_enabled": False,
    "intervening_stock_movement_allowed": False,
    "cross_date_auto_post_enabled": False,
    "serialized_batched_generic_auto_post_enabled": False,
    "stock_reconciliation_primary_owned": True,
}


def _require_primary() -> None:
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Physical Inventory materialization is available only on Primary.")


def _manila_datetime(value: Any, label: str) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(raw)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=PH_TZ)
    return dt.astimezone(PH_TZ)


def _request_id(event_uuid: str) -> str:
    return "C15C10J-" + str(event_uuid)


def _journal_for_update(event_uuid: str):
    rows = frappe.db.sql(
        f"SELECT name FROM `tab{PRIMARY_JOURNAL}` WHERE name=%s FOR UPDATE",
        (event_uuid,),
        as_dict=True,
    )
    if not rows:
        raise frappe.DoesNotExistError("Preserved Physical Inventory count intent is unavailable.")
    return frappe.get_doc(PRIMARY_JOURNAL, event_uuid)


def _lock_bins(item_codes: Iterable[str], warehouse: str) -> List[str]:
    """Lock every existing Bin row for counted item/warehouse pairs.

    Automatic materialization is refused when a counted item has no Bin row,
    because there would be no canonical stock row to lock against a concurrent
    movement. The preserved count remains safe evidence for a current recount.
    """
    locked = []
    missing = []
    for item_code in sorted(set(item_codes)):
        rows = frappe.db.sql(
            """
            SELECT name
            FROM `tabBin`
            WHERE item_code=%s AND warehouse=%s
            FOR UPDATE
            """,
            (item_code, warehouse),
            as_list=True,
        )
        if not rows:
            missing.append(item_code)
        else:
            locked.append(rows[0][0])
    if missing:
        raise frappe.ValidationError(
            "Automatic Physical Inventory materialization requires an existing locked Bin "
            "for every counted item. Fresh/current recount or Primary review required for: "
            + ", ".join(missing)
        )
    return locked


def intervening_stock_movements(
    item_codes: Iterable[str],
    warehouse: str,
    count_datetime: Any,
) -> List[Dict[str, Any]]:
    """Return non-cancelled SLE movements at/after the physical count timestamp.

    Same-second movement is treated conservatively as ambiguous and therefore
    unsafe for automatic materialization.
    """
    count_dt = _manila_datetime(count_datetime, "Physical Count Time").replace(tzinfo=None)
    results = []
    for item_code in sorted(set(item_codes)):
        rows = frappe.db.sql(
            """
            SELECT name, item_code, warehouse, posting_date, posting_time,
                   actual_qty, voucher_type, voucher_no, creation
            FROM `tabStock Ledger Entry`
            WHERE item_code=%s
              AND warehouse=%s
              AND COALESCE(is_cancelled,0)=0
              AND (
                    posting_date > %s
                    OR (posting_date = %s AND posting_time >= %s)
                  )
            ORDER BY posting_date, posting_time, creation, name
            LIMIT 20
            """,
            (
                item_code,
                warehouse,
                count_dt.date(),
                count_dt.date(),
                count_dt.time(),
            ),
            as_dict=True,
        )
        for row in rows:
            results.append(
                {
                    "name": row.name,
                    "item_code": row.item_code,
                    "warehouse": row.warehouse,
                    "posting_date": str(row.posting_date),
                    "posting_time": str(row.posting_time),
                    "actual_qty": float(row.actual_qty or 0),
                    "voucher_type": row.voucher_type,
                    "voucher_no": row.voucher_no,
                    "creation": str(row.creation),
                }
            )
    return results


def classify_materialization_eligibility(
    payload: Dict[str, Any],
    *,
    current_date: Any = None,
) -> Dict[str, Any]:
    normalized = normalize_physical_inventory_count_intent(payload)
    count_dt = _manila_datetime(normalized["count_datetime"], "Physical Count Time")
    current = getdate(current_date or nowdate())

    if getdate(normalized["business_date"]) != count_dt.date():
        return {
            "eligible": False,
            "code": "IMMUTABLE_DATE_MISMATCH",
            "decision": "Manual Primary Review Required",
            "reason": "Preserved Business Date no longer matches its immutable physical count time.",
        }

    if count_dt.date() != current:
        return {
            "eligible": False,
            "code": "CROSS_DATE_RECOUNT_REQUIRED",
            "decision": "Fresh Recount Required",
            "reason": (
                "Offline physical count is from a previous business date. "
                "NKT does not backdate Stock Reconciliation; perform a fresh/current recount."
            ),
        }

    return {
        "eligible": True,
        "code": "SAME_DAY_ELIGIBLE_FOR_MOVEMENT_GUARD",
        "decision": "Pending",
        "reason": "Same-day preserved count may proceed to the intervening-movement guard.",
    }


def _existing_materialized_adjustment(event_uuid: str):
    return frappe.db.get_value(
        "NKT Physical Inventory Adjustment",
        {"custom_nkt_adjustment_request_id": _request_id(event_uuid)},
        "name",
    )


def _set_blocked_decision(journal, decision: str, notes: str) -> None:
    journal.downstream_state = "Awaiting Physical Inventory Reconciliation"
    journal.materialization_decision = decision
    journal.materialization_notes = notes
    journal.save(ignore_permissions=True)


def _materialized_result(journal, adjustment, *, replay: bool) -> Dict[str, Any]:
    return {
        "event_uuid": journal.name,
        "status": "Materialized",
        "replay": bool(replay),
        "materialization_decision": journal.materialization_decision,
        "physical_inventory_adjustment": adjustment.name,
        "physical_inventory_adjustment_docstatus": int(adjustment.docstatus or 0),
        "stock_reconciliation": adjustment.stock_reconciliation or None,
        "canonical_stock_adjustment_created": bool(adjustment.stock_reconciliation),
        "current_date_only": True,
        "manual_backdate_used": False,
    }


def materialize_preserved_physical_inventory_count(event_uuid: str) -> Dict[str, Any]:
    """Materialize one preserved count through the accepted C8 front door.

    Automatic posting is allowed only when:
    - physical count business date is still today in NKT/Manila time;
    - every counted item has a lockable Bin row;
    - there is no non-cancelled SLE at/after physical count time;
    - the accepted C8 front door itself has no blockers.

    Cross-date or moved-stock counts remain preserved and require a fresh/current
    recount or explicit Primary review. No historical Stock Reconciliation is created.
    """
    _require_primary()
    event_uuid = str(event_uuid)

    journal = _journal_for_update(event_uuid)
    if journal.preservation_state != "Preserved":
        raise frappe.ValidationError("Physical Inventory count intent is not in Preserved state.")

    payload = normalize_physical_inventory_count_intent(
        json.loads(journal.canonical_payload_json)
    )

    existing_name = _existing_materialized_adjustment(event_uuid)
    if existing_name:
        adjustment = frappe.get_doc("NKT Physical Inventory Adjustment", existing_name)
        if journal.downstream_state != "Materialized":
            journal.downstream_state = "Materialized"
            journal.materialization_decision = (
                "Posted" if adjustment.stock_reconciliation else "No Variance"
            )
            journal.materialized_adjustment = adjustment.name
            journal.materialized_stock_reconciliation = adjustment.stock_reconciliation or None
            journal.materialized_on = journal.materialized_on or now()
            journal.materialization_notes = (
                "Existing deterministic C8 materialization recovered by request identity."
            )
            journal.save(ignore_permissions=True)
        return _materialized_result(journal, adjustment, replay=True)

    eligibility = classify_materialization_eligibility(payload)
    if not eligibility["eligible"]:
        _set_blocked_decision(
            journal,
            eligibility["decision"],
            eligibility["reason"],
        )
        return {
            "event_uuid": event_uuid,
            "status": "Blocked",
            **eligibility,
            "canonical_stock_adjustment_created": False,
            "manual_backdate_used": False,
        }

    item_codes = [row["item_code"] for row in payload["items"]]

    try:
        _lock_bins(item_codes, payload["warehouse"])
    except frappe.ValidationError as exc:
        _set_blocked_decision(
            journal,
            "Manual Primary Review Required",
            str(exc),
        )
        return {
            "event_uuid": event_uuid,
            "status": "Blocked",
            "eligible": False,
            "code": "UNLOCKABLE_BIN_SCOPE",
            "decision": "Manual Primary Review Required",
            "reason": str(exc),
            "canonical_stock_adjustment_created": False,
            "manual_backdate_used": False,
        }

    movements = intervening_stock_movements(
        item_codes,
        payload["warehouse"],
        payload["count_datetime"],
    )
    if movements:
        notes = (
            "Counted stock moved at/after the physical count timestamp. "
            "Automatic materialization refused; perform a fresh/current recount."
        )
        _set_blocked_decision(journal, "Fresh Recount Required", notes)
        return {
            "event_uuid": event_uuid,
            "status": "Blocked",
            "eligible": False,
            "code": "INTERVENING_STOCK_MOVEMENT",
            "decision": "Fresh Recount Required",
            "reason": notes,
            "movements": movements,
            "canonical_stock_adjustment_created": False,
            "manual_backdate_used": False,
        }

    original_user = frappe.session.user
    operator_user = payload["counted_by"]
    try:
        frappe.set_user(operator_user)

        doc = frappe.get_doc(
            {
                "doctype": "NKT Physical Inventory Adjustment",
                "company": payload["company"],
                "warehouse": payload["warehouse"],
                "business_date": nowdate(),
                "count_datetime": _manila_datetime(
                    payload["count_datetime"],
                    "Physical Count Time",
                ).replace(tzinfo=None),
                "count_reason": payload["count_reason"],
                "physical_count_reference": payload.get("physical_count_reference"),
                "operator_notes": payload.get("operator_notes"),
                "physical_count_confirmed": 1,
                "custom_nkt_adjustment_request_id": _request_id(event_uuid),
                "items": [
                    {
                        "item_code": row["item_code"],
                        "physical_qty": row["physical_qty"],
                        "physical_qty_confirmed": 1,
                    }
                    for row in payload["items"]
                ],
            }
        )

        # Accepted C8 preflight refreshes current stock/valuation and keeps all
        # existing whole-number, serial/batch, valuation, and role guards.
        preview = prepare_adjustment_document(doc, force_refresh=True)
        if preview.get("blockers"):
            notes = "Accepted C8 front door blocked automatic materialization: " + "; ".join(
                preview["blockers"]
            )
            frappe.set_user(original_user)
            _set_blocked_decision(
                journal,
                "Manual Primary Review Required",
                notes,
            )
            return {
                "event_uuid": event_uuid,
                "status": "Blocked",
                "eligible": False,
                "code": "C8_FRONT_DOOR_BLOCKER",
                "decision": "Manual Primary Review Required",
                "reason": notes,
                "blockers": preview["blockers"],
                "canonical_stock_adjustment_created": False,
                "manual_backdate_used": False,
            }

        # Re-run the movement guard immediately after C8 preflight while Bin
        # locks remain held. Any movement visible now makes the old observation stale.
        movements = intervening_stock_movements(
            item_codes,
            payload["warehouse"],
            payload["count_datetime"],
        )
        if movements:
            notes = (
                "Stock moved during materialization preflight. "
                "Automatic posting refused; perform a fresh/current recount."
            )
            frappe.set_user(original_user)
            _set_blocked_decision(journal, "Fresh Recount Required", notes)
            return {
                "event_uuid": event_uuid,
                "status": "Blocked",
                "eligible": False,
                "code": "INTERVENING_STOCK_MOVEMENT",
                "decision": "Fresh Recount Required",
                "reason": notes,
                "movements": movements,
                "canonical_stock_adjustment_created": False,
                "manual_backdate_used": False,
            }

        doc.insert()

        if int(doc.variance_line_count or 0) > 0:
            doc.submit()
            decision = "Posted"
        else:
            # C8 deliberately refuses submission when there is no variance.
            # Keeping the deterministic front-door draft records the count
            # without inventing an unnecessary Stock Reconciliation.
            decision = "No Variance"

        journal.downstream_state = "Materialized"
        journal.materialization_decision = decision
        journal.materialized_adjustment = doc.name
        journal.materialized_stock_reconciliation = doc.stock_reconciliation or None
        journal.materialized_on = now()
        journal.materialization_notes = (
            "Materialized through accepted C8 current-date Physical Inventory front door. "
            + (
                "Canonical Stock Reconciliation posted on Primary."
                if doc.stock_reconciliation
                else "No quantity variance; no Stock Reconciliation was required."
            )
        )
        journal.save(ignore_permissions=True)

        return _materialized_result(journal, doc, replay=False)
    finally:
        frappe.set_user(original_user)


def materialization_policy_status() -> Dict[str, Any]:
    return {
        "foundation_version": FOUNDATION_VERSION,
        **MATERIALIZATION_POLICY,
        "accepted_front_door": "NKT Physical Inventory Adjustment",
        "cross_date_result": "Fresh Recount Required",
        "intervening_movement_result": "Fresh Recount Required",
        "no_variance_result": "Materialized front-door draft; no Stock Reconciliation",
        "variance_result": "Submitted C8 front door; Primary-owned Stock Reconciliation",
    }


@frappe.whitelist()
def materialize_preserved_count(event_uuid):
    return materialize_preserved_physical_inventory_count(event_uuid)
