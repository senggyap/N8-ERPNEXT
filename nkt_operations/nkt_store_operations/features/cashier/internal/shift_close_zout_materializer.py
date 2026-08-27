from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import flt, get_datetime, now

from nkt_operations.nkt_store_operations.features.cashier.internal.cashier_shift_alias import (
    resolve_primary_cashier_shift,
)
from nkt_operations.nkt_store_operations.features.cashier.internal.shift_close_zout_offline_intent import (
    CASHIER_SHIFT_OPEN_FAMILY,
    CASHIER_SHIFT_CLOSE_FAMILY,
    ENCODER_ZOUT_FINALIZE_FAMILY,
    normalize_cashier_shift_open_intent,
    normalize_cashier_shift_close_intent,
    normalize_encoder_zout_finalization_intent,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role
from nkt_operations.nkt_store_operations.features.cashier.encoder_zout import _apply_summary_fields
from nkt_operations.nkt_store_operations.features.cashier.shift_engine import (
    CASHIER_CLOSED_STATUS,
    DENOMINATIONS,
    _summary_field_values,
    calculate_shift_summary,
)

FOUNDATION_VERSION = "C15C.10K-R4D"
PRIMARY_JOURNAL = "NKT Primary Shift Close Z-Out Intent"
PH_TZ = ZoneInfo("Asia/Manila")
TOLERANCE = 0.005

RECEIPT_STATES = {
    CASHIER_SHIFT_OPEN_FAMILY: "Cashier Shift Open Intent Preserved",
    CASHIER_SHIFT_CLOSE_FAMILY: "Cashier Shift Close Intent Preserved",
    ENCODER_ZOUT_FINALIZE_FAMILY: "Encoder Z-Out Finalization Intent Preserved",
}


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Shift Close / Z-Out materialization is Primary-only.")


def _event_uuid(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError("Shift Close / Z-Out Event UUID is invalid.") from exc


def _claim(event_uuid: str) -> str:
    name = "nkt-10k-materialize-" + hashlib.sha256(event_uuid.encode()).hexdigest()[:28]
    rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (name, 30), as_list=True)
    if not rows or int(rows[0][0] or 0) != 1:
        raise frappe.ValidationError("Shift Close / Z-Out materialization is busy. Safe retry required.")
    return name


def _release(name: str):
    try:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
    except Exception:
        pass


def _journal(event_uuid: str):
    rows = frappe.db.sql(
        f"SELECT name FROM `tab{PRIMARY_JOURNAL}` WHERE name=%s FOR UPDATE",
        (event_uuid,),
        as_dict=True,
    )
    if not rows:
        raise frappe.DoesNotExistError("Preserved Shift Close / Z-Out intent is unavailable.")
    return frappe.get_doc(PRIMARY_JOURNAL, event_uuid)


def _payload(journal) -> Dict[str, Any]:
    raw = json.loads(journal.canonical_payload_json)
    if journal.event_family == CASHIER_SHIFT_OPEN_FAMILY:
        return normalize_cashier_shift_open_intent(raw)
    if journal.event_family == CASHIER_SHIFT_CLOSE_FAMILY:
        return normalize_cashier_shift_close_intent(raw)
    if journal.event_family == ENCODER_ZOUT_FINALIZE_FAMILY:
        return normalize_encoder_zout_finalization_intent(raw)
    raise frappe.ValidationError("Unsupported preserved Shift Close / Z-Out family.")


def _verify_receipt(journal):
    receipt = frappe.get_doc("NKT Sync Primary Receipt", journal.name)
    expected_state = RECEIPT_STATES[journal.event_family]
    if (
        receipt.result_code != "Committed"
        or receipt.materialization_state != expected_state
        or receipt.canonical_doctype != PRIMARY_JOURNAL
        or receipt.canonical_name != journal.name
        or str(receipt.payload_sha256 or "").lower() != str(journal.payload_sha256 or "").lower()
    ):
        raise frappe.ValidationError("Shift Close / Z-Out Primary preservation receipt is invalid.")


def _as_naive_manila(value: Any) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(PH_TZ).replace(tzinfo=None)


def _materialize_open(journal, payload):
    if journal.canonical_name:
        if not frappe.db.exists("NKT Cashier Shift", journal.canonical_name):
            raise frappe.ValidationError("Materialized offline Cashier Shift binding is missing.")
        shift = frappe.get_doc("NKT Cashier Shift", journal.canonical_name)
        hard = []
        for field, expected in (
            ("company", payload["company"]),
            ("settlement_location", payload["settlement_location"]),
            ("cashier", payload["cashier"]),
        ):
            if str(shift.get(field) or "") != str(expected or ""):
                hard.append(field)
        if get_datetime(shift.shift_start) != get_datetime(_as_naive_manila(payload["shift_start"])):
            hard.append("shift_start")
        if abs(flt(shift.opening_cash) - flt(payload["opening_cash"])) > TOLERANCE:
            hard.append("opening_cash")
        if hard:
            raise frappe.ValidationError(
                "Materialized offline Cashier Shift conflicts with preserved open intent: "
                + ", ".join(hard)
            )
        return {"doctype": "NKT Cashier Shift", "name": shift.name, "replay": True}

    original_user = frappe.session.user
    try:
        # Accepted NKT Cashier Shift.before_validate() intentionally stamps
        # a NEW shift's cashier from frappe.session.user. Therefore Primary
        # must materialize the canonical shift under the preserved immutable
        # offline Cashier identity, not under Administrator.
        frappe.set_user(payload["cashier"])
        shift = frappe.get_doc({
            "doctype": "NKT Cashier Shift",
            "company": payload["company"],
            "settlement_location": payload["settlement_location"],
            "cashier": payload["cashier"],
            "opening_cash": payload["opening_cash"],
            "shift_start": _as_naive_manila(payload["shift_start"]),
            "status": "Open",
        })
        shift.flags.nkt_v19_internal = True
        shift.flags.nkt_c15c10k_primary_materialization = True
        shift.insert(ignore_permissions=True)
        if str(shift.cashier or "") != str(payload["cashier"] or ""):
            raise frappe.ValidationError(
                "Canonical Cashier Shift did not preserve the immutable offline Cashier identity."
            )
    finally:
        frappe.set_user(original_user)

    frappe.db.set_value(
        PRIMARY_JOURNAL,
        journal.name,
        {
            "materialization_state": "Canonical Materialized",
            "canonical_doctype": "NKT Cashier Shift",
            "canonical_name": shift.name,
            "reconciliation_status": "Not Reconciled",
            "reconciliation_notes": "Offline Store Edge Shift Open materialized on Primary.",
        },
        update_modified=False,
    )
    return {"doctype": "NKT Cashier Shift", "name": shift.name, "replay": False}


def _close_shift_name(journal, payload):
    primary_name = str(payload.get("primary_shift_name") or "").strip()
    if primary_name:
        if not frappe.db.exists("NKT Cashier Shift", primary_name):
            raise frappe.DoesNotExistError(
                "Offline close references a Primary Cashier Shift that is not yet available."
            )
        return primary_name
    return resolve_primary_cashier_shift("EDGE-SHIFT-" + payload["edge_shift_uuid"])


def _materialize_close(journal, payload):
    shift_name = _close_shift_name(journal, payload)
    shift = frappe.get_doc("NKT Cashier Shift", shift_name)
    close_dt = _as_naive_manila(payload["physical_close_datetime"])

    if journal.canonical_name:
        if journal.canonical_name != shift.name:
            raise frappe.ValidationError("Offline Cashier Shift close replay changed canonical shift identity.")
        if str(shift.status or "") not in (
            CASHIER_CLOSED_STATUS,
            "Reviewed / Closed",
            "Closed",
        ):
            raise frappe.ValidationError("Previously materialized offline close is no longer in a closed/review state.")
        if abs(flt(shift.actual_cash_count) - flt(payload["actual_cash"])) > TOLERANCE:
            raise frappe.ValidationError("Previously materialized Cashier physical count changed.")
        if get_datetime(shift.shift_end) != get_datetime(close_dt):
            raise frappe.ValidationError("Previously materialized Cashier true close time changed.")
        return {
            "doctype": "NKT Cashier Shift",
            "name": shift.name,
            "replay": True,
            "primary_expected_cash_at_materialization": flt(shift.custom_nkt_expected_cash_at_count),
        }

    if int(shift.docstatus or 0) != 0 or str(shift.status or "") != "Open":
        raise frappe.ValidationError(
            "Canonical Cashier Shift must still be Open for first offline-close materialization."
        )
    if str(shift.cashier or "") != payload["cashier"]:
        raise frappe.ValidationError("Canonical Cashier Shift cashier conflicts with offline close.")
    if str(shift.company or "") != payload["company"]:
        raise frappe.ValidationError("Canonical Cashier Shift company conflicts with offline close.")
    if str(shift.settlement_location or "") != payload["settlement_location"]:
        raise frappe.ValidationError("Canonical Cashier Shift location conflicts with offline close.")

    summary = calculate_shift_summary(shift.name)
    actual_cash = flt(payload["actual_cash"])
    primary_over_short = actual_cash - flt(summary["expected_cash"])

    original_user = frappe.session.user
    try:
        frappe.set_user("Administrator")
        shift = frappe.get_doc("NKT Cashier Shift", shift.name)
        shift.flags.nkt_v19_internal = True
        for field, denomination in DENOMINATIONS.items():
            shift.set(field, int(payload["denominations"].get(field) or 0))
        for field, value in _summary_field_values(summary).items():
            shift.set(field, value)
        shift.actual_cash_count = actual_cash
        shift.over_short = primary_over_short
        shift.count_notes = payload["count_notes"]
        shift.blind_count_confirmed = 1
        shift.count_locked_by = payload["cashier"]
        shift.count_locked_on = close_dt
        shift.custom_nkt_cashier_closed_by = payload["cashier"]
        shift.custom_nkt_cashier_closed_on = close_dt
        shift.custom_nkt_expected_cash_at_count = summary["expected_cash"]
        shift.custom_nkt_movement_count_at_count = summary["movement_count"]
        shift.custom_nkt_breakdown_snapshot_json = json.dumps(
            summary, default=str, sort_keys=True
        )
        shift.turnover_status = CASHIER_CLOSED_STATUS
        shift.turnover_amount = actual_cash
        shift.shift_end = close_dt
        shift.status = CASHIER_CLOSED_STATUS
        shift.save(ignore_permissions=True)
    finally:
        frappe.set_user(original_user)

    expected_delta = flt(summary["expected_cash"]) - flt(payload["provisional_expected_cash"])
    movement_delta = int(summary["movement_count"]) - int(payload["provisional_movement_count"])
    difference = abs(expected_delta) > TOLERANCE or movement_delta != 0
    notes = (
        "Primary expected cash at materialization differs from the Store Edge provisional close. "
        f"Expected-cash delta={expected_delta:.2f}; movement-count delta={movement_delta}. "
        "Cashier physical denomination count remains unchanged; Owner/Admin review is required."
        if difference
        else "Primary matched Store Edge provisional expected cash/movement count at materialization."
    )

    frappe.db.set_value(
        PRIMARY_JOURNAL,
        journal.name,
        {
            "materialization_state": "Canonical Materialized",
            "canonical_doctype": "NKT Cashier Shift",
            "canonical_name": shift.name,
            "reconciliation_status": "Difference Found" if difference else "Matched",
            "reconciliation_notes": notes,
        },
        update_modified=False,
    )
    return {
        "doctype": "NKT Cashier Shift",
        "name": shift.name,
        "replay": False,
        "primary_expected_cash_at_materialization": flt(summary["expected_cash"]),
        "store_edge_provisional_expected_cash": flt(payload["provisional_expected_cash"]),
        "expected_cash_delta": expected_delta,
        "movement_count_delta": movement_delta,
        "cashier_physical_count_rewritten": False,
    }


def _materialize_zout(journal, payload):
    if journal.canonical_name:
        if not frappe.db.exists("NKT Encoder Z-Out", journal.canonical_name):
            raise frappe.ValidationError("Materialized official Encoder Z-Out binding is missing.")
        doc = frappe.get_doc("NKT Encoder Z-Out", journal.canonical_name)
        if (
            str(doc.snapshot_hash or "").lower() != payload["snapshot_sha256"]
            or str(doc.snapshot_json or "") != payload["snapshot_json"]
            or int(doc.docstatus or 0) != 1
        ):
            raise frappe.ValidationError("Materialized official offline Encoder Z-Out was rewritten.")
        return {"doctype": "NKT Encoder Z-Out", "name": doc.name, "replay": True}

    data = json.loads(payload["snapshot_json"])
    start_dt = _as_naive_manila(payload["start_datetime"])
    end_dt = _as_naive_manila(payload["effective_end_datetime"])
    finalized_dt = _as_naive_manila(payload["finalized_on"])

    original_user = frappe.session.user
    try:
        frappe.set_user("Administrator")
        doc = frappe.get_doc({
            "doctype": "NKT Encoder Z-Out",
            "company": payload["company"],
            "business_date": payload["business_date"],
            "encoder": payload["encoder"],
            "start_time": start_dt.time(),
            "end_time": end_dt.time(),
            "include_reconciliation_exceptions": int(
                bool((data.get("options") or {}).get("include_reconciliation_exceptions", True))
            ),
            "include_inventory_appendix": 0,
        })
        doc.insert(ignore_permissions=True)
        _apply_summary_fields(doc, data)
        doc.snapshot_json = payload["snapshot_json"]
        doc.snapshot_hash = payload["snapshot_sha256"]
        doc.finalized_by = payload["encoder"]
        doc.finalized_on = finalized_dt
        doc.flags.nkt_c15c10k_preserved_offline_zout = True
        doc.flags.ignore_permissions = True
        doc.submit()
    finally:
        frappe.set_user(original_user)

    frappe.db.set_value(
        PRIMARY_JOURNAL,
        journal.name,
        {
            "materialization_state": "Canonical Materialized",
            "canonical_doctype": "NKT Encoder Z-Out",
            "canonical_name": doc.name,
            "reconciliation_status": "Not Reconciled",
            "reconciliation_notes": (
                "Official offline Encoder Z-Out snapshot materialized exactly as finalized at Store Edge. "
                "Later Primary reconciliation may attach findings but must not rewrite this snapshot."
            ),
        },
        update_modified=False,
    )
    return {
        "doctype": "NKT Encoder Z-Out",
        "name": doc.name,
        "replay": False,
        "official_snapshot_sha256": doc.snapshot_hash,
        "official_snapshot_rewritten": False,
    }


def materialize_preserved_shift_close_zout(event_uuid: str) -> Dict[str, Any]:
    _require_primary()
    event_uuid = _event_uuid(event_uuid)
    lock = _claim(event_uuid)
    try:
        journal = _journal(event_uuid)
        if journal.preservation_state != "Preserved":
            raise frappe.ValidationError("Shift Close / Z-Out intent is not preserved.")
        _verify_receipt(journal)
        payload = _payload(journal)

        if journal.event_family == CASHIER_SHIFT_OPEN_FAMILY:
            result = _materialize_open(journal, payload)
        elif journal.event_family == CASHIER_SHIFT_CLOSE_FAMILY:
            result = _materialize_close(journal, payload)
        elif journal.event_family == ENCODER_ZOUT_FINALIZE_FAMILY:
            result = _materialize_zout(journal, payload)
        else:
            raise frappe.ValidationError("Unsupported Shift Close / Z-Out materialization family.")

        return {
            "event_uuid": event_uuid,
            "event_family": journal.event_family,
            "materialization_state": "Canonical Materialized",
            **result,
        }
    finally:
        _release(lock)


@frappe.whitelist()
def materialize(event_uuid):
    return materialize_preserved_shift_close_zout(event_uuid)
