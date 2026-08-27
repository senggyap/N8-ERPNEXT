from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import flt, get_datetime

from nkt_operations.nkt_store_operations.features.cashier.internal.shift_close_zout_offline_intent import (
    CASHIER_SHIFT_CLOSE_FAMILY,
    ENCODER_ZOUT_FINALIZE_FAMILY,
    normalize_cashier_shift_close_intent,
    normalize_encoder_zout_finalization_intent,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role
from nkt_operations.nkt_store_operations.features.cashier.encoder_zout import build_zout_data
from nkt_operations.nkt_store_operations.features.cashier.shift_engine import calculate_shift_summary

FOUNDATION_VERSION = "C15C.10K-R5"
PRIMARY_JOURNAL = "NKT Primary Shift Close Z-Out Intent"
PH_TZ = ZoneInfo("Asia/Manila")
TOLERANCE = 0.005


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError(
            "Shift Close / Z-Out late-sync reconciliation is Primary-only."
        )


def _journal_for_update(event_uuid: str):
    rows = frappe.db.sql(
        f"SELECT name FROM `tab{PRIMARY_JOURNAL}` WHERE name=%s FOR UPDATE",
        (str(event_uuid),),
        as_dict=True,
    )
    if not rows:
        raise frappe.DoesNotExistError(
            "Preserved Shift Close / Z-Out journal is unavailable."
        )
    return frappe.get_doc(PRIMARY_JOURNAL, str(event_uuid))


def _payload(journal) -> Dict[str, Any]:
    raw = json.loads(journal.canonical_payload_json)
    if journal.event_family == CASHIER_SHIFT_CLOSE_FAMILY:
        return normalize_cashier_shift_close_intent(raw)
    if journal.event_family == ENCODER_ZOUT_FINALIZE_FAMILY:
        return normalize_encoder_zout_finalization_intent(raw)
    raise frappe.ValidationError(
        "Late-sync reconciliation supports Cashier Shift Close and official Encoder Z-Out only."
    )


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _stable_zout_projection(value: Dict[str, Any]) -> Dict[str, Any]:
    """Remove generation-only metadata, preserving all business snapshot content."""
    data = deepcopy(value)
    scope = data.get("scope")
    if isinstance(scope, dict):
        scope.pop("generated_on", None)
    return json.loads(_canonical(data))


def _time(value: Any):
    dt = get_datetime(value)
    return dt.time()


def _set_reconciliation(journal, *, status: str, notes: str):
    if status not in ("Matched", "Difference Found", "Review Required"):
        raise frappe.ValidationError("Invalid late-sync reconciliation status.")
    frappe.db.set_value(
        PRIMARY_JOURNAL,
        journal.name,
        {
            "reconciliation_status": status,
            "reconciliation_notes": notes,
        },
        update_modified=False,
    )


def reconcile_cashier_shift_close(event_uuid: str) -> Dict[str, Any]:
    _require_primary()
    journal = _journal_for_update(event_uuid)
    if journal.event_family != CASHIER_SHIFT_CLOSE_FAMILY:
        raise frappe.ValidationError(
            "Journal is not a preserved Cashier Shift Close intent."
        )
    if (
        journal.materialization_state != "Canonical Materialized"
        or journal.canonical_doctype != "NKT Cashier Shift"
        or not journal.canonical_name
    ):
        raise frappe.ValidationError(
            "Cashier Shift Close must be canonically materialized before reconciliation."
        )

    payload = _payload(journal)
    shift = frappe.get_doc("NKT Cashier Shift", journal.canonical_name)

    # Physical truth must stay exactly as materialized from Store Edge.
    if abs(flt(shift.actual_cash_count) - flt(payload["actual_cash"])) > TOLERANCE:
        raise frappe.ValidationError(
            "Cashier physical Actual Cash no longer matches immutable offline close."
        )
    if get_datetime(shift.shift_end) != get_datetime(payload["physical_close_datetime"]).replace(tzinfo=None):
        raise frappe.ValidationError(
            "Cashier true physical close time no longer matches immutable offline close."
        )

    current = calculate_shift_summary(shift.name)
    current_expected = flt(current["expected_cash"])
    current_movements = int(current["movement_count"])
    at_count_expected = flt(shift.custom_nkt_expected_cash_at_count)
    at_count_movements = int(shift.custom_nkt_movement_count_at_count or 0)

    expected_delta = current_expected - at_count_expected
    movement_delta = current_movements - at_count_movements
    difference = abs(expected_delta) > TOLERANCE or movement_delta != 0

    if difference:
        status = "Difference Found"
        notes = (
            "Late-sync reconciliation found Primary cash-ledger activity after the offline "
            "Cashier physical close snapshot. "
            f"Expected Cash at close materialization={at_count_expected:.2f}; "
            f"current Primary Expected Cash={current_expected:.2f}; "
            f"delta={expected_delta:.2f}. "
            f"Movement count at close materialization={at_count_movements}; "
            f"current movement count={current_movements}; delta={movement_delta}. "
            "Cashier denomination count, Actual Cash, and true close time remain immutable. "
            "Owner/Admin review or reopen is required under the accepted Shift Close control."
        )
    else:
        status = "Matched"
        notes = (
            "Late-sync reconciliation matched the Primary cash-ledger state to the values "
            "captured when the offline Cashier close materialized. "
            "Cashier physical count remains immutable."
        )

    _set_reconciliation(journal, status=status, notes=notes)
    return {
        "event_uuid": journal.name,
        "event_family": journal.event_family,
        "canonical_shift": shift.name,
        "reconciliation_status": status,
        "owner_admin_review_required": bool(difference),
        "expected_cash_at_close_materialization": at_count_expected,
        "current_primary_expected_cash": current_expected,
        "expected_cash_delta": expected_delta,
        "movement_count_at_close_materialization": at_count_movements,
        "current_primary_movement_count": current_movements,
        "movement_count_delta": movement_delta,
        "cashier_physical_actual_cash": flt(shift.actual_cash_count),
        "cashier_physical_count_rewritten": False,
        "cashier_true_close_time": str(shift.shift_end),
    }


def reconcile_encoder_zout(event_uuid: str) -> Dict[str, Any]:
    _require_primary()
    journal = _journal_for_update(event_uuid)
    if journal.event_family != ENCODER_ZOUT_FINALIZE_FAMILY:
        raise frappe.ValidationError(
            "Journal is not a preserved official Encoder Z-Out intent."
        )
    if (
        journal.materialization_state != "Canonical Materialized"
        or journal.canonical_doctype != "NKT Encoder Z-Out"
        or not journal.canonical_name
    ):
        raise frappe.ValidationError(
            "Official Encoder Z-Out must be canonically materialized before reconciliation."
        )

    payload = _payload(journal)
    official = frappe.get_doc("NKT Encoder Z-Out", journal.canonical_name)
    if str(official.snapshot_hash or "").lower() != payload["snapshot_sha256"]:
        raise frappe.ValidationError(
            "Official offline Encoder Z-Out snapshot hash was rewritten."
        )
    if str(official.snapshot_json or "") != payload["snapshot_json"]:
        raise frappe.ValidationError(
            "Official offline Encoder Z-Out snapshot content was rewritten."
        )

    frozen = json.loads(payload["snapshot_json"])
    options = frozen.get("options") if isinstance(frozen, dict) else {}
    if not isinstance(options, dict):
        options = {}

    live = build_zout_data(
        company=payload["company"],
        business_date=payload["business_date"],
        encoder=payload["encoder"],
        start_time=_time(payload["start_datetime"]),
        end_time=_time(payload["effective_end_datetime"]),
        include_reconciliation_exceptions=int(
            bool(options.get("include_reconciliation_exceptions", True))
        ),
        include_inventory_appendix=int(
            bool(options.get("include_inventory_appendix", False))
        ),
    )

    frozen_stable = _stable_zout_projection(frozen)
    live_stable = _stable_zout_projection(live)
    frozen_hash = _hash(frozen_stable)
    live_hash = _hash(live_stable)
    difference = frozen_hash != live_hash

    changed_sections = []
    all_keys = sorted(set(frozen_stable) | set(live_stable))
    for key in all_keys:
        if _hash(frozen_stable.get(key)) != _hash(live_stable.get(key)):
            changed_sections.append(key)

    if difference:
        status = "Difference Found"
        notes = (
            "Late-sync reconciliation found Primary data that differs from the official "
            "offline Encoder Z-Out snapshot. "
            f"Changed top-level sections: {', '.join(changed_sections) or '(unspecified)'}. "
            f"Official frozen projection hash={frozen_hash}; "
            f"current Primary projection hash={live_hash}. "
            "The original official offline Z-Out snapshot/hash remains immutable; "
            "Primary attaches this reconciliation difference for Owner/Admin review."
        )
    else:
        status = "Matched"
        notes = (
            "Late-sync reconciliation matched the current Primary Z-Out projection to the "
            "official offline snapshot after excluding generation-only metadata. "
            "The official offline snapshot/hash remains immutable."
        )

    _set_reconciliation(journal, status=status, notes=notes)
    return {
        "event_uuid": journal.name,
        "event_family": journal.event_family,
        "canonical_zout": official.name,
        "reconciliation_status": status,
        "owner_admin_review_required": bool(difference),
        "official_snapshot_sha256": payload["snapshot_sha256"],
        "official_snapshot_rewritten": False,
        "frozen_reconciliation_projection_sha256": frozen_hash,
        "current_primary_projection_sha256": live_hash,
        "changed_sections": changed_sections,
    }


def reconcile_preserved_offline_close(event_uuid: str) -> Dict[str, Any]:
    _require_primary()
    family = frappe.db.get_value(PRIMARY_JOURNAL, str(event_uuid), "event_family")
    if family == CASHIER_SHIFT_CLOSE_FAMILY:
        return reconcile_cashier_shift_close(event_uuid)
    if family == ENCODER_ZOUT_FINALIZE_FAMILY:
        return reconcile_encoder_zout(event_uuid)
    raise frappe.ValidationError(
        "Only preserved Cashier Shift Close and official Encoder Z-Out events are reconcilable."
    )


@frappe.whitelist()
def reconcile(event_uuid):
    return reconcile_preserved_offline_close(event_uuid)
